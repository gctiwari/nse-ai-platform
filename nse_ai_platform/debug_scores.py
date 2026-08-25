"""
debug_scores.py
Diagnostic tool: fetches data for the whole universe, runs it through
feature engineering + scoring, and prints every stock's score breakdown
WITHOUT filtering/classifying -- so you can see the real distribution of
Overall AI Scores, Risk Scores, etc. on live data and share it back if
recommendations still look too sparse or lopsided.

Usage:
    python debug_scores.py                 # yfinance, no news (faster)
    python debug_scores.py --news          # yfinance + news sentiment
    python debug_scores.py --demo          # offline synthetic data
"""

from __future__ import annotations

import argparse
import logging

from db.schema import DatabaseManager
from data.providers import get_provider
from core.feature_engineering import compute_technical_levels
from core.scoring import score_stock
from core.recommendation_engine import _fair_value, classify_category, classify_market_cap, DEFAULT_THRESHOLDS
from news.engine import NewsAnalysisEngine
from news.models import NewsAnalysisResult
import run_pipeline

logging.basicConfig(level=logging.WARNING)  # quiet -- we want the table, not the noise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="use offline synthetic data instead of yfinance")
    parser.add_argument("--news", action="store_true", help="also fetch real news sentiment (slower)")
    args = parser.parse_args()

    provider = get_provider("demo" if args.demo else "yfinance")
    news_engine = NewsAnalysisEngine(DatabaseManager()) if args.news else None

    universe = provider.get_universe()
    print(f"Fetching + scoring {len(universe)} stocks (provider={'demo' if args.demo else 'yfinance'}, "
          f"news={'on' if args.news else 'off'}, up to {run_pipeline.MAX_CONCURRENT_STOCKS} concurrently)...\n")

    provider.prefetch([s for s, *_ in universe])

    rows: list = []
    rows_lock = __import__("threading").Lock()

    def score_and_record(prov, news_eng, symbol, company_name, sector):
        snapshot = prov.get_snapshot(symbol, company_name, sector)
        technicals = compute_technical_levels(snapshot)
        news_result = (news_eng.analyze(symbol, company_name, sector) if news_eng
                       else NewsAnalysisResult.empty(symbol, company_name))
        scores = score_stock(snapshot, technicals, news_result)
        fv = _fair_value(snapshot)
        mos = (fv - snapshot.price) / fv * 100 if fv else 0
        mcap_cat = classify_market_cap(snapshot.market_cap)
        category = classify_category(snapshot, scores, fv, DEFAULT_THRESHOLDS, market_cap_category=mcap_cat)
        with rows_lock:
            rows.append((symbol, scores.overall_ai_score, scores.fundamental_score,
                         scores.technical_score, scores.valuation_score, scores.risk_score,
                         scores.sentiment_score, mos, mcap_cat, category.value if category else "DROPPED"))
        return snapshot.price, None  # reuse run_pipeline's plumbing; rec unused here

    # Monkey-patch run_pipeline's per-stock function for this diagnostic run only,
    # so we get the exact same proven concurrency/timeout machinery as the real
    # pipeline instead of a second, divergent implementation.
    original = run_pipeline._process_one_stock
    run_pipeline._process_one_stock = score_and_record
    try:
        _, _, timed_out, failed = run_pipeline._process_universe_concurrently(provider, news_engine, universe)
    finally:
        run_pipeline._process_one_stock = original

    for symbol in timed_out:
        rows.append((symbol, None, None, None, None, None, None, None, None, "TIMEOUT"))
    for symbol, err in failed:
        rows.append((symbol, None, None, None, None, None, None, None, None, f"ERROR: {err}"))

    rows.sort(key=lambda r: (r[1] is None, -(r[1] or 0)))

    header = (f"{'Symbol':<14}{'Overall':>9}{'Fund':>7}{'Tech':>7}{'Val':>7}{'Risk':>7}"
              f"{'News':>7}{'MoS%':>8}  {'MCap':<6}Category")
    print(header)
    print("-" * len(header))
    for r in rows:
        symbol, overall, fund, tech, val, risk, news, mos, mcap_cat, cat = r
        if overall is None:
            print(f"{symbol:<14}{'--':>9}  {cat}")
        else:
            print(f"{symbol:<14}{overall:>9.1f}{fund:>7.1f}{tech:>7.1f}{val:>7.1f}{risk:>7.1f}"
                  f"{news:>7.1f}{mos:>8.1f}  {mcap_cat:<6}{cat}")

    valid = [r for r in rows if r[1] is not None]
    if valid:
        overalls = [r[1] for r in valid]
        print(f"\n{len(valid)}/{len(rows)} stocks scored successfully.")
        print(f"Overall AI Score -- min: {min(overalls):.1f}  max: {max(overalls):.1f}  "
              f"avg: {sum(overalls)/len(overalls):.1f}")
        by_cat = {}
        for r in valid:
            by_cat[r[9]] = by_cat.get(r[9], 0) + 1
        print("Category breakdown:", by_cat)
        print(f"\nCurrent min_overall_score threshold: {DEFAULT_THRESHOLDS.min_overall_score}")
        print("If most stocks are landing in DROPPED or one category, share this output "
              "and I'll retune core/recommendation_engine.py's ClassificationThresholds.")


if __name__ == "__main__":
    main()
