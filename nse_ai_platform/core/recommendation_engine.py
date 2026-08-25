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
from core.classifier import classify, ClassificationThresholds, ChecklistResult, DEFAULT_THRESHOLDS, MAX_PER_CATEGORY
from core.valuation import score_valuation_pillar, UNDERVALUED, OVERVALUED
from core.financial_health import score_financial_health_pillar
from core.ownership import score_ownership_pillar
from core.earnings_quality import score_earnings_quality_pillar
from core.fundamental_analysis import score_growth_pillar, score_cash_flow_quality_pillar
from core.profitability import score_profitability_pillar
from news.models import NewsAnalysisResult

logger = logging.getLogger(__name__)

# These are now imported from core/classifier.py
# MAX_PER_CATEGORY, ClassificationThresholds, DEFAULT_THRESHOLDS are re-exported for backward compat
# with debug_scores.py and other callers that import them from here.

def classify_category(snapshot, scores, fair_value, thresholds=None, disq_result=None,
                       market_cap_category="Mid", risk_reward_ratio=0.0):
    """Wrapper delegating to core/classifier.py checklist. Returns Category or None."""
    if thresholds is None:
        thresholds = DEFAULT_THRESHOLDS
    if disq_result is None:
        disq_result = check_disqualifiers(snapshot, market_cap_category)
    result = classify(snapshot, scores, fair_value, disq_result, risk_reward_ratio, thresholds)
    return result.category


def _build_explanation(snapshot, scores, technicals, category,
                        margin_of_safety_pct, news, disq_result=None, checklist=None):
    parts = []
    # §2.2 DuPont leverage flag
    if scores.profitability_score <= 35 or scores.profitability_score >= 65:
        from core.profitability import score_profitability_pillar
        _, pf = score_profitability_pillar(snapshot)
        lf = next((f for f in pf if "leverage-assisted" in f), None)
        mf = next((f for f in pf if "margin" in f and "%" in f), None)
        if lf: parts.append(lf)
        elif mf: parts.append(mf)
    # §2.3 Financial Health
    if scores.financial_health_score <= 35: parts.append("balance sheet shows stress")
    elif scores.financial_health_score >= 70: parts.append("strong balance sheet")
    # §2.1 Growth
    if scores.growth_score >= 65:
        from core.fundamental_analysis import score_growth_pillar
        _, gf = score_growth_pillar(snapshot)
        parts.append(f"strong consistent growth ({gf[0].lower()})")
    elif scores.growth_score <= 35: parts.append("growth weak or inconsistent")
    # §2.4 Cash Flow Quality
    if scores.cash_flow_quality_score <= 35:
        from core.fundamental_analysis import score_cash_flow_quality_pillar
        _, cfq = score_cash_flow_quality_pillar(snapshot)
        of = next((f for f in cfq if "OCF" in f), None)
        parts.append(f"cash flow quality concern{' ('+of.lower()+')' if of else ''}")
    elif scores.cash_flow_quality_score >= 75:
        parts.append("profit converting to cash flow")
    # §2.5 Valuation
    _, verdict, vf = score_valuation_pillar(snapshot)
    if verdict in (UNDERVALUED, OVERVALUED):
        parts.append(f"valuation {verdict.lower()} ({vf[0] if vf else ''})")
    # Technical
    parts.append(f"technical trend {technicals.trend.lower()} (RSI {technicals.rsi})")
    # §2.6 Ownership
    if scores.ownership_score >= 70: parts.append(f"promoter {snapshot.promoter_holding_pct:.1f}% (healthy)")
    elif scores.ownership_score <= 35: parts.append("ownership trend concern")
    # §2.7 Earnings quality
    if scores.earnings_quality_score >= 70: parts.append("consistent earnings beats")
    elif scores.earnings_quality_score <= 30: parts.append("weak analyst/earnings track record")
    # News
    if news.article_count > 0 and news.sentiment_label != "Neutral":
        parts.append(f"news {news.sentiment_label.lower()} ({news.article_count} articles)")
    if margin_of_safety_pct > 10: parts.append(f"trading {margin_of_safety_pct:.0f}% below fair value")
    elif margin_of_safety_pct < 0: parts.append("above estimated fair value")

    reason = "; ".join(parts).capitalize() + "."
    cat_note = {
        Category.WATCHLIST:    " Wait for price to reach the ideal buy range.",
        Category.CONSERVATIVE: " Meets Conservative checklist — long-term capital preservation.",
        Category.BALANCED:     " Meets Balanced checklist — medium-term risk/reward.",
        Category.AGGRESSIVE:   " Higher risk/reward; verify thesis before acting.",
    }[category]
    disq_note = ""
    if disq_result and disq_result.aggressive_carveout_eligible:
        disq_note = " ⚠ FCF negative for multiple years — growth-stage, capped at Aggressive."
    return reason + cat_note + disq_note



def _fair_value(snapshot) -> float:
    """Simple fair-value estimate: blended PE/PB/Graham-number approach."""
    from core.models import MarketSnapshot
    price = snapshot.price
    eps = snapshot.eps or 0
    pe = snapshot.pe_ratio or snapshot.sector_avg_pe or 20
    bvps = snapshot.pb_ratio and price / snapshot.pb_ratio or 0
    pe_fv = eps * min(pe, snapshot.sector_avg_pe or pe) if eps > 0 else 0
    graham = (22.5 * eps * bvps) ** 0.5 if eps > 0 and bvps > 0 else 0
    candidates = [v for v in [pe_fv, graham] if v > 0]
    return round(sum(candidates) / len(candidates), 2) if candidates else price


def _targets_and_stoploss(snapshot, technicals, category, fair_value: float) -> dict:
    from core.models import Category as Cat, RiskLevel
    price = snapshot.price
    atr = max(price * 0.02, 1)  # fallback ATR approximation
    try:
        atr = float(technicals.pivot_point * 0.02) if technicals.pivot_point else price * 0.02
    except Exception:
        pass
    mult = {Cat.CONSERVATIVE: (0.06, 0.10, 0.15, 0.03),
            Cat.WATCHLIST:     (0.06, 0.10, 0.15, 0.03),
            Cat.BALANCED:      (0.08, 0.14, 0.20, 0.05),
            Cat.AGGRESSIVE:    (0.12, 0.20, 0.30, 0.07)}.get(category,
                                (0.08, 0.14, 0.20, 0.05))
    t1_mult, t2_mult, t3_mult, sl_mult = mult
    fv_target = max(fair_value, price * 1.05) if fair_value and fair_value > price else price * (1 + t2_mult)
    return {
        "buy_range_low":  round(price * 0.97, 2),
        "buy_range_high": round(price, 2),
        "stop_loss":      round(price * (1 - sl_mult), 2),
        "target_1":       round(price * (1 + t1_mult), 2),
        "target_2":       round(fv_target, 2),
        "target_3":       round(price * (1 + t3_mult), 2),
    }


def _risk_level(scores, technicals):
    from core.models import RiskLevel
    if scores.risk_score >= 65 and scores.overall_ai_score >= 60:
        return RiskLevel.LOW
    if scores.risk_score <= 35 or scores.overall_ai_score <= 45:
        return RiskLevel.HIGH
    return RiskLevel.MEDIUM


def _investment_horizon(category, risk_level):
    from core.models import Category as Cat, RiskLevel
    map_ = {
        (Cat.CONSERVATIVE, RiskLevel.LOW):    ("12-24 months", 365),
        (Cat.CONSERVATIVE, RiskLevel.MEDIUM): ("9-18 months",  270),
        (Cat.CONSERVATIVE, RiskLevel.HIGH):   ("6-12 months",  180),
        (Cat.BALANCED, RiskLevel.LOW):        ("6-12 months",  240),
        (Cat.BALANCED, RiskLevel.MEDIUM):     ("3-9 months",   180),
        (Cat.BALANCED, RiskLevel.HIGH):       ("3-6 months",   120),
        (Cat.AGGRESSIVE, RiskLevel.LOW):      ("3-6 months",   120),
        (Cat.AGGRESSIVE, RiskLevel.MEDIUM):   ("1-3 months",    60),
        (Cat.AGGRESSIVE, RiskLevel.HIGH):     ("1-2 months",    45),
        (Cat.WATCHLIST, RiskLevel.LOW):       ("Watch: 1-3 months", 60),
        (Cat.WATCHLIST, RiskLevel.MEDIUM):    ("Watch: 1-3 months", 60),
        (Cat.WATCHLIST, RiskLevel.HIGH):      ("Watch: 1-3 months", 60),
    }
    return map_.get((category, risk_level), ("3-6 months", 120))

def build_recommendation(snapshot: MarketSnapshot, technicals: TechnicalLevels,
                          scores: ScoreBreakdown, news: NewsAnalysisResult,
                          thresholds: ClassificationThresholds = DEFAULT_THRESHOLDS,
                          market_cap_category: str = "Mid",
                          rec_date: date | None = None) -> Recommendation | None:
    fair_value = _fair_value(snapshot)
    disq_result = check_disqualifiers(snapshot, market_cap_category, DEFAULT_DISQUALIFIER_THRESHOLDS)
    # Compute preliminary targets to get risk:reward for the classifier
    tp_prelim = _targets_and_stoploss(snapshot, technicals, Category.BALANCED, fair_value)
    risk_amount_p = max(snapshot.price - tp_prelim["stop_loss"], 0.01)
    rr_prelim = round((tp_prelim["target_2"] - snapshot.price) / risk_amount_p, 2)
    # Run checklist classifier with real risk:reward
    checklist_result = classify(snapshot, scores, fair_value, disq_result, rr_prelim, thresholds)
    category = checklist_result.category
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
    dist_from_52w_high = round((snapshot.price - snapshot.week52_high) / snapshot.week52_high * 100, 1)
    dist_from_52w_low = round((snapshot.price - snapshot.week52_low) / snapshot.week52_low * 100, 1)
    explanation = _build_explanation(snapshot, scores, technicals, category, margin_of_safety_pct,
                                      news, disq_result, checklist_result)

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
        price_history=snapshot.price_history[-200:],
        high_history=snapshot.high_history[-200:],
        low_history=snapshot.low_history[-200:],
        volume_history=snapshot.volume_history[-200:],
    )


LARGE_CAP_THRESHOLD_RUPEES = 70_000 * 1e7
MID_CAP_THRESHOLD_RUPEES  = 15_000 * 1e7

def classify_market_cap(market_cap_rupees: float) -> str:
    if market_cap_rupees >= LARGE_CAP_THRESHOLD_RUPEES: return "Large"
    if market_cap_rupees >= MID_CAP_THRESHOLD_RUPEES:  return "Mid"
    return "Small"


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
