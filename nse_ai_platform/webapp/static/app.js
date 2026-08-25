// webapp/static/app.js
// Vanilla JS single-page app. No build step, no framework -- fetch() +
// DOM APIs are plenty for a dashboard this size, and it keeps "install
// and run" as simple as possible for a desktop-app replacement.

const CATEGORIES = ["Watchlist", "Conservative", "Balanced", "Aggressive"];
let activeTab = "Watchlist";
let pollTimer = null;

// ---------------------------------------------------------------- utils --

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

function fmtPct(v) {
  if (v === null || v === undefined) return "&mdash;";
  const cls = v >= 0 ? "pct-pos" : "pct-neg";
  return `<span class="${cls}">${v >= 0 ? "+" : ""}${v.toFixed(2)}%</span>`;
}

function fmtMoney(v) {
  if (v === null || v === undefined) return "&mdash;";
  return "\u20B9" + Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}

function updateUniverseBadge(universeInfo) {
  const badge = document.getElementById("universeInfo");
  if (!universeInfo) {
    badge.textContent = "Not yet run";
    badge.className = "dh-universe";
    return;
  }
  badge.textContent = "Scanned: " + universeInfo.label;
  badge.className = "dh-universe src-" + (universeInfo.source || "unknown");
  badge.title = universeInfo.source === "live" || universeInfo.source === "cached"
    ? "Full NIFTY 500 universe"
    : "NOT the full NIFTY 500 -- see label for why";
}

// ------------------------------------------------------------- top bar --

async function refreshDashboard() {
  let data;
  try {
    data = await getJSON("/api/dashboard");
  } catch (e) {
    return;
  }

  document.getElementById("serverTime").textContent = data.server_time;
  const ms = document.getElementById("marketStatus");
  ms.textContent = "Market: " + (data.market_status || "Unknown (run analysis to check)");

  updateUniverseBadge(data.universe_info);

  const lastRun = document.getElementById("lastRun");
  lastRun.textContent = data.last_run_at ? "Last run: " + new Date(data.last_run_at).toLocaleString() : "No run yet";

  const idxContainer = document.getElementById("indices");
  idxContainer.innerHTML = "";
  const quotes = data.index_quotes || {};
  Object.entries(quotes).forEach(([name, q]) => {
    const dir = q.change_pct >= 0 ? "idx-up" : "idx-down";
    const box = el("div", "idx",
      `<span class="idx-name">${name}</span><span class="${dir}">${q.value.toLocaleString("en-IN")} (${q.change_pct >= 0 ? "+" : ""}${q.change_pct.toFixed(2)}%)</span>`);
    idxContainer.appendChild(box);
  });

  // Update tab counts by re-rendering the active tab's tab label counts (cheap, just counts)
  CATEGORIES.forEach(cat => {
    const btn = document.querySelector(`.tab-btn[data-tab="${cat}"]`);
    const count = data.category_counts ? data.category_counts[cat] : undefined;
    if (btn && count !== undefined) btn.textContent = `${cat} (${count})`;
  });

  window._lastDashboard = data;
}

// --------------------------------------------------------- run pipeline --

async function startRun() {
  const provider = document.getElementById("providerSelect").value;
  const news = document.getElementById("newsToggle").checked;
  const btn = document.getElementById("runBtn");
  const statusBox = document.getElementById("runStatus");

  btn.disabled = true;
  btn.textContent = "Running...";
  statusBox.classList.remove("hidden", "error");
  statusBox.textContent = "Starting run...";

  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider, news }),
    });
    if (res.status === 409) {
      statusBox.textContent = "A run is already in progress...";
    } else if (!res.ok) {
      throw new Error("Failed to start run");
    }
  } catch (e) {
    statusBox.classList.add("error");
    statusBox.textContent = "Failed to start run: " + e.message;
    btn.disabled = false;
    btn.textContent = "Run AI Analysis";
    return;
  }

  pollTimer = setInterval(pollRunStatus, 1500);
}

async function pollRunStatus() {
  const statusBox = document.getElementById("runStatus");
  const btn = document.getElementById("runBtn");
  let job;
  try {
    job = await getJSON("/api/run-status");
  } catch (e) {
    return;
  }

  if (job.logs && job.logs.length) {
    statusBox.textContent = job.logs.slice(-12).join("\n");
    statusBox.scrollTop = statusBox.scrollHeight;
  }

  if (job.status === "complete") {
    clearInterval(pollTimer);
    btn.disabled = false;
    btn.textContent = "Run AI Analysis";
    const r = job.result || {};
    const uLabel = r.universe_info ? r.universe_info.label : `${r.stocks_scanned} stocks`;
    statusBox.textContent =
      `Done: scanned ${uLabel} \u2192 ${r.recommendations_created} recommendations ` +
      `(${r.trades_opened || 0} new trades, ${r.trades_refreshed || 0} refreshed, ` +
      `${r.watchlist_monitored || 0} watchlisted, ${r.trades_closed || 0} closed).`;
    setTimeout(() => statusBox.classList.add("hidden"), 6000);
    refreshDashboard();
    renderActiveTab();
  } else if (job.status === "error") {
    clearInterval(pollTimer);
    btn.disabled = false;
    btn.textContent = "Run AI Analysis";
    statusBox.classList.add("error");
    statusBox.textContent = "Run failed: " + (job.error || "unknown error");
  }
}

// --------------------------------------------------------------- tabs ---

function setActiveTab(tab) {
  activeTab = tab;
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  renderActiveTab();
}

function renderActiveTab() {
  if (CATEGORIES.includes(activeTab)) renderCategoryTab(activeTab);
  else if (activeTab === "PaperTrading") renderPaperTradingTab();
  else if (activeTab === "History") renderHistoryTab();
}

// ------------------------------------------------------- category tab ---

async function renderCategoryTab(category) {
  const main = document.getElementById("tabContent");
  main.innerHTML = '<div class="empty-state">Loading...</div>';

  let recs;
  try {
    recs = await getJSON(`/api/recommendations/${encodeURIComponent(category)}`);
  } catch (e) {
    main.innerHTML = '<div class="empty-state">Failed to load recommendations.</div>';
    return;
  }

  if (!recs.length) {
    main.innerHTML = `<div class="empty-state">No ${category} recommendations yet. Click "Run AI Analysis" to generate some.</div>`;
    return;
  }

  const list = el("div", "card-list");
  recs.forEach(r => list.appendChild(buildCard(r)));
  main.innerHTML = "";
  main.appendChild(list);
}

function buildCard(r) {
  const card = el("article", "card");
  card.style.borderLeftColor = r.color_hex || "#666";

  const head = el("div", "card-head");
  head.innerHTML = `<span class="card-rank">#${r.rank_in_category}</span>
    <span class="card-name">${r.company_name} (${r.symbol})</span>
    <span class="card-score">AI Score ${r.overall_ai_score?.toFixed(1)}</span>`;
  card.appendChild(head);

  const fields = [
    ["CMP", fmtMoney(r.current_price)],
    ["Buy Range", `${fmtMoney(r.buy_range_low)} - ${fmtMoney(r.buy_range_high)}`],
    ["Stop Loss", fmtMoney(r.stop_loss)],
    ["Target 1 / 2 / 3", `${fmtMoney(r.target_1)} / ${fmtMoney(r.target_2)} / ${fmtMoney(r.target_3)}`],
    ["Fair Value", fmtMoney(r.fair_value)],
    ["Margin of Safety", `${r.margin_of_safety?.toFixed(1)}%`],
    ["Expected Return", fmtPct(r.expected_return_pct)],
    ["Confidence", r.confidence_score?.toFixed(1)],
    ["Growth Score", r.growth_score?.toFixed(1)],
    ["Cash Flow Quality", r.cash_flow_quality_score?.toFixed(1)],
    ["Profitability", r.profitability_score?.toFixed(1)],
    ["Risk Level", r.risk_level],
    ["Risk : Reward", `1 : ${r.risk_reward_ratio?.toFixed(2)}`],
    ["Horizon", r.investment_horizon],
    ["Recommended", r.recommendation_date],
  ];
  const grid = el("div", "card-grid");
  grid.innerHTML = fields.map(([k, v]) => `<div>${k}: <b>${v}</b></div>`).join("");
  card.appendChild(grid);

  card.appendChild(el("p", "card-reason", r.ai_explanation || ""));

  // --- News section ---
  const impact = r.news_overall_impact || "Neutral";
  const articleCount = r.news_article_count || 0;
  const newsBox = el("div", "card-news");

  if (articleCount === 0) {
    const reason = (r.news_summary || "").toLowerCase();
    const isDisabled = reason.includes("disabled");
    const label = isDisabled ? "News analysis disabled for this run" : "No recent news found for this stock";
    const cls = isDisabled ? "news-badge news-off" : "news-badge news-empty";
    newsBox.innerHTML = `<span class="${cls}">\u2014 ${label}</span>`;
  } else {
    newsBox.innerHTML = `<span class="news-badge ${impact}">\uD83D\uDCF0 ${r.news_sentiment_label || "Neutral"} &middot; ${impact} &middot; ${articleCount} article${articleCount !== 1 ? "s" : ""}</span>`;
    if (r.news_summary) {
      newsBox.appendChild(el("p", "news-summary", r.news_summary));
    }
    const headlines = Array.isArray(r.news_headlines) ? r.news_headlines : [];
    headlines.slice(0, 5).forEach(h => {
      const scope = h.scope ? `<span class="news-scope">${h.scope}</span>` : "";
      newsBox.appendChild(el("div", "news-headline", `&bull; ${h.title} <em>(${h.source})</em> ${scope}`));
    });
    const pos = Array.isArray(r.news_positive_factors) ? r.news_positive_factors : [];
    const neg = Array.isArray(r.news_negative_factors) ? r.news_negative_factors : [];
    if (pos.length) newsBox.appendChild(el("div", "news-factors pos", `&#x2B; ${pos[0]}`));
    if (neg.length) newsBox.appendChild(el("div", "news-factors neg", `&#x2212; ${neg[0]}`));
  }
  card.appendChild(newsBox);

  card.appendChild(el("div", "card-tech",
    `Pivot ${r.pivot_point?.toFixed(1)} | S1 ${r.s1?.toFixed(1)} R1 ${r.r1?.toFixed(1)} | ` +
    `52W H/L ${r.week52_high?.toFixed(1)} / ${r.week52_low?.toFixed(1)}`));

  return card;
}

// ---------------------------------------------------------- paper trading --

async function renderPaperTradingTab() {
  const main = document.getElementById("tabContent");
  main.innerHTML = '<div class="empty-state">Loading...</div>';

  const [summary, trades, watchlist] = await Promise.all([
    window._lastDashboard
      ? Promise.resolve(window._lastDashboard.paper_trading_summary)
      : getJSON("/api/dashboard").then(d => d.paper_trading_summary),
    getJSON("/api/paper-trades"),
    getJSON("/api/watchlist-monitor"),
  ]);

  main.innerHTML = "";

  const stats = [
    ["Open", summary.open_trades], ["Closed", summary.closed_trades],
    ["Win Rate", summary.win_rate_pct + "%"], ["Winning", summary.winning_trades],
    ["Losing", summary.losing_trades], ["Avg Return", summary.avg_return_pct + "%"],
    ["Avg Hold (days)", summary.avg_holding_days],
  ];
  const statGrid = el("div", "stat-grid");
  stats.forEach(([label, value]) => {
    statGrid.appendChild(el("div", "stat-box",
      `<div class="stat-value">${value}</div><div class="stat-label">${label}</div>`));
  });
  main.appendChild(statGrid);

  // --- Watchlist Monitor section (separate from paper trades) ---
  const wlHead = el("h3", "section-head", `\uD83D\uDC41 Watchlist Monitor (${watchlist.length} stocks being tracked)`);
  main.appendChild(wlHead);

  if (watchlist.length === 0) {
    main.appendChild(el("div", "empty-state",
      "No Watchlist stocks yet. These appear when the AI identifies a fundamentally strong stock that isn't yet in the ideal buy range -- no trade is opened, just the price is tracked."));
  } else {
    const wlTable = el("table", "data-table");
    wlTable.innerHTML = `<thead><tr>
        <th>Stock</th><th>Sector</th><th>Current Price</th>
        <th>Ideal Buy Range</th><th>Fair Value</th>
        <th>% Away from Entry</th><th>AI Score</th><th>Status</th>
      </tr></thead>`;
    const wlBody = el("tbody");
    watchlist.forEach(w => {
      const away = w.pct_away_from_entry;
      const awayStr = away <= 0
        ? `<span class="pct-pos">\u2705 In range (${away.toFixed(1)}%)</span>`
        : `<span class="pct-neg">${away.toFixed(1)}% above entry</span>`;
      const tr = el("tr");
      tr.innerHTML = `
        <td style="font-family:var(--font-ui);font-weight:600">${w.symbol}</td>
        <td style="font-family:var(--font-ui)">${w.sector || ""}</td>
        <td>${fmtMoney(w.current_price)}</td>
        <td>${fmtMoney(w.buy_range_low)} \u2013 ${fmtMoney(w.buy_range_high)}</td>
        <td>${fmtMoney(w.fair_value)}</td>
        <td>${awayStr}</td>
        <td>${w.overall_ai_score?.toFixed(1) ?? ""}</td>
        <td><span class="status-pill ${w.status === 'ENTERED' ? 'OPEN' : 'CLOSED'}">${w.status}</span></td>`;
      wlBody.appendChild(tr);
    });
    wlTable.appendChild(wlBody);
    main.appendChild(wlTable);
  }

  // --- Active paper trades ---
  const trHead = el("h3", "section-head", `\uD83D\uDCCA Paper Trades \u2014 Called Positions (${trades.length})`);
  main.appendChild(trHead);

  if (!trades.length) {
    main.appendChild(el("div", "empty-state",
      "No paper trades yet. Click \"Run AI Analysis\" to generate Conservative/Balanced/Aggressive recommendations and auto-open paper trades for them."));
    return;
  }

  const table = el("table", "data-table");
  table.innerHTML = `<thead><tr>
      <th>Call</th><th>Category</th><th>Date</th><th>Call Price</th>
      <th>Days</th><th>% Change</th><th>Status</th>
    </tr></thead>`;
  const tbody = el("tbody");
  trades.forEach(t => {
    const tr = el("tr");
    tr.innerHTML = `
      <td style="font-family:var(--font-ui);font-weight:600">${t.call}</td>
      <td style="font-family:var(--font-ui)">${t.category}</td>
      <td>${t.date}</td>
      <td>${fmtMoney(t.call_price)}</td>
      <td>${t.days}</td>
      <td>${fmtPct(t.pct_change)}</td>
      <td><span class="status-pill ${t.status}">${t.status}${t.exit_reason ? " &middot; " + t.exit_reason : ""}</span></td>`;
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  main.appendChild(table);
}

// ---------------------------------------------------------------- history --

async function renderHistoryTab() {
  const main = document.getElementById("tabContent");
  main.innerHTML = "";

  const filters = el("div", "filters");
  filters.innerHTML = `
    <select id="fCategory"><option value="">All Categories</option>${CATEGORIES.map(c => `<option>${c}</option>`).join("")}</select>
    <select id="fStatus"><option value="">All Status</option><option>OPEN</option><option>CLOSED</option></select>
    <input type="text" id="fMinScore" placeholder="Min AI Score">
    <button class="btn-primary" id="fApply">Apply Filters</button>
  `;
  main.appendChild(filters);

  const tableHolder = el("div");
  main.appendChild(tableHolder);

  async function load() {
    const params = new URLSearchParams();
    const cat = document.getElementById("fCategory").value;
    const status = document.getElementById("fStatus").value;
    const minScore = document.getElementById("fMinScore").value;
    if (cat) params.set("category", cat);
    if (status) params.set("status", status);
    if (minScore) params.set("min_score", minScore);

    tableHolder.innerHTML = '<div class="empty-state">Loading...</div>';
    let rows;
    try {
      rows = await getJSON("/api/history?" + params.toString());
    } catch (e) {
      tableHolder.innerHTML = '<div class="empty-state">Failed to load history.</div>';
      return;
    }
    if (!rows.length) {
      tableHolder.innerHTML = '<div class="empty-state">No matching history.</div>';
      return;
    }
    const table = el("table", "data-table");
    table.innerHTML = `<thead><tr>
        <th>Symbol</th><th>Sector</th><th>Category</th><th>Date</th>
        <th>AI Score</th><th>Confidence</th><th>Status</th><th>Return</th>
      </tr></thead>`;
    const tbody = el("tbody");
    rows.forEach(r => {
      const tr = el("tr");
      tr.innerHTML = `<td>${r.symbol}</td><td>${r.sector || ""}</td><td>${r.category}</td>
        <td>${r.recommendation_date}</td><td>${r.overall_ai_score?.toFixed(1) ?? ""}</td>
        <td>${r.confidence_score?.toFixed(1) ?? ""}</td><td>${r.status || ""}</td>
        <td>${r.return_pct !== undefined && r.return_pct !== null ? fmtPct(r.return_pct) : ""}</td>`;
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    tableHolder.innerHTML = "";
    tableHolder.appendChild(table);
  }

  document.getElementById("fApply").addEventListener("click", load);
  load();
}

// ---------------------------------------------------------------- init --

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => setActiveTab(btn.dataset.tab));
});
document.getElementById("runBtn").addEventListener("click", startRun);

refreshDashboard();
renderActiveTab();
setInterval(refreshDashboard, 30000); // keep header (time/indices/counts) fresh

// ─── Technical Analysis Tab ───────────────────────────────────────────────
async function renderTATab() {
  const main = document.getElementById("tabContent");
  main.innerHTML = '<div class="empty-state">Loading technical analysis…</div>';
  let data;
  try { data = await getJSON("/api/technical-analysis"); }
  catch(e) { main.innerHTML = '<div class="empty-state">Run AI Analysis first to generate data.</div>'; return; }
  if (!data || !data.length) {
    main.innerHTML = '<div class="empty-state">No technical data yet — run AI Analysis first.</div>'; return;
  }
  main.innerHTML = "";
  const tbl = el("table", "data-table");
  tbl.innerHTML = `<thead><tr>
    <th>Stock</th><th>Category</th><th>Price</th>
    <th>Signal</th><th>Confidence</th>
    <th>Bullish</th><th>Neutral</th><th>Bearish</th>
    <th>Summary</th>
  </tr></thead>`;
  const tbody = el("tbody");
  data.forEach(r => {
    const sigCls = r.composite_signal === "Buy" ? "signal-buy" : r.composite_signal === "Sell" ? "signal-sell" : "signal-neutral";
    const row = el("tr");
    row.innerHTML = `
      <td style="font-weight:600">${r.symbol}<br><small style="color:var(--text-dim)">${r.company_name}</small></td>
      <td><span class="cat-pill ${r.category}">${r.category}</span></td>
      <td>${fmtMoney(r.current_price)}</td>
      <td><span class="signal-pill ${sigCls}">${r.composite_signal}</span></td>
      <td>${r.confidence.toFixed(1)}%</td>
      <td style="color:var(--positive)">${r.bullish_count}</td>
      <td style="color:var(--text-dim)">${r.neutral_count}</td>
      <td style="color:var(--negative)">${r.bearish_count}</td>
      <td style="font-size:11px;color:var(--text-dim)">${r.summary}</td>`;
    row.style.cursor = "pointer";
    row.onclick = () => showTADetail(r, row, tbody);
    tbody.appendChild(row);
  });
  tbl.appendChild(tbody);
  main.appendChild(tbl);
}

function showTADetail(r, clickedRow, tbody) {
  const existing = tbody.querySelector(".ta-detail-row");
  if (existing) { existing.remove(); if (existing.dataset.for === r.symbol) return; }
  const detailRow = document.createElement("tr");
  detailRow.className = "ta-detail-row"; detailRow.dataset.for = r.symbol;
  const td = document.createElement("td"); td.colSpan = 9;
  const grid = el("div", "ta-indicator-grid");
  (r.indicators || []).forEach(ind => {
    const card = el("div", "ta-ind-card " + (ind.signal === "Buy" ? "sig-buy" : ind.signal === "Sell" ? "sig-sell" : "sig-neutral"));
    card.innerHTML = `<div class="ta-ind-name">${ind.name}</div>
      <div class="ta-ind-val">${ind.value}</div>
      <div class="ta-ind-signal">${ind.signal}</div>
      <div class="ta-ind-reason">${ind.reason}</div>`;
    grid.appendChild(card);
  });
  td.appendChild(grid);
  detailRow.appendChild(td);
  clickedRow.after(detailRow);
}

