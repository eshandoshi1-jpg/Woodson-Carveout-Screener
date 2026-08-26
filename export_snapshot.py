"""
export_snapshot.py — emit the front-end data contract (Fable build brief §8).

Reads data/woodson_enriched.xlsx (the pipeline output), attaches contacts, and
writes data/snapshot.json: one meta block + one row per identity-verified US
company, with candidate division, evidence-linked signals, contact, and a
pre-rendered outreach email (sender left as a {{SENDER}} token the UI fills).

Honesty rules honored here so the UI can make them visible:
  * a signal carries a filing link only when we actually have one; otherwise
    filing:null and the UI renders it "model-flagged".
  * outreach.evidenceTier is computed the same way outreach.py tiers the email
    (held-for-sale / deleveraging / exploring = A; model-only division = B; else C).
"""

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import contacts as C
import outreach as O

ENRICHED = "data/woodson_enriched.xlsx"
OUT = "data/snapshot.json"
UNIVERSE_SCANNED = 1107          # total companies the full run scanned (incl. unresolved)

# signal type -> (hard?, Factor_Links_JSON key or None)
HARD = {"held_for_sale", "activist_13d", "exploring_alternatives"}


def _num(x):
    if x is None:
        return None
    try:
        f = float(x)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _truthy(x):
    """bool() that treats NaN/None as False (bool(float('nan')) is True — the trap)."""
    if x is None:
        return False
    if isinstance(x, float) and math.isnan(x):
        return False
    if isinstance(x, str):
        return x.strip().lower() in ("true", "1", "yes")
    return bool(x)


def _hfs_live(r):
    """Material held-for-sale still on a recent balance sheet (derived here, as the
    dashboard does — the enriched file stores HFS_Present/Value/Latest, not HFS_Live)."""
    if not _truthy(r.get("HFS_Present")):
        return False
    val = _num(r.get("HFS_Value_M"))
    rev = _num(r.get("Revenue_M_parent"))
    pct = (val / rev * 100) if (val and rev) else None
    material = (pct is not None and pct >= 2) or (pct is None and val is not None and val >= 50)
    latest = pd.to_datetime(r.get("HFS_Latest_Filed"), errors="coerce")
    if pd.isna(latest):
        return False
    age = (pd.Timestamp.utcnow().tz_localize(None) - latest).days
    return bool(material and age <= 365)


def _s(x):
    return None if (x is None or (isinstance(x, float) and math.isnan(x))) else str(x).strip() or None


def _iso(x):
    d = pd.to_datetime(x, errors="coerce")
    return None if pd.isna(d) else d.strftime("%Y-%m-%d")


def _clean_div(s):
    s = _s(s)
    return None if (not s or s.lower() in ("nan", "none")) else re.sub(r"(\s*(Segment|Member))+\s*$", "", s).strip()


def _links(row):
    try:
        return json.loads(row.get("Factor_Links_JSON") or "{}")
    except Exception:
        return {}


def _filing(url, form=None, date=None):
    url = _s(url)
    if not url or not url.startswith("http"):
        return None
    return {"form": form, "date": _iso(date) if date else None, "url": url}


def build_signals(r, fl):
    """Only the seven divestiture signal types; each with a filing link or null."""
    out = []
    doc = _s(r.get("Filing_Doc_URL"))
    fdate = r.get("Filing_Date")

    if _hfs_live(r):
        filing = _filing(r.get("HFS_Doc_URL") or doc, _s(r.get("HFS_Form")) or "10-K/Q",
                         r.get("HFS_Latest_Filed"))
        out.append({"type": "held_for_sale", "hard": bool(filing),
                    "date": _iso(r.get("HFS_Latest_Filed")), "filing": filing})
    if str(r.get("Co_Timing_Signal")) == "EXPLORATORY":
        lang = _s(r.get("Language_Source_URLs"))
        u = lang.split("|")[0].strip() if lang else doc
        filing = _filing(u, "10-K/Q", fdate)
        out.append({"type": "exploring_alternatives", "hard": bool(filing),
                    "date": _iso(fdate), "filing": filing})
    if _num(r.get("P_Activist")) and _num(r.get("P_Activist")) >= 2:
        filing = _filing(fl.get("P_Activist"), "SC 13D")
        out.append({"type": "activist_13d", "hard": bool(filing), "date": None,
                    "filing": filing})
    if _truthy(r.get("Deleveraging_Intent")):
        out.append({"type": "deleveraging_intent", "hard": False, "date": None,
                    "filing": _filing(r.get("Deleveraging_URL"), "10-K/Q")})
    if _num(r.get("P_CsuiteChange")) and _num(r.get("P_CsuiteChange")) >= 2:
        out.append({"type": "exec_departure", "hard": False, "date": None,
                    "filing": _filing(fl.get("P_CsuiteChange"), "8-K")})
    if _num(r.get("P_GuidanceCut")) and _num(r.get("P_GuidanceCut")) >= 1:
        out.append({"type": "guidance_dividend_cut", "hard": False, "date": None,
                    "filing": _filing(fl.get("P_GuidanceCut"), "8-K")})
    if _truthy(r.get("Serial_Divester_18mo")):
        out.append({"type": "serial_divester", "hard": False, "date": None, "filing": None})
    return out


# Woodson's mandate (from the firm's criteria): the DIVISION must fit, not the parent.
MANDATE = {"rev_min": 75e6, "ebitda_min": 6e6, "ebitda_max": 30e6, "rev_max": 750e6}


def div_mandate(rev_usd, margin_pct):
    """Assess one division against Woodson's size box. EBITDA is proxied by operating
    income (rev * op-margin) — conservative, since true EBITDA adds back D&A. 'above'
    means the deal likely clears the $300M ceiling (~$30M EBITDA at ~10x)."""
    if rev_usd is None:
        return {"fit": "unknown", "ebitdaEstUsd": None}
    ebitda = rev_usd * margin_pct / 100 if margin_pct is not None else None
    # size (revenue / deal) is judged before the EBITDA floor, so a large loss-making
    # division reads as "above" (too big), not "below" (too small).
    if rev_usd > MANDATE["rev_max"] or (ebitda is not None and ebitda > MANDATE["ebitda_max"]):
        f = "above"                                   # deal likely clears the $300M ceiling
    elif rev_usd < MANDATE["rev_min"]:
        f = "below"                                   # below the $75M revenue floor
    elif ebitda is not None and ebitda < MANDATE["ebitda_min"]:
        f = "weak"                                    # right size, but under the $6M EBITDA floor
    elif ebitda is None:
        f = "possible"                                # revenue fits; EBITDA remains unresolved
    else:
        f = "fit"
    return {"fit": f, "ebitdaEstUsd": int(ebitda) if ebitda is not None else None,
            "verify": bool(f == "possible")}


def build_all_segments(sub, candidate_name):
    """Every reportable segment of the parent (the candidate in context of its siblings) —
    so a thin headline like 'United Kingdom' is read against the rest of the portfolio."""
    revs = pd.to_numeric(sub["Revenue_M"], errors="coerce")
    total = float(revs.sum(skipna=True)) or None
    out = []
    for _, s in sub.iterrows():
        name = _clean_div(s.get("Segment"))
        if not name:
            continue
        sr = _num(s.get("Revenue_M"))
        rev_usd = int(sr * 1_000_000) if sr else None
        out.append({
            "name": name,
            "revenueUsd": rev_usd,
            "marginPct": _num(s.get("Margin_pct")),
            "pctOfParent": round(sr / total * 100, 1) if (sr and total) else None,
            "isCatchall": _truthy(s.get("Is_Catchall")),
            "isCandidate": bool(candidate_name and name == candidate_name),
            "mandate": div_mandate(rev_usd, _num(s.get("Margin_pct"))),
        })
    out.sort(key=lambda x: (x["revenueUsd"] if x["revenueUsd"] is not None else -1), reverse=True)
    return out


def why_text(r):
    """Plain-English rationale (company-level), mirrors outreach's factor basis + timing."""
    parts = []
    t = str(r.get("Co_Timing_Signal"))
    if t == "EXPLORATORY":
        parts.append("is publicly exploring strategic alternatives")
    if _truthy(r.get("Deleveraging_Intent")):
        parts.append("has signalled intent to reduce leverage")
    if _num(r.get("P_Activist")) and _num(r.get("P_Activist")) >= 2:
        parts.append("has an activist 13D on file")
    if _num(r.get("P_Leverage")) and _num(r.get("P_Leverage")) >= 2:
        parts.append("carries elevated leverage")
    if _num(r.get("P_CsuiteChange")) and _num(r.get("P_CsuiteChange")) >= 2:
        parts.append("recently lost a senior officer (8-K Item 5.02)")
    if _num(r.get("P_SegCount")) and _num(r.get("P_SegCount")) >= 1:
        parts.append("reports multiple operating segments")
    if _num(r.get("P_GuidanceCut")) and _num(r.get("P_GuidanceCut")) >= 1:
        parts.append("filed a guidance or dividend cut")
    if _truthy(r.get("Serial_Divester_18mo")):
        parts.append("has divested within the last 18 months")
    if not parts:
        return "Scored on portfolio structure and segment financials; see the filings for detail."
    co = r["Company"]
    s = f"{co} " + "; ".join(parts) + "."
    return s[0].upper() + s[1:]


def evidence_tier(signals, has_division):
    if any(s.get("hard") and s.get("filing") for s in signals):
        return "A"
    if has_division:
        return "B"
    return "C"


def tier_val(t):
    return {"Tier 1": 1, "Tier 2": 2, "Tier 3": 3, "Watchlist": "watchlist", "Drop": "drop"}.get(str(t), "drop")


SRC = {"Fortune 1000": "fortune1000", "Fortune LY": "fortune1000", "Forbes US HQ": "forbes"}


def main():
    df = pd.read_excel(ENRICHED, engine="openpyxl")
    co = df.drop_duplicates("Company").copy()
    V = co["Match_Quality"].isin(["VERIFIED", "REVIEW"]) & (co["Region"] == "US")
    co = co[V].copy()
    co = C.attach_contacts(co)
    # source list (Fortune 1000 / Forbes) from the committed rolodex CSV — repo-relative,
    # so this never touches ~/Downloads at refresh time (CI-safe).
    rolo = pd.read_csv(C.OUT, dtype=str)[["join_key", "source"]].drop_duplicates("join_key")
    co["_jk"] = co["Company"].map(C._join_key)
    co = co.merge(rolo, left_on="_jk", right_on="join_key", how="left")

    seg_groups = {k: v for k, v in df.groupby("Company")}
    companies = []
    for _, r in co.sort_values("Propensity_Score", ascending=False).iterrows():
        fl = _links(r)
        signals = build_signals(r, fl)
        div_name = _clean_div(r.get("FP_Candidate_Segment"))
        geographic_candidate = bool(div_name and O.is_geographic_segment(div_name))
        division = None
        if div_name:
            _drev = (int(_num(r.get("FP_Candidate_Rev_M")) * 1_000_000)
                     if _num(r.get("FP_Candidate_Rev_M")) else None)
            division = {
                "name": div_name,
                "revenueUsd": _drev,
                "operatingMarginPct": _num(r.get("FP_Candidate_Margin_pct")),
                "pctOfParentRevenue": _num(r.get("FP_Candidate_Share_pct")),
                "mandate": div_mandate(_drev, _num(r.get("FP_Candidate_Margin_pct"))),
                "outreachNameAllowed": not geographic_candidate,
            }
        contact = None
        if _s(r.get("primary_email")):
            role = _s(r.get("primary_role")) or ""
            contact = {"name": _s(r.get("primary_name")), "title": _s(r.get("primary_title")),
                       "function": "corp_dev" if "Development" in role else "cfo",
                       "email": _s(r.get("primary_email"))}
        # outreach body with a {{SENDER}} token the UI fills
        body = O.build_email(r).replace(O.SENDER, "{{SENDER}}")
        companies.append({
            "company": r["Company"], "ticker": _s(r.get("Ticker")), "cik": _s(r.get("CIK")),
            "score": int(_num(r.get("Propensity_Score")) or 0), "tier": tier_val(r.get("Tier")),
            "mandateFit": bool(division and division["mandate"]["fit"] == "fit"),
            "sector": None,
            "sources": [SRC.get(str(r.get("source")), "fortune1000")],
            "archetype": {"strategic_pruner": "pruner", "forced_seller": "forced_seller"}.get(str(r.get("FP_Archetype"))),
            "candidateDivision": division,
            "hasFitDivision": _truthy(r.get("FP_Has_Fit")),   # a Woodson-sized divisible unit exists
            "hasPossibleFitDivision": _truthy(r.get("FP_Has_Possible_Fit")),
            "segments": build_all_segments(seg_groups.get(r["Company"]), div_name) if seg_groups.get(r["Company"]) is not None else [],
            "tenKUrl": _s(r.get("Filing_Doc_URL")),
            "why": why_text(r),
            "signals": signals,
            "contact": contact,
            "outreach": {"evidenceTier": evidence_tier(signals, bool(div_name) and not geographic_candidate),
                         "subject": "Woodson Equity — carveout / divestiture inquiry",
                         "body": body,
                         "divisionNameSuppressed": geographic_candidate,
                         "suppressionReason": ("Geographic reporting segment — use general portfolio outreach"
                                               if geographic_candidate else None)},
        })

    # what moved since the previous snapshot — tier upgrades (from the last refresh's re-scan)
    changes = []
    prev_path = Path("data/woodson_enriched_prev.xlsx")
    if prev_path.exists():
        try:
            pv = pd.read_excel(prev_path, engine="openpyxl").drop_duplicates("Company")
            prev_tier = dict(zip(pv["Company"], pv["Tier"].astype(str)))
            rank = {"Tier 1": 0, "Tier 2": 1, "Tier 3": 2, "Watchlist": 3, "Drop": 4}
            for _, r in co.iterrows():
                pt, ct = prev_tier.get(r["Company"]), str(r.get("Tier"))
                if pt and rank.get(ct, 9) < rank.get(pt, 9):        # improved to a better tier
                    changes.append({"company": r["Company"], "ticker": _s(r.get("Ticker")),
                                    "prevTier": tier_val(pt), "newTier": tier_val(ct),
                                    "score": int(_num(r.get("Propensity_Score")) or 0),
                                    "hasFitDivision": _truthy(r.get("FP_Has_Fit"))})
            nrank = {1: 0, 2: 1, 3: 2, "watchlist": 3, "drop": 4}
            changes.sort(key=lambda x: (nrank.get(x["newTier"], 9), -x["score"]))
        except Exception:
            pass

    mtime = datetime.fromtimestamp(Path(ENRICHED).stat().st_mtime, tz=timezone.utc)
    coverage = {"runMode": "full", "sources": "10-K / 10-Q / 8-K / SC 13D/G"}
    sp = Path("data/state.json")
    if sp.exists():
        try:
            s = json.loads(sp.read_text()); lc = s.get("lastCoverage", {})
            coverage = {"runMode": "incremental", "highWater": s.get("highWater"),
                        "lastRun": s.get("lastRun"), "filingsProcessed": lc.get("filingsProcessed"),
                        "byForm": lc.get("byForm"), "sources": "10-K / 10-Q / 8-K / SC 13D/G"}
        except Exception:
            pass
    snap = {
        "meta": {
            "generatedAt": mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "universeScanned": UNIVERSE_SCANNED,
            "secVerified": len(companies),
            "engineVersion": "woodson-2026.08",
            "coverage": coverage,
        },
        "changes": changes[:12],
        "companies": companies,
    }
    Path(OUT).parent.mkdir(exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(snap, f, separators=(",", ":"))
    # quick coverage report
    withdiv = sum(1 for c in companies if c["candidateDivision"])
    withcon = sum(1 for c in companies if c["contact"])
    withsig = sum(1 for c in companies if c["signals"])
    t1 = sum(1 for c in companies if c["tier"] == 1)
    t2 = sum(1 for c in companies if c["tier"] == 2)
    from collections import Counter
    sig = Counter(s["type"] for c in companies for s in c["signals"])
    print(f"wrote {OUT}: {len(companies)} companies | {Path(OUT).stat().st_size/1e6:.2f} MB")
    print(f"  Tier1={t1} Tier2={t2} | with division={withdiv} contact={withcon} signal={withsig}")
    print(f"  signal-type counts: {dict(sig)}")


if __name__ == "__main__":
    main()
