"""
news/providers.py
Pluggable news sources. NewsProvider is the interface -- swap any
implementation for a premium one (Bloomberg, Refinitiv, Moneycontrol Pro,
NewsAPI.ai, etc.) later without touching news/engine.py or anything
upstream of it.

All providers are defensive: network/parsing failures return an empty
list rather than raising, so a single bad feed never breaks a run.
"""

from __future__ import annotations

import abc
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from news.models import NewsArticle

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SEC = 6
USER_AGENT = "Mozilla/5.0 (compatible; AIStockPlatform/1.0; +research-tool)"


def _http_get(url: str, timeout: int = REQUEST_TIMEOUT_SEC) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        logger.warning("HTTP fetch failed for %s: %s", url, e)
        return None


def _parse_rss(raw: bytes, source_label: str, provider_label: str) -> list[NewsArticle]:
    """Parses a standard RSS 2.0 <item> feed into NewsArticle objects."""
    articles: list[NewsArticle] = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        logger.warning("RSS parse failed for %s: %s", source_label, e)
        return articles

    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        desc = (item.findtext("description") or "").strip()
        desc = re.sub("<[^<]+?>", "", desc)  # strip any embedded HTML

        pub_raw = item.findtext("pubDate")
        try:
            published_at = parsedate_to_datetime(pub_raw) if pub_raw else datetime.now()
            if published_at.tzinfo is not None:
                published_at = published_at.replace(tzinfo=None)
        except Exception:
            published_at = datetime.now()

        # Google News RSS nests the real source in a <source> tag.
        source_tag = item.findtext("source")
        source = source_tag.strip() if source_tag else source_label

        articles.append(NewsArticle(
            title=title, source=source, url=link, published_at=published_at,
            summary=desc[:400], provider=provider_label,
        ))
    return articles


class NewsProvider(abc.ABC):
    """Every news source implements this. Swap implementations freely."""

    name: str = "base"

    @abc.abstractmethod
    def fetch(self, symbol: str, company_name: str, max_results: int = 10) -> list[NewsArticle]:
        ...


class YFinanceNewsProvider(NewsProvider):
    """Company-specific headlines via yfinance's Ticker.news."""

    name = "Yahoo Finance"

    def fetch(self, symbol: str, company_name: str, max_results: int = 10) -> list[NewsArticle]:
        try:
            import yfinance as yf
        except ImportError:
            logger.info("yfinance not installed -- skipping YFinanceNewsProvider")
            return []

        try:
            raw_items = yf.Ticker(f"{symbol}.NS").news or []
        except Exception as e:
            logger.warning("yfinance news fetch failed for %s: %s", symbol, e)
            return []

        articles = []
        for item in raw_items[:max_results]:
            # yfinance news schema has shifted over versions; handle both
            # the flat and the nested "content" formats defensively.
            content = item.get("content", item)
            title = content.get("title") or item.get("title")
            link = (content.get("canonicalUrl") or {}).get("url") or item.get("link")
            if not title or not link:
                continue
            pub_str = content.get("pubDate") or item.get("providerPublishTime")
            try:
                if isinstance(pub_str, (int, float)):
                    published_at = datetime.fromtimestamp(pub_str)
                elif isinstance(pub_str, str):
                    published_at = datetime.fromisoformat(pub_str.replace("Z", "+00:00")).replace(tzinfo=None)
                else:
                    published_at = datetime.now()
            except Exception:
                published_at = datetime.now()

            provider_name = (content.get("provider") or {}).get("displayName", self.name)
            articles.append(NewsArticle(
                title=title, source=provider_name, url=link, published_at=published_at,
                summary=content.get("summary", "") or "", provider=self.name,
            ))
        return articles


class GoogleNewsRSSProvider(NewsProvider):
    """
    Company-specific search via Google News' public RSS search endpoint.
    No API key required; this is the same feed format Google News' own
    website consumes. Free but unofficial -- treat as best-effort.
    """

    name = "Google News"
    BASE_URL = "https://news.google.com/rss/search"

    def fetch(self, symbol: str, company_name: str, max_results: int = 10) -> list[NewsArticle]:
        query = urllib.parse.quote(f'"{company_name}" OR "{symbol}" NSE stock')
        url = f"{self.BASE_URL}?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
        raw = _http_get(url)
        if raw is None:
            return []
        return _parse_rss(raw, source_label="Google News", provider_label=self.name)[:max_results]


class StaticRSSProvider(NewsProvider):
    """
    Generic RSS reader for fixed market/sector feeds (Economic Times
    Markets, Moneycontrol, etc.). These aren't company-filterable at the
    feed level, so the engine scans returned items for company/sector
    mentions and tags scope accordingly (see news/classifier.py).
    """

    def __init__(self, feed_url: str, label: str):
        self.feed_url = feed_url
        self.name = label

    def fetch(self, symbol: str, company_name: str, max_results: int = 20) -> list[NewsArticle]:
        raw = _http_get(self.feed_url)
        if raw is None:
            return []
        return _parse_rss(raw, source_label=self.name, provider_label=self.name)[:max_results]


# Fixed market/sector-context feeds used to supply "Market-wide" /
# "Sector-specific" articles even when a stock has little company-specific
# coverage. Feel free to add/remove URLs; each is fetched independently
# and a failure in one doesn't affect the others.
DEFAULT_MARKET_FEEDS = [
    StaticRSSProvider("https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
                       "Economic Times Markets"),
    StaticRSSProvider("https://www.moneycontrol.com/rss/marketreports.xml",
                       "Moneycontrol Markets"),
    StaticRSSProvider("https://www.moneycontrol.com/rss/business.xml",
                       "Moneycontrol Business"),
]


def default_providers() -> list[NewsProvider]:
    """The Phase-2 default source set: yfinance news + Google News RSS
    (company-specific) plus a couple of general market/business feeds
    (market-wide/sector-context). All free, no API keys."""
    return [YFinanceNewsProvider(), GoogleNewsRSSProvider(), *DEFAULT_MARKET_FEEDS]
