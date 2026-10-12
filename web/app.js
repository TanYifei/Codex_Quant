const COLORS = ["#287c5c", "#d5913c", "#6374a9", "#bb6862", "#459aa0", "#8b6ba8", "#93a342", "#466a8d"];
const state = { strategies: [], runs: [], currentRun: null, status: null, editorId: null, toastTimer: null, selectedIds: new Set() };
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) },
  });
  const data = response.status === 204 ? null : await response.json();
  if (!response.ok) throw new Error(data?.error || `请求失败 (${response.status})`);
  return data;
}

function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", isError);
  node.classList.add("show");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => node.classList.remove("show"), 3500);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function money(value, digits = 0) {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: digits, minimumFractionDigits: digits }).format(Number(value) || 0);
}

function dateLabel(value, options = {}) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", ...options }).format(date);
}

function setDefaultDates() {
  const now = new Date();
  const end = now.toISOString().slice(0, 10);
  const start = new Date(now);
  start.setFullYear(start.getFullYear() - 3);
  $("input[name='start']").value = start.toISOString().slice(0, 10);
  $("input[name='end']").value = end;
}

function pageName(page) {
  return ({ backtest: "回测比较", strategies: "策略库", paper: "模拟交易" })[page] || "回测比较";
}

function navigate(page) {
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.page === page));
  $$(".page").forEach((section) => section.classList.toggle("active", section.id === `page-${page}`));
  $("#current-section").textContent = pageName(page);
  if (page === "paper") refreshPaper();
}

async function loadStatus() {
  try {
    state.status = await api("/api/status");
    const pill = $("#connection-status");
    const ready = state.status.alpaca_keys_configured;
    pill.className = `status-pill ${ready ? "connected" : ""}`;
    pill.innerHTML = `<i></i><span>${ready ? "模拟盘密钥已配置" : "模拟盘密钥未配置"}</span>`;
    updateRunnerEligibility();
  } catch (error) {
    $("#connection-status").className = "status-pill error";
    $("#connection-status").innerHTML = `<i></i><span>本机服务异常</span>`;
  }
}

async function loadStrategies() {
  const oldSelected = new Set($$("#strategy-picker input:checked").map((input) => input.value));
  state.strategies = await api("/api/strategies");
  if (!state.selectedIds.size) state.selectedIds = new Set(state.strategies.slice(0, Math.min(2, state.strategies.length)).map((strategy) => strategy.id));
  else state.selectedIds = new Set([...state.selectedIds].filter((id) => state.strategies.some((strategy) => strategy.id === id)));
  if (oldSelected.size) state.selectedIds = new Set([...oldSelected].filter((id) => state.strategies.some((strategy) => strategy.id === id)));
  renderStrategyPicker();
  renderStrategyLibrary();
  renderRunnerStrategySelect();
  updateRunnerEligibility();
}

function renderRunnerStrategySelect() {
  const select = $("#runner-strategy");
  if (!select) return;
  const previous = select.value;
  select.innerHTML = state.strategies.length
    ? state.strategies.map((strategy) => `<option value="${escapeHtml(strategy.id)}">${escapeHtml(strategy.name)} · v${strategy.active_version}</option>`).join("")
    : '<option value="">请先创建策略</option>';
  if (state.strategies.some((strategy) => strategy.id === previous)) select.value = previous;
}

function updateRunnerEligibility() {
  const button = $("#start-runner");
  if (!button) return;
  button.disabled = !state.status?.alpaca_keys_configured || !state.status?.paper_trading_enabled || !state.strategies.length;
}

function renderStrategyPicker() {
  const picker = $("#strategy-picker");
  if (!state.strategies.length) {
    picker.innerHTML = '<div class="history-empty">还没有策略，请先在策略库中新建。</div>';
    $("#strategy-count").textContent = "0 / 8";
    return;
  }
  picker.innerHTML = state.strategies.map((strategy, index) => `
    <label class="strategy-check"><input type="checkbox" value="${escapeHtml(strategy.id)}" ${state.selectedIds.has(strategy.id) ? "checked" : ""}>
      <span>${escapeHtml(strategy.name)}</span><small>v${strategy.active_version}</small></label>`).join("");
  $$("input[type='checkbox']", picker).forEach((input) => input.addEventListener("change", () => {
    if (input.checked && state.selectedIds.size > 8) {
      input.checked = false;
      toast("一次最多比较 8 个策略。", true);
      return;
    }
    if (input.checked) state.selectedIds.add(input.value); else state.selectedIds.delete(input.value);
    $("#strategy-count").textContent = `${state.selectedIds.size} / 8`;
  }));
  $("#strategy-count").textContent = `${state.selectedIds.size} / 8`;
}

function renderStrategyLibrary() {
  const list = $("#strategy-list");
  if (!state.strategies.length) {
    list.innerHTML = '<div class="card history-empty">还没有策略。新建一个策略，设置进场和出场规则。</div>';
    return;
  }
  list.innerHTML = state.strategies.map((strategy) => {
    const def = strategy.definition;
    return `<article class="strategy-card">
      <div class="strategy-card-top"><div class="strategy-title-wrap"><div class="strategy-glyph">⌁</div><div><h3>${escapeHtml(strategy.name)}</h3><div class="version-tag">VERSION ${strategy.active_version}</div></div></div></div>
      <p class="strategy-description">${escapeHtml(def.description || "尚未添加策略说明。")}</p>
      <div class="rule-preview"><code>进场 · ${escapeHtml(def.entry_rule)}</code><code>出场 · ${escapeHtml(def.exit_rule)}</code></div>
      <div class="strategy-card-footer"><span class="strategy-updated">更新于 ${escapeHtml(dateLabel(strategy.updated_at))}</span><div class="card-actions">
        <button class="icon-button edit-strategy" data-id="${escapeHtml(strategy.id)}">编辑</button><button class="icon-button duplicate-strategy" data-id="${escapeHtml(strategy.id)}">复制</button><button class="icon-button danger delete-strategy" data-id="${escapeHtml(strategy.id)}" title="归档策略">归档</button>
      </div></div></article>`;
  }).join("");
  $$(".edit-strategy", list).forEach((button) => button.addEventListener("click", () => openEditor(button.dataset.id)));
  $$(".duplicate-strategy", list).forEach((button) => button.addEventListener("click", () => duplicateStrategy(button.dataset.id)));
  $$(".delete-strategy", list).forEach((button) => button.addEventListener("click", () => deleteStrategy(button.dataset.id)));
}

function openEditor(id = null, definition = null) {
  state.editorId = id;
  const strategy = id ? state.strategies.find((item) => item.id === id) : null;
  const def = definition || strategy?.definition || { name: "", description: "", entry_rule: "", exit_rule: "" };
  $("#editor-empty").classList.add("hidden");
  $("#strategy-form").classList.remove("hidden");
  $("#editor-title").textContent = id ? "编辑策略" : "新建策略";
  $("#editor-eyebrow").textContent = id ? "STRATEGY EDITOR" : "NEW STRATEGY";
  $("#editor-version").textContent = id ? `当前 v${strategy.active_version} · 保存后 v${strategy.active_version + 1}` : "初始版本 v1";
  const form = $("#strategy-form");
  form.elements.id.value = id || "";
  form.elements.name.value = def.name || "";
  form.elements.description.value = def.description || "";
  form.elements.entry_rule.value = def.entry_rule || "";
  form.elements.exit_rule.value = def.exit_rule || "";
  $("#strategy-editor").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function closeEditor() {
  state.editorId = null;
  $("#strategy-form").reset();
  $("#strategy-form").classList.add("hidden");
  $("#editor-empty").classList.remove("hidden");
}

async function saveStrategy(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  delete data.id;
  const editingId = state.editorId;
  const button = $("button[type='submit']", form);
  button.disabled = true;
  try {
    await api(editingId ? `/api/strategies/${editingId}` : "/api/strategies", { method: editingId ? "PUT" : "POST", body: JSON.stringify(data) });
    await loadStrategies();
    closeEditor();
    toast(editingId ? "已保存为新策略版本。" : "策略已创建。" );
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function duplicateStrategy(id) {
  const strategy = state.strategies.find((item) => item.id === id);
  if (!strategy) return;
  try {
    const result = await api(`/api/strategies/${id}/duplicate`, { method: "POST", body: JSON.stringify({ name: `${strategy.name} 副本` }) });
    await loadStrategies();
    openEditor(result.id);
    toast("策略副本已创建。" );
  } catch (error) { toast(error.message, true); }
}

async function deleteStrategy(id) {
  const strategy = state.strategies.find((item) => item.id === id);
  if (!strategy || !window.confirm(`归档策略“${strategy.name}”？旧版本和回测记录会保留。`)) return;
  try {
    await api(`/api/strategies/${id}`, { method: "DELETE" });
    await loadStrategies();
    if (state.editorId === id) closeEditor();
    toast("策略已归档。" );
  } catch (error) { toast(error.message, true); }
}

async function submitBacktest(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  data.strategy_ids = $$("#strategy-picker input:checked").map((input) => input.value);
  data.capital = Number(data.capital);
  data.fee_bps = Number(data.fee_bps);
  data.slippage_bps = Number(data.slippage_bps);
  const button = $("#run-backtest");
  button.disabled = true;
  button.innerHTML = '<span class="loading-dot"></span> 正在计算…';
  try {
    const result = await api("/api/backtests", { method: "POST", body: JSON.stringify(data) });
    state.currentRun = result;
    renderRun(result);
    await loadRuns();
    toast(`回测完成 · ${result.settings.bar_count.toLocaleString()} 根 K 线。`);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.innerHTML = '<span class="play-icon">▶</span>运行回测'; }
}

function renderRun(run) {
  state.currentRun = run;
  $("#empty-results").classList.add("hidden");
  $("#run-results").classList.remove("hidden");
  const settings = run.settings;
  const sourceLabel = ({ yfinance: "Yahoo Finance · yfinance", demo: "旧版演示合成数据", alpaca: "旧版 Alpaca 行情" })[settings.source] || "Yahoo Finance · yfinance";
  $("#active-range").textContent = `${settings.start} — ${settings.end}`;
  $("#result-meta").textContent = `${settings.symbol} · ${settings.timeframe} · ${settings.bar_count.toLocaleString()} 根 K 线 · ${sourceLabel} · $${Number(settings.capital).toLocaleString()} 初始资金`;
  const bestReturn = [...run.results].sort((a, b) => b.metrics.total_return_pct - a.metrics.total_return_pct)[0];
  const lowestDrawdown = [...run.results].sort((a, b) => a.metrics.max_drawdown_pct - b.metrics.max_drawdown_pct)[0];
  const bestSharpe = [...run.results].sort((a, b) => b.metrics.sharpe - a.metrics.sharpe)[0];
  const topMetrics = [
    { label: "最高总收益", value: signedPct(bestReturn.metrics.total_return_pct), sub: bestReturn.name, tone: bestReturn.metrics.total_return_pct >= 0 ? "positive" : "negative" },
    { label: "最小最大回撤", value: `${lowestDrawdown.metrics.max_drawdown_pct.toFixed(2)}%`, sub: lowestDrawdown.name, tone: "" },
    { label: "最高 Sharpe", value: bestSharpe.metrics.sharpe.toFixed(2), sub: bestSharpe.name, tone: "" },
    { label: "策略数量", value: `${run.results.length} 个`, sub: `同一组 ${settings.bar_count.toLocaleString()} 根 K 线`, tone: "" },
  ];
  $("#metrics-grid").innerHTML = topMetrics.map((metric) => `<div class="metric-card"><div class="metric-label">${escapeHtml(metric.label)}</div><div class="metric-value ${metric.tone}">${escapeHtml(metric.value)}</div><div class="metric-sub">${escapeHtml(metric.sub)}</div></div>`).join("");
  const benchmark = run.benchmark?.curve?.length ? run.benchmark : null;
  const benchmarkNote = $("#benchmark-note");
  benchmarkNote.textContent = benchmark
    ? `${settings.symbol} 持有基准按首根 K 线收盘价归一化到初始资金，不计手续费和滑点；灰色虚线为股票基准。`
    : "这次回测记录中没有股票基准行情。请确认已重启本地工作台服务后重新运行回测；旧记录没有保存基准价格，无法补画。";
  benchmarkNote.classList.toggle("benchmark-missing", !benchmark);
  const strategyLegend = run.results.map((result, index) => `<span class="legend-item"><i class="legend-swatch" style="background:${COLORS[index % COLORS.length]}"></i>${escapeHtml(result.name)} v${result.version}</span>`);
  if (benchmark) strategyLegend.push(`<span class="legend-item"><i class="benchmark-swatch"></i>${escapeHtml(benchmark.name)}</span>`);
  $("#equity-legend").innerHTML = strategyLegend.join("");
  $("#metrics-table").innerHTML = run.results.map((result, index) => {
    const m = result.metrics;
    return `<tr><td><span class="table-strategy-name"><i class="table-color" style="background:${COLORS[index % COLORS.length]}"></i>${escapeHtml(result.name)} <small>v${result.version}</small></span></td><td class="${m.total_return_pct >= 0 ? "positive" : "negative"}">${signedPct(m.total_return_pct)}</td><td>${signedPct(m.annualized_return_pct)}</td><td class="negative">${m.max_drawdown_pct.toFixed(2)}%</td><td>${m.sharpe.toFixed(2)}</td><td>${m.closed_trades}</td><td>${m.win_rate_pct == null ? "—" : `${m.win_rate_pct.toFixed(1)}%`}</td></tr>`;
  }).join("");
  drawChart($("#equity-chart"), run.results, benchmark);
}

function signedPct(value) { return `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)}%`; }

function drawChart(canvas, results, benchmark = null) {
  if (!canvas || !results?.length) return;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const width = rect.width, height = rect.height;
  const margin = { left: 59, right: 8, top: 9, bottom: 23 };
  const plotW = width - margin.left - margin.right, plotH = height - margin.top - margin.bottom;
  const lines = results.map((result, index) => ({ curve: result.curve, color: COLORS[index % COLORS.length], dashed: false }));
  if (benchmark?.curve?.length) lines.push({ curve: benchmark.curve, color: "#8b9690", dashed: true });
  let values = lines.flatMap((line) => line.curve.map((point) => Number(point.equity))).filter(Number.isFinite);
  if (!values.length) return;
  let min = Math.min(...values), max = Math.max(...values);
  const pad = Math.max((max - min) * .1, max * .025, 1); min -= pad; max += pad;
  if (max === min) { max += 1; min -= 1; }
  const y = (value) => margin.top + (max - value) / (max - min) * plotH;
  ctx.clearRect(0, 0, width, height);
  ctx.font = "9px 'DM Mono', monospace";
  ctx.textBaseline = "middle";
  for (let tick = 0; tick <= 4; tick++) {
    const value = max - (max - min) * tick / 4;
    const py = y(value);
    ctx.strokeStyle = "#edf1ee"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(margin.left, py); ctx.lineTo(width - margin.right, py); ctx.stroke();
    ctx.fillStyle = "#9aa59f";
    const label = money(value, 0);
    ctx.textAlign = "right"; ctx.fillText(label, margin.left - 8, py);
  }
  const count = Math.max(...lines.map((line) => line.curve.length));
  [0, Math.floor((count - 1) / 2), count - 1].forEach((index, labelIndex, labels) => {
    if (index < 0) return;
    const point = lines[0].curve[Math.min(index, lines[0].curve.length - 1)];
    const x = margin.left + (labels.length === 1 ? 0 : index / Math.max(count - 1, 1)) * plotW;
    ctx.fillStyle = "#9aa59f"; ctx.textBaseline = "top";
    ctx.textAlign = labelIndex === 0 ? "left" : (labelIndex === labels.length - 1 ? "right" : "center");
    ctx.fillText(dateLabel(point?.timestamp), x, height - 15);
  });
  lines.forEach((series) => {
    const line = series.curve;
    if (!line.length) return;
    ctx.beginPath();
    line.forEach((point, index) => {
      const x = margin.left + index / Math.max(line.length - 1, 1) * plotW;
      const py = y(Number(point.equity));
      if (index === 0) ctx.moveTo(x, py); else ctx.lineTo(x, py);
    });
    ctx.strokeStyle = series.color;
    ctx.lineWidth = series.dashed ? 1.8 : 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.setLineDash(series.dashed ? [5, 4] : []);
    ctx.stroke();
    ctx.setLineDash([]);
  });
}

async function loadRuns() {
  try {
    state.runs = await api("/api/runs");
    renderHistory();
  } catch (error) { toast(error.message, true); }
}

function renderHistory() {
  const container = $("#run-history");
  if (!state.runs.length) {
    container.innerHTML = '<div class="history-empty">完成回测后，记录会显示在这里。</div>';
    return;
  }
  container.innerHTML = state.runs.map((run) => `<div class="history-row" data-id="${escapeHtml(run.id)}"><span class="history-symbol">${escapeHtml(run.symbol)}</span><span class="history-details">${escapeHtml(run.settings.start)} — ${escapeHtml(run.settings.end)} · ${escapeHtml(run.settings.timeframe)}</span><span class="history-time">${escapeHtml(dateLabel(run.created_at, { hour: "2-digit", minute: "2-digit" }))}</span><span class="history-open">查看 →</span></div>`).join("");
  $$(".history-row", container).forEach((row) => row.addEventListener("click", () => openRun(row.dataset.id)));
}

async function openRun(id) {
  try {
    const run = await api(`/api/runs/${id}`);
    renderRun(run);
    navigate("backtest");
  } catch (error) { toast(error.message, true); }
}

async function refreshPaper() {
  const accountNode = $("#paper-account");
  const ordersNode = $("#paper-orders");
  const orderButton = $("#submit-order");
  await refreshRunners();
  accountNode.innerHTML = '<div class="paper-placeholder"><div class="loader"></div><p>正在读取模拟账户…</p></div>';
  orderButton.disabled = true;
  try {
    if (!state.status) state.status = await api("/api/status");
    updateRunnerEligibility();
    if (!state.status.alpaca_keys_configured || !state.status.paper_trading_enabled) {
      const reason = !state.status.alpaca_keys_configured
        ? "请在项目根目录的 .env 文件中配置 Alpaca API 密钥。"
        : "模拟交易默认关闭。确认使用模拟账户后，在 .env 中设置 ALPACA_PAPER_TRADING_ENABLED=true 并重启服务。";
      accountNode.innerHTML = `<div class="account-alert"><strong>模拟账户尚未连接</strong>${escapeHtml(reason)}<br>服务只请求 Alpaca 的模拟交易地址。</div>`;
      ordersNode.innerHTML = '<div class="history-empty">连接模拟账户后显示订单。</div>';
      return;
    }
    const [account, orders] = await Promise.all([api("/api/paper/account"), api("/api/paper/orders")]);
    accountNode.innerHTML = `<div class="account-status"><i></i>账户状态：${escapeHtml(account.status || "已连接")} · ${escapeHtml(account.currency || "USD")}</div><div class="account-grid">
      <div class="account-stat"><span>账户净值</span><strong>${money(account.equity)}</strong></div><div class="account-stat"><span>现金余额</span><strong>${money(account.cash)}</strong></div>
      <div class="account-stat"><span>可用购买力</span><strong>${money(account.buying_power)}</strong></div><div class="account-stat"><span>投资组合价值</span><strong>${money(account.portfolio_value)}</strong></div></div>`;
    orderButton.disabled = false;
    renderOrders(orders);
  } catch (error) {
    accountNode.innerHTML = `<div class="account-alert"><strong>连接失败</strong>${escapeHtml(error.message)}</div>`;
    ordersNode.innerHTML = '<div class="history-empty">无法读取订单。</div>';
  }
}

async function testPaperConnection() {
  const button = $("#test-paper-connection");
  const result = $("#paper-connection-result");
  button.disabled = true;
  button.textContent = "正在测试…";
  result.className = "connection-test-result is-pending";
  result.textContent = "正在验证 Alpaca 模拟盘 API 密钥…";
  try {
    await api("/api/paper/test-connection", { method: "POST" });
    result.className = "connection-test-result is-success";
    result.textContent = "API 密钥连接成功 · Alpaca 模拟盘可访问。";
  } catch (error) {
    result.className = "connection-test-result is-error";
    result.textContent = `连接失败 · ${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = "测试密钥连接";
  }
}

async function refreshRunners() {
  try {
    const runners = await api("/api/paper/runners");
    renderRunners(runners);
  } catch (error) {
    $("#paper-runners").innerHTML = `<div class="history-empty">${escapeHtml(error.message)}</div>`;
  }
}

function renderRunners(runners) {
  const container = $("#paper-runners");
  if (!runners.length) {
    container.innerHTML = '<div class="history-empty">尚无模拟策略运行记录。</div>';
    return;
  }
  const statusText = { running: "运行中", stopped: "已停止", interrupted: "中断", error: "异常" };
  container.innerHTML = runners.map((runner) => `<div class="runner-row">
    <div class="runner-state ${runner.status === "running" ? "is-running" : ""}"><i></i>${statusText[runner.status] || runner.status}</div>
    <div class="runner-main"><strong>${escapeHtml(runner.symbol)} · ${escapeHtml(runner.strategy_name)} v${runner.strategy_version}</strong><small>${escapeHtml(runner.timeframe)} · ${escapeHtml(runner.qty)} 股 · ${runner.position_open ? "策略持仓标记：有" : "策略持仓标记：无"}</small></div>
    <div class="runner-detail"><span>${escapeHtml(runner.last_signal || "等待数据")}</span><small>${runner.last_bar ? `最新已处理 K 线 ${escapeHtml(dateLabel(runner.last_bar, { hour: "2-digit", minute: "2-digit" }))}` : `启动于 ${escapeHtml(dateLabel(runner.created_at))}`}</small>${runner.last_error ? `<em title="${escapeHtml(runner.last_error)}">${escapeHtml(runner.last_error)}</em>` : ""}</div>
    ${runner.status === "running" ? `<button class="icon-button danger stop-runner" data-id="${escapeHtml(runner.id)}">停止</button>` : `<span class="runner-closed">${runner.stopped_at ? escapeHtml(dateLabel(runner.stopped_at)) : "—"}</span>`}
  </div>`).join("");
  $$(".stop-runner", container).forEach((button) => button.addEventListener("click", () => stopRunner(button.dataset.id)));
}

async function startRunner(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  data.symbol = data.symbol.trim().toUpperCase();
  data.qty = Number(data.qty);
  const strategy = state.strategies.find((item) => item.id === data.strategy_id);
  if (!strategy) { toast("请先选择一个策略。", true); return; }
  const message = `启动 ${data.symbol} · ${strategy.name} v${strategy.active_version}？\n\n策略满足条件时会自动向 Alpaca 模拟账户提交 ${data.qty} 股的市价订单。`;
  if (!window.confirm(message)) return;
  const button = $("#start-runner");
  button.disabled = true;
  try {
    await api("/api/paper/runners", { method: "POST", body: JSON.stringify(data) });
    toast("模拟策略运行器已启动。首次先记录最新 K 线，之后等待新信号。" );
    await refreshPaper();
  } catch (error) { toast(error.message, true); updateRunnerEligibility(); }
}

async function stopRunner(id) {
  try {
    const runner = (await api("/api/paper/runners")).find((item) => item.id === id);
    if (!runner || !window.confirm(`停止 ${runner.symbol} · ${runner.strategy_name} v${runner.strategy_version}？`)) return;
    await api(`/api/paper/runners/${id}/stop`, { method: "POST", body: JSON.stringify({}) });
    await refreshRunners();
    toast("模拟策略运行器已停止。" );
  } catch (error) { toast(error.message, true); }
}

function renderOrders(orders) {
  const container = $("#paper-orders");
  if (!Array.isArray(orders) || !orders.length) {
    container.innerHTML = '<div class="history-empty">模拟账户目前没有订单。</div>';
    return;
  }
  container.innerHTML = `<div class="table-wrap"><table class="order-table"><thead><tr><th>时间</th><th>代码</th><th>方向</th><th>股数</th><th>类型</th><th>状态</th></tr></thead><tbody>${orders.map((order) => {
    const side = String(order.side || "").toLowerCase();
    return `<tr><td>${escapeHtml(dateLabel(order.submitted_at || order.created_at, { hour: "2-digit", minute: "2-digit" }))}</td><td>${escapeHtml(order.symbol || "—")}</td><td class="${side === "buy" ? "order-side-buy" : "order-side-sell"}">${side === "buy" ? "买入" : "卖出"}</td><td>${escapeHtml(order.qty || order.notional || "—")}</td><td>${escapeHtml(order.type || "—")}</td><td>${escapeHtml(order.status || "—")}</td></tr>`;
  }).join("")}</tbody></table></div>`;
}

async function submitPaperOrder(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  const symbol = data.symbol.toUpperCase().trim();
  if (!window.confirm(`确认向 Alpaca 模拟账户提交 ${data.side === "buy" ? "买入" : "卖出"} ${data.qty} 股 ${symbol} 的市场订单？`)) return;
  const button = $("#submit-order");
  button.disabled = true;
  try {
    const order = await api("/api/paper/orders", { method: "POST", body: JSON.stringify({ ...data, symbol, qty: Number(data.qty) }) });
    toast(`模拟订单已提交 · ${order.id ? String(order.id).slice(0, 8) : order.status || "成功"}`);
    await refreshPaper();
  } catch (error) { toast(error.message, true); button.disabled = false; }
}

function attachEvents() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.page)));
  $("#backtest-form").addEventListener("submit", submitBacktest);
  $("#strategy-form").addEventListener("submit", saveStrategy);
  $("#new-strategy").addEventListener("click", () => { navigate("strategies"); openEditor(); });
  $("#editor-create").addEventListener("click", () => openEditor());
  $("#cancel-edit").addEventListener("click", closeEditor);
  $("#refresh-runs").addEventListener("click", loadRuns);
  $("#refresh-paper").addEventListener("click", refreshPaper);
  $("#test-paper-connection").addEventListener("click", testPaperConnection);
  $("#order-form").addEventListener("submit", submitPaperOrder);
  $("#runner-form").addEventListener("submit", startRunner);
  setInterval(() => {
    if ($("#page-paper").classList.contains("active")) refreshRunners();
  }, 15000);
  window.addEventListener("resize", () => {
    if (state.currentRun && $("#page-backtest").classList.contains("active")) {
      drawChart($("#equity-chart"), state.currentRun.results, state.currentRun.benchmark);
    }
  });
}

async function init() {
  setDefaultDates();
  attachEvents();
  try {
    await Promise.all([loadStatus(), loadStrategies(), loadRuns()]);
  } catch (error) { toast(error.message, true); }
}

document.addEventListener("DOMContentLoaded", init);
