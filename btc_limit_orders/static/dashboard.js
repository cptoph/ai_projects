const $ = (id) => document.getElementById(id);
const svgNs = "http://www.w3.org/2000/svg";
const intervalRows = [
  ["1m", "1 min"],
  ["5m", "5 min"],
  ["15m", "15 min"],
  ["1h", "1 hour"],
  ["5 PM ET", "5 PM ET"],
];
let latestState = null;
let noticeTimer = null;

function setText(id, value) {
  $(id).textContent = value;
}

function money(value, digits = 2) {
  return value == null || !Number.isFinite(Number(value))
    ? "—"
    : `$${Number(value).toLocaleString(undefined, {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      })}`;
}

function etTime(timestamp, seconds = false) {
  if (timestamp == null || !Number.isFinite(Number(timestamp))) return "—";
  return new Intl.DateTimeFormat(undefined, {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    ...(seconds ? { second: "2-digit" } : {}),
  }).format(new Date(Number(timestamp) * 1000));
}

function showNotice(message, isError = true) {
  const notice = $("notice");
  notice.textContent = message;
  notice.classList.toggle("notice-error", isError);
  notice.classList.toggle("notice-ok", !isError);
  notice.classList.add("visible");
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => notice.classList.remove("visible"), 5000);
}

function updateForecasts(forecasts) {
  const byHorizon = new Map(
    forecasts.map((forecast) => [forecast.horizon_seconds, forecast]),
  );
  for (const horizon of [60, 300, 900, 3600]) {
    const forecast = byHorizon.get(horizon);
    const value = $(`forecast-${horizon}`);
    const confidence = $(`confidence-${horizon}`);
    const card = $(`forecast-card-${horizon}`);
    const note = $(`forecast-note-${horizon}`);
    if (!forecast) {
      value.textContent = "Warming up…";
      confidence.textContent = "—";
      note.textContent = "";
      value.className = "";
      continue;
    }
    const direction = forecast.direction === "UP" ? "UP" : "DOWN";
    value.textContent =
      `${direction === "UP" ? "↑" : "↓"} ${money(forecast.predicted)} ` +
      `(${money(forecast.low)}–${money(forecast.high)})`;
    value.className = direction === "UP" ? "direction-up" : "direction-down";
    confidence.textContent =
      `${forecast.confidence.toFixed(0)}% · ` +
      `${forecast.provider === "DeepSeek" ? "uncalibrated AI" : forecast.trained ? "trained local model" : "warming up"}` +
      ` · target ${etTime(forecast.target_time)} ET`;
    note.textContent = forecast.update_note
      ? `Updated ${etTime(forecast.updated_at, true)} ET · ${forecast.update_note}`
      : "";
    card.dataset.provider = forecast.provider;
  }
}

function renderPriceChart(history) {
  const svg = $("price-chart");
  svg.replaceChildren();
  const width = 1200;
  const height = 350;
  const left = 82;
  const right = 96;
  const top = 20;
  const bottom = 36;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const now = latestState?.generated_at ?? Date.now() / 1000;
  const span = Number($("price-window").value);
  const start = now - span;
  const samples = (history || []).filter(
    (item) => item.time >= start && item.time <= now && item.price > 0,
  );
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute(
    "aria-label",
    `Bitcoin US dollar price chart over the last ${span / 60} minutes`,
  );
  svg.append(svgElement("rect", {
    x: 0, y: 0, width, height, fill: "#0b1118",
  }));
  if (!samples.length) {
    const empty = svgElement("text", {
      x: width / 2,
      y: height / 2,
      class: "price-empty",
      "text-anchor": "middle",
    });
    empty.textContent = "Waiting for live Coinbase price samples";
    svg.append(empty);
    for (const id of ["chart-last-price", "price-change", "period-high", "period-low", "chart-updated"]) {
      setText(id, "—");
    }
    return;
  }

  const prices = samples.map((item) => item.price);
  const last = samples[samples.length - 1];
  const first = samples[0];
  const high = Math.max(...prices);
  const low = Math.min(...prices);
  const range = high - low;
  const padding = Math.max(range * 0.12, last.price * 0.00015, 0.5);
  const minPrice = low - padding;
  const maxPrice = high + padding;
  const priceRange = maxPrice - minPrice;
  const x = (timestamp) => left + ((timestamp - start) / span) * plotWidth;
  const y = (price) => top + ((maxPrice - price) / priceRange) * plotHeight;
  const defs = svgElement("defs");
  const gradient = svgElement("linearGradient", {
    id: "price-area-gradient", x1: "0", x2: "0", y1: "0", y2: "1",
  });
  gradient.append(
    svgElement("stop", { offset: "0%", "stop-color": "#e7b45f", "stop-opacity": ".24" }),
    svgElement("stop", { offset: "100%", "stop-color": "#e7b45f", "stop-opacity": "0" }),
  );
  defs.append(gradient);
  const clip = svgElement("clipPath", { id: "price-chart-clip" });
  clip.append(svgElement("rect", {
    x: left, y: top, width: plotWidth, height: plotHeight,
  }));
  defs.append(clip);
  svg.append(defs);

  for (let index = 0; index <= 4; index += 1) {
    const tickY = top + (plotHeight * index) / 4;
    const tickPrice = maxPrice - (priceRange * index) / 4;
    svg.append(svgElement("line", {
      x1: left, x2: width - right, y1: tickY, y2: tickY,
      class: index === 4 ? "price-grid-strong" : "price-grid",
    }));
    const label = svgElement("text", {
      x: left - 11, y: tickY + 4, class: "price-axis", "text-anchor": "end",
    });
    label.textContent = money(tickPrice, 0);
    svg.append(label);
  }

  const tickStep = span <= 300 ? 60 : span <= 900 ? 180 : 720;
  const firstTick = Math.ceil(start / tickStep) * tickStep;
  for (let tick = firstTick; tick <= now; tick += tickStep) {
    const tickX = x(tick);
    svg.append(svgElement("line", {
      x1: tickX, x2: tickX, y1: top, y2: top + plotHeight, class: "price-grid",
    }));
    const label = svgElement("text", {
      x: tickX, y: height - 11, class: "price-axis", "text-anchor": "middle",
    });
    label.textContent = etTime(tick, span <= 300);
    svg.append(label);
  }

  const chartGroup = svgElement("g", { "clip-path": "url(#price-chart-clip)" });
  const points = samples.map((item) => [x(item.time), y(item.price)]);
  const linePath = points
    .map(([pointX, pointY], index) => `${index ? "L" : "M"}${pointX.toFixed(2)},${pointY.toFixed(2)}`)
    .join(" ");
  const areaPath =
    `${linePath} L${points[points.length - 1][0].toFixed(2)},${(top + plotHeight).toFixed(2)} ` +
    `L${points[0][0].toFixed(2)},${(top + plotHeight).toFixed(2)} Z`;
  chartGroup.append(svgElement("path", { d: areaPath, class: "price-area" }));
  chartGroup.append(svgElement("path", { d: linePath, class: "price-line" }));
  const currentY = y(last.price);
  chartGroup.append(svgElement("line", {
    x1: left, x2: width - right, y1: currentY, y2: currentY, class: "price-current-line",
  }));
  chartGroup.append(svgElement("circle", {
    cx: x(last.time), cy: currentY, r: 4, class: "price-current-dot",
  }));
  svg.append(chartGroup);

  const tagY = Math.max(top, Math.min(currentY - 10, top + plotHeight - 20));
  svg.append(svgElement("rect", {
    x: width - right + 5, y: tagY, width: right - 11, height: 20,
    rx: 4, fill: "#e3aa55",
  }));
  const currentLabel = svgElement("text", {
    x: width - right / 2, y: tagY + 13, class: "price-current-label",
    "text-anchor": "middle",
  });
  currentLabel.textContent = money(last.price, 0);
  svg.append(currentLabel);

  const change = ((last.price - first.price) / first.price) * 100;
  setText("chart-last-price", money(last.price));
  setText("price-change", `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`);
  $("price-change").className = change > 0 ? "price-stat-positive"
    : change < 0 ? "price-stat-negative" : "";
  setText("period-high", money(high));
  setText("period-low", money(low));
  setText("chart-updated", `${etTime(last.time, true)} ET`);
}

function svgElement(tag, attributes = {}) {
  const node = document.createElementNS(svgNs, tag);
  for (const [key, value] of Object.entries(attributes)) {
    node.setAttribute(key, String(value));
  }
  return node;
}

function renderTimeline(outcomes) {
  const svg = $("outcome-chart");
  svg.replaceChildren();
  svg.setAttribute("viewBox", "0 0 1000 290");
  const width = 1000;
  const height = 290;
  const left = 125;
  const right = 18;
  const top = 15;
  const bottom = 35;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const now = latestState?.generated_at ?? Date.now() / 1000;
  const selectedWindow = $("time-window").value;
  let start;
  if (selectedWindow === "all") {
    start = outcomes.reduce(
      (earliest, item) => Math.min(earliest, item.time),
      now,
    );
    if (outcomes.length === 0) start = now - 3600;
  } else {
    start = now - Number(selectedWindow);
  }
  if (!Number.isFinite(start) || start >= now) start = now - 3600;
  const span = Math.max(now - start, 1);
  const laneHeight = plotHeight / intervalRows.length;
  const x = (timestamp) => left + ((timestamp - start) / span) * plotWidth;
  const shades = {
    "1m": { correct: "#2ed49a", missed: "#ff6573" },
    "5m": { correct: "#00a000", missed: "#f00000" },
    "15m": { correct: "#008000", missed: "#c00000" },
    "1h": { correct: "#006400", missed: "#a00000" },
    "5 PM ET": { correct: "#009040", missed: "#e00000" },
  };

  svg.append(svgElement("rect", {
    x: left, y: top, width: plotWidth, height: plotHeight, fill: "#0b1118",
  }));
  for (let index = 0; index <= intervalRows.length; index += 1) {
    const y = top + index * laneHeight;
    svg.append(svgElement("line", {
      x1: left, x2: width - right, y1: y, y2: y, class: "chart-grid",
    }));
  }

  const spanSeconds = span;
  const tickEvery = spanSeconds <= 3600 ? 300
    : spanSeconds <= 21600 ? 900
      : spanSeconds <= 86400 ? 3600
        : spanSeconds <= 604800 ? 21600
          : 86400;
  const firstTick = Math.ceil(start / tickEvery) * tickEvery;
  for (let tick = firstTick; tick <= now; tick += tickEvery) {
    const tickX = x(tick);
    svg.append(svgElement("line", {
      x1: tickX, x2: tickX, y1: top, y2: top + plotHeight, class: "chart-grid",
    }));
    const label = svgElement("text", {
      x: tickX, y: height - 11, class: "chart-axis", "text-anchor": "middle",
    });
    label.textContent = spanSeconds >= 86400
      ? new Intl.DateTimeFormat(undefined, {
          timeZone: "America/New_York", month: "numeric", day: "numeric",
        }).format(new Date(tick * 1000))
      : etTime(tick);
    svg.append(label);
  }

  intervalRows.forEach(([key, label], index) => {
    const laneY = top + index * laneHeight;
    const laneLabel = svgElement("text", {
      x: left - 9,
      y: laneY + laneHeight / 2 + 4,
      class: "chart-lane",
      "text-anchor": "end",
    });
    laneLabel.textContent = label;
    svg.append(laneLabel);
  });

  const clipId = "outcome-plot-clip";
  const defs = svgElement("defs");
  const clipPath = svgElement("clipPath", { id: clipId });
  clipPath.append(svgElement("rect", {
    x: left, y: top, width: plotWidth, height: plotHeight,
  }));
  defs.append(clipPath);
  svg.prepend(defs);
  const bars = svgElement("g", { "clip-path": `url(#${clipId})` });
  for (const outcome of outcomes) {
    const rowIndex = intervalRows.findIndex(([key]) => key === outcome.interval);
    if (rowIndex < 0 || outcome.target_time < start || outcome.time > now) continue;
    const startX = x(outcome.time);
    const endX = x(Math.min(outcome.target_time, now));
    const bar = svgElement("rect", {
      x: startX,
      y: top + rowIndex * laneHeight + laneHeight * 0.3,
      width: Math.max(endX - startX, 2),
      height: laneHeight * 0.4,
      fill: shades[outcome.interval][outcome.correct ? "correct" : "missed"],
      class: "chart-bar",
      rx: 1,
    });
    const title = svgElement("title");
    title.textContent =
      `${intervalRows[rowIndex][1]} · ${outcome.correct ? "Correct" : "Miss"} · ` +
      `${etTime(outcome.time)}–${etTime(outcome.target_time)} ET`;
    bar.append(title);
    bars.append(bar);
  }
  svg.append(bars);

  if (outcomes.length === 0) {
    const empty = svgElement("text", {
      x: left + plotWidth / 2,
      y: top + plotHeight / 2,
      class: "chart-empty",
      "text-anchor": "middle",
    });
    empty.textContent = "No resolved outcomes in this time window";
    svg.append(empty);
  }
}

function updateOutcomeSummary(summary) {
  setText(
    "outcome-summary",
    intervalRows.map(([key, label]) => {
      const item = summary[key] || { correct: 0, total: 0 };
      return `${label}: ${item.correct}/${item.total}`;
    }).join("   ·   "),
  );
}

function fillTable(id, rows, columns, emptyMessage) {
  const body = $(id);
  body.replaceChildren();
  if (!rows.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = columns.length;
    cell.className = "empty-state";
    cell.textContent = emptyMessage;
    row.append(cell);
    body.append(row);
    return;
  }
  for (const data of rows) {
    const row = document.createElement("tr");
    for (const column of columns) {
      const cell = document.createElement("td");
      const value = column.value(data);
      cell.textContent = value == null ? "—" : String(value);
      if (column.className) cell.className = column.className(data);
      row.append(cell);
    }
    body.append(row);
  }
}

function updateScoreboard(scoreboard) {
  setText(
    "score-summary",
    `${scoreboard.total.toLocaleString()} saved · ` +
    `${scoreboard.accuracy == null ? "—" : `${(scoreboard.accuracy * 100).toFixed(1)}%`} directional accuracy`,
  );
  fillTable("score-table", scoreboard.recent, [
    { value: (row) => `${etTime(row.time, true)} ET` },
    { value: (row) => `${Math.round((row.horizon - row.time) / 60)} min` },
    { value: (row) => row.direction, className: (row) => row.direction === "UP" ? "call-up" : "call-down" },
    { value: (row) => money(row.predicted) },
    { value: (row) => money(row.actual) },
    { value: (row) => row.correct ? "Correct" : "Miss", className: (row) => row.correct ? "hit" : "miss" },
  ], "No resolved forecasts yet");

  const todayAccuracy = scoreboard.today.total
    ? (scoreboard.today.correct / scoreboard.today.total) * 100
    : null;
  setText(
    "today-summary",
    `${scoreboard.today.total} resolved · ${scoreboard.today.correct} correct`,
  );
  setText(
    "today-accuracy",
    todayAccuracy == null ? "Accuracy today: —" : `Accuracy today: ${todayAccuracy.toFixed(1)}%`,
  );
  $("today-accuracy").className = `muted ${todayAccuracy == null ? "" : todayAccuracy >= 50 ? "direction-up" : "direction-down"}`;
  fillTable("today-table", scoreboard.today_1m, [
    { value: (row) => `${etTime(row.time)} ET` },
    { value: (row) => row.direction, className: (row) => row.direction === "UP" ? "call-up" : "call-down" },
    { value: (row) => `${money(row.predicted, 0)} → ${money(row.actual, 0)}` },
    { value: (row) => row.correct ? "Hit" : "Miss", className: (row) => row.correct ? "hit" : "miss" },
  ], "No resolved 1-minute forecasts today");
}

function updatePaper(paper) {
  const winRate = paper.completed
    ? `${((paper.wins / paper.completed) * 100).toFixed(1)}%`
    : "—";
  setText(
    "paper-summary",
    `Simulation only · ${paper.completed} closed · ${paper.wins} profitable (${winRate}) · ` +
    `net ${money(paper.pnl)} · ${paper.open_count} open · $10 max per entry`,
  );
  if (document.activeElement !== $("fee-bps")) {
    $("fee-bps").value = paper.fee_bps;
  }
  fillTable("paper-table", paper.trades, [
    { value: (trade) => `${Math.round(trade.horizon_seconds / 60)}m` },
    { value: (trade) => trade.state },
    { value: (trade) => `${etTime(trade.entry_time, true)} ET` },
    { value: (trade) => money(trade.entry_price) },
    { value: (trade) => trade.exit_price == null ? "—" : money(trade.exit_price) },
    { value: (trade) => trade.pnl == null ? "—" : money(trade.pnl), className: (trade) => trade.pnl == null ? "" : trade.pnl >= 0 ? "hit" : "miss" },
  ], "No simulated trades yet");
}

function updateTransactions(transactions) {
  const list = $("transactions");
  list.replaceChildren();
  if (!transactions.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Waiting for unconfirmed transactions…";
    list.append(empty);
    return;
  }
  for (const transaction of transactions) {
    const link = document.createElement("a");
    link.className = `transaction-row ${transaction.btc > 50 ? "whale" : ""}`;
    link.href = `https://mempool.space/tx/${encodeURIComponent(transaction.txid)}`;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent =
      `${etTime(transaction.time, true)} · ${transaction.txid.slice(0, 12)}… · ` +
      `${transaction.btc ? `${transaction.btc.toFixed(4)} BTC` : "value unavailable"}`;
    const detail = document.createElement("span");
    detail.textContent =
      `${transaction.fee_rate ? `${transaction.fee_rate.toFixed(1)} sat/vB` : "fee —"} · ${transaction.source}`;
    link.append(detail);
    list.append(link);
  }
}

function updateFeedStatus(status) {
  const list = $("feed-status");
  list.replaceChildren();
  const entries = Object.entries(status);
  if (!entries.length) {
    const item = document.createElement("li");
    item.textContent = "Waiting for feed updates…";
    list.append(item);
    return;
  }
  for (const [source, value] of entries) {
    const item = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = source;
    const state = document.createElement("span");
    state.className = `feed-state ${value.state === "connected" ? "direction-up" : value.state === "error" || value.state === "reconnecting" ? "direction-down" : ""}`;
    state.textContent = value.state.toUpperCase();
    item.append(name, state);
    list.append(item);
  }
}

function updateState(state) {
  latestState = state;
  const connection = $("connection");
  connection.textContent = state.connection;
  connection.className = `connection ${state.connection === "LIVE FEEDS" ? "status-live" : state.connection.includes("ERROR") || state.connection.includes("RECONNECTING") ? "status-error" : "status-waiting"}`;
  if (state.service_error) showNotice(`Dashboard update error: ${state.service_error}`);

  setText("price-value", state.price.value == null ? "Waiting for Coinbase…" : money(state.price.value));
  setText("price-updated", state.price.time == null ? "No price update yet" : `Updated ${etTime(state.price.time, true)} ET`);
  renderPriceChart(state.price_history);
  updateForecasts(state.forecasts);

  const mempool = state.network.mempool;
  setText("mempool-value", mempool == null
    ? "Mempool: loading"
    : `${Number(mempool.count).toLocaleString()} tx · ${(mempool.vsize / 1_000_000).toFixed(2)} M vB`);
  const fees = state.network.fees;
  setText("fee-value", fees && Object.keys(fees).length
    ? `${fees.fastestFee ?? "—"} / ${fees.halfHourFee ?? "—"} / ${fees.hourFee ?? "—"} sat/vB`
    : "Fee estimates loading");

  setText("signal-tx-rate", `${state.signals.tx_rate.toFixed(2)} / sec`);
  setText("signal-btc", `${state.signals.btc_moved.toLocaleString(undefined, { maximumFractionDigits: 2 })} BTC`);
  setText("signal-whales", state.signals.whales.toLocaleString());
  setText("signal-mempool", state.signals.mempool_trend);
  setText("signal-fee", state.signals.fee_pressure == null ? "—" : `${state.signals.fee_pressure} sat/vB`);
  setText("signal-imbalance", `${state.signals.order_book_imbalance >= 0 ? "+" : ""}${state.signals.order_book_imbalance.toFixed(2)}`);

  updateOutcomeSummary(state.outcome_summary);
  renderTimeline(state.timeline);
  updateTransactions(state.transactions);
  updateScoreboard(state.scoreboard);
  updatePaper(state.paper);

  const target = state.daily_target;
  const targetLabel = $("daily-target");
  targetLabel.textContent = target.price == null ? "Building estimate…" : money(target.price);
  targetLabel.className = `daily-target ${target.projected_return == null ? "" : target.projected_return >= 0 ? "direction-up" : "direction-down"}`;
  setText("daily-detail", target.detail);
  updateFeedStatus(state.feed_status);
  if (state.model.selected !== $("model-select").value) {
    $("model-select").value = state.model.selected;
  }
}

async function refreshState() {
  const windowValue = encodeURIComponent($("time-window").value);
  try {
    const response = await fetch(`/api/state?window=${windowValue}`, { cache: "no-store" });
    const state = await response.json();
    if (!response.ok) throw new Error(state.error || `HTTP ${response.status}`);
    updateState(state);
  } catch (error) {
    showNotice(`Could not load dashboard state: ${error.message}`);
  }
}

async function saveSettings(settings) {
  const response = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  await refreshState();
}

$("time-window").addEventListener("change", refreshState);
$("price-window").addEventListener("change", () => {
  if (latestState) renderPriceChart(latestState.price_history);
});
$("model-select").addEventListener("change", async (event) => {
  try {
    await saveSettings({ provider: event.target.value });
    showNotice("Forecast model updated.", false);
  } catch (error) {
    showNotice(error.message);
    await refreshState();
  }
});
$("fee-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await saveSettings({ fee_bps: Number($("fee-bps").value) });
    showNotice("Paper-trading fee assumption updated.", false);
  } catch (error) {
    showNotice(error.message);
    await refreshState();
  }
});

$("open-scenario-tool").addEventListener("click", () => {
  $("scenario-dialog").showModal();
  $("scenario-datetime").focus();
});
$("close-scenario-tool").addEventListener("click", () => {
  $("scenario-dialog").close();
});
$("scenario-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const result = $("scenario-result");
  result.textContent = "Calculating…";
  try {
    const response = await fetch("/api/scenario-likelihood", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_datetime: $("scenario-datetime").value,
        direction: $("scenario-direction").value,
        target_price: Number($("scenario-price").value),
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    result.textContent =
      `${data.probability_percent.toFixed(1)}% likelihood BTC will be ` +
      `${data.direction} $${Number(data.target_price).toLocaleString()} at ` +
      `${etTime(new Date(data.target_time).getTime() / 1000)} ET. ` +
      `${data.explanation}`;
  } catch (error) {
    result.textContent = `Could not calculate likelihood: ${error.message}`;
  }
});

function updateClock() {
  $("clock").textContent = new Intl.DateTimeFormat(undefined, {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date()) + " ET";
}

updateClock();
refreshState();
setInterval(updateClock, 1000);
setInterval(refreshState, 2000);
