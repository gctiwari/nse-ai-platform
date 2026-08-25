"""
data/providers.py
Data Collection layer.

DataProvider is an abstract interface. Everything downstream (feature
engineering, scoring, recommendations) only talks to this interface, so
swapping data sources never touches business logic.

Two implementations are given:
  - DemoDataProvider: deterministic-but-randomized synthetic data so the
    whole pipeline is runnable/testable with zero network access. Uses a
    small fixed 41-stock list (FALLBACK_UNIVERSE below) since there's no
    point simulating fake data for 500 fake companies.
  - YFinanceDataProvider: real data via `yfinance`. Scans the live NIFTY
    500 constituent list, fetched from NSE Indices at runtime and cached
    locally (nifty500_universe_cache.json) so it isn't re-downloaded every
    run -- the index only rebalances twice a year. Falls back to
    FALLBACK_UNIVERSE if the live fetch ever fails (no internet, NSE
    changed their URL, etc.) so the app never breaks outright.

Future: add NSEDataProvider / broker API providers here without touching
anything else in the codebase.
"""

from __future__ import annotations

import abc
import csv
import io
import json
import logging
import random
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from core.models import MarketSnapshot

logger = logging.getLogger(__name__)

# Small, hand-picked universe across sectors/market-cap bands. Used as (a)
# the entire universe for DemoDataProvider (offline testing), and (b) the
# safety-net fallback for YFinanceDataProvider if the live NIFTY 500 list
# can't be fetched. Market cap category is no longer hardcoded here -- with
# up to 500 real stocks there's no practical way to hand-tag each one, so
# it's computed dynamically from each stock's actual fetched market cap
# (see core.recommendation_engine.classify_market_cap).
FALLBACK_UNIVERSE = [
    ("RELIANCE", "Reliance Industries Ltd", "Energy"),
    ("TCS", "Tata Consultancy Services Ltd", "IT"),
    ("HDFCBANK", "HDFC Bank Ltd", "Banking"),
    ("ICICIBANK", "ICICI Bank Ltd", "Banking"),
    ("INFY", "Infosys Ltd", "IT"),
    ("BHARTIARTL", "Bharti Airtel Ltd", "Telecom"),
    ("SBIN", "State Bank of India", "Banking"),
    ("LT", "Larsen & Toubro Ltd", "Infrastructure"),
    ("HINDUNILVR", "Hindustan Unilever Ltd", "FMCG"),
    ("ITC", "ITC Ltd", "FMCG"),
    ("KOTAKBANK", "Kotak Mahindra Bank Ltd", "Banking"),
    ("AXISBANK", "Axis Bank Ltd", "Banking"),
    ("BAJFINANCE", "Bajaj Finance Ltd", "NBFC"),
    ("MARUTI", "Maruti Suzuki India Ltd", "Auto"),
    ("SUNPHARMA", "Sun Pharmaceutical Industries Ltd", "Pharma"),
    ("TITAN", "Titan Company Ltd", "Consumer Durables"),
    ("NTPC", "NTPC Ltd", "Power"),
    ("NHPC", "NHPC Ltd", "Power"),
    ("POWERGRID", "Power Grid Corporation of India Ltd", "Power"),
    ("ONGC", "Oil & Natural Gas Corporation Ltd", "Energy"),
    ("COALINDIA", "Coal India Ltd", "Mining"),
    ("ADANIPORTS", "Adani Ports & SEZ Ltd", "Infrastructure"),
    ("TATASTEEL", "Tata Steel Ltd", "Metals"),
    ("JSWSTEEL", "JSW Steel Ltd", "Metals"),
    ("BANDHANBNK", "Bandhan Bank Ltd", "Banking"),
    ("FEDERALBNK", "Federal Bank Ltd", "Banking"),
    ("IDFCFIRSTB", "IDFC First Bank Ltd", "Banking"),
    ("PFC", "Power Finance Corporation Ltd", "NBFC"),
    ("RECLTD", "REC Ltd", "NBFC"),
    ("SJVN", "SJVN Ltd", "Power"),
    ("TATAPOWER", "Tata Power Company Ltd", "Power"),
    ("IRFC", "Indian Railway Finance Corporation Ltd", "NBFC"),
    ("ETERNAL", "Eternal Ltd (formerly Zomato)", "Internet"),
    ("NYKAA", "FSN E-Commerce (Nykaa)", "Internet"),
    ("DIXON", "Dixon Technologies Ltd", "Electronics Mfg"),
    ("PERSISTENT", "Persistent Systems Ltd", "IT"),
    ("POLYCAB", "Polycab India Ltd", "Capital Goods"),
    ("CUMMINSIND", "Cummins India Ltd", "Capital Goods"),
    ("IEX", "Indian Energy Exchange Ltd", "Power"),
    ("SUZLON", "Suzlon Energy Ltd", "Power"),
    ("IRCTC", "Indian Railway Catering & Tourism Corp", "Travel"),
]

# --- NIFTY 500 live universe fetch ------------------------------------

# NSE Indices' standard public download path for index constituent lists
# (the same pattern serves ind_nifty50list.csv, ind_nifty100list.csv,
# ind_nifty500list.csv, etc). Tried in order; first one that responds with
# a parseable CSV wins.
NIFTY500_CSV_URLS = [
    "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
]
NIFTY500_CACHE_PATH = Path(__file__).resolve().parent.parent / "data_store" / "nifty500_universe_cache.json"
NIFTY500_CACHE_MAX_AGE_DAYS = 7  # index rebalances twice a year; no need to refetch often
NIFTY500_HTTP_TIMEOUT_SEC = 15
NIFTY500_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# NSE's own "Industry" labels (dozens of them, e.g. "FERTILISERS &
# PESTICIDES", "CEMENT & CEMENT PRODUCTS") are far more granular than the
# sector buckets the rest of this app uses for valuation/news
# classification. This maps common substrings down to a manageable
# internal taxonomy; anything unmatched falls back to a title-cased
# version of NSE's own label, so nothing is silently dropped -- it just
# won't have a tuned SECTOR_AVG_PE entry (uses DEFAULT_SECTOR_PE instead).
_SECTOR_KEYWORD_MAP = [
    (("bank",), "Banking"),
    (("nbfc", "finance", "housing finance"), "NBFC"),
    (("software", "it services", "information technology"), "IT"),
    (("power", "electric utilities"), "Power"),
    (("oil", "gas", "petroleum", "refiner"), "Energy"),
    (("telecom",), "Telecom"),
    (("construction", "infrastructure", "engineering"), "Infrastructure"),
    (("fmcg", "consumer non durables", "food products", "personal products"), "FMCG"),
    (("auto", "vehicle", "tyres"), "Auto"),
    (("pharma", "healthcare services", "hospital"), "Pharma"),
    (("consumer durables", "household"), "Consumer Durables"),
    (("mining", "coal"), "Mining"),
    (("steel", "metals", "aluminium", "mining & metals"), "Metals"),
    (("internet", "e-commerce", "retailing"), "Internet"),
    (("electronics", "consumer electronics"), "Electronics Mfg"),
    (("capital goods", "industrial products", "industrial manufacturing"), "Capital Goods"),
    (("travel", "aviation", "hotel", "tourism"), "Travel"),
    (("cement",), "Cement"),
    (("chemicals", "fertilisers", "pesticides", "agro"), "Chemicals"),
    (("textile",), "Textiles"),
    (("realty", "real estate"), "Realty"),
    (("media", "entertainment"), "Media"),
    (("insurance",), "Insurance"),
    (("capital markets", "financial services"), "Financial Services"),
    (("paper", "forest"), "Paper"),
    (("sugar", "agricultural food"), "Agri"),
    (("gas distribution", "city gas"), "Gas"),
    (("diversified",), "Diversified"),
]


def normalize_sector(raw_industry: str) -> str:
    """Maps an NSE 'Industry' label to this app's internal sector taxonomy."""
    if not raw_industry:
        return "Diversified"
    lower = raw_industry.lower()
    for keywords, bucket in _SECTOR_KEYWORD_MAP:
        if any(k in lower for k in keywords):
            return bucket
    return raw_industry.strip().title()


def fetch_nifty500_constituents(force_refresh: bool = False) -> tuple[list[tuple[str, str, str]], str, str | None] | tuple[None, None, None]:
    """
    Fetches the live NIFTY 500 constituent list from NSE Indices, with a
    local JSON cache (refreshed at most every NIFTY500_CACHE_MAX_AGE_DAYS
    days, since the index only rebalances twice a year).

    Returns (constituents, source_label, fetched_at_iso):
      - source_label is one of "cached", "live", "stale_cache"
      - Returns (None, None, None) if both the network fetch AND any
        existing cache are unavailable -- callers should fall back to
        FALLBACK_UNIVERSE in that case.
    """
    if not force_refresh and NIFTY500_CACHE_PATH.exists():
        try:
            cached = json.loads(NIFTY500_CACHE_PATH.read_text())
            cached_at = datetime.fromisoformat(cached["fetched_at"])
            if (datetime.now() - cached_at).days < NIFTY500_CACHE_MAX_AGE_DAYS:
                logger.info("Using cached NIFTY 500 list from %s (%d symbols)",
                            cached_at.date(), len(cached["symbols"]))
                return [tuple(row) for row in cached["symbols"]], "cached", cached["fetched_at"]
        except Exception as e:
            logger.warning("NIFTY 500 cache unreadable (%s), will try a fresh fetch", e)

    for url in NIFTY500_CSV_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": NIFTY500_USER_AGENT})
            with urllib.request.urlopen(req, timeout=NIFTY500_HTTP_TIMEOUT_SEC) as resp:
                raw = resp.read().decode("utf-8-sig", errors="replace")
            reader = csv.DictReader(io.StringIO(raw))
            constituents = []
            for row in reader:
                # NSE's CSV header is typically: Company Name,Industry,Symbol,Series,ISIN Code
                symbol = (row.get("Symbol") or "").strip()
                company = (row.get("Company Name") or "").strip()
                industry = (row.get("Industry") or "").strip()
                if not symbol or not company:
                    continue
                constituents.append((symbol, company, normalize_sector(industry)))

            if len(constituents) < 400:  # sanity check -- a real Nifty 500 pull should be ~500 rows
                logger.warning("Fetched only %d rows from %s -- looks malformed, trying next source",
                                len(constituents), url)
                continue

            fetched_at = datetime.now().isoformat()
            NIFTY500_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            NIFTY500_CACHE_PATH.write_text(json.dumps({
                "fetched_at": fetched_at,
                "source_url": url,
                "symbols": constituents,
            }))
            logger.info("Fetched live NIFTY 500 list from %s: %d symbols (cached for %d days)",
                        url, len(constituents), NIFTY500_CACHE_MAX_AGE_DAYS)
            return constituents, "live", fetched_at
        except Exception as e:
            logger.warning("NIFTY 500 fetch failed from %s: %s", url, e)
            continue

    # Every source failed -- fall back to a STALE cache if one exists,
    # rather than nothing at all.
    if NIFTY500_CACHE_PATH.exists():
        try:
            cached = json.loads(NIFTY500_CACHE_PATH.read_text())
            logger.warning("All live fetches failed; using stale cache from %s", cached["fetched_at"])
            return [tuple(row) for row in cached["symbols"]], "stale_cache", cached["fetched_at"]
        except Exception:
            pass

    logger.warning("Could not fetch NIFTY 500 list from any source and no cache exists -- "
                    "falling back to the small %d-stock FALLBACK_UNIVERSE", len(FALLBACK_UNIVERSE))
    return None, None, None


# Fallback "typical" sector P/E used only when a live provider can't supply
# a real sector-average PE (yfinance has no such field). Used as a neutral
# valuation reference point rather than leaving valuation scoring blind.
# Rough, indicative figures for the Indian market -- update periodically.
SECTOR_AVG_PE = {
    "Banking": 16, "NBFC": 20, "IT": 26, "Energy": 12, "Telecom": 30,
    "Infrastructure": 25, "FMCG": 45, "Auto": 24, "Pharma": 28,
    "Consumer Durables": 60, "Power": 18, "Mining": 8, "Metals": 11,
    "Internet": 80, "Electronics Mfg": 55, "Capital Goods": 35, "Travel": 40,
    "Cement": 28, "Chemicals": 24, "Textiles": 20, "Realty": 30, "Media": 25,
    "Insurance": 35, "Financial Services": 22, "Paper": 14, "Agri": 18,
    "Gas": 20, "Diversified": 22,
}
DEFAULT_SECTOR_PE = 22  # broad market fallback when sector isn't in the table


class DataProvider(abc.ABC):
    """Abstract interface every data source must implement."""

    @abc.abstractmethod
    def get_universe(self) -> list[tuple[str, str, str]]:
        """Return list of (symbol, company_name, sector). Market cap
        category is deliberately not part of this tuple -- it's computed
        dynamically per-stock from real fetched market cap data (see
        core.recommendation_engine.classify_market_cap), since it can't be
        hand-tagged for hundreds of symbols."""

    @abc.abstractmethod
    def get_snapshot(self, symbol: str, company_name: str, sector: str) -> MarketSnapshot:
        """Return a full MarketSnapshot for one symbol."""

    @abc.abstractmethod
    def get_index_quotes(self) -> dict:
        """Return dict of Nifty/Sensex/BankNifty/VIX quotes."""

    def get_market_status(self) -> str:
        """NSE cash market hours: 9:15-15:30 IST, Mon-Fri (holidays not modeled)."""
        import datetime as _dt
        now = _dt.datetime.now()
        if now.weekday() >= 5:
            return "Closed (Weekend)"
        open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
        close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return "Open" if open_t <= now <= close_t else "Closed"

    def prefetch(self, symbols: list[str]) -> None:
        """Optional hook: providers that can fetch data for many symbols in
        one batched network call (e.g. yfinance's bulk download) should
        override this to warm an internal cache before get_snapshot() is
        called in a per-symbol loop. Default is a no-op."""
        return

    def describe_universe(self) -> dict:
        """
        Answers "did this actually scan the full NIFTY 500, or just the
        small fallback list?" -- call this AFTER get_universe(). Default
        implementation covers providers with a single fixed universe
        (e.g. DemoDataProvider); YFinanceDataProvider overrides this with
        real fetch-outcome detail.
        """
        universe = self.get_universe()
        return {
            "count": len(universe),
            "source": "fixed",
            "label": f"{len(universe)} stocks (fixed list)",
        }


class DemoDataProvider(DataProvider):
    """
    Deterministic synthetic data generator. Seeded per-symbol so repeated
    runs are reproducible for a given "day" but still vary day to day,
    which lets paper trades show meaningful price movement over time.
    """

    def __init__(self, seed_salt: str = "v1"):
        self.seed_salt = seed_salt

    def get_universe(self):
        return FALLBACK_UNIVERSE

    def describe_universe(self) -> dict:
        n = len(FALLBACK_UNIVERSE)
        return {"count": n, "source": "demo",
                "label": f"{n} stocks (offline demo data, not NIFTY 500)"}

    def _rng(self, symbol: str) -> random.Random:
        # Seed changes daily so prices "move" across runs on different days,
        # but stay stable within the same day for reproducibility.
        seed_key = f"{symbol}-{self.seed_salt}-{date.today().isoformat()}"
        return random.Random(seed_key)

    def get_snapshot(self, symbol: str, company_name: str, sector: str) -> MarketSnapshot:
        rng = self._rng(symbol)

        base_price = rng.uniform(50, 4000)
        prev_close = base_price * rng.uniform(0.97, 1.03)
        price = prev_close * rng.uniform(0.96, 1.05)
        day_range = price * rng.uniform(0.01, 0.035)
        high = price + day_range * rng.uniform(0.3, 1.0)
        low = price - day_range * rng.uniform(0.3, 1.0)
        open_ = low + (high - low) * rng.random()

        week52_high = price * rng.uniform(1.05, 1.60)
        week52_low = price * rng.uniform(0.55, 0.95)
        all_time_high = max(week52_high, price * rng.uniform(1.0, 2.0))

        volume = int(rng.uniform(2e5, 1.5e7))
        # Log-uniform across ~Rs 500cr to Rs 20 lakh cr so demo data spans
        # Small/Mid/Large the same way a real universe does (a plain
        # price-linear formula clustered everything into "Small").
        market_cap = 10 ** rng.uniform(9.7, 13.3)

        # Synthetic 200-day price history via random walk ending at `price`.
        n = 200
        history = [price]
        vol_hist = [volume]
        drift = rng.uniform(-0.0015, 0.0020)
        for _ in range(n - 1):
            step = rng.gauss(drift, 0.018)
            history.append(history[-1] / (1 + step))
            vol_hist.append(max(1000, int(rng.uniform(0.4, 1.8) * volume)))
        history.reverse()
        vol_hist.reverse()
        history[-1] = price
        vol_hist[-1] = volume

        # --- Synthetic deep fundamentals for disqualifier testing (core/disqualifiers.py).
        # ~20% of stocks get a randomly-chosen "problem" so the demo universe
        # actually exercises each disqualifier rule occasionally, rather than
        # every synthetic stock sailing through clean.
        is_financial = sector in ("Banking", "NBFC", "Insurance", "Financial Services")
        problem_roll = rng.random()

        debt_to_equity_val = rng.uniform(0.0, 2.2)
        if not is_financial and 0.29 <= problem_roll < 0.34:  # ~5%: excessive leverage
            debt_to_equity_val = rng.uniform(3.1, 5.5)

        total_equity = market_cap * rng.uniform(0.3, 0.9)
        if problem_roll < 0.04:  # ~4%: negative equity
            total_equity = -abs(total_equity) * 0.1

        # Total assets = equity + liabilities, with liabilities derived from
        # the same leverage figure (debt) plus a generic operating-liability
        # slice, so debt_to_equity/total_equity/total_assets stay internally
        # consistent for the DuPont equity-multiplier calculation.
        total_liabilities = abs(total_equity) * (debt_to_equity_val + rng.uniform(0.2, 0.5))
        total_assets = total_equity + total_liabilities

        interest_coverage = max(0.3, rng.uniform(2, 12) - rng.uniform(0, 8) * debt_to_equity_val / 2.5)
        if 0.04 <= problem_roll < 0.09:  # ~5%: weak interest coverage specifically
            interest_coverage = rng.uniform(0.3, 1.2)

        # Multi-year annual revenue/earnings: base growth trend with noise;
        # ~6% of stocks get a genuinely declining trend on both fronts.
        base_rev = market_cap * rng.uniform(0.15, 0.6) / 1e0
        if 0.09 <= problem_roll < 0.15:  # ~6%: structural decline
            rev_trend = [-rng.uniform(0.03, 0.12) for _ in range(3)]
            earn_trend = [-rng.uniform(0.05, 0.18) for _ in range(3)]
        else:
            rev_trend = [rng.uniform(-0.05, 0.20) for _ in range(3)]
            earn_trend = [rng.uniform(-0.08, 0.25) for _ in range(3)]
        revenue_history = [base_rev]
        earnings_history = [base_rev * rng.uniform(0.05, 0.18)]
        for g in rev_trend:
            revenue_history.append(revenue_history[-1] * (1 + g))
        for g in earn_trend:
            earnings_history.append(earnings_history[-1] * (1 + g))

        # OCF/NI: usually healthy (~1.0 +- noise); ~6% sustained low (earnings quality issue)
        if 0.15 <= problem_roll < 0.21:
            ocf_to_ni_history = [rng.uniform(0.1, 0.45) for _ in range(3)]
        else:
            ocf_to_ni_history = [max(0.2, rng.gauss(1.05, 0.2)) for _ in range(3)]

        # FCF history: usually positive; ~8% negative 3-yr streak (tests the
        # Aggressive small/mid-cap carve-out logic end to end)
        if 0.21 <= problem_roll < 0.29:
            fcf_history = [-abs(rng.uniform(1e8, 5e9)) for _ in range(3)]
        else:
            fcf_history = [rng.uniform(-1e8, 8e9) for _ in range(3)]

        # Capex as a plausible fraction of revenue (5-15% of revenue is a
        # reasonable generic range); OCF derived so fcf = ocf - capex holds,
        # keeping the three series internally consistent for the pillar
        # scoring functions that cross-check them against each other.
        capex_history = [abs(revenue_history[i + 1]) * rng.uniform(0.05, 0.15) for i in range(3)]
        ocf_history = [fcf_history[i] + capex_history[i] for i in range(3)]

        # ~4% of stocks simulate genuinely incomplete data (Insufficient Data path)
        if problem_roll >= 0.96:
            deep_available, deep_total = 1, 7
        else:
            deep_available, deep_total = 7, 7

        return MarketSnapshot(
            symbol=symbol,
            company_name=company_name,
            exchange="NSE",
            price=round(price, 2),
            open=round(open_, 2),
            high=round(high, 2),
            low=round(low, 2),
            prev_close=round(prev_close, 2),
            volume=volume,
            week52_high=round(week52_high, 2),
            week52_low=round(week52_low, 2),
            all_time_high=round(all_time_high, 2),
            market_cap=round(market_cap, 2),
            sector=sector,
            industry=sector,
            revenue_growth_pct=round(rng.uniform(-5, 35), 1),
            profit_growth_pct=round(rng.uniform(-15, 45), 1),
            eps=round(rng.uniform(2, 250), 2),
            roe_pct=round(rng.uniform(4, 32), 1),
            roce_pct=round(rng.uniform(5, 34), 1),
            debt_to_equity=round(debt_to_equity_val, 2),
            free_cash_flow=round(rng.uniform(-500, 8000) * 1e6, 0),
            promoter_holding_pct=round(rng.uniform(0, 75), 1),
            fii_holding_pct=round(rng.uniform(2, 40), 1),
            dii_holding_pct=round(rng.uniform(2, 35), 1),
            dividend_yield_pct=round(rng.uniform(0, 4.5), 2),
            pe_ratio=round(rng.uniform(6, 60), 1),
            pb_ratio=round(rng.uniform(0.5, 12), 2),
            peg_ratio=round(rng.uniform(0.3, 3.5), 2),
            ev_ebitda=round(rng.uniform(4, 30), 1),
            sector_avg_pe=round(rng.uniform(15, 35), 1),
            price_history=history,
            volume_history=vol_hist,
            news_sentiment_score=round(rng.uniform(-1, 1), 2),
            analyst_rating_score=round(rng.uniform(-1, 1), 2),
            total_equity=round(total_equity, 2),
            total_assets=round(total_assets, 2),
            interest_coverage=round(interest_coverage, 2) if interest_coverage is not None else None,
            ocf_to_ni_history=[round(v, 2) for v in ocf_to_ni_history],
            revenue_history_annual=[round(v, 2) for v in revenue_history],
            earnings_history_annual=[round(v, 2) for v in earnings_history],
            fcf_history_annual=[round(v, 2) for v in fcf_history],
            capex_history_annual=[round(v, 2) for v in capex_history],
            ocf_history_annual=[round(v, 2) for v in ocf_history],
            is_financial_sector=is_financial,
            deep_fields_available=deep_available,
            deep_fields_total=deep_total,
        )

    def get_index_quotes(self) -> dict:
        rng = random.Random(f"indices-{date.today().isoformat()}")
        def idx(base):
            chg_pct = rng.uniform(-1.5, 1.5)
            val = base * (1 + chg_pct / 100)
            return {"value": round(val, 2), "change_pct": round(chg_pct, 2)}
        return {
            "NIFTY 50": idx(24500),
            "SENSEX": idx(80500),
            "BANK NIFTY": idx(51500),
            "INDIA VIX": {"value": round(rng.uniform(11, 18), 2),
                          "change_pct": round(rng.uniform(-5, 5), 2)},
        }


class YFinanceDataProvider(DataProvider):
    """
    Real-data provider using `yfinance`. NSE symbols use the ".NS" suffix
    (e.g. "RELIANCE.NS").

    Yahoo Finance actively blocks Python's default urllib/requests User-Agent
    and returns an HTML block page, which yfinance fails to parse and raises
    "Expecting value: line 1 column 1" or reports every ticker as "possibly
    delisted" -- this is not a symbol problem, it is a transport problem.

    Fix: all Yahoo requests go through a curl_cffi session that impersonates
    a real Chrome browser (impersonate="chrome"). This is yfinance's own
    currently-recommended fix. curl_cffi ships as a dependency of recent
    yfinance versions so it should already be installed; if not:
        pip install curl_cffi

    One session is created at __init__ time and reused across all calls --
    this is important because the session holds the crumb/cookie that Yahoo
    requires for the authenticated endpoints (.info, .balance_sheet, etc.),
    and re-creating it per call would force a fresh handshake each time.
    """

    HTTP_TIMEOUT_SEC = 15

    def __init__(self, universe: Iterable[tuple[str, str, str]] | None = None):
        self._explicit_universe = list(universe) if universe else None
        self._history_cache: dict = {}
        self._last_universe_info: dict = {}
        self._session = self._make_session()

    @staticmethod
    def _make_session():
        """Creates a curl_cffi session that impersonates Chrome. Falls back
        to None (yfinance default behaviour) if curl_cffi isn't installed,
        so the provider still works -- just with the original bot-block risk."""
        try:
            from curl_cffi import requests as curl_requests
            session = curl_requests.Session(impersonate="chrome")
            logger.info("curl_cffi Chrome-impersonation session created (Yahoo bot-block fix active)")
            return session
        except ImportError:
            logger.warning("curl_cffi not installed -- yfinance will use its default requests "
                           "session, which Yahoo Finance may block. Fix: pip install curl_cffi")
            return None

    def get_universe(self):
        if self._explicit_universe is not None:
            self._last_universe_info = {
                "count": len(self._explicit_universe), "source": "custom",
                "label": f"{len(self._explicit_universe)} stocks (custom list)",
            }
            return self._explicit_universe

        constituents, source, fetched_at = fetch_nifty500_constituents()
        if constituents:
            labels = {
                "live": "live NIFTY 500, just fetched from NSE",
                "cached": "NIFTY 500, cached copy",
                "stale_cache": "NIFTY 500, STALE cached copy -- live fetch failed",
            }
            self._last_universe_info = {
                "count": len(constituents), "source": source,
                "fetched_at": fetched_at,
                "label": f"{len(constituents)} stocks ({labels.get(source, source)})",
            }
            return constituents

        logger.warning("Falling back to the small %d-stock universe -- live NIFTY 500 "
                        "list unavailable (no internet, or NSE blocked/changed the fetch).",
                        len(FALLBACK_UNIVERSE))
        self._last_universe_info = {
            "count": len(FALLBACK_UNIVERSE), "source": "fallback",
            "label": f"{len(FALLBACK_UNIVERSE)} stocks (FALLBACK list -- live NIFTY 500 "
                     f"fetch failed, not the full universe)",
        }
        return FALLBACK_UNIVERSE

    def describe_universe(self) -> dict:
        if not self._last_universe_info:
            self.get_universe()  # populate it if describe_universe() is called first
        return self._last_universe_info

    def prefetch(self, symbols: list[str]) -> None:
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("yfinance not installed -- skipping batch history prefetch")
            return

        tickers = [f"{s}.NS" for s in symbols]
        try:
            kwargs = dict(
                period="1y", group_by="ticker", threads=True,
                progress=False, auto_adjust=False, timeout=self.HTTP_TIMEOUT_SEC,
            )
            if self._session is not None:
                kwargs["session"] = self._session
            data = yf.download(tickers, **kwargs)
        except Exception as e:
            logger.warning("Batch history download failed (%s) -- falling back to "
                            "per-symbol fetch inside get_snapshot()", e)
            return

        for symbol in symbols:
            ticker_key = f"{symbol}.NS"
            try:
                df = data if len(tickers) == 1 else data[ticker_key]
                df = df.dropna(how="all")
                if not df.empty:
                    self._history_cache[symbol] = df
            except Exception:
                continue

        logger.info("Batch-prefetched history for %d/%d symbols", len(self._history_cache), len(symbols))

    def _get_history(self, symbol: str, yf_module):
        if symbol in self._history_cache:
            return self._history_cache[symbol]
        kwargs = {"session": self._session} if self._session is not None else {}
        ticker = yf_module.Ticker(f"{symbol}.NS", **kwargs)
        return ticker.history(period="1y", timeout=self.HTTP_TIMEOUT_SEC)

    @staticmethod
    def _safe_info(ticker) -> dict:
        """`.info` is the slowest/flakiest yfinance call (needs a crumb/cookie
        handshake that some networks block or stall). Never let its failure
        take down the whole snapshot -- fall back to an empty dict; the
        neutral-default logic below fills in reasonable fundamentals."""
        try:
            info = ticker.info
            return info if isinstance(info, dict) else {}
        except Exception as e:
            logger.info("`.info` unavailable for %s (%s) -- using neutral fundamentals", ticker.ticker, e)
            return {}

    # Sectors where a raw Debt/Equity ceiling is the wrong leverage lens
    # (banks/NBFCs run naturally high D/E as their business model -- see
    # spec §3). Matches the normalize_sector() bucket names.
    FINANCIAL_SECTOR_BUCKETS = {"Banking", "NBFC", "Insurance", "Financial Services"}

    @staticmethod
    def _get_row(df, *possible_labels):
        """yfinance's statement DataFrames use slightly different row
        labels across versions/companies (e.g. 'EBIT' vs 'Operating
        Income', 'Stockholders Equity' vs 'Total Stockholder Equity').
        Tries each candidate label and returns the first match as a
        pandas Series (columns = periods), or None if none are present."""
        if df is None or df.empty:
            return None
        for label in possible_labels:
            if label in df.index:
                return df.loc[label]
        return None

    def _fetch_deep_fundamentals(self, ticker, sector_name: str, symbol: str) -> dict:
        """
        Pulls multi-year income statement / balance sheet / cash flow data
        for the hard-disqualifier checks (core/disqualifiers.py) and the
        Profitability (core/profitability.py) and Growth/Cash-Flow-Quality
        (core/fundamental_analysis.py) pillars. Every piece is fetched and
        parsed defensively -- yfinance's statement DataFrames are
        considerably less standardized than `.info`, so a missing row or
        an empty DataFrame for one company is expected and must not crash
        the whole snapshot. Returns a dict of fields plus available/total
        counts for the "Insufficient Data" disqualifier.
        """
        result = {
            "total_equity": None, "total_assets": None, "interest_coverage": None,
            "ocf_to_ni_history": [], "revenue_history_annual": [],
            "earnings_history_annual": [], "fcf_history_annual": [],
            "capex_history_annual": [], "ocf_history_annual": [],
            # §2.3
            "current_ratio": None, "quick_ratio": None,
            "total_debt_history": [], "current_assets_history": [], "current_liab_history": [],
            "beta": None,
            # §2.6 ownership
            "promoter_holding_pct_prev": None, "institutional_pct_prev": None,
            "shares_short_ratio": None,
            # §2.7 analyst/earnings
            "analyst_target_mean": None, "analyst_target_high": None, "analyst_target_low": None,
            "analyst_recommendation_mean": None, "analyst_count": 0,
            "upgrades_90d": 0, "downgrades_90d": 0,
            "eps_beat_miss_history": [], "earnings_date_str": "",
            "is_financial_sector": sector_name in self.FINANCIAL_SECTOR_BUCKETS,
        }
        available = 0

        # --- Balance sheet: equity + assets + current items + debt history ---
        try:
            bs = ticker.balance_sheet
            equity_row = self._get_row(bs, "Stockholders Equity", "Total Stockholder Equity",
                                        "Common Stock Equity", "Total Equity Gross Minority Interest")
            if equity_row is not None and len(equity_row) > 0:
                val = equity_row.iloc[0]
                if val == val:
                    result["total_equity"] = float(val)
                    available += 1

            assets_row = self._get_row(bs, "Total Assets")
            if assets_row is not None and len(assets_row) > 0:
                val = assets_row.iloc[0]
                if val == val:
                    result["total_assets"] = float(val)
                    available += 1

            # Current assets / liabilities (for current ratio + working-capital trend)
            ca_row = self._get_row(bs, "Current Assets", "Total Current Assets")
            cl_row = self._get_row(bs, "Current Liabilities", "Total Current Liabilities",
                                    "Current Liabilities Net Minority Interest")
            if ca_row is not None and cl_row is not None:
                ca_vals = [float(v) for v in ca_row.dropna().tolist()]
                cl_vals = [float(v) for v in cl_row.dropna().tolist()]
                if ca_vals and cl_vals:
                    result["current_assets_history"] = list(reversed(ca_vals))
                    result["current_liab_history"] = list(reversed(cl_vals))
                    if cl_vals[0] > 0:   # most recent period current ratio
                        result["current_ratio"] = round(ca_vals[0] / cl_vals[0], 2)
                        available += 1

            debt_row = self._get_row(bs, "Total Debt", "Long Term Debt And Capital Lease Obligation")
            if debt_row is not None and len(debt_row) > 0:
                debt_vals = [float(v) for v in debt_row.dropna().tolist()]
                if debt_vals:
                    result["total_debt_history"] = list(reversed(debt_vals))

        except Exception as e:
            logger.info("Balance sheet unavailable for %s (%s)", symbol, e)

        # --- Income statement ---
        try:
            inc = ticker.income_stmt
            ebit_row = self._get_row(inc, "EBIT", "Operating Income")
            interest_row = self._get_row(inc, "Interest Expense", "Interest Expense Non Operating")
            if ebit_row is not None and interest_row is not None and len(ebit_row) > 0 and len(interest_row) > 0:
                ebit = ebit_row.iloc[0]
                interest = abs(interest_row.iloc[0]) if interest_row.iloc[0] == interest_row.iloc[0] else 0
                if ebit == ebit and interest > 0:
                    result["interest_coverage"] = float(ebit) / float(interest)
                    available += 1

            rev_row = self._get_row(inc, "Total Revenue", "Operating Revenue")
            if rev_row is not None and len(rev_row) > 0:
                vals = [float(v) for v in rev_row.dropna().tolist()]
                if vals:
                    result["revenue_history_annual"] = list(reversed(vals))
                    available += 1

            ni_row = self._get_row(inc, "Net Income", "Net Income Common Stockholders")
            if ni_row is not None and len(ni_row) > 0:
                vals = [float(v) for v in ni_row.dropna().tolist()]
                if vals:
                    result["earnings_history_annual"] = list(reversed(vals))
                    available += 1
        except Exception as e:
            logger.info("Income statement unavailable for %s (%s)", symbol, e)

        # --- Cash flow ---
        try:
            cf = ticker.cashflow
            ocf_row = self._get_row(cf, "Operating Cash Flow", "Total Cash From Operating Activities",
                                     "Cash Flow From Continuing Operating Activities")
            capex_row = self._get_row(cf, "Capital Expenditure", "Purchase Of PPE")

            if ocf_row is not None and len(ocf_row) > 0:
                ocf_vals = [float(v) for v in ocf_row.dropna().tolist()]
                result["ocf_history_annual"] = list(reversed(ocf_vals))

                try:
                    inc = ticker.income_stmt
                    ni_row = self._get_row(inc, "Net Income", "Net Income Common Stockholders")
                except Exception:
                    ni_row = None
                if ni_row is not None and len(ni_row) > 0:
                    ni_vals = [float(v) for v in ni_row.dropna().tolist()]
                    n = min(len(ocf_vals), len(ni_vals))
                    ratios = [ocf_vals[i] / ni_vals[i] for i in range(n) if ni_vals[i] not in (0, 0.0)]
                    if ratios:
                        result["ocf_to_ni_history"] = list(reversed(ratios))
                        available += 1

                if capex_row is not None and len(capex_row) > 0:
                    capex_vals = [abs(float(v)) for v in capex_row.dropna().tolist()]
                    result["capex_history_annual"] = list(reversed(capex_vals))
                    n = min(len(ocf_vals), len(capex_vals))
                    fcf_vals = [ocf_vals[i] - capex_vals[i] for i in range(n)]
                    if fcf_vals:
                        result["fcf_history_annual"] = list(reversed(fcf_vals))
                        available += 1
        except Exception as e:
            logger.info("Cash flow statement unavailable for %s (%s)", symbol, e)

        # --- Analyst targets + recommendation + upgrades/downgrades (§2.7) ---
        try:
            from datetime import datetime as _dt, timedelta as _td
            cutoff = _dt.now() - _td(days=90)

            ud = ticker.upgrades_downgrades
            if ud is not None and not ud.empty:
                try:
                    ud = ud[ud.index >= cutoff] if hasattr(ud.index, 'tz') else ud
                    grades = ud.get("ToGrade", ud.get("Action", []))
                    if grades is not None:
                        upgrades = sum(1 for g in grades if str(g).lower() in
                                       ("buy", "strong buy", "outperform", "overweight", "upgrade"))
                        downgrades = sum(1 for g in grades if str(g).lower() in
                                         ("sell", "strong sell", "underperform", "underweight",
                                          "downgrade", "reduce"))
                        result["upgrades_90d"] = upgrades
                        result["downgrades_90d"] = downgrades
                except Exception:
                    pass
        except Exception as e:
            logger.info("Upgrades/downgrades unavailable for %s (%s)", symbol, e)

        # --- Earnings beat/miss streak (§2.7) ---
        try:
            ed = ticker.earnings_dates
            if ed is not None and not ed.empty:
                hits = []
                for _, row in ed.head(4).iterrows():
                    eps_est = row.get("EPS Estimate")
                    eps_act = row.get("Reported EPS")
                    if eps_est is not None and eps_act is not None:
                        try:
                            diff = float(eps_act) - float(eps_est)
                            hits.append(1 if diff > 0 else (-1 if diff < 0 else 0))
                        except (TypeError, ValueError):
                            pass
                result["eps_beat_miss_history"] = list(reversed(hits))  # oldest first
                if hits:
                    available += 1
        except Exception as e:
            logger.info("Earnings dates unavailable for %s (%s)", symbol, e)

        # --- Upcoming earnings date (flag imminent volatility risk) ---
        try:
            cal = ticker.calendar
            if cal is not None:
                ear_date = None
                if hasattr(cal, "get"):
                    ear_date = cal.get("Earnings Date")
                elif hasattr(cal, "loc"):
                    try:
                        ear_date = cal.loc["Earnings Date"].iloc[0] if "Earnings Date" in cal.index else None
                    except Exception:
                        pass
                if ear_date is not None:
                    result["earnings_date_str"] = str(ear_date)
        except Exception as e:
            logger.info("Calendar unavailable for %s (%s)", symbol, e)

        # Total load-bearing fields now covers equity, assets, current ratio,
        # interest coverage, revenue, earnings, OCF/NI, FCF, earnings beat/miss = 9
        result["deep_fields_available"] = available
        result["deep_fields_total"] = 9
        return result

    def get_snapshot(self, symbol: str, company_name: str, sector: str) -> MarketSnapshot:
        import pandas as pd
        try:
            import yfinance as yf
        except ImportError as e:
            raise RuntimeError(
                "yfinance not installed. Run `pip install yfinance` to use "
                "YFinanceDataProvider, or switch back to DemoDataProvider."
            ) from e

        hist = self._get_history(symbol, yf)
        if hist is None or hist.empty:
            raise RuntimeError(f"No price history returned for {symbol}")

        ticker_kwargs = {"session": self._session} if self._session is not None else {}
        ticker = yf.Ticker(f"{symbol}.NS", **ticker_kwargs)
        info = self._safe_info(ticker)

        last = hist.iloc[-1]
        price = float(last["Close"])
        sector_name = info.get("sector") or sector
        sector_pe = SECTOR_AVG_PE.get(sector_name, DEFAULT_SECTOR_PE)

        # --- Fundamentals: prefer real values from `.info`; fall back to a
        # NEUTRAL midpoint (not zero) so missing data scores as
        # "unknown/average" rather than "terrible" downstream. ---
        def neutral(key: str, transform, default: float) -> float:
            raw = info.get(key)
            if raw is None:
                return default
            try:
                return transform(raw)
            except (TypeError, ValueError):
                return default

        debt_to_equity = neutral("debtToEquity", lambda v: float(v) / 100.0, 0.8)  # yfinance reports as %
        roe_pct = neutral("returnOnEquity", lambda v: float(v) * 100, 15.0)
        # yfinance has no direct ROCE; returnOnAssets is the closest free proxy.
        roce_pct = neutral("returnOnAssets", lambda v: float(v) * 100, roe_pct * 0.75)
        revenue_growth_pct = neutral("revenueGrowth", lambda v: float(v) * 100, 8.0)
        profit_growth_pct = neutral("earningsGrowth", lambda v: float(v) * 100, 8.0)
        promoter_holding_pct = neutral("heldPercentInsiders", lambda v: float(v) * 100, 45.0)
        institutional_pct = neutral("heldPercentInstitutions", lambda v: float(v) * 100, 25.0)
        pe_ratio = neutral("trailingPE", float, info.get("forwardPE") or sector_pe)
        pb_ratio = neutral("priceToBook", float, 4.0)
        peg_ratio = neutral("pegRatio", float, info.get("trailingPegRatio") or 1.0)
        ev_ebitda = neutral("enterpriseToEbitda", float, 15.0)
        dividend_yield_pct = neutral("dividendYield", lambda v: float(v) * (100 if v < 1 else 1), 0.0)
        free_cash_flow = neutral("freeCashflow", float, 0.0)
        eps = neutral("trailingEps", float, 0.0)
        week52_high = float(info.get("fiftyTwoWeekHigh") or hist["High"].max())
        week52_low = float(info.get("fiftyTwoWeekLow") or hist["Low"].min())
        market_cap = neutral("marketCap", float, 0.0)

        # Extra .info fields for the new pillars
        quick_ratio = neutral("quickRatio", float, None)
        beta = neutral("beta", float, None)
        short_ratio = neutral("shortRatio", float, None)
        analyst_target_mean = neutral("targetMeanPrice", float, None)
        analyst_target_high = neutral("targetHighPrice", float, None)
        analyst_target_low = neutral("targetLowPrice", float, None)
        analyst_rec_mean = neutral("recommendationMean", float, None)
        analyst_count = int(info.get("numberOfAnalystOpinions") or 0)

        # Multi-year financials for the hard-disqualifier checks
        # (core/disqualifiers.py). Fully defensive -- see method docstring.
        try:
            deep = self._fetch_deep_fundamentals(ticker, sector_name, symbol)
        except Exception as e:
            logger.info("Deep fundamentals fetch failed entirely for %s (%s)", symbol, e)
            deep = {"total_equity": None, "total_assets": None, "interest_coverage": None,
                    "ocf_to_ni_history": [], "revenue_history_annual": [], "earnings_history_annual": [],
                    "fcf_history_annual": [], "capex_history_annual": [], "ocf_history_annual": [],
                    "current_ratio": None, "quick_ratio": None, "total_debt_history": [],
                    "current_assets_history": [], "current_liab_history": [], "beta": None,
                    "promoter_holding_pct_prev": None, "institutional_pct_prev": None,
                    "shares_short_ratio": None, "analyst_target_mean": None,
                    "analyst_target_high": None, "analyst_target_low": None,
                    "analyst_recommendation_mean": None, "analyst_count": 0,
                    "upgrades_90d": 0, "downgrades_90d": 0,
                    "eps_beat_miss_history": [], "earnings_date_str": "",
                    "is_financial_sector": sector_name in self.FINANCIAL_SECTOR_BUCKETS,
                    "deep_fields_available": 0, "deep_fields_total": 9}

        return MarketSnapshot(
            symbol=symbol,
            company_name=info.get("longName") or company_name,
            exchange="NSE",
            price=price,
            open=float(last["Open"]),
            high=float(last["High"]),
            low=float(last["Low"]),
            prev_close=float(hist.iloc[-2]["Close"]) if len(hist) > 1 else price,
            volume=int(last["Volume"]) if not pd.isna(last["Volume"]) else 0,
            week52_high=week52_high,
            week52_low=week52_low,
            all_time_high=week52_high,  # 1y proxy; extend history for true ATH
            market_cap=market_cap,
            sector=sector_name,
            industry=info.get("industry") or sector_name,
            revenue_growth_pct=revenue_growth_pct,
            profit_growth_pct=profit_growth_pct,
            eps=eps,
            roe_pct=roe_pct,
            roce_pct=roce_pct,
            debt_to_equity=debt_to_equity,
            free_cash_flow=free_cash_flow,
            promoter_holding_pct=promoter_holding_pct,
            fii_holding_pct=institutional_pct * 0.6,   # yfinance doesn't split FII/DII; approximate split
            dii_holding_pct=institutional_pct * 0.4,
            dividend_yield_pct=dividend_yield_pct,
            pe_ratio=pe_ratio,
            pb_ratio=pb_ratio,
            peg_ratio=peg_ratio,
            ev_ebitda=ev_ebitda,
            sector_avg_pe=sector_pe,
            price_history=hist["Close"].tolist(),
            volume_history=hist["Volume"].fillna(0).tolist(),
            high_history=hist["High"].tolist(),
            low_history=hist["Low"].tolist(),
            total_equity=deep["total_equity"],
            total_assets=deep["total_assets"],
            interest_coverage=deep["interest_coverage"],
            ocf_to_ni_history=deep["ocf_to_ni_history"],
            revenue_history_annual=deep["revenue_history_annual"],
            earnings_history_annual=deep["earnings_history_annual"],
            fcf_history_annual=deep["fcf_history_annual"],
            capex_history_annual=deep["capex_history_annual"],
            ocf_history_annual=deep["ocf_history_annual"],
            is_financial_sector=deep["is_financial_sector"],
            deep_fields_available=deep["deep_fields_available"],
            deep_fields_total=deep["deep_fields_total"],
        )

    def get_index_quotes(self) -> dict:
        try:
            import yfinance as yf
        except ImportError as e:
            raise RuntimeError("yfinance not installed.") from e

        ticker_kwargs = {"session": self._session} if self._session is not None else {}
        symbols = {"NIFTY 50": "^NSEI", "SENSEX": "^BSESN",
                   "BANK NIFTY": "^NSEBANK", "INDIA VIX": "^INDIAVIX"}
        result = {}
        for name, ysym in symbols.items():
            try:
                hist = yf.Ticker(ysym, **ticker_kwargs).history(period="2d", timeout=self.HTTP_TIMEOUT_SEC)
                if len(hist) >= 2:
                    chg = (hist["Close"].iloc[-1] / hist["Close"].iloc[-2] - 1) * 100
                    result[name] = {"value": round(float(hist["Close"].iloc[-1]), 2),
                                     "change_pct": round(float(chg), 2)}
            except Exception as e:
                logger.warning("Index quote failed for %s: %s", name, e)
        return result


def get_provider(name: str = "demo") -> DataProvider:
    """Factory so the rest of the app never imports a concrete provider directly."""
    if name == "demo":
        return DemoDataProvider()
    if name == "yfinance":
        return YFinanceDataProvider()
    raise ValueError(f"Unknown data provider: {name}")
