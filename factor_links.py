"""
Factor Source Links
===================

For every scored factor on a company (leverage, multi-segment, exec departure,
activist, revenue decline, guidance cut, quarterly restructuring/impairment),
resolve a direct SEC EDGAR link to *where that factor was found* so each chip
on the dashboard is click-through auditable.

Resolution per factor:
  Leverage / Revenue decline  -> the 10-K's XBRL financial-statement viewer
  Multi-segment               -> the 10-K, jumped to the segment footnote
  Exec departure              -> the specific 8-K (Item 5.02) filed on the noted date
  Guidance / dividend cut     -> the specific 8-K (2.02/8.01) filed on the noted date
  Activist                    -> the company's SC 13D/13G filing list on EDGAR
  Restructuring / Impairment  -> the 10-Q/8-K source URL already captured
"""

import os
import re
import time
import json
import logging
from urllib.parse import quote

import requests

log = logging.getLogger("factor_links")
HEADERS = {"User-Agent": os.environ.get("SEC_USER_AGENT", "WoodsonEquity research@woodsonequity.com"), "Accept-Encoding": "gzip, deflate"}
EDGAR = "https://www.sec.gov"
BASE = "https://data.sec.gov"
RATE = 0.12


def pad_cik(cik) -> str:
    try:
        return str(int(float(cik))).zfill(10)
    except (TypeError, ValueError):
        return ""


def _acc_from_url(url: str) -> str:
    m = re.search(r"/(\d{10})(\d{2})(\d{6})/", str(url))
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m2 = re.search(r"(\d{10}-\d{2}-\d{6})", str(url))
    return m2.group(1) if m2 else ""


def _frag(doc_url: str, phrase: str) -> str:
    # hyphen is a delimiter in the text-fragment grammar → encode as %2D
    return doc_url + "#:~:text=" + quote(phrase).replace("-", "%2D")


def _get_text(url: str, timeout: int = 25) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        time.sleep(RATE)
        return r.text
    except Exception:
        time.sleep(RATE)
        return ""


def get_submissions(cik: str) -> dict:
    cikp = pad_cik(cik)
    if not cikp:
        return {}
    for _ in range(3):
        try:
            r = requests.get(f"{BASE}/submissions/CIK{cikp}.json", headers=HEADERS, timeout=20)
            r.raise_for_status()
            time.sleep(RATE)
            return r.json()
        except Exception:
            time.sleep(0.8)
    return {}


def resolve_8k(subs: dict, cik: str, date_str: str, items_wanted=None):
    """Return the primary-document URL of the 8-K filed on date_str (exact, else
    nearest within 3 days). items_wanted optionally filters by 8-K item numbers."""
    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accs = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    items = recent.get("items", [""] * len(forms))
    cand = []
    for i, form in enumerate(forms):
        if not str(form).startswith("8-K"):
            continue
        d = dates[i]
        if items_wanted and not any(it in str(items[i]) for it in items_wanted):
            continue
        cand.append((d, accs[i], docs[i]))
    # exact date first, then nearest
    for d, acc, doc in cand:
        if d == date_str and doc:
            acc_nd = acc.replace("-", "")
            return f"{EDGAR}/Archives/edgar/data/{int(cik)}/{acc_nd}/{doc}"
    # nearest within 3 days
    if date_str:
        from datetime import datetime
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            target = None
        if target:
            best, bestdelta = None, 4
            for d, acc, doc in cand:
                try:
                    delta = abs((datetime.strptime(d, "%Y-%m-%d") - target).days)
                except ValueError:
                    continue
                if delta < bestdelta and doc:
                    best, bestdelta = (acc, doc), delta
            if best:
                acc_nd = best[0].replace("-", "")
                return f"{EDGAR}/Archives/edgar/data/{int(cik)}/{acc_nd}/{best[1]}"
    return ""


def _browse(cik: str, ftype: str) -> str:
    return (f"{EDGAR}/cgi-bin/browse-edgar?action=getcompany&CIK={pad_cik(cik)}"
            f"&type={quote(ftype)}&dateb=&owner=include&count=40")


# --- direct-document resolvers ---------------------------------------------
# EDGAR's cgi-bin/viewer is a JavaScript shell and browse-edgar is a search page;
# neither is a document. These resolve to real rendered filings under /Archives/.

_RFILE_CACHE = {}

def statement_rfiles(cik: str, acc: str) -> dict:
    """Map {'income','balance','cash','segment'} -> rendered R-file URLs for a filing.

    EDGAR renders every XBRL statement as an R#.htm page (real HTML with the
    actual numbers). FilingSummary.xml is the index of them.
    """
    key = (str(cik), acc)
    if key in _RFILE_CACHE:
        return _RFILE_CACHE[key]
    out = {}
    if not acc:
        _RFILE_CACHE[key] = out
        return out
    base = f"{EDGAR}/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}"
    xml = _get_text(f"{base}/FilingSummary.xml", timeout=25)
    if xml:
        for rep in re.findall(r"<Report[^>]*>(.*?)</Report>", xml, re.S):
            fn = re.search(r"<HtmlFileName>([^<]+)</HtmlFileName>", rep)
            sn = re.search(r"<ShortName>([^<]+)</ShortName>", rep)
            if not fn or not sn:
                continue
            name = sn.group(1).upper()
            url = f"{base}/{fn.group(1)}"
            if "income" not in out and re.search(r"STATEMENTS? OF (OPERATIONS|INCOME)", name):
                out["income"] = url
            elif "balance" not in out and "BALANCE SHEET" in name and "PARENTHETICAL" not in name:
                out["balance"] = url
            elif "cash" not in out and "CASH FLOW" in name:
                out["cash"] = url
            elif "segment" not in out and "SEGMENT" in name and "TABLES" not in name:
                out["segment"] = url
    _RFILE_CACHE[key] = out
    return out


def resolve_13d_doc(cik: str) -> str:
    """Direct URL of the most recent SC 13D/13G *document* targeting this company."""
    for ftype in ("SC 13D", "SC 13G"):
        url = (f"{EDGAR}/cgi-bin/browse-edgar?action=getcompany&CIK={pad_cik(cik)}"
               f"&type={quote(ftype)}&dateb=&owner=include&count=10&output=atom")
        xml = _get_text(url, timeout=20)
        if not xml:
            continue
        m = re.search(r"<accession-number>([\d\-]+)</accession-number>", xml)
        if not m:
            continue
        acc_nd = m.group(1).replace("-", "")
        # 13D documents are stored under the SUBJECT company's CIK path
        d = _get_text(f"{EDGAR}/Archives/edgar/data/{int(cik)}/{acc_nd}/", timeout=20)
        for link in re.findall(r'href="(/Archives/edgar/data/[^"]+\.htm[l]?)"', d or "", re.I):
            fname = link.split("/")[-1].lower()
            if "index" not in fname:
                return f"{EDGAR}{link}"
    return ""


def _first_url(s: str) -> str:
    if not s or str(s).strip() in ("—", "nan", ""):
        return ""
    for sep in ("|", ","):
        if sep in str(s):
            return str(s).split(sep)[0].strip()
    return str(s).strip()


def build_factor_links(row, subs: dict) -> dict:
    """Return {factor_key: url} for every active factor we can source-link."""
    links = {}
    cik = pad_cik(row.get("CIK"))
    if not cik:
        return links
    doc = str(row.get("Filing_Doc_URL", ""))
    idx = str(row.get("Filing_Index_URL", ""))
    notes = str(row.get("Parent_Notes", ""))
    acc = _acc_from_url(idx or doc)
    rf = statement_rfiles(cik, acc)          # real rendered statements, not the JS viewer
    eight_k_list = _browse(cik, "8-K")

    def g(k):
        try:
            return float(row.get(k, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    # Leverage → the rendered BALANCE SHEET (where debt actually is)
    if g("P_Leverage") > 0:
        links["P_Leverage"] = rf.get("balance") or rf.get("income") or doc
    # Revenue decline → the rendered INCOME STATEMENT
    if g("P_RevDecline") > 0:
        links["P_RevDecline"] = rf.get("income") or rf.get("balance") or doc

    # Multi-segment → the rendered segment footnote, else the 10-K at that heading
    if g("P_SegCount") > 0:
        links["P_SegCount"] = rf.get("segment") or (
            _frag(doc, "reportable segments") if doc.startswith("http") else "")

    # Exec departure → the specific Item 5.02 8-K
    if g("P_CsuiteChange") > 0:
        m = re.search(r"Item 5\.02[^()]*\((\d{4}-\d{2}-\d{2})\)", notes)
        u = ""
        if m:
            u = (resolve_8k(subs, cik, m.group(1), items_wanted=["5.02"])
                 or resolve_8k(subs, cik, m.group(1)))
        links["P_CsuiteChange"] = u or eight_k_list

    # Guidance / dividend cut → the specific 8-K on the noted date (2.02/8.01)
    if g("P_GuidanceCut") > 0:
        # NB: [^0-9]* fails here — the "8" in "8-K" stops it before the date.
        m = re.search(r"guidance/div cut:.*?(\d{4}-\d{2}-\d{2})", notes)
        u = ""
        if m:
            u = (resolve_8k(subs, cik, m.group(1), items_wanted=["2.02", "8.01", "7.01"])
                 or resolve_8k(subs, cik, m.group(1)))
        links["P_GuidanceCut"] = u or eight_k_list

    # Activist → the actual SC 13D/13G document filed against the company
    if g("P_Activist") >= 1:
        links["P_Activist"] = resolve_13d_doc(cik) or _browse(
            cik, "SC 13D" if g("P_Activist") >= 2 else "SC 13G")

    # Quarterly signals → captured 10-Q/8-K source URL
    q_url = _first_url(row.get("Q_Source_URLs"))
    if bool(row.get("Q_Restructuring_Hit")) and q_url:
        links["Q_Restructuring"] = q_url
    if bool(row.get("Q_Impairment_Hit")) and q_url:
        links["Q_Impairment"] = q_url

    return links


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    subs = get_submissions("814453")  # Newell
    print("has subs:", bool(subs))
    print(resolve_8k(subs, "814453", "2026-05-13"))
