# AI Stock Recommendation & Paper Trading Platform (NSE/BSE)

**This application never places real trades.** It only generates AI-powered
recommendations and maintains a simulated (paper) trading portfolio in a
local SQLite database.

## Status: Phase 10 — All spec pillars complete (§2.1–§2.7, §3, §4, §5)

All sections of the analyst-grade redesign spec are now implemented,
tested end-to-end, and wired through the full stack.

**Pillars**: §2.1 Growth (multi-year CAGR+consistency), §2.2 Profitability
(ROE/ROCE+DuPont), §2.3 Financial Health (liquidity+debt trend), §2.4
Cash Flow Quality (OCF/NI+capex+FCF alignment), §2.5 Valuation
(cross-sectional sector medians+analyst targets), §2.6 Ownership
(promoter/institutional trend+short interest), §2.7 Earnings Quality
(beat/miss streak+analyst consensus+upgrades). §3 Hard Disqualifiers
(6 red-flag vetos). §4 Checklist-based classification (Conservative=6/8
criteria, Balanced=4/6, not score-cutoffs). §5 Technical Analysis tab
(8 indicators, composite Buy/Neutral/Sell, click-to-expand detail).

**Weights (11 pillars, sum=1.0)**: fundamental=0.08, technical=0.15,
valuation=0.12, sentiment=0.07, quality=0.08, growth=0.10,
cash_flow_quality=0.10, profitability=0.10, financial_health=0.08,
ownership=0.07, earnings_quality=0.05

**Two-pass pipeline**: snapshots collected concurrently → sector stats
computed → injected into each snapshot → scoring pass. Enables real
cross-sectional valuation without hardcoded sector constants.

**Bug fixes**: curl_cffi+fast_info Yahoo block fix; Watchlist separated
from paper trades; re-recommendation refreshes open trade instead of
duplicating; news display shows three distinct states; quality-over-quantity
thresholds (score≥50, confidence≥45, R:R≥1.5, MAX=10 per category).

## Status: Phase 8 -- three bug fixes + quality filter

### Bug 1 fixed: same news component on every stock
The news section on each card now has three clearly-distinct visual states:
- **"News analysis disabled for this run"** (dashed badge) — you unchecked
  "News sentiment" in the UI or ran with `news_enabled=False`.
- **"No recent news found for this stock"** (muted badge) — news fetch ran
  but returned zero articles for this specific symbol (a valid outcome for
  smaller/less-covered stocks, not a broken component).
- **Real headlines** (coloured Bullish/Neutral/Bearish badge) — when actual
  articles were fetched and analysed. Now also shows the first positive and
  negative factor alongside up to 5 headlines, each tagged with their scope
  (Company-specific / Sector-specific / Market-wide).

Previously all empty-news states showed the same generic "No recent news
found" text regardless of whether news was disabled, blocked, or genuinely
absent for that specific stock. That's now explicitly labelled.

### Bug 2 fixed: Watchlist stocks appearing in paper trading
Watchlist means "good company, not yet cheap enough to buy." Opening a
paper trade on a Watchlist stock contradicted its entire meaning.

Now:
- **Watchlist → watchlist_monitor table** (never paper_trade). One row per
  symbol tracks current price vs. ideal buy range and marks status as
  `WATCHING` (not yet at entry) or `ENTERED` (price has reached the buy
  range — you should look at this).
- **Conservative / Balanced / Aggressive → paper trade** (unchanged).
- The Paper Trading tab now has two clearly-labelled sections: a
  **Watchlist Monitor** table at the top (price proximity, AI score, buy
  range, status) and the **Called Positions** table below it (only actual
  paper trades). No Watchlist stock ever appears in the trades table;
  verified with an automated assertion in the test suite.
- New API endpoint: `GET /api/watchlist-monitor`.

### Bug 3 fixed: same stock recommended daily spawning duplicate trades
Previously, if RELIANCE was already in an open paper trade and got
recommended again the next day, a second paper trade was opened on top of
the first. The idempotency check only blocked *same-day* duplicates.

Now, the pipeline uses a **three-way dispatch** per new recommendation:

| Scenario | Behaviour |
|---|---|
| Same day (any category) | Skip — idempotent same-day re-run protection |
| Watchlist | Update watchlist_monitor, never open a trade |
| Tradeable, no open trade | Open a new paper trade |
| Tradeable, already has an open trade | **Refresh** the existing trade's stop-loss and targets to the new recommendation's levels — no duplicate. Growing stocks: AI may raise stop-loss (trailing effect). Falling stocks: AI may tighten stop-loss (reduces further loss). All refreshes are logged with direction and magnitude of the stop-loss movement. |

Confirmed with a simulated two-day run: Day 1 = 6 trades opened, 4 watchlisted.
Day 2 = 0 new trades, 6 refreshed, 4 watchlist monitors updated.

### Quality filter: fewer, better recommendations
`MAX_PER_CATEGORY` cut from 25 → **10**. More importantly, three
independent quality gates must ALL be cleared before a stock is recommended:

1. **`min_overall_score = 57`** (was 45) — requires genuine quality, not
   just clearing a low bar.
2. **`min_confidence_score = 52`** — sub-scores must broadly agree. A
   stock scoring 65 overall with fundamentals=85 and technicals=15 is a
   conflicted signal and gets dropped here regardless of its blended number.
3. **`min_risk_reward_ratio = 1.5`** — the trade setup itself must make
   sense. Risk 1 rupee, make at least 1.5. Applied after targets are
   computed, so a technically-good stock in a poor price setup (too close
   to its stop, target barely above current price) still gets filtered out.

Result in the demo run: 10 recommendations from 41 stocks (was 20+ before),
4 Watchlist + 3 Conservative + 3 Aggressive. Fewer calls, each one with a
reason to exist beyond "it cleared the minimum score."

## Status: Phase 7 -- Profitability & Efficiency pillar (spec §2.2)

**`core/profitability.py`** — the third dedicated fundamental pillar,
returning `(score_0_100, facts)` per the spec's explainability
requirement:

- **ROE and ROCE** current-year levels (baseline -- same data as before,
  but now contributing to a dedicated, independently-visible pillar rather
  than folded into the catch-all `fundamental_score`).
- **Net margin trend** — this year vs. the multi-year average from real
  income statement history. A company with 10% margins and expanding
  direction scores higher than one with 12% margins and contracting
  direction. This is the spec's "trend over absolute level" requirement,
  distinct from the snapshot-only `fundamental_score`.
- **Asset turnover** (revenue / total assets) — capital efficiency proxy,
  differentiating businesses that generate a lot of revenue per rupee of
  assets from capital-heavy ones with the same bottom line.
- **DuPont leverage flag** — the spec's explicit requirement that "40% ROE
  on 2x leverage is not the same quality signal as 25% ROE on no
  leverage." When a high ROE is accompanied by a high equity multiplier
  (total assets / equity), the score is penalised by up to 20 points and
  the reasoning is surfaced verbatim in the AI explanation text. Verified
  with a targeted test: a 28% "leverage-assisted" ROE (equity multiplier
  6x) correctly scored 29.7 vs. a 22% "genuine" ROE (equity multiplier
  1.67x) scoring 55.1, despite the leveraged stock having higher raw ROE.

**Data layer**: added `total_assets` to `MarketSnapshot` and fetched it
from yfinance's `balance_sheet` in `YFinanceDataProvider._fetch_deep_
fundamentals()`, alongside the existing `total_equity`. `DemoDataProvider`
generates `total_assets = total_equity + total_liabilities` where
liabilities are derived from the same leverage ratio that drives D/E,
keeping all three fields internally consistent.

**Weight rebalancing**: `ScoringWeights` now has 8 components, all summing
to exactly 1.0. `fundamental` (was 0.25, now 0.10) was reduced because the
new `profitability` (0.10), `growth` (0.10), and `cash_flow_quality` (0.10)
pillars cover what `fundamental_score`'s ROE/ROCE/growth components used
to approximate -- the residual `fundamental` weight now covers just the
items the dedicated pillars don't: promoter holding, FII/DII participation,
debt/equity, dividend yield, and FCF sign.

**DB and UI**: `profitability_score` is persisted in the `recommendation`
table, shown on web UI recommendation cards alongside `growth_score` and
`cash_flow_quality_score`, and cited in the AI explanation text when
notable (the DuPont leverage flag is always included verbatim when it
fires, regardless of whether the score threshold is hit).

**Deferred from this pass** (documented inline in `profitability.py`):
gross margin and operating margin trend specifically need "Gross Profit" /
"Operating Income" history from the income statement, not just the
most-recent-period EBIT that's currently fetched for interest coverage.
That will be a natural extension once the income statement fetch is
broadened for the future earnings quality pillar (§2.7).

**Still outstanding from the full spec**: financial health as a standalone
ranked pillar beyond the disqualifier gate (§2.3), the full valuation
verdict with cross-sectional sector stats (§2.5), ownership/governance
trend tracking (§2.6), earnings quality & analyst sentiment (§2.7), the
category classification checklist rewrite (§4), and the technical
analysis module + new UI tab (§5).

## Status: Phase 6 -- Growth and Cash Flow Quality pillars (spec §2.1, §2.4)

**`core/fundamental_analysis.py`** -- two new scored pillars, each
returning `(score, facts)` per the spec's explainability requirement:

- **Growth (§2.1)**: multi-year revenue/earnings CAGR from real financial
  history, replacing the old single-year `revenueGrowth`/`earningsGrowth`
  blend the spec calls out as noisy. Critically also scores **consistency**
  (% of years with positive YoY growth) -- a steady 4/4-up grower now
  correctly outscores a choppy stock with the same trailing CAGR built from
  one huge year propping up flat/declining others. Verified with 5 targeted
  tests including that exact choppy-vs-consistent comparison. Falls back
  to the single-year fields (clearly flagged in the facts) when multi-year
  statements aren't available for a stock.
- **Cash Flow Quality (§2.4)**: OCF/Net Income ratio (the spec's
  highest-value single check), capex intensity as % of revenue, and
  whether FCF is keeping pace with reported earnings growth. This is a
  separate, positive-scoring pillar from the OCF/NI *disqualifier* gate
  added in Phase 5 -- same underlying data, different job (one vetoes,
  this one ranks). Verified with 4 targeted tests.

**Wired into `core/scoring.py`**: `ScoringWeights` gained a
`cash_flow_quality` slot (10%), rebalanced from `fundamental` (25%→20%)
and `quality` (15%→10%) to keep the total at 100%. `score_growth()` now
delegates to the new pillar instead of the old single-year blend.

**Data layer**: extended `YFinanceDataProvider`'s deep-fundamentals fetch
to also capture OCF and capex history (needed for the capex-intensity
check), and extended `DemoDataProvider`'s synthetic data generation to
produce internally-consistent OCF/capex/FCF figures offline.

**Explanation text and DB persistence**: `cash_flow_quality_score` is now
a full sibling of the other sub-scores -- persisted in the `recommendation`
table, shown on the web UI cards, and cited by name in the AI explanation
when notably strong or weak (confirmed via a real pipeline run: 11/22
demo recommendations mention growth or cash-flow-quality facts by name).

**Deferred from this pass** (documented inline in
`fundamental_analysis.py`, not silently skipped): quarterly growth-trend
acceleration/deceleration (needs `quarterly_income_stmt`, not fetched
yet) and buyback/dividend-funded-by-debt detection (needs financing cash
flow, not fetched yet).

**Not verified against live yfinance data** (no internet in this
sandbox) -- verified instead via 9 targeted unit tests against
hand-crafted scenarios (strong/choppy/declining growth, healthy vs.
red-flag cash flow quality, missing-data fallback) plus a full pipeline
run confirming the scores persist and surface correctly end to end.

**Still outstanding from the full spec**: profitability/DuPont (§2.2),
financial health as a standalone ranked pillar beyond the existing
disqualifier gate (§2.3), the full valuation verdict system with
cross-sectional sector stats (§2.5), ownership/governance trend tracking
(§2.6), earnings quality & analyst sentiment (§2.7), the category
classification checklist rewrite (§4), and the entire technical analysis
module + new UI tab (§5).

## Status: Phase 5 -- hard disqualifiers + cash-flow quality (first slice of the analyst-grade rewrite)

Per the uploaded spec, implemented as a deliberately self-contained first
slice rather than attempting the full rewrite at once (multiple new
fundamental pillars, cross-sectional valuation, a full technical-analysis
module, and a new UI tab are all still outstanding -- see below).

**`core/disqualifiers.py`** -- red flags that exclude a stock regardless
of how good its blended score looks, checked as a separate, higher-
priority gate BEFORE the existing weighted-score classification (not
blended into the score itself):
- Negative/near-zero total equity
- Debt/Equity above 3.0x (skipped for banks/NBFCs/insurance -- different
  leverage model, per the spec)
- Interest coverage below 1.5x
- Structural decline: revenue AND earnings both falling 3+ consecutive years
- Cash flow quality: OCF/Net Income sustained below 0.5x for 2+ years
  (profit not converting to cash -- the single check the spec calls out
  as catching more real-world problems than almost anything else)
- Free cash flow negative 3+ consecutive years -- **with the spec's
  explicit carve-out**: small/mid-cap stocks with otherwise-clean numbers
  are allowed into Aggressive ONLY (never Conservative/Balanced/
  Watchlist), with the reasoning disclosed directly in the AI explanation
  rather than hidden
- Insufficient data: if we can't tell whether a stock is healthy (less
  than 60% of load-bearing fields available), it's excluded rather than
  scored as "neutral" -- deliberately different from the rest of the
  app's missing-data philosophy, since these specific fields determine
  solvency, not just a secondary signal

Verified with 11 targeted unit tests (one per rule plus the carve-out
interaction edge case) before wiring into anything else, then confirmed
end-to-end against the full pipeline: 21/41 demo stocks correctly
excluded, and every carve-out stock correctly forced into Aggressive only
with its disclosure visible through the API.

**Data layer**: `YFinanceDataProvider` now also fetches multi-year
`income_stmt`/`balance_sheet`/`cashflow` per stock (needed for the checks
above), parsed defensively since yfinance's statement row labels vary
across versions -- a missing statement for one company degrades that
stock's data-completeness score rather than crashing the run.
`DemoDataProvider` generates synthetic multi-year financials with ~20% of
stocks randomly assigned a "problem" so the disqualifier logic has
something real to exercise offline.

**Not verified against live yfinance data in this sandbox** (no internet
here) -- the yfinance statement row-label guesses
(`_get_row()` in `data/providers.py`) are based on common yfinance
conventions but could be wrong for some companies/yfinance versions.
Run `python debug_scores.py` and check whether real stocks are showing
`deep_fields_available` close to 6/6 -- if most real stocks show low
completeness, the row labels likely need adjusting; share the output and
I'll fix the parser.

**Deferred from the spec** (following its own advice to validate one
pillar at a time): the full fundamental pillar rebuild (2.1-2.7 --
DuPont profitability, sector-relative cross-sectional valuation, ownership
trend tracking, etc.), the entire technical analysis module rewrite, and
the new dedicated Technical Analysis UI tab. Tell me which to tackle next.

## Status: Phase 4 -- NIFTY 500 universe + concurrent processing

**Universe expanded from 41 to ~500 stocks.** `YFinanceDataProvider` now
scans the live NIFTY 500 constituent list by default, fetched from NSE
Indices at runtime (`data/providers.py`'s `fetch_nifty500_constituents()`)
and cached locally (`data_store/nifty500_universe_cache.json`, refreshed
at most every 7 days -- the index only rebalances twice a year). If the
live fetch fails for any reason (no internet, NSE changed their URL), it
falls back to a stale cache if one exists, and failing that, to the
original hand-picked 41-stock list (`FALLBACK_UNIVERSE`) -- the app never
breaks outright, it just runs on a smaller universe with a clear warning
logged. **Not verified against the live NSE endpoint in this sandbox** (no
internet here) -- please run it once and confirm you actually get ~500
symbols; if NSE's CSV format or URL has changed, share the error and I'll
fix the parser.

**Market cap category (Large/Mid/Small) is now computed dynamically**
from each stock's real fetched market cap (`core/recommendation_engine.py`'s
`classify_market_cap()`, using round approximate rupee thresholds) instead
of a hardcoded per-symbol label -- there's no practical way to hand-tag
hundreds of companies, and this is honestly more correct anyway since it
reacts to the real number rather than a stale guess.

**Sector classification** got a new `normalize_sector()` mapping NSE's ~70
granular "Industry" labels (e.g. "FERTILISERS & PESTICIDES") down to this
app's internal taxonomy (~27 buckets, expanded from the original ~17) for
consistent valuation/news-scope scoring; anything unmapped falls back to a
cleaned version of NSE's own label rather than being dropped.

**Concurrent processing, rebuilt and empirically verified (not just
reasoned about).** Scanning 500 stocks sequentially at the ~4s/stock
observed in your last real run would take 30+ minutes. The pipeline now
processes up to `MAX_CONCURRENT_STOCKS` (10 by default) stocks at once.
Getting this right took two failed attempts, both caught by actually
running simulated-hang tests rather than trusting the design on paper:

1. First attempt used `concurrent.futures.ThreadPoolExecutor` with
   `future.result(timeout=...)` per item. This looked correct and even
   passed an initial test, but a closer test revealed that Python's
   global atexit hook for `ThreadPoolExecutor` joins ALL worker threads at
   process exit, REGARDLESS of `shutdown(wait=False)` -- so a single
   permanently-stuck stock would make the whole *process* hang on exit,
   reproducing your original Ctrl+C problem at a different point in
   execution.
2. Rebuilt using genuine daemon `threading.Thread`s (which the process
   never waits on) gated by a `threading.Semaphore`. First version of
   this joined threads sequentially in submission order, which meant one
   early hung stock blocked the collection loop from ever checking later
   stocks that had already finished in milliseconds -- so a healthy run
   with one hang up front would *still* take the full worst-case time.
   Fixed by polling all threads for completion instead of joining
   one-at-a-time.

Both bugs were only caught because I actually ran timed tests with
simulated permanent hangs and checked the process actually exited (`echo
$?` after `timeout N python3 ...`) rather than trusting the code by
inspection -- worth knowing given how much this app now depends on this
mechanism holding up on your machine's specific network conditions.
`run_pipeline.py`'s module docstring on `_process_universe_concurrently`
has the full design rationale if you want the details.

**Practical impact**: a healthy ~500-stock run should take roughly
(500 / 10) x a few seconds per stock -- likely 2-5 minutes depending on
your network and how much `.info` and news-fetch latency you see, versus
30-40+ minutes sequential. A run with several stuck stocks will still
complete, bounded by an internal deadline (worst case ~21 minutes if
everything possible times out), rather than hanging indefinitely.

## Status: Phase 3 -- browser-based UI, simplified paper trading

**UI**: replaced the PySide6 desktop app with a local Flask web server +
vanilla JS/HTML/CSS frontend (`webapp/`). PySide6 had a real reliability
problem on Windows -- an uncaught exception in a Qt slot triggered by a
cross-thread signal can silently abort the whole process, which is what
caused the app to close itself right after a pipeline run completed. A
browser UI sidesteps that whole class of failure and is far easier to
debug (browser dev console vs. a native crash with no traceback).

Run with:
```bash
python webapp_main.py
```
This starts a local server at `http://127.0.0.1:5000` and opens it in
your default browser automatically. Nothing leaves your machine except
the pipeline's normal outbound calls to Yahoo Finance / news sources.

The old PySide6 app (`ui/main_window.py`, `python main.py`) still exists
in the zip but is no longer the recommended path -- see the note in
`requirements.txt` if you want to keep using it anyway.

**Paper trading simplified**: per your request, removed all notional
"portfolio value" / "paper P&L" tracking -- this was simulating a capital
account (assuming a fixed rupee allocation per trade), which added noise
without answering the actual question of interest. The Paper Trading tab
now tracks exactly: **the call (symbol), recommendation date, call price,
days held, % change, and category** -- nothing else. `core/analytics.py`'s
`dashboard_summary()` no longer computes `portfolio_value`/`paper_pnl` at
all.

**Daily history now persists per trade**: added a `paper_trade_history`
table (`db/schema.py`) that logs one row per trade per calendar day it's
touched by a pipeline run -- price, % return, days held, status. The
`paper_trade` table itself still holds only the latest state (so the
main table stays simple), but nothing is lost across runs anymore: every
day's mark-to-market is kept, whether you run the app once a day or
several times. Query it via `AnalyticsEngine.trade_history(trade_id)` or
`GET /api/trade-history/<trade_id>`.

**Not execution-tested in a real browser** (no browser available in this
sandbox), but every backend route was verified directly with Flask's test
client, including: full API responses for dashboard/recommendations/
paper-trades/history/trade-history, the async run-and-poll flow end to
end (`POST /api/run` -> `GET /api/run-status` until `complete`), and a
concurrent-access stress test (rapid reads while a background pipeline
write is in progress) to rule out SQLite locking issues -- no errors.
Please open it in an actual browser and flag anything that looks off
visually; I can't see that from here.

## Status: Phase 2b -- reliability fixes (yfinance hangs, empty categories)

Two real issues surfaced when this was first run against live data on
Windows, both now fixed:

**1. Pipeline hung/crashed on `.info` calls.** yfinance's `.info` property
uses a "crumb" auth handshake over `curl_cffi` that some networks
(corporate firewalls, antivirus SSL-inspection) stall indefinitely --
your log's `KeyboardInterrupt` was you manually breaking out of that hang.
Fixes:
  - Every per-stock fetch now runs in a **daemon thread with a hard 25s
    timeout** (`PER_STOCK_TIMEOUT_SEC` in `run_pipeline.py`). If a stock's
    data/news fetch doesn't finish in time, it's abandoned and the
    pipeline moves on -- it will never hang the whole run again. (Note:
    this had to be a plain `threading.Thread`, not
    `concurrent.futures.ThreadPoolExecutor` -- the executor's context
    manager blocks on exit waiting for stuck workers, silently defeating
    a timeout. Documented in the code.)
  - History is now fetched in **one batched `yf.download()` call** for
    all 41 symbols instead of 41 separate requests (`YFinanceDataProvider.
    prefetch()`), cutting network round-trips roughly in half and reducing
    exposure to rate-limiting.
  - If you still see frequent hangs/timeouts on your network, try:
    `pip install -U yfinance curl_cffi`, temporarily disabling
    antivirus HTTPS/SSL scanning for Python, or running from a different
    network (mobile hotspot is a good test) to confirm it's a
    firewall/SSL-inspection issue rather than a code issue.

**2. Only 2 of 41 stocks got a recommendation, all crammed into one tab.**
yfinance's `.info` often returns `0`/`None` for many fundamental fields
(ROCE, debt/equity, promoter holding, sector P/E...). The scoring engine
was treating "missing" the same as "actually bad," which tanked real
stocks' scores below the recommendation threshold across the board.
Fixed in `data/providers.py`: missing fields now fall back to **neutral
midpoint estimates** (documented inline per field) instead of 0, so
"unknown" reads as "average" rather than "terrible." Also added a
sector-average P/E lookup table (yfinance has no such field) so valuation
scoring has a real reference point, and real daily High/Low history is
now preserved for more accurate technicals (previously only Close was
kept and High/Low were synthesized).
  - Classification thresholds in `core/recommendation_engine.py` were
    also loosened slightly, since they were originally tuned against
    demo (synthetic, more artificially dispersed) data.
  - **Run `python debug_scores.py`** any time to see the real score
    distribution per stock without filtering -- share that output if
    recommendations still look sparse/lopsided against your real data and
    the thresholds can be tuned precisely instead of guessed at.

**Also fixed**: `ZOMATO` was delisted after the company's rename to
Eternal Ltd -- updated to the `ETERNAL` ticker.

**Still not execution-tested against live data in this sandbox** (no
internet access here). Please re-run and let me know what
`debug_scores.py` shows.

## Status: Phase 2 -- live data + news sentiment

`run_pipeline.run()` now defaults to `data_provider_name="yfinance"` (real
NSE prices/OHLCV/basic fundamentals via Yahoo Finance) and
`news_enabled=True` (recency-weighted news sentiment folded into the AI
score). Pass `data_provider_name="demo"` / `news_enabled=False` to fall
back to offline synthetic data or skip news, e.g. for testing without
internet.

**Not execution-tested against live data in this sandbox** (no internet
access here) -- verified instead by: (1) unit-testing the news
classification/sentiment/dedup logic directly with synthetic articles,
(2) confirming the full pipeline runs to completion without crashing when
yfinance/news calls fail (as they do in this sandbox), falling back to
neutral/empty results per stock rather than aborting the run. Please run
`python run_pipeline.py` on your own machine (with internet) and let me
know if anything errors -- I'll fix fast.

### News module (`news/`)

Separate package, swappable independently of everything else:
- `news/providers.py` -- pluggable `NewsProvider` sources. Currently:
  Yahoo Finance company news (via yfinance), Google News RSS search
  (company-specific), and Economic Times/Moneycontrol RSS (market/sector
  context). All free, no API keys.
- `news/classifier.py` -- dedup (URL + near-duplicate title matching),
  scope classification (Company-specific/Sector-specific/Market-wide),
  and corporate-event detection (results, guidance, acquisitions, orders,
  regulatory action, litigation, credit rating changes, dividends,
  buybacks, promoter stake changes).
- `news/sentiment.py` -- lexicon-based sentiment scoring with recency
  weighting (half-life configurable, default 3 days; articles older than
  30 days are excluded).
- `news/engine.py` -- `NewsAnalysisEngine.analyze(symbol, company_name,
  sector)` is the single entry point everything else calls. Swapping in a
  premium provider later means rewriting this file's internals (or
  `self.providers`) only.
- `news/cache.py` -- per-symbol-per-day SQLite cache so repeat runs the
  same day don't re-hit free sources.

**News influence on the Overall AI Score is configurable**: see
`ScoringWeights.sentiment` in `core/scoring.py` (default 0.10 / 10%).
Raise it to make the AI react more to news, lower/zero it to make
recommendations purely fundamentals/technicals driven.

## Status: Phase 1 complete

Phase 1 delivers a fully working, tested backend pipeline plus a written
(not yet execution-tested in this environment) PySide6 desktop UI.

**What's tested and verified working end-to-end in this sandbox:**
- SQLite schema + persistence (never loses history — every recommendation
  is append-only; re-running the app never destroys prior data)
- Data collection layer with a `DemoDataProvider` (synthetic but realistic
  NSE data, works offline) and a `YFinanceDataProvider` stub (real data,
  drop-in swap, needs `pip install yfinance` + internet)
- Feature engineering: RSI, MACD, SMA/EMA, VWAP, ADX, ATR, pivot points,
  support/resistance, trend detection
- AI scoring engine: fundamental / technical / valuation / sentiment /
  quality / growth / risk sub-scores blended into a 0-100 overall score,
  fully explainable (no black box)
- Recommendation engine: classifies into Watchlist / Conservative /
  Balanced / Aggressive, computes buy range, stop-loss, 3 targets, fair
  value, margin of safety, risk:reward, and a human-readable AI explanation
- Paper trading engine: auto-opens a trade per new recommendation,
  mark-to-market on every run, auto-closes on target/stop-loss hits,
  idempotent (re-running the app the same day won't spam duplicate trades)
- Analytics: win rate, avg return, avg holding period, monthly/yearly
  performance, filterable history

**What's written but NOT execution-tested here** (no internet access in
this sandbox to install PySide6): the `ui/main_window.py` desktop GUI.
It's built directly against the tested backend API, so it should work,
but please flag anything that errors on your machine and I'll fix it fast.

## Running it

```bash
pip install -r requirements.txt

# Backend only (prints a console report, no GUI needed):
python run_pipeline.py

# Full desktop app:
python main.py
```

The database lives at `data_store/platform.db` (created automatically).

## Architecture

```
data/providers.py          Data Collection (DemoDataProvider, YFinanceDataProvider)
core/models.py              Shared dataclasses (MarketSnapshot, Recommendation, ...)
core/feature_engineering.py Technical indicators & support/resistance
core/scoring.py              AI Scoring Engine (weighted factor model)
core/recommendation_engine.py  Classification, targets/stop-loss, AI explanations
core/paper_trading.py        Paper Trading Engine (open/update/close trades)
core/analytics.py             Win rate, performance, filtered history
db/schema.py                   SQLite schema + connection manager
db/repository.py                Recommendation persistence
ui/main_window.py                PySide6 desktop UI
run_pipeline.py                   Orchestrator ("run everything" entry point)
main.py                            App entry point (launches UI)
```

Each layer only talks to the layer below through a narrow interface
(`DataProvider`, dataclasses, `DatabaseManager`), so swapping any one piece
— e.g. real NSE data, a trained ML scorer instead of the rule-based one,
or a different UI toolkit — never requires touching the others.

## Swapping in real market data

Edit `run_pipeline.py`'s `run(data_provider_name=...)` call (or wire it to
a settings file) and change `"demo"` to `"yfinance"`, then
`pip install yfinance`. No other code changes needed.

## Next phases (per your spec, not yet built)

- Aggressive tab currently shows 0 in typical runs because the demo
  scoring distribution rarely produces a score profile that maps there
  before Conservative/Balanced claim the stock — worth tuning
  `ClassificationThresholds` once you're looking at real data, since real
  small/mid-cap volatility will naturally populate this tab.
- Excel export button in the UI (openpyxl is in requirements.txt, wiring
  not yet added)
- Charts (equity curve, monthly performance) — matplotlib is included but
  not yet embedded in the UI
- Broker API / alerting / backtesting / ML-LLM scoring hooks — the
  architecture has clean seams for all of these (new `DataProvider`,
  new scorer implementing the same interface as `score_stock`, etc.) but
  none are implemented yet, per your "build incrementally" instruction.

Tell me which of these to tackle next and I'll continue the build.
