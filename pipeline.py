"""
Woodson Equity — Carveout Sourcing Pipeline
Full A1 → A2 → A3 run with backtest fixes applied.

Backtest fixes (applied here):
  1. Serial divester +1: companies that have completed a prior divestiture
     in the past 5 years get +1 to Parent_Score (max 10 cap still applies)
  2. Language-signal tier override: minimum Tier 3 when Seg_Timing_Signal
     or Co_Timing_Signal is EXPLORATORY or PENDING, regardless of propensity
"""

import os
import re
import time
import logging
from datetime import datetime

import requests
import pandas as pd

from conditions_engine import run_batch as run_a1_batch
from language_engine import run_language_batch
from quarterly_signals import run_quarterly_batch

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("pipeline")

HEADERS = {"User-Agent": os.environ.get("SEC_USER_AGENT", "WoodsonEquity research@woodsonequity.com"), "Accept-Encoding": "gzip, deflate"}
EDGAR_BASE = "https://www.sec.gov"
DATA_BASE = "https://data.sec.gov"


# ---------------------------------------------------------------------------
# Backtest fix 1: Serial Divester detection
# ---------------------------------------------------------------------------

def _check_serial_divester(cik: str) -> bool:
    """
    Returns True if this company filed an 8-K Item 2.01 (completion of
    acquisition or disposition) in the past 5 years — indicating a prior
    divestiture track record.
    """
    if not cik:
        return False
    try:
        url = f"{DATA_BASE}/submissions/CIK{str(cik).zfill(10)}.json"
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        subs = r.json()
        time.sleep(0.12)

        recent = subs.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        items = recent.get("items", [])  # Item field in 8-K
        dates = recent.get("filingDate", [])

        cutoff = "2020-01-01"
        for form, item, date in zip(forms, items, dates):
            if form == "8-K" and date >= cutoff:
                # Item 2.01 = completion of acquisition or disposition
                if "2.01" in str(item):
                    return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# A3: Tiering + Mandate Fit
# ---------------------------------------------------------------------------

WOODSON_MANDATE = {
    "min_seg_rev_M": 75,
    "min_ebitda_M": 6,
    "min_equity_M": 15,
    "max_ev_M": 300,
}


def _mandate_fit(row: pd.Series) -> str:
    rev = row.get("Revenue_M")
    if rev is None or pd.isna(rev):
        return "UNKNOWN"
    if rev < WOODSON_MANDATE["min_seg_rev_M"]:
        return "TOO_SMALL"
    # Rough EBITDA proxy: use OpIncome if available
    op = row.get("OpIncome_M")
    if op is not None and not pd.isna(op) and op < WOODSON_MANDATE["min_ebitda_M"]:
        return "EBITDA_MISS"
    # Upper bound: segment rev > 300M pushes EV likely above mandate
    if rev > 400:
        return "TOO_BIG"
    return "FIT"


def assign_tiers(df: pd.DataFrame, apply_serial_divester: bool = True) -> pd.DataFrame:
    """
    Assign Tier, Mandate_Fit to each row. Applies backtest fixes in-place.
    Tier logic:
      Tier 1 = Propensity >= 12 AND timing signal EXPLORATORY or PENDING
      Tier 2 = Propensity >= 12 AND timing signal NONE
      Tier 3 = Propensity 8-11 AND timing signal EXPLORATORY or PENDING
            OR override: any propensity with EXPLORATORY/PENDING (language override fix)
      Watchlist = completed timing signal (deal already announced)
      Drop = propensity < 8 and no signal
    """
    # --- Serial divester fix: compute once per company ---
    if apply_serial_divester:
        log.info("Checking serial divester status...")
        divester_cache = {}
        for _, row in df.drop_duplicates("CIK").iterrows():
            cik = row.get("CIK")
            if cik and str(cik) not in divester_cache:
                is_serial = _check_serial_divester(str(cik))
                divester_cache[str(cik)] = is_serial
                if is_serial:
                    log.info(f"  Serial divester: {row['Company']}")

        def _add_serial(row):
            cik = str(row.get("CIK", ""))
            if divester_cache.get(cik, False):
                return min(10, row["Parent_Score"] + 1)
            return row["Parent_Score"]

        df = df.copy()
        df["Parent_Score"] = df.apply(_add_serial, axis=1)
        df["Propensity_Score"] = df.apply(
            lambda r: min(20, r["Parent_Score"] + r.get("Seg_Score", 0)), axis=1
        )

    # --- Tier assignment ---
    def _tier(row):
        prop = row.get("Propensity_Score", 0)
        co_sig = str(row.get("Co_Timing_Signal", "NONE")).upper()
        seg_sig = str(row.get("Seg_Timing_Signal", "NONE")).upper()

        # Completed = already announced, watch for close
        if "COMPLETED" in co_sig or "COMPLETED" in seg_sig:
            return "Watchlist"

        active_signal = any(s in (co_sig, seg_sig) for s in ("EXPLORATORY", "PENDING"))

        if prop >= 12 and active_signal:
            return "Tier 1"
        if prop >= 12:
            return "Tier 2"
        if prop >= 8 and active_signal:
            return "Tier 3"
        # Language override fix: any propensity with active signal → Tier 3 minimum
        if active_signal:
            return "Tier 3"
        if prop < 8:
            return "Drop"
        return "Tier 3"

    df["Tier"] = df.apply(_tier, axis=1)
    df["Mandate_Fit"] = df.apply(_mandate_fit, axis=1)

    return df


# ---------------------------------------------------------------------------
# Company list loaders — one per spreadsheet tab
# ---------------------------------------------------------------------------

def _parse_companies(df: pd.DataFrame, name_hints: list, rev_hints: list) -> list:
    """
    Generic helper: find company name + revenue columns by keyword hints,
    return list of (name, revenue_M) tuples, deduplicating by name.
    """
    df.columns = [str(c).strip() for c in df.columns]

    def _col_match(col, hints):
        c = str(col).lower().strip()
        return any(c == h or c.startswith(h + " ") or c.endswith(" " + h) or f" {h} " in c
                   for h in hints) and not c.startswith("unnamed")

    name_col = next(
        (c for c in df.columns if _col_match(c, name_hints)),
        df.columns[0]
    )
    rev_col = next(
        (c for c in df.columns if _col_match(c, rev_hints)),
        None
    )

    seen = set()
    companies = []
    for _, row in df.iterrows():
        name = str(row[name_col]).strip()
        if not name or name == "nan" or name.lower() in ("company name", "name", "company"):
            continue
        if name in seen:
            continue
        seen.add(name)

        rev = 0
        if rev_col:
            try:
                raw = str(row[rev_col]).replace(",", "").replace("$", "").replace("B", "").strip()
                rev = float(raw)
                # Forbes uses billions; Fortune uses millions — normalise
                if rev < 500:          # likely billions
                    rev = rev * 1000
            except Exception:
                rev = 0
        companies.append((name, rev))

    return companies


def load_fortune1000(path: str) -> list:
    """Current Fortune 1000 (primary run list)."""
    df = pd.read_excel(path, sheet_name="Fortune 1000", skiprows=3)
    return _parse_companies(df,
        name_hints=["company name", "company"],
        rev_hints=["revenue", "revenues"],
    )


def load_fortune1000_last_year(path: str) -> list:
    """
    Prior year Fortune 1000 — companies marked (DI) dropped off the list,
    often because they were acquired or revenues fell. Worth scanning for
    carveout signals that preceded the exit.
    """
    df = pd.read_excel(path, sheet_name="Fortune 1000 - Last Year's", skiprows=3)
    return _parse_companies(df,
        name_hints=["company"],
        rev_hints=["revenue", "revenues", "sales"],
    )


def load_forbes_us(path: str) -> list:
    """Forbes Global 2000 — US-headquartered companies."""
    df = pd.read_excel(path, sheet_name="Forbes Global US HQ", skiprows=5)
    return _parse_companies(df,
        name_hints=["name", "company"],
        rev_hints=["sales", "revenue"],
    )


def load_forbes_non_us(path: str) -> list:
    """
    Forbes Global 2000 — non-US companies with US subsidiaries.
    These file 20-F not 10-K, so XBRL segment parsing may be limited.
    Still worth scanning for 8-K and activist signals.
    """
    df = pd.read_excel(path, sheet_name="Forbes Global NON-US HQ", skiprows=4)
    df.columns = [str(c).strip() for c in df.columns]

    # Column structure: [blank, rank, name, hq, industry, sales, profit, assets]
    name_col = df.columns[2] if len(df.columns) > 2 else df.columns[0]
    rev_col  = df.columns[5] if len(df.columns) > 5 else None

    seen = set()
    companies = []
    for _, row in df.iterrows():
        name = str(row[name_col]).strip()
        if not name or name == "nan":
            continue
        if name in seen:
            continue
        seen.add(name)

        rev = 0
        if rev_col:
            try:
                raw = str(row[rev_col]).replace(",", "").replace("$", "").replace("B", "").strip()
                rev = float(raw) * 1000  # Forbes reports in billions
            except Exception:
                rev = 0
        companies.append((name, rev))

    return companies


def load_all_tabs(path: str, exclude_non_us: bool = False) -> dict:
    """
    Load all tabs from the CORPORATE LIST spreadsheet.
    Returns dict: {tab_name: [(company, revenue_M), ...]}

    NOTE: No name-based deduplication is done here. Each tab runs independently
    through the pipeline and CIK-based deduplication happens at output time
    (same CIK = same company regardless of name spelling differences).
    """
    tabs = {}

    loaders = [
        ("Fortune 1000",            load_fortune1000),
        ("Fortune 1000 Last Year",  load_fortune1000_last_year),
        ("Forbes Global US HQ",     load_forbes_us),
    ]
    if not exclude_non_us:
        loaders.append(("Forbes Global NON-US HQ", load_forbes_non_us))

    for tab_name, loader in loaders:
        try:
            companies = loader(path)
        except Exception as e:
            log.warning(f"Could not load tab '{tab_name}': {e}")
            companies = []

        tabs[tab_name] = companies
        log.info(f"  Loaded '{tab_name}': {len(companies)} companies")

    total = sum(len(v) for v in tabs.values())
    log.info(f"  Total companies across all tabs (pre-CIK dedup): {total}")
    return tabs


def merge_pipeline_outputs(output_paths: list) -> "pd.DataFrame":
    """
    Merge multiple pipeline output files (from different tabs) into one
    master ranked list, deduplicating by CIK — keeps the row with the
    highest Propensity_Score when the same CIK appears in multiple runs.
    """
    import pandas as pd

    dfs = []
    for path in output_paths:
        try:
            df = pd.read_excel(path, sheet_name="All Companies")
            dfs.append(df)
            log.info(f"  Loaded {len(df)} rows from {path}")
        except Exception as e:
            log.warning(f"  Could not load {path}: {e}")

    if not dfs:
        return pd.DataFrame()

    combined = pd.concat(dfs, ignore_index=True)
    before = len(combined)

    # Deduplicate by CIK: keep highest propensity score per CIK+Segment pair
    combined = combined.sort_values("Propensity_Score", ascending=False)
    combined = combined.drop_duplicates(subset=["CIK", "Segment"], keep="first")
    log.info(f"  Combined: {before} rows → {len(combined)} after CIK+segment dedup")

    # Re-sort
    tier_order = {"Tier 1": 0, "Tier 2": 1, "Tier 3": 2, "Watchlist": 3, "Drop": 4}
    combined["_r"] = combined["Tier"].map(tier_order).fillna(5)
    combined = combined.sort_values(["_r", "Propensity_Score"], ascending=[True, False])
    combined = combined.drop(columns=["_r"])

    return combined


# ---------------------------------------------------------------------------
# Master pipeline runner
# ---------------------------------------------------------------------------

def run_full_pipeline(
    companies: list,
    output_path: str = "/tmp/woodson_full_run.xlsx",
    checkpoint_a1: str = "/tmp/full_run_a1.xlsx",
    checkpoint_a2: str = "/tmp/full_run_a2.xlsx",
    batch_size: int = 50,
    resume_from: int = 0,
) -> pd.DataFrame:
    """
    Run A1 → A2 → A3 on the full company list.
    Checkpoints after each A1 batch and after A2 completes.
    """
    log.info(f"=== Woodson Full Pipeline ===")
    log.info(f"  {len(companies)} companies, starting from index {resume_from}")

    # --- A1: Conditions Engine ---
    companies_to_run = companies[resume_from:]
    all_a1_rows = []

    # Load existing checkpoint if resuming
    if resume_from > 0:
        try:
            existing = pd.read_excel(checkpoint_a1)
            all_a1_rows = [existing]
            log.info(f"  Loaded {len(existing)} existing A1 rows from checkpoint")
        except Exception:
            log.warning("  Could not load A1 checkpoint, starting fresh")
            all_a1_rows = []

    for batch_start in range(0, len(companies_to_run), batch_size):
        batch = companies_to_run[batch_start:batch_start + batch_size]
        actual_idx = resume_from + batch_start
        log.info(f"\n--- A1 Batch {batch_start//batch_size + 1}: companies {actual_idx+1}-{actual_idx+len(batch)} ---")

        batch_df = run_a1_batch(batch)
        all_a1_rows.append(batch_df)

        # Save checkpoint after each batch
        checkpoint_df = pd.concat(all_a1_rows, ignore_index=True)
        checkpoint_df.to_excel(checkpoint_a1, index=False)
        log.info(f"  A1 checkpoint saved: {len(checkpoint_df)} rows → {checkpoint_a1}")

    a1_df = pd.concat(all_a1_rows, ignore_index=True) if all_a1_rows else pd.DataFrame()
    log.info(f"\nA1 complete: {len(a1_df)} rows")

    # --- A2: Language Engine (10-K + 8-K items 1.01/2.05/2.06/5.02/7.01/8.01) ---
    log.info("\n--- A2: Language Engine ---")
    a2_df = run_language_batch(a1_df)
    a2_df.to_excel(checkpoint_a2, index=False)
    log.info(f"A2 complete: {len(a2_df)} rows → {checkpoint_a2}")

    # --- A2.5: Quarterly Signals (10-Q + 8-K Items 2.02/2.05/2.06) ---
    log.info("\n--- A2.5: Quarterly Signals ---")
    checkpoint_a25 = checkpoint_a2.replace(".xlsx", "_q.xlsx")
    a2_df = run_quarterly_batch(a2_df)
    a2_df.to_excel(checkpoint_a25, index=False)
    log.info(f"A2.5 complete: {len(a2_df)} rows → {checkpoint_a25}")

    # --- A3: Tiering + Mandate Fit ---
    log.info("\n--- A3: Tiering + Mandate Fit ---")
    final_df = assign_tiers(a2_df, apply_serial_divester=True)

    # Sort by tier priority then propensity
    tier_order = {"Tier 1": 0, "Tier 2": 1, "Tier 3": 2, "Watchlist": 3, "Drop": 4}
    final_df["_tier_rank"] = final_df["Tier"].map(tier_order).fillna(5)
    final_df = final_df.sort_values(["_tier_rank", "Propensity_Score"], ascending=[True, False])
    final_df = final_df.drop(columns=["_tier_rank"])

    # Save final output
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        # Summary: top targets (Tier 1 + 2 + FIT)
        summary = final_df[
            (final_df["Tier"].isin(["Tier 1", "Tier 2"])) &
            (final_df["Mandate_Fit"] == "FIT")
        ].copy()
        summary.to_excel(writer, sheet_name="Priority Targets", index=False)

        # All Tier 1-3
        tier3_up = final_df[final_df["Tier"].isin(["Tier 1", "Tier 2", "Tier 3"])].copy()
        tier3_up.to_excel(writer, sheet_name="Tier 1-3", index=False)

        # Watchlist
        watchlist = final_df[final_df["Tier"] == "Watchlist"].copy()
        watchlist.to_excel(writer, sheet_name="Watchlist", index=False)

        # Full output
        final_df.to_excel(writer, sheet_name="All Companies", index=False)

    log.info(f"\n=== Pipeline Complete ===")
    log.info(f"  Output: {output_path}")
    log.info(f"  Tier 1: {len(final_df[final_df['Tier']=='Tier 1'])} rows")
    log.info(f"  Tier 2: {len(final_df[final_df['Tier']=='Tier 2'])} rows")
    log.info(f"  Tier 3: {len(final_df[final_df['Tier']=='Tier 3'])} rows")
    log.info(f"  Watchlist: {len(final_df[final_df['Tier']=='Watchlist'])} rows")
    log.info(f"  Priority (Tier1-2 + FIT): {len(summary)} rows")

    return final_df


if __name__ == "__main__":
    import sys

    CORP_LIST = "/Users/eshan/Downloads/CORPORATE LIST 2026 FINAL  (1).xlsx"

    # Args: [tab_index] [resume_from]
    # tab_index: 0=Fortune1000, 1=Fortune1000LastYear, 2=ForbesUSHQ, 3=ForbesNonUS
    # Default: run Fortune 1000 from scratch
    tab_idx    = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    resume_from = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    tab_configs = [
        {
            "name":       "Fortune 1000",
            "loader":     load_fortune1000,
            "output":     "/tmp/woodson_fortune1000.xlsx",
            "ckpt_a1":    "/tmp/full_run_a1.xlsx",
            "ckpt_a2":    "/tmp/full_run_a2.xlsx",
        },
        {
            "name":       "Fortune 1000 Last Year",
            "loader":     load_fortune1000_last_year,
            "output":     "/tmp/woodson_fortune1000_lastyear.xlsx",
            "ckpt_a1":    "/tmp/lastyear_a1.xlsx",
            "ckpt_a2":    "/tmp/lastyear_a2.xlsx",
        },
        {
            "name":       "Forbes Global US HQ",
            "loader":     load_forbes_us,
            "output":     "/tmp/woodson_forbes_us.xlsx",
            "ckpt_a1":    "/tmp/forbes_us_a1.xlsx",
            "ckpt_a2":    "/tmp/forbes_us_a2.xlsx",
        },
        {
            "name":       "Forbes Global NON-US HQ",
            "loader":     load_forbes_non_us,
            "output":     "/tmp/woodson_forbes_nonus.xlsx",
            "ckpt_a1":    "/tmp/forbes_nonus_a1.xlsx",
            "ckpt_a2":    "/tmp/forbes_nonus_a2.xlsx",
        },
    ]

    cfg = tab_configs[tab_idx]
    log.info(f"Running tab: {cfg['name']} (resume from index {resume_from})")

    try:
        companies = cfg["loader"](CORP_LIST)
        log.info(f"Loaded {len(companies)} companies from '{cfg['name']}'")
    except Exception as e:
        log.error(f"Could not load '{cfg['name']}': {e}")
        sys.exit(1)

    run_full_pipeline(
        companies=companies,
        output_path=cfg["output"],
        checkpoint_a1=cfg["ckpt_a1"],
        checkpoint_a2=cfg["ckpt_a2"],
        batch_size=50,
        resume_from=resume_from,
    )
