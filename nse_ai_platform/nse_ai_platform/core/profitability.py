"""
core/profitability.py
Pillar 2.2 (Profitability & Efficiency) from the analyst-grade redesign
spec. Returns (score_0_100, facts), same pattern as
core/disqualifiers.py / core/fundamental_analysis.py.

Covers:
  - ROE / ROCE current level.
  - Net margin TREND (this year vs. multi-year average) -- expanding
    margins score higher than flat, flat higher than contracting,
    independent of the absolute margin level.
  - Asset turnover (revenue / total assets) as a capital-efficiency proxy.
  - DuPont-style leverage flag: ROE = Net Margin x Asset Turnover x Equity
    Multiplier. A high ROE built mainly on a high equity multiplier
    (leverage) is flagged and scored differently than the same ROE built
    on genuine margin/efficiency, per the spec's explicit "40% ROE on 2x
    leverage is not the same quality signal as 25% ROE on no leverage."

Deliberately self-contained (no import from core/scoring.py), matching
the existing disqualifiers.py / fundamental_analysis.py pattern.

Deferred from this pass: gross/operating margin trend specifically
(needs "Gross Profit"/"Operating Income" history from income_stmt, which
isn't fetched yet -- only the most-recent-period EBIT is currently
pulled, for interest coverage). Net margin trend (computable today from
revenue_history_annual/earnings_history_annual, which are fetched) stands
in as the primary margin-trend signal for this pass.
"""

from __future__ import annotations

from core.models import MarketSnapshot


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _scale(value: float, low: float, high: float) -> float:
    if high == low:
        return 50.0
    return _clip((value - low) / (high - low) * 100)


# DuPont leverage-flag thresholds -- "is this ROE mostly a leverage
# artifact rather than genuine operating quality." Tunable, same pattern
# as ScoringWeights/ClassificationThresholds/DisqualifierThresholds.
HIGH_ROE_THRESHOLD_PCT = 25.0
HIGH_EQUITY_MULTIPLIER_THRESHOLD = 2.5


def _net_margins(snapshot: MarketSnapshot) -> list:
    """Per-year net margin (%) from revenue/earnings history, aligned by
    index. Skips years with non-positive revenue (undefined margin)."""
    rev = snapshot.revenue_history_annual
    earn = snapshot.earnings_history_annual
    n = min(len(rev), len(earn))
    return [earn[i] / rev[i] * 100 for i in range(n) if rev[i] > 0]


def score_profitability_pillar(snapshot: MarketSnapshot) -> tuple:
    facts = []

    roe_score = _scale(snapshot.roe_pct, 5, 30)
    roce_score = _scale(snapshot.roce_pct, 5, 32)
    facts.append(f"ROE {snapshot.roe_pct:.1f}%, ROCE {snapshot.roce_pct:.1f}%")

    # --- Net margin trend: this year vs. the multi-year average ---
    margins = _net_margins(snapshot)
    if len(margins) >= 2:
        current_margin = margins[-1]
        avg_margin = sum(margins) / len(margins)
        margin_delta = current_margin - avg_margin
        if margin_delta > 1.0:
            margin_trend_score = _scale(margin_delta, 1.0, 5.0)
            facts.append(f"net margin expanding ({current_margin:.1f}% vs {avg_margin:.1f}% "
                         f"{len(margins)}yr avg)")
        elif margin_delta < -1.0:
            margin_trend_score = _scale(margin_delta, -5.0, -1.0)
            facts.append(f"net margin contracting ({current_margin:.1f}% vs {avg_margin:.1f}% "
                         f"{len(margins)}yr avg)")
        else:
            margin_trend_score = 55.0  # flat -- mildly above neutral, stability has some value
            facts.append(f"net margin roughly flat (~{current_margin:.1f}%)")
    else:
        margin_trend_score = 50.0
        facts.append("margin trend unavailable (insufficient multi-year history)")

    # --- Asset turnover: revenue / total assets ---
    asset_turnover = None
    if snapshot.total_assets and snapshot.total_assets > 0 and snapshot.revenue_history_annual:
        asset_turnover = snapshot.revenue_history_annual[-1] / snapshot.total_assets
        turnover_score = _scale(asset_turnover, 0.2, 1.5)
        facts.append(f"asset turnover {asset_turnover:.2f}x")
    else:
        turnover_score = 50.0

    # --- DuPont leverage flag ---
    equity_multiplier = None
    dupont_penalty = 0.0
    if snapshot.total_assets and snapshot.total_equity and snapshot.total_equity > 0:
        equity_multiplier = snapshot.total_assets / snapshot.total_equity
        if (snapshot.roe_pct >= HIGH_ROE_THRESHOLD_PCT and
                equity_multiplier >= HIGH_EQUITY_MULTIPLIER_THRESHOLD):
            # Don't zero out the ROE credit -- just temper it, since
            # leverage-boosted ROE is still real profit, just a different
            # (riskier) quality signal than margin/efficiency-driven ROE.
            dupont_penalty = min(20.0, (equity_multiplier - HIGH_EQUITY_MULTIPLIER_THRESHOLD) * 8)
            facts.append(f"NOTE: {snapshot.roe_pct:.0f}% ROE is significantly leverage-assisted "
                         f"(equity multiplier {equity_multiplier:.1f}x) -- not the same quality "
                         f"signal as the same ROE built on margin/efficiency alone")

    score = round(
        0.30 * roe_score + 0.20 * roce_score + 0.25 * margin_trend_score + 0.25 * turnover_score
        - dupont_penalty, 1
    )
    return _clip(score), facts
