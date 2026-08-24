"""
Quarterly Signals Engine — 10-Q + 8-K content expansion

Adds four signal layers not covered by the annual 10-K pass:

1. 10-Q segment financials — quarterly revenue/margin from the last 2-3 10-Qs,
   catching mid-year deterioration before the next annual filing

2. 10-Q MD&A (Item 2) — quarterly management commentary on segments, often
   more candid than the annual 10-K (less legal polish)

3. 8-K Item 2.05 — material restructuring charges attributed to a segment
   (common precursor to segment sale/wind-down)

4. 8-K Item 2.06 — material goodwill/asset impairment linked to a segment
   (strong signal: management is writing down the value they expect to recover)

Output per company:
  {
    "Q_Seg_Revenue_Trend":  "DECLINING" | "FLAT" | "IMPROVING" | "UNKNOWN",
    "Q_Seg_Margin_Gap":     float or None,   # most recent Q vs company avg
    "Q_Restructuring_Hit":  bool,
    "Q_Impairment_Hit":     bool,
    "Q_Language_Signal":    "NONE" | "EXPLORATORY" | "PENDING" | "COMPLETED",
    "Q_Language_Hits":      list of str,
    "Q_Source_URLs":        list of str,
    "Q_Score_Delta":        int,             # additional points to add to A1 scores
    "Q_Notes":              str,
  }
"""

import re
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

log = logging.getLogger("quarterly_signals")

HEADERS = {"User-Agent": "WoodsonEquity eshandoshi1@gmail.com", "Accept-Encoding": "gzip, deflate"}
EDGAR_BASE = "https://www.sec.gov"
DATA_BASE  = "https://data.sec.gov"
RATE_LIMIT = 0.13

# ---------------------------------------------------------------------------
# Language keywords for 10-Q / 8-K content scanning
# ---------------------------------------------------------------------------

COMPLETED_KW  = ["completed the sale", "closed the transaction", "divested", "completed divestiture",
                  "transaction closed", "successfully sold"]
PENDING_KW    = ["definitive agreement to sell", "entered into an agreement", "subject to regulatory",
                  "expected to close", "anticipated to close", "pending regulatory approval"]
EXPLORATORY_KW = ["strategic alternatives", "exploring options", "portfolio review", "evaluating strategic",
                   "non-core", "considering a sale", "engaged financial advisor", "retained advisor",
                   "simplify our portfolio", "focus on core", "restructuring our portfolio",
                   "reviewing all options", "investor day", "spinning off", "potential separation"]
RESTRUCTURE_KW = ["restructuring charge", "restructuring costs", "severance and restructuring",
                   "exit costs", "facility closure", "plant closure", "headcount reduction",
                   "reorganization charge", "workforce reduction"]
IMPAIRMENT_KW  = ["goodwill impairment", "impairment charge", "impairment of goodwill",
                   "asset impairment", "impairment loss", "write-down", "write-off of goodwill"]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 20) -> Optional[dict]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        time.sleep(RATE_LIMIT)
        return r.json()
    except Exception as e:
        time.sleep(RATE_LIMIT)
        log.debug(f"GET failed: {url[:80]} — {e}")
        return None


def _get_text(url: str, timeout: int = 40) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        time.sleep(RATE_LIMIT)
        return r.text
    except Exception:
        time.sleep(RATE_LIMIT)
        return ""


def _strip_html(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;|&amp;|&lt;|&gt;|&#\d+;|&[a-z]+;', ' ', text)
    return re.sub(r'\s{3,}', '  ', text).strip()


# ---------------------------------------------------------------------------
# Filing helpers
# ---------------------------------------------------------------------------

def _recent_filings(subs: dict, form_type: str, max_count: int = 4) -> list:
    """Return list of recent filings of the given form type."""
    recent = subs.get("filings", {}).get("recent", {})
    results = []
    for form, date, acc, doc in zip(
        recent.get("form", []),
        recent.get("filingDate", []),
        recent.get("accessionNumber", []),
        recent.get("primaryDocument", []),
    ):
        if form.startswith(form_type):
            results.append({"form": form, "date": date, "acc": acc, "doc": doc})
        if len(results) >= max_count:
            break
    return results


def _fetch_doc_text(cik: str, acc: str, doc: str) -> tuple:
    """Fetch filing document text. Returns (text, url)."""
    cik_int = int(cik.lstrip("0") or "0")
    acc_nd = acc.replace("-", "")

    if doc:
        url = f"{EDGAR_BASE}/Archives/edgar/data/{cik_int}/{acc_nd}/{doc}"
        raw = _get_text(url)
        if raw and len(raw) > 200:
            return _strip_html(raw), url

    # Fallback: scrape directory for primary htm
    dir_url = f"{EDGAR_BASE}/Archives/edgar/data/{cik_int}/{acc_nd}/"
    dir_html = _get_text(dir_url)
    if not dir_html:
        return "", ""

    links = re.findall(r'href="(/Archives/edgar/data/[^"]+\.htm[l]?)"', dir_html, re.I)
    for link in links:
        fname = link.split("/")[-1].lower()
        if "index" not in fname and "ex" not in fname[:3]:
            url = f"{EDGAR_BASE}{link}"
            raw = _get_text(url)
            if raw and len(raw) > 500:
                return _strip_html(raw), url

    return "", ""


# ---------------------------------------------------------------------------
# 1. 10-Q segment financial data
# ---------------------------------------------------------------------------

def _extract_10q_segments_xbrl(cik: str, acc: str) -> dict:
    """
    Extract segment revenue and operating income from 10-Q inline XBRL.
    Returns {segment_name: {revenue: float, op_income: float}} or {}.
    """
    cik_int = int(cik.lstrip("0") or "0")
    acc_nd = acc.replace("-", "")

    dir_url = f"{EDGAR_BASE}/Archives/edgar/data/{cik_int}/{acc_nd}/"
    dir_html = _get_text(dir_url)
    if not dir_html:
        return {}

    # Find _htm.xml (inline XBRL)
    xml_links = re.findall(r'href="(/Archives/edgar/data/[^"]+_htm\.xml)"', dir_html)
    if not xml_links:
        return {}

    xml_url = f"{EDGAR_BASE}{xml_links[0]}"
    xml_text = _get_text(xml_url)
    if not xml_text:
        return {}

    # Parse segment-dimensional facts (same pattern as conditions_engine)
    fact_pattern = re.compile(
        r'<([^\s>]+)\s[^>]*contextRef="([^"]+)"[^>]*decimals="(-?\d+)"[^>]*>([^<]+)</',
        re.S
    )

    rev_map: dict = {}
    inc_map: dict = {}

    for tag, ctx, decimals, val_str in fact_pattern.findall(xml_text):
        tag_lower = tag.lower()
        is_rev = any(t in tag_lower for t in ["revenues", "netsales", "revenue"])
        is_inc = any(t in tag_lower for t in ["operatingincome", "operatingprofit", "incomefromoperations"])

        if not (is_rev or is_inc):
            continue

        # Only capture segment-dimensional contexts (contain member/segment keywords)
        ctx_lower = ctx.lower()
        if not any(k in ctx_lower for k in ["member", "segment", "division", "operating"]):
            continue

        try:
            val = float(val_str.strip())
        except ValueError:
            continue

        if is_rev:
            if ctx not in rev_map or abs(val) > abs(rev_map[ctx]):
                rev_map[ctx] = val
        elif is_inc:
            if ctx not in inc_map or abs(val) > abs(inc_map[ctx]):
                inc_map[ctx] = val

    if not rev_map:
        return {}

    # Combine
    all_contexts = set(rev_map) | set(inc_map)
    segments = {}
    for ctx in all_contexts:
        # Use context as segment name (simplified — real name from entity label would need more parsing)
        seg_name = ctx.replace("Member", "").replace("_", " ").strip()
        segments[seg_name] = {
            "revenue": rev_map.get(ctx),
            "op_income": inc_map.get(ctx),
        }

    return segments


def analyze_quarterly_segment_trend(cik: str, subs: dict, seg_names: list) -> dict:
    """
    Pull last 3 10-Qs and compute revenue trend + margin gap per segment.
    Returns {segment_name: {trend: str, margin_gap: float}} or {}.
    """
    filings = _recent_filings(subs, "10-Q", max_count=3)
    if not filings:
        return {}

    quarterly_data = []  # list of {seg: {revenue, op_income}} per quarter
    for f in filings:
        segs = _extract_10q_segments_xbrl(cik, f["acc"])
        if segs:
            quarterly_data.append({"date": f["date"], "segments": segs})

    if not quarterly_data:
        return {}

    results = {}
    most_recent = quarterly_data[0]["segments"]

    # Compute company-wide average margin from most recent quarter
    total_rev = sum(s.get("revenue") or 0 for s in most_recent.values() if s.get("revenue"))
    total_inc = sum(s.get("op_income") or 0 for s in most_recent.values() if s.get("op_income") is not None)
    co_avg_margin = (total_inc / total_rev) if total_rev > 0 else None

    for ctx_name, data in most_recent.items():
        rev = data.get("revenue")
        inc = data.get("op_income")

        seg_margin = (inc / rev) if rev and rev != 0 else None
        margin_gap = (seg_margin - co_avg_margin) if seg_margin is not None and co_avg_margin is not None else None

        # Revenue trend: compare most recent Q vs 2 quarters ago
        trend = "UNKNOWN"
        if len(quarterly_data) >= 3:
            older = quarterly_data[2]["segments"].get(ctx_name, {})
            older_rev = older.get("revenue")
            if rev and older_rev and older_rev != 0:
                change = (rev - older_rev) / abs(older_rev)
                if change < -0.05:
                    trend = "DECLINING"
                elif change > 0.05:
                    trend = "IMPROVING"
                else:
                    trend = "FLAT"

        results[ctx_name] = {
            "revenue_M": round(rev / 1e6, 1) if rev else None,
            "op_income_M": round(inc / 1e6, 1) if inc else None,
            "margin_pct": round(seg_margin * 100, 1) if seg_margin is not None else None,
            "margin_gap_pct": round(margin_gap * 100, 1) if margin_gap is not None else None,
            "revenue_trend": trend,
        }

    return results


# ---------------------------------------------------------------------------
# 2. 10-Q MD&A text scan (Item 2)
# ---------------------------------------------------------------------------

def _extract_10q_mda(text: str) -> str:
    """Extract Item 2 (MD&A) from a 10-Q filing."""
    patterns = [
        r'item\s+2[\.\s]+management.{0,30}discussion',
        r'item\s+2[\.\s]+md&a',
        r'item\s+2[\.\s]+results\s+of\s+operations',
    ]
    start = -1
    text_lower = text.lower()
    for pat in patterns:
        m = re.search(pat, text_lower)
        if m:
            start = m.start()
            break

    if start == -1:
        return text[:5000]  # fallback

    # End at Item 3 or Item 4
    end_m = re.search(r'item\s+[34][\.\s]', text_lower[start + 100:])
    end = (start + 100 + end_m.start()) if end_m else min(start + 12000, len(text))

    return text[start:end]


def scan_10q_language(cik: str, subs: dict, seg_names: list) -> dict:
    """
    Scan recent 10-Q MD&A sections for divestiture/restructuring language.
    Returns {signal, hits, source_urls}.
    """
    filings = _recent_filings(subs, "10-Q", max_count=3)
    all_hits = []
    source_urls = []
    best_signal = "NONE"

    signal_rank = {"NONE": 0, "EXPLORATORY": 1, "PENDING": 2, "COMPLETED": 3}

    for f in filings:
        text, url = _fetch_doc_text(cik, f["acc"], f["doc"])
        if not text:
            continue

        mda = _extract_10q_mda(text)
        text_lower = mda.lower()

        hits_this = []
        for kw in COMPLETED_KW:
            if kw in text_lower:
                hits_this.append(("COMPLETED", kw))
        for kw in PENDING_KW:
            if kw in text_lower:
                hits_this.append(("PENDING", kw))
        for kw in EXPLORATORY_KW:
            if kw in text_lower:
                hits_this.append(("EXPLORATORY", kw))

        if hits_this:
            all_hits.extend(hits_this)
            source_urls.append(url)
            top = max(hits_this, key=lambda h: signal_rank[h[0]])
            if signal_rank[top[0]] > signal_rank[best_signal]:
                best_signal = top[0]

    return {
        "signal": best_signal,
        "hits": [f"{tier}:{kw}" for tier, kw in all_hits],
        "source_urls": source_urls,
    }


# ---------------------------------------------------------------------------
# 3. 8-K Item 2.05 / 2.06 — restructuring charges and impairments
# ---------------------------------------------------------------------------

def _extract_8k_section(text: str, item_num: str) -> str:
    """Extract a specific item section from 8-K text."""
    pattern = re.compile(
        rf'item\s+{re.escape(item_num)}\b[^a-z]{{0,60}}', re.I
    )
    m = pattern.search(text.lower())
    if not m:
        return ""
    start = m.start()

    # Find next item
    next_item = re.search(
        r'item\s+\d+\.\d+\b',
        text.lower()[start + 50:]
    )
    end = (start + 50 + next_item.start()) if next_item else min(start + 4000, len(text))
    return text[start:end]


def scan_8k_restructuring_impairment(cik: str, subs: dict, seg_names: list) -> dict:
    """
    Scan recent 8-K Items 2.05 and 2.06 for restructuring charges and
    goodwill/asset impairments, checking for segment attribution.

    Returns:
      restructuring_hit: bool — recent restructuring charge announcement
      impairment_hit: bool — recent goodwill/asset impairment
      seg_hits: list of (segment_name, event_type, date, url)
    """
    filings = _recent_filings(subs, "8-K", max_count=10)

    restructuring_hit = False
    impairment_hit = False
    seg_hits = []
    source_urls = []

    seg_tokens = [s.lower() for s in seg_names if s and len(s) > 3]

    for f in filings:
        # Quick check: only fetch if 2.05 or 2.06 appears in items field
        # (subs.recent.items is available)
        recent = subs.get("filings", {}).get("recent", {})
        idx = None
        for i, acc in enumerate(recent.get("accessionNumber", [])):
            if acc == f["acc"]:
                idx = i
                break

        if idx is not None:
            items_str = str(recent.get("items", [""] * (idx + 1))[idx] if idx < len(recent.get("items", [])) else "")
            if "2.05" not in items_str and "2.06" not in items_str:
                continue  # Skip 8-Ks without these items

        text, url = _fetch_doc_text(cik, f["acc"], f["doc"])
        if not text:
            continue

        text_lower = text.lower()

        # Check restructuring (Item 2.05)
        section_205 = _extract_8k_section(text, "2.05")
        if section_205:
            sec_lower = section_205.lower()
            if any(kw in sec_lower for kw in RESTRUCTURE_KW):
                restructuring_hit = True
                source_urls.append(url)
                # Check if any segment is mentioned
                for seg in seg_tokens:
                    if seg in sec_lower:
                        seg_hits.append((seg, "RESTRUCTURING", f["date"], url))

        # Check impairment (Item 2.06)
        section_206 = _extract_8k_section(text, "2.06")
        if section_206:
            sec_lower = section_206.lower()
            if any(kw in sec_lower for kw in IMPAIRMENT_KW):
                impairment_hit = True
                if url not in source_urls:
                    source_urls.append(url)
                for seg in seg_tokens:
                    if seg in sec_lower:
                        seg_hits.append((seg, "IMPAIRMENT", f["date"], url))

        # Also scan Item 2.02 (earnings press release) for impairment/restructuring language
        section_202 = _extract_8k_section(text, "2.02")
        if section_202:
            sec_lower = section_202.lower()
            if any(kw in sec_lower for kw in IMPAIRMENT_KW):
                impairment_hit = True
            if any(kw in sec_lower for kw in RESTRUCTURE_KW):
                restructuring_hit = True

    return {
        "restructuring_hit": restructuring_hit,
        "impairment_hit": impairment_hit,
        "seg_hits": seg_hits,
        "source_urls": source_urls[:5],
    }


# ---------------------------------------------------------------------------
# 4. 8-K Item 2.02 earnings language scan
# ---------------------------------------------------------------------------

def scan_8k_earnings_language(cik: str, subs: dict, seg_names: list) -> dict:
    """
    Scan recent 8-K Item 2.02 (earnings press releases) and Item 7.01/8.01
    (Reg FD / other events) for strategic language about segments.
    These items often contain candid management commentary not in the 10-K.
    """
    filings = _recent_filings(subs, "8-K", max_count=8)
    SCAN_ITEMS = {"2.02", "7.01", "8.01"}

    all_hits = []
    source_urls = []
    best_signal = "NONE"
    signal_rank = {"NONE": 0, "EXPLORATORY": 1, "PENDING": 2, "COMPLETED": 3}

    seg_tokens = [s.lower() for s in seg_names if s and len(s) > 3]

    for f in filings:
        recent = subs.get("filings", {}).get("recent", {})
        idx = None
        for i, acc in enumerate(recent.get("accessionNumber", [])):
            if acc == f["acc"]:
                idx = i
                break

        items_str = ""
        if idx is not None and idx < len(recent.get("items", [])):
            items_str = str(recent.get("items", [])[idx])

        if not any(it in items_str for it in SCAN_ITEMS):
            continue

        text, url = _fetch_doc_text(cik, f["acc"], f["doc"])
        if not text:
            continue

        text_lower = text.lower()
        hits_this = []

        for kw in COMPLETED_KW:
            if kw in text_lower:
                hits_this.append(("COMPLETED", kw))
        for kw in PENDING_KW:
            if kw in text_lower:
                hits_this.append(("PENDING", kw))
        for kw in EXPLORATORY_KW:
            if kw in text_lower:
                hits_this.append(("EXPLORATORY", kw))

        if hits_this:
            all_hits.extend(hits_this)
            source_urls.append(url)
            top = max(hits_this, key=lambda h: signal_rank[h[0]])
            if signal_rank[top[0]] > signal_rank[best_signal]:
                best_signal = top[0]

    return {
        "signal": best_signal,
        "hits": [f"{tier}:{kw}" for tier, kw in all_hits],
        "source_urls": source_urls[:5],
    }


# ---------------------------------------------------------------------------
# Master scorer — combines all quarterly signals into a single output
# ---------------------------------------------------------------------------

def score_quarterly_signals(
    company_name: str,
    cik: str,
    subs: dict,
    seg_names: list,
) -> dict:
    """
    Run all quarterly signal checks. Returns a unified dict with:
      - Q_Score_Delta: additional points to add to parent/segment scores
      - Q_Language_Signal: best language signal found across all quarterly docs
      - Q_Restructuring_Hit, Q_Impairment_Hit: bool flags
      - Q_Seg_Trends: per-segment quarterly trend data
      - Q_Language_Hits, Q_Source_URLs: evidence
      - Q_Notes: human-readable summary
    """
    cik_padded = str(cik).zfill(10)

    result = {
        "Q_Score_Delta": 0,
        "Q_Language_Signal": "NONE",
        "Q_Restructuring_Hit": False,
        "Q_Impairment_Hit": False,
        "Q_Seg_Trends": {},
        "Q_Language_Hits": [],
        "Q_Source_URLs": [],
        "Q_Notes": "",
    }

    notes = []
    signal_rank = {"NONE": 0, "EXPLORATORY": 1, "PENDING": 2, "COMPLETED": 3}

    # --- 1. 10-Q segment trends ---
    try:
        seg_trends = analyze_quarterly_segment_trend(cik_padded, subs, seg_names)
        result["Q_Seg_Trends"] = seg_trends
        declining = [s for s, d in seg_trends.items() if d.get("revenue_trend") == "DECLINING"]
        if declining:
            result["Q_Score_Delta"] += 1
            notes.append(f"Q_trend_declining: {', '.join(declining[:2])}")
    except Exception as e:
        log.debug(f"10-Q segment trend failed for {company_name}: {e}")

    # --- 2. 10-Q MD&A language ---
    try:
        q_lang = scan_10q_language(cik_padded, subs, seg_names)
        if signal_rank[q_lang["signal"]] > signal_rank[result["Q_Language_Signal"]]:
            result["Q_Language_Signal"] = q_lang["signal"]
        result["Q_Language_Hits"].extend(q_lang["hits"])
        result["Q_Source_URLs"].extend(q_lang["source_urls"])
        if q_lang["signal"] != "NONE":
            result["Q_Score_Delta"] += 1
            notes.append(f"10-Q_MDA_{q_lang['signal']}: {q_lang['hits'][:2]}")
    except Exception as e:
        log.debug(f"10-Q language scan failed for {company_name}: {e}")

    # --- 3. 8-K Item 2.05/2.06 restructuring and impairment ---
    try:
        ri = scan_8k_restructuring_impairment(cik_padded, subs, seg_names)
        result["Q_Restructuring_Hit"] = ri["restructuring_hit"]
        result["Q_Impairment_Hit"]    = ri["impairment_hit"]
        result["Q_Source_URLs"].extend(ri["source_urls"])

        if ri["restructuring_hit"]:
            result["Q_Score_Delta"] += 1
            seg_note = f"affecting {ri['seg_hits'][0][0]}" if ri["seg_hits"] else ""
            notes.append(f"8-K_2.05_restructuring {seg_note}".strip())

        if ri["impairment_hit"]:
            result["Q_Score_Delta"] += 1
            seg_note = f"affecting {ri['seg_hits'][0][0]}" if ri["seg_hits"] else ""
            notes.append(f"8-K_2.06_impairment {seg_note}".strip())
    except Exception as e:
        log.debug(f"8-K restructuring/impairment scan failed for {company_name}: {e}")

    # --- 4. 8-K earnings language (Items 2.02, 7.01, 8.01) ---
    try:
        e_lang = scan_8k_earnings_language(cik_padded, subs, seg_names)
        if signal_rank[e_lang["signal"]] > signal_rank[result["Q_Language_Signal"]]:
            result["Q_Language_Signal"] = e_lang["signal"]
        result["Q_Language_Hits"].extend(e_lang["hits"])
        result["Q_Source_URLs"].extend(e_lang["source_urls"])
        if e_lang["signal"] != "NONE":
            result["Q_Score_Delta"] += 1
            notes.append(f"8-K_earnings_{e_lang['signal']}: {e_lang['hits'][:2]}")
    except Exception as e:
        log.debug(f"8-K earnings language scan failed for {company_name}: {e}")

    # Deduplicate source URLs
    seen = set()
    deduped = []
    for u in result["Q_Source_URLs"]:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    result["Q_Source_URLs"] = deduped[:8]

    result["Q_Notes"] = " | ".join(notes) if notes else "No quarterly signals"
    return result


# ---------------------------------------------------------------------------
# Batch runner — adds quarterly signals to an A2 DataFrame
# ---------------------------------------------------------------------------

def run_quarterly_batch(a2_df: "pd.DataFrame", verbose: bool = True) -> "pd.DataFrame":
    """
    Takes the A2 language engine DataFrame, adds quarterly signal columns.
    Returns the augmented DataFrame.
    """
    import pandas as pd
    from conditions_engine import _get_submissions

    q_cols = [
        "Q_Score_Delta", "Q_Language_Signal", "Q_Restructuring_Hit",
        "Q_Impairment_Hit", "Q_Language_Hits", "Q_Source_URLs", "Q_Notes",
    ]
    for col in q_cols:
        a2_df[col] = None

    processed = {}  # cache per CIK

    companies = a2_df[["Company", "CIK", "Segment"]].drop_duplicates("Company")

    for _, row in companies.iterrows():
        company = row["Company"]
        cik = str(row["CIK"]) if row["CIK"] else None

        if not cik or cik == "None":
            processed[company] = {c: None for c in q_cols}
            continue

        cik_padded = cik.zfill(10)
        subs = _get_submissions(cik_padded)
        if not subs:
            processed[company] = {c: None for c in q_cols}
            continue

        co_rows = a2_df[a2_df["Company"] == company]
        seg_names = [
            str(s) for s in co_rows["Segment"].tolist()
            if s and str(s) not in ("N/A", "nan") and "Corporate" not in str(s)
        ]

        if verbose:
            log.info(f"  Q-signals: {company} | CIK {cik}")

        try:
            q = score_quarterly_signals(company, cik_padded, subs, seg_names)
            processed[company] = {
                "Q_Score_Delta":      q["Q_Score_Delta"],
                "Q_Language_Signal":  q["Q_Language_Signal"],
                "Q_Restructuring_Hit": q["Q_Restructuring_Hit"],
                "Q_Impairment_Hit":   q["Q_Impairment_Hit"],
                "Q_Language_Hits":    "; ".join(q["Q_Language_Hits"][:5]),
                "Q_Source_URLs":      " | ".join(q["Q_Source_URLs"][:5]),
                "Q_Notes":            q["Q_Notes"],
            }
        except Exception as e:
            log.warning(f"  Q-signals failed: {company} — {e}")
            processed[company] = {c: None for c in q_cols}

    # Merge back
    out = a2_df.copy()
    for col in q_cols:
        out[col] = out["Company"].map(lambda co: processed.get(co, {}).get(col))

    # Update Propensity_Score with Q_Score_Delta
    out["Propensity_Score"] = out.apply(
        lambda r: min(20, (r.get("Propensity_Score") or 0) + (r.get("Q_Score_Delta") or 0)),
        axis=1
    )

    # Upgrade language signal if quarterly signal is stronger
    signal_rank = {"NONE": 0, "EXPLORATORY": 1, "PENDING": 2, "COMPLETED": 3}

    def _best_signal(row):
        existing = str(row.get("Co_Timing_Signal") or "NONE").upper()
        q_sig    = str(row.get("Q_Language_Signal") or "NONE").upper()
        return existing if signal_rank.get(existing, 0) >= signal_rank.get(q_sig, 0) else q_sig

    out["Co_Timing_Signal"] = out.apply(_best_signal, axis=1)

    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    from conditions_engine import _get_submissions

    test_cases = [
        ("Newell Brands", "814453"),
        ("Hillenbrand",   "1417398"),
        ("ITT",           "49826"),
    ]

    for name, cik in test_cases:
        print(f"\n=== {name} ===")
        subs = _get_submissions(cik.zfill(10))
        if not subs:
            print("  SUBMISSIONS_FAIL")
            continue

        # Use blank seg_names for test
        result = score_quarterly_signals(name, cik, subs, [])
        print(f"  Q_Score_Delta:       {result['Q_Score_Delta']}")
        print(f"  Q_Language_Signal:   {result['Q_Language_Signal']}")
        print(f"  Q_Restructuring_Hit: {result['Q_Restructuring_Hit']}")
        print(f"  Q_Impairment_Hit:    {result['Q_Impairment_Hit']}")
        print(f"  Q_Notes:             {result['Q_Notes']}")
        if result['Q_Source_URLs']:
            print(f"  Q_Sources:           {result['Q_Source_URLs'][0]}")
