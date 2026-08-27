"""
Phase A1 — Conditions Engine
Computes a fundamentals-based divestiture propensity score (0–20) per segment,
with no keyword involvement. Scoring:

Parent-level (0–10):
  +2  Net debt / EBITDA > 3.0x
  +1  3+ reportable segments; +2 if 5+
  +2  CEO or CFO change in last 18 months (8-K Item 5.02)
  +2  Active 13D filer
  +1  Parent revenue declined YoY
  +1  Guidance cut or dividend cut/suspension (8-K 2.02 / 8.01)

Segment-level (0–10 per segment):
  +3  Negative operating income  (OR)
  +2  Operating margin below company average
  +2  3-year revenue trend declining
  +1  Margin gap is widest vs best segment
  +2  Segment in "Corporate / All Other" catch-all bucket
  +2  Goodwill impairment in last 2 years

Unit of output: (parent, segment) pair, not company.
"""

import os
import re
import time
import json
import logging
import html as html_lib
from typing import Optional, Tuple
from datetime import datetime, timedelta

import requests
import pandas as pd
from rapidfuzz import fuzz, process as fuzz_process

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HEADERS = {"User-Agent": os.environ.get("SEC_USER_AGENT", "WoodsonEquity research@woodsonequity.com"), "Accept-Encoding": "gzip, deflate"}
RATE_LIMIT = 0.12   # ~8 req/s, safe under 10/s limit
BASE_URL = "https://data.sec.gov"
EDGAR_BASE = "https://www.sec.gov"

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("conditions_engine")

PARSE_LOG = []   # running log of parse success/failure per company


def _get(url: str, timeout: int = 20) -> Optional[dict]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        time.sleep(RATE_LIMIT)
        return r.json()
    except Exception as e:
        time.sleep(RATE_LIMIT)
        log.warning(f"GET failed: {url} — {e}")
        return None


def _get_text(url: str, timeout: int = 30) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        time.sleep(RATE_LIMIT)
        return r.text
    except Exception:
        time.sleep(RATE_LIMIT)
        return ""


# ---------------------------------------------------------------------------
# CIK resolution
# ---------------------------------------------------------------------------

_CIK_INDEX: Optional[dict] = None


def _load_cik_index() -> dict:
    global _CIK_INDEX
    if _CIK_INDEX is not None:
        return _CIK_INDEX
    log.info("Loading SEC company ticker index…")
    data = _get("https://www.sec.gov/files/company_tickers.json", timeout=30)
    if not data:
        return {}
    idx = {}
    for entry in data.values():
        title = entry.get("title", "").strip().upper()
        if title:
            idx[title] = {
                "cik": str(entry["cik_str"]).zfill(10),
                "ticker": entry.get("ticker", ""),
                "title": entry["title"],
            }
    _CIK_INDEX = idx
    log.info(f"  {len(idx):,} companies loaded")
    return idx


def _normalize(name: str) -> str:
    name = name.upper().strip()
    for sfx in [r"\bINC\.?\b", r"\bCORP\.?\b", r"\bCO\.?\b", r"\bLTD\.?\b",
                r"\bL\.?L\.?C\.?\b", r"\bPLC\.?\b", r"\bGROUP\b", r"\bHOLDINGS?\b",
                r"\bINTERNATIONAL\b", r"\bENTERPRISES?\b", r"\bCOMPANY\b", r"\bCOMPANIES\b"]:
        name = re.sub(sfx, "", name)
    name = re.sub(r"[^\w\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def resolve_cik(company_name: str) -> Tuple[Optional[str], Optional[str], int, str]:
    idx = _load_cik_index()
    names = list(idx.keys())

    norm_input = _normalize(company_name)
    norm_names_map = {_normalize(n): n for n in names}
    norm_names = list(norm_names_map.keys())

    match = fuzz_process.extractOne(norm_input, norm_names, scorer=fuzz.token_set_ratio)
    if match is None:
        raise LookupError(f"No SEC company match for {company_name}")
    best_norm, score, _choice_index = match
    original = norm_names_map[best_norm]
    entry = idx[original]
    return entry["cik"], entry["ticker"], int(round(score)), entry["title"]


# ---------------------------------------------------------------------------
# Submissions helper
# ---------------------------------------------------------------------------

def _get_submissions(cik: str) -> Optional[dict]:
    return _get(f"{BASE_URL}/submissions/CIK{cik}.json")


def _recent_filings(subs: dict, form_type: str, months: int = 18) -> list:
    cutoff = datetime.now() - timedelta(days=months * 30)
    recent = subs.get("filings", {}).get("recent", {})
    out = []
    for form, date_str, acc, doc in zip(
        recent.get("form", []),
        recent.get("filingDate", []),
        recent.get("accessionNumber", []),
        recent.get("primaryDocument", []),
    ):
        if not form.startswith(form_type):
            continue
        try:
            fd = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        if form_type == "10-K" or fd >= cutoff:
            out.append({"form": form, "date": date_str, "acc": acc, "doc": doc})
            if form_type == "10-K":
                break
    return out


def _filing_url(cik: str, acc: str, doc: str) -> str:
    acc_nd = acc.replace("-", "")
    return f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{doc}"


# ---------------------------------------------------------------------------
# Parent factor 1: Net debt / EBITDA via XBRL
# ---------------------------------------------------------------------------

def _xbrl_val(us_gaap: dict, concepts: list) -> Optional[float]:
    """Try each concept name in order; return most recent annual value or None."""
    for concept in concepts:
        units = us_gaap.get(concept, {}).get("units", {})
        entries = units.get("USD", []) or units.get("pure", [])
        annual = [e for e in entries if e.get("form") in ("10-K", "10-K/A") and e.get("val") is not None]
        if annual:
            annual.sort(key=lambda e: e.get("end", ""), reverse=True)
            return float(annual[0]["val"])
    return None


def _score_leverage(facts: dict) -> Tuple[int, str]:
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    debt = _xbrl_val(us_gaap, [
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebt",
        "DebtAndCapitalLeaseObligations",
        "LongTermDebtNoncurrent",
    ])
    cash = _xbrl_val(us_gaap, ["CashAndCashEquivalentsAtCarryingValue", "Cash"])
    ebitda = _xbrl_val(us_gaap, ["OperatingIncomeLoss"])
    da = _xbrl_val(us_gaap, ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization"])

    if debt is None or ebitda is None:
        return 0, "leverage: data unavailable"
    net_debt = (debt - (cash or 0))
    ebitda_adj = ebitda + (da or 0)
    if ebitda_adj <= 0:
        return 0, f"leverage: EBITDA non-positive ({ebitda_adj/1e6:.0f}M)"
    ratio = net_debt / ebitda_adj
    pts = 2 if ratio > 3.0 else 0
    return pts, f"leverage: net debt/EBITDA {ratio:.1f}x{' → +2' if pts else ''}"


# ---------------------------------------------------------------------------
# Parent factor 2: Segment count
# ---------------------------------------------------------------------------

SEG_AXES = [
    "StatementBusinessSegmentsAxis",
    "SegmentReportingInformationBySegmentAxis",
]


def _score_segment_count(facts: dict) -> Tuple[int, str, set]:
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    seg_names: set = set()
    for concept in ["NumberOfReportableSegments", "NumberOfOperatingSegments"]:
        entries = us_gaap.get(concept, {}).get("units", {}).get("pure", [])
        annual = [e for e in entries if e.get("form") in ("10-K", "10-K/A")]
        if annual:
            annual.sort(key=lambda e: e.get("end", ""), reverse=True)
            n = int(annual[0].get("val", 0))
            pts = 2 if n >= 5 else 1 if n >= 3 else 0
            return pts, f"segments: {n} reportable{' → +' + str(pts) if pts else ''}", set()

    # Fall back: count distinct segment dimension values from revenue concept
    for rev_concept in ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"]:
        entries = us_gaap.get(rev_concept, {}).get("units", {}).get("USD", [])
        for e in entries:
            dims = e.get("dimensions", {})
            for axis in SEG_AXES:
                seg = dims.get(axis)
                if seg and e.get("form") in ("10-K", "10-K/A"):
                    seg_names.add(seg)

    n = len(seg_names)
    pts = 2 if n >= 5 else 1 if n >= 3 else 0
    return pts, f"segments: {n} from XBRL dims{' → +' + str(pts) if pts else ''}", seg_names


# ---------------------------------------------------------------------------
# Parent factor 3: C-suite change (8-K Item 5.02)
# ---------------------------------------------------------------------------

def _score_csuite_change(subs: dict, cik: str) -> Tuple[int, str]:
    """
    Score material C-suite departures only. Item 5.02 covers routine director
    elections and appointments too — we only count departures/resignations of
    principal officers (CEO, CFO, COO, President) or board departures.
    """
    # Strong departure verbs — these distinguish a resignation/removal from a
    # routine appointment or annual director election (both also file 5.02).
    DEPARTURE_TERMS = [
        "resign", "resignation", "retire", "retirement", "step down",
        "stepped down", "will no longer serve", "no longer serve",
        "departure of", "termination of", "separation of",
        "removed as", "relieved of", "transition of",
    ]
    # Senior-officer context so a departing junior director doesn't score.
    OFFICER_TERMS = [
        "chief executive", "chief financial", "chief operating",
        "principal executive", "principal financial", "president and chief",
        " ceo", " cfo", " coo",
    ]

    eight_ks = _recent_filings(subs, "8-K", months=18)
    for filing in eight_ks:
        acc = filing["acc"]
        acc_nd = acc.replace("-", "")
        idx_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{acc}-index.htm"
        idx_html = _get_text(idx_url).lower()
        if "5.02" not in idx_html:
            continue

        # The index page's Item 5.02 TITLE always contains "departure" and
        # "appointment" — useless for discrimination. Fetch the actual 8-K body.
        doc = filing.get("doc") or ""
        if not doc:
            continue
        body = _get_text(_filing_url(cik, acc, doc))
        if not body:
            continue
        text = html_lib.unescape(re.sub(r"<[^>]+>", " ", body)).lower()
        # Remove the standard Item 5.02 boilerplate title so its "departure"
        # wording doesn't create a false positive.
        text = re.sub(
            r"departure of directors or certain officers[^.]*?compensatory arrangements of certain officers",
            " ", text)

        # Require the departure language and senior-officer title in the same
        # local passage. Their appearing anywhere in a long earnings release is
        # not evidence that the senior officer departed.
        departure_windows = []
        for term in DEPARTURE_TERMS:
            departure_windows.extend(text[max(0, m.start()-220):m.end()+220]
                                     for m in re.finditer(re.escape(term), text))
        if any(any(officer in window for officer in OFFICER_TERMS)
               for window in departure_windows):
            return 2, f"c-suite change: senior officer departure via 8-K Item 5.02 ({filing['date']}) → +2"
    return 0, "c-suite change: no senior departure in last 18 months"


# ---------------------------------------------------------------------------
# Parent factor 4: 13D activist (real signal — searches EDGAR for 13Ds targeting this company)
# ---------------------------------------------------------------------------

def _score_activist(cik: str, company_name: str = "") -> Tuple[int, str]:
    try:
        from activist_monitor import score_activist_13d
        name = company_name or cik
        return score_activist_13d(name, cik, lookback_days=1095)
    except Exception as e:
        log.warning(f"Activist monitor failed for {cik}: {e}")
        return 0, "13D: lookup failed"


# ---------------------------------------------------------------------------
# Parent factor 5: Revenue decline YoY
# ---------------------------------------------------------------------------

def _score_rev_decline(facts: dict) -> Tuple[int, str]:
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    for concept in ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"]:
        entries = us_gaap.get(concept, {}).get("units", {}).get("USD", [])
        # Parent-level only (no dimensions)
        annual = [e for e in entries
                  if e.get("form") in ("10-K", "10-K/A")
                  and not e.get("dimensions")
                  and e.get("val") is not None]
        if len(annual) >= 2:
            annual.sort(key=lambda e: e.get("end", ""), reverse=True)
            v0, v1 = annual[0]["val"], annual[1]["val"]
            if v1 > 0:
                chg = (v0 - v1) / v1 * 100
                pts = 1 if chg < 0 else 0
                return pts, f"rev trend: {chg:+.1f}% YoY{' → +1' if pts else ''}"
    return 0, "rev trend: data unavailable"


# ---------------------------------------------------------------------------
# Parent factor 6: Guidance/dividend cut (8-K 2.02 / 8.01)
# ---------------------------------------------------------------------------

def _has_explicit_guidance_or_dividend_cut(text: str) -> bool:
    low = re.sub(r"\s+", " ", text.lower())
    action = r"(?:cut|cuts|cutting|lower(?:ed|ing|s)?|reduc(?:e|ed|es|ing|tion)|withdraw(?:n|s|ing)?|suspend(?:ed|s|ing)?)"
    subject = r"(?:guidance|outlook|forecast|dividend|distribution)"
    return bool(re.search(rf"{action}.{{0,100}}{subject}", low) or
                re.search(rf"{subject}.{{0,100}}{action}", low))


def _score_guidance_cut(subs: dict, cik: str) -> Tuple[int, str]:
    eight_ks = _recent_filings(subs, "8-K", months=18)
    for filing in eight_ks:
        acc = filing["acc"]
        acc_nd = acc.replace("-", "")
        idx_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{acc}-index.htm"
        html = _get_text(idx_url)
        if "8.01" in html or ("2.02" in html):
            # Check text for dividend/guidance cut language
            doc_url = _filing_url(cik, acc, filing["doc"])
            text = html_lib.unescape(re.sub(r"<[^>]+>", " ", _get_text(doc_url)))
            # An explicit action must be tied to guidance/dividends in the same
            # short passage; generic words like "decline" in earnings text do not count.
            if _has_explicit_guidance_or_dividend_cut(text):
                return 1, f"guidance/div cut: signal in 8-K {filing['date']} → +1"
    return 0, "guidance/div cut: none detected"


# ---------------------------------------------------------------------------
# Segment-level scoring via XBRL + HTML fallback
# ---------------------------------------------------------------------------

def _clean_seg_name(raw: str) -> str:
    name = raw.split(":")[-1]
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return name.strip()


def _is_catchall(seg_name: str) -> bool:
    patterns = [
        r"\bcorporate\b", r"\ball other\b", r"\bother\b", r"\bunallocated\b",
        r"\belimination\b", r"\bcorp\b", r"\bgeneral\b", r"\bheadquarters\b",
    ]
    low = seg_name.lower()
    return any(re.search(p, low) for p in patterns)


def _get_segmented_xbrl(us_gaap: dict, concept: str, annual_periods: list) -> dict:
    """Returns {seg_name: {period_end: value}} for 10-K filings."""
    entries = us_gaap.get(concept, {}).get("units", {}).get("USD", [])
    result: dict = {}
    for e in entries:
        dims = e.get("dimensions", {})
        seg_key = None
        for axis in SEG_AXES:
            seg_key = dims.get(axis)
            if seg_key:
                break
        if not seg_key:
            continue
        if e.get("form") not in ("10-K", "10-K/A"):
            continue
        seg = _clean_seg_name(seg_key)
        period = e.get("end", "")
        val = e.get("val")
        if val is None or not period:
            continue
        if seg not in result:
            result[seg] = {}
        result[seg][period] = val
    return result


def _fetch_inline_xbrl_segments(cik: str, subs: dict) -> Tuple[dict, dict, dict, str, str, str, str]:
    """
    Fetches the inline XBRL (*_htm.xml) from the latest 10-K filing index,
    parses segment contexts and facts.
    Returns (rev_map, inc_map, gw_map, source_label, filing_index_url, primary_doc_url, filing_date).
    Each map: {clean_seg_name: {period_end: value_in_dollars}}.
    """
    # Find latest 10-K
    ten_ks = _recent_filings(subs, "10-K", months=24)
    if not ten_ks:
        ten_ks = _recent_filings(subs, "10-K/A", months=24)
    if not ten_ks:
        return {}, {}, {}, "INLINE_XBRL_FAIL", "", "", ""

    filing = ten_ks[0]
    acc = filing["acc"]
    acc_nd = acc.replace("-", "")
    filing_date = filing.get("date", "")
    filing_index_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{acc}-index.htm"
    primary_doc_url = _filing_url(cik, acc, filing.get("doc", "")) if filing.get("doc") else filing_index_url

    # Fetch filing directory listing to find the *_htm.xml inline XBRL file
    dir_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/"
    dir_html = _get_text(dir_url, timeout=20)
    if not dir_html:
        return {}, {}, {}, "INLINE_XBRL_FAIL", filing_index_url, primary_doc_url, filing_date

    # Find *_htm.xml in the directory listing
    xbrl_file = None
    for match in re.finditer(r'href="(/Archives/edgar/data/[^"]+_htm\.xml)"', dir_html):
        xbrl_file = match.group(1).split("/")[-1]
        break
    if not xbrl_file:
        # Fallback: any .xml that's not an index or schema
        for match in re.finditer(r'href="(/Archives/edgar/data/[^"]+\.xml)"', dir_html):
            name = match.group(1).split("/")[-1]
            if not any(x in name for x in ["-index", "_cal", "_def", "_lab", "_pre", "_ref"]):
                xbrl_file = name
                break

    if not xbrl_file:
        return {}, {}, {}, "INLINE_XBRL_FAIL", filing_index_url, primary_doc_url, filing_date

    xbrl_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{xbrl_file}"
    xml_text = _get_text(xbrl_url, timeout=60)
    if not xml_text:
        return {}, {}, {}, "INLINE_XBRL_FAIL", filing_index_url, primary_doc_url, filing_date

    # Parse segment contexts: <context id="c-N">...<xbrldi:explicitMember dimension="...SegmentsAxis">MEMBER</...>
    ctx_pattern = re.compile(r'<context\s+id="([^"]+)"[^>]*>(.*?)</context>', re.DOTALL | re.IGNORECASE)
    seg_contexts: dict = {}
    for m in ctx_pattern.finditer(xml_text):
        ctx_id, body = m.group(1), m.group(2)
        for axis in SEG_AXES:
            dm = re.search(
                rf'explicitMember[^>]*dimension="[^"]*{axis}[^"]*"[^>]*>([^<]+)<', body, re.I
            )
            if dm:
                end_m = re.search(r'<endDate>([^<]+)</endDate>', body)
                start_m = re.search(r'<startDate>([^<]+)</startDate>', body)
                if end_m:
                    seg_contexts[ctx_id] = {
                        "member": _clean_seg_name(dm.group(1).strip()),
                        "end": end_m.group(1).strip(),
                        "start": start_m.group(1).strip() if start_m else "",
                    }
                break

    if not seg_contexts:
        return {}, {}, {}, "INLINE_XBRL_NO_SEGS", filing_index_url, primary_doc_url, filing_date

    # Parse all fact elements: <TAG contextRef="ID" decimals="N" ...>VALUE</TAG>
    fact_pattern = re.compile(
        r'<([^\s>]+)\s[^>]*contextRef="([^"]+)"[^>]*decimals="(-?\d+)"[^>]*>([^<]+)</'
    )

    # Build {concept: {seg: {period: value}}}
    all_facts: dict = {}
    for m in fact_pattern.finditer(xml_text):
        tag, ctx_id, _decimals, val_str = m.groups()
        if ctx_id not in seg_contexts:
            continue
        try:
            val = float(val_str.strip())  # decimals is precision, NOT a scale factor
        except ValueError:
            continue
        concept = tag.split(":")[-1]
        ctx = seg_contexts[ctx_id]
        seg = ctx["member"]
        end = ctx["end"]
        if concept not in all_facts:
            all_facts[concept] = {}
        if seg not in all_facts[concept]:
            all_facts[concept][seg] = {}
        if end not in all_facts[concept][seg]:
            all_facts[concept][seg][end] = val

    def _extract_map(concepts: list) -> dict:
        for c in concepts:
            if c in all_facts:
                return all_facts[c]
        return {}

    rev_map = _extract_map([
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues", "NetRevenues", "SalesRevenueNet",
    ])
    inc_map = _extract_map([
        "OperatingIncomeLoss",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        # Also search for company-specific "Adjusted" variants by suffix
    ])
    # If no standard op income, try any concept with "OperatingIncome" or "AdjustedOperatingIncome" in name
    if not inc_map:
        for c, data in all_facts.items():
            if "OperatingIncome" in c or "AdjustedOperatingIncome" in c:
                inc_map = data
                break

    gw_map = _extract_map([
        "GoodwillImpairmentLoss",
        "GoodwillImpairmentLossExcludingImpairmentLossOnTheDisposalGroupHeldForSale",
        "GoodwillAndIntangibleAssetImpairment",
    ])

    return rev_map, inc_map, gw_map, "INLINE_XBRL", filing_index_url, primary_doc_url, filing_date


def score_segments(cik: str, subs: dict) -> Tuple[list, str, str, str, str]:
    """
    Returns (segments, parse_status, filing_index_url, primary_doc_url, filing_date).
    Fetches inline XBRL from the latest 10-K filing to get real segment data.
    """
    rev_map, inc_map, gw_map, source, filing_index_url, primary_doc_url, filing_date = \
        _fetch_inline_xbrl_segments(cik, subs)

    all_segs = set(rev_map) | set(inc_map)
    if not all_segs:
        return [], (source if source != "INLINE_XBRL" else "UNPARSED"), filing_index_url, primary_doc_url, filing_date

    # Determine the 3 most recent annual period-ends across all segments
    all_periods: set = set()
    for d in list(rev_map.values()) + list(inc_map.values()):
        all_periods.update(d.keys())
    sorted_periods = sorted(all_periods, reverse=True)
    annual_periods: list = []
    for p in sorted_periods:
        if not annual_periods or annual_periods[-1][:4] != p[:4]:
            annual_periods.append(p)
        if len(annual_periods) >= 4:
            break

    if not annual_periods:
        return [], "UNPARSED"

    latest = annual_periods[0]

    def latest_val(m: dict, seg: str) -> Optional[float]:
        yrs = m.get(seg, {})
        if not yrs:
            return None
        return yrs.get(latest) or yrs.get(sorted(yrs.keys(), reverse=True)[0])

    def val_for(m: dict, seg: str, period: str) -> Optional[float]:
        return m.get(seg, {}).get(period)

    # Company-wide totals (sum of positive segment values)
    total_rev = sum(v for s in all_segs for v in [latest_val(rev_map, s)] if v and v > 0)
    total_inc = sum(v for s in all_segs for v in [latest_val(inc_map, s)] if v is not None)
    company_margin = (total_inc / total_rev * 100) if total_rev else None

    # Best segment margin (for "widest gap" flag)
    seg_margins: list = []
    for s in all_segs:
        r = latest_val(rev_map, s) or 0
        i = latest_val(inc_map, s)
        if r > 0 and i is not None:
            seg_margins.append(i / r * 100)
    best_margin = max(seg_margins) if seg_margins else None

    segments = []
    for seg in sorted(all_segs):
        rev0 = latest_val(rev_map, seg) or 0
        inc0 = latest_val(inc_map, seg)
        gw_imp = latest_val(gw_map, seg) or 0

        # YoY revenue trend (3 years)
        rev_hist = []
        for p in annual_periods[:4]:
            v = val_for(rev_map, seg, p)
            if v is not None:
                rev_hist.append((p[:4], v))
        yoy = []
        for i in range(len(rev_hist) - 1):
            yr, vn = rev_hist[i]
            _, vo = rev_hist[i + 1]
            if vo and vo != 0:
                yoy.append((yr, (vn - vo) / abs(vo) * 100))

        margin = (inc0 / rev0 * 100) if rev0 > 0 and inc0 is not None else None

        # --- Segment score ---
        seg_score = 0
        flags = []

        # +3 negative op income / +2 below company margin (take higher, not both)
        if inc0 is not None and inc0 < 0:
            seg_score += 3
            flags.append(f"Op LOSS ${inc0/1e6:.0f}M → +3")
        elif margin is not None and company_margin is not None and margin < company_margin:
            seg_score += 2
            flags.append(f"Margin {margin:.1f}% < co avg {company_margin:.1f}% → +2")

        # +2 declining 3-year revenue
        neg_yoy = [g for _, g in yoy if g < 0]
        if len(neg_yoy) >= 2:
            seg_score += 2
            flags.append(f"Revenue declining {len(neg_yoy)} of last {len(yoy)} years → +2")
        elif len(neg_yoy) == 1 and yoy:
            flags.append(f"Revenue declined {yoy[0][0]} ({yoy[0][1]:+.1f}%)")

        # +1 widest margin gap vs best segment
        if margin is not None and best_margin is not None and seg_margins:
            gap = best_margin - margin
            if gap == max(best_margin - m for m in seg_margins) and gap > 5:
                seg_score += 1
                flags.append(f"Widest margin gap: {gap:.1f}pp below best segment → +1")

        # +2 catch-all / orphan bucket
        if _is_catchall(seg):
            seg_score += 2
            flags.append(f"Corporate/All Other catch-all bucket → +2")

        # +2 goodwill impairment (last 2 years)
        if gw_imp > 0:
            gw_yrs = [p[:4] for p in gw_map.get(seg, {}).keys()
                      if int(p[:4]) >= datetime.now().year - 2]
            if gw_yrs:
                seg_score += 2
                flags.append(f"Goodwill impairment ${gw_imp/1e6:.0f}M ({', '.join(gw_yrs)}) → +2")

        yoy_str = " | ".join(f"{yr}: {g:+.1f}%" for yr, g in yoy) if yoy else "N/A"

        segments.append({
            "Segment": seg,
            "Revenue_M": round(rev0 / 1e6, 1) if rev0 else None,
            "OpIncome_M": round(inc0 / 1e6, 1) if inc0 is not None else None,
            "Margin_pct": round(margin, 1) if margin is not None else None,
            "Co_Avg_Margin_pct": round(company_margin, 1) if company_margin is not None else None,
            "Revenue_YoY_3yr": yoy_str,
            "Goodwill_Imp_M": round(gw_imp / 1e6, 1) if gw_imp else None,
            "Is_Catchall": _is_catchall(seg),
            "Seg_Score": seg_score,
            "Seg_Flags": " | ".join(flags) if flags else "—",
        })

    segments.sort(key=lambda x: x["Seg_Score"], reverse=True)
    return segments, "XBRL", filing_index_url, primary_doc_url, filing_date


# ---------------------------------------------------------------------------
# Master per-company scorer
# ---------------------------------------------------------------------------

def score_company(company_name: str, revenue_M: float) -> dict:
    """
    Resolves CIK, pulls XBRL facts and filings, computes parent + segment scores.
    Returns a dict with all factor columns.
    """
    base = {
        "Company": company_name,
        "Revenue_M_parent": revenue_M,
        "CIK": None, "Ticker": None, "Match_Score": 0, "Matched_SEC_Name": "",
        "P_Leverage": 0, "P_SegCount": 0, "P_CsuiteChange": 0, "P_Activist": 0,
        "P_RevDecline": 0, "P_GuidanceCut": 0, "Parent_Score": 0,
        "Parent_Notes": "",
        "Segments": [], "Parse_Status": "NOT_ATTEMPTED",
        "Filing_Index_URL": "", "Filing_Doc_URL": "", "Filing_Date": "",
    }

    # Resolve CIK
    try:
        cik, ticker, match_score, matched_title = resolve_cik(company_name)
    except Exception as e:
        base["Parse_Status"] = f"CIK_FAIL: {e}"
        PARSE_LOG.append({"Company": company_name, "Status": "CIK_FAIL", "Detail": str(e)})
        return base

    base.update({"CIK": cik, "Ticker": ticker, "Match_Score": match_score, "Matched_SEC_Name": matched_title})

    if match_score < 75:
        base["Parse_Status"] = f"LOW_CONFIDENCE ({match_score}%)"
        PARSE_LOG.append({"Company": company_name, "Status": "LOW_CONF", "Detail": f"{match_score}% → {matched_title}"})
        return base

    # Get submissions
    subs = _get_submissions(cik)
    if not subs:
        base["Parse_Status"] = "SUBMISSIONS_FAIL"
        PARSE_LOG.append({"Company": company_name, "Status": "SUBMISSIONS_FAIL", "Detail": ""})
        return base

    # Get XBRL facts
    facts = _get(f"{BASE_URL}/api/xbrl/companyfacts/CIK{cik}.json", timeout=30)
    if not facts:
        base["Parse_Status"] = "XBRL_FAIL"
        PARSE_LOG.append({"Company": company_name, "Status": "XBRL_FAIL", "Detail": ""})
        return base

    # Parent factors
    p_lev, n_lev   = _score_leverage(facts)
    p_seg, n_seg, _segs_found = _score_segment_count(facts)
    p_ceo, n_ceo   = _score_csuite_change(subs, cik)
    p_act, n_act   = _score_activist(cik, company_name=company_name)
    p_rev, n_rev   = _score_rev_decline(facts)
    p_cut, n_cut   = _score_guidance_cut(subs, cik)

    parent_score = min(10, p_lev + p_seg + p_ceo + p_act + p_rev + p_cut)
    parent_notes = " | ".join([n_lev, n_seg, n_ceo, n_act, n_rev, n_cut])

    # Segment factors — uses inline XBRL from latest 10-K, not companyfacts API
    segments, parse_status, filing_index_url, filing_doc_url, filing_date = score_segments(cik, subs)

    base.update({
        "P_Leverage": p_lev, "P_SegCount": p_seg,
        "P_CsuiteChange": p_ceo, "P_Activist": p_act,
        "P_RevDecline": p_rev, "P_GuidanceCut": p_cut,
        "Parent_Score": parent_score, "Parent_Notes": parent_notes,
        "Segments": segments, "Parse_Status": parse_status,
        "Filing_Index_URL": filing_index_url,
        "Filing_Doc_URL": filing_doc_url,
        "Filing_Date": filing_date,
    })

    PARSE_LOG.append({
        "Company": company_name, "Status": parse_status,
        "Segments_Found": len(segments),
        "Detail": f"CIK {cik}, match {match_score}% | {filing_index_url}"
    })
    return base


# ---------------------------------------------------------------------------
# Batch runner + flat output builder
# ---------------------------------------------------------------------------

def run_batch(companies: list, verbose: bool = True) -> pd.DataFrame:
    """
    companies: list of (company_name, revenue_M) tuples.
    Returns flat DataFrame with one row per (company, segment).
    Companies with no segment data get one row with Segment='N/A'.
    """
    rows = []
    total = len(companies)

    for i, (name, rev) in enumerate(companies, 1):
        if verbose:
            log.info(f"[{i}/{total}] {name}  (${rev:,.0f}M)")
        result = score_company(name, rev)

        parent_cols = {k: result[k] for k in [
            "Company", "Revenue_M_parent", "CIK", "Ticker", "Match_Score",
            "Matched_SEC_Name", "P_Leverage", "P_SegCount", "P_CsuiteChange",
            "P_Activist", "P_RevDecline", "P_GuidanceCut", "Parent_Score",
            "Parent_Notes", "Parse_Status",
            "Filing_Date", "Filing_Index_URL", "Filing_Doc_URL",
        ]}

        segs = result.get("Segments", [])
        if segs:
            for seg in segs:
                row = {**parent_cols, **seg}
                row["Propensity_Score"] = min(20, parent_cols["Parent_Score"] + seg["Seg_Score"])
                rows.append(row)
        else:
            row = {**parent_cols,
                   "Segment": "N/A", "Revenue_M": None, "OpIncome_M": None,
                   "Margin_pct": None, "Co_Avg_Margin_pct": None,
                   "Revenue_YoY_3yr": "N/A", "Goodwill_Imp_M": None,
                   "Is_Catchall": False, "Seg_Score": 0, "Seg_Flags": "—",
                   "Propensity_Score": parent_cols["Parent_Score"]}
            rows.append(row)

    df = pd.DataFrame(rows)

    # Sort: highest propensity first, then by segment score
    df = df.sort_values(["Propensity_Score", "Seg_Score"], ascending=False).reset_index(drop=True)
    return df
