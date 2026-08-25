"""
news/models.py
Shared dataclasses for the News Analysis module. Kept separate from
core/models.py so the news subsystem can be lifted out, swapped for a
premium provider, or versioned independently of the rest of the app.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class NewsArticle:
    """A single normalized news item, regardless of which provider it came from."""
    title: str
    source: str                 # e.g. "Yahoo Finance", "Google News", "Economic Times"
    url: str
    published_at: datetime
    summary: str = ""
    provider: str = ""          # which NewsProvider fetched it (for debugging/attribution)

    # Filled in by the analysis pipeline after fetch:
    scope: str = "Unknown"      # "Company-specific" / "Sector-specific" / "Market-wide"
    sentiment: float = 0.0      # -1 (very negative) to +1 (very positive)
    events: list[str] = field(default_factory=list)  # e.g. ["Quarterly Results", "New Order"]
    recency_weight: float = 1.0


@dataclass
class NewsAnalysisResult:
    """Everything the recommendation engine / UI needs about a stock's news."""
    symbol: str
    company_name: str
    analyzed_at: datetime

    sentiment_score: float          # -1..1, recency-weighted aggregate
    sentiment_label: str            # "Positive" / "Neutral" / "Negative"
    overall_impact: str             # "Bullish" / "Neutral" / "Bearish" (on the recommendation)

    article_count: int
    sources_used: list[str]

    top_headlines: list[dict]       # [{title, source, url, published_at, scope}, ...] (top 3-5)
    summary: str                    # one-paragraph AI-generated (template-based) summary
    positive_factors: list[str]
    negative_factors: list[str]
    event_tags: list[str]           # deduped corporate events detected across all articles
    scope_breakdown: dict           # {"Company-specific": n, "Sector-specific": n, "Market-wide": n}

    @classmethod
    def empty(cls, symbol: str, company_name: str, reason: str = "No news data available") -> "NewsAnalysisResult":
        """Neutral fallback used whenever fetching fails (e.g. no internet) so the
        rest of the pipeline never breaks because of a news-fetch problem."""
        return cls(
            symbol=symbol, company_name=company_name, analyzed_at=datetime.now(),
            sentiment_score=0.0, sentiment_label="Neutral", overall_impact="Neutral",
            article_count=0, sources_used=[],
            top_headlines=[], summary=reason,
            positive_factors=[], negative_factors=[], event_tags=[],
            scope_breakdown={"Company-specific": 0, "Sector-specific": 0, "Market-wide": 0},
        )
