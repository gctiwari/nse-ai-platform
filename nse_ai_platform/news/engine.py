"""
news/engine.py
Orchestrates the full news-analysis pipeline for one stock:
  fetch (multiple providers) -> dedupe -> classify scope -> detect events
  -> score sentiment (recency-weighted) -> summarize -> cache.

This is the single entry point the rest of the app should use
(`NewsAnalysisEngine.analyze`). Everything upstream (scoring engine,
recommendation engine, UI) only ever talks to this class and to
NewsAnalysisResult -- never to individual providers -- so the whole
free-source stack can be replaced by a premium API later by rewriting
only this file's `analyze` internals (or swapping `self.providers`).
"""

from __future__ import annotations

import logging
from datetime import datetime

from db.schema import DatabaseManager
from news.cache import NewsCache
from news.classifier import deduplicate, classify_scope, detect_events
from news.models import NewsArticle, NewsAnalysisResult
from news.providers import NewsProvider, default_providers
from news.sentiment import aggregate_sentiment, sentiment_label, overall_impact, score_article

logger = logging.getLogger(__name__)

MAX_ARTICLES_PER_PROVIDER = 10
TOP_HEADLINES_SHOWN = 5


class NewsAnalysisEngine:
    def __init__(self, db: DatabaseManager, providers: list[NewsProvider] | None = None,
                 use_cache: bool = True):
        self.providers = providers if providers is not None else default_providers()
        self.cache = NewsCache(db) if use_cache else None

    def analyze(self, symbol: str, company_name: str, sector: str) -> NewsAnalysisResult:
        if self.cache is not None:
            cached = self.cache.get(symbol)
            if cached is not None:
                return cached

        try:
            result = self._analyze_fresh(symbol, company_name, sector)
        except Exception as e:
            logger.warning("News analysis failed for %s, falling back to neutral: %s", symbol, e)
            result = NewsAnalysisResult.empty(symbol, company_name, reason="News analysis failed (see logs)")

        if self.cache is not None:
            self.cache.set(symbol, result)
        return result

    def _analyze_fresh(self, symbol: str, company_name: str, sector: str) -> NewsAnalysisResult:
        all_articles: list[NewsArticle] = []
        sources_used: list[str] = []

        for provider in self.providers:
            try:
                items = provider.fetch(symbol, company_name, max_results=MAX_ARTICLES_PER_PROVIDER)
            except Exception as e:
                logger.warning("Provider %s failed for %s: %s", provider.name, symbol, e)
                items = []
            if items:
                sources_used.append(provider.name)
            all_articles.extend(items)

        if not all_articles:
            return NewsAnalysisResult.empty(symbol, company_name, reason="No recent news found")

        deduped = deduplicate(all_articles)

        for a in deduped:
            a.scope = classify_scope(a, symbol, company_name, sector)
            a.events = detect_events(a)
            a.sentiment = score_article(a)

        # Only company-specific + sector-specific articles drive the
        # per-stock sentiment score; pure market-wide noise (general
        # index commentary) is excluded so it doesn't dilute stock-specific
        # signal, but it's still shown in the scope breakdown for context.
        relevant = [a for a in deduped if a.scope in ("Company-specific", "Sector-specific")]
        scored_pool = relevant if relevant else deduped

        agg_score = aggregate_sentiment(scored_pool)
        label = sentiment_label(agg_score)
        impact = overall_impact(agg_score)

        scope_breakdown = {"Company-specific": 0, "Sector-specific": 0, "Market-wide": 0}
        for a in deduped:
            scope_breakdown[a.scope] = scope_breakdown.get(a.scope, 0) + 1

        # Rank by recency-weighted |sentiment| so the most impactful, most
        # recent stories surface first for the "top headlines" display.
        ranked = sorted(deduped, key=lambda a: a.recency_weight * (1 + abs(a.sentiment)), reverse=True)
        top = ranked[:TOP_HEADLINES_SHOWN]
        top_headlines = [
            {"title": a.title, "source": a.source, "url": a.url,
             "published_at": a.published_at.isoformat(), "scope": a.scope,
             "sentiment": round(a.sentiment, 2)}
            for a in top
        ]

        positive_factors = self._extract_factors(deduped, positive=True)
        negative_factors = self._extract_factors(deduped, positive=False)
        event_tags = sorted({e for a in deduped for e in a.events})

        summary = self._build_summary(company_name, agg_score, label, deduped, scope_breakdown, event_tags)

        return NewsAnalysisResult(
            symbol=symbol, company_name=company_name, analyzed_at=datetime.now(),
            sentiment_score=round(agg_score, 3), sentiment_label=label, overall_impact=impact,
            article_count=len(deduped), sources_used=sources_used,
            top_headlines=top_headlines, summary=summary,
            positive_factors=positive_factors, negative_factors=negative_factors,
            event_tags=event_tags, scope_breakdown=scope_breakdown,
        )

    @staticmethod
    def _extract_factors(articles: list[NewsArticle], positive: bool, limit: int = 4) -> list[str]:
        pool = [a for a in articles if (a.sentiment > 0.1) == positive and abs(a.sentiment) > 0.1]
        pool.sort(key=lambda a: a.recency_weight * abs(a.sentiment), reverse=True)
        factors = []
        for a in pool[:limit]:
            tag = f" [{', '.join(a.events)}]" if a.events else ""
            factors.append(f"{a.title}{tag}")
        return factors

    @staticmethod
    def _build_summary(company_name: str, score: float, label: str, articles: list[NewsArticle],
                        scope_breakdown: dict, event_tags: list[str]) -> str:
        n = len(articles)
        if n == 0:
            return f"No recent news found for {company_name}."

        scope_bits = ", ".join(f"{v} {k.lower()}" for k, v in scope_breakdown.items() if v > 0)
        event_bit = f" Key events flagged: {', '.join(event_tags)}." if event_tags else ""
        tone = {
            "Positive": "News flow over the recent period skews positive",
            "Negative": "News flow over the recent period skews negative",
            "Neutral": "News flow over the recent period is largely mixed/neutral",
        }[label]

        return (
            f"{tone} for {company_name} (sentiment score {score:+.2f}), based on {n} "
            f"deduplicated articles ({scope_bits}) from the last 30 days, weighted toward "
            f"more recent coverage.{event_bit}"
        )
