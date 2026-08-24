"""
Contact Rolodex — Stage 3 of the workflow.

The Corporate List already carries the outreach layer: a CFO and a Corporate
Development contact (name, title, email) for most companies. The scanner ingested
only company name + revenue and dropped these columns, so a flagged target has no
one attached to it. This module extracts the contacts into a clean, name-keyed
rolodex and joins them back onto the target set.

Corp Dev is the primary outreach contact — that desk runs divestitures — with the
CFO as fallback.

Company names are normalized to the SAME canonical form the identity gate uses
(evidence.classify_match helpers), so contacts join to the enriched targets even
when the list spells a name differently from EDGAR ("JPMorganChase" vs "JPMorgan
Chase & Co.").
"""

import re
import logging
from pathlib import Path

import pandas as pd

import evidence  # reuse _canon / _significant_tokens for join keys

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("contacts")

DEFAULT_LIST = "/Users/eshan/Downloads/CORPORATE LIST 2026 FINAL  (1).xlsx"
# repo-relative so the refresh runs anywhere (CI, another machine) — the committed
# data/contacts.csv is the runtime source; DEFAULT_LIST is only the regeneration input.
OUT = str(Path(__file__).parent / "data" / "contacts.csv")

_EMAIL = re.compile(r"[\w.\-+]+@[\w\-]+\.[\w.\-]+")


def _join_key(name: str) -> str:
    """Canonical, order-independent key: squished significant tokens."""
    return "".join(evidence._significant_tokens(name))


def _first_email(*vals) -> str:
    for v in vals:
        if pd.notna(v):
            m = _EMAIL.search(str(v))
            if m:
                return m.group(0)
    return ""


def _clean(v) -> str:
    return "" if pd.isna(v) else re.sub(r"\s+", " ", str(v)).strip()


def _col(df, *names):
    """Return the first column whose stripped header matches one of `names`."""
    lut = {str(c).strip(): c for c in df.columns}
    for n in names:
        if n in lut:
            return lut[n]
    return None


# ---------------------------------------------------------------------------
# Per-tab extractors (the four tabs have different contact layouts)
# ---------------------------------------------------------------------------

def _fortune1000(path):
    d = pd.read_excel(path, sheet_name="Fortune 1000", header=3)
    name_c = _col(d, "Company Name", "Company")
    d = d[d[name_c].notna()]
    rows = []
    cfo_first, cfo_last, cfo_title, cfo_email = (
        _col(d, "First Name"), _col(d, "Last Name"), _col(d, "Title"), _col(d, "Email"))
    cd_name, cd_title, cd_email = _col(d, "Name 1"), _col(d, "Title 1"), _col(d, "Email 1")
    for _, r in d.iterrows():
        cfo_nm = f"{_clean(r.get(cfo_first))} {_clean(r.get(cfo_last))}".strip()
        rows.append({
            "list_company": _clean(r[name_c]), "source": "Fortune 1000",
            "cfo_name": cfo_nm, "cfo_title": _clean(r.get(cfo_title)),
            "cfo_email": _first_email(r.get(cfo_email)),
            "corpdev_name": _clean(r.get(cd_name)), "corpdev_title": _clean(r.get(cd_title)),
            "corpdev_email": _first_email(r.get(cd_email)),
        })
    return rows


def _forbes_us(path):
    d = pd.read_excel(path, sheet_name="Forbes Global US HQ", header=5)
    name_c = _col(d, "Name")
    d = d[d[name_c].notna()]
    # CFO block: Name/Position, Position, Email ; Corp Dev block: Name/Position, Email
    cols = list(d.columns)
    email_cols = [c for c in cols if str(c).strip() == "Email"]
    np_cols = [c for c in cols if "Name/Position" in str(c)]
    rows = []
    for _, r in d.iterrows():
        cfo_np = _clean(r.get(np_cols[0])) if np_cols else ""
        cfo_email = _first_email(r.get(email_cols[0])) if email_cols else ""
        cd_np = _clean(r.get(np_cols[1])) if len(np_cols) > 1 else ""
        cd_email = _first_email(r.get(email_cols[1])) if len(email_cols) > 1 else ""
        rows.append({
            "list_company": _clean(r[name_c]), "source": "Forbes US HQ",
            "cfo_name": cfo_np, "cfo_title": "CFO" if cfo_np else "",
            "cfo_email": cfo_email,
            "corpdev_name": cd_np, "corpdev_title": "Corporate Development" if cd_np else "",
            "corpdev_email": cd_email,
        })
    return rows


def _fortune_lastyear(path):
    d = pd.read_excel(path, sheet_name="Fortune 1000 - Last Year's", header=3)
    name_c = _col(d, "Company")
    d = d[d[name_c].notna()]
    cols = list(d.columns)
    email_cols = [c for c in cols if str(c).strip() == "Email"]
    np_cols = [c for c in cols if "Name" in str(c) and "Company" not in str(c)]
    rows = []
    for _, r in d.iterrows():
        c1 = _clean(r.get(np_cols[0])) if np_cols else ""
        e1 = _first_email(r.get(email_cols[0])) if email_cols else ""
        c2 = _clean(r.get(np_cols[1])) if len(np_cols) > 1 else ""
        e2 = _first_email(r.get(email_cols[1])) if len(email_cols) > 1 else ""
        # these are analyst / IR contacts — file under corpdev with cfo fallback
        rows.append({
            "list_company": _clean(r[name_c]), "source": "Fortune LY",
            "cfo_name": c2, "cfo_title": "", "cfo_email": e2,
            "corpdev_name": c1, "corpdev_title": "", "corpdev_email": e1,
        })
    return rows


def build_rolodex(path=DEFAULT_LIST) -> pd.DataFrame:
    rows = []
    for fn in (_fortune1000, _forbes_us, _fortune_lastyear):
        try:
            rows += fn(path)
        except Exception as e:
            log.warning(f"{fn.__name__} failed: {e}")
    df = pd.DataFrame(rows)
    df["join_key"] = df["list_company"].map(_join_key)
    # primary outreach target = corp dev, else cfo
    df["primary_name"] = df["corpdev_name"].where(df["corpdev_email"] != "", df["cfo_name"])
    df["primary_title"] = df["corpdev_title"].where(df["corpdev_email"] != "", df["cfo_title"])
    df["primary_email"] = df["corpdev_email"].where(df["corpdev_email"] != "", df["cfo_email"])
    df["primary_role"] = df["corpdev_email"].apply(
        lambda e: "Corporate Development" if e else "CFO")
    # dedupe by join key, prefer the row that actually has a corp-dev email
    df = df.sort_values("corpdev_email", ascending=False).drop_duplicates("join_key", keep="first")
    return df


# ---------------------------------------------------------------------------
# Join onto a targets dataframe
# ---------------------------------------------------------------------------

CONTACT_COLS = ["primary_name", "primary_title", "primary_email", "primary_role",
                "corpdev_name", "corpdev_email", "cfo_name", "cfo_email", "list_company"]


def _load_rolodex(path=DEFAULT_LIST) -> pd.DataFrame:
    """Prefer the repo-persisted rolodex (data/contacts.csv) — the dashboard runs
    in a sandbox that cannot read the source list under ~/Downloads. Fall back to
    building it from the source list when the CSV is absent (e.g. a fresh machine)."""
    if Path(OUT).exists():
        rolo = pd.read_csv(OUT, dtype=str, keep_default_na=False)
        rolo = rolo[rolo["join_key"].astype(str).str.len() > 0]
        return rolo
    return build_rolodex(path)


def attach_contacts(targets: pd.DataFrame, path=DEFAULT_LIST,
                    company_col="Company") -> pd.DataFrame:
    """Left-join the rolodex onto `targets` by canonical company name."""
    rolo = _load_rolodex(path)
    t = targets.copy()
    t["_jk"] = t[company_col].map(_join_key)
    r = rolo.set_index("join_key")[CONTACT_COLS]
    out = t.merge(r, left_on="_jk", right_index=True, how="left").drop(columns="_jk")
    out["has_contact"] = out["primary_email"].fillna("").ne("")
    return out


if __name__ == "__main__":
    Path(OUT).parent.mkdir(exist_ok=True)
    rolo = build_rolodex()
    rolo.to_csv(OUT, index=False)
    print(f"Rolodex: {len(rolo)} companies")
    print(f"  with Corporate Development email : {(rolo['corpdev_email'] != '').sum()}")
    print(f"  with CFO email                   : {(rolo['cfo_email'] != '').sum()}")
    print(f"  with ANY primary contact         : {(rolo['primary_email'] != '').sum()}")
    print(f"  primary role = Corp Dev          : {(rolo['primary_role'] == 'Corporate Development').sum()}")
    print(f"\nSample:")
    for _, r in rolo[rolo["primary_email"] != ""].head(6).iterrows():
        print(f"  {r['list_company'][:26]:<26} {r['primary_role']:<22} "
              f"{r['primary_name'][:24]:<24} {r['primary_email']}")
    print(f"\nWrote {OUT}")
