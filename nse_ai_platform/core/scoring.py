"""
core/scoring.py
AI Scoring Engine.

Phase 1 implementation is a transparent, tunable rule-based/statistical
scorer (weighted factor model) rather than a black-box ML model, so every
score is explainable out of the box -- which the spec requires (AI
Explanation with human-readable reasons). This is also the natural place
to later swap in a trained ML model or LLM-based scorer: the interface
(`score_stock`) stays the same, only the internals change.

All sub-scores are 0-100. Higher is always better/less risky, except for
risk_score's raw volatility component which we deliberately invert so
that "risk_score" reads as *risk-adjusted quality* (100 = very safe) to
match the "Risk Score" the platform displays alongside the others.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.models import MarketSnapshot, TechnicalLevels, ScoreBreakdown
from core.fundamental_analysis import score_growth_pillar, score_cash_flow_quality_pillar
from core.profitability import score_profitability_pillar
from core.financial_health import score_financial_health_pillar
from core.ownership import score_ownership_pillar
from core.earnings_quality import score_earnings_quality_pillar
from core.valuation import score_valuation_pillar
from news.models import NewsAnalysisResult

import logging

logger = logging.getLogger(__name__)


def _clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _scale(value: float, low: float, high: float) -> float:
    """Linearly map value in [low, high] to [0, 100], clipped at the ends."""
    if high == low:
        return 50.0
    return _clip((value - low) / (high - low) * 100)


@dataclass
class ScoringWeights:
    """Configurable weights -- tune thresholds here without touching logic.

    `sentiment` is the NEWS INFLUENCE knob: it controls how much the
    recency-weighted news sentiment score (from news/engine.py) moves the
    Overall AI Score. Raise it to make the AI react more to news; lower
    it (or set to 0) to make recommendations purely fundamentals/technicals
    driven. Default is a conservative 10% -- news is informative but
    shouldn't dominate a multi-factor score built mostly from financials
    and price action.
    """
    fundamental: float = 0.08
    technical: float = 0.15
    valuation: float = 0.12
    sentiment: float = 0.07
    quality: float = 0.08
    growth: float = 0.10
    cash_flow_quality: float = 0.10
    profitability: float = 0.10
    financial_health: float = 0.08
    ownership: float = 0.07
    earnings_quality: float = 0.05


DEFAULT_WEIGHTS = ScoringWeights()


def score_fundamentals(s: MarketSnapshot) -> float:
    roe = _scale(s.roe_pct, 5, 30)
    roce = _scale(s.roce_pct, 5, 32)
    debt = _scale(-s.debt_to_equity, -2.0, 0.0)  # lower D/E is better
    fcf_positive = 70 if s.free_cash_flow > 0 else 30
    promoter = _scale(s.promoter_holding_pct, 20, 70)
    institutional = _scale(s.fii_holding_pct + s.dii_holding_pct, 5, 50)
    return round(
        0.25 * roe + 0.20 * roce + 0.15 * debt + 0.15 * fcf_positive +
        0.15 * promoter + 0.10 * institutional, 1
    )


def score_growth(s: MarketSnapshot) -> float:
    """Delegates to core/fundamental_analysis.py's multi-year CAGR +
    consistency pillar (spec §2.1) -- replaces the old single-year
    revenueGrowth/earningsGrowth-only blend, which the spec calls out as
    noisy (one huge year can prop up an otherwise flat/declining trend)."""
    score, _facts = score_growth_pillar(s)
    return score


def score_cash_flow_quality(s: MarketSnapshot) -> float:
    """Delegates to core/fundamental_analysis.py's OCF/NI + capex
    intensity + FCF-vs-earnings-alignment pillar (spec §2.4)."""
    score, _facts = score_cash_flow_quality_pillar(s)
    return score


def score_profitability(s: MarketSnapshot) -> float:
    score, _facts = score_profitability_pillar(s)
    return score

def score_financial_health(s: MarketSnapshot) -> float:
    score, _facts = score_financial_health_pillar(s)
    return score

def score_ownership(s: MarketSnapshot) -> float:
    score, _facts = score_ownership_pillar(s)
    return score

def score_earnings_quality(s: MarketSnapshot) -> float:
    score, _facts = score_earnings_quality_pillar(s)
    return score


def score_valuation(s: MarketSnapshot) -> float:
    score, _verdict, _facts = score_valuation_pillar(s)
    return score


def score_technical(t: TechnicalLevels, price: float) -> float:
    rsi_score = 100 - abs(t.rsi - 55) * 1.4  # sweet spot ~ mildly bullish RSI
    rsi_score = _clip(rsi_score)
    macd_score = 70 if t.macd > t.macd_signal else 30
    trend_score = {"Uptrend": 85, "Sideways": 50, "Downtrend": 20}[t.trend]
    above_vwap = 65 if price > t.vwap else 35
    adx_score = _scale(t.adx, 10, 40)  # stronger trend (either direction) = more conviction
    return round(
        0.30 * rsi_score + 0.20 * macd_score + 0.25 * trend_score +
        0.10 * above_vwap + 0.15 * adx_score, 1
    )


def score_sentiment(news: NewsAnalysisResult) -> float:
    """Scales the real, recency-weighted news sentiment (-1..1, from
    news/engine.py) to a 0-100 sub-score. Zero/neutral news maps to 50
    (a neutral midpoint) rather than penalizing stocks with no coverage."""
    return round(_scale(news.sentiment_score, -1, 1), 1)


def score_quality(s: MarketSnapshot) -> float:
    """Governance/quality proxy: promoter holding + low leverage + consistent ROE."""
    governance = _scale(s.promoter_holding_pct, 15, 65)
    leverage = _scale(-s.debt_to_equity, -2.0, 0.0)
    roe_consistency = _scale(s.roe_pct, 8, 28)
    return round(0.4 * governance + 0.3 * leverage + 0.3 * roe_consistency, 1)


def score_risk(s: MarketSnapshot, t: TechnicalLevels) -> float:
    """Risk score expressed as safety (100 = low risk)."""
    volatility_penalty = _scale(-t.atr / max(s.price, 1) * 100, -8, -0.5)  # ATR as % of price
    leverage_penalty = _scale(-s.debt_to_equity, -2.5, 0.0)
    liquidity_score = _scale(s.volume, 1e5, 5e6)
    drawdown_from_high = (s.week52_high - s.price) / s.week52_high * 100 if s.week52_high else 0
    drawdown_penalty = _scale(-drawdown_from_high, -60, 0)
    return round(
        0.35 * volatility_penalty + 0.25 * leverage_penalty +
        0.20 * liquidity_score + 0.20 * drawdown_penalty, 1
    )


def score_confidence(sub_scores: list[float]) -> float:
    """
    Confidence reflects agreement across factors: if fundamental, technical,
    valuation, sentiment all point the same direction, confidence is high;
    if they conflict (high variance), confidence is lower.
    """
    if not sub_scores:
        return 50.0
    mean = sum(sub_scores) / len(sub_scores)
    variance = sum((x - mean) ** 2 for x in sub_scores) / len(sub_scores)
    spread_penalty = min(variance ** 0.5, 35)  # stdev, capped
    return round(_clip(85 - spread_penalty), 1)


def score_stock(snapshot: MarketSnapshot, technicals: TechnicalLevels, news: NewsAnalysisResult,
                 weights: ScoringWeights = DEFAULT_WEIGHTS) -> ScoreBreakdown:
    fundamental = score_fundamentals(snapshot)
    technical = score_technical(technicals, snapshot.price)
    valuation = score_valuation(snapshot)
    sentiment = score_sentiment(news)
    quality = score_quality(snapshot)
    growth = score_growth(snapshot)
    cash_flow_quality = score_cash_flow_quality(snapshot)
    profitability = score_profitability(snapshot)
    financial_health = score_financial_health(snapshot)
    ownership = score_ownership(snapshot)
    earnings_quality = score_earnings_quality(snapshot)
    risk = score_risk(snapshot, technicals)

    overall = round(
        weights.fundamental * fundamental
        + weights.technical * technical
        + weights.valuation * valuation
        + weights.sentiment * sentiment
        + weights.quality * quality
        + weights.growth * growth
        + weights.cash_flow_quality * cash_flow_quality
        + weights.profitability * profitability
        + weights.financial_health * financial_health
        + weights.ownership * ownership
        + weights.earnings_quality * earnings_quality, 1
    )
    risk_adjustment = (risk - 50) * 0.08
    overall = round(_clip(overall + risk_adjustment), 1)
    confidence = score_confidence([fundamental, technical, valuation, sentiment,
                                    growth, profitability, financial_health])

    return ScoreBreakdown(
        fundamental_score=fundamental, technical_score=technical,
        valuation_score=valuation, sentiment_score=sentiment, risk_score=risk,
        quality_score=quality, growth_score=growth,
        cash_flow_quality_score=cash_flow_quality, profitability_score=profitability,
        financial_health_score=financial_health, ownership_score=ownership,
        earnings_quality_score=earnings_quality,
        overall_ai_score=overall, confidence_score=confidence,
    )
