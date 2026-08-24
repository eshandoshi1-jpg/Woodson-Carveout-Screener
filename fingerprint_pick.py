"""
fingerprint_pick.py — choose the carveout-candidate DIVISION per company.

The live scanner ranks segments by `Seg_Score`, which sometimes surfaces the
LARGEST segment (a 52%-of-parent core, or a whole-company operating segment) as
the "candidate" — not a real carveout. This module replaces that pick with the
Divestiture Fingerprint v2 (`divestiture_fingerprint.py`), which runs the two
seller archetypes from the Master Training Document:

  * STRATEGIC PRUNER    → the sub-scale / underperforming orphan (most companies)
  * BALANCE-SHEET-FORCED → the most MARKETABLE division, when the parent has
    signaled a deleveraging sale (the crown-jewel pattern, CS-008 NN Inc)

It is PURE (segment financials already in the enriched file — no EDGAR I/O), so
it runs in seconds and can be applied to an existing build without re-scanning.

Input mapping (enriched columns → v2 fields), and the honest gaps:
  seg_rev_M          ← Revenue_M
  parent_rev_M       ← Σ segment Revenue_M  (Revenue_M_parent is ~always 0 here)
  seg_op_margin_pct  ← Margin_pct
  co_avg_margin_pct  ← Co_Avg_Margin_pct
  seg_rev_yoy_pct    ← None   (Revenue_YoY_3yr is unpopulated in the scan)
  identity_mismatch  ← None   (qualitative; not machine-derivable here)
  failed_bolton      ← None
Selection therefore keys off share-of-parent + margin + margin-gap + the parent
archetype — enough to separate the orphan from the core, which is the whole point.
"""

import re
import pandas as pd

import divestiture_fingerprint as fp

# Accounting residuals — never a sellable business. Belt-and-suspenders on top of
# the scanner's own Is_Catchall flag.
CATCHALL_TOKENS = ("all other", "other segments", "corporate", "unallocated",
                   "eliminations", "reconciling", "intersegment", "total segment")

# A carveout is a DIVISION, not most of the company. Above this share of parent
# revenue the "candidate" is really a whole-company sale (Coty "Prestige" at 65%,
# Asbury "Dealerships" at ~100%), so it is not a divisible carveout target.
MAX_SHARE_PCT = 55.0

# An operating margin outside this band is a segment-extraction artifact (mis-scaled
# revenue or op-income), not a real figure — e.g. Carnival "Cruise" −151%, Warner
# Music Publishing +98%. v2 keys hard on margin, so a candidate must not be chosen
# off a broken number; such segments are skipped for candidate eligibility.
MARGIN_LO, MARGIN_HI = -100.0, 80.0

FP_COLS = ["FP_Candidate_Segment", "FP_Candidate_Rev_M", "FP_Candidate_Margin_pct",
           "FP_Candidate_Share_pct", "FP_Archetype", "FP_Grade", "FP_Pct",
           "FP_Score", "FP_Max"]


def _clean_seg(s) -> str:
    # XBRL member names often end "... Segment Member" — strip repeated trailing tokens
    return re.sub(r"(\s*(Segment|Member))+\s*$", "", str(s)).strip()


def _num(x):
    v = pd.to_numeric(pd.Series([x]), errors="coerce").iloc[0]
    return None if pd.isna(v) else float(v)


def _activist_level(p_activist):
    """Map the scanner's 0–3 activist score to v2's activist_level vocabulary.
    (Constant per company, so it shifts a company's score but not which of its
    segments is chosen — segment choice is driven by segment-level factors.)"""
    v = _num(p_activist) or 0
    if v >= 3:
        return "campaign"   # public carveout-intent activist (SC 13D, Item 4)
    if v == 2:
        return "letter"
    if v == 1:
        return "passive"
    return None


def _parent_flags(row) -> dict:
    ry = _num(row.get("Parent_YoY_pct")) if bool(row.get("YoY_Meaningful")) else None
    return {
        "deleveraging_intent": bool(row.get("Deleveraging_Intent")),
        "leverage_high": (_num(row.get("P_Leverage")) or 0) >= 2,
        "serial_divester": bool(row.get("Serial_Divester_18mo")),
        "activist_level": _activist_level(row.get("P_Activist")),
        "rev_yoy_pct": ry,
        "restructuring": bool(row.get("Q_Restructuring_Hit")),
        # distress / megadeal / post-acquisition spike not derivable from the scan
    }


def pick_for_company(seg_rows: pd.DataFrame) -> dict:
    """Run v2 across a company's segments and return the winning candidate's FP_* fields
    (empty dict when the company has no divisible reportable segment we can name)."""
    parent = _parent_flags(seg_rows.iloc[0])

    revs = pd.to_numeric(seg_rows["Revenue_M"], errors="coerce")
    seg_sum = float(revs.sum(skipna=True)) if revs.notna().any() else 0.0
    stated = _num(seg_rows.iloc[0].get("Revenue_M_parent")) or 0.0
    parent_rev = max(seg_sum, stated) or None

    best = None
    for _, s in seg_rows.iterrows():
        name = _clean_seg(s.get("Segment"))
        low = name.lower()
        if not name or low in ("", "nan", "none"):
            continue
        if bool(s.get("Is_Catchall")) or any(k in low for k in CATCHALL_TOKENS):
            continue
        sr = _num(s.get("Revenue_M"))
        share = (sr / parent_rev * 100) if (sr is not None and parent_rev) else None
        if share is not None and share > MAX_SHARE_PCT:
            continue  # this "segment" is essentially the whole company

        margin = _num(s.get("Margin_pct"))
        if margin is not None and not (MARGIN_LO <= margin <= MARGIN_HI):
            continue  # implausible margin → extraction artifact, not a real candidate

        seg = {
            "seg_rev_M": sr,
            "parent_rev_M": parent_rev,
            "seg_op_margin_pct": margin,
            "co_avg_margin_pct": _num(s.get("Co_Avg_Margin_pct")),
            "seg_rev_yoy_pct": None,       # unpopulated in the scan — honest gap
            "identity_mismatch": None,
            "failed_bolton": None,
            "separable": True,
        }
        r = fp.score_segment(seg, parent)
        cand = {"name": name, "rev": sr, "margin": margin, "share": share, "res": r}
        # Highest normalized score wins; tie-break toward the smaller (more orphan-like)
        # share so we never default to the biggest unit.
        if best is None or (r["pct"], -(share or 0)) > (best["res"]["pct"], -(best["share"] or 0)):
            best = cand

    if best is None:
        return {}
    r = best["res"]
    return {
        "FP_Candidate_Segment": best["name"],
        "FP_Candidate_Rev_M": best["rev"],
        "FP_Candidate_Margin_pct": best["margin"],
        "FP_Candidate_Share_pct": round(best["share"], 1) if best["share"] is not None else None,
        "FP_Archetype": r["archetype"],
        "FP_Grade": r["grade"],
        "FP_Pct": r["pct"],
        "FP_Score": r["score"],
        "FP_Max": r["max"],
    }


def add_fingerprint(df: pd.DataFrame) -> pd.DataFrame:
    """Add FP_* columns to the enriched frame, one carveout candidate per company,
    broadcast onto every segment row of that company (so it survives the dashboard's
    dedup regardless of which row wins)."""
    df = df.copy()
    picks = {}
    for company, grp in df.groupby("Company"):
        picks[company] = pick_for_company(grp)
    for col in FP_COLS:
        df[col] = df["Company"].map(lambda c: picks.get(c, {}).get(col))
    return df


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "data/woodson_enriched.xlsx"
    d = pd.read_excel(path, engine="openpyxl")
    out = add_fingerprint(d)
    co = out.sort_values(["Propensity_Score"], ascending=False).drop_duplicates("Company")
    show = co[co["Tier"].isin(["Tier 1", "Tier 2"])].head(20)
    print(f"{'Company':<28}{'Archetype':<18}{'Candidate division':<34}{'Share':>7}  {'Margin':>7}  Grade")
    for _, r in show.iterrows():
        seg = str(r.get("FP_Candidate_Segment") or "— (no divisible segment)")
        sh = r.get("FP_Candidate_Share_pct")
        mg = r.get("FP_Candidate_Margin_pct")
        arch = {"forced_seller": "forced (delever)", "strategic_pruner": "pruner (orphan)"}.get(
            str(r.get("FP_Archetype")), "—")
        print(f"{r['Company'][:27]:<28}{arch:<18}{seg[:33]:<34}"
              f"{(f'{sh:.0f}%' if pd.notna(sh) else '—'):>7}  "
              f"{(f'{mg:.1f}%' if pd.notna(mg) else '—'):>7}  {str(r.get('FP_Grade'))[:1]}")
