"""
build_pipeline.py — the ONE durable, resumable build of the screener dataset.

Replaces the scatter of ad-hoc /tmp runner scripts (tag_region, build_factor_links,
patch_guidance, apply_fingerprint) that were lost when /tmp was wiped. Every stage
checkpoints into  data/build/  (inside the repo, not /tmp), so a crash resumes
instead of restarting, and the artifacts survive a machine cleanup.

Chain:
  1. SCAN      pipeline.run_full_pipeline per tab   (the long pole — heavy EDGAR I/O)
  2. MERGE     merge_pipeline_outputs -> master
  3. ENRICH    match-quality gate, YoY sanitation, C-suite recheck, evidence links, retier
  4. REGION    US / Non-US tagging + honest segment-data provenance
  5. LINKS     direct-document factor links (R-files, 13D, dated 8-Ks)
  6. UPGRADE   held-for-sale, verified serial-divester, deleveraging intent
  7. FINGERPRINT  carveout-candidate DIVISION per company (v2, pure) -> FP_* cols
  ->           data/woodson_enriched.xlsx   (what the dashboard reads)

Usage:
  python3 build_pipeline.py                 # tabs 0,1,2 (skip parked Non-US); full
  python3 build_pipeline.py --limit 20      # smoke test: 20 companies per tab
  python3 build_pipeline.py --tabs 0,1,2,3  # include Forbes Non-US
  python3 build_pipeline.py --resume        # skip stages whose output already exists
"""

import argparse
import json
import logging
import re
import shutil
import time
from pathlib import Path

import pandas as pd

import pipeline
import enrich_master as em
import factor_links as fl
import screener_upgrade as su
import evidence
import fingerprint_pick as fpk

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("build")

REPO = Path(__file__).parent
BUILD = REPO / "data" / "build"
FINAL = REPO / "data" / "woodson_enriched.xlsx"
CORP_LIST = "/Users/eshan/Downloads/CORPORATE LIST 2026 FINAL  (1).xlsx"
FOREIGN = REPO / "foreign_companies_combined.csv"

TABS = [
    ("Fortune 1000", pipeline.load_fortune1000, "fortune1000"),
    ("Fortune 1000 Last Year", pipeline.load_fortune1000_last_year, "fortune_ly"),
    ("Forbes Global US HQ", pipeline.load_forbes_us, "forbes_us"),
    ("Forbes Global NON-US HQ", pipeline.load_forbes_non_us, "forbes_nonus"),
]


# ── Stage 1: scan ───────────────────────────────────────────────────────────

def stage_scan(tab_idx, limit=None, resume=False, only=None):
    name, loader, slug = TABS[tab_idx]
    out = BUILD / f"scan_{slug}.xlsx"
    if resume and out.exists():
        log.info(f"[scan] {name}: using existing {out.name}")
        return out
    companies = loader(CORP_LIST)
    if only:                    # keep companies whose name contains any target token
        toks = [t.strip().lower() for t in only if t.strip()]
        companies = [c for c in companies if any(t in str(c[0]).lower() for t in toks)]
    if limit:
        companies = companies[:limit]
    log.info(f"[scan] {name}: {len(companies)} companies")
    pipeline.run_full_pipeline(
        companies=companies, output_path=str(out),
        checkpoint_a1=str(BUILD / f"ckpt_{slug}_a1.xlsx"),
        checkpoint_a2=str(BUILD / f"ckpt_{slug}_a2.xlsx"),
        batch_size=50, resume_from=0)
    return out


# ── Stage 2: merge ──────────────────────────────────────────────────────────

def stage_merge(scan_paths):
    master = pipeline.merge_pipeline_outputs([str(p) for p in scan_paths])
    p = BUILD / "master.xlsx"
    master.to_excel(p, index=False)
    log.info(f"[merge] {len(master)} rows, {master['Company'].nunique()} companies -> {p.name}")
    return master


# ── Stage 3: enrich (reuse enrich_master's vetted functions) ────────────────

def stage_enrich(master):
    df = master.copy()
    for col in ["Propensity_Score", "Parent_Score", "P_CsuiteChange"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    df = em.add_match_quality(df)      # fixed identity gate (accents/apostrophes/first-token)
    df = em.add_yoy_flags(df)
    df = em.recheck_csuite(df)         # Item 5.02 verified against filing body
    df = em.retier(df)
    df = em.build_evidence(df)         # #:~:text= deep-links to the triggering sentence
    p = BUILD / "enriched_core.xlsx"
    df.to_excel(p, index=False)
    log.info(f"[enrich] {len(df)} rows -> {p.name}")
    return df


# ── Stage 4: region + honest segment provenance ─────────────────────────────

def stage_region(df):
    foreign = set(pd.read_csv(FOREIGN)["company_name"].astype(str).str.strip())
    canon = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())
    fset = {canon(f) for f in foreign}
    df["Region"] = df["Company"].apply(lambda c: "Non-US" if canon(c) in fset else "US")

    def coverage(r):
        if r["Match_Quality"] == "UNRESOLVED":
            return "Not covered"
        return "Non-US filer" if r["Region"] == "Non-US" else "Covered"
    df["Coverage"] = df.apply(coverage, axis=1)

    # Honest provenance: do we actually HAVE segment financials for this company?
    seg_rev = pd.to_numeric(df["Revenue_M"], errors="coerce")
    has_seg = df.assign(_r=seg_rev).groupby("Company")["_r"].apply(lambda s: s.notna().any())
    df["Seg_Data_Source"] = df["Company"].map(
        lambda c: "Segment financials extracted" if has_seg.get(c, False)
        else "No segment breakout reported")
    log.info(f"[region] US={int((df.drop_duplicates('Company')['Region']=='US').sum())} "
             f"Non-US={int((df.drop_duplicates('Company')['Region']=='Non-US').sum())}")
    return df


# ── Stage 5: direct-document factor links ───────────────────────────────────

def stage_links(df):
    mask = df["Tier"].isin(["Tier 1", "Tier 2", "Tier 3", "Watchlist"]) & \
           df["Match_Quality"].isin(["VERIFIED", "REVIEW"])
    todo = df[mask].drop_duplicates("Company")
    log.info(f"[links] resolving factor documents for {len(todo)} companies…")
    by_co = {}
    for i, (_, r) in enumerate(todo.iterrows(), 1):
        subs = fl.get_submissions(r["CIK"])
        by_co[r["Company"]] = fl.build_factor_links(r, subs)
        if i % 50 == 0:
            log.info(f"  [links] …{i}/{len(todo)}")
    df["Factor_Links_JSON"] = df["Company"].map(lambda c: json.dumps(by_co.get(c, {})))
    p = BUILD / "enriched_links.xlsx"
    df.to_excel(p, index=False)
    return df


# ── Stage 6: screener upgrade (HFS / serial-divester / deleveraging) ────────

def stage_upgrade(df):
    p = BUILD / "enriched_links.xlsx"
    df.to_excel(p, index=False)
    su.F = str(p)                      # point the upgrade at our build file
    su.run()                           # writes HFS_*, Serial_Divester_*, Deleveraging_* back into p
    return pd.read_excel(p, engine="openpyxl")


# ── Stage 7: carveout candidate via Divestiture Fingerprint v2 (pure) ────────

def stage_fingerprint(df):
    """Pick the carveout-candidate DIVISION per company with fingerprint v2
    (strategic-pruner vs balance-sheet-forced). Pure — no EDGAR I/O."""
    df = fpk.add_fingerprint(df)
    named = df.drop_duplicates("Company")["FP_Candidate_Segment"].notna().sum()
    log.info(f"[fingerprint] carveout candidate named for {int(named)} companies")
    return df


# ── Orchestrate ─────────────────────────────────────────────────────────────

def main(tabs, limit, resume, only=None):
    BUILD.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    scan_paths = [stage_scan(i, limit=limit, resume=resume, only=only) for i in tabs]
    master = stage_merge(scan_paths)
    df = stage_enrich(master)
    df = stage_region(df)
    df = stage_links(df)
    df = stage_upgrade(df)
    df = stage_fingerprint(df)

    df.to_excel(FINAL, index=False)
    # keep a /tmp copy too so anything still pointing there keeps working
    try:
        shutil.copy(FINAL, "/tmp/woodson_enriched.xlsx")
    except Exception:
        pass
    co = df.drop_duplicates("Company")
    log.info(f"\n=== BUILD COMPLETE in {(time.time()-t0)/60:.1f} min ===")
    log.info(f"  {FINAL}")
    log.info(f"  companies: {len(co)}  |  rows: {len(df)}")
    log.info(f"  tiers: {co['Tier'].value_counts().to_dict()}")
    if "HFS_Live" in co.columns:
        log.info(f"  live held-for-sale: {int(co['HFS_Live'].fillna(False).sum())}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tabs", default="0,1,2", help="comma tab indices (default skips Non-US)")
    ap.add_argument("--limit", type=int, default=None, help="cap companies per tab (smoke test)")
    ap.add_argument("--resume", action="store_true", help="skip scan stages already done")
    ap.add_argument("--only", default=None, help="comma-separated company-name tokens to keep")
    a = ap.parse_args()
    only = a.only.split(",") if a.only else None
    main([int(x) for x in a.tabs.split(",")], a.limit, a.resume, only)
