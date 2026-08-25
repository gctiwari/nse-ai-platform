"""
news/classifier.py
Deduplication + classification: is an article company-specific,
sector-specific, or market-wide, and which corporate events does it
mention. Pure functions over NewsArticle -- no I/O.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from news.models import NewsArticle

# Keyword sets are intentionally simple/transparent (Phase-2 rule-based
# approach) so results stay explainable; swap for an NLP/LLM classifier
# later without changing the interface (dedupe/classify/detect_events).

SECTOR_KEYWORDS = {
    "Banking": ["bank", "nbfc", "rbi", "repo rate", "npa", "credit growth", "casa"],
    "IT": ["it sector", "software", "tcs", "infosys", "it services", "digital"],
    "Power": ["power sector", "electricity", "discom", "renewable", "solar", "thermal power"],
    "Energy": ["oil", "gas", "crude", "opec", "refinery", "petroleum"],
    "Auto": ["auto sector", "vehicle sales", "ev sales", "automaker"],
    "FMCG": ["fmcg", "consumer goods", "rural demand"],
    "Metals": ["steel", "metals sector", "aluminium", "iron ore"],
    "Pharma": ["pharma sector", "drug approval", "usfda"],
}

MARKET_KEYWORDS = [
    "sensex", "nifty", "rbi policy", "inflation", "gdp growth", "fii outflow",
    "fii inflow", "budget 2", "fed rate", "global markets", "crude oil prices",
    "rupee", "market close", "market opens", "bse", "nse",
]

# Values can be plain substrings, or regex patterns (prefixed "re:") for
# event types where real headlines vary too much in word order/adjacency
# for a fixed phrase to reliably match (e.g. "wins new ... order").
EVENT_KEYWORDS = {
    "Quarterly Results": ["q1 results", "q2 results", "q3 results", "q4 results",
                           "quarterly results", "net profit", "profit rises", "profit falls",
                           "revenue rose", "beats estimates", "misses estimates", "earnings"],
    "Management Guidance": ["guidance", "management commentary", "outlook raised",
                             "outlook cut", "forward guidance"],
    "Acquisition": ["acquire", "acquisition", "to buy stake", "merger", "amalgamation",
                     "takeover"],
    "New Order/Contract": [
        r"re:\b(wins?|bags?|secures?|receives?|gets?|awarded)\b[^.]{0,40}\b(order|contract)s?\b",
        "new contract", "order win",
    ],
    "Regulatory Action": ["sebi", "regulatory action", "penalty imposed", "show cause notice",
                           "compliance", "ban on"],
    "Litigation": ["lawsuit", "court", "litigation", "legal notice", "tribunal"],
    "Credit Rating Change": ["rating upgrade", "rating downgrade", "crisil", "icra",
                              "care ratings", "moody's", "outlook revised"],
    "Dividend": ["dividend", "interim dividend", "final dividend"],
    "Buyback": ["buyback", "share repurchase"],
    "Promoter Stake Change": ["promoter stake", "pledge shares", "stake sale", "stake increase",
                               "block deal", "bulk deal"],
}

POSITIVE_WORDS = [
    "surge", "jump", "rally", "gain", "beat", "beats", "outperform", "upgrade", "upgraded",
    "record high", "strong growth", "profit rises", "wins", "bags", "raised guidance",
    "buyback", "expansion", "robust", "exceeds", "positive", "bullish", "growth",
    "all-time high", "best quarter",
]
NEGATIVE_WORDS = [
    "plunge", "fall", "falls", "crash", "downgrade", "downgraded", "miss", "misses",
    "loss", "losses", "probe", "penalty", "fraud", "default", "weak", "decline",
    "negative", "bearish", "lawsuit", "resign", "resignation", "sell-off", "selloff",
    "cut guidance", "profit warning", "concerns",
]


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def deduplicate(articles: list[NewsArticle]) -> list[NewsArticle]:
    """Removes exact URL duplicates and near-duplicate titles (same story
    picked up by multiple feeds/outlets)."""
    seen_urls: set[str] = set()
    kept: list[NewsArticle] = []
    kept_norms: list[str] = []

    for a in articles:
        if a.url in seen_urls:
            continue
        norm = _normalize_title(a.title)
        is_dupe = any(SequenceMatcher(None, norm, k).ratio() > 0.82 for k in kept_norms)
        if is_dupe:
            continue
        seen_urls.add(a.url)
        kept_norms.append(norm)
        kept.append(a)
    return kept


def classify_scope(article: NewsArticle, symbol: str, company_name: str, sector: str) -> str:
    text = f"{article.title} {article.summary}".lower()
    company_terms = [company_name.lower(), symbol.lower()]
    if any(term in text for term in company_terms if term):
        return "Company-specific"

    sector_terms = SECTOR_KEYWORDS.get(sector, [])
    if any(term in text for term in sector_terms):
        return "Sector-specific"

    if any(term in text for term in MARKET_KEYWORDS):
        return "Market-wide"

    # Default: articles sourced from a company-targeted search (Google
    # News/yfinance queries) that don't hit our keyword lists are still
    # most likely company-specific since the query itself was company-scoped.
    if article.provider in ("Yahoo Finance", "Google News"):
        return "Company-specific"
    return "Market-wide"


def _keyword_matches(text: str, keyword: str) -> bool:
    if keyword.startswith("re:"):
        return re.search(keyword[3:], text) is not None
    return keyword in text


def detect_events(article: NewsArticle) -> list[str]:
    text = f"{article.title} {article.summary}".lower()
    return [event for event, keywords in EVENT_KEYWORDS.items()
            if any(_keyword_matches(text, k) for k in keywords)]
