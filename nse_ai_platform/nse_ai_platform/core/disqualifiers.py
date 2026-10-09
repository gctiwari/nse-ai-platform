"""
core/disqualifiers.py
Hard disqualifiers: red flags that exclude a stock from recommendation
regardless of how good its other numbers look. An analyst wouldn't call a
stock "Conservative" just because its weighted average score cleared a
bar -- certain conditions veto that outright.

This is deliberately a SEPARATE, EARLIER gate from core/scoring.py's
weighted blend: disqualifiers are checked first, and a disqualified stock
never reaches the scoring/classification stage at all (mirrors how
`overall_ai_score < min_overall_score` already excludes a stock in
core/recommendation_engine.py -- this is the same "return None to exclude"
pattern, just with explicit, named reasons instead of an opaque score cutoff).

Deliberate exception to the rest of the app's "missing data = neutral
midpoint" philosophy: for the specific fields that determine whether a
stock is fundamentally healthy at all, missing data is NOT treated as
neutral -- see `MIN_DATA_COMPLETENESS_PCT` below. That default is right
for optional secondary signals; it's wrong for "can we tell if this
company is solvent."
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.models import MarketSnapshot


@dataclass
class DisqualifierThresholds:
    """Tunable without touching logic -- same pattern as ScoringWeights/
    ClassificationThresholds elsewhere in the app."""
    max_debt_to_equity: float = 3.0          # non-financial sector only; see is_financial_sector
    min_interest_coverage: float = 1.5
    structural_decline_years: int = 3         # revenue AND earnings both declining this many years = structural
    min_ocf_to_ni_sustained: float = 0.5      # sustained below this over 2+ years = earnings quality red flag
    ocf_ni_sustained_years: int = 2
    max_fcf_negative_years: int = 3           # negative FCF this many consecutive years disqualifies...
                                                # ...UNLESS the Aggressive small/mid-cap growth carve-out applies
    min_data_completeness_pct: float = 60.0   # below this, "Insufficient Data" -- exclude rather than guess


DEFAULT_DISQUALIFIER_THRESHOLDS = DisqualifierThresholds()


@dataclass
class DisqualifierResult:
    is_disqualified: bool
    insufficient_data: bool
    reasons: list = field(default_factory=list)
    # True only when the SOLE disqualifying reason is negative FCF, and the
    # stock is otherwise clean and small/mid-cap -- the spec's explicit
    # carve-out for growth-stage companies where burning cash is a known,
    # disclosed part of the thesis rather than a red flag.
    aggressive_carveout_eligible: bool = False


def _consecutive_declining_years(history: list) -> int:
    """Counts consecutive YoY declines ending at the most recent period."""
    if len(history) < 2:
        return 0
    count = 0
    for i in range(len(history) - 1, 0, -1):
        if history[i] < history[i - 1]:
            count += 1
        else:
            break
    return count


def _consecutive_negative_years(history: list) -> int:
    if not history:
        return 0
    count = 0
    for v in reversed(history):
        if v < 0:
            count += 1
        else:
            break
    return count


def check_disqualifiers(snapshot: MarketSnapshot, market_cap_category: str,
                         thresholds: DisqualifierThresholds = DEFAULT_DISQUALIFIER_THRESHOLDS
                         ) -> DisqualifierResult:
    reasons: list[str] = []

    # --- Insufficient data gate goes first: if we can't tell whether this
    # stock is healthy, don't guess -- exclude it outright rather than
    # silently scoring it as "average" via neutral defaults. ---
    if snapshot.deep_fields_total > 0:
        completeness_pct = snapshot.deep_fields_available / snapshot.deep_fields_total * 100
        if completeness_pct < thresholds.min_data_completeness_pct:
            return DisqualifierResult(
                is_disqualified=True, insufficient_data=True,
                reasons=[f"Insufficient data to assess financial health "
                         f"({completeness_pct:.0f}% of load-bearing fields available, "
                         f"need {thresholds.min_data_completeness_pct:.0f}%)"],
            )

    # --- Negative or near-zero equity ---
    if snapshot.total_equity is not None and snapshot.total_equity <= 0:
        reasons.append(f"Negative or near-zero total equity (Rs.{snapshot.total_equity:,.0f})")

    # --- Leverage ceiling (skip for banks/NBFCs -- different leverage lens) ---
    if not snapshot.is_financial_sector and snapshot.debt_to_equity > thresholds.max_debt_to_equity:
        reasons.append(f"Debt/Equity {snapshot.debt_to_equity:.2f}x exceeds the "
                        f"{thresholds.max_debt_to_equity:.1f}x ceiling")

    # --- Interest coverage ---
    if snapshot.interest_coverage is not None and snapshot.interest_coverage < thresholds.min_interest_coverage:
        reasons.append(f"Interest coverage {snapshot.interest_coverage:.2f}x is below "
                        f"{thresholds.min_interest_coverage:.1f}x -- can't comfortably service debt "
                        f"from operating earnings")

    # --- Structural decline: revenue AND earnings both falling for N+ years ---
    rev_decline = _consecutive_declining_years(snapshot.revenue_history_annual)
    earn_decline = _consecutive_declining_years(snapshot.earnings_history_annual)
    if (rev_decline >= thresholds.structural_decline_years and
            earn_decline >= thresholds.structural_decline_years):
        reasons.append(f"Structural decline: revenue AND earnings both falling for "
                        f"{min(rev_decline, earn_decline)}+ consecutive years")

    # --- Earnings quality: OCF/NI sustained below floor ---
    if len(snapshot.ocf_to_ni_history) >= thresholds.ocf_ni_sustained_years:
        recent = snapshot.ocf_to_ni_history[-thresholds.ocf_ni_sustained_years:]
        if all(r < thresholds.min_ocf_to_ni_sustained for r in recent):
            reasons.append(f"Operating cash flow / net income below "
                            f"{thresholds.min_ocf_to_ni_sustained:.1f}x for "
                            f"{thresholds.ocf_ni_sustained_years}+ years running "
                            f"(profit isn't converting to cash -- earnings quality red flag)")

    # --- Negative FCF for N+ consecutive years (with Aggressive carve-out) ---
    fcf_negative_years = _consecutive_negative_years(snapshot.fcf_history_annual)
    fcf_disqualifies = fcf_negative_years >= thresholds.max_fcf_negative_years
    carveout_eligible = False
    if fcf_disqualifies:
        is_smallmid = market_cap_category in ("Small", "Mid")
        # Carve-out only applies if FCF is the ONLY problem found so far --
        # a growth story with clean leverage/coverage/earnings-quality can
        # get the pass; one that's ALSO over-levered or structurally
        # declining cannot use "growth stage" as cover.
        if is_smallmid and not reasons:
            carveout_eligible = True
        else:
            reasons.append(f"Free cash flow negative for {fcf_negative_years}+ consecutive years")

    is_disqualified = bool(reasons) or (fcf_disqualifies and not carveout_eligible)
    if fcf_disqualifies and carveout_eligible:
        # Not added to `reasons` as a disqualifier -- it's disclosed instead
        # of penalized, per the spec's explicit Aggressive-only exception.
        pass

    return DisqualifierResult(
        is_disqualified=is_disqualified,
        insufficient_data=False,
        reasons=reasons,
        aggressive_carveout_eligible=carveout_eligible,
    )
