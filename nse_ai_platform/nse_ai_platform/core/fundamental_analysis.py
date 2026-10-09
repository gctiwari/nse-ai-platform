"""
core/fundamental_analysis.py
Pillars 2.1 (Growth) and 2.4 (Cash Flow Quality) from the analyst-grade
redesign spec. Each scoring function returns (score_0_100, facts) --
facts is a list of plain-English strings that justified the score, same
pattern as the rest of the app's "no black box" explanation requirement.

Deliberately self-contained (no import from core/scoring.py) to avoid a
circular import, since core/scoring.py calls into this module, not the
other way around -- same relationship core/scoring.py already has with
core/disqualifiers.py.

Deferred from the full spec for this pass (documented, not silently
skipped): quarterly growth-trend acceleration/deceleration (§2.1, needs
quarterly_income_stmt which isn't fetched yet), and buyback/dividend
funded-by-debt detection (§2.4, needs financing cash flow which isn't
fetched yet).
"""

from __future__ import annotations

from core.models import MarketSnapshot


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _scale(value: float, low: float, high: float) -> float:
    if high == low:
        return 50.0
    return _clip((value - low) / (high - low) * 100)


def compute_cagr(history: list) -> float | None:
    """CAGR as a percentage over len(history)-1 years. Returns None when
    it can't be computed meaningfully (too few points, or a non-positive
    start/end value makes a geometric growth rate undefined)."""
    if not history or len(history) < 2:
        return None
    start, end = history[0], history[-1]
    years = len(history) - 1
    if start <= 0 or end <= 0:
        return None
    return ((end / start) ** (1 / years) - 1) * 100


def positive_years_pct(history: list) -> float | None:
    """% of year-over-year steps that were increases -- the spec's
    "consistency, not just magnitude" check. None if there's not enough
    history to compute even one YoY step."""
    if not history or len(history) < 2:
        return None
    steps = len(history) - 1
    positive = sum(1 for i in range(1, len(history)) if history[i] > history[i - 1])
    return positive / steps * 100


def score_growth_pillar(snapshot: MarketSnapshot) -> tuple[float, list[str]]:
    """
    Multi-year revenue/earnings CAGR + consistency, replacing the old
    single-year revenueGrowth/earningsGrowth-only approach the spec
    specifically calls out as noisy. Falls back to the single-year info
    fields when multi-year history isn't available, so this still works
    (with a lower-confidence flavor, noted in facts) for a stock whose
    financial statements didn't fetch cleanly.
    """
    facts: list[str] = []
    rev_hist = snapshot.revenue_history_annual
    earn_hist = snapshot.earnings_history_annual

    rev_cagr = compute_cagr(rev_hist)
    earn_cagr = compute_cagr(earn_hist)
    rev_consistency = positive_years_pct(rev_hist)
    earn_consistency = positive_years_pct(earn_hist)

    used_fallback = False
    if rev_cagr is None:
        rev_cagr = snapshot.revenue_growth_pct
        used_fallback = True
    if earn_cagr is None:
        earn_cagr = snapshot.profit_growth_pct
        used_fallback = True

    rev_score = _scale(rev_cagr, -5, 25)
    earn_score = _scale(earn_cagr, -10, 35)

    # Consistency defaults to a neutral 50 (not available) rather than
    # rewarding/penalizing a stock for missing history specifically here.
    consistency_vals = [v for v in (rev_consistency, earn_consistency) if v is not None]
    consistency_score = sum(consistency_vals) / len(consistency_vals) if consistency_vals else 50.0

    score = round(0.35 * rev_score + 0.35 * earn_score + 0.30 * consistency_score, 1)

    years_label = f"{len(rev_hist) - 1}yr" if len(rev_hist) >= 2 else "1yr"
    facts.append(f"Revenue CAGR {rev_cagr:+.1f}% ({years_label})" +
                 (" [single-year estimate, multi-year history unavailable]" if used_fallback else ""))
    facts.append(f"Earnings CAGR {earn_cagr:+.1f}%")
    if consistency_vals:
        facts.append(f"{consistency_score:.0f}% of tracked years had positive YoY growth (consistency)")

    return score, facts


def score_cash_flow_quality_pillar(snapshot: MarketSnapshot) -> tuple[float, list[str]]:
    """
    Operating cash flow / net income (the spec's highest-value single
    check -- "catches more real-world problems than almost anything else
    on this list"), capex intensity, and whether FCF is keeping pace with
    reported earnings growth. This is a POSITIVE scoring pillar; the same
    OCF/NI and FCF history also feed core/disqualifiers.py's hard-veto
    checks, but that's a separate, earlier gate -- a stock that clears the
    disqualifier floor still gets ranked here on the same underlying data.
    """
    facts: list[str] = []

    ocf_ni_hist = snapshot.ocf_to_ni_history
    if ocf_ni_hist:
        avg_ocf_ni = sum(ocf_ni_hist) / len(ocf_ni_hist)
        ocf_ni_score = _scale(avg_ocf_ni, 0.3, 1.3)
        facts.append(f"OCF/Net Income averaging {avg_ocf_ni:.2f}x over {len(ocf_ni_hist)} year(s)")
    else:
        ocf_ni_score = 50.0
        facts.append("OCF/Net Income history unavailable")

    capex_hist = snapshot.capex_history_annual
    rev_hist = snapshot.revenue_history_annual
    avg_intensity = None
    if capex_hist and rev_hist and len(rev_hist) >= len(capex_hist) and len(capex_hist) > 0:
        recent_rev = rev_hist[-len(capex_hist):]
        intensities = [capex_hist[i] / recent_rev[i] * 100 for i in range(len(capex_hist)) if recent_rev[i] > 0]
        avg_intensity = sum(intensities) / len(intensities) if intensities else None

    if avg_intensity is not None:
        # Moderate capex intensity is normal and not penalized much; only
        # genuinely heavy, sustained capex spend (as % of revenue) drags
        # the score -- this isn't inherently bad (could be growth capex),
        # just a fact worth surfacing.
        capex_score = _scale(-avg_intensity, -30, -4)
        facts.append(f"Capex intensity {avg_intensity:.1f}% of revenue")
    else:
        capex_score = 55.0
        facts.append("Capex intensity unavailable")

    fcf_hist = snapshot.fcf_history_annual
    earn_hist = snapshot.earnings_history_annual
    fcf_cagr = compute_cagr(fcf_hist)
    earn_cagr = compute_cagr(earn_hist)
    if fcf_cagr is not None and earn_cagr is not None:
        gap = fcf_cagr - earn_cagr
        alignment_score = _scale(gap, -20, 10)
        facts.append(f"FCF CAGR {fcf_cagr:+.1f}% vs earnings CAGR {earn_cagr:+.1f}%")
        if gap < -15:
            facts.append("FCF growing meaningfully slower than reported earnings -- worth scrutinizing")
    else:
        alignment_score = 50.0

    score = round(0.5 * ocf_ni_score + 0.25 * capex_score + 0.25 * alignment_score, 1)
    return score, facts
