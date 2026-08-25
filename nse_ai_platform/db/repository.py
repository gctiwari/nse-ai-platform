"""
db/repository.py
Persistence layer between the core engines (which speak dataclasses) and
SQLite. This is the only place that translates Recommendation objects
into rows and back.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime  # noqa: F401 (datetime used below via .now())

from db.schema import DatabaseManager
from core.models import Recommendation

logger = logging.getLogger(__name__)


class RecommendationRepository:
    def __init__(self, db: DatabaseManager):
        self.db = db

    def has_recommendation_today(self, symbol: str, rec_date: str) -> bool:
        """Guards against duplicate recommendations/trades if the app is
        launched more than once on the same day."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM recommendation WHERE symbol=? AND recommendation_date=? LIMIT 1",
                (symbol, rec_date),
            ).fetchone()
            return row is not None

    def get_open_trade_for_symbol(self, symbol: str) -> dict | None:
        """Returns the currently open paper trade for a symbol, or None.
        Used to decide whether to open a new trade or refresh an existing one
        when a stock is re-recommended on a later day."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_trade WHERE symbol=? AND status='OPEN' "
                "ORDER BY opened_at DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            return dict(row) if row else None

    def upsert_watchlist_monitor(self, symbol: str, company_name: str, sector: str,
                                  recommendation_id: int, recommendation_date: str,
                                  current_price: float, buy_range_low: float,
                                  buy_range_high: float, fair_value: float,
                                  overall_ai_score: float) -> None:
        """Upsert the watchlist monitor record for a symbol. Updates on every
        run so the price and proximity figures are always current."""
        pct_away = (current_price - buy_range_high) / buy_range_high * 100 if buy_range_high else 0
        status = "ENTERED" if current_price <= buy_range_high else "WATCHING"
        from datetime import datetime as _dt
        now = _dt.now().isoformat()
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO watchlist_monitor
                   (symbol, company_name, sector, recommendation_id, recommendation_date,
                    current_price, buy_range_low, buy_range_high, fair_value,
                    pct_away_from_entry, overall_ai_score, status, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET
                     company_name=excluded.company_name, sector=excluded.sector,
                     recommendation_id=excluded.recommendation_id,
                     recommendation_date=excluded.recommendation_date,
                     current_price=excluded.current_price, buy_range_low=excluded.buy_range_low,
                     buy_range_high=excluded.buy_range_high, fair_value=excluded.fair_value,
                     pct_away_from_entry=excluded.pct_away_from_entry,
                     overall_ai_score=excluded.overall_ai_score,
                     status=excluded.status, updated_at=excluded.updated_at""",
                (symbol, company_name, sector, recommendation_id, recommendation_date,
                 current_price, buy_range_low, buy_range_high, fair_value,
                 round(pct_away, 2), overall_ai_score, status, now),
            )

    def get_watchlist_monitors(self) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM watchlist_monitor ORDER BY pct_away_from_entry ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_latest_version(self, symbol: str) -> int:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT MAX(recommendation_version) as v FROM recommendation WHERE symbol=?",
                (symbol,),
            ).fetchone()
            return (row["v"] or 0)

    def save(self, rec: Recommendation) -> int:
        """Insert a new recommendation row (never overwrites history)."""
        version = self.get_latest_version(rec.symbol) + 1
        now = datetime.now().isoformat()
        t = rec.technicals
        s = rec.scores
        with self.db.connect() as conn:
            cur = conn.execute(
                """INSERT INTO recommendation (
                    recommendation_version, symbol, company_name, sector,
                    market_cap_category, category, rank_in_category, recommendation_date,
                    current_price, investment_horizon, buy_range_low, buy_range_high,
                    stop_loss, target_1, target_2, target_3, fair_value, margin_of_safety,
                    expected_return_pct, expected_holding_days, confidence_score,
                    overall_ai_score, fundamental_score, technical_score, valuation_score,
                    sentiment_score, risk_score, quality_score, growth_score,
                    cash_flow_quality_score, profitability_score,
                    financial_health_score, ownership_score, earnings_quality_score,
                    risk_level,
                    risk_reward_ratio, pivot_point, s1, s2, s3, r1, r2, r3,
                    breakout_level, breakdown_level, nearest_support, nearest_resistance,
                    week52_high, week52_low, all_time_high,
                    distance_from_52w_high_pct, distance_from_52w_low_pct,
                    ai_explanation, price_history, high_history, low_history, volume_history, news_sentiment_score, news_sentiment_label,
                    news_overall_impact, news_summary, news_headlines,
                    news_positive_factors, news_negative_factors, news_event_tags,
                    news_article_count, created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    version, rec.symbol, rec.company_name, rec.sector,
                    rec.market_cap_category, rec.category.value, rec.rank_in_category,
                    rec.recommendation_date.isoformat(), rec.current_price,
                    rec.investment_horizon, rec.buy_range_low, rec.buy_range_high,
                    rec.stop_loss, rec.target_1, rec.target_2, rec.target_3,
                    rec.fair_value, rec.margin_of_safety, rec.expected_return_pct,
                    rec.expected_holding_days, s.confidence_score, s.overall_ai_score,
                    s.fundamental_score, s.technical_score, s.valuation_score,
                    s.sentiment_score, s.risk_score, s.quality_score, s.growth_score,
                    s.cash_flow_quality_score, s.profitability_score,
                    s.financial_health_score, s.ownership_score, s.earnings_quality_score,
                    rec.risk_level.value, rec.risk_reward_ratio,
                    t.pivot_point, t.s1, t.s2, t.s3, t.r1, t.r2, t.r3,
                    t.breakout_level, t.breakdown_level, t.nearest_support, t.nearest_resistance,
                    rec.week52_high, rec.week52_low, rec.all_time_high,
                    rec.distance_from_52w_high_pct, rec.distance_from_52w_low_pct,
                    rec.ai_explanation,
                    json.dumps(rec.price_history), json.dumps(rec.high_history),
                    json.dumps(rec.low_history), json.dumps(rec.volume_history),
                    rec.news_sentiment_score, rec.news_sentiment_label,
                    rec.news_overall_impact, rec.news_summary, json.dumps(rec.news_headlines),
                    json.dumps(rec.news_positive_factors), json.dumps(rec.news_negative_factors),
                    json.dumps(rec.news_event_tags), rec.news_article_count, now,
                ),
            )
            rec_id = cur.lastrowid
        rec.recommendation_id = rec_id
        rec.recommendation_version = version
        return rec_id

    def get_by_category(self, category: str) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM recommendation
                   WHERE category=? AND recommendation_id IN (
                       SELECT MAX(recommendation_id) FROM recommendation
                       WHERE category=? GROUP BY symbol
                   )
                   ORDER BY rank_in_category ASC""",
                (category, category),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_top_overall(self, limit: int = 10) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM recommendation
                   WHERE recommendation_id IN (
                       SELECT MAX(recommendation_id) FROM recommendation GROUP BY symbol
                   )
                   ORDER BY overall_ai_score DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
