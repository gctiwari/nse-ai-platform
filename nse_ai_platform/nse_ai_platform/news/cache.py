"""
news/cache.py
Simple per-symbol-per-day cache for NewsAnalysisResult, backed by SQLite.
Avoids re-hitting free news sources every time the app is launched the
same day (politeness to the free feeds + faster repeat runs).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date, datetime

from db.schema import DatabaseManager
from news.models import NewsAnalysisResult

logger = logging.getLogger(__name__)


class NewsCache:
    def __init__(self, db: DatabaseManager):
        self.db = db

    def get(self, symbol: str, cache_date: str | None = None) -> NewsAnalysisResult | None:
        cache_date = cache_date or date.today().isoformat()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM news_cache WHERE symbol=? AND cache_date=?",
                (symbol, cache_date),
            ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["payload"])
            data["analyzed_at"] = datetime.fromisoformat(data["analyzed_at"])
            return NewsAnalysisResult(**data)
        except Exception as e:
            logger.warning("Failed to deserialize cached news for %s: %s", symbol, e)
            return None

    def set(self, symbol: str, result: NewsAnalysisResult, cache_date: str | None = None) -> None:
        cache_date = cache_date or date.today().isoformat()
        payload = asdict(result)
        payload["analyzed_at"] = result.analyzed_at.isoformat()
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO news_cache (symbol, cache_date, payload, created_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(symbol, cache_date) DO UPDATE SET
                     payload=excluded.payload, created_at=excluded.created_at""",
                (symbol, cache_date, json.dumps(payload), datetime.now().isoformat()),
            )
