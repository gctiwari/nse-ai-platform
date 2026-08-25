"""
news/sentiment.py
Lexicon-based sentiment scoring with recency weighting. Phase-2 approach:
transparent, explainable, zero-cost (no paid NLP API). Swap for a proper
FinBERT/LLM sentiment model later by replacing `score_article` -- the
rest of the pipeline (recency weighting, aggregation) is unaffected.
"""

from __future__ import annotations

import math
from datetime import datetime

from news.models import NewsArticle
from news.classifier import POSITIVE_WORDS, NEGATIVE_WORDS, detect_events

# Recency half-life: an article's influence on the aggregate score halves
# every RECENCY_HALF_LIFE_DAYS. Configurable so you can make news "stale"
# faster or slower depending on how reactive you want the AI Score to be.
RECENCY_HALF_LIFE_DAYS = 3.0
MIN_RECENCY_WEIGHT = 0.05   # articles never fully drop to zero influence
MAX_ARTICLE_AGE_DAYS = 30   # articles older than this are excluded entirely

# A handful of event types carry an inherent sentiment tilt independent of
# the surrounding wording (e.g. "credit rating downgrade" is negative even
# if the word "downgrade" isn't flagged elsewhere in a short headline).
EVENT_SENTIMENT_TILT = {
    "Credit Rating Change": 0.0,      # direction depends on upgrade/downgrade -- handled by word lists
    "Litigation": -0.3,
    "Regulatory Action": -0.2,
    "New Order/Contract": 0.3,
    "Buyback": 0.25,
    "Acquisition": 0.1,
}


def recency_weight(published_at: datetime, now: datetime | None = None) -> float:
    now = now or datetime.now()
    age_days = max((now - published_at).total_seconds() / 86400.0, 0.0)
    if age_days > MAX_ARTICLE_AGE_DAYS:
        return 0.0
    weight = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
    return max(weight, MIN_RECENCY_WEIGHT)


def score_article(article: NewsArticle) -> float:
    """Returns a -1..1 sentiment score for a single article based on
    finance-lexicon keyword hits plus event-type tilts."""
    text = f"{article.title} {article.summary}".lower()

    pos_hits = sum(1 for w in POSITIVE_WORDS if w in text)
    neg_hits = sum(1 for w in NEGATIVE_WORDS if w in text)

    if pos_hits == 0 and neg_hits == 0:
        base = 0.0
    else:
        base = (pos_hits - neg_hits) / max(pos_hits + neg_hits, 1)

    events = article.events or detect_events(article)
    tilt = sum(EVENT_SENTIMENT_TILT.get(e, 0.0) for e in events)

    score = base * 0.75 + tilt * 0.25
    return max(-1.0, min(1.0, score))


def aggregate_sentiment(articles: list[NewsArticle], now: datetime | None = None) -> float:
    """Recency-weighted average sentiment across all articles, -1..1."""
    if not articles:
        return 0.0
    now = now or datetime.now()

    total_weight = 0.0
    weighted_sum = 0.0
    for a in articles:
        w = recency_weight(a.published_at, now)
        a.recency_weight = w
        s = a.sentiment if a.sentiment else score_article(a)
        a.sentiment = s
        weighted_sum += s * w
        total_weight += w

    if total_weight == 0:
        return 0.0
    return max(-1.0, min(1.0, weighted_sum / total_weight))


def sentiment_label(score: float) -> str:
    if score > 0.15:
        return "Positive"
    if score < -0.15:
        return "Negative"
    return "Neutral"


def overall_impact(score: float) -> str:
    if score > 0.2:
        return "Bullish"
    if score < -0.2:
        return "Bearish"
    return "Neutral"
