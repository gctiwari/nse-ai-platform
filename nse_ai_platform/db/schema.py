"""
db/schema.py
Database schema definitions and connection management for the AI Stock
Recommendation & Paper Trading Platform.

Design goals:
- Single SQLite file, never lose history (append-only recommendation log,
  paper trades reference recommendations by ID, never overwritten).
- All writes go through DatabaseManager so we have one place to add
  transactions / logging / migrations later.
"""

from __future__ import annotations

import sqlite3
import logging
from pathlib import Path
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data_store" / "platform.db"

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

-- Raw/normalized daily market snapshot per stock, per fetch run.
CREATE TABLE IF NOT EXISTS market_snapshot (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    exchange        TEXT NOT NULL DEFAULT 'NSE',
    fetched_at      TEXT NOT NULL,             -- ISO timestamp
    price           REAL NOT NULL,
    open            REAL,
    high            REAL,
    low             REAL,
    prev_close      REAL,
    volume          INTEGER,
    week52_high     REAL,
    week52_low      REAL,
    all_time_high   REAL,
    market_cap      REAL,
    sector          TEXT,
    industry        TEXT,
    raw_json        TEXT                       -- full payload for future-proofing
);
CREATE INDEX IF NOT EXISTS idx_snapshot_symbol ON market_snapshot(symbol, fetched_at);

-- One row per AI recommendation ever generated. Never updated/deleted;
-- if the AI re-evaluates a stock, a NEW row is created (recommendation_version
-- links back via parent_recommendation_id) so history is fully preserved.
CREATE TABLE IF NOT EXISTS recommendation (
    recommendation_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_recommendation_id INTEGER REFERENCES recommendation(recommendation_id),
    recommendation_version  INTEGER NOT NULL DEFAULT 1,
    symbol                  TEXT NOT NULL,
    company_name            TEXT NOT NULL,
    sector                  TEXT,
    market_cap_category     TEXT,               -- Large/Mid/Small
    category                TEXT NOT NULL,       -- Watchlist/Conservative/Balanced/Aggressive
    rank_in_category        INTEGER,
    recommendation_date     TEXT NOT NULL,       -- ISO date
    current_price           REAL NOT NULL,
    investment_horizon      TEXT,
    buy_range_low            REAL,
    buy_range_high           REAL,
    stop_loss                REAL,
    target_1                 REAL,
    target_2                 REAL,
    target_3                 REAL,
    fair_value                REAL,
    margin_of_safety          REAL,
    expected_return_pct       REAL,
    expected_holding_days      INTEGER,
    confidence_score            REAL,
    overall_ai_score             REAL,
    fundamental_score            REAL,
    technical_score               REAL,
    valuation_score                REAL,
    sentiment_score                 REAL,
    risk_score                       REAL,
    quality_score                     REAL,
    growth_score                       REAL,
    cash_flow_quality_score            REAL,
    profitability_score                REAL,
    financial_health_score             REAL,
    ownership_score                    REAL,
    earnings_quality_score             REAL,
    risk_level                          TEXT,   -- Low/Medium/High
    risk_reward_ratio                    REAL,
    pivot_point REAL, s1 REAL, s2 REAL, s3 REAL,
    r1 REAL, r2 REAL, r3 REAL,
    breakout_level REAL, breakdown_level REAL,
    nearest_support REAL, nearest_resistance REAL,
    week52_high REAL, week52_low REAL, all_time_high REAL,
    distance_from_52w_high_pct REAL, distance_from_52w_low_pct REAL,
    ai_explanation TEXT,
    price_history  TEXT,
    high_history   TEXT,
    low_history    TEXT,
    volume_history TEXT,
    news_sentiment_score REAL,                  -- -1..1, recency-weighted
    news_sentiment_label TEXT,                  -- Positive/Neutral/Negative
    news_overall_impact TEXT,                   -- Bullish/Neutral/Bearish
    news_summary TEXT,                          -- one-paragraph AI-generated summary
    news_headlines TEXT,                        -- JSON list of top 3-5 headlines
    news_positive_factors TEXT,                 -- JSON list
    news_negative_factors TEXT,                 -- JSON list
    news_event_tags TEXT,                       -- JSON list of detected corporate events
    news_article_count INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reco_symbol ON recommendation(symbol);
CREATE INDEX IF NOT EXISTS idx_reco_category ON recommendation(category, recommendation_date);

-- Paper trades: exactly one open trade auto-created per recommendation.
CREATE TABLE IF NOT EXISTS paper_trade (
    trade_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id   INTEGER NOT NULL REFERENCES recommendation(recommendation_id),
    symbol               TEXT NOT NULL,
    category              TEXT NOT NULL,
    recommendation_date    TEXT NOT NULL,
    buy_price                REAL NOT NULL,
    current_price              REAL NOT NULL,
    stop_loss                    REAL,
    target_1 REAL, target_2 REAL, target_3 REAL,
    quantity                       INTEGER NOT NULL DEFAULT 1,
    status                          TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN/CLOSED
    days_held                        INTEGER NOT NULL DEFAULT 0,
    return_pct                        REAL NOT NULL DEFAULT 0,
    max_gain_pct                       REAL NOT NULL DEFAULT 0,
    max_loss_pct                        REAL NOT NULL DEFAULT 0,
    exit_price                           REAL,
    exit_date                             TEXT,
    exit_reason                            TEXT,   -- TARGET1_HIT/STOPLOSS_HIT/TIME_EXIT/MANUAL
    recommendation_version                  INTEGER NOT NULL DEFAULT 1,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trade_status ON paper_trade(status);
CREATE INDEX IF NOT EXISTS idx_trade_symbol ON paper_trade(symbol);

-- Daily snapshot log per paper trade -- one row per trade per calendar
-- day it was touched by a pipeline run. This is separate from the
-- paper_trade table itself (which only holds the LATEST state per trade)
-- so day-by-day price/return history is never lost, however many times
-- the app is run per day or across many days.
CREATE TABLE IF NOT EXISTS paper_trade_history (
    trade_id        INTEGER NOT NULL REFERENCES paper_trade(trade_id),
    snapshot_date   TEXT NOT NULL,
    price           REAL NOT NULL,
    return_pct      REAL NOT NULL,
    days_held       INTEGER NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (trade_id, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_trade_history_trade ON paper_trade_history(trade_id, snapshot_date);

-- Sector-level cross-sectional stats computed once per run from the whole
-- universe (§2.5). Keyed by (sector, run_date) so the history is kept.
CREATE TABLE IF NOT EXISTS sector_stats (
    sector          TEXT NOT NULL,
    run_date        TEXT NOT NULL,
    median_pe       REAL, median_pb REAL, median_ev_ebitda REAL,
    median_fcf_yield REAL, median_roe REAL, median_beta REAL,
    stock_count     INTEGER,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (sector, run_date)
);

-- Per-symbol ownership snapshot per run (§2.6). Append-only so trend is
-- computable by comparing consecutive rows for the same symbol.
CREATE TABLE IF NOT EXISTS ownership_snapshot (
    symbol               TEXT NOT NULL,
    snapshot_date        TEXT NOT NULL,
    promoter_holding_pct REAL,
    institutional_pct    REAL,
    shares_short_ratio   REAL,
    created_at           TEXT NOT NULL,
    PRIMARY KEY (symbol, snapshot_date)
);

-- Watchlist price monitor: tracks Watchlist-category stocks against their
-- ideal buy range WITHOUT opening a paper trade (because watchlist means
-- "not yet cheap enough to buy"). One row per symbol, updated on every run.
CREATE TABLE IF NOT EXISTS watchlist_monitor (
    symbol              TEXT PRIMARY KEY,
    company_name        TEXT NOT NULL,
    sector              TEXT,
    recommendation_id   INTEGER REFERENCES recommendation(recommendation_id),
    recommendation_date TEXT NOT NULL,
    current_price       REAL NOT NULL,
    buy_range_low       REAL NOT NULL,
    buy_range_high      REAL NOT NULL,
    fair_value          REAL,
    pct_away_from_entry REAL NOT NULL,  -- (current - buy_range_high) / buy_range_high * 100; negative = inside range
    overall_ai_score    REAL,
    status              TEXT NOT NULL DEFAULT 'WATCHING',  -- WATCHING | ENTERED (price reached buy range)
    updated_at          TEXT NOT NULL
);

-- Daily portfolio equity curve for analytics/charting.
CREATE TABLE IF NOT EXISTS portfolio_equity (
    date TEXT PRIMARY KEY,
    portfolio_value REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    open_trade_count INTEGER NOT NULL DEFAULT 0,
    closed_trade_count INTEGER NOT NULL DEFAULT 0
);

-- Cache of computed news analysis, keyed per symbol per day, so re-running
-- the app multiple times the same day doesn't re-hit free news sources
-- unnecessarily (politeness + speed). One JSON blob per symbol/day.
CREATE TABLE IF NOT EXISTS news_cache (
    symbol      TEXT NOT NULL,
    cache_date  TEXT NOT NULL,
    payload     TEXT NOT NULL,      -- JSON-serialized NewsAnalysisResult
    created_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, cache_date)
);

-- Run log so every application run is auditable.
CREATE TABLE IF NOT EXISTS run_log (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stocks_scanned INTEGER,
    recommendations_created INTEGER,
    trades_opened INTEGER,
    trades_updated INTEGER,
    trades_closed INTEGER,
    status TEXT DEFAULT 'RUNNING',
    notes TEXT
);
"""


class DatabaseManager:
    """Owns the SQLite connection and schema lifecycle."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)
        logger.debug("Database schema ready at %s", self.db_path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL mode allows concurrent readers alongside a writer (default
        # rollback-journal mode serializes much more aggressively); busy_timeout
        # makes SQLite retry for up to 10s instead of immediately raising
        # "database is locked" under write contention. Both matter now that
        # the pipeline fetches/scores multiple stocks concurrently (see
        # run_pipeline.py), each writing to news_cache/recommendation/etc.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
