"""
core/models.py
Typed dataclasses shared across the pipeline. Keeping these separate from
the DB layer means the scoring/recommendation engines never need to know
about SQL, and the DB layer never needs to know about scoring logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


class Category(str, Enum):
    WATCHLIST = "Watchlist"
    CONSERVATIVE = "Conservative"
    BALANCED = "Balanced"
    AGGRESSIVE = "Aggressive"


class RiskLevel(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class TradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class ExitReason(str, Enum):
    TARGET1 = "TARGET1_HIT"
    TARGET2 = "TARGET2_HIT"
    TARGET3 = "TARGET3_HIT"
    STOPLOSS = "STOPLOSS_HIT"
    TIME_EXIT = "TIME_EXIT"
    MANUAL = "MANUAL"


@dataclass
class MarketSnapshot:
    """One point-in-time market data pull for a symbol."""
    symbol: str
    company_name: str
    exchange: str
    price: float
    open: float
    high: float
    low: float
    prev_close: float
    volume: int
    week52_high: float
    week52_low: float
    all_time_high: float
    market_cap: float
    sector: str
    industry: str
    # Fundamentals
    revenue_growth_pct: float = 0.0
    profit_growth_pct: float = 0.0
    eps: float = 0.0
    roe_pct: float = 0.0
    roce_pct: float = 0.0
    debt_to_equity: float = 0.0
    free_cash_flow: float = 0.0
    promoter_holding_pct: float = 0.0
    fii_holding_pct: float = 0.0
    dii_holding_pct: float = 0.0
    dividend_yield_pct: float = 0.0
    # Valuation
    pe_ratio: float = 0.0
    pb_ratio: float = 0.0
    peg_ratio: float = 0.0
    ev_ebitda: float = 0.0
    sector_avg_pe: float = 0.0
    # Historical prices for technical calc (most recent last)
    price_history: list = field(default_factory=list)
    volume_history: list = field(default_factory=list)
    high_history: list = field(default_factory=list)  # real daily highs, if the provider has them
    low_history: list = field(default_factory=list)   # real daily lows, if the provider has them

    # --- Deep fundamentals ---
    total_equity: float | None = None
    total_assets: float | None = None
    interest_coverage: float | None = None
    ocf_to_ni_history: list = field(default_factory=list)
    revenue_history_annual: list = field(default_factory=list)
    earnings_history_annual: list = field(default_factory=list)
    fcf_history_annual: list = field(default_factory=list)
    capex_history_annual: list = field(default_factory=list)
    ocf_history_annual: list = field(default_factory=list)

    # §2.3 Financial Health
    current_ratio: float | None = None
    quick_ratio: float | None = None
    total_debt_history: list = field(default_factory=list)       # annual, oldest→newest (net-debt trend)
    current_assets_history: list = field(default_factory=list)   # annual, oldest→newest (working capital trend)
    current_liab_history: list = field(default_factory=list)     # annual, oldest→newest
    beta: float | None = None

    # §2.5 Valuation cross-sectional (populated by core/valuation.py after the universe pass)
    sector_median_pe: float | None = None
    sector_median_pb: float | None = None
    sector_median_ev_ebitda: float | None = None
    sector_median_fcf_yield: float | None = None
    analyst_target_mean: float | None = None
    analyst_target_high: float | None = None
    analyst_target_low: float | None = None
    analyst_recommendation_mean: float | None = None   # 1=Strong Buy … 5=Strong Sell
    analyst_count: int = 0

    # §2.6 Ownership & Governance
    promoter_holding_pct_prev: float | None = None   # prior-quarter snapshot for trend
    institutional_pct_prev: float | None = None
    shares_short_ratio: float | None = None

    # §2.7 Earnings Quality & Analyst Sentiment
    eps_beat_miss_history: list = field(default_factory=list)  # +1 beat / 0 meet / -1 miss, newest last
    upgrades_90d: int = 0
    downgrades_90d: int = 0
    earnings_date_str: str = ""

    is_financial_sector: bool = False
    deep_fields_available: int = 0
    deep_fields_total: int = 0

    # Legacy news sentiment (from MarketSnapshot phase — kept for backward compat)
    news_sentiment_score: float = 0.0
    analyst_rating_score: float = 0.0


@dataclass
class TechnicalLevels:
    pivot_point: float
    s1: float
    s2: float
    s3: float
    r1: float
    r2: float
    r3: float
    breakout_level: float
    breakdown_level: float
    nearest_support: float
    nearest_resistance: float
    rsi: float
    macd: float
    macd_signal: float
    sma_50: float
    sma_200: float
    ema_20: float
    vwap: float
    adx: float
    atr: float
    trend: str  # "Uptrend"/"Downtrend"/"Sideways"


@dataclass
class ScoreBreakdown:
    fundamental_score: float
    technical_score: float
    valuation_score: float
    sentiment_score: float
    risk_score: float
    quality_score: float
    growth_score: float
    cash_flow_quality_score: float
    profitability_score: float
    financial_health_score: float
    ownership_score: float
    earnings_quality_score: float
    overall_ai_score: float
    confidence_score: float


@dataclass
class Recommendation:
    symbol: str
    company_name: str
    sector: str
    market_cap_category: str
    category: Category
    rank_in_category: int
    recommendation_date: date
    current_price: float
    investment_horizon: str
    buy_range_low: float
    buy_range_high: float
    stop_loss: float
    target_1: float
    target_2: float
    target_3: float
    fair_value: float
    margin_of_safety: float
    expected_return_pct: float
    expected_holding_days: int
    scores: ScoreBreakdown
    risk_level: RiskLevel
    risk_reward_ratio: float
    technicals: TechnicalLevels
    week52_high: float
    week52_low: float
    all_time_high: float
    distance_from_52w_high_pct: float
    distance_from_52w_low_pct: float
    ai_explanation: str
    news_sentiment_score: float = 0.0
    news_sentiment_label: str = "Neutral"
    news_overall_impact: str = "Neutral"
    news_summary: str = ""
    news_headlines: list = field(default_factory=list)
    news_positive_factors: list = field(default_factory=list)
    news_negative_factors: list = field(default_factory=list)
    news_event_tags: list = field(default_factory=list)
    news_article_count: int = 0
    price_history: list = field(default_factory=list)
    high_history: list = field(default_factory=list)
    low_history: list = field(default_factory=list)
    volume_history: list = field(default_factory=list)
    recommendation_id: Optional[int] = None
    recommendation_version: int = 1
    parent_recommendation_id: Optional[int] = None
