"""
core/analytics.py
Analytics & Reporting over the paper_trade / recommendation tables.
Read-only: computes everything the Paper Trading Dashboard and History
tab need directly from SQLite via pandas.
"""

from __future__ import annotations

import logging
import pandas as pd

from db.schema import DatabaseManager

logger = logging.getLogger(__name__)


class AnalyticsEngine:
    def __init__(self, db: DatabaseManager):
        self.db = db

    def _trades_df(self) -> pd.DataFrame:
        with self.db.connect() as conn:
            return pd.read_sql_query("SELECT * FROM paper_trade", conn)

    def trade_history(self, trade_id: int) -> pd.DataFrame:
        """Day-by-day price/return snapshots for one paper trade (see
        core/paper_trading.py's _log_snapshot) -- independent of the
        trade's current/latest state, so nothing is lost across runs."""
        with self.db.connect() as conn:
            return pd.read_sql_query(
                "SELECT * FROM paper_trade_history WHERE trade_id=? ORDER BY snapshot_date",
                conn, params=(trade_id,),
            )

    def _recos_df(self) -> pd.DataFrame:
        with self.db.connect() as conn:
            return pd.read_sql_query("SELECT * FROM recommendation", conn)

    def dashboard_summary(self) -> dict:
        """NOTE: deliberately does NOT compute a notional portfolio
        value/P&L -- this platform tracks call performance (did the
        recommendation work out, by how much, how fast), not a simulated
        capital account. Quantity/currency amounts are intentionally out
        of scope here."""
        df = self._trades_df()
        if df.empty:
            return {
                "open_trades": 0, "closed_trades": 0, "win_rate_pct": 0.0,
                "total_recommendations": 0, "winning_trades": 0, "losing_trades": 0,
                "avg_return_pct": 0.0, "avg_holding_days": 0.0,
                "best_trade": None, "worst_trade": None,
            }

        open_df = df[df.status == "OPEN"]
        closed_df = df[df.status == "CLOSED"]
        winners = closed_df[closed_df.return_pct > 0]
        losers = closed_df[closed_df.return_pct <= 0]

        win_rate = round(len(winners) / len(closed_df) * 100, 1) if len(closed_df) else 0.0

        best = closed_df.loc[closed_df.return_pct.idxmax()] if not closed_df.empty else None
        worst = closed_df.loc[closed_df.return_pct.idxmin()] if not closed_df.empty else None

        return {
            "open_trades": int(len(open_df)),
            "closed_trades": int(len(closed_df)),
            "win_rate_pct": win_rate,
            "total_recommendations": int(len(df)),
            "winning_trades": int(len(winners)),
            "losing_trades": int(len(losers)),
            "avg_return_pct": round(closed_df.return_pct.mean(), 2) if not closed_df.empty else 0.0,
            "avg_holding_days": round(closed_df.days_held.mean(), 1) if not closed_df.empty else 0.0,
            "best_trade": None if best is None else {"symbol": best.symbol, "return_pct": best.return_pct},
            "worst_trade": None if worst is None else {"symbol": worst.symbol, "return_pct": worst.return_pct},
        }

    def monthly_performance(self) -> pd.DataFrame:
        df = self._trades_df()
        closed = df[df.status == "CLOSED"].copy()
        if closed.empty:
            return pd.DataFrame(columns=["month", "trades", "win_rate_pct", "avg_return_pct"])
        closed["exit_month"] = pd.to_datetime(closed.exit_date).dt.to_period("M").astype(str)
        grouped = closed.groupby("exit_month").agg(
            trades=("trade_id", "count"),
            win_rate_pct=("return_pct", lambda s: round((s > 0).mean() * 100, 1)),
            avg_return_pct=("return_pct", lambda s: round(s.mean(), 2)),
        ).reset_index().rename(columns={"exit_month": "month"})
        return grouped

    def yearly_performance(self) -> pd.DataFrame:
        df = self._trades_df()
        closed = df[df.status == "CLOSED"].copy()
        if closed.empty:
            return pd.DataFrame(columns=["year", "trades", "win_rate_pct", "avg_return_pct"])
        closed["exit_year"] = pd.to_datetime(closed.exit_date).dt.year
        grouped = closed.groupby("exit_year").agg(
            trades=("trade_id", "count"),
            win_rate_pct=("return_pct", lambda s: round((s > 0).mean() * 100, 1)),
            avg_return_pct=("return_pct", lambda s: round(s.mean(), 2)),
        ).reset_index().rename(columns={"exit_year": "year"})
        return grouped

    def filtered_history(self, *, sector: str | None = None, category: str | None = None,
                          status: str | None = None, min_ai_score: float | None = None,
                          min_confidence: float | None = None,
                          date_from: str | None = None, date_to: str | None = None,
                          winning_only: bool | None = None) -> pd.DataFrame:
        recos = self._recos_df()
        trades = self._trades_df()
        merged = recos.merge(trades, on="recommendation_id", how="left", suffixes=("", "_trade"))

        if sector:
            merged = merged[merged.sector == sector]
        if category:
            merged = merged[merged.category == category]
        if status:
            merged = merged[merged.status == status]
        if min_ai_score is not None:
            merged = merged[merged.overall_ai_score >= min_ai_score]
        if min_confidence is not None:
            merged = merged[merged.confidence_score >= min_confidence]
        if date_from:
            merged = merged[merged.recommendation_date >= date_from]
        if date_to:
            merged = merged[merged.recommendation_date <= date_to]
        if winning_only is True:
            merged = merged[merged.return_pct > 0]
        elif winning_only is False:
            merged = merged[merged.return_pct <= 0]

        return merged
