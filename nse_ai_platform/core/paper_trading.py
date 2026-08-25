"""
core/paper_trading.py
Paper Trading Engine. NEVER places real trades -- this module only ever
writes to the local SQLite paper_trade table.

Responsibilities:
  - Auto-open a paper trade the moment a new recommendation is created.
  - Mark-to-market every open trade against latest prices each run.
  - Auto-close trades that hit stop-loss / targets / time-exit.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from db.schema import DatabaseManager
from core.models import ExitReason, TradeStatus

logger = logging.getLogger(__name__)

STARTING_CAPITAL_PER_TRADE = 100_000.0  # notional paper-money allocation per position


class PaperTradingEngine:
    def __init__(self, db: DatabaseManager):
        self.db = db

    @staticmethod
    def _log_snapshot(conn, trade_id: int, snapshot_date: str, price: float,
                       return_pct: float, days_held: int, status: str) -> None:
        """Appends (or updates, if this trade was already touched today) a
        row to paper_trade_history -- the day-by-day record of this trade's
        price/return, independent of whatever the LATEST state in
        paper_trade says. This is what lets you see how a recommendation
        evolved over time, not just where it stands right now."""
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO paper_trade_history
               (trade_id, snapshot_date, price, return_pct, days_held, status, created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(trade_id, snapshot_date) DO UPDATE SET
                 price=excluded.price, return_pct=excluded.return_pct,
                 days_held=excluded.days_held, status=excluded.status,
                 created_at=excluded.created_at""",
            (trade_id, snapshot_date, price, return_pct, days_held, status, now),
        )

    def open_trade_for_recommendation(self, recommendation_id: int, symbol: str,
                                       category: str, recommendation_date: str,
                                       buy_price: float, stop_loss: float,
                                       target_1: float, target_2: float, target_3: float,
                                       recommendation_version: int = 1) -> int:
        qty = max(1, int(STARTING_CAPITAL_PER_TRADE // buy_price))
        now = datetime.now().isoformat()
        with self.db.connect() as conn:
            cur = conn.execute(
                """INSERT INTO paper_trade
                   (recommendation_id, symbol, category, recommendation_date,
                    buy_price, current_price, stop_loss, target_1, target_2, target_3,
                    quantity, status, days_held, return_pct, max_gain_pct, max_loss_pct,
                    recommendation_version, opened_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'OPEN',0,0,0,0,?,?,?)""",
                (recommendation_id, symbol, category, recommendation_date,
                 buy_price, buy_price, stop_loss, target_1, target_2, target_3,
                 qty, recommendation_version, now, now),
            )
            trade_id = cur.lastrowid
            self._log_snapshot(conn, trade_id, date.today().isoformat(),
                                buy_price, 0.0, 0, "OPEN")
        logger.info("Opened paper trade #%s for %s @ %.2f (qty=%d)", trade_id, symbol, buy_price, qty)
        return trade_id

    def refresh_open_trade(self, trade_id: int, symbol: str, new_stop_loss: float,
                            new_target_1: float, new_target_2: float, new_target_3: float,
                            current_price: float, recommendation_version: int) -> None:
        """
        Called when a stock is re-recommended on a later day while it already
        has an open paper trade. Instead of spawning a duplicate trade, we:
          - Update the stop-loss and targets to the new recommendation's levels
            (the AI has re-evaluated and potentially tightened/widened them)
          - Record what changed in the daily snapshot so history is preserved
          - Log clearly: "position refreshed -- stop-loss moved X, targets updated"

        This handles both the "still growing" and "falling" cases:
          Growing: AI may raise stop-loss (trailing stop effect) and raise targets
          Falling: AI may tighten stop-loss closer to current price (reduce loss)
        """
        today = date.today().isoformat()
        now = datetime.now().isoformat()
        with self.db.connect() as conn:
            old = conn.execute(
                "SELECT stop_loss, target_1, target_2, target_3, buy_price, return_pct, days_held "
                "FROM paper_trade WHERE trade_id=?", (trade_id,)
            ).fetchone()
            if not old:
                return

            sl_moved = new_stop_loss - old["stop_loss"]
            conn.execute(
                """UPDATE paper_trade SET stop_loss=?, target_1=?, target_2=?, target_3=?,
                   current_price=?, recommendation_version=?, updated_at=? WHERE trade_id=?""",
                (new_stop_loss, new_target_1, new_target_2, new_target_3,
                 current_price, recommendation_version, now, trade_id),
            )
            # Preserve daily snapshot with the updated state
            return_pct = round((current_price - old["buy_price"]) / old["buy_price"] * 100, 2)
            self._log_snapshot(conn, trade_id, today, current_price, return_pct, old["days_held"], "OPEN")

        direction = "up" if sl_moved > 0 else "down" if sl_moved < 0 else "unchanged"
        logger.info(
            "Refreshed trade #%s (%s) on re-recommendation: stop-loss moved %s %.2f "
            "(%.2f -> %.2f), return=%.2f%%",
            trade_id, symbol, direction, abs(sl_moved), old["stop_loss"], new_stop_loss,
            return_pct,
        )

    def update_open_trades(self, latest_prices: dict[str, float]) -> dict:
        """
        Mark every OPEN trade to market using latest_prices[symbol].
        Auto-closes trades that hit stop-loss or any target.
        Returns summary counts.
        """
        updated, closed = 0, 0
        today = date.today().isoformat()
        now = datetime.now().isoformat()

        with self.db.connect() as conn:
            open_trades = conn.execute(
                "SELECT * FROM paper_trade WHERE status = 'OPEN'"
            ).fetchall()

            for t in open_trades:
                symbol = t["symbol"]
                if symbol not in latest_prices:
                    continue
                price = latest_prices[symbol]
                buy_price = t["buy_price"]
                return_pct = round((price - buy_price) / buy_price * 100, 2)

                days_held = (date.fromisoformat(today) - date.fromisoformat(t["recommendation_date"])).days
                max_gain = max(t["max_gain_pct"], return_pct)
                max_loss = min(t["max_loss_pct"], return_pct)

                exit_reason = None
                if price <= t["stop_loss"]:
                    exit_reason = ExitReason.STOPLOSS.value
                elif t["target_3"] and price >= t["target_3"]:
                    exit_reason = ExitReason.TARGET3.value
                elif t["target_2"] and price >= t["target_2"]:
                    exit_reason = ExitReason.TARGET2.value
                elif t["target_1"] and price >= t["target_1"]:
                    exit_reason = ExitReason.TARGET1.value
                elif days_held >= 730:  # 2-year hard time-stop safety valve
                    exit_reason = ExitReason.TIME_EXIT.value

                if exit_reason:
                    conn.execute(
                        """UPDATE paper_trade SET current_price=?, status='CLOSED',
                           days_held=?, return_pct=?, max_gain_pct=?, max_loss_pct=?,
                           exit_price=?, exit_date=?, exit_reason=?, updated_at=?
                           WHERE trade_id=?""",
                        (price, days_held, return_pct, max_gain, max_loss,
                         price, today, exit_reason, now, t["trade_id"]),
                    )
                    self._log_snapshot(conn, t["trade_id"], today, price, return_pct, days_held, "CLOSED")
                    closed += 1
                    logger.info("Closed trade #%s (%s) reason=%s return=%.2f%%",
                                t["trade_id"], symbol, exit_reason, return_pct)
                else:
                    conn.execute(
                        """UPDATE paper_trade SET current_price=?, days_held=?,
                           return_pct=?, max_gain_pct=?, max_loss_pct=?, updated_at=?
                           WHERE trade_id=?""",
                        (price, days_held, return_pct, max_gain, max_loss, now, t["trade_id"]),
                    )
                    self._log_snapshot(conn, t["trade_id"], today, price, return_pct, days_held, "OPEN")
                    updated += 1

        return {"updated": updated, "closed": closed}

    def record_daily_equity(self) -> None:
        """Snapshot today's total portfolio value (cash-equivalent) for the equity curve chart."""
        today = date.today().isoformat()
        with self.db.connect() as conn:
            open_trades = conn.execute(
                "SELECT buy_price, current_price, quantity FROM paper_trade WHERE status='OPEN'"
            ).fetchall()
            closed_trades = conn.execute(
                "SELECT buy_price, exit_price, quantity FROM paper_trade WHERE status='CLOSED'"
            ).fetchall()

            unrealized = sum((t["current_price"] - t["buy_price"]) * t["quantity"] for t in open_trades)
            realized = sum((t["exit_price"] - t["buy_price"]) * t["quantity"]
                            for t in closed_trades if t["exit_price"] is not None)
            open_value = sum(t["current_price"] * t["quantity"] for t in open_trades)

            portfolio_value = open_value + realized

            conn.execute(
                """INSERT INTO portfolio_equity (date, portfolio_value, realized_pnl,
                   unrealized_pnl, open_trade_count, closed_trade_count)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(date) DO UPDATE SET
                     portfolio_value=excluded.portfolio_value,
                     realized_pnl=excluded.realized_pnl,
                     unrealized_pnl=excluded.unrealized_pnl,
                     open_trade_count=excluded.open_trade_count,
                     closed_trade_count=excluded.closed_trade_count""",
                (today, portfolio_value, realized, unrealized, len(open_trades), len(closed_trades)),
            )
