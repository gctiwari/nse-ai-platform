"""
webapp/server.py
Flask backend for the browser-based dashboard (replaces the PySide6
desktop UI in ui/main_window.py, which had reliability issues on some
Windows setups). The frontend (webapp/static/) is a single-page
vanilla JS/HTML/CSS app that talks to this via a small JSON API.

Nothing in core/, data/, db/, or news/ changes because of this -- the
whole point of the layered architecture is that swapping the UI layer
never touches the pipeline underneath it.

Run with: python webapp_main.py
"""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

import run_pipeline
from db.schema import DatabaseManager
from db.repository import RecommendationRepository
from core.analytics import AnalyticsEngine
from core.recommendation_engine import rank_to_color_hex, MAX_PER_CATEGORY
from core.models import Category
from core.technical_module import analyse as ta_analyse

logger = logging.getLogger("webapp")

app = Flask(__name__, static_folder="static", static_url_path="")

# --- Background job state (pipeline runs can take 1-3 minutes against
# live yfinance data, far too long for a single synchronous HTTP request,
# so it runs in a background thread and the frontend polls for status). ---
_job_lock = threading.Lock()
_job_state = {
    "status": "idle",       # idle | running | complete | error
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
    "logs": [],              # recent log lines, newest last
}
_last_run_meta = {
    "market_status": None,
    "index_quotes": {},
    "last_run_at": None,
    "universe_info": None,
}


class _JobLogHandler(logging.Handler):
    """Captures log lines into _job_state['logs'] while a run is in
    progress, so the frontend can show live-ish progress instead of a
    silent multi-minute wait."""

    def emit(self, record):
        try:
            msg = self.format(record)
            with _job_lock:
                _job_state["logs"].append(msg)
                _job_state["logs"] = _job_state["logs"][-200:]  # cap memory use
        except Exception:
            pass


def _run_pipeline_background(provider_name: str, news_enabled: bool):
    handler = _JobLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        result = run_pipeline.run(data_provider_name=provider_name, news_enabled=news_enabled)
        with _job_lock:
            _job_state["status"] = "complete"
            _job_state["result"] = result
            _job_state["finished_at"] = datetime.now().isoformat()
        _last_run_meta["market_status"] = result.get("market_status")
        _last_run_meta["index_quotes"] = result.get("index_quotes", {})
        _last_run_meta["universe_info"] = result.get("universe_info")
        _last_run_meta["last_run_at"] = datetime.now().isoformat()
    except Exception as e:
        logger.exception("Pipeline run failed")
        with _job_lock:
            _job_state["status"] = "error"
            _job_state["error"] = str(e)
            _job_state["finished_at"] = datetime.now().isoformat()
    finally:
        root_logger.removeHandler(handler)


def _clean(obj):
    """Recursively replaces NaN/Infinity (which pandas produces freely but
    which aren't valid JSON) with None, so nothing downstream chokes on it."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def _get_db() -> DatabaseManager:
    # A fresh DatabaseManager per request is cheap (SQLite connections are
    # lightweight) and avoids any cross-thread connection-sharing issues
    # between Flask's request threads and the background pipeline thread.
    return DatabaseManager()


# ---------------------------------------------------------------- routes --

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/dashboard")
def api_dashboard():
    db = _get_db()
    repo = RecommendationRepository(db)
    analytics = AnalyticsEngine(db)

    counts = {c.value: len(repo.get_by_category(c.value)) for c in Category}
    top_picks = repo.get_top_overall(10)
    summary = analytics.dashboard_summary()

    return jsonify(_clean({
        "server_time": datetime.now().strftime("%A, %d %B %Y  %H:%M:%S"),
        "market_status": _last_run_meta["market_status"],
        "index_quotes": _last_run_meta["index_quotes"],
        "last_run_at": _last_run_meta["last_run_at"],
        "universe_info": _last_run_meta["universe_info"],
        "category_counts": counts,
        "top_picks": top_picks,
        "paper_trading_summary": summary,
    }))


@app.route("/api/recommendations/<category>")
def api_recommendations(category: str):
    db = _get_db()
    repo = RecommendationRepository(db)
    rows = repo.get_by_category(category)

    try:
        cat_enum = Category(category)
    except ValueError:
        return jsonify({"error": f"Unknown category '{category}'"}), 400

    for r in rows:
        r["color_hex"] = rank_to_color_hex(cat_enum, r["rank_in_category"], MAX_PER_CATEGORY)
        for field in ("news_headlines", "news_positive_factors", "news_negative_factors", "news_event_tags"):
            if isinstance(r.get(field), str):
                try:
                    r[field] = json.loads(r[field])
                except Exception:
                    r[field] = []

    return jsonify(_clean(rows))


@app.route("/api/watchlist-monitor")
def api_watchlist_monitor():
    """Watchlist stocks with price-vs-entry tracking -- no paper trades,
    just proximity to the ideal buy range."""
    db = _get_db()
    repo = RecommendationRepository(db)
    rows = repo.get_watchlist_monitors()
    return jsonify(_clean(rows))


@app.route("/api/paper-trades")
def api_paper_trades():
    """Deliberately minimal: the call (symbol), recommendation date, call
    price, days held, % change, and category -- no portfolio value or P&L,
    per the user's explicit request. This is a call-tracking tool, not a
    capital simulator."""
    db = _get_db()
    analytics = AnalyticsEngine(db)
    df = analytics._trades_df()
    if df.empty:
        return jsonify([])

    cols = ["symbol", "category", "recommendation_date", "buy_price",
            "days_held", "return_pct", "status", "exit_reason", "trade_id"]
    out = df[cols].rename(columns={
        "symbol": "call", "recommendation_date": "date", "buy_price": "call_price",
        "days_held": "days", "return_pct": "pct_change",
    })
    out = out.sort_values("date", ascending=False)
    return jsonify(_clean(out.to_dict(orient="records")))


@app.route("/api/trade-history/<int:trade_id>")
def api_trade_history(trade_id: int):
    db = _get_db()
    analytics = AnalyticsEngine(db)
    df = analytics.trade_history(trade_id)
    return jsonify(_clean(df.to_dict(orient="records")))



@app.route("/api/technical-analysis")
def api_technical_analysis():
    db = _get_db(); repo = RecommendationRepository(db)
    results = []; seen = set()
    for cat in [c.value for c in Category]:
        for rec in repo.get_by_category(cat):
            sym = rec["symbol"]
            if sym in seen: continue
            seen.add(sym)
            try:
                import json as _json
                def pj(v):
                    if isinstance(v, list): return v
                    try: return _json.loads(v) if v else []
                    except: return []
                ph = pj(rec.get("price_history"))
                if not ph: continue
                ta = ta_analyse(sym, rec["current_price"], ph,
                    pj(rec.get("high_history")), pj(rec.get("low_history")),
                    pj(rec.get("volume_history")),
                    rec.get("week52_high") or 0, rec.get("week52_low") or 0)
                results.append({"symbol": sym,
                    "company_name": rec.get("company_name", sym),
                    "category": rec.get("category"),
                    "current_price": rec["current_price"],
                    "composite_signal": ta.composite_signal,
                    "confidence": ta.confidence,
                    "bullish_count": ta.bullish_count,
                    "bearish_count": ta.bearish_count,
                    "neutral_count": ta.neutral_count,
                    "summary": ta.summary,
                    "indicators": [{"name": i.name, "value": i.value,
                        "signal": i.signal, "reason": i.reason} for i in ta.indicators]})
            except Exception as e:
                logger.debug("TA failed for %s: %s", sym, e)
    results.sort(key=lambda r: ({"Buy":0,"Neutral":1,"Sell":2}.get(r["composite_signal"],1), -r["confidence"]))
    return jsonify(_clean(results))


@app.route("/api/history")
def api_history():
    db = _get_db()
    analytics = AnalyticsEngine(db)
    df = analytics.filtered_history(
        sector=request.args.get("sector") or None,
        category=request.args.get("category") or None,
        status=request.args.get("status") or None,
        min_ai_score=float(request.args["min_score"]) if request.args.get("min_score") else None,
        min_confidence=float(request.args["min_confidence"]) if request.args.get("min_confidence") else None,
        date_from=request.args.get("date_from") or None,
        date_to=request.args.get("date_to") or None,
    )
    keep = ["symbol", "sector", "category", "recommendation_date", "overall_ai_score",
            "confidence_score", "status", "return_pct"]
    keep = [c for c in keep if c in df.columns]
    return jsonify(_clean(df[keep].to_dict(orient="records")))


@app.route("/api/run", methods=["POST"])
def api_run():
    with _job_lock:
        if _job_state["status"] == "running":
            return jsonify({"error": "A run is already in progress"}), 409
        _job_state.update(status="running", started_at=datetime.now().isoformat(),
                           finished_at=None, result=None, error=None, logs=[])

    body = request.get_json(silent=True) or {}
    provider_name = body.get("provider", "yfinance")
    news_enabled = bool(body.get("news", True))

    thread = threading.Thread(
        target=_run_pipeline_background, args=(provider_name, news_enabled), daemon=True
    )
    thread.start()
    return jsonify({"status": "started"})


@app.route("/api/run-status")
def api_run_status():
    with _job_lock:
        return jsonify(_clean(dict(_job_state)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
