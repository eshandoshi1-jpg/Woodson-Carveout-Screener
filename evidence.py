"""
Evidence Deep-Linking
=====================

For each scored signal, locate the exact triggering passage inside the source
SEC filing and build a browser text-fragment deep link (`#:~:text=...`) so a
click jumps to — and highlights — the precise sentence that drove the score.

Also computes match-quality flags so phantom fuzzy matches (e.g. "MRC Global"
resolving to "DMC Global") are caught and never presented as real targets.
"""

import os
import re
import time
import logging
from urllib.parse import quote
from typing import Optional

import requests

log = logging.getLogger("evidence")

HEADERS = {"User-Agent": os.environ.get("SEC_USER_AGENT", "WoodsonEquity research@woodsonequity.com"), "Accept-Encoding": "gzip, deflate"}
RATE_LIMIT = 0.12

_DOC_CACHE: dict = {}


# ---------------------------------------------------------------------------
# Filing text retrieval + passage location
# ---------------------------------------------------------------------------

def _fetch_visible_text(url: str) -> str:
    """Fetch an EDGAR document and return whitespace-normalized visible text."""
    if url in _DOC_CACHE:
        return _DOC_CACHE[url]
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        time.sleep(RATE_LIMIT)
        html = r.text
    except Exception as e:
        log.debug(f"fetch failed {url[:80]}: {e}")
        _DOC_CACHE[url] = ""
        return ""

    text = re.sub(r"(?is)<script.*?</script>", " ", html)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&#\d+;|&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    _DOC_CACHE[url] = text
    return text


def _extract_sentence(text: str, start: int, end: int) -> str:
    """Extract the sentence containing text[start:end]."""
    lo = max(0, start - 400)
    hi = min(len(text), end + 400)
    window = text[lo:hi]
    rel = start - lo

    # sentence start: last ". Capital" or paragraph boundary before the match
    left = window[:rel]
    m = list(re.finditer(r"(?<=[.!?;:])\s+(?=[A-Z(])", left))
    s_start = m[-1].end() if m else 0

    # sentence end: first ". Capital" after the match
    right = window[rel:]
    m2 = re.search(r"[.!?](?:\s+(?=[A-Z(])|$)", right)
    s_end = rel + (m2.end() if m2 else len(right))

    sentence = window[s_start:s_end].strip()
    return re.sub(r"\s+", " ", sentence)


def _digit_density(s: str) -> float:
    if not s:
        return 1.0
    return sum(c.isdigit() or c in "$%()" for c in s) / len(s)


def _best_occurrence(text: str, keyword: str) -> Optional[int]:
    """
    Find the most relevant occurrence of a keyword. Prefer an occurrence that
    sits in substantive narrative prose (MD&A / strategic discussion) rather
    than a table, a footnote, or the forward-looking boilerplate list.
    """
    kw = keyword.lower()
    positions = [m.start() for m in re.finditer(re.escape(kw), text.lower())]
    if not positions:
        return None

    # Hypothetical / risk-factor boilerplate — these mention divestitures
    # defensively, not as a real signal. Strongly penalized.
    boiler = ["risk factor", "forward-looking", "including, but not limited",
              "could differ materially", "no assurance", "from time to time",
              "may not", "actual results", "could change", "unexpected adverse",
              "among other things", "could adversely", "we may", "we could",
              "if we", "any future", "our ability to", "there can be no",
              "acquisitions or divestitures", "acquisitions and divestitures",
              "acquisitions, divestitures"]
    # Committed / announced language — the real thing.
    good = ["we announced", "the company announced", "we completed", "completed the",
            "we entered into", "entered into an agreement", "agreement to sell",
            "we are exploring", "has initiated", "we have initiated", "board approved",
            "plan to sell", "plans to divest", "decision to", "we intend to",
            "review of strategic alternatives", "strategic alternatives for",
            "classified as held for sale", "held for sale", "we sold", "we divested"]

    best_pos, best_score = None, -1e9
    for p in positions:
        ctx = text[max(0, p - 260): p + 260]
        ctx_low = ctx.lower()
        score = 0.0
        for b in boiler:
            if b in ctx_low:
                score -= 6
        score -= _digit_density(ctx) * 12          # tables/footnotes are digit-heavy
        if re.search(r"\([a-z]\)", text[max(0, p - 60): p]):
            score -= 4                              # footnote marker just before
        for g in good:
            if g in ctx_low:
                score += 4
        if score > best_score:
            best_score, best_pos = score, p

    # If every occurrence is boilerplate, this keyword yields no credible passage.
    if best_score < -3:
        return None
    return best_pos


def _clean_word(w: str) -> str:
    return w.strip(".,;:()[]\"'")


def build_text_fragment(sentence: str, keyword: str) -> str:
    """
    Build a single-phrase URL text-fragment highlighting the triggering passage.

    A single contiguous phrase (`text=<phrase>`) is far more robust than a
    prefix/suffix range because it only needs one uncut run of visible text to
    match. We anchor the phrase at the keyword and extend through the next few
    clean words, skipping numbers/currency that signal a table cell.
    """
    words = sentence.split()
    low_words = [_clean_word(w).lower() for w in words]
    kw_low = keyword.lower().split()

    idx = -1
    for i in range(len(low_words) - len(kw_low) + 1):
        if low_words[i:i + len(kw_low)] == kw_low:
            idx = i
            break
    if idx == -1:
        idx = 0  # fall back to sentence start

    # Build a SHORT clean run (≤6 words) starting at the keyword. Short anchors
    # are far more robust: SEC inline-XBRL wraps terms in <ix:> tags, and long
    # phrases routinely cross those element boundaries, breaking the match.
    MAX_WORDS = 6
    run = []
    for w in words[idx: idx + MAX_WORDS + 4]:
        if re.search(r"\d|\$|%|&", w):   # stop at numeric/table/entity tokens
            if len(run) >= len(kw_low) + 1:
                break
            continue
        run.append(w)
        if len(run) >= MAX_WORDS:
            break

    phrase = " ".join(run).strip().strip(".,;:")
    if len(phrase.split()) < 3:          # too short to be unique — widen slightly
        phrase = " ".join(words[idx: idx + 5]).strip().strip(".,;:")

    # A literal hyphen is a delimiter in the text-fragment grammar (prefix-,text),
    # so a hyphenated word like "long-term" silently breaks the match. quote()
    # leaves '-' untouched (it's RFC-unreserved), so encode it explicitly.
    encoded = quote(phrase).replace("-", "%2D")
    return "#:~:text=" + encoded


def evidence_for_keyword(doc_url: str, keyword: str) -> Optional[dict]:
    """
    Return {'url', 'quote', 'keyword'} deep-linking to the exact passage in
    doc_url where `keyword` appears, or None if not found.
    """
    if not doc_url or not doc_url.startswith("http"):
        return None
    text = _fetch_visible_text(doc_url)
    if not text:
        return None
    pos = _best_occurrence(text, keyword)
    if pos is None:
        return None
    sentence = _extract_sentence(text, pos, pos + len(keyword))
    # Sentence-level quality gate: drop table rows and accounting boilerplate.
    low = sentence.lower()
    if _digit_density(sentence) > 0.10:                       # a table row, not prose
        return None
    if any(b in low for b in ["transaction costs primarily", "primarily include",
                              "(in millions)", "in thousands", "carrying amount",
                              "the following table", "set forth below"]):
        return None
    if len(sentence.split()) < 6:                             # too fragmentary
        return None
    frag = build_text_fragment(sentence, keyword)
    return {
        "url": doc_url + frag,
        "quote": sentence[:320] + ("…" if len(sentence) > 320 else ""),
        "keyword": keyword,
    }


# ---------------------------------------------------------------------------
# Activist 13D evidence — deep-link to the "Purpose of Transaction" passage
# ---------------------------------------------------------------------------

_CARVEOUT_CUES = [
    "strategic alternatives", "divest", "spin-off", "spin off", "separation",
    "sale of", "non-core", "break up", "sum of the parts", "portfolio",
    "unlock value", "explore", "review of", "carve out", "carve-out",
]


def activist_evidence(company: str, cik: str) -> Optional[dict]:
    """
    Locate the most relevant SC 13D targeting `company` and return an
    exact-passage deep link into its Item 4 (Purpose of Transaction), or None.
    Reuses activist_monitor to find the filing, then builds the fragment here.
    """
    try:
        from activist_monitor import find_activist_filings, EDGAR_BASE
    except Exception:
        return None

    filings = find_activist_filings(company, str(cik), lookback_days=1095, max_results=4)
    if not filings:
        return None

    subject_cik = str(int(float(cik))) if str(cik).replace(".", "").isdigit() else str(cik).lstrip("0")

    for f in filings:
        acc_nd = f["accession"].replace("-", "")
        dir_url = f"{EDGAR_BASE}/Archives/edgar/data/{subject_cik}/{acc_nd}/"
        # find the primary document in the filing directory
        try:
            r = requests.get(dir_url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            time.sleep(RATE_LIMIT)
        except Exception:
            continue
        htm = re.findall(r'href="(/Archives/edgar/data/[^"]+\.htm[l]?)"', r.text, re.I)
        doc_link = next((l for l in htm if "index" not in l.split("/")[-1].lower()), None)
        if not doc_link:
            continue
        doc_url = EDGAR_BASE + doc_link
        text = _fetch_visible_text(doc_url)
        if not text:
            continue

        # Isolate the Item 4 "Purpose of Transaction" section — strategic intent
        # lives here, not in the ownership tables elsewhere in the filing.
        low = text.lower()
        p4 = low.find("purpose of transaction")
        if p4 == -1:
            p4 = low.find("item 4")
        if p4 == -1:
            continue
        p5 = low.find("item 5", p4 + 20)
        section = text[p4: p5 if p5 != -1 else min(len(text), p4 + 6000)]

        # Find the best carveout-cue occurrence in clean prose within Item 4.
        best = None
        for cue in _CARVEOUT_CUES:
            rel = _best_occurrence(section, cue)
            if rel is None:
                continue
            ctx = section[max(0, rel - 120): rel + 120]
            if _digit_density(ctx) > 0.10:      # skip table-like context
                continue
            sentence = _extract_sentence(section, rel, rel + len(cue))
            if len(sentence) < 40 or _digit_density(sentence) > 0.12:
                continue
            best = (cue, sentence)
            break
        if not best:
            continue

        cue, sentence = best
        frag = build_text_fragment(sentence, cue)
        return {
            "kind": "activist 13D",
            "keyword": cue,
            "form": "SC 13D", "date": f.get("file_date", ""),
            "quote": sentence[:320] + ("…" if len(sentence) > 320 else ""),
            "url": doc_url + frag,
        }
    return None


# ---------------------------------------------------------------------------
# Parsing stored signal columns into (keyword, form, date) + url
# ---------------------------------------------------------------------------

def parse_language_hits(hits_str: str) -> list:
    """
    Parse "strategic alternatives (10-K 2026-02) | restructuring (10-K 2026-02)"
    into [{'keyword','form','date'}...].
    """
    out = []
    if not hits_str or str(hits_str).strip() in ("—", "nan", ""):
        return out
    for part in str(hits_str).split("|"):
        part = part.strip()
        m = re.match(r"(.+?)\s*\(([^)]+)\)\s*$", part)
        if m:
            kw = m.group(1).strip()
            meta = m.group(2).strip()
            fm = re.match(r"(\S+)\s+(\S+)", meta)
            form = fm.group(1) if fm else meta
            date = fm.group(2) if fm else ""
            out.append({"keyword": kw, "form": form, "date": date})
        elif part:
            out.append({"keyword": part, "form": "", "date": ""})
    return out


def first_url(urls_str: str) -> str:
    if not urls_str or str(urls_str).strip() in ("—", "nan", ""):
        return ""
    for sep in ("|", ","):
        if sep in str(urls_str):
            return str(urls_str).split(sep)[0].strip()
    return str(urls_str).strip()


# ---------------------------------------------------------------------------
# Match-quality gate — prevents MRC→DMC style phantom matches
# ---------------------------------------------------------------------------

import unicodedata

_SUFFIXES = re.compile(
    r"\b(INC|CORP|CO|CORPORATION|LTD|LLC|PLC|GROUP|HOLDINGS?|INTERNATIONAL|"
    r"ENTERPRISES?|COMPANY|COMPANIES|THE|AG|SA|NV|PLC|LP|SE|OYJ|SPA|AB)\b\.?", re.I)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _canon(name: str) -> str:
    """Uppercase, accent- and apostrophe-normalized (Estée→ESTEE, McDonald's→MCDONALDS)."""
    s = _strip_accents(str(name)).upper()
    s = s.replace("'", "").replace("’", "").replace("&", " AND ")
    s = re.sub(r"/[A-Z]{2,4}/", " ", s)   # drop SEC incorporation markers: /DE/ /NEW/
    return s


def _significant_tokens(name: str) -> list:
    name = _SUFFIXES.sub(" ", _canon(name))
    name = re.sub(r"[^A-Z0-9\s]", " ", name)
    return [t for t in name.split() if len(t) > 1]


def _squished(name: str) -> str:
    """All significant tokens joined — 'Sirius XM' and 'SiriusXM' both → 'SIRIUSXM'."""
    return "".join(_significant_tokens(name))


def classify_match(company: str, matched_name: str, match_score, ticker: str) -> str:
    """
    Return 'VERIFIED', 'REVIEW', or 'UNRESOLVED'.

    Guards against phantom fuzzy matches (MRC→DMC, Punjab National Bank→National
    Bank Holdings) while tolerating cosmetic differences in real matches
    (accents, apostrophes, spacing: Estée Lauder, McDonald's, SiriusXM).
    """
    try:
        score = float(match_score)
    except (TypeError, ValueError):
        score = 0.0

    if not matched_name or str(matched_name).strip() in ("", "nan"):
        return "UNRESOLVED"

    # Squished-name equality settles the common cosmetic mismatches outright.
    sq_in, sq_out = _squished(company), _squished(matched_name)
    if sq_in and sq_out and sq_in == sq_out:
        return "VERIFIED"
    # Also handle a parenthetical alias equalling the match (WTW (Willis Towers Watson)).
    m_alias = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", str(company).strip())
    if m_alias:
        for seg in (m_alias.group(1), m_alias.group(2)):
            if _squished(seg) and _squished(seg) == sq_out:
                return "VERIFIED"

    in_tokens = _significant_tokens(company)
    out_tokens = set(_significant_tokens(matched_name))

    if not in_tokens:
        return "REVIEW" if score >= 97 else "UNRESOLVED"

    # Names often carry a parenthetical alias — "WTW (Willis Towers Watson)" or
    # "Bank of New York (BNY)". The parenthetical (or the part before it) is the
    # canonical name; the other part is usually a ticker/acronym. Build a first-
    # token candidate from each segment so an acronym prefix can't wrongly fail
    # the guard while a genuinely distinctive token mismatch (Punjab) still does.
    lead_candidates = []
    m_paren = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", str(company).strip())
    if m_paren:
        for seg in (m_paren.group(1), m_paren.group(2)):
            toks = _significant_tokens(seg)
            if toks:
                lead_candidates.append(toks[0])
    if in_tokens:
        lead_candidates.append(in_tokens[0])

    first_ok = any(c in out_tokens for c in lead_candidates)
    overlap = sum(1 for t in in_tokens if t in out_tokens)
    coverage = overlap / len(in_tokens)

    # The first-significant-token guard applies at ALL scores. token_set_ratio
    # returns 100 whenever one name's tokens are a subset of the other's, so a
    # perfect score alone does NOT prove identity — e.g. "Punjab National Bank"
    # scores 100 against "National Bank Holdings". Require a lead token to match.
    if not first_ok:
        return "UNRESOLVED"

    if coverage >= 0.5 and score >= 90:
        return "VERIFIED"
    if score >= 85:
        return "REVIEW"
    if coverage >= 0.5 and score >= 92:
        return "REVIEW"
    return "UNRESOLVED"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    # Match-quality tests
    print("MRC/DMC:", classify_match("MRC Global", "DMC Global Inc.", 90, "BOOM"))
    print("Newell :", classify_match("Newell Brands", "NEWELL BRANDS INC.", 100, "NWL"))
    print("BNY    :", classify_match("Bank of New York (BNY)", "Bank of New York Mellon Corp", 89, "BK"))
    print("Thyssen:", classify_match("ThyssenKrupp Group", "ThredUp Inc.", 53, "TDUP"))

    # Evidence test on Newell 10-K
    ev = evidence_for_keyword(
        "https://www.sec.gov/Archives/edgar/data/814453/000081445326000008/nwl-20251231.htm",
        "divestiture",
    )
    if ev:
        print("\nEvidence URL:\n", ev["url"])
        print("\nQuote:\n", ev["quote"])
