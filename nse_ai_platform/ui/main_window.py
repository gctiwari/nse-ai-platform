"""
ui/main_window.py
PySide6 desktop UI. Dark theme, dashboard header, 4 recommendation tabs
(Watchlist/Conservative/Balanced/Aggressive) with rank-shaded cards,
Paper Trading dashboard tab, and a filterable History tab.

Run via: python main.py   (see project root)

NOTE ON THIS SANDBOX: PySide6 cannot be installed here (no network access
in this environment), so this file has been written carefully against the
already-tested backend (db/core modules) but could not be executed here.
Install locally with `pip install PySide6` and run `python main.py`.
"""

from __future__ import annotations

import sys
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QTabWidget, QScrollArea, QFrame, QLineEdit, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QSplitter, QMessageBox,
    QProgressDialog,
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QColor

from db.schema import DatabaseManager
from db.repository import RecommendationRepository
from core.analytics import AnalyticsEngine
from core.recommendation_engine import rank_to_color_hex, MAX_PER_CATEGORY
from core.models import Category
import run_pipeline

DARK_BG = "#121212"
PANEL_BG = "#1c1c1c"
CARD_BORDER = "#2a2a2a"
TEXT_PRIMARY = "#e8e8e8"
TEXT_SECONDARY = "#9a9a9a"
ACCENT = "#4f8cff"
POSITIVE = "#3ddc84"
NEGATIVE = "#ff5c5c"

STYLE_SHEET = f"""
QMainWindow, QWidget {{ background-color: {DARK_BG}; color: {TEXT_PRIMARY}; font-family: 'Segoe UI', Arial; }}
QTabWidget::pane {{ border: 1px solid {CARD_BORDER}; background: {PANEL_BG}; }}
QTabBar::tab {{ background: {PANEL_BG}; color: {TEXT_SECONDARY}; padding: 10px 18px; border: 1px solid {CARD_BORDER}; }}
QTabBar::tab:selected {{ background: {ACCENT}; color: white; }}
QLineEdit, QComboBox {{ background: {PANEL_BG}; border: 1px solid {CARD_BORDER}; padding: 6px; border-radius: 4px; color: {TEXT_PRIMARY}; }}
QPushButton {{ background: {ACCENT}; color: white; border: none; padding: 8px 16px; border-radius: 4px; font-weight: bold; }}
QPushButton:hover {{ background: #6ea0ff; }}
QTableWidget {{ background: {PANEL_BG}; gridline-color: {CARD_BORDER}; border: 1px solid {CARD_BORDER}; }}
QHeaderView::section {{ background: {PANEL_BG}; color: {TEXT_SECONDARY}; padding: 6px; border: 1px solid {CARD_BORDER}; }}
QScrollArea {{ border: none; }}
"""


class RunPipelineThread(QThread):
    """Runs the (potentially slow) fetch/analyze/recommend pipeline off the UI thread."""
    finished_ok = Signal(dict)
    failed = Signal(str)

    def run(self):
        try:
            result = run_pipeline.run(data_provider_name="yfinance")
            self.finished_ok.emit(result)
        except Exception as e:
            self.failed.emit(str(e))


class RecommendationCard(QFrame):
    """A single recommendation card with rank-based color shading."""

    def __init__(self, rec: dict, category: Category):
        super().__init__()
        color = rank_to_color_hex(category, rec["rank_in_category"], MAX_PER_CATEGORY)
        text_color = "#ffffff" if rec["rank_in_category"] <= MAX_PER_CATEGORY // 2 else "#1a1a1a"
        self.setStyleSheet(f"""
            QFrame {{ background-color: {color}; border-radius: 8px; }}
            QLabel {{ color: {text_color}; background: transparent; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)

        header = QHBoxLayout()
        rank_lbl = QLabel(f"#{rec['rank_in_category']}")
        rank_lbl.setFont(QFont("Segoe UI", 11, QFont.Bold))
        name_lbl = QLabel(f"{rec['company_name']} ({rec['symbol']})")
        name_lbl.setFont(QFont("Segoe UI", 12, QFont.Bold))
        header.addWidget(rank_lbl)
        header.addWidget(name_lbl, 1)
        score_lbl = QLabel(f"AI Score: {rec['overall_ai_score']:.1f}")
        score_lbl.setFont(QFont("Segoe UI", 11, QFont.Bold))
        header.addWidget(score_lbl)
        layout.addLayout(header)

        grid = QGridLayout()
        fields = [
            ("CMP", f"₹{rec['current_price']:.2f}"),
            ("Buy Range", f"₹{rec['buy_range_low']:.2f} - ₹{rec['buy_range_high']:.2f}"),
            ("Stop Loss", f"₹{rec['stop_loss']:.2f}"),
            ("Target 1", f"₹{rec['target_1']:.2f}"),
            ("Target 2", f"₹{rec['target_2']:.2f}"),
            ("Target 3", f"₹{rec['target_3']:.2f}"),
            ("Fair Value", f"₹{rec['fair_value']:.2f}"),
            ("Margin of Safety", f"{rec['margin_of_safety']:.1f}%"),
            ("Expected Return", f"{rec['expected_return_pct']:.1f}%"),
            ("Confidence", f"{rec['confidence_score']:.1f}"),
            ("Risk Level", rec["risk_level"]),
            ("Risk:Reward", f"1:{rec['risk_reward_ratio']:.2f}"),
            ("Horizon", rec["investment_horizon"]),
        ]
        for i, (label, value) in enumerate(fields):
            r, c = divmod(i, 2)
            lbl = QLabel(f"<b>{label}:</b> {value}")
            lbl.setFont(QFont("Segoe UI", 9))
            grid.addWidget(lbl, r, c)
        layout.addLayout(grid)

        reason_lbl = QLabel(rec["ai_explanation"])
        reason_lbl.setWordWrap(True)
        reason_lbl.setFont(QFont("Segoe UI", 9))
        layout.addWidget(reason_lbl)

        # News sentiment section
        import json as _json
        news_impact = rec.get("news_overall_impact", "Neutral")
        impact_color = {"Bullish": "#2e7d32", "Bearish": "#c62828"}.get(news_impact, "#616161")
        news_header = QLabel(
            f"📰 News: {rec.get('news_sentiment_label', 'Neutral')} "
            f"(score {rec.get('news_sentiment_score', 0):+.2f}, {news_impact}, "
            f"{rec.get('news_article_count', 0)} articles)"
        )
        news_header.setFont(QFont("Segoe UI", 9, QFont.Bold))
        news_header.setStyleSheet(f"background: {impact_color}; color: white; padding: 3px; border-radius: 3px;")
        layout.addWidget(news_header)

        if rec.get("news_summary"):
            news_summary_lbl = QLabel(rec["news_summary"])
            news_summary_lbl.setWordWrap(True)
            news_summary_lbl.setFont(QFont("Segoe UI", 8))
            layout.addWidget(news_summary_lbl)

        try:
            headlines = _json.loads(rec["news_headlines"]) if isinstance(rec.get("news_headlines"), str) else (rec.get("news_headlines") or [])
        except Exception:
            headlines = []
        for h in headlines[:5]:
            hl = QLabel(f"• {h.get('title', '')} ({h.get('source', '')})")
            hl.setWordWrap(True)
            hl.setFont(QFont("Segoe UI", 8))
            layout.addWidget(hl)

        tech_lbl = QLabel(
            f"Pivot {rec['pivot_point']:.1f} | S1 {rec['s1']:.1f} R1 {rec['r1']:.1f} | "
            f"52W H/L {rec['week52_high']:.1f}/{rec['week52_low']:.1f}"
        )
        tech_lbl.setFont(QFont("Segoe UI", 8))
        layout.addWidget(tech_lbl)


class CategoryTab(QWidget):
    """Scrollable list of recommendation cards for one category."""

    def __init__(self, category: Category, repo: RecommendationRepository):
        super().__init__()
        self.category = category
        self.repo = repo
        outer = QVBoxLayout(self)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        outer.addWidget(self.scroll)
        self.refresh()

    def refresh(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        recs = self.repo.get_by_category(self.category.value)
        if not recs:
            empty = QLabel(f"No {self.category.value} recommendations yet. Run the pipeline to generate some.")
            empty.setAlignment(Qt.AlignCenter)
            layout.addWidget(empty)
        else:
            for rec in recs:
                layout.addWidget(RecommendationCard(rec, self.category))
        layout.addStretch()
        self.scroll.setWidget(container)


class DashboardHeader(QWidget):
    """Top strip: date/time, market status, indices, top picks summary."""

    def __init__(self, repo: RecommendationRepository):
        super().__init__()
        self.repo = repo
        self.layout_ = QVBoxLayout(self)
        self.info_row = QHBoxLayout()
        self.layout_.addLayout(self.info_row)
        self.indices_row = QHBoxLayout()
        self.layout_.addLayout(self.indices_row)
        self.refresh()

    def refresh(self, index_quotes: dict | None = None, market_status: str | None = None):
        for lay in (self.info_row, self.indices_row):
            while lay.count():
                item = lay.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

        dt_lbl = QLabel(datetime.now().strftime("%A, %d %B %Y  %H:%M:%S"))
        dt_lbl.setFont(QFont("Segoe UI", 13, QFont.Bold))
        self.info_row.addWidget(dt_lbl)
        self.info_row.addStretch()
        status_lbl = QLabel(f"Market: {market_status or '—'}")
        status_lbl.setStyleSheet(f"color: {POSITIVE if market_status == 'Open' else TEXT_SECONDARY}")
        self.info_row.addWidget(status_lbl)

        if index_quotes:
            for name, q in index_quotes.items():
                color = POSITIVE if q["change_pct"] >= 0 else NEGATIVE
                lbl = QLabel(f"{name}: {q['value']:,.2f}  ({q['change_pct']:+.2f}%)")
                lbl.setStyleSheet(f"color: {color}; padding: 4px 10px;")
                self.indices_row.addWidget(lbl)
        self.indices_row.addStretch()


class PaperTradingTab(QWidget):
    def __init__(self, analytics: AnalyticsEngine):
        super().__init__()
        self.analytics = analytics
        self.layout_ = QVBoxLayout(self)
        self.stats_grid = QGridLayout()
        self.layout_.addLayout(self.stats_grid)
        self.table = QTableWidget()
        self.layout_.addWidget(self.table)
        self.refresh()

    def refresh(self):
        while self.stats_grid.count():
            item = self.stats_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        s = self.analytics.dashboard_summary()
        stats = [
            ("Open Trades", s["open_trades"]), ("Closed Trades", s["closed_trades"]),
            ("Win Rate", f"{s['win_rate_pct']}%"), ("Total Recos", s["total_recommendations"]),
            ("Winning", s["winning_trades"]), ("Losing", s["losing_trades"]),
            ("Avg Return", f"{s['avg_return_pct']}%"), ("Avg Hold (days)", s["avg_holding_days"]),
        ]
        for i, (label, value) in enumerate(stats):
            r, c = divmod(i, 5)
            box = QFrame()
            box.setStyleSheet(f"background: {PANEL_BG}; border: 1px solid {CARD_BORDER}; border-radius: 6px;")
            bl = QVBoxLayout(box)
            vlbl = QLabel(str(value))
            vlbl.setFont(QFont("Segoe UI", 14, QFont.Bold))
            llbl = QLabel(label)
            llbl.setStyleSheet(f"color: {TEXT_SECONDARY}")
            bl.addWidget(vlbl)
            bl.addWidget(llbl)
            self.stats_grid.addWidget(box, r, c)

        df = self.analytics._trades_df()
        cols = ["symbol", "category", "status", "buy_price", "current_price",
                "return_pct", "days_held", "exit_reason"]
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels([c.replace("_", " ").title() for c in cols])
        self.table.setRowCount(len(df))
        for i, row in df.iterrows():
            for j, col in enumerate(cols):
                val = row[col]
                item = QTableWidgetItem("" if val is None else str(val))
                if col == "return_pct" and val is not None:
                    item.setForeground(QColor(POSITIVE if val >= 0 else NEGATIVE))
                self.table.setItem(i, j, item)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)


class HistoryTab(QWidget):
    def __init__(self, analytics: AnalyticsEngine):
        super().__init__()
        self.analytics = analytics
        layout = QVBoxLayout(self)

        filters = QHBoxLayout()
        self.category_filter = QComboBox()
        self.category_filter.addItems(["All"] + [c.value for c in Category])
        self.status_filter = QComboBox()
        self.status_filter.addItems(["All", "OPEN", "CLOSED"])
        self.min_score = QLineEdit()
        self.min_score.setPlaceholderText("Min AI Score")
        apply_btn = QPushButton("Apply Filters")
        apply_btn.clicked.connect(self.refresh)
        filters.addWidget(QLabel("Category:")); filters.addWidget(self.category_filter)
        filters.addWidget(QLabel("Status:")); filters.addWidget(self.status_filter)
        filters.addWidget(self.min_score)
        filters.addWidget(apply_btn)
        layout.addLayout(filters)

        self.table = QTableWidget()
        layout.addWidget(self.table)
        self.refresh()

    def refresh(self):
        cat = None if self.category_filter.currentText() == "All" else self.category_filter.currentText()
        status = None if self.status_filter.currentText() == "All" else self.status_filter.currentText()
        min_score = float(self.min_score.text()) if self.min_score.text().strip() else None

        df = self.analytics.filtered_history(category=cat, status=status, min_ai_score=min_score)
        cols = ["symbol", "sector", "category", "recommendation_date", "overall_ai_score",
                "confidence_score", "status", "return_pct"]
        cols = [c for c in cols if c in df.columns]
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels([c.replace("_", " ").title() for c in cols])
        self.table.setRowCount(len(df))
        for i, (_, row) in enumerate(df.iterrows()):
            for j, col in enumerate(cols):
                val = row[col]
                self.table.setItem(i, j, QTableWidgetItem("" if val is None else str(val)))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI Stock Recommendation & Paper Trading Platform (NSE/BSE) — Paper Trading Only")
        self.resize(1400, 900)

        self.db = DatabaseManager()
        self.repo = RecommendationRepository(self.db)
        self.analytics = AnalyticsEngine(self.db)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        banner = QLabel("⚠ PAPER TRADING ONLY — This application NEVER places real trades.")
        banner.setStyleSheet(f"background: {NEGATIVE}; color: white; padding: 6px; font-weight: bold;")
        banner.setAlignment(Qt.AlignCenter)
        root.addWidget(banner)

        top_bar = QHBoxLayout()
        self.header = DashboardHeader(self.repo)
        top_bar.addWidget(self.header, 1)
        self.run_btn = QPushButton("Run AI Analysis Now")
        self.run_btn.clicked.connect(self.run_pipeline_clicked)
        top_bar.addWidget(self.run_btn)
        root.addLayout(top_bar)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        self.category_tabs: dict[Category, CategoryTab] = {}
        for cat in [Category.WATCHLIST, Category.CONSERVATIVE, Category.BALANCED, Category.AGGRESSIVE]:
            tab = CategoryTab(cat, self.repo)
            self.category_tabs[cat] = tab
            self.tabs.addTab(tab, cat.value)

        self.paper_trading_tab = PaperTradingTab(self.analytics)
        self.tabs.addTab(self.paper_trading_tab, "Paper Trading")

        self.history_tab = HistoryTab(self.analytics)
        self.tabs.addTab(self.history_tab, "History")

        self.setStyleSheet(STYLE_SHEET)

    def run_pipeline_clicked(self):
        self.run_btn.setEnabled(False)
        self.progress = QProgressDialog("Fetching data & generating AI recommendations...", None, 0, 0, self)
        self.progress.setWindowModality(Qt.WindowModal)
        self.progress.show()

        self.thread = RunPipelineThread()
        self.thread.finished_ok.connect(self.on_pipeline_done)
        self.thread.failed.connect(self.on_pipeline_failed)
        self.thread.start()

    def on_pipeline_done(self, result: dict):
        self.progress.close()
        self.run_btn.setEnabled(True)
        self.header.refresh(result["index_quotes"], result["market_status"])
        for tab in self.category_tabs.values():
            tab.refresh()
        self.paper_trading_tab.refresh()
        self.history_tab.refresh()
        QMessageBox.information(
            self, "Run Complete",
            f"Scanned {result['stocks_scanned']} stocks "
            f"({result.get('universe_info', {}).get('label', '?')}).\n"
            f"{result['recommendations_created']} recommendations generated.\n"
            f"{result['trades_opened']} new paper trades opened.\n"
            f"{result['trades_closed']} trades closed this run."
        )

    def on_pipeline_failed(self, error: str):
        self.progress.close()
        self.run_btn.setEnabled(True)
        QMessageBox.critical(self, "Error", f"Pipeline run failed:\n{error}")


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE_SHEET)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
