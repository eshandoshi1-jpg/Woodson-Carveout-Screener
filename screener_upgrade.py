"""
Screener Upgrade — wire the corrected engines into the live universe.

Adds the PART 2A/2B signals the deployed screener was missing entirely:

  1. HELD-FOR-SALE (balance sheet)   — the EARLIEST signal in the doc. CS-015's
     PPE was on the balance sheet as HFS a full quarter before the announcement.
     We had no coverage of this at all; prose parsing cannot see it.
  2. SERIAL DIVESTER (18-month)      — PART 2A: any completed divestiture in the
     trailing 18 months raises propensity on ALL remaining non-core assets.
     Our old proxy was "Co_Timing_Signal == COMPLETED", which is much weaker.
  3. DELEVERAGING INTENT             — required to classify the parent as a
     balance-sheet-forced seller, which switches segment ranking from weakness
     to marketability (the CS-008 crown-jewel fix).
  4. TERMINATION / STALE-PENDING     — states the old engine did not have.

Writes the new columns back into /tmp/woodson_enriched.xlsx.
"""

import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

import timing_engine as te

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("upgrade")

F = "/tmp/woodson_enriched.xlsx"
HEADERS = te.HEADERS
EDGAR = te.EDGAR
RATE = 0.12

# Language that marks a parent as a FORCED seller (P2). Proceeds earmarked for
# debt reduction is the signature; CS-008 NN Inc is the archetype.
DELEVER_PATTERNS = [
    r"proceeds\s+(?:will\s+be\s+|to\s+be\s+)?used\s+to\s+(?:repay|pay\s+down|reduce)\s+(?:our\s+)?(?:the\s+)?debt",
    r"pay\s+down\s+(?:our|the)\s+debt",
    r"reduce\s+(?:our\s+)?(?:net\s+)?(?:leverage|indebtedness)",
    r"improve\s+our\s+balance\s+sheet",
    r"de-?lever(?:aging|age)",
    r"proceeds.{0,60}debt\s+(?:repayment|reduction|paydown)",
]


def _get_text(url, timeout=30):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout); r.raise_for_status()
        time.sleep(RATE); return r.text
    except Exception:
        time.sleep(RATE); return ""


def _strip(html):
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"&nbsp;|&#160;", " ", t)
    return re.sub(r"\s+", " ", t)


# ---------------------------------------------------------------------------

def serial_divester(cik: str, subs: dict, as_of: str = None, months: int = 18,
                    verify: bool = True, max_verify: int = 4) -> dict:
    """PART 2A: any COMPLETED DIVESTITURE in the trailing 18 months.

    Item 2.01 is "Completion of Acquisition OR Disposition of Assets", so the raw
    item count conflates buyers with sellers — an acquisitive company would be
    mislabelled a serial divester. We read each 8-K to confirm direction.
    """
    as_of = as_of or datetime.utcnow().strftime("%Y-%m-%d")
    cutoff = (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=months * 30)).strftime("%Y-%m-%d")
    cand = [f for f in te._recent(subs)
            if str(f["form"]).startswith("8-K") and "2.01" in te._items(f)
            and f["filed"] >= cutoff]
    if not verify:
        return {"serial_divester": len(cand) > 0, "count_18mo": len(cand),
                "verified": False, "dates": [c["filed"] for c in cand[:6]]}
    disposals = []
    for f in cand[:max_verify]:
        if te.classify_201_direction(cik, f, _get_text) == "disposition":
            disposals.append(f["filed"])
    return {"serial_divester": len(disposals) > 0, "count_18mo": len(disposals),
            "candidates_2_01": len(cand), "verified": True, "dates": disposals}


def deleveraging_intent(cik: str, subs: dict, max_docs: int = 3) -> dict:
    """Scan recent 8-K/10-Q text for proceeds-to-debt-paydown language (P2)."""
    docs = [f for f in te._recent(subs)
            if str(f["form"]).startswith(("8-K", "10-Q"))][:max_docs]
    for f in docs:
        if not f["doc"]:
            continue
        url = f"{EDGAR}/Archives/edgar/data/{int(cik)}/{f['acc'].replace('-','')}/{f['doc']}"
        txt = _strip(_get_text(url)).lower()
        if not txt:
            continue
        for pat in DELEVER_PATTERNS:
            m = re.search(pat, txt)
            if m:
                s = max(0, m.start() - 120)
                return {"deleveraging_intent": True, "filed": f["filed"],
                        "quote": txt[s:m.end() + 120].strip()[:260], "url": url}
    return {"deleveraging_intent": False}


# ---------------------------------------------------------------------------

def run(limit=None, tiers=("Tier 1", "Tier 2", "Tier 3", "Watchlist"), do_delever=True):
    df = pd.read_excel(F, engine="openpyxl")
    mask = (df["Region"] == "US") & df["Match_Quality"].isin(["VERIFIED", "REVIEW"]) \
           & df["Tier"].isin(tiers)
    todo = df[mask].drop_duplicates("Company")
    if limit:
        todo = todo.head(limit)
    log.info(f"Upgrading {len(todo)} US identity-verified companies…")

    out = {}
    for i, (_, r) in enumerate(todo.iterrows(), 1):
        cik = te.pad_cik(r["CIK"])
        if not cik:
            continue
        rec = {}
        try:
            # 1. held-for-sale from the balance sheet (the earliest signal)
            hfs = te.detect_held_for_sale(cik, since="2023-01-01")
            if hfs:
                rec["HFS_Present"] = True
                rec["HFS_First_Filed"] = hfs[0]["filed"]
                rec["HFS_Latest_Filed"] = hfs[-1]["filed"]
                rec["HFS_Value_M"] = round(max(h["value_usd"] for h in hfs) / 1e6, 1)
                rec["HFS_Concept"] = hfs[0]["concept"]
                rec["HFS_Form"] = hfs[0]["form"]
            else:
                rec["HFS_Present"] = False

            subs = te._get_json(f"{te.DATA}/submissions/CIK{cik}.json") or {}
            # 2. serial divester (18 months)
            sd = serial_divester(cik, subs)
            rec["Serial_Divester_18mo"] = sd["serial_divester"]
            rec["Serial_Divester_Count"] = sd["count_18mo"]
            rec["Disposal_2_01_Candidates"] = sd.get("candidates_2_01", 0)
            # 4. terminations
            terms = te.detect_terminations(subs)
            rec["Termination_8K"] = bool(terms)
            rec["Termination_Date"] = terms[-1]["filed"] if terms else ""
            # withdrawn separations (CS-008 signal)
            wd = te.detect_withdrawals(subs)
            rec["Withdrawn_Separation"] = bool(wd)
            # 3. deleveraging intent -> forced-seller archetype
            if do_delever:
                dl = deleveraging_intent(cik, subs)
                rec["Deleveraging_Intent"] = dl["deleveraging_intent"]
                rec["Deleveraging_Quote"] = dl.get("quote", "")
                rec["Deleveraging_URL"] = dl.get("url", "")
        except Exception as e:
            log.debug(f"{r['Company']}: {e}")
        out[r["Company"]] = rec
        if i % 25 == 0:
            log.info(f"  …{i}/{len(todo)}   hfs={sum(1 for v in out.values() if v.get('HFS_Present'))}"
                     f"  serial={sum(1 for v in out.values() if v.get('Serial_Divester_18mo'))}"
                     f"  delever={sum(1 for v in out.values() if v.get('Deleveraging_Intent'))}")
            _flush(df, out)

    _flush(df, out)
    n = len(out)
    log.info(f"DONE — {n} companies upgraded")
    log.info(f"  held-for-sale on balance sheet : {sum(1 for v in out.values() if v.get('HFS_Present'))}")
    log.info(f"  serial divester (18mo)         : {sum(1 for v in out.values() if v.get('Serial_Divester_18mo'))}")
    log.info(f"  deleveraging intent (forced)   : {sum(1 for v in out.values() if v.get('Deleveraging_Intent'))}")
    log.info(f"  termination 8-K on file        : {sum(1 for v in out.values() if v.get('Termination_8K'))}")
    log.info(f"  withdrawn separation           : {sum(1 for v in out.values() if v.get('Withdrawn_Separation'))}")


COLS = ["Disposal_2_01_Candidates", "HFS_Present", "HFS_First_Filed", "HFS_Latest_Filed", "HFS_Value_M", "HFS_Concept",
        "HFS_Form", "Serial_Divester_18mo", "Serial_Divester_Count", "Termination_8K",
        "Termination_Date", "Withdrawn_Separation", "Deleveraging_Intent",
        "Deleveraging_Quote", "Deleveraging_URL"]


def _flush(df, out):
    for c in COLS:
        df[c] = df["Company"].map(lambda co: out.get(co, {}).get(c))
    df.to_excel(F, index=False)


if __name__ == "__main__":
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(limit=lim)
