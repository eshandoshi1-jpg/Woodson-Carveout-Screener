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
BANKER_CRM = "data/banker_crm.json"
UNIVERSE = "data/universe.csv"
UNIVERSE_SCANNED = 1107          # total companies the full run scanned (incl. unresolved)

# signal type -> (hard?, Factor_Links_JSON key or None)
HARD = {"held_for_sale", "activist_13d", "exploring_alternatives"}


def load_banker_crm(path=BANKER_CRM):
    """Load the static banker directory and validate its front-end contract."""
    p = Path(path)
    if not p.exists():
        return {"meta": {"bankCount": 0, "bankerCount": 0}, "banks": [], "bankers": []}
    data = json.loads(p.read_text())
    banks, bankers = data.get("banks", []), data.get("bankers", [])
    if data.get("meta", {}).get("bankCount") != len(banks):
        raise ValueError("banker CRM bank count does not match its records")
    if data.get("meta", {}).get("bankerCount") != len(bankers):
        raise ValueError("banker CRM contact count does not match its records")
    bank_ids = {bank.get("id") for bank in banks}
    if len(bank_ids) != len(banks) or any(person.get("bankId") not in bank_ids for person in bankers):
        raise ValueError("banker CRM contains duplicate or orphaned records")
    tier3 = sum(bank.get("tier") == 3 for bank in banks)
    tier4 = sum(bank.get("tier") == 4 for bank in banks)
    if tier3 != 127 or tier4 != 391 or tier3 + tier4 != len(banks):
        raise ValueError("banker CRM tiering must contain 127 Tier 3 and 391 Tier 4 banks")
    return data


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


def company_website(email):
    """Use the verified corporate-contact domain; do not invent a website without one."""
    email = (_s(email) or "").lower()
    if "@" not in email:
        return None
    domain = email.rsplit("@", 1)[-1].strip().strip(".")
    if "." not in domain or not re.fullmatch(r"[a-z0-9.-]+", domain):
        return None
    return f"https://{domain}"


INDUSTRY_DESCRIPTIONS = {
    "advertising, marketing": "A provider of advertising and marketing services.",
    "aerospace & defense": "A manufacturer and provider of aerospace and defense products.",
    "airlines": "An airline operator.",
    "apparel": "A manufacturer and marketer of apparel.",
    "automotive retailing, services": "A provider of automotive retail and related services.",
    "banking": "A banking and financial services company.",
    "commercial banks": "A commercial banking and financial services company.",
    "beverages": "A producer and distributor of beverages.",
    "building materials, glass": "A manufacturer of building materials and glass products.",
    "business services & supplies": "A provider of business services and supplies.",
    "capital goods": "A manufacturer of capital goods.",
    "chemicals": "A manufacturer of chemical products.",
    "computer software": "A developer of computer software.",
    "computers, office equipment": "A manufacturer of computers and office equipment.",
    "construction": "A provider of construction services.",
    "construction and farm machinery": "A manufacturer of construction and farm machinery.",
    "consumer durables": "A manufacturer of consumer durable products.",
    "diversified financials": "A diversified financial services company.",
    "diversified outsourcing services": "A provider of diversified outsourcing services.",
    "drugs & biotechnology": "A developer and manufacturer of pharmaceuticals and biotechnology products.",
    "education": "A provider of education services.",
    "electronics, electrical equip.": "A manufacturer of electronic and electrical equipment.",
    "energy": "An energy company.",
    "energy + digital": "An energy and digital infrastructure company.",
    "engineering & construction": "A provider of engineering and construction services.",
    "entertainment": "An entertainment company.",
    "equipment leasing": "A provider of equipment leasing services.",
    "financial data services": "A provider of financial data and information services.",
    "food and drug stores": "A retailer of food, pharmacy, and consumer products.",
    "food consumer products": "A producer of packaged food and consumer products.",
    "food markets": "A food retail company.",
    "food production": "A producer of food products.",
    "food services": "A provider of food services.",
    "food, drink & tobacco": "A producer of food, beverage, and tobacco products.",
    "forest and paper products": "A manufacturer of forest and paper products.",
    "general merchandisers": "A general merchandise retailer.",
    "global products (oils & fluids)": "A manufacturer and distributor of specialty oils and fluids.",
    "health care equipment & services": "A provider of health care equipment and services.",
    "health care: insurance and managed care": "A health insurance and managed care company.",
    "health care: medical facilities": "An operator of health care facilities.",
    "health care: pharmacy and other services": "A provider of pharmacy and health care services.",
    "healthcare": "A health care products and services company.",
    "home equipment, furnishings": "A manufacturer and marketer of home equipment and furnishings.",
    "homebuilders": "A residential homebuilder.",
    "hotels, casinos, resorts": "An operator of hotels, casinos, and resorts.",
    "hotels, restaurants & leisure": "An operator of hospitality, restaurant, and leisure businesses.",
    "household & personal products": "A manufacturer of household and personal care products.",
    "household and personal products": "A manufacturer of household and personal care products.",
    "industrial machinery": "A manufacturer of industrial machinery.",
    "information technology services": "A provider of information technology services.",
    "insurance": "An insurance provider.",
    "insurance: life, health (mutual)": "A mutual life and health insurance provider.",
    "insurance: life, health (stock)": "A life and health insurance provider.",
    "insurance: property and casualty (mutual)": "A mutual property and casualty insurance provider.",
    "insurance: property and casualty (stock)": "A property and casualty insurance provider.",
    "internet services and retailing": "An internet services and online retail company.",
    "it software & services": "A provider of software and information technology services.",
    "mail, package, and freight delivery": "A provider of mail, package, and freight delivery services.",
    "materials": "A producer of industrial materials.",
    "media": "A media company.",
    "medical products and equipment": "A manufacturer of medical products and equipment.",
    "metals": "A producer and manufacturer of metal products.",
    "mining, crude-oil production": "A mining and crude oil production company.",
    "miscellaneous": "A diversified operating company.",
    "motor vehicles & parts": "A manufacturer of motor vehicles and automotive parts.",
    "network and other communications equipment": "A manufacturer of networking and communications equipment.",
    "oil & gas operations": "An oil and gas exploration and production company.",
    "oil and gas equipment, services": "A provider of oil and gas equipment and services.",
    "packaging, containers": "A manufacturer of packaging and container products.",
    "petroleum refining": "A petroleum refining and marketing company.",
    "pharmaceuticals": "A developer and manufacturer of pharmaceutical products.",
    "pipelines": "An operator of energy pipelines and related infrastructure.",
    "publishing, printing": "A publishing and printing company.",
    "railroads": "A railroad operator.",
    "real estate": "A real estate company.",
    "retailing": "A retail company.",
    "scientific, photographic, and control equipment": "A manufacturer of scientific, photographic, and control equipment.",
    "securities": "A securities and financial services company.",
    "semiconductors": "A manufacturer of semiconductors.",
    "semiconductors and other electronic components": "A manufacturer of semiconductors and electronic components.",
    "shipping": "A provider of shipping and logistics services.",
    "specialty retailers: apparel": "A specialty apparel retailer.",
    "specialty retailers: other": "A specialty retail company.",
    "technology hardware & equipment": "A manufacturer of technology hardware and equipment.",
    "telecommunications": "A provider of telecommunications services.",
    "telecommunications services": "A provider of telecommunications services.",
    "tobacco": "A manufacturer of tobacco products.",
    "toys, sporting goods": "A manufacturer and marketer of toys and sporting goods.",
    "trading companies": "A diversified trading company.",
    "transportation": "A provider of transportation services.",
    "transportation and logistics": "A provider of transportation and logistics services.",
    "transportation equipment": "A manufacturer of transportation equipment.",
    "trucking, truck leasing": "A provider of trucking and truck leasing services.",
    "utilities": "A utility company.",
    "utilities: gas and electric": "A gas and electric utility.",
    "waste management": "A provider of waste management services.",
    "wholesalers: diversified": "A diversified wholesale distributor.",
    "wholesalers: diversified - industrial equipment supplier": "A distributor of industrial equipment.",
    "wholesalers: electronics and office equipment": "A distributor of electronics and office equipment.",
    "wholesalers: food and grocery": "A distributor of food and grocery products.",
    "wholesalers: health care": "A distributor of health care products.",
}


def clean_industry(value):
    industry = re.sub(r"\s*\(DI\)\s*$", "", _s(value) or "").strip()
    fixes = {
        "Scietific, Photographic and Control Equipment": "Scientific, Photographic, and Control Equipment",
        "Scientific,Photographic and  Control Equipment": "Scientific, Photographic, and Control Equipment",
    }
    return fixes.get(industry, industry) or None


def dealcloud_description(industry):
    """One plain business sentence for DealCloud's company-description field."""
    industry = clean_industry(industry)
    if not industry:
        return "A diversified operating company."
    return INDUSTRY_DESCRIPTIONS.get(industry.lower(), f"A company operating in the {industry.lower()} sector.")


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
    universe = pd.read_csv(UNIVERSE, dtype={"CIK": str})
    industry_by_company = (dict(zip(universe["Company"], universe["Industry"]))
                           if "Industry" in universe.columns else {})
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
        website = company_website(contact["email"] if contact else None)
        industry = clean_industry(industry_by_company.get(r["Company"]))
        dealcloud = {
            "name": r["Company"],
            "website": website,
            "description": dealcloud_description(industry),
        }
        # outreach body with a {{SENDER}} token the UI fills
        body = O.build_email(r).replace(O.SENDER, "{{SENDER}}")
        companies.append({
            "company": r["Company"], "ticker": _s(r.get("Ticker")), "cik": _s(r.get("CIK")),
            "score": int(_num(r.get("Propensity_Score")) or 0), "tier": tier_val(r.get("Tier")),
            "mandateFit": bool(division and division["mandate"]["fit"] == "fit"),
            "sector": industry,
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
            "dealCloud": dealcloud,
            "outreach": {"evidenceTier": evidence_tier(signals, bool(div_name) and not geographic_candidate),
                         "subject": O.SUBJECT,
                         "body": body,
                         "divisionNameSuppressed": geographic_candidate,
                         "suppressionReason": ("Geographic reporting segment; use general portfolio outreach"
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

    exported_at = datetime.now(timezone.utc)
    data_mtime = datetime.fromtimestamp(Path(ENRICHED).stat().st_mtime, tz=timezone.utc)
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
    banker_crm = load_banker_crm()
    snap = {
        "meta": {
            # This is the completed snapshot/export time, even when the SEC check found no changed rows.
            # The former workbook-mtime value made successful no-change refreshes look stale in the UI.
            "generatedAt": exported_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dataAsOf": data_mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "universeScanned": UNIVERSE_SCANNED,
            "secVerified": len(companies),
            "engineVersion": "woodson-2026.08",
            "coverage": coverage,
        },
        "changes": changes[:12],
        "companies": companies,
        "bankerCRM": banker_crm,
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
    print(f"  banker CRM: {len(banker_crm['banks'])} banks | {len(banker_crm['bankers'])} contacts")


if __name__ == "__main__":
    main()
