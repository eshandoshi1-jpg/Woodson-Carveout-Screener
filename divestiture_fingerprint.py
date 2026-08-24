"""
Divestiture Fingerprint  —  v2, aligned to the MASTER TRAINING DOCUMENT (v1.0)

Scores divestiture propensity from conditions visible in filings, per the
Conditions Engine factor set in PART 2A.

WHAT CHANGED IN v2 (and why v1 was wrong)
-----------------------------------------
v1 scored a single "weakest segment" model: sub-scale + negative margin + decay.
The case library shows that is an overfit which misses two whole classes of deal:

  * CS-008 (NN Inc) — a FORCED SELLER sold its CROWN JEWEL (Life Sciences, its best
    division) to pay down debt. A weakness-ranking model ranks that segment LAST.
  * CS-006 (Hillenbrand/Batesville) — a PROFITABLE orphan (~40% of cash flow) sold
    on strategic-identity mismatch, not weakness. A margin-gated model never fires.

So v2 splits into two sub-models (PART 2A "two seller archetypes"):

  1. STRATEGIC PRUNER    -> predict the weakest-fit segment (orphan logic, P1/P3/P6)
  2. BALANCE-SHEET-FORCED -> flag the PARENT as a forced seller and rank segments by
     MARKETABILITY, not weakness (P2). Never penalized for ranking a crown jewel low.

Strategic-identity mismatch is now a first-class factor so profitable orphans score.

Patterns: P1 orphan · P2 deleveraging-forced · P3 failed bolt-on · P4 serial divester
          P5 activist · P6 post-megadeal pruning · P7 distress/token
"""

from typing import Optional

# ── Parent-level: is this a FORCED seller? (P2) ─────────────────────────────
W_DELEVER_INTENT   = 4   # proceeds explicitly earmarked for debt paydown
W_LEVERAGE_HIGH    = 2   # elevated / post-acquisition leverage spike
W_PARENT_DISTRESS  = 2   # net loss, covenant pressure, going-concern language

# ── Parent-level: event triggers ───────────────────────────────────────────
W_MEGADEAL_TRIGGER = 3   # P6: acquisition >25% of parent EV in trailing 24 months
W_SERIAL_DIVESTER  = 3   # P4: any completed divestiture in trailing 18 months
W_ACTIVIST_BOARD   = 4   # P5: board seat(s)
W_ACTIVIST_FIGHT   = 3   # P5: nomination fight
W_ACTIVIST_LETTER  = 2   # P5: public letter / campaign
W_ACTIVIST_PASSIVE = 1   # P5: passive stake only (13G)
W_PARENT_REV_DECL  = 2
W_RESTRUCTURING    = 1

# ── Segment-level: orphan logic (strategic-pruner sub-model) ───────────────
W_SUBSCALE_STRONG  = 3   # <5% of parent revenue (P1)
W_SUBSCALE_MOD     = 1   # 5-10%
W_NEG_MARGIN       = 3   # negative gross/operating margin (P1)
W_MARGIN_GAP       = 2   # materially below parent average
W_DECAY_STRONG     = 3   # revenue YoY < -25%
W_DECAY_MOD        = 1   # -10% to -25%
W_IDENTITY_MISMATCH= 5   # sector/strategy mismatch vs parent — PRIMARY signal for profitable
                         # orphans (CS-006 Batesville), which weakness-only scoring misses
W_FAILED_BOLTON    = 3   # P3: acquired 3-6y ago, revenue since -40%+
W_CRITERIA_MISS    = 2   # fails the parent's own published portfolio criteria

# ── Segment-level: marketability (forced-seller sub-model) ─────────────────
W_MKT_MARGIN       = 3   # healthy/premium margin -> sells well
W_MKT_SCALE        = 2   # material scale -> clears a real check
W_MKT_GROWTH       = 2   # growing -> attracts buyers
W_MKT_SEPARABLE    = 1   # clean reportable segment / standalone division

MAX_PRUNER = (W_SUBSCALE_STRONG + W_NEG_MARGIN + W_DECAY_STRONG + W_IDENTITY_MISMATCH
              + W_FAILED_BOLTON + W_CRITERIA_MISS + W_MEGADEAL_TRIGGER + W_SERIAL_DIVESTER
              + W_ACTIVIST_BOARD + W_PARENT_REV_DECL + W_RESTRUCTURING + W_LEVERAGE_HIGH)
MAX_FORCED = (W_DELEVER_INTENT + W_LEVERAGE_HIGH + W_PARENT_DISTRESS + W_SERIAL_DIVESTER
              + W_ACTIVIST_BOARD + W_MKT_MARGIN + W_MKT_SCALE + W_MKT_GROWTH + W_MKT_SEPARABLE)


def _num(x) -> Optional[float]:
    try:
        if x is None:
            return None
        f = float(x)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Archetype classification (PART 2A)
# ---------------------------------------------------------------------------

def classify_seller(parent: dict) -> dict:
    """Decide whether the PARENT is a balance-sheet-forced seller or a strategic pruner.

    Forced sellers sell whatever clears at the best price — often the crown jewel —
    so segment ranking must switch from weakness to marketability.
    """
    score, why = 0, []
    if parent.get("deleveraging_intent"):     # "proceeds to pay down debt"
        score += W_DELEVER_INTENT; why.append("stated deleveraging intent")
    if parent.get("leverage_high"):
        score += W_LEVERAGE_HIGH; why.append("elevated leverage")
    if parent.get("post_acquisition_leverage_spike"):
        score += W_LEVERAGE_HIGH; why.append("post-acquisition leverage spike")
    if parent.get("distress"):
        score += W_PARENT_DISTRESS; why.append("net loss / covenant pressure")
    forced = score >= W_DELEVER_INTENT      # stated intent alone is sufficient
    return {"archetype": "forced_seller" if forced else "strategic_pruner",
            "forced_score": score, "why": why}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _parent_factors(p: dict, hits, add) -> int:
    """Apply parent-level factors; returns the measurable weight added to the denominator."""
    possible = 0
    for key, w in (("megadeal_trigger", W_MEGADEAL_TRIGGER), ("serial_divester", W_SERIAL_DIVESTER),
                   ("activist_level", W_ACTIVIST_BOARD), ("rev_yoy_pct", W_PARENT_REV_DECL),
                   ("restructuring", W_RESTRUCTURING), ("leverage_high", W_LEVERAGE_HIGH)):
        if p.get(key) is not None:
            possible += w
    if p.get("megadeal_trigger"):
        add(W_MEGADEAL_TRIGGER, "P6 post-megadeal pruning",
            "acquisition >25% of parent EV in trailing 24 months")
    if p.get("serial_divester"):
        add(W_SERIAL_DIVESTER, "P4 serial divester",
            "a divestiture completed in the trailing 18 months")
    act = (p.get("activist_level") or "").lower()
    if act == "board_seat":
        add(W_ACTIVIST_BOARD, "P5 activist — board seat", "activist holds board representation")
    elif act in ("nomination", "proxy_fight"):
        add(W_ACTIVIST_FIGHT, "P5 activist — nomination fight", "activist running a slate")
    elif act in ("letter", "campaign"):
        add(W_ACTIVIST_LETTER, "P5 activist — public campaign", "activist letter / public campaign")
    elif act in ("passive", "13g"):
        add(W_ACTIVIST_PASSIVE, "P5 activist — passive stake", "13G passive holder")
    ry = _num(p.get("rev_yoy_pct"))
    if ry is not None and ry < 0:
        add(W_PARENT_REV_DECL, "Parent revenue declining", f"consolidated revenue {ry:+.1f}% YoY")
    if p.get("restructuring"):
        add(W_RESTRUCTURING, "Restructuring in progress", "active restructuring program")
    if p.get("leverage_high"):
        add(W_LEVERAGE_HIGH, "Elevated leverage", "net leverage above comfort")
    return possible


def score_segment(seg: dict, parent: dict = None) -> dict:
    """Score one (segment, parent) under BOTH sub-models and return the stronger.

    A parent can be deleveraging AND pruning orphans at the same time (CS-001 is
    tagged P1+P2+P3), so the archetypes are not mutually exclusive. We run the
    orphan model and the marketability model independently and report whichever
    produces the higher normalized score, plus both for transparency.
    """
    p = parent if parent is not None else seg
    pruner = _score_one(seg, p, "strategic_pruner")
    # The marketability model only applies once the PARENT is flagged a forced
    # seller ("flag the parent, THEN rank by marketability"). Applying it to a
    # healthy parent would score every good segment as a divestiture candidate.
    if classify_seller(p)["archetype"] != "forced_seller":
        pruner["alternate"] = {"archetype": "forced_seller", "score": 0, "pct": 0.0,
                               "note": "not applied — parent is not a forced seller"}
        return pruner
    forced = _score_one(seg, p, "forced_seller")
    best = max((pruner, forced), key=lambda r: r["pct"])
    best["alternate"] = {"archetype": (forced if best is pruner else pruner)["archetype"],
                         "score": (forced if best is pruner else pruner)["score"],
                         "pct": (forced if best is pruner else pruner)["pct"]}
    return best


def _score_one(seg: dict, p: dict, mode: str) -> dict:
    cls = classify_seller(p)
    hits, gaps, score, possible = [], [], 0, 0

    def add(pts, factor, evidence):
        nonlocal score
        score += pts
        hits.append({"factor": factor, "points": pts, "evidence": evidence})

    def avail(w):
        """Count a factor's max weight toward the denominator only when we could
        actually evaluate it — grading against a theoretical max understates."""
        nonlocal possible
        possible += w

    sr, pr = _num(seg.get("seg_rev_M")), _num(seg.get("parent_rev_M"))
    share = (sr / pr * 100) if (sr is not None and pr) else None
    margin = _num(seg.get("seg_op_margin_pct"))
    co_margin = _num(seg.get("co_avg_margin_pct"))
    decay = _num(seg.get("seg_rev_yoy_pct"))

    if mode == "forced_seller":
        # ---- FORCED SELLER: rank by MARKETABILITY, never by weakness ------
        avail(W_DELEVER_INTENT + W_LEVERAGE_HIGH + W_PARENT_DISTRESS)
        for pts, why in [(W_DELEVER_INTENT if p.get("deleveraging_intent") else 0,
                          "stated deleveraging intent"),
                         (W_LEVERAGE_HIGH if p.get("leverage_high") else 0, "elevated leverage"),
                         (W_PARENT_DISTRESS if p.get("distress") else 0, "net loss / covenant pressure")]:
            if pts:
                add(pts, "P2 balance-sheet-forced seller", why)
        if margin is not None:
            avail(W_MKT_MARGIN)
            if margin > 0 and (co_margin is None or margin >= co_margin):
                add(W_MKT_MARGIN, "Marketable — healthy margin",
                    f"segment margin {margin:.1f}% at/above parent average")
        else:
            gaps.append("segment margin")
        if share is not None:
            avail(W_MKT_SCALE)
            if share >= 10:
                add(W_MKT_SCALE, "Marketable — material scale", f"{share:.1f}% of parent revenue")
        else:
            gaps.append("segment vs. parent revenue")
        if decay is not None:
            avail(W_MKT_GROWTH)
            if decay > 0:
                add(W_MKT_GROWTH, "Marketable — growing", f"segment revenue {decay:+.1f}% YoY")
        else:
            gaps.append("segment revenue YoY")
        avail(W_MKT_SEPARABLE)
        if seg.get("separable", True):
            add(W_MKT_SEPARABLE, "Separable unit", "reportable segment / standalone division")
        possible += _parent_factors(p, hits, add)
    else:
        # ---- STRATEGIC PRUNER: orphan logic ------------------------------
        # Orphan status has TWO alternative evidence routes: weakness (sub-scale /
        # negative margin / decay) OR strategic-identity mismatch. When the case is
        # mismatch-driven (CS-006 Batesville, profitable), the weakness factors are
        # not the operative path — counting their unearned weight in the denominator
        # would bury exactly the class of deal the doc says we must catch.
        mismatch_route = bool(seg.get("identity_mismatch"))

        if share is not None:
            if share < 5:
                avail(W_SUBSCALE_STRONG)
                add(W_SUBSCALE_STRONG, "P1 sub-scale segment", f"{share:.1f}% of parent revenue (<5%)")
            elif share < 10:
                avail(W_SUBSCALE_STRONG)
                add(W_SUBSCALE_MOD, "P1 sub-scale segment", f"{share:.1f}% of parent revenue")
            elif not mismatch_route:
                avail(W_SUBSCALE_STRONG)
        else:
            gaps.append("segment vs. parent revenue")

        if margin is not None:
            if margin < 0:
                avail(W_NEG_MARGIN)
                add(W_NEG_MARGIN, "P1 negative margin", f"segment margin {margin:.1f}% (negative)")
            elif co_margin is not None and (co_margin - margin) > 10:
                avail(W_NEG_MARGIN)
                add(W_MARGIN_GAP, "P1 margin gap vs parent",
                    f"segment {margin:.1f}% vs parent avg {co_margin:.1f}%")
            elif not mismatch_route:
                avail(W_NEG_MARGIN)
        else:
            gaps.append("segment margin")

        if decay is not None:
            if decay < -25:
                avail(W_DECAY_STRONG)
                add(W_DECAY_STRONG, "Severe revenue decay", f"segment revenue {decay:+.1f}% YoY")
            elif decay < -10:
                avail(W_DECAY_STRONG)
                add(W_DECAY_MOD, "Revenue declining", f"segment revenue {decay:+.1f}% YoY")
            elif not mismatch_route:
                avail(W_DECAY_STRONG)
        else:
            gaps.append("segment revenue YoY")

        # Catches PROFITABLE orphans (CS-006 Batesville) that weakness-only misses
        if seg.get("identity_mismatch") is not None:
            avail(W_IDENTITY_MISMATCH)
            if seg.get("identity_mismatch"):
                add(W_IDENTITY_MISMATCH, "Strategic-identity mismatch",
                    seg.get("identity_mismatch_note") or "segment sector diverges from parent's direction")
        else:
            gaps.append("strategic-identity mismatch")

        if seg.get("failed_bolton") is not None:
            if not mismatch_route or seg.get("failed_bolton"):
                avail(W_FAILED_BOLTON)
            if seg.get("failed_bolton"):
                add(W_FAILED_BOLTON, "P3 failed bolt-on reversal",
                    "acquired 3-6y ago; revenue since declined >40%")
        else:
            gaps.append("acquisition history / failed bolt-on")

        if seg.get("fails_portfolio_criteria") is not None:
            avail(W_CRITERIA_MISS)
            if seg.get("fails_portfolio_criteria"):
                add(W_CRITERIA_MISS, "Fails parent's published portfolio criteria",
                    seg.get("criteria_note") or "below the parent's stated growth/margin thresholds")

        possible += _parent_factors(p, hits, add)

    pct = (score / possible) if possible else 0.0
    grade = ("A — high propensity" if pct >= 0.55 else
             "B — elevated" if pct >= 0.38 else
             "C — watch" if pct >= 0.22 else
             "D — low")
    return {"score": score, "max": possible, "pct": round(pct, 3), "grade": grade,
            "archetype": mode, "archetype_why": cls["why"],
            "parent_is_forced_seller": cls["archetype"] == "forced_seller",
            "patterns": sorted({h["factor"].split()[0] for h in hits
                                if h["factor"].startswith("P")}),
            "hits": hits, "gaps": gaps}


# ---------------------------------------------------------------------------
# Ground-truth anchors from the case library
# ---------------------------------------------------------------------------

CS001_IA_Q1_2024 = {   # strategic pruner selling an orphan
    "seg_rev_M": 4.3, "parent_rev_M": 232.1, "seg_op_margin_pct": -46.5,
    "co_avg_margin_pct": 3.0, "seg_rev_yoy_pct": -55.9,
    "identity_mismatch": True,
    "identity_mismatch_note": "warehouse-automation electronics inside a commercial-vehicle parts parent",
    "failed_bolton": True, "separable": True,
}
CS001_PARENT = {
    "rev_yoy_pct": -11.6, "leverage_high": True, "distress": True, "restructuring": True,
    "serial_divester": True, "deleveraging_intent": True, "activist_level": None,
}

CS008_LIFESCI = {      # forced seller selling the CROWN JEWEL
    "seg_rev_M": 200.0, "parent_rev_M": 850.0, "seg_op_margin_pct": 18.0,
    "co_avg_margin_pct": 6.0, "seg_rev_yoy_pct": 8.0, "separable": True,
    "identity_mismatch": False, "failed_bolton": False,
}
CS008_PARENT = {
    "rev_yoy_pct": -4.0, "leverage_high": True, "distress": True, "restructuring": False,
    "serial_divester": False, "deleveraging_intent": True, "activist_level": None,
}

CS006_BATESVILLE = {   # PROFITABLE orphan — strategic-identity mismatch
    "seg_rev_M": 570.0, "parent_rev_M": 2900.0, "seg_op_margin_pct": 24.0,
    "co_avg_margin_pct": 13.0, "seg_rev_yoy_pct": -3.0, "separable": True,
    "identity_mismatch": True,
    "identity_mismatch_note": "death-care products inside a parent repositioning to industrial processing",
    "failed_bolton": False,
}
CS006_PARENT = {
    "rev_yoy_pct": 2.0, "leverage_high": True, "distress": False, "restructuring": False,
    "serial_divester": False, "deleveraging_intent": False, "activist_level": None,
    "megadeal_trigger": True,
}


if __name__ == "__main__":
    for name, seg, par in [
        ("CS-001 CVGI / Industrial Automation (orphan)", CS001_IA_Q1_2024, CS001_PARENT),
        ("CS-008 NN Inc / Life Sciences (CROWN JEWEL)", CS008_LIFESCI, CS008_PARENT),
        ("CS-006 Hillenbrand / Batesville (PROFITABLE orphan)", CS006_BATESVILLE, CS006_PARENT),
    ]:
        r = score_segment(seg, par)
        print(f"\n{name}")
        print(f"  archetype : {r['archetype']}  ({', '.join(r['archetype_why']) or 'n/a'})")
        print(f"  score     : {r['score']}/{r['max']}  ({r['pct']:.0%})  ->  {r['grade']}")
        print(f"  patterns  : {', '.join(r['patterns']) or '—'}")
        for h in r["hits"]:
            print(f"     +{h['points']}  {h['factor']:<38} {h['evidence']}")
