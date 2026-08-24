"""
Phase A2 — Language Engine
Section-aware keyword scanner producing a timing signal (EXPLORATORY/PENDING/COMPLETED/NONE)
per (company, segment) pair from 10-K MD&A and 8-K filings.

Scans ONLY:
  10-K: Item 7 (MD&A), Item 8 (Financial Statements / Segment Note)
  8-K:  Items 1.01, 2.05, 2.06, 5.02, 7.01, 8.01

Excludes (strips before scanning):
  Item 1A (Risk Factors), EX-10.x compensation exhibits, GAAP reconciliation tables,
  forward-looking statement disclaimers, exhibit indexes

Output per segment:
  EXPLORATORY  — company is actively evaluating/exploring a sale; process not yet announced
  PENDING      — transaction publicly announced, not yet closed
  COMPLETED    — sale/spin closed or terminated
  NONE         — no meaningful divestiture signal in allowed sections
"""

import re
import time
import logging
from typing import Optional, Tuple

import requests

log = logging.getLogger("language_engine")

HEADERS = {"User-Agent": "WoodsonEquity eshandoshi1@gmail.com", "Accept-Encoding": "gzip, deflate"}
EDGAR_BASE = "https://www.sec.gov"
DATA_BASE  = "https://data.sec.gov"
RATE_LIMIT = 0.12


def _get_text(url: str, timeout: int = 30) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        time.sleep(RATE_LIMIT)
        return r.text
    except Exception:
        time.sleep(RATE_LIMIT)
        return ""


def _get_json(url: str, timeout: int = 20) -> Optional[dict]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        time.sleep(RATE_LIMIT)
        return r.json()
    except Exception:
        time.sleep(RATE_LIMIT)
        return None


# ---------------------------------------------------------------------------
# Section extraction: carve the filing into labeled text blocks
# ---------------------------------------------------------------------------

# Boundary patterns that mark the START of each item in 10-K filings
# We match case-insensitively; order matters (earlier = higher priority)
ITEM_BOUNDARIES_10K = [
    (re.compile(r'item\s+1a[\.\s]', re.I),       "RISK_FACTORS"),
    (re.compile(r'item\s+1b[\.\s]', re.I),        "UNRESOLVED_COMMENTS"),
    (re.compile(r'item\s+2[\.\s]', re.I),         "PROPERTIES"),
    (re.compile(r'item\s+7a[\.\s]', re.I),        "QUANT_DISCLOSURES"),
    (re.compile(r'item\s+7[\.\s]', re.I),         "MDA"),       # must come after 7a
    (re.compile(r'item\s+8[\.\s]', re.I),         "FIN_STMTS"),
    (re.compile(r'item\s+9a[\.\s]', re.I),        "CONTROLS"),
    (re.compile(r'item\s+9[\.\s]', re.I),         "DISAGREEMENTS"),
    (re.compile(r'item\s+10[\.\s]', re.I),        "GOVERNANCE"),
    (re.compile(r'item\s+15[\.\s]', re.I),        "EXHIBITS"),
    (re.compile(r'item\s+16[\.\s]', re.I),        "EXHIBITS"),
]

ALLOWED_SECTIONS_10K  = {"MDA", "FIN_STMTS"}
ALLOWED_SECTIONS_8K   = {"1.01", "2.05", "2.06", "5.02", "7.01", "8.01"}

# Noise patterns: strip these blocks before scanning even within allowed sections
_NOISE_PATTERNS = [
    # Forward-looking statement boilerplate (typically in a paragraph header)
    re.compile(
        r'(?:cautionary|forward.looking\s+statements?|safe\s+harbor)'
        r'.{0,2000}?(?=\n\n|\Z)', re.I | re.DOTALL
    ),
    # Reconciliation tables (GAAP → non-GAAP)
    re.compile(
        r'(?:reconciliation\s+of|non.gaap\s+(?:financial\s+)?(?:measure|result))'
        r'.{0,3000}?(?=\n\n[A-Z]|\Z)', re.I | re.DOTALL
    ),
    # Exhibit index tables
    re.compile(
        r'exhibit\s+(?:index|list|number).{0,2000}?(?=\n\n|\Z)', re.I | re.DOTALL
    ),
]


def _strip_html(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'\s{3,}', '  ', text)
    return text


def _extract_allowed_sections_10k(full_text: str) -> str:
    """
    Finds Item 7 and Item 8 blocks in a 10-K, strips noise, returns combined text.
    """
    # Find all item boundary positions
    boundaries = []
    for pattern, label in ITEM_BOUNDARIES_10K:
        for m in pattern.finditer(full_text):
            boundaries.append((m.start(), label))
    boundaries.sort(key=lambda x: x[0])

    if not boundaries:
        return full_text  # fallback: scan everything

    # Build (start, end, label) segments
    segments = []
    for i, (pos, label) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(full_text)
        segments.append((pos, end, label))

    # Collect allowed sections
    allowed_text = []
    for start, end, label in segments:
        if label in ALLOWED_SECTIONS_10K:
            chunk = full_text[start:end]
            allowed_text.append(chunk)

    combined = "\n\n".join(allowed_text)

    # Strip noise within allowed sections
    for pattern in _NOISE_PATTERNS:
        combined = pattern.sub(' ', combined)

    return combined if combined.strip() else full_text


def _extract_allowed_sections_8k(full_text: str) -> str:
    """
    Extract only the listed items from an 8-K (1.01, 2.05, 2.06, 5.02, 7.01, 8.01).
    """
    item_pattern = re.compile(
        r'item\s+(\d+\.\d+)\b', re.I
    )
    boundaries = [(m.start(), m.group(1)) for m in item_pattern.finditer(full_text)]
    if not boundaries:
        return full_text

    allowed_text = []
    for i, (pos, item_num) in enumerate(boundaries):
        if item_num in ALLOWED_SECTIONS_8K:
            end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(full_text)
            allowed_text.append(full_text[pos:end])

    combined = "\n\n".join(allowed_text)
    return combined if combined.strip() else ""


# ---------------------------------------------------------------------------
# Keyword taxonomy — tiered by timing signal strength
# ---------------------------------------------------------------------------

# COMPLETED: transaction already closed or terminated
COMPLETED_KEYWORDS = [
    "completed the sale of",
    "completed the divestiture",
    "closed the sale of",
    "closed the divestiture",
    "successfully divested",
    "transaction has closed",
    "divestiture was completed",
    "spin-off was completed",
    "spinoff was completed",
    "carve-out was completed",
    "sale was completed",
    "sold our",
    "divested our",
    "terminated the strategic review",
    "discontinued our",
    "no longer own",
]

# PENDING: announced and in process
PENDING_KEYWORDS = [
    "entered into an agreement to sell",
    "entered into a definitive agreement",
    "definitive agreement to divest",
    "announced the sale of",
    "announced a definitive agreement",
    "agreed to sell",
    "signed an agreement to sell",
    "expect to complete the sale",
    "pending regulatory approval",
    "subject to regulatory approval",
    "subject to customary closing conditions",
    "transaction is expected to close",
    "anticipated to close",
    "sale is expected to close",
    "separation is expected to",
    "spin-off is expected to",
    "spinoff is expected to",
]

# EXPLORATORY: evaluation underway but not yet announced
EXPLORATORY_KEYWORDS = [
    "strategic alternatives",
    "exploring strategic alternatives",
    "evaluating strategic alternatives",
    "reviewing strategic alternatives",
    "considering strategic alternatives",
    "retained a financial advisor",
    "retained financial advisors",
    "engaged a financial advisor",
    "engaged financial advisors",
    "engaged an investment bank",
    "retained an investment bank",
    "exploring a potential sale",
    "evaluating a potential sale",
    "exploring a sale",
    "actively exploring",
    "exploring options for",
    "evaluating all options",
    "evaluate all options",
    "strategic review",
    "portfolio review",
    "portfolio optimization",
    "reviewing our portfolio",
    "simplify our portfolio",
    "simplify the portfolio",
    "return to core",
    "focus on core",
    "focusing on core",
    "non-core",
    "non core",
    "under review",
]

# Weak signal — meaningful only when near a segment name
SOFT_KEYWORDS = [
    "divest",
    "divestiture",
    "spin-off",
    "spinoff",
    "carve-out",
    "discontinue",
    "separation of",
    "held for sale",
    "discontinued operations",
    "restructuring",
    "portfolio optimization",
    "streamline our operations",
    "streamline operations",
]

ALL_KEYWORDS = list(dict.fromkeys(
    COMPLETED_KEYWORDS + PENDING_KEYWORDS + EXPLORATORY_KEYWORDS + SOFT_KEYWORDS
))


def _extract_context(text: str, start: int, end: int, window: int = 300) -> str:
    s = max(0, start - window)
    e = min(len(text), end + window)
    snippet = text[s:e].strip()
    return re.sub(r'\s+', ' ', snippet)


# ---------------------------------------------------------------------------
# Segment name matching: does a keyword hit mention a known segment?
# ---------------------------------------------------------------------------

def _clean_seg_token(name: str) -> str:
    """Reduce segment member name to searchable tokens."""
    name = re.sub(r'(Segment|Member|Division|Group|Business|Unit)\b', '', name, flags=re.I)
    name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    name = name.strip().lower()
    return name


def _hit_mentions_segment(context: str, seg_names: list) -> Optional[str]:
    """Return the first segment name found in the context snippet, or None."""
    ctx_low = context.lower()
    for seg in seg_names:
        token = _clean_seg_token(seg)
        if token and len(token) > 3 and token in ctx_low:
            return seg
    return None


# ---------------------------------------------------------------------------
# Signal classification
# ---------------------------------------------------------------------------

def _classify_hits(hits: list) -> str:
    """
    Given a list of hits (each with a 'tier' key), return the highest timing signal:
    COMPLETED > PENDING > EXPLORATORY > NONE
    """
    tiers = {h["tier"] for h in hits}
    if "COMPLETED" in tiers:
        return "COMPLETED"
    if "PENDING" in tiers:
        return "PENDING"
    if "EXPLORATORY" in tiers:
        return "EXPLORATORY"
    return "NONE"


def _scan_text(text: str, company: str, seg_names: list, filing_url: str,
               form_type: str, filing_date: str) -> list:
    """
    Scan pre-filtered text for keywords. Returns list of hit dicts.
    """
    text_lower = text.lower()
    hits = []

    def _check(kw_list: list, tier: str):
        for kw in kw_list:
            for m in re.finditer(re.escape(kw.lower()), text_lower):
                ctx = _extract_context(text, m.start(), m.end())
                mentioned_seg = _hit_mentions_segment(ctx, seg_names)
                # For SOFT keywords, require a segment mention to count
                if tier == "SOFT" and not mentioned_seg:
                    continue
                hits.append({
                    "keyword": kw,
                    "tier": tier if tier != "SOFT" else "EXPLORATORY",
                    "context": ctx,
                    "filing_url": filing_url,
                    "form_type": form_type,
                    "filing_date": filing_date,
                    "mentioned_segment": mentioned_seg,
                })

    _check(COMPLETED_KEYWORDS,   "COMPLETED")
    _check(PENDING_KEYWORDS,     "PENDING")
    _check(EXPLORATORY_KEYWORDS, "EXPLORATORY")
    _check(SOFT_KEYWORDS,        "SOFT")

    return hits


# ---------------------------------------------------------------------------
# EDGAR fetchers
# ---------------------------------------------------------------------------

def _recent_filings(subs: dict, form_type: str, max_count: int = 3) -> list:
    recent = subs.get("filings", {}).get("recent", {})
    out = []
    for form, date_str, acc, doc in zip(
        recent.get("form", []),
        recent.get("filingDate", []),
        recent.get("accessionNumber", []),
        recent.get("primaryDocument", []),
    ):
        if form.startswith(form_type):
            out.append({"form": form, "date": date_str, "acc": acc, "doc": doc})
            if len(out) >= max_count:
                break
    return out


def _fetch_filing_text(cik: str, acc: str, doc: str) -> str:
    acc_nd = acc.replace("-", "")
    url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{doc}"
    raw = _get_text(url, timeout=45)
    return _strip_html(raw)


def _filing_url(cik: str, acc: str, doc: str) -> str:
    acc_nd = acc.replace("-", "")
    return f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{doc}"


# ---------------------------------------------------------------------------
# Main scanner: one company, returns per-segment timing signals
# ---------------------------------------------------------------------------

def scan_company(
    company: str,
    cik: str,
    subs: dict,
    seg_names: list,
    scan_10k: bool = True,
    scan_8k: bool = True,
    max_8k: int = 6,
) -> dict:
    """
    Scans the latest 10-K and recent 8-Ks for divestiture language.
    Returns:
      {
        "timing_signal": "EXPLORATORY" | "PENDING" | "COMPLETED" | "NONE",
        "all_hits": [...],
        "per_segment": {seg_name: {"signal": ..., "hits": [...]}},
        "source_filings": [...],
      }
    """
    all_hits = []
    source_filings = []

    # --- Scan latest 10-K ---
    if scan_10k:
        ten_ks = _recent_filings(subs, "10-K", max_count=1)
        for filing in ten_ks:
            raw_text = _fetch_filing_text(cik, filing["acc"], filing["doc"])
            if not raw_text:
                continue
            allowed = _extract_allowed_sections_10k(raw_text)
            url = _filing_url(cik, filing["acc"], filing["doc"])
            hits = _scan_text(allowed, company, seg_names, url, filing["form"], filing["date"])
            all_hits.extend(hits)
            source_filings.append({
                "form": filing["form"],
                "date": filing["date"],
                "url": url,
                "hits": len(hits),
                "chars_scanned": len(allowed),
            })

    # --- Scan recent 8-Ks ---
    if scan_8k:
        eight_ks = _recent_filings(subs, "8-K", max_count=max_8k)
        for filing in eight_ks:
            raw_text = _fetch_filing_text(cik, filing["acc"], filing["doc"])
            if not raw_text:
                continue
            allowed = _extract_allowed_sections_8k(raw_text)
            if not allowed:
                allowed = raw_text  # some 8-Ks have no item headers, scan all
            url = _filing_url(cik, filing["acc"], filing["doc"])
            hits = _scan_text(allowed, company, seg_names, url, filing["form"], filing["date"])
            all_hits.extend(hits)
            if hits:
                source_filings.append({
                    "form": filing["form"],
                    "date": filing["date"],
                    "url": url,
                    "hits": len(hits),
                    "chars_scanned": len(allowed),
                })

    # --- Overall company-level signal ---
    overall_signal = _classify_hits(all_hits)

    # --- Per-segment signals ---
    per_segment = {}
    for seg in seg_names:
        seg_hits = [h for h in all_hits if h.get("mentioned_segment") == seg]
        per_segment[seg] = {
            "signal": _classify_hits(seg_hits) if seg_hits else "NONE",
            "hits": seg_hits,
        }

    # Any unattributed hits (mentioned_segment is None) inform the company signal
    # but are not assigned to a specific segment

    return {
        "timing_signal": overall_signal,
        "all_hits": all_hits,
        "per_segment": per_segment,
        "source_filings": source_filings,
    }


# ---------------------------------------------------------------------------
# Batch runner: merge with A1 conditions output
# ---------------------------------------------------------------------------

def run_language_batch(a1_df, verbose: bool = True) -> "pd.DataFrame":
    """
    Takes the A1 conditions DataFrame (one row per company/segment),
    adds Language Engine columns: Timing_Signal, Language_Hits, Source_Filings.
    Returns the augmented DataFrame.
    """
    import pandas as pd
    from conditions_engine import _get_submissions

    results = []
    processed_ciks = {}  # cache scan results per CIK so we only fetch once per company

    companies = a1_df[["Company", "CIK", "Segment"]].drop_duplicates("Company")

    for _, row in companies.iterrows():
        company = row["Company"]
        cik = str(row["CIK"]) if row["CIK"] else None
        if not cik or cik == "None":
            processed_ciks[company] = {
                "timing_signal": "NO_CIK",
                "all_hits": [],
                "per_segment": {},
                "source_filings": [],
            }
            continue

        cik_padded = cik.zfill(10)
        subs = _get_submissions(cik_padded)
        if not subs:
            processed_ciks[company] = {
                "timing_signal": "FETCH_FAIL",
                "all_hits": [],
                "per_segment": {},
                "source_filings": [],
            }
            continue

        # Collect segment names for this company from A1 output
        co_rows = a1_df[a1_df["Company"] == company]
        seg_names = [str(s) for s in co_rows["Segment"].tolist()
                     if s and str(s) != "N/A" and "Corporate" not in str(s) and "All Other" not in str(s)]

        if verbose:
            log.info(f"  {company} | CIK {cik} | {len(seg_names)} segments")

        scan = scan_company(company, cik_padded, subs, seg_names)
        processed_ciks[company] = scan

    # Build output rows (one per company/segment, preserving A1 columns)
    out_rows = []
    for _, row in a1_df.iterrows():
        company = row["Company"]
        seg = row["Segment"]
        scan = processed_ciks.get(company, {})

        seg_result = scan.get("per_segment", {}).get(seg, {})
        seg_signal = seg_result.get("signal", "NONE")
        seg_hits = seg_result.get("hits", [])

        # Company-level fallback if no segment-specific hits
        company_signal = scan.get("timing_signal", "NONE")

        # Summarize hits for output
        hit_summary = " | ".join(
            f"{h['keyword']} ({h['form_type']} {h['filing_date'][:7]})"
            for h in seg_hits[:5]
        )
        source_urls = " | ".join(
            f['url'] for f in scan.get("source_filings", [])[:3] if f.get("hits", 0) > 0
        )

        new_row = row.to_dict()
        new_row["Seg_Timing_Signal"] = seg_signal
        new_row["Co_Timing_Signal"]  = company_signal
        new_row["Language_Hits"]     = hit_summary or "—"
        new_row["Language_Source_URLs"] = source_urls or "—"
        new_row["Language_Hit_Count"] = len(seg_hits)
        out_rows.append(new_row)

    return pd.DataFrame(out_rows)
