"""
pipeline_incremental.py — refresh the enriched dataset from NEW SEC filings only.

The full scan is ~21h and can't run in CI. This runs INCREMENTALLY: it reads EDGAR's
daily index, keeps only filings by companies in our universe in a relevant form, and
re-scans just those companies — a handful per day, minutes not hours.

Flow:
  data/universe.csv (Company, CIK, Revenue_M)  +  data/state.json (highWater)
    -> pull EDGAR daily master.idx for each business day since highWater
    -> keep filings by our CIKs in RELEVANT_FORMS  (8-K item filtering happens in the
       re-scan; the index only exposes form type)
    -> re-scan affected companies through the full per-company chain (reuse build_pipeline)
    -> merge fresh rows into data/woodson_enriched.xlsx (replace those companies)
    -> advance highWater, record coverage in state.json

Robust for unattended cron: one index/day, backoff on 429/5xx, 404 (weekend/holiday)
skipped, backfill capped per run (the next run catches up). CI-safe: no ~/Downloads;
SEC_USER_AGENT env sets the fair-access User-Agent. Called by refresh.py.
"""
import os
import json
import time
import logging
import datetime as dt
from pathlib import Path

import requests
import pandas as pd

import pipeline
import build_pipeline as bp

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("incremental")

REPO = Path(__file__).parent
ENRICHED = REPO / "data" / "woodson_enriched.xlsx"
UNIVERSE = REPO / "data" / "universe.csv"
STATE = REPO / "data" / "state.json"
BUILD = REPO / "data" / "build"

UA = {"User-Agent": os.environ.get("SEC_USER_AGENT", "WoodsonEquity research@woodsonequity.com"),
      "Accept-Encoding": "gzip, deflate"}
# forms that can carry a divestiture signal (8-K items are filtered during the re-scan)
RELEVANT = {"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A", "SC 13D", "SC 13D/A", "DEF 14A"}
DAILY = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/master.{d}.idx"
MAX_BACKFILL_DAYS = 21          # cap per run; cron catches up over subsequent runs
SEED_LOOKBACK_DAYS = 2          # safety overlap when seeding highWater from the file date


def _get(url, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=30)
        except requests.RequestException:
            time.sleep(1.5 * (i + 1)); continue
        if r.status_code == 200:
            return r
        if r.status_code == 404:
            return None                      # no index that day (weekend/holiday)
        time.sleep(2.0 * (i + 1))            # 429 / 5xx backoff
    return None


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    seed = dt.date.fromtimestamp(ENRICHED.stat().st_mtime) - dt.timedelta(days=SEED_LOOKBACK_DAYS)
    return {"highWater": seed.isoformat(), "lastRun": None, "runs": 0}


def scan_daily_index(since: dt.date, until: dt.date, cikset: set):
    """Return (hits{cikz:set(forms)}, byform, index_days, filings) over (since, until]."""
    hits, byform, index_days, filings = {}, {}, 0, 0
    day = since + dt.timedelta(days=1)
    while day <= until:
        if day.weekday() < 5:                # skip weekends
            q = (day.month - 1) // 3 + 1
            r = _get(DAILY.format(y=day.year, q=q, d=day.strftime("%Y%m%d")))
            if r:
                index_days += 1
                for line in r.text.splitlines():
                    parts = line.split("|")
                    if len(parts) != 5:
                        continue
                    cik, _name, form, _date, _fn = parts
                    cik = cik.strip(); form = form.strip()
                    if not cik.isdigit():
                        continue
                    cikz = cik.zfill(10)
                    if cikz in cikset and form in RELEVANT:
                        hits.setdefault(cikz, set()).add(form)
                        byform[form] = byform.get(form, 0) + 1
                        filings += 1
                time.sleep(0.15)             # fair-access throttle
        day += dt.timedelta(days=1)
    return hits, byform, index_days, filings


def rescan(affected_names, name2rev):
    """Re-scan the affected companies through the full per-company chain -> enriched rows."""
    companies = [(n, name2rev.get(n, 0)) for n in affected_names]
    BUILD.mkdir(parents=True, exist_ok=True)
    scan = pipeline.run_full_pipeline(
        companies, output_path=str(BUILD / "inc_scan.xlsx"),
        checkpoint_a1=str(BUILD / "inc_a1.xlsx"), checkpoint_a2=str(BUILD / "inc_a2.xlsx"),
        batch_size=50)
    df = bp.stage_enrich(scan)
    df = bp.stage_region(df)
    df = bp.stage_links(df)
    df = bp.stage_upgrade(df)
    df = bp.stage_fingerprint(df)
    return df


def main():
    uni = pd.read_csv(UNIVERSE, dtype=str)
    uni["cikz"] = uni["CIK"].str.replace(r"\.0$", "", regex=True).str.zfill(10)
    cikset = set(uni["cikz"])
    cik2name = dict(zip(uni["cikz"], uni["Company"]))
    name2rev = {r["Company"]: (float(r["Revenue_M"]) if str(r.get("Revenue_M")) not in ("nan", "None", "")
                               else 0) for _, r in uni.iterrows()}

    st = load_state()
    since = dt.date.fromisoformat(st["highWater"])
    until = dt.date.today()
    if (until - since).days > MAX_BACKFILL_DAYS:
        until = since + dt.timedelta(days=MAX_BACKFILL_DAYS)
    if until <= since:
        log.info("[incremental] nothing new since highWater; up to date.")
        return

    log.info(f"[incremental] daily index {since}..{until} across {len(cikset)} universe CIKs")
    hits, byform, index_days, filings = scan_daily_index(since, until, cikset)
    affected = [cik2name[c] for c in hits if c in cik2name]
    log.info(f"[incremental] {filings} relevant filings over {index_days} index days "
             f"-> {len(affected)} affected companies {byform}")

    if affected:
        fresh = rescan(affected, name2rev)
        existing = pd.read_excel(ENRICHED, engine="openpyxl")
        keep = existing[~existing["Company"].isin(affected)]
        merged = pd.concat([keep, fresh.reindex(columns=existing.columns)], ignore_index=True)
        merged.to_excel(ENRICHED, index=False)
        log.info(f"[incremental] merged {fresh['Company'].nunique()} refreshed companies; "
                 f"dataset now {merged['Company'].nunique()} companies")

    st["highWater"] = until.isoformat()
    st["lastRun"] = dt.datetime.utcnow().isoformat() + "Z"
    st["runs"] = st.get("runs", 0) + 1
    st["lastCoverage"] = {"filingsProcessed": filings, "indexDays": index_days,
                          "byForm": byform, "affected": len(affected),
                          "window": f"{since.isoformat()}..{until.isoformat()}"}
    STATE.write_text(json.dumps(st, indent=2))
    log.info(f"[incremental] highWater -> {until.isoformat()}")


if __name__ == "__main__":
    main()
