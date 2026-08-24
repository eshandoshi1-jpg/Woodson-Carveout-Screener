"""
Enrich Master Pipeline Output
=============================

Post-processes the merged pipeline output into a demo-grade dataset:

  1. Match quality gate      — flags phantom fuzzy matches (MRC→DMC) as UNRESOLVED
  2. Revenue YoY sanitation  — marks acquisition-distorted YoY (>75% swing) n/m
  3. Segment data provenance — records whether segments came from XBRL dims
  4. C-suite re-check        — re-runs the *fixed* Item 5.02 logic on headline
                               targets (old logic fired on ~100% of companies)
  5. Evidence deep-links     — precomputes #:~:text= links to the exact passage
                               behind each language/quarterly signal

Writes /tmp/woodson_enriched.xlsx (+ Evidence_JSON column).
"""

import json
import logging
import re

import pandas as pd

import evidence
from evidence import classify_match, parse_language_hits, first_url, evidence_for_keyword

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("enrich")

SRC = "/tmp/woodson_master.xlsx"
OUT = "/tmp/woodson_enriched.xlsx"

# YoY beyond this absolute % is treated as an acquisition/segment artifact, not organic
YOY_MEANINGFUL_LIMIT = 75.0


def pad_cik(cik) -> str:
    try:
        return str(int(float(cik))).zfill(10)
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# 1. Match quality
# ---------------------------------------------------------------------------

def add_match_quality(df: pd.DataFrame) -> pd.DataFrame:
    df["Match_Quality"] = df.apply(
        lambda r: classify_match(r["Company"], r.get("Matched_SEC_Name"),
                                 r.get("Match_Score"), r.get("Ticker")),
        axis=1,
    )
    n = df.drop_duplicates("Company")["Match_Quality"].value_counts().to_dict()
    log.info(f"Match quality (unique cos): {n}")
    return df


# ---------------------------------------------------------------------------
# 2. Revenue YoY sanitation
# ---------------------------------------------------------------------------

def parse_parent_yoy(notes: str):
    m = re.search(r"rev trend:\s*([+\-]?[\d\.]+)%\s*YoY", str(notes))
    return float(m.group(1)) if m else None


def add_yoy_flags(df: pd.DataFrame) -> pd.DataFrame:
    yoy = df["Parent_Notes"].apply(parse_parent_yoy)
    df["Parent_YoY_pct"] = yoy
    df["YoY_Meaningful"] = yoy.apply(
        lambda v: bool(v is not None and abs(v) <= YOY_MEANINGFUL_LIMIT)
    )
    distorted = int(((yoy.notna()) & (yoy.abs() > YOY_MEANINGFUL_LIMIT)).sum())
    log.info(f"Revenue YoY flagged non-meaningful (acquisition-distorted): {distorted} rows")
    return df


# ---------------------------------------------------------------------------
# 3. Segment data provenance
# ---------------------------------------------------------------------------

def add_segment_provenance(df: pd.DataFrame) -> pd.DataFrame:
    def src(notes):
        m = re.search(r"segments:\s*(\d+)\s*from XBRL dims", str(notes))
        if m:
            return "XBRL dimensions" if int(m.group(1)) > 0 else "Text extraction (no XBRL segments)"
        m2 = re.search(r"segments:\s*(\d+)\s*reportable", str(notes))
        if m2:
            return "XBRL dimensions"
        return "Unknown"
    df["Seg_Data_Source"] = df["Parent_Notes"].apply(src)
    return df


# ---------------------------------------------------------------------------
# 4. C-suite re-check with fixed logic (headline targets only)
# ---------------------------------------------------------------------------

def recheck_csuite(df: pd.DataFrame, tiers=("Tier 1", "Tier 2")) -> pd.DataFrame:
    import conditions_engine as ce

    df["Csuite_Rechecked"] = False
    targets = (
        df[df["Tier"].isin(tiers) & df["Match_Quality"].isin(["VERIFIED", "REVIEW"])]
        .drop_duplicates("Company")
    )
    log.info(f"Re-checking C-suite (fixed logic) for {len(targets)} headline targets…")

    updates = {}  # company -> (new_pceo, note)
    for _, r in targets.iterrows():
        cik = pad_cik(r["CIK"])
        if not cik:
            continue
        subs = None
        for _attempt in range(3):
            subs = ce._get_submissions(cik)
            if subs:
                break
            import time as _t; _t.sleep(1.0)
        if not subs:
            log.warning(f"  submissions unavailable after retries: {r['Company']}")
            continue
        try:
            pts, note = ce._score_csuite_change(subs, cik)
        except Exception as e:
            log.debug(f"csuite recheck failed {r['Company']}: {e}")
            continue
        updates[r["Company"]] = (pts, note)
        log.info(f"  {r['Company']:<34} {r.get('P_CsuiteChange',0)} -> {pts}")

    # Apply updates across all rows of each company
    for company, (pts, note) in updates.items():
        mask = df["Company"] == company
        old = df.loc[mask, "P_CsuiteChange"].iloc[0]
        delta = pts - (old if pd.notna(old) else 0)
        df.loc[mask, "P_CsuiteChange"] = pts
        df.loc[mask, "Parent_Score"] = df.loc[mask, "Parent_Score"] + delta
        df.loc[mask, "Propensity_Score"] = df.loc[mask, "Propensity_Score"] + delta
        df.loc[mask, "Csuite_Rechecked"] = True
        # replace the c-suite fragment in Parent_Notes
        df.loc[mask, "Parent_Notes"] = df.loc[mask, "Parent_Notes"].apply(
            lambda s: re.sub(r"c-suite change:[^|]*", note + " ", str(s))
        )

    return df


def retier(df: pd.DataFrame) -> pd.DataFrame:
    """Recompute tiers after score adjustments, matching pipeline thresholds."""
    def tier_of(r):
        p = r["Propensity_Score"]
        sig = str(r.get("Co_Timing_Signal", "NONE"))
        lang_override = int(r.get("Language_Hit_Count", 0) or 0) > 0
        if sig == "COMPLETED":
            return "Watchlist"
        if p >= 12 and sig in ("EXPLORATORY", "PENDING"):
            return "Tier 1"
        if p >= 12:
            return "Tier 2"
        if p >= 8 and (sig in ("EXPLORATORY", "PENDING") or lang_override):
            return "Tier 3"
        return "Drop"
    df["Tier"] = df.apply(tier_of, axis=1)
    return df


# ---------------------------------------------------------------------------
# 5. Evidence deep-links
# ---------------------------------------------------------------------------

def build_evidence(df: pd.DataFrame, max_items=4) -> pd.DataFrame:
    """
    For VERIFIED/REVIEW actionable companies, resolve each language / quarterly
    signal to an exact-passage deep link. Stored as JSON per row.
    """
    actionable = df["Tier"].isin(["Tier 1", "Tier 2", "Tier 3"]) & \
                 df["Match_Quality"].isin(["VERIFIED", "REVIEW"])
    # de-dup work by company (evidence is parent-level)
    todo = df[actionable].drop_duplicates("Company")
    log.info(f"Building evidence deep-links for {len(todo)} companies…")

    ev_by_company = {}
    for i, (_, r) in enumerate(todo.iterrows(), 1):
        items = []
        seen = set()

        def add(url, hits, kind):
            if not url:
                return
            for h in hits:
                kw = h["keyword"]
                key = (kw.lower(), kind)
                if key in seen or len(items) >= max_items:
                    continue
                ev = evidence_for_keyword(url, kw)
                if ev:
                    items.append({
                        "kind": kind, "keyword": kw,
                        "form": h.get("form", ""), "date": h.get("date", ""),
                        "quote": ev["quote"], "url": ev["url"],
                    })
                    seen.add(key)

        add(first_url(r.get("Language_Source_URLs")),
            parse_language_hits(r.get("Language_Hits")), "10-K language")
        add(first_url(r.get("Q_Source_URLs")),
            parse_language_hits(r.get("Q_Language_Hits")), "10-Q/8-K language")

        ev_by_company[r["Company"]] = items
        if i % 25 == 0:
            log.info(f"  …{i}/{len(todo)} ({r['Company']})")

    df["Evidence_JSON"] = df["Company"].map(
        lambda c: json.dumps(ev_by_company.get(c, []))
    )
    total = sum(len(v) for v in ev_by_company.values())
    log.info(f"Evidence items resolved: {total}")
    return df


# ---------------------------------------------------------------------------

def main():
    log.info(f"Loading {SRC}")
    df = pd.read_excel(SRC)
    for col in ["Propensity_Score", "Parent_Score", "P_CsuiteChange"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    df = add_match_quality(df)
    df = add_yoy_flags(df)
    df = add_segment_provenance(df)
    df = recheck_csuite(df)
    df = retier(df)
    df = build_evidence(df)

    df.to_excel(OUT, index=False)
    log.info(f"Wrote {OUT}  ({len(df)} rows)")

    # Summary
    co = df.drop_duplicates("Company")
    log.info("Tier x Match_Quality (unique companies):")
    log.info("\n" + str(co.groupby(["Tier", "Match_Quality"]).size().unstack(fill_value=0)))


if __name__ == "__main__":
    main()
