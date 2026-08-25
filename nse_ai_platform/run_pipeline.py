"""
run_pipeline.py
Main orchestrator. This is what runs "every time the application runs":
  1. Fetch latest market data
  2. Analyze NSE/BSE stocks (feature engineering)
  3. Generate AI recommendations (scoring + recommendation engine)
  4. Categorize into Watchlist / Conservative / Balanced / Aggressive
  5. Auto-create paper trades for every new recommendation
  6. Update existing open paper trades (mark-to-market, auto-exit)
  7. Persist everything to SQLite; print a run summary

Run with:  python run_pipeline.py
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, date

from db.schema import DatabaseManager
from db.repository import RecommendationRepository
from data.providers import get_provider
from core.feature_engineering import compute_technical_levels
from core.scoring import score_stock
from core.recommendation_engine import build_recommendation, rank_and_trim, classify_market_cap
from core.valuation import compute_sector_stats, inject_sector_stats
from core.paper_trading import PaperTradingEngine
from core.analytics import AnalyticsEngine
from news.engine import NewsAnalysisEngine
from news.models import NewsAnalysisResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pipeline")

# How long ONE stock's fetch+score is expected to reasonably take. Used to
# size the overall batch deadline below -- not used as a hard per-item
# cutoff in isolation (see _process_universe_concurrently's docstring for
# why that distinction matters).
PER_STOCK_TIMEOUT_SEC = 25

# How many stocks to fetch/score concurrently. Scanning the full NIFTY 500
# sequentially at ~3-5s/stock would take 25-40+ minutes -- concurrency is
# what makes that practical. This is I/O-bound work (waiting on network
# calls), so Python threads are effective despite the GIL. 10 is a
# moderate default: fast enough to matter, gentle enough not to hammer
# Yahoo/news sources into rate-limiting.
MAX_CONCURRENT_STOCKS = 10


def _process_one_stock(provider, news_engine, symbol, company_name, sector):
    """Fetch + analyze + score one stock. Market cap category is computed
    dynamically from the real fetched market cap (see
    core.recommendation_engine.classify_market_cap) -- there's no
    practical way to hand-tag Large/Mid/Small for hundreds of symbols."""
    snapshot = provider.get_snapshot(symbol, company_name, sector)
    technicals = compute_technical_levels(snapshot)

    if news_engine is not None:
        news_result = news_engine.analyze(symbol, company_name, sector)
    else:
        news_result = NewsAnalysisResult.empty(symbol, company_name, reason="News analysis disabled")

    scores = score_stock(snapshot, technicals, news_result)
    mcap_cat = classify_market_cap(snapshot.market_cap)
    rec = build_recommendation(
        snapshot, technicals, scores, news_result,
        market_cap_category=mcap_cat, rec_date=date.today(),
    )
    return snapshot.price, rec


def _process_universe_concurrently(provider, news_engine, universe,
                                    max_workers: int = MAX_CONCURRENT_STOCKS,
                                    per_item_budget: float = PER_STOCK_TIMEOUT_SEC):
    """
    Runs _process_one_stock for every (symbol, company_name, sector) in
    universe, up to max_workers at a time, guaranteed to never hang the
    process -- even if some stocks' network calls get permanently stuck.

    IMPLEMENTATION NOTE -- this deliberately does NOT use
    concurrent.futures.ThreadPoolExecutor. Verified by direct testing:
    ThreadPoolExecutor's worker threads are non-daemon by design, and
    Python registers a global atexit hook that joins ALL of them at
    interpreter shutdown -- REGARDLESS of calling shutdown(wait=False).
    A single permanently-stuck stock (e.g. a hung yfinance `.info` crumb
    call) would make the whole PROCESS hang on exit even though the run()
    function itself returns and logs "complete". That's the same
    Ctrl+C-required hang the user originally hit, just moved to a
    different point in execution.

    Instead: every stock gets a genuine daemon `threading.Thread`
    (processes exit cleanly regardless of stragglers), concurrency is
    bounded by a `threading.Semaphore(max_workers)`, and BOTH the
    semaphore acquisition and the final join use a shared overall batch
    deadline (not a flat per-item timeout) -- this matters because if a
    few stocks permanently occupy worker slots, remaining HEALTHY stocks
    need to wait longer in the queue, not get falsely marked "timed out"
    just because they didn't get a worker immediately. The overall
    deadline scales with queue depth: ceil(N / max_workers) * per_item_budget,
    plus a fixed buffer -- i.e. "how long this would take in the worst
    case if every single stock took the full per-item budget."
    """
    all_recs = []
    latest_prices: dict[str, float] = {}
    timed_out: list[str] = []
    failed: list[tuple[str, str]] = []
    lock = threading.Lock()
    sem = threading.Semaphore(max_workers)

    overall_budget = math.ceil(len(universe) / max_workers) * per_item_budget + 20
    deadline = time.monotonic() + overall_budget

    def worker(symbol, company_name, sector):
        remaining = max(0, deadline - time.monotonic())
        acquired = sem.acquire(timeout=remaining)
        try:
            if not acquired:
                with lock:
                    timed_out.append(symbol)
                return
            price, rec = _process_one_stock(provider, news_engine, symbol, company_name, sector)
            with lock:
                latest_prices[symbol] = price
                if rec is not None:
                    all_recs.append(rec)
        except Exception as e:
            with lock:
                failed.append((symbol, str(e)))
        finally:
            if acquired:
                sem.release()

    threads = []
    for symbol, company_name, sector in universe:
        t = threading.Thread(target=worker, args=(symbol, company_name, sector), daemon=True)
        t.start()
        threads.append((symbol, t))

    # Poll for completion rather than joining sequentially in submission
    # order. Sequential joins would block the ENTIRE remaining budget on
    # whichever thread happens to be hung and listed first, before ever
    # checking later threads that may have already finished in a fraction
    # of a second -- turning "one early stock hangs" into "the whole batch
    # always takes the full worst-case deadline," which defeats the point
    # of concurrency for a mostly-healthy run.
    pending = dict(threads)  # symbol -> Thread, shrinks as threads finish
    poll_interval = 0.2
    while pending and time.monotonic() < deadline:
        done_symbols = [s for s, t in pending.items() if not t.is_alive()]
        for s in done_symbols:
            del pending[s]
        if pending:
            time.sleep(poll_interval)

    for symbol in pending:  # anything still alive when the deadline hit
        with lock:
            if symbol not in timed_out:
                timed_out.append(symbol)

    return all_recs, latest_prices, timed_out, failed


def run(data_provider_name: str = "yfinance", news_enabled: bool = True) -> dict:
    """
    data_provider_name: "yfinance" (default, real NIFTY 500 prices/OHLCV/
        basic fundamentals via Yahoo Finance) or "demo" (offline synthetic
        data over a small fixed universe, useful for testing without
        internet access).
    news_enabled: set False to skip news analysis entirely (pure
        price/fundamentals-driven scoring, faster runs, no internet needed
        for the news step specifically).
    """
    started = datetime.now()
    db = DatabaseManager()
    repo = RecommendationRepository(db)
    trading = PaperTradingEngine(db)
    provider = get_provider(data_provider_name)
    news_engine = NewsAnalysisEngine(db) if news_enabled else None

    with db.connect() as conn:
        run_id = conn.execute(
            "INSERT INTO run_log (started_at, status) VALUES (?, 'RUNNING')",
            (started.isoformat(),),
        ).lastrowid

    logger.info("=== Run #%s started (provider=%s) ===", run_id, data_provider_name)

    universe = provider.get_universe()
    universe_info = provider.describe_universe()
    logger.info("Scanning %d stocks in universe (up to %d concurrently)... source: %s",
                len(universe), MAX_CONCURRENT_STOCKS, universe_info.get("label", "?"))

    # Batched history prefetch (yfinance: one network round-trip for all
    # symbols instead of one-per-symbol). No-op for providers that don't
    # support it (e.g. DemoDataProvider).
    try:
        provider.prefetch([s for s, *_ in universe])
    except Exception:
        logger.exception("Prefetch failed -- continuing with per-symbol fetches")

    # Two-pass approach: collect all snapshots first, compute sector stats,
    # inject them, then score. This enables cross-sectional valuation (§2.5).
    import math as _math, threading as _threading

    raw_snaps: dict = {}; snap_timed_out = []; snap_failed = []
    snap_lock = _threading.Lock()
    budget = _math.ceil(len(universe)/MAX_CONCURRENT_STOCKS)*PER_STOCK_TIMEOUT_SEC + 20
    deadline = time.monotonic() + budget
    sem = _threading.Semaphore(MAX_CONCURRENT_STOCKS)

    def _fetch_worker(symbol, company_name, sector):
        rem = max(0, deadline - time.monotonic())
        if not sem.acquire(timeout=rem):
            with snap_lock: snap_timed_out.append(symbol)
            return
        try:
            snap = provider.get_snapshot(symbol, company_name, sector)
            with snap_lock: raw_snaps[symbol] = snap
        except Exception as e:
            with snap_lock: snap_failed.append((symbol, str(e)))
        finally:
            sem.release()

    threads = [_threading.Thread(target=_fetch_worker, args=item, daemon=True) for item in universe]
    for t in threads: t.start()
    deadline2 = deadline
    pending = dict(zip([i[0] for i in universe], threads))
    while pending and time.monotonic() < deadline2:
        done = [s for s,t in pending.items() if not t.is_alive()]
        for s in done: del pending[s]
        if pending: time.sleep(0.2)
    for sym in pending:
        if sym not in snap_timed_out: snap_timed_out.append(sym)

    # Compute + inject sector stats
    valid_snaps = list(raw_snaps.values())
    sector_stats = compute_sector_stats(valid_snaps)
    logger.info("Computed sector stats for %d sector(s) from %d snapshots",
                len(sector_stats), len(valid_snaps))
    for sym in raw_snaps:
        inject_sector_stats(raw_snaps[sym], sector_stats)

    # Pass 2: score
    all_recs = []; latest_prices: dict = {}
    score_timed_out = []; score_failed = []
    score_lock = _threading.Lock()
    sem2 = _threading.Semaphore(MAX_CONCURRENT_STOCKS)
    deadline3 = time.monotonic() + budget

    def _score_worker(symbol, company_name, sector):
        snap = raw_snaps.get(symbol)
        if snap is None: return
        rem = max(0, deadline3 - time.monotonic())
        if not sem2.acquire(timeout=rem):
            with score_lock: score_timed_out.append(symbol)
            return
        try:
            from core.feature_engineering import compute_technical_levels
            from core.scoring import score_stock
            from core.recommendation_engine import classify_market_cap
            technicals = compute_technical_levels(snap)
            if news_engine is not None:
                news_result = news_engine.analyze(symbol, company_name, sector)
            else:
                news_result = NewsAnalysisResult.empty(symbol, company_name, reason="disabled")
            scores = score_stock(snap, technicals, news_result)
            mcap_cat = classify_market_cap(snap.market_cap)
            rec = build_recommendation(snap, technicals, scores, news_result,
                                        market_cap_category=mcap_cat, rec_date=date.today())
            with score_lock:
                latest_prices[symbol] = snap.price
                if rec is not None: all_recs.append(rec)
        except Exception as e:
            with score_lock: score_failed.append((symbol, str(e)))
        finally:
            sem2.release()

    threads2 = [_threading.Thread(target=_score_worker, args=item, daemon=True) for item in universe]
    for t in threads2: t.start()
    pending2 = dict(zip([i[0] for i in universe], threads2))
    while pending2 and time.monotonic() < deadline3:
        done2 = [s for s,t in pending2.items() if not t.is_alive()]
        for s in done2: del pending2[s]
        if pending2: time.sleep(0.2)

    timed_out = snap_timed_out + score_timed_out
    failed = snap_failed + score_failed

    if timed_out:
        logger.warning("%d stock(s) timed out after %ds: %s%s", len(timed_out), PER_STOCK_TIMEOUT_SEC,
                        ", ".join(timed_out[:15]), " ..." if len(timed_out) > 15 else "")
    if failed:
        logger.warning("%d stock(s) failed: %s%s", len(failed),
                        ", ".join(f"{s} ({e})" for s, e in failed[:10]), " ..." if len(failed) > 10 else "")

    logger.info("%d/%d stocks cleared the minimum AI-score bar", len(all_recs), len(universe))

    by_category = rank_and_trim(all_recs)
    for cat, recs in by_category.items():
        logger.info("  %-14s -> %d recommendations", cat.value, len(recs))

    # Persist recommendations and dispatch paper trades with correct behavior
    # per category and per the existing-trade state:
    #
    #   Watchlist  -> NEVER open a paper trade. Update the watchlist monitor
    #                 table (price vs. buy range). This is a "watch, don't buy."
    #
    #   Conservative / Balanced / Aggressive, symbol already has an OPEN trade
    #                -> REFRESH the existing trade's stop-loss and targets to the
    #                   new recommendation's levels. Don't open a duplicate.
    #                   Growing stock: AI may have raised stop-loss (trailing effect)
    #                   Falling stock: AI may have tightened stop-loss (cut loss faster)
    #
    #   Conservative / Balanced / Aggressive, no existing open trade
    #                -> OPEN a new paper trade at today's price.
    #
    #   Same-day duplicate (already recommended today, any category)
    #                -> SKIP entirely (idempotent same-day re-run protection).

    trades_opened = 0
    trades_refreshed = 0
    watchlist_monitored = 0
    recos_saved = 0
    recos_skipped_duplicate = 0
    today_str = date.today().isoformat()

    for cat, recs in by_category.items():
        for rec in recs:
            if repo.has_recommendation_today(rec.symbol, today_str):
                recos_skipped_duplicate += 1
                continue

            rec_id = repo.save(rec)
            recos_saved += 1

            if rec.category.value == "Watchlist":
                # Watchlist: track price vs buy range, never trade.
                repo.upsert_watchlist_monitor(
                    symbol=rec.symbol, company_name=rec.company_name, sector=rec.sector,
                    recommendation_id=rec_id, recommendation_date=today_str,
                    current_price=rec.current_price, buy_range_low=rec.buy_range_low,
                    buy_range_high=rec.buy_range_high, fair_value=rec.fair_value,
                    overall_ai_score=rec.scores.overall_ai_score,
                )
                watchlist_monitored += 1
            else:
                # Tradeable category: refresh if already open, open if not.
                existing_trade = repo.get_open_trade_for_symbol(rec.symbol)
                if existing_trade:
                    trading.refresh_open_trade(
                        trade_id=existing_trade["trade_id"], symbol=rec.symbol,
                        new_stop_loss=rec.stop_loss, new_target_1=rec.target_1,
                        new_target_2=rec.target_2, new_target_3=rec.target_3,
                        current_price=rec.current_price,
                        recommendation_version=rec.recommendation_version,
                    )
                    trades_refreshed += 1
                else:
                    trading.open_trade_for_recommendation(
                        recommendation_id=rec_id, symbol=rec.symbol,
                        category=rec.category.value,
                        recommendation_date=today_str,
                        buy_price=rec.current_price, stop_loss=rec.stop_loss,
                        target_1=rec.target_1, target_2=rec.target_2, target_3=rec.target_3,
                        recommendation_version=rec.recommendation_version,
                    )
                    trades_opened += 1

    if recos_skipped_duplicate:
        logger.info("Skipped %d already-recommended-today stocks (idempotent same-day run)",
                    recos_skipped_duplicate)
    if trades_refreshed:
        logger.info("%d existing open trades refreshed with new stop-loss/targets", trades_refreshed)
    if watchlist_monitored:
        logger.info("%d Watchlist stocks monitored (no trade opened -- not yet at buy range)",
                    watchlist_monitored)

    # Mark-to-market every existing OPEN trade (including ones opened in prior runs).
    update_summary = trading.update_open_trades(latest_prices)
    trading.record_daily_equity()

    finished = datetime.now()
    with db.connect() as conn:
        conn.execute(
            """UPDATE run_log SET finished_at=?, stocks_scanned=?, recommendations_created=?,
               trades_opened=?, trades_updated=?, trades_closed=?, status='COMPLETE'
               WHERE run_id=?""",
            (finished.isoformat(), len(universe), len(all_recs), trades_opened,
             update_summary["updated"], update_summary["closed"], run_id),
        )

    elapsed = (finished - started).total_seconds()
    logger.info("=== Run #%s complete in %.2fs: %d recos, %d new trades, %d refreshed, "
                "%d watchlist monitored, %d updated, %d closed ===",
                run_id, elapsed, len(all_recs), trades_opened, trades_refreshed,
                watchlist_monitored, update_summary["updated"], update_summary["closed"])

    analytics = AnalyticsEngine(db)
    summary = analytics.dashboard_summary()

    try:
        index_quotes = provider.get_index_quotes()
    except Exception:
        logger.exception("Failed to fetch index quotes -- continuing without them")
        index_quotes = {}

    try:
        market_status = provider.get_market_status()
    except Exception:
        logger.exception("Failed to determine market status -- defaulting to 'Unknown'")
        market_status = "Unknown"

    return {
        "run_id": run_id,
        "elapsed_sec": elapsed,
        "stocks_scanned": len(universe),
        "universe_info": universe_info,
        "recommendations_created": len(all_recs),
        "recommendations_saved_new": recos_saved,
        "recommendations_skipped_duplicate": recos_skipped_duplicate,
        "by_category_counts": {c.value: len(r) for c, r in by_category.items()},
        "trades_opened": trades_opened,
        "trades_refreshed": trades_refreshed,
        "watchlist_monitored": watchlist_monitored,
        "trades_updated": update_summary["updated"],
        "trades_closed": update_summary["closed"],
        "dashboard_summary": summary,
        "index_quotes": index_quotes,
        "market_status": market_status,
    }


def print_console_report(result: dict) -> None:
    print("\n" + "=" * 64)
    print("  AI STOCK RECOMMENDATION & PAPER TRADING PLATFORM")
    print("=" * 64)
    print(f"  Date/Time      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Market Status  : {result['market_status']}")
    print("-" * 64)
    for name, q in result["index_quotes"].items():
        print(f"  {name:<14}: {q['value']:>10}   ({q['change_pct']:+.2f}%)")
    print("-" * 64)
    print(f"  Stocks scanned          : {result['stocks_scanned']}  "
          f"({result.get('universe_info', {}).get('label', '?')})")
    print(f"  Recommendations created : {result['recommendations_created']}")
    for cat, count in result["by_category_counts"].items():
        print(f"    - {cat:<14}: {count}")
    print("-" * 64)
    print(f"  Paper trades opened     : {result['trades_opened']}")
    print(f"  Paper trades updated    : {result['trades_updated']}")
    print(f"  Paper trades closed     : {result['trades_closed']}")
    print("-" * 64)
    s = result["dashboard_summary"]
    print(f"  Open trades    : {s['open_trades']}   Closed trades: {s['closed_trades']}")
    print(f"  Win rate       : {s['win_rate_pct']}%")
    print(f"  Avg return     : {s['avg_return_pct']}%   Avg hold: {s['avg_holding_days']} days")
    print("=" * 64 + "\n")


if __name__ == "__main__":
    result = run(data_provider_name="yfinance")
    print_console_report(result)
