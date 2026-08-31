"""
outreach.py — fill Woodson's outreach template with screener findings (Stage 5).

Takes the enriched targets + their contacts and produces a personalized email per
company, following the boss-approved format. The one variable line (paragraph 2)
is generated from the signals we ACTUALLY have, tiered by evidence strength:

  held-for-sale / stated deleveraging / exploring-alternatives  -> a factual claim
  fingerprint-only read (no filing language)                    -> our softer analytical read
  weak / core-business candidate or no read                     -> the generic non-core line

HONESTY RULES (this email goes to the company's own Corp Dev desk — a wrong claim
kills credibility):
  * Never assert "under strategic review" unless the filing language actually says so
    (Co_Timing_Signal == EXPLORATORY, and NOT a FETCH_FAIL from the big build).
  * Never name a division as "non-core" if it's really the company's core business —
    if the candidate is a large share of the parent, drop the name and stay generic.
  * When in doubt, use the generic line. It's still a good email.

Nothing here sends anything. It writes drafts to data/outreach_drafts.csv for review.
"""

import re
import pandas as pd

import contacts as C

SENDER = "Joel Mathew"          # per the boss's template; swap per sender
FROM_FIRM = "Woodson Equity"
SUBJECT = "Woodson Equity: carveout / divestiture inquiry"
NAME_MAX_SHARE = 45.0           # above this % of parent, the "division" is ~core — don't name it
ENRICHED = "data/woodson_enriched.xlsx"
OUT = "data/outreach_drafts.csv"

# Reportable geographies are useful screening clues, but they are not evidence that
# management views the region as a separable, non-core business.  Keep the company in
# the outreach universe while suppressing the region name from the email.
GEOGRAPHIC_SEGMENT_RE = re.compile(
    r"\b(asia|asia[ -]?pacific|apac|europe|emea|middle east|africa|international|"
    r"united kingdom|u\.?k\.?|japan|australia|new zealand|janz|china|americas?|"
    r"north america|latin america|south america|canada|mexico)\b",
    re.IGNORECASE,
)


def _first_name(full: str) -> str:
    full = str(full or "").strip()
    if not full:
        return "there"
    # skip honorifics AND leading initials ("H. Jason Mullins" -> "Jason")
    for t in full.split():
        if re.fullmatch(r"(Mr|Ms|Mrs|Dr|Mx)\.?", t):
            continue
        if re.fullmatch(r"[A-Za-z]\.?", t):   # single-letter initial like "H." or "H"
            continue
        return t
    return full.split()[0]   # all-initials name: fall back to the first token


def _clean_div(s) -> str:
    s = str(s or "").strip()
    return "" if s.lower() in ("", "nan", "none") else s


def _truthy(value) -> bool:
    """Boolean conversion that treats missing pandas values as false."""
    if value is None or pd.isna(value):
        return False
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def is_geographic_segment(name: str) -> bool:
    """True when a candidate is a reporting geography rather than a named business."""
    return bool(GEOGRAPHIC_SEGMENT_RE.search(_clean_div(name)))


def _list_phrase(source: str) -> str:
    return {"Fortune 1000": "the Fortune 1000 list",
            "Fortune LY": "the Fortune 1000 list",
            "Forbes US HQ": "the Forbes Global 2000 list"}.get(str(source), "the Fortune 1000 list")


def personalization(r) -> str:
    """The evidence-specific second paragraph, tiered by what filings support."""
    company = r["Company"]
    div = _clean_div(r.get("FP_Candidate_Segment"))
    share = pd.to_numeric(pd.Series([r.get("FP_Candidate_Share_pct")]), errors="coerce").iloc[0]
    nameable = (bool(div) and not is_geographic_segment(div)
                and (pd.isna(share) or share <= NAME_MAX_SHARE))
    timing = str(r.get("Co_Timing_Signal") or "")
    delever = _truthy(r.get("Deleveraging_Intent"))
    hfs = _truthy(r.get("HFS_Live"))
    exploring = timing == "EXPLORATORY"          # real language only; FETCH_FAIL/PENDING excluded
    pruner = str(r.get("FP_Archetype")) == "strategic_pruner"

    discussion = ("we'd love to learn more and see if it would be a good fit to discuss since we are "
                  "an operationally focused firm with carveout experience")
    division = div if div.lower().endswith("division") else f"{div} division"

    if hfs:
        return (f"I recently reviewed your latest filings, and we saw that a business was classified "
                f"as held for sale, and {discussion}.")
    if exploring and nameable:
        return (f"I recently reviewed your latest filings, and we saw that the {division} had a review "
                f"of strategic alternatives underway, and {discussion}.")
    if delever and nameable:
        return (f"I recently reviewed your latest filings, and we saw a focus on reducing leverage. "
                f"The {division} may be non-core to that path, and {discussion}.")
    if delever:
        return (f"I recently reviewed your latest filings, and we saw a focus on reducing leverage. "
                f"We'd love to learn whether there are any non-core units that would be a good fit to "
                f"discuss since we are an operationally focused firm with carveout experience.")
    if nameable and pruner:
        return (f"I recently reviewed your latest filings, and the {division} stood out as potentially "
                f"non-core relative to the rest of the portfolio. {discussion.capitalize()}.")
    # Generic, evidence-safe version of the same streamlined paragraph.
    return (f"I recently reviewed your latest filings, and we'd love to learn whether {company} has any "
            f"non-core or underperforming business units that would be a good fit to discuss since we are "
            f"an operationally focused firm with carveout experience.")


def build_email(r) -> str:
    first = _first_name(r.get("primary_name"))
    body = (
        f"Hi {first},\n\n"
        f"My name is {SENDER} from {FROM_FIRM}. We are a private equity firm that specializes in corporate "
        f"carveouts and divestitures based in Washington, D.C. Our team has completed over 20 corporate "
        f"carveouts and we have the playbook on how to execute expeditiously with minimal lift to the "
        f"parent company.\n\n"
        f"{personalization(r)}\n\n"
        f"One example: last year we executed a complex corporate carveout from a public company and "
        f"closed the deal in 35 days, and came off of the TSA within the first 100 days. We have the "
        f"playbook to expeditiously execute corporate carveouts and divestitures while minimizing the "
        f"lift on your end.\n\n"
        f"Please advise on who from your team I should speak with regarding the above.\n\n"
        f"Thanks,\n\n{SENDER}"
    )
    # Email copy intentionally avoids typographic dashes for a plainer, human-written style.
    return body.replace("\u2014", "-").replace("\u2013", "-")


def build_drafts(path=ENRICHED, tiers=("Tier 1", "Tier 2")) -> pd.DataFrame:
    df = pd.read_excel(path, engine="openpyxl")
    co = df.drop_duplicates("Company")
    V = co["Match_Quality"].isin(["VERIFIED", "REVIEW"]) & (co["Region"] == "US")
    t = co[V & co["Tier"].isin(tiers)].copy()
    t = C.attach_contacts(t)
    # bring in which list the company came from (for the "I found you through …" line)
    rolo = C.build_rolodex()[["join_key", "source"]].drop_duplicates("join_key")
    t["_jk"] = t["Company"].map(C._join_key)
    t = t.merge(rolo, left_on="_jk", right_on="join_key", how="left").drop(columns=["_jk", "join_key"])
    t = t[t["primary_email"].fillna("") != ""]          # only targets we can actually reach
    t = t.sort_values("Propensity_Score", ascending=False)
    t["subject"] = SUBJECT
    t["email_body"] = t.apply(build_email, axis=1)
    return t


if __name__ == "__main__":
    d = build_drafts()
    keep = ["Company", "Tier", "Propensity_Score", "primary_name", "primary_role",
            "primary_email", "FP_Candidate_Segment", "subject", "email_body"]
    d[keep].to_csv(OUT, index=False)
    print(f"{len(d)} drafts written to {OUT}\n")
    for _, r in d.head(3).iterrows():
        print("=" * 78)
        print(f"TO: {r['primary_name']} <{r['primary_email']}>  ({r['Company']}, {r['Tier']})")
        print(f"SUBJECT: {r['subject']}")
        print("-" * 78)
        print(r["email_body"])
        print()
