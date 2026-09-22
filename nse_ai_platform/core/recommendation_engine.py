"""
core/recommendation_engine.py
Turns (MarketSnapshot, TechnicalLevels, ScoreBreakdown) into a full
Recommendation: category classification, buy range, stop loss, three
targets, fair value/margin of safety, holding period, risk level, and a
human-readable AI explanation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from core.models import (
    MarketSnapshot, TechnicalLevels, ScoreBreakdown,
    Recommendation, Category, RiskLevel,
)
from core.disqualifiers import check_disqualifiers, DisqualifierResult, DEFAULT_DISQUALIFIER_THRESHOLDS
from core.fundamental_analysis import score_growth_pillar, score_cash_flow_quality_pillar
from core.profitability import score_profitability_pillar
from news.models import NewsAnalysisResult

logger = logging.getLogger(__name__)

MAX_PER_CATEGORY = 10   # hard cap per tab -- quality over quantity

# Approximate absolute-rupee cutoffs for Large/Mid/Small cap classification.
LARGE_CAP_THRESHOLD_RUPEES = 70_000 * 1e7   # ~Rs 70,000 crore
MID_CAP_THRESHOLD_RUPEES = 15_000 * 1e7     # ~Rs 15,000 crore


def classify_market_cap(market_cap_rupees: float) -> str:
    if market_cap_rupees >= LARGE_CAP_THRESHOLD_RUPEES:
        return "Large"
    if market_cap_rupees >= MID_CAP_THRESHOLD_RUPEES:
        return "Mid"
    return "Small"


@dataclass
class ClassificationThresholds:
    """Quality gates. Every stock must pass ALL thresholds independently.
    3 high-conviction calls beat 20 noisy ones.
    Tune via: python debug_scores.py"""
    min_overall_score: float = 57.0          # raised from 45 -- requires genuine quality
    min_confidence_score: float = 52.0       # sub-score agreement: penalises conflicted signals
    min_risk_reward_ratio: float = 1.5       # must risk 1 to make at least 1.5
    watchlist_min_margin_of_safety_pct: float = 3.0
    conservative_risk_min: float = 62.0
    conservative_min_score: float = 57.0
    balanced_min_score: float = 52.0
    balanced_risk_min: float = 30.0


DEFAULT_THRESHOLDS = ClassificationThresholds()


def _fair_value(snapshot: MarketSnapshot) -> float:
    """
    Simple blended fair value: average of
      (a) EPS x fair PE (sector average, capped),
      (b) DCF-lite via FCF yield normalisation,
    A production system would run a full DCF; this Phase-1 blend is
    intentionally transparent and auditable.
    """
    fair_pe = min(snapshot.sector_avg_pe, 40) if snapshot.sector_avg_pe else 20
    pe_based_value = snapshot.eps * fair_pe if snapshot.eps > 0 else snapshot.price

    # FCF-per-share proxy: treat free_cash_flow as company-wide, roughly
    # normalise against market cap to get a yield-based fair value nudge.
    if snapshot.market_cap > 0:
        fcf_yield = snapshot.free_cash_flow / snapshot.market_cap
        fcf_adjustment = 1 + max(-0.25, min(0.25, fcf_yield))
    else:
        fcf_adjustment = 1.0

    blended = pe_based_value * fcf_adjustment
    # Sanity bound: fair value shouldn't be wildly detached from CMP (+-40%),
    # otherwise a low/negative EPS can produce nonsensical margin-of-safety swings.
    blended = max(snapshot.price * 0.6, min(snapshot.price * 1.4, blended))
    return round(blended, 2)


def _risk_level(scores: ScoreBreakdown, technicals: TechnicalLevels) -> RiskLevel:
    if scores.risk_score >= 65 and technicals.atr / 100 < 5:
        return RiskLevel.LOW
    if scores.risk_score >= 45:
        return RiskLevel.MEDIUM
    return RiskLevel.HIGH


def _investment_horizon(category: Category, risk_level: RiskLevel) -> tuple[str, int]:
    mapping = {
        (Category.CONSERVATIVE,): ("Long-term (12-24 months)", 450),
        (Category.BALANCED,): ("Medium-term (6-12 months)", 270),
        (Category.AGGRESSIVE,): ("Short-to-medium term (1-6 months)", 90),
        (Category.WATCHLIST,): ("Wait for entry (monitor)", 0),
    }
    for keys, val in mapping.items():
        if category in keys:
            return val
    return "Medium-term", 180


def _targets_and_stoploss(snapshot: MarketSnapshot, technicals: TechnicalLevels,
                           category: Category, fair_value: float) -> dict:
    price = snapshot.price
    atr = max(technicals.atr, price * 0.01)

    if category == Category.CONSERVATIVE:
        sl_mult, t1_mult, t2_mult, t3_mult = 2.0, 2.0, 3.5, 5.5
    elif category == Category.BALANCED:
        sl_mult, t1_mult, t2_mult, t3_mult = 2.5, 3.0, 5.0, 7.5
    else:  # Aggressive / Watchlist(pre-entry) use wider swings
        sl_mult, t1_mult, t2_mult, t3_mult = 3.0, 4.0, 7.0, 11.0

    stop_loss = round(min(max(price - sl_mult * atr, technicals.nearest_support * 0.98, price * 0.85), price * 0.995), 2)
    # Targets must always sit strictly above CMP -- fair-value blending can
    # otherwise pull target_1 below price for stocks already above fair value.
    target_1 = round(max(min(price + t1_mult * atr, fair_value * 1.05), price * 1.02), 2)
    target_2 = round(max(price + t2_mult * atr, target_1 * 1.03), 2)
    target_3 = round(max(price + t3_mult * atr, fair_value * 1.15, target_2 * 1.03), 2)

    buy_low = round(min(price * 0.985, technicals.nearest_support * 1.02), 2)
    buy_high = round(price * 1.01, 2)

    return dict(stop_loss=stop_loss, target_1=target_1, target_2=target_2,
                target_3=target_3, buy_range_low=buy_low, buy_range_high=buy_high)


def classify_category(snapshot: MarketSnapshot, scores: ScoreBreakdown,
                       fair_value: float, thresholds: ClassificationThresholds = DEFAULT_THRESHOLDS,
                       disq_result: DisqualifierResult | None = None,
                       market_cap_category: str = "Mid") -> Category | None:
    """Returns None if the stock doesn't clear the minimum bar for any tab.

    Hard disqualifiers (core/disqualifiers.py) are checked BEFORE the
    weighted-score checklist below -- an analyst wouldn't call a stock
    "Conservative" just because its blended score is high if it also has
    negative equity or can't cover its interest payments. This is a
    separate, higher-priority gate, not another factor blended into the
    score."""
    if disq_result is None:
        disq_result = check_disqualifiers(snapshot, market_cap_category)

    if disq_result.insufficient_data:
        return None
    if disq_result.is_disqualified:
        return None
    if disq_result.aggressive_carveout_eligible:
        # Growth-stage stock burning cash by design (negative FCF) but
        # otherwise clean -- eligible ONLY for Aggressive, never
        # Conservative/Balanced/Watchlist, regardless of how the rest of
        # its scores look.
        return Category.AGGRESSIVE

    if scores.overall_ai_score < thresholds.min_overall_score:
        return None

    # Confidence gate: sub-scores must broadly agree. A blended 60 built
    # from fundamentals=85 and technicals=10 is a confused signal -- the
    # stock isn't ready, whatever the average says.
    if scores.confidence_score < thresholds.min_confidence_score:
        return None

    margin_of_safety_pct = (fair_value - snapshot.price) / fair_value * 100 if fair_value else 0

    # Watchlist: fundamentally strong (good fundamental+quality) but price
    # not yet in an attractive buy zone (small/no margin of safety, or
    # price has run up beyond fair value already).
    fundamentally_strong = scores.fundamental_score >= 55 and scores.quality_score >= 50
    not_yet_attractive = margin_of_safety_pct < thresholds.watchlist_min_margin_of_safety_pct
    if fundamentally_strong and not_yet_attractive and scores.overall_ai_score >= thresholds.min_overall_score:
        return Category.WATCHLIST

    if scores.risk_score >= thresholds.conservative_risk_min and scores.overall_ai_score >= thresholds.conservative_min_score:
        return Category.CONSERVATIVE
    if scores.overall_ai_score >= thresholds.balanced_min_score and scores.risk_score >= thresholds.balanced_risk_min:
        return Category.BALANCED
    return Category.AGGRESSIVE


def _build_explanation(snapshot: MarketSnapshot, scores: ScoreBreakdown,
                        technicals: TechnicalLevels, category: Category,
                        margin_of_safety_pct: float, news: NewsAnalysisResult,
                        disq_result: DisqualifierResult | None = None) -> str:
    parts = []
    if scores.fundamental_score >= 65:
        parts.append(f"strong fundamentals (ROE {snapshot.roe_pct}%, ROCE {snapshot.roce_pct}%)")
    elif scores.fundamental_score <= 40:
        parts.append("relatively weak fundamentals")

    # Profitability pillar -- surface the DuPont leverage flag if present,
    # and margin trend direction when notable.
    if scores.profitability_score >= 65 or scores.profitability_score <= 35:
        _, prof_facts = score_profitability_pillar(snapshot)
        leverage_flag = next((f for f in prof_facts if "leverage-assisted" in f), None)
        margin_fact = next((f for f in prof_facts if "margin" in f and "%" in f), None)
        if leverage_flag:
            parts.append(leverage_flag)  # always include the DuPont warning in full
        elif margin_fact:
            parts.append(margin_fact)

    if scores.valuation_score >= 65:
        parts.append(f"attractive valuation (PE {snapshot.pe_ratio} vs sector {snapshot.sector_avg_pe})")
    elif scores.valuation_score <= 35:
        parts.append("valuation looks stretched versus sector peers")

    parts.append(f"technical trend is {technicals.trend.lower()} (RSI {technicals.rsi})")

    if scores.growth_score >= 65:
        _, growth_facts = score_growth_pillar(snapshot)
        parts.append(f"strong, consistent growth ({growth_facts[0].lower()})")
    elif scores.growth_score <= 35:
        parts.append("growth is weak or inconsistent")

    if scores.cash_flow_quality_score <= 35:
        _, cfq_facts = score_cash_flow_quality_pillar(snapshot)
        ocf_fact = next((f for f in cfq_facts if "OCF/Net Income" in f), None)
        parts.append(f"cash flow quality is a concern{' (' + ocf_fact.lower() + ')' if ocf_fact else ''}")
    elif scores.cash_flow_quality_score >= 75:
        parts.append("cash flow quality is strong (profit is converting to real cash)")

    if snapshot.fii_holding_pct + snapshot.dii_holding_pct >= 25:
        parts.append("healthy institutional (FII/DII) participation")

    if snapshot.news_sentiment_score > 0.3:
        parts.append("positive recent news/analyst sentiment")
    elif snapshot.news_sentiment_score < -0.3:
        parts.append("cautious news/analyst sentiment")

    if scores.risk_score < 40:
        parts.append("elevated volatility/leverage raises risk")

    if news.article_count > 0 and news.sentiment_label != "Neutral":
        parts.append(f"recent news sentiment is {news.sentiment_label.lower()} "
                      f"({news.article_count} articles, impact: {news.overall_impact.lower()})")
    elif news.article_count == 0:
        parts.append("limited recent news coverage found")

    if margin_of_safety_pct > 10:
        parts.append(f"trading at a {margin_of_safety_pct:.0f}% discount to estimated fair value")
    elif margin_of_safety_pct < 0:
        parts.append("currently trading above estimated fair value")

    reason = "; ".join(parts).capitalize() + "."
    category_note = {
        Category.WATCHLIST: " Recommended to wait for a better entry near the ideal buy range.",
        Category.CONSERVATIVE: " Suitable for capital-preservation-focused, long-horizon investors.",
        Category.BALANCED: " Balanced risk-reward profile for medium-term investors.",
        Category.AGGRESSIVE: " Higher risk/reward; suited to investors comfortable with volatility.",
    }[category]

    disq_note = ""
    if disq_result and disq_result.aggressive_carveout_eligible:
        disq_note = (" NOTE: free cash flow has been negative for multiple consecutive years. "
                     "This is disclosed, not hidden -- it's why this stock is capped at Aggressive "
                     "regardless of its other scores, rather than appearing in Conservative/Balanced/"
                     "Watchlist. Typical of a growth-stage company reinvesting heavily; verify the "
                     "cash burn is funding genuine growth before treating this as a buy signal.")

    return reason + category_note + disq_note


def build_recommendation(snapshot: MarketSnapshot, technicals: TechnicalLevels,
                          scores: ScoreBreakdown, news: NewsAnalysisResult,
                          thresholds: ClassificationThresholds = DEFAULT_THRESHOLDS,
                          market_cap_category: str = "Mid",
                          rec_date: date | None = None) -> Recommendation | None:
    fair_value = _fair_value(snapshot)
    disq_result = check_disqualifiers(snapshot, market_cap_category, DEFAULT_DISQUALIFIER_THRESHOLDS)
    category = classify_category(snapshot, scores, fair_value, thresholds, disq_result, market_cap_category)
    if category is None:
        return None

    margin_of_safety_pct = round((fair_value - snapshot.price) / fair_value * 100, 1) if fair_value else 0.0
    tp = _targets_and_stoploss(snapshot, technicals, category, fair_value)
    risk_level = _risk_level(scores, technicals)
    horizon_label, holding_days = _investment_horizon(category, risk_level)

    expected_return_pct = round((tp["target_2"] - snapshot.price) / snapshot.price * 100, 1)
    risk_amount = max(snapshot.price - tp["stop_loss"], 0.01)
    reward_amount = tp["target_2"] - snapshot.price
    risk_reward_ratio = round(reward_amount / risk_amount, 2) if risk_amount else 0.0

    # Final quality gate: the trade itself must be structurally attractive.
    # A stock that scores well but has a poor setup (too close to its stop,
    # target barely above current price) gets dropped here, not promoted.
    if risk_reward_ratio < thresholds.min_risk_reward_ratio:
        return None

    dist_from_52w_high = round((snapshot.price - snapshot.week52_high) / snapshot.week52_high * 100, 1)
    dist_from_52w_low = round((snapshot.price - snapshot.week52_low) / snapshot.week52_low * 100, 1)

    explanation = _build_explanation(snapshot, scores, technicals, category, margin_of_safety_pct,
                                      news, disq_result)

    return Recommendation(
        symbol=snapshot.symbol,
        company_name=snapshot.company_name,
        sector=snapshot.sector,
        market_cap_category=market_cap_category,
        category=category,
        rank_in_category=0,  # filled in during ranking pass
        recommendation_date=rec_date or date.today(),
        current_price=snapshot.price,
        investment_horizon=horizon_label,
        buy_range_low=tp["buy_range_low"],
        buy_range_high=tp["buy_range_high"],
        stop_loss=tp["stop_loss"],
        target_1=tp["target_1"],
        target_2=tp["target_2"],
        target_3=tp["target_3"],
        fair_value=fair_value,
        margin_of_safety=margin_of_safety_pct,
        expected_return_pct=expected_return_pct,
        expected_holding_days=holding_days,
        scores=scores,
        risk_level=risk_level,
        risk_reward_ratio=risk_reward_ratio,
        technicals=technicals,
        week52_high=snapshot.week52_high,
        week52_low=snapshot.week52_low,
        all_time_high=snapshot.all_time_high,
        distance_from_52w_high_pct=dist_from_52w_high,
        distance_from_52w_low_pct=dist_from_52w_low,
        ai_explanation=explanation,
        news_sentiment_score=news.sentiment_score,
        news_sentiment_label=news.sentiment_label,
        news_overall_impact=news.overall_impact,
        news_summary=news.summary,
        news_headlines=news.top_headlines,
        news_positive_factors=news.positive_factors,
        news_negative_factors=news.negative_factors,
        news_event_tags=news.event_tags,
        news_article_count=news.article_count,
    )


def rank_and_trim(recommendations: list[Recommendation]) -> dict[Category, list[Recommendation]]:
    """Group by category, sort by overall AI score desc, cap at 25, assign ranks."""
    by_category: dict[Category, list[Recommendation]] = {c: [] for c in Category}
    for r in recommendations:
        by_category[r.category].append(r)

    for cat, recs in by_category.items():
        recs.sort(key=lambda r: r.scores.overall_ai_score, reverse=True)
        trimmed = recs[:MAX_PER_CATEGORY]
        for i, r in enumerate(trimmed, start=1):
            r.rank_in_category = i
        by_category[cat] = trimmed

    return by_category


# Color shading: rank 1 = darkest, rank 25 = lightest, per category theme.
CATEGORY_BASE_COLORS = {
    Category.CONSERVATIVE: (27, 94, 32),    # dark green RGB
    Category.BALANCED: (13, 71, 161),        # dark blue RGB
    Category.AGGRESSIVE: (191, 54, 12),      # dark orange/red RGB
    Category.WATCHLIST: (69, 39, 160),       # dark purple RGB (neutral accent)
}


def rank_to_color_hex(category: Category, rank: int, max_rank: int = MAX_PER_CATEGORY) -> str:
    """Interpolates from the dark base color (rank 1) to a light tint (rank max_rank)."""
    base_r, base_g, base_b = CATEGORY_BASE_COLORS[category]
    light_r, light_g, light_b = 235, 240, 235
    t = (rank - 1) / max(max_rank - 1, 1)
    r = round(base_r + (light_r - base_r) * t)
    g = round(base_g + (light_g - base_g) * t)
    b = round(base_b + (light_b - base_b) * t)
    return f"#{r:02x}{g:02x}{b:02x}"
