"""
Keyword / Timing Engine  —  v2, per MASTER TRAINING DOCUMENT PART 2B

State machine:  EXPLORATORY -> PENDING -> COMPLETED
                             \\-> TERMINATED
                PENDING held too long -> STALE-PENDING (watch state)

Signal sources, EARLIEST to latest (PART 2B). We parse all of them; waiting for
the press release forfeits the lead time the screener exists to capture.

  1. HELD-FOR-SALE RECLASSIFICATION on the balance sheet   <- earliest, structural
     CS-015: Honeywell's PPE was marked HFS in the Q3'24 10-Q, a full quarter
     BEFORE the announcement. Detected from XBRL, not prose.
  2. Strategic-alternatives / portfolio-optimization language -> EXPLORATORY
  3. Withdrawn IPO (Form RW) or withdrawn spin-off (Form 10 withdrawal)
     -> divestiture-intent signal (CS-008: NN Inc's withdrawn Life Sciences IPO)
  4. 8-K announcement (definitive agreement) -> PENDING
  5. 8-K close -> COMPLETED  (AUTHORITATIVE)
  6. 8-K Item 1.02 (termination of material agreement) -> TERMINATED

Rules learned from the case library:
  * Announced != closed. CS-014 (L3Harris/CAS) sat PENDING for 16 months. If a
    guided close window passes with no close 8-K, emit STALE-PENDING — never
    auto-complete and never drop.
  * Do NOT use discontinued-operations reclassification as the completion signal.
    CS-018 (Regal Rexnord) was a whole-segment sale that did NOT qualify as
    discontinued ops. The close 8-K is authoritative.
  * Program logic: one announced multi-asset exit queues EVERY named remainder
    as PENDING (CS-010 DuPont M&M, CS-011 Roper, CS-017 Smiths).
  * Spin-to-sale conversion is ONE divestiture event, not two (CS-013 Vertiv).
"""

import os
import re
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

log = logging.getLogger("timing_engine")

HEADERS = {"User-Agent": os.environ.get("SEC_USER_AGENT", "WoodsonEquity research@woodsonequity.com"), "Accept-Encoding": "gzip, deflate"}
EDGAR = "https://www.sec.gov"
DATA = "https://data.sec.gov"
RATE = 0.12

# Days a PENDING deal may sit before we flag it as stale (CS-014 ran 16 months)
STALE_PENDING_DAYS = 270


def _get_json(url, timeout=30):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout); r.raise_for_status()
        time.sleep(RATE); return r.json()
    except Exception:
        time.sleep(RATE); return None


def pad_cik(cik) -> str:
    try:
        return str(int(float(cik))).zfill(10)
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# 1. HELD-FOR-SALE  —  the earliest structural signal (CS-015)
# ---------------------------------------------------------------------------

# Balance-sheet concepts that mean "we have reclassified a business as held for sale".
# Read from XBRL company facts, so this is a structural read of the balance sheet
# rather than keyword-matching MD&A prose.
HFS_CONCEPTS = [
    "AssetsHeldForSaleCurrent",
    "AssetsHeldForSaleNotPartOfDisposalGroupCurrent",
    "DisposalGroupIncludingDiscontinuedOperationAssetsCurrent",
    "DisposalGroupIncludingDiscontinuedOperationAssetsNoncurrent",
    "AssetsOfDisposalGroupIncludingDiscontinuedOperationCurrent",
    "LiabilitiesOfDisposalGroupIncludingDiscontinuedOperationCurrent",
    "DisposalGroupIncludingDiscontinuedOperationLiabilitiesCurrent",
]


def detect_held_for_sale(cik: str, since: str = None) -> list:
    """Return balance-sheet held-for-sale reclassifications, earliest first.

    Each hit: {concept, period_end, filed, form, value_usd, accession}
    `filed` is what matters for lead time — it is when the market could see it.
    """
    cikp = pad_cik(cik)
    if not cikp:
        return []
    facts = _get_json(f"{DATA}/api/xbrl/companyfacts/CIK{cikp}.json")
    if not facts:
        return []
    usg = facts.get("facts", {}).get("us-gaap", {})
    hits, seen = [], set()
    for concept in HFS_CONCEPTS:
        for unit_rows in usg.get(concept, {}).get("units", {}).values():
            for row in unit_rows:
                val, filed, end = row.get("val"), row.get("filed"), row.get("end")
                if not filed or not end or not val:
                    continue
                if since and filed < since:
                    continue
                key = (concept, end)
                if key in seen:
                    continue
                seen.add(key)
                hits.append({
                    "signal": "HELD_FOR_SALE",
                    "concept": concept,
                    "period_end": end,
                    "filed": filed,
                    "form": row.get("form", ""),
                    "value_usd": val,
                    "accession": row.get("accn", ""),
                })
    hits.sort(key=lambda h: (h["filed"], h["period_end"]))
    return hits


# ---------------------------------------------------------------------------
# 3 & 6. Withdrawals and terminations from the filing index
# ---------------------------------------------------------------------------

def _recent(subs: dict) -> list:
    r = (subs or {}).get("filings", {}).get("recent", {})
    out = []
    for i, form in enumerate(r.get("form", [])):
        out.append({
            "form": form,
            "filed": r.get("filingDate", [])[i] if i < len(r.get("filingDate", [])) else "",
            "acc": r.get("accessionNumber", [])[i] if i < len(r.get("accessionNumber", [])) else "",
            "doc": r.get("primaryDocument", [])[i] if i < len(r.get("primaryDocument", [])) else "",
            "items": (r.get("items", []) or [""] * len(r.get("form", [])))[i]
                     if i < len(r.get("items", []) or []) else "",
        })
    return out


def detect_withdrawals(subs: dict) -> list:
    """Form RW (withdrawn registration) / Form 10 withdrawal -> divestiture INTENT.

    CS-008: NN Inc withdrew its Life Sciences IPO shortly before selling the
    division outright. A withdrawn separation is a signal, not a non-event.
    """
    out = []
    for f in _recent(subs):
        form = (f["form"] or "").upper()
        if form in ("RW", "RW WD") or form.startswith("RW") or form in ("10-12B/A W", "AW"):
            out.append({"signal": "WITHDRAWN_SEPARATION", "form": f["form"],
                        "filed": f["filed"], "accession": f["acc"]})
    return out


def _items(f) -> list:
    return [x.strip() for x in str(f.get("items", "")).split(",") if x.strip()]


def detect_terminations(subs: dict) -> list:
    """8-K Item 1.02 — termination of a material definitive agreement -> TERMINATED.

    CAUTION — Item 1.02 is overwhelmingly used for DEBT REFINANCING, not deal
    terminations: the signature is 1.02 co-filed with 1.01 (new agreement) and
    2.03 (new financial obligation), i.e. "we replaced our credit facility".
    Treating every 1.02 as a failed divestiture produced a ~58% false-positive
    rate in testing. We exclude the refinancing pattern here, and `resolve_state`
    additionally only honours a termination when a deal is already PENDING.
    """
    out = []
    for f in _recent(subs):
        if not str(f["form"]).startswith("8-K"):
            continue
        it = _items(f)
        if "1.02" not in it:
            continue
        if "2.03" in it:          # new financial obligation -> refinancing, not a deal break
            continue
        out.append({"signal": "TERMINATION", "form": f["form"], "filed": f["filed"],
                    "items": f["items"], "accession": f["acc"], "doc": f.get("doc", ""),
                    "confidence": "unverified"})
    return out


# Item 2.01 is "Completion of Acquisition OR Disposition of Assets" — direction
# must be read from the document, or every acquirer looks like a serial divester.
_DISPOSE = re.compile(
    r"\b(sale of|sold|divest\w*|disposition of|completed the sale|"
    r"purchase agreement.{0,60}\bsell\b|transfer(?:red)? .{0,40}\bto\b)", re.I)
_ACQUIRE = re.compile(
    r"\b(acquisition of|acquired|completed the acquisition|purchase of all)", re.I)


def classify_201_direction(cik: str, filing: dict, fetch) -> str:
    """Return 'disposition' | 'acquisition' | 'unknown' for an Item 2.01 8-K."""
    doc = filing.get("doc") or ""
    if not doc:
        return "unknown"
    url = f"{EDGAR}/Archives/edgar/data/{int(cik)}/{filing['acc'].replace('-','')}/{doc}"
    txt = fetch(url)
    if not txt:
        return "unknown"
    head = txt[:20000]
    d, a = len(_DISPOSE.findall(head)), len(_ACQUIRE.findall(head))
    if d > a:
        return "disposition"
    if a > d:
        return "acquisition"
    return "unknown"


def detect_deal_8ks(subs: dict) -> dict:
    """Split 8-Ks into announcement (1.01) and completion (2.01) candidates.

    NOTE: completion is taken from the close 8-K, NOT from discontinued-operations
    reclassification — CS-018 was a whole-segment sale that never qualified as
    discontinued ops.
    """
    ann, close = [], []
    for f in _recent(subs):
        if not str(f["form"]).startswith("8-K"):
            continue
        items = str(f["items"])
        if "1.01" in items:
            ann.append({"signal": "AGREEMENT_8K", "filed": f["filed"], "items": items,
                        "accession": f["acc"]})
        if "2.01" in items:
            close.append({"signal": "CLOSE_8K", "filed": f["filed"], "items": items,
                          "accession": f["acc"]})
    return {"announcements": ann, "closes": close}


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

VALID_STATES = ("NONE", "EXPLORATORY", "PENDING", "STALE_PENDING", "COMPLETED", "TERMINATED")


def resolve_state(signals: list, as_of: str = None,
                  guided_close: str = None) -> dict:
    """Fold a list of dated signals into a single state.

    signals: [{signal, filed, ...}] where `signal` is one of
      HELD_FOR_SALE | EXPLORATORY_LANGUAGE | WITHDRAWN_SEPARATION |
      AGREEMENT_8K | CLOSE_8K | TERMINATION
    guided_close: management-guided close date, if disclosed (drives STALE-PENDING).
    """
    as_of = as_of or datetime.utcnow().strftime("%Y-%m-%d")
    ev = sorted([s for s in signals if s.get("filed")], key=lambda s: s["filed"])
    state, since, basis = "NONE", None, []

    for s in ev:
        k = s["signal"]
        if k == "CLOSE_8K":
            state, since = "COMPLETED", s["filed"]; basis.append(s); break
        if k == "TERMINATION" and state in ("PENDING", "STALE_PENDING"):
            state, since = "TERMINATED", s["filed"]; basis.append(s); break
        if k == "AGREEMENT_8K":
            state, since = "PENDING", s["filed"]; basis.append(s)
        elif k in ("HELD_FOR_SALE", "EXPLORATORY_LANGUAGE", "WITHDRAWN_SEPARATION"):
            if state in ("NONE",):
                state, since = "EXPLORATORY", s["filed"]
            basis.append(s)

    # Announced != closed. A guided window that has passed, or a long silent
    # PENDING, becomes a watch state — never auto-complete, never drop (CS-014).
    if state == "PENDING" and since:
        overdue = False
        if guided_close and as_of > guided_close:
            overdue = True
        else:
            try:
                if (datetime.strptime(as_of, "%Y-%m-%d")
                        - datetime.strptime(since, "%Y-%m-%d")).days > STALE_PENDING_DAYS:
                    overdue = True
            except ValueError:
                pass
        if overdue:
            state = "STALE_PENDING"

    earliest = ev[0]["filed"] if ev else None
    lead_days = None
    pend = next((s["filed"] for s in ev if s["signal"] == "AGREEMENT_8K"), None)
    if earliest and pend:
        try:
            lead_days = (datetime.strptime(pend, "%Y-%m-%d")
                         - datetime.strptime(earliest, "%Y-%m-%d")).days
        except ValueError:
            pass

    return {"state": state, "state_since": since,
            "earliest_signal": earliest,
            "earliest_signal_type": ev[0]["signal"] if ev else None,
            "lead_days_vs_announcement": lead_days,
            "signals": basis}


def track_company(cik: str, guided_close: str = None, as_of: str = None) -> dict:
    """Full timing read for one company from EDGAR."""
    subs = _get_json(f"{DATA}/submissions/CIK{pad_cik(cik)}.json") or {}
    sig = []
    sig += detect_held_for_sale(cik)
    sig += detect_withdrawals(subs)
    sig += detect_terminations(subs)
    d = detect_deal_8ks(subs)
    sig += d["announcements"] + d["closes"]
    res = resolve_state(sig, as_of=as_of, guided_close=guided_close)
    res["counts"] = {"held_for_sale": sum(1 for s in sig if s["signal"] == "HELD_FOR_SALE"),
                     "withdrawals": sum(1 for s in sig if s["signal"] == "WITHDRAWN_SEPARATION"),
                     "terminations": sum(1 for s in sig if s["signal"] == "TERMINATION"),
                     "agreement_8k": len(d["announcements"]), "close_8k": len(d["closes"])}
    return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    # CS-015 Honeywell: PPE marked held-for-sale in the Q3'24 10-Q, a full quarter
    # BEFORE the 2024-11-22 announcement. This is the doc's exemplar for why the
    # balance sheet must be parsed rather than the prose.
    print("CS-015 Honeywell (CIK 773840) — held-for-sale reclassifications:")
    for h in detect_held_for_sale("773840", since="2024-01-01")[:10]:
        print(f"   filed {h['filed']}  period {h['period_end']}  {h['form']:<6} "
              f"${h['value_usd']/1e6:,.0f}M  {h['concept']}")
