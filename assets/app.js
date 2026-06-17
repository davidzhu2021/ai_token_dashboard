const sourceColors = {
  Cursor: "#1f7a5b",
  "Claude Code": "#b88727",
  "其他": "#2e6f9f",
};

const sourceLabels = {
  Cursor: "Codex",
};

let currentUser = null;
let currentView = "dashboard";
let usageData = [];
let usageSummary = null;
let lastPersonalUsageCacheHit = false;
let lastAdminUsageCacheHit = false;
let lastDepartmentUsageCacheHit = false;
let adminUsageData = [];
let adminSummaryData = [];
let adminEmployees = [];
let selectedAdminEmployee = "";
let departmentUsageData = [];
let departmentSummaryData = [];
let departmentRankings = [];
let departmentEmployees = [];
let selectedDepartment = "";
let departmentPickerOpen = false;
let departmentPickerOptions = [];
let modelCatalog = [];
let isDashboardLoading = false;
let isAdminLoading = false;
let isDepartmentLoading = false;
let authConfig = { devLoginEnabled: false, oidcConfigured: false, providerName: "飞书扫码登录" };

const el = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat("zh-CN");
const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });

function formatTokens(value) {
  const num = Number(value || 0);
  if (num >= 1000000) return `${(num / 1000000).toFixed(2)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(1)}K`;
  return fmt.format(num);
}

function initials(email, name) {
  const prefix = (name || email || "员工").trim();
  return prefix.slice(0, 1).toUpperCase();
}

function showToast(message) {
  const toast = el("toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.setTimeout(() => toast.classList.remove("show"), 2200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const payload = await response.json();
      message = typeof payload.detail === "string" ? payload.detail : payload.detail?.error || payload.error || message;
    } catch {}
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

function localDate(date) {
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
}

function selectedDateRange() {
  const days = Number(el("rangeSelect").value || 30);
  const end = new Date();
  const start = new Date(end);
  start.setDate(end.getDate() - days + 1);
  return { startDate: localDate(start), endDate: localDate(end), days };
}

function sum(data, field) {
  return data.reduce((acc, item) => acc + Number(item[field] || 0), 0);
}

function groupBy(data, field) {
  return data.reduce((acc, item) => {
    const key = item[field] || "其他";
    if (!acc[key]) acc[key] = [];
    acc[key].push(item);
    return acc;
  }, {});
}

function aggregateByDate(data) {
  const grouped = groupBy(data, "date");
  return Object.keys(grouped)
    .sort()
    .map((date) => ({
      date,
      promptTokens: sum(grouped[date], "promptTokens"),
      completionTokens: sum(grouped[date], "completionTokens"),
      totalTokens: sum(grouped[date], "totalTokens"),
      spend: sum(grouped[date], "spend"),
    }));
}

function icon(name) {
  return `<svg><use href="#icon-${name}"></use></svg>`;
}

function metric(label, value, sub, chip, tone = "", iconName = "token") {
  return `
    <article class="metric-card">
      <div class="metric-label">
        <div class="metric-title">
          <span class="metric-icon ${tone}">${icon(iconName)}</span>
          <span>${label}</span>
        </div>
        ${chip ? `<span class="chip ${tone}">${chip}</span>` : ""}
      </div>
      <div>
        <div class="metric-value">${value}</div>
        <div class="metric-sub">${sub}</div>
      </div>
    </article>
  `;
}

function metricGroup(title, subtitle, items) {
  return `
    <section class="metric-group">
      <div class="metric-group-head"><div><h3>${title}</h3><p>${subtitle}</p></div></div>
      <div class="metric-pair">${items.join("")}</div>
    </section>
  `;
}

function sourceText() {
  const source = el("sourceSelect").value;
  return source === "all" ? "全部来源" : displaySource(source);
}

function displaySource(source) {
  return sourceLabels[source] || source || "其他";
}

function rangeLabel() {
  return `近 ${el("rangeSelect").value} 天`;
}

function selectedDepartmentInfo() {
  if (!selectedDepartment) return null;
  const matched = departmentRankings.find((item) => item.departmentId === selectedDepartment || item.departmentName === selectedDepartment);
  return {
    id: matched?.departmentId || selectedDepartment,
    name: matched?.departmentName || selectedDepartment,
    bindStatus: matched?.bindStatus || "部门字段",
  };
}

function departmentScopeLabel() {
  return selectedDepartmentInfo()?.name || "全部部门";
}

function metricScopeSuffix(mode) {
  if (mode !== "department") return "";
  if (!selectedDepartment) return " · 全部部门";
  return ` · ${departmentScopeLabel()}`;
}

function renderMetricGroups(containerId, data, mode = "personal", summary = null, splitData = data) {
  const latest = summary?.latestDay || aggregateByDate(data).slice(-1)[0] || {};
  const total = sum(data, "totalTokens");
  const cursor = sum(splitData.filter((item) => item.source === "Cursor"), "totalTokens");
  const cc = sum(splitData.filter((item) => item.source === "Claude Code"), "totalTokens");
  const requests = sum(data, "requestCount");
  const successes = sum(data, "successCount");
  const successRate = requests ? Math.round((successes / requests) * 1000) / 10 : 0;
  const spend = sum(data, "spend");
  const scope = mode === "admin" ? "全员" : mode === "department" ? "部门" : "个人";
  const label = rangeLabel();
  const source = sourceText();
  const scopeSuffix = metricScopeSuffix(mode);

  el(containerId).innerHTML = [
    metricGroup("最近一天", latest.date || "暂无日期", [
      metric("最近一天 Token", formatTokens(latest.totalTokens || 0), latest.date ? `${latest.date} 的整日汇总` : `最新日期${scope}消耗`, "最近", "", "token"),
      metric("最近一天消耗金额", money.format(latest.spend || 0), latest.date ? `${latest.date} 的整日预估金额` : "最新日期预估金额", "最近", "gold", "cost"),
    ]),
    metricGroup("所选范围消耗", `${label} · ${source}${scopeSuffix}`, [
      metric(`${label} Token`, formatTokens(total), "按当前日期与来源筛选累计", source, "gold", "trend"),
      metric(`${label} 消耗金额`, money.format(spend), "按上游记录汇总", "估算", "gold", "cost"),
    ]),
    metricGroup("所选范围请求", `${label} · ${source}${scopeSuffix}`, [
      metric(`${label} 请求次数`, fmt.format(requests), "按当前筛选累计", "请求", "blue", "request"),
      metric(`${label} 请求成功率`, `${successRate}%`, `${fmt.format(successes)} / ${fmt.format(requests)} 次成功`, "稳定", "", "success"),
    ]),
    metricGroup("工具消耗拆分", `${label} · ${source}${scopeSuffix}`, [
      metric(`${label} Codex Token`, formatTokens(cursor), "Codex 相关消耗", "Codex", "", "cursor"),
      metric(`${label} Claude Code Token`, formatTokens(cc), "终端工具相关消耗", "Claude Code", "blue", "terminal"),
    ]),
  ].join("");
}

function renderPersonalMetrics(data) {
  const total = sum(data, "totalTokens");
  const requests = sum(data, "requestCount");
  const successes = sum(data, "successCount");
  const successRate = requests ? Math.round((successes / requests) * 1000) / 10 : 0;
  const label = rangeLabel();
  const source = sourceText();
  el("heroTotal").textContent = formatTokens(total);
  el("heroSuccess").textContent = `${successRate}%`;
  el("heroRequests").textContent = fmt.format(requests);
  el("heroTotalLabel").textContent = `${label} Token`;
  el("trendBadge").textContent = `${label} · ${source}`;
  el("spendBadge").textContent = `${label} · ${source}`;
  renderMetricGroups("metrics", data, "personal", usageSummary);
}

function renderAdminMetrics(data) {
  const totalData = adminSummaryData.length ? adminSummaryData : data;
  const total = sum(totalData, "totalTokens");
  const requests = sum(totalData, "requestCount");
  const label = rangeLabel();
  const source = sourceText();
  el("adminHeroTotal").textContent = formatTokens(total);
  el("adminHeroTotalLabel").textContent = selectedAdminEmployee ? "员工 Token" : "全员 Token";
  el("adminHeroRequests").textContent = fmt.format(requests);
  el("adminActiveUsers").textContent = fmt.format(adminEmployees.length);
  el("adminTrendBadge").textContent = `${label} · ${source}`;
  el("adminSpendBadge").textContent = `${label} · ${source}`;
  renderMetricGroups("adminMetrics", totalData, "admin", null, data);
}

function renderDepartmentMetrics(data) {
  const total = sum(data, "totalTokens");
  const requests = sum(data, "requestCount");
  const label = rangeLabel();
  const source = sourceText();
  const scopeLabel = departmentScopeLabel();
  el("departmentHeroTotal").textContent = formatTokens(total);
  el("departmentHeroTotalLabel").textContent = `${scopeLabel} Token`;
  el("departmentHeroRequests").textContent = fmt.format(requests);
  el("departmentActiveUsers").textContent = fmt.format(selectedDepartment ? departmentEmployees.length : departmentRankings.length);
  el("departmentRequestLabel").textContent = `${scopeLabel}请求次数`;
  el("departmentTrendBadge").textContent = `${label} · ${source}`;
  el("departmentSpendBadge").textContent = `${label} · ${source}`;
  el("departmentWelcomeTitle").textContent = `${scopeLabel} AI 用量总览`;
  el("departmentWelcomeDesc").textContent = selectedDepartment
    ? `当前展示 ${scopeLabel} 在所选日期范围与来源筛选下的员工用量明细。`
    : "当前展示所有部门在所选日期范围与来源筛选下的汇总数据。";
  el("departmentActiveLabel").textContent = selectedDepartment ? "活跃员工" : "活跃部门";
  el("departmentTrendTitle").textContent = `${scopeLabel}每日 Token 趋势`;
  el("departmentTrendDesc").textContent = `按日期汇总${scopeLabel} Prompt 与 Completion Token。`;
  el("departmentSpendTitle").textContent = `${scopeLabel}每日金额消费趋势`;
  el("departmentSpendDesc").textContent = `按日期汇总${scopeLabel}预估消费金额。`;
  el("departmentSourceTitle").textContent = `${scopeLabel}来源占比`;
  el("departmentSourceDesc").textContent = `${scopeLabel} Codex、Claude Code 与其他来源占比。`;
  el("departmentModelTitle").textContent = `${scopeLabel}模型使用排行`;
  el("departmentModelDesc").textContent = `按${scopeLabel}总 Token 消耗排序。`;
  el("departmentSplitTitle").textContent = `${scopeLabel} Prompt / Completion 拆分`;
  el("departmentSplitDesc").textContent = `观察${scopeLabel}输入输出 Token 比例。`;
  renderMetricGroups("departmentMetrics", data, "department");
}

function showChartTooltip(event, html) {
  const tooltip = el("chartTooltip");
  tooltip.innerHTML = html;
  tooltip.classList.add("show");
  const margin = 14;
  const rect = tooltip.getBoundingClientRect();
  let left = event.clientX + 16;
  let top = event.clientY - rect.height - 14;
  if (left + rect.width + margin > window.innerWidth) left = event.clientX - rect.width - 16;
  if (top < margin) top = event.clientY + 16;
  tooltip.style.left = `${Math.max(margin, left)}px`;
  tooltip.style.top = `${Math.max(margin, top)}px`;
}

function hideChartTooltip() {
  el("chartTooltip").classList.remove("show");
}

function tooltipMarkup(date, rows) {
  return `<div class="tooltip-date">${date}</div>${rows.map((row) => `<div class="tooltip-row"><span>${row.label}</span><strong>${row.value}</strong></div>`).join("")}`;
}

function renderEmptyChart(svg, label) {
  svg.setAttribute("viewBox", "0 0 900 280");
  svg.innerHTML = `<rect width="900" height="280" rx="8" fill="#fffdf6"/><text x="450" y="140" fill="#65736f" font-size="16" text-anchor="middle">${label}</text>`;
}

function renderLineChart({ svg, points, valueField, color, fill, axisFormatter, tooltipRows }) {
  if (!points.length) {
    renderEmptyChart(svg, "当前筛选范围暂无数据");
    return;
  }
  const width = 900;
  const height = 280;
  const pad = { left: 54, right: 18, top: 20, bottom: 42 };
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const max = Math.max(1, ...points.map((p) => Number(p[valueField] || 0)));
  const xStep = points.length > 1 ? (width - pad.left - pad.right) / (points.length - 1) : 1;
  const y = (value) => height - pad.bottom - (Number(value || 0) / max) * (height - pad.top - pad.bottom);
  const x = (index) => (points.length > 1 ? pad.left + index * xStep : width / 2);
  const path = points.map((p, index) => `${index ? "L" : "M"} ${x(index)} ${y(p[valueField])}`).join(" ");
  const area = `${path} L ${x(points.length - 1 || 0)} ${height - pad.bottom} L ${x(0)} ${height - pad.bottom} Z`;
  const grid = [0, 0.25, 0.5, 0.75, 1]
    .map((ratio) => {
      const yy = y(max * ratio);
      return `<line x1="${pad.left}" y1="${yy}" x2="${width - pad.right}" y2="${yy}" stroke="#dbe2d5" stroke-dasharray="4 7"/><text x="12" y="${yy + 4}" fill="#65736f" font-size="12">${axisFormatter(max * ratio)}</text>`;
    })
    .join("");
  const dots = points
    .map((p, index) => {
      const cx = x(index);
      const cy = y(p[valueField]);
      return `<circle cx="${cx}" cy="${cy}" r="4.5" fill="${color}"/><circle class="chart-hit" cx="${cx}" cy="${cy}" r="16" fill="transparent" data-tooltip="${encodeURIComponent(tooltipMarkup(p.date, tooltipRows(p)))}"/>`;
    })
    .join("");
  const labelEvery = Math.max(1, Math.ceil(points.length / 5));
  const labels = points
    .filter((_, index) => index === 0 || index === points.length - 1 || index % labelEvery === 0)
    .map((p, index, arr) => {
      const originalIndex = points.findIndex((item) => item.date === p.date);
      return `<text x="${x(originalIndex)}" y="${height - 16}" fill="#65736f" font-size="12" text-anchor="${index === arr.length - 1 ? "end" : "middle"}">${p.date.slice(5)}</text>`;
    })
    .join("");

  svg.innerHTML = `<rect width="${width}" height="${height}" rx="8" fill="#fffdf6"/>${grid}<path d="${area}" fill="${fill}"/><path d="${path}" fill="none" stroke="${color}" stroke-width="4"/>${dots}${labels}`;
  svg.querySelectorAll(".chart-hit").forEach((node) => {
    node.addEventListener("pointermove", (event) => showChartTooltip(event, decodeURIComponent(node.dataset.tooltip)));
    node.addEventListener("pointerleave", hideChartTooltip);
  });
  svg.addEventListener("pointerleave", hideChartTooltip);
}

function renderTrendTo(svgId, data) {
  const points = aggregateByDate(data);
  renderLineChart({
    svg: el(svgId),
    points,
    valueField: "totalTokens",
    color: "#1f7a5b",
    fill: "rgba(31,122,91,.13)",
    axisFormatter: formatTokens,
    tooltipRows: (p) => [
      { label: "总 Token", value: fmt.format(p.totalTokens) },
      { label: "Prompt Token", value: fmt.format(p.promptTokens) },
      { label: "Completion Token", value: fmt.format(p.completionTokens) },
    ],
  });
}

function renderSpendTrendTo(svgId, data) {
  const points = aggregateByDate(data);
  renderLineChart({
    svg: el(svgId),
    points,
    valueField: "spend",
    color: "#b17916",
    fill: "rgba(177,121,22,.13)",
    axisFormatter: (value) => money.format(value),
    tooltipRows: (p) => [{ label: "预估金额", value: money.format(p.spend) }],
  });
}

function renderDonutTo(svgId, totalId, legendId, data) {
  const grouped = groupBy(data, "source");
  const totals = Object.keys(sourceColors).map((source) => ({ source, value: grouped[source] ? sum(grouped[source], "totalTokens") : 0 }));
  const total = totals.reduce((acc, item) => acc + item.value, 0);
  const radius = 68;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;
  const circles = totals
    .map((item) => {
      const part = total ? item.value / total : 0;
      const dash = part * circumference;
      const circle = `<circle cx="90" cy="90" r="${radius}" fill="none" stroke="${sourceColors[item.source]}" stroke-width="18" stroke-dasharray="${dash} ${circumference - dash}" stroke-dashoffset="${-offset}" transform="rotate(-90 90 90)"/>`;
      offset += dash;
      return circle;
    })
    .join("");
  el(svgId).innerHTML = `<circle cx="90" cy="90" r="${radius}" fill="none" stroke="#edf0e8" stroke-width="18"/>${circles}`;
  el(totalId).textContent = formatTokens(total);
  el(legendId).innerHTML = totals
    .map((item) => {
      const pct = total ? Math.round((item.value / total) * 100) : 0;
      return `<div class="legend-item"><span><i class="dot" style="background:${sourceColors[item.source]}"></i>${displaySource(item.source)}</span><strong>${pct}%</strong></div>`;
    })
    .join("");
}

function renderModelBarsTo(containerId, data) {
  const grouped = groupBy(data, "model");
  const rows = Object.keys(grouped)
    .map((model) => ({ model, value: sum(grouped[model], "totalTokens") }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 5);
  const max = Math.max(1, ...rows.map((row) => row.value));
  el(containerId).innerHTML = rows.length
    ? rows
        .map((row) => `<div class="bar-row"><strong>${row.model}</strong><div class="bar-track"><div class="bar-fill" style="width:${Math.max(3, (row.value / max) * 100)}%"></div></div><span class="num">${formatTokens(row.value)}</span></div>`)
        .join("")
    : `<div class="model-empty">当前筛选范围暂无模型用量</div>`;
}

function renderSplitTo(svgId, data) {
  const svg = el(svgId);
  const prompt = sum(data, "promptTokens");
  const completion = sum(data, "completionTokens");
  const total = Math.max(1, prompt + completion);
  const promptWidth = (prompt / total) * 760;
  svg.setAttribute("viewBox", "0 0 820 236");
  svg.innerHTML = `
    <rect width="820" height="236" rx="8" fill="#fffdf6"/>
    <text x="30" y="42" fill="#65736f" font-size="14">Prompt Token</text>
    <rect x="30" y="62" width="760" height="42" rx="6" fill="#edf0e8"/>
    <rect x="30" y="62" width="${promptWidth}" height="42" rx="6" fill="#1f7a5b"/>
    <text x="30" y="134" fill="#14201d" font-size="28" font-weight="800">${formatTokens(prompt)}</text>
    <text x="30" y="176" fill="#65736f" font-size="14">Completion Token</text>
    <rect x="30" y="196" width="760" height="18" rx="6" fill="#b88727"/>
    <text x="660" y="176" fill="#14201d" font-size="28" font-weight="800">${formatTokens(completion)}</text>
  `;
}

function renderTable(data) {
  el("tableCount").textContent = `${data.length} 条`;
  el("usageTable").innerHTML = data.length
    ? data
        .slice()
        .reverse()
        .map((item) => {
          const status = item.failureCount > 0 ? `<span class="chip rose">${item.failureCount} 次失败</span>` : `<span class="chip">正常</span>`;
          return `<tr><td>${item.date}</td><td>${displaySource(item.source)}</td><td>${item.model}</td><td class="num">${fmt.format(item.requestCount || 0)}</td><td class="num">${fmt.format(item.promptTokens || 0)}</td><td class="num">${fmt.format(item.completionTokens || 0)}</td><td class="num"><strong>${fmt.format(item.totalTokens || 0)}</strong></td><td>${status}</td></tr>`;
        })
        .join("")
    : `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:26px">当前筛选范围暂无用量记录</td></tr>`;
}

function sortedAdminEmployees(items) {
  return items.slice().sort((a, b) => {
    const tokenDiff = Number(b.totalTokens || 0) - Number(a.totalTokens || 0);
    if (tokenDiff) return tokenDiff;
    const spendDiff = Number(b.spend || 0) - Number(a.spend || 0);
    if (spendDiff) return spendDiff;
    const requestDiff = Number(b.requestCount || 0) - Number(a.requestCount || 0);
    if (requestDiff) return requestDiff;
    const aName = a.employeeName || a.employeeEmail || a.employeeId || "";
    const bName = b.employeeName || b.employeeEmail || b.employeeId || "";
    return aName.localeCompare(bName, "zh-CN");
  });
}

function sortedDepartments(items) {
  return items.slice().sort((a, b) => {
    const tokenDiff = Number(b.totalTokens || 0) - Number(a.totalTokens || 0);
    if (tokenDiff) return tokenDiff;
    const spendDiff = Number(b.spend || 0) - Number(a.spend || 0);
    if (spendDiff) return spendDiff;
    const requestDiff = Number(b.requestCount || 0) - Number(a.requestCount || 0);
    if (requestDiff) return requestDiff;
    const aName = a.departmentName || a.departmentId || "";
    const bName = b.departmentName || b.departmentId || "";
    return aName.localeCompare(bName, "zh-CN");
  });
}

function departmentOptionKey(item) {
  return item.departmentId || item.departmentName || "";
}

function departmentOptionName(item) {
  return item.departmentName || item.departmentId || "未命名部门";
}

function departmentOptionList() {
  return sortedDepartments(departmentPickerOptions.length ? departmentPickerOptions : departmentRankings);
}

function filteredDepartmentOptions() {
  const keyword = el("departmentEmployeeSearch").value.trim().toLowerCase();
  const options = departmentOptionList();
  if (!keyword) return options;
  return options.filter((item) => {
    const name = String(item.departmentName || "").toLowerCase();
    const id = String(item.departmentId || "").toLowerCase();
    return name.includes(keyword) || id.includes(keyword);
  });
}

function closeDepartmentPicker() {
  departmentPickerOpen = false;
  el("departmentEmployeeSearch").setAttribute("aria-expanded", "false");
  el("departmentDepartmentOptions").classList.add("hidden");
}

function openDepartmentPicker() {
  departmentPickerOpen = true;
  el("departmentEmployeeSearch").setAttribute("aria-expanded", "true");
  el("departmentDepartmentOptions").classList.remove("hidden");
  renderDepartmentPickerOptions();
}

function renderDepartmentPickerOptions() {
  const optionsEl = el("departmentDepartmentOptions");
  optionsEl.innerHTML = "";
  if (!departmentPickerOpen) return;

  const allButton = document.createElement("button");
  allButton.type = "button";
  allButton.className = "department-option all";
  allButton.setAttribute("role", "option");
  allButton.innerHTML = "<strong>全部部门</strong><span>查看所有部门汇总排行</span>";
  allButton.addEventListener("click", () => selectAllDepartments());
  optionsEl.appendChild(allButton);

  const options = filteredDepartmentOptions();
  if (!options.length) {
    const empty = document.createElement("div");
    empty.className = "department-option";
    empty.innerHTML = "<strong>暂无匹配部门</strong><span>可点击搜索继续按输入内容查询</span>";
    optionsEl.appendChild(empty);
    return;
  }

  options.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "department-option";
    button.setAttribute("role", "option");

    const title = document.createElement("strong");
    title.textContent = departmentOptionName(item);
    const meta = document.createElement("span");
    meta.textContent = `ID：${item.departmentId || "未绑定部门"} · Token：${formatTokens(item.totalTokens || 0)} · 活跃员工：${fmt.format(item.activeEmployees || 0)}`;

    button.append(title, meta);
    button.addEventListener("click", () => selectDepartmentOption(item));
    optionsEl.appendChild(button);
  });
}

async function selectDepartmentOption(item) {
  selectedDepartment = departmentOptionKey(item);
  el("departmentEmployeeSearch").value = departmentOptionName(item);
  closeDepartmentPicker();
  await loadDepartmentData();
}

async function selectAllDepartments() {
  selectedDepartment = "";
  el("departmentEmployeeSearch").value = "";
  closeDepartmentPicker();
  await loadDepartmentData();
}

async function runDepartmentSearch() {
  const search = el("departmentEmployeeSearch").value.trim();
  if (!search) {
    await selectAllDepartments();
    return;
  }
  const match = filteredDepartmentOptions()[0];
  if (match) {
    await selectDepartmentOption(match);
    return;
  }
  selectedDepartment = "";
  closeDepartmentPicker();
  await loadDepartmentData();
}

function employeeSummariesFromRows(rows) {
  const grouped = {};
  const sourceTotals = {};
  rows.forEach((row) => {
    const employeeId = row.employeeId || row.employeeEmail || "mock-employee";
    if (!grouped[employeeId]) {
      grouped[employeeId] = {
        employeeId,
        employeeName: row.employeeName || employeeId,
        employeeEmail: row.employeeEmail || "",
        bindStatus: row.bindStatus || "未绑定部门",
        promptTokens: 0,
        completionTokens: 0,
        totalTokens: 0,
        requestCount: 0,
        successCount: 0,
        failureCount: 0,
        spend: 0,
        primarySource: "其他",
      };
      sourceTotals[employeeId] = {};
    }
    grouped[employeeId].promptTokens += Number(row.promptTokens || 0);
    grouped[employeeId].completionTokens += Number(row.completionTokens || 0);
    grouped[employeeId].totalTokens += Number(row.totalTokens || 0);
    grouped[employeeId].requestCount += Number(row.requestCount || 0);
    grouped[employeeId].successCount += Number(row.successCount || 0);
    grouped[employeeId].failureCount += Number(row.failureCount || 0);
    grouped[employeeId].spend += Number(row.spend || 0);
    sourceTotals[employeeId][row.source || "其他"] = (sourceTotals[employeeId][row.source || "其他"] || 0) + Number(row.totalTokens || 0);
  });
  Object.keys(grouped).forEach((employeeId) => {
    const sources = Object.entries(sourceTotals[employeeId]);
    if (sources.length) grouped[employeeId].primarySource = sources.sort((a, b) => b[1] - a[1])[0][0];
  });
  return sortedAdminEmployees(Object.values(grouped));
}

function renderEmployeeRanking(tableId, countId, employees, emptyText) {
  const sorted = sortedAdminEmployees(employees);
  el(countId).textContent = `${sorted.length} 人`;
  el(tableId).innerHTML = sorted.length
    ? sorted
        .map((item) => {
          const requests = Number(item.requestCount || 0);
          const successRate = requests ? Math.round((Number(item.successCount || 0) / requests) * 1000) / 10 : 0;
          return `
            <tr class="admin-employee-row" data-employee="${item.employeeEmail || item.employeeId}">
              <td><strong>${item.employeeName || item.employeeId}</strong></td>
              <td>${item.employeeEmail || "未绑定邮箱"}</td>
              <td>${displaySource(item.primarySource)}</td>
              <td class="num">${fmt.format(requests)}</td>
              <td class="num"><strong>${formatTokens(item.totalTokens || 0)}</strong></td>
              <td class="num">${money.format(item.spend || 0)}</td>
              <td class="num">${successRate}%</td>
              <td><span class="chip ${item.bindStatus === "未绑定邮箱" ? "rose" : "blue"}">${item.bindStatus || "已绑定邮箱"}</span></td>
            </tr>
          `;
        })
        .join("")
    : `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:26px">${emptyText}</td></tr>`;
}

function renderDepartmentRanking(tableId, countId, departments, emptyText) {
  const sorted = sortedDepartments(departments);
  el(countId).textContent = `${sorted.length} 个部门`;
  el(tableId).innerHTML = sorted.length
    ? sorted
        .map((item) => {
          const requests = Number(item.requestCount || 0);
          const successRate = requests ? Math.round((Number(item.successCount || 0) / requests) * 1000) / 10 : 0;
          return `
            <tr class="admin-employee-row" data-department="${item.departmentId}">
              <td><strong>${item.departmentName || item.departmentId}</strong></td>
              <td>${item.departmentId || "未绑定部门"}</td>
              <td>${displaySource(item.primarySource)}</td>
              <td class="num">${fmt.format(requests)}</td>
              <td class="num"><strong>${formatTokens(item.totalTokens || 0)}</strong></td>
              <td class="num">${money.format(item.spend || 0)}</td>
              <td class="num">${successRate}%</td>
              <td><span class="chip ${item.bindStatus === "未绑定部门" ? "rose" : "blue"}">${item.bindStatus || "已绑定部门"}</span></td>
            </tr>
          `;
        })
        .join("")
    : `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:26px">${emptyText}</td></tr>`;
}

function renderAdminUsers() {
  renderEmployeeRanking("adminUserTable", "adminUserCount", adminEmployees, "当前筛选范围暂无员工用量");
}

function renderDepartmentUsers() {
  const scopeLabel = departmentScopeLabel();
  el("departmentBackButton").classList.toggle("hidden", !selectedDepartment);
  if (selectedDepartment) {
    el("departmentRankingTitle").textContent = `${scopeLabel}员工排行`;
    el("departmentRankingDesc").textContent = `当前展示 ${scopeLabel} 内员工用量，默认按 Token 从高到低排序。`;
    renderEmployeeRanking("departmentUserTable", "departmentUserCount", departmentEmployees, "当前筛选范围暂无部门员工用量");
  } else {
    el("departmentRankingTitle").textContent = "部门用量排行";
    el("departmentRankingDesc").textContent = "当前展示全部部门汇总排行，点击部门行查看该部门员工排行。";
    renderDepartmentRanking("departmentUserTable", "departmentUserCount", departmentRankings, "当前筛选范围暂无部门用量");
  }
}

function loadingLine(width = "100%") {
  return `<div class="loading-line" style="width:${width}"></div>`;
}

function renderMetricSkeleton(containerId) {
  el(containerId).innerHTML = Array.from({ length: 4 })
    .map(
      (_, index) => `
        <section class="metric-group" aria-busy="true">
          <div class="metric-group-head">
            <div>
              <div class="loading-status">
                <span class="loading-pill" style="width:28px"></span>
                <span>${index === 0 ? "数据加载中" : "正在汇总"}</span>
              </div>
              <div style="margin-top:8px">${loadingLine("62%")}</div>
            </div>
          </div>
          <div class="metric-pair">
            <article class="loading-card">
              ${loadingLine("46%")}
              <div>
                <div class="loading-block" style="width:72%;height:30px"></div>
                <div style="margin-top:10px">${loadingLine("58%")}</div>
              </div>
            </article>
            <article class="loading-card">
              ${loadingLine("54%")}
              <div>
                <div class="loading-block" style="width:64%;height:30px"></div>
                <div style="margin-top:10px">${loadingLine("50%")}</div>
              </div>
            </article>
          </div>
        </section>
      `,
    )
    .join("");
}

function renderChartSkeleton(svgId) {
  const svg = el(svgId);
  svg.setAttribute("viewBox", "0 0 900 280");
  svg.innerHTML = `
    <rect width="900" height="280" rx="8" fill="#fffdf6"/>
    <text x="450" y="126" fill="#65736f" font-size="16" font-weight="800" text-anchor="middle">数据加载中</text>
    <text x="450" y="154" fill="#8a938f" font-size="13" text-anchor="middle">正在从后端汇总当前筛选范围</text>
    <rect x="64" y="196" width="772" height="14" rx="7" fill="#e5ebe3"/>
    <rect x="64" y="224" width="512" height="10" rx="5" fill="#edf1ea"/>
  `;
}

function renderDonutSkeleton(totalId, legendId) {
  el(totalId).textContent = "--";
  el(legendId).innerHTML = `
    <div class="loading-status"><span class="loading-pill"></span><span>数据加载中</span></div>
    <div style="margin-top:18px">${loadingLine("86%")}</div>
    <div style="margin-top:14px">${loadingLine("72%")}</div>
    <div style="margin-top:14px">${loadingLine("64%")}</div>
  `;
}

function renderBarsSkeleton(containerId) {
  el(containerId).innerHTML = Array.from({ length: 5 })
    .map(
      (_, index) => `
        <div class="bar-row">
          <strong><span class="loading-line" style="display:block;width:${70 - index * 6}px"></span></strong>
          <div class="bar-track"><div class="bar-fill" style="width:${78 - index * 10}%;background:#dfe6de"></div></div>
          <span class="num">--</span>
        </div>
      `,
    )
    .join("");
}

function renderSplitSkeleton(svgId) {
  const svg = el(svgId);
  svg.setAttribute("viewBox", "0 0 820 236");
  svg.innerHTML = `
    <rect width="820" height="236" rx="8" fill="#fffdf6"/>
    <text x="410" y="102" fill="#65736f" font-size="16" font-weight="800" text-anchor="middle">数据加载中</text>
    <rect x="30" y="132" width="760" height="24" rx="8" fill="#e5ebe3"/>
    <rect x="30" y="176" width="520" height="16" rx="8" fill="#edf1ea"/>
  `;
}

function renderTableSkeleton(tableId, countId, colSpan, label = "数据加载中") {
  if (countId) el(countId).textContent = label;
  el(tableId).innerHTML = Array.from({ length: 5 })
    .map(
      () => `
        <tr>
          <td colspan="${colSpan}">
            <div class="loading-table-row" aria-busy="true">
              ${loadingLine("74%")}
              ${loadingLine("62%")}
              ${loadingLine("82%")}
              ${loadingLine("55%")}
              ${loadingLine("68%")}
            </div>
          </td>
        </tr>
      `,
    )
    .join("");
}

function renderPersonalLoading() {
  const label = rangeLabel();
  const source = sourceText();
  el("heroTotal").textContent = "加载中";
  el("heroSuccess").textContent = "--";
  el("heroRequests").textContent = "--";
  el("heroTotalLabel").textContent = `${label} Token`;
  el("trendBadge").textContent = `${label} · ${source}`;
  el("spendBadge").textContent = `${label} · ${source}`;
  renderMetricSkeleton("metrics");
  renderChartSkeleton("trendChart");
  renderChartSkeleton("spendChart");
  renderDonutSkeleton("donutTotal", "sourceLegend");
  renderBarsSkeleton("modelBars");
  renderSplitSkeleton("splitChart");
  renderTableSkeleton("usageTable", "tableCount", 8);
}

function renderAdminLoading() {
  const label = rangeLabel();
  const source = sourceText();
  el("adminHeroTotal").textContent = "加载中";
  el("adminHeroTotalLabel").textContent = selectedAdminEmployee ? "员工 Token" : "全员 Token";
  el("adminHeroRequests").textContent = "--";
  el("adminActiveUsers").textContent = "--";
  el("adminTrendBadge").textContent = `${label} · ${source}`;
  el("adminSpendBadge").textContent = `${label} · ${source}`;
  el("adminLimitHint").textContent = "数据加载中";
  renderMetricSkeleton("adminMetrics");
  renderChartSkeleton("adminTrendChart");
  renderChartSkeleton("adminSpendChart");
  renderDonutSkeleton("adminDonutTotal", "adminSourceLegend");
  renderBarsSkeleton("adminModelBars");
  renderSplitSkeleton("adminSplitChart");
  renderTableSkeleton("adminUserTable", "adminUserCount", 8);
}

function renderDepartmentLoading() {
  const label = rangeLabel();
  const source = sourceText();
  const scopeLabel = departmentScopeLabel();
  el("departmentHeroTotal").textContent = "加载中";
  el("departmentHeroTotalLabel").textContent = `${scopeLabel} Token`;
  el("departmentHeroRequests").textContent = "--";
  el("departmentRequestLabel").textContent = `${scopeLabel}请求次数`;
  el("departmentActiveUsers").textContent = "--";
  el("departmentActiveLabel").textContent = selectedDepartment ? "活跃员工" : "活跃部门";
  el("departmentTrendBadge").textContent = `${label} · ${source}`;
  el("departmentSpendBadge").textContent = `${label} · ${source}`;
  el("departmentLimitHint").textContent = "数据加载中";
  renderMetricSkeleton("departmentMetrics");
  renderChartSkeleton("departmentTrendChart");
  renderChartSkeleton("departmentSpendChart");
  renderDonutSkeleton("departmentDonutTotal", "departmentSourceLegend");
  renderBarsSkeleton("departmentModelBars");
  renderSplitSkeleton("departmentSplitChart");
  renderTableSkeleton("departmentUserTable", "departmentUserCount", 8);
}

function renderPersonal() {
  if (isDashboardLoading) {
    renderPersonalLoading();
    return;
  }
  renderPersonalMetrics(usageData);
  renderTrendTo("trendChart", usageData);
  renderSpendTrendTo("spendChart", usageData);
  renderDonutTo("sourceDonut", "donutTotal", "sourceLegend", usageData);
  renderModelBarsTo("modelBars", usageData);
  renderSplitTo("splitChart", usageData);
  renderTable(usageData);
}

function renderAdmin() {
  if (isAdminLoading) {
    renderAdminLoading();
    return;
  }
  const totalData = adminSummaryData.length ? adminSummaryData : adminUsageData;
  renderAdminMetrics(adminUsageData);
  renderTrendTo("adminTrendChart", totalData);
  renderSpendTrendTo("adminSpendChart", totalData);
  renderDonutTo("adminSourceDonut", "adminDonutTotal", "adminSourceLegend", adminUsageData);
  renderModelBarsTo("adminModelBars", adminUsageData);
  renderSplitTo("adminSplitChart", adminUsageData);
  renderAdminUsers();

  const detailCard = el("adminDetailCard");
  detailCard.classList.toggle("show", Boolean(selectedAdminEmployee));
  if (selectedAdminEmployee) {
    const employee = adminEmployees.find((item) => item.employeeEmail === selectedAdminEmployee || item.employeeId === selectedAdminEmployee);
    el("adminDetailTitle").textContent = `${employee?.employeeName || selectedAdminEmployee} 的用量详情`;
    el("adminDetailSubtitle").textContent = employee?.employeeEmail || employee?.employeeId || selectedAdminEmployee;
  }
}

function renderDepartment() {
  if (isDepartmentLoading) {
    renderDepartmentLoading();
    return;
  }
  const totalData = departmentSummaryData.length ? departmentSummaryData : departmentUsageData;
  renderDepartmentMetrics(totalData);
  renderTrendTo("departmentTrendChart", totalData);
  renderSpendTrendTo("departmentSpendChart", totalData);
  renderDonutTo("departmentSourceDonut", "departmentDonutTotal", "departmentSourceLegend", departmentUsageData);
  renderModelBarsTo("departmentModelBars", departmentUsageData);
  renderSplitTo("departmentSplitChart", departmentUsageData);
  renderDepartmentUsers();
  renderDepartmentPickerOptions();

  const detailCard = el("departmentDetailCard");
  detailCard.classList.toggle("show", Boolean(selectedDepartment));
  if (selectedDepartment) {
    const department = selectedDepartmentInfo();
    el("departmentDetailTitle").textContent = `${department.name} 的部门详情`;
    el("departmentDetailSubtitle").textContent = `部门 ID：${department.id} · 数据来源：${department.bindStatus} · 下方排行已切换为该部门员工用量`;
  }
}

function render() {
  renderPersonal();
  if (currentUser?.isAdmin) renderAdmin();
  if (currentUser?.isAdmin) renderDepartment();
}

function uniqueValues(items, getter) {
  return [...new Set(items.flatMap((item) => getter(item)).filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh-CN"));
}

function setupModelFilters() {
  const providers = uniqueValues(modelCatalog, (item) => [item.provider]);
  const capabilities = uniqueValues(modelCatalog, (item) => item.capabilities || []);
  const currentProvider = el("providerFilter").value || "all";
  const currentCapability = el("capabilityFilter").value || "all";
  el("providerFilter").innerHTML = [`<option value="all">全部供应商</option>`, ...providers.map((provider) => `<option value="${provider}">${provider}</option>`)].join("");
  el("capabilityFilter").innerHTML = [`<option value="all">全部能力</option>`, ...capabilities.map((capability) => `<option value="${capability}">${capability}</option>`)].join("");
  el("providerFilter").value = providers.includes(currentProvider) ? currentProvider : "all";
  el("capabilityFilter").value = capabilities.includes(currentCapability) ? currentCapability : "all";
}

function filteredModels() {
  const keyword = el("modelSearch").value.trim().toLowerCase();
  const provider = el("providerFilter").value;
  const capability = el("capabilityFilter").value;
  return modelCatalog.filter((model) => {
    const matchesKeyword = !keyword || model.modelName.toLowerCase().includes(keyword) || model.provider.toLowerCase().includes(keyword) || String(model.recommendedFor || "").toLowerCase().includes(keyword);
    const matchesProvider = provider === "all" || model.provider === provider;
    const matchesCapability = capability === "all" || (model.capabilities || []).includes(capability);
    return matchesKeyword && matchesProvider && matchesCapability;
  });
}

function renderModels() {
  const models = filteredModels();
  el("modelCount").textContent = fmt.format(models.length);
  if (!models.length) {
    el("modelGrid").innerHTML = `<article class="panel model-empty">没有找到匹配的模型，请调整筛选条件。</article>`;
    return;
  }
  el("modelGrid").innerHTML = models
    .map(
      (model) => `
        <article class="model-card">
          <div class="model-card-head">
            <div class="key-title">
              <span class="key-icon">${icon("model")}</span>
              <div><h3 class="model-name">${model.modelName}</h3><div class="provider">${model.provider}</div></div>
            </div>
            <span class="chip ${model.status === "推荐" || model.status === "默认" ? "" : "blue"}">${model.status || "可用"}</span>
          </div>
          <p class="model-desc">${model.description || "当前账号可用模型。"}</p>
          <div class="tag-row">${(model.capabilities || ["通用"]).map((capability) => `<span class="chip blue">${capability}</span>`).join("")}</div>
          <button class="copy-btn" type="button" data-copy-model="${model.modelName}"><span class="app-icon">${icon("copy")}</span>复制模型名称</button>
        </article>
      `,
    )
    .join("");
}

async function copyText(text, successMessage) {
  try {
    await navigator.clipboard.writeText(text);
    showToast(successMessage);
  } catch {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    document.body.appendChild(textarea);
    textarea.select();
    const copied = document.execCommand("copy");
    textarea.remove();
    showToast(copied ? successMessage : "复制失败，请手动选中复制");
  }
}

function switchView(view) {
  if (view === "admin" && !currentUser?.isAdmin) view = "dashboard";
  if (view === "department" && !currentUser?.isAdmin) view = "dashboard";
  currentView = view;
  el("dashboardView").classList.toggle("hidden", view !== "dashboard");
  el("adminView").classList.toggle("hidden", view !== "admin");
  el("departmentView").classList.toggle("hidden", view !== "department");
  el("modelsView").classList.toggle("hidden", view !== "models");
  el("dashboardFilters").classList.toggle("hidden", view === "models");
  document.querySelectorAll("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  if (view === "models") {
    renderModels();
    if (!modelCatalog.length) loadModels();
  }
  if (view === "dashboard" && !usageData.length) loadDashboardData();
  if (view === "admin" && !adminUsageData.length) loadAdminData();
  if (view === "department" && !departmentUsageData.length) loadDepartmentData();
}

async function loadCurrentViewData(forceRefresh = false) {
  if (currentView === "models") return loadModels();
  if (currentView === "admin") return loadAdminData(forceRefresh);
  if (currentView === "department") return loadDepartmentData(forceRefresh);
  return loadDashboardData(forceRefresh);
}

async function loadDashboardData(forceRefresh = false) {
  if (!currentUser || isDashboardLoading) return;
  isDashboardLoading = true;
  renderPersonal();
  const { startDate, endDate } = selectedDateRange();
  const source = el("sourceSelect").value;
  try {
    const payload = await api(`/api/me/usage?start_date=${encodeURIComponent(startDate)}&end_date=${encodeURIComponent(endDate)}&source=${encodeURIComponent(source)}${forceRefresh ? "&refresh=1" : ""}`);
    usageData = payload.rows || [];
    usageSummary = payload.summary || null;
    lastPersonalUsageCacheHit = Boolean(payload.cache?.hit);
  } catch (error) {
    showToast(error.message || "用量数据加载失败");
    usageData = [];
    usageSummary = null;
  } finally {
    isDashboardLoading = false;
    renderPersonal();
  }
}

async function loadAdminData(forceRefresh = false) {
  if (!currentUser?.isAdmin || isAdminLoading) return;
  isAdminLoading = true;
  renderAdmin();
  const { startDate, endDate } = selectedDateRange();
  const source = el("sourceSelect").value;
  const search = el("adminEmployeeSearch").value.trim();
  const employee = selectedAdminEmployee || search;
  const query = new URLSearchParams({ start_date: startDate, end_date: endDate, source });
  if (employee) query.set("employee", employee);
  if (forceRefresh) query.set("refresh", "1");
  try {
    const payload = await api(`/api/admin/usage?${query.toString()}`);
    adminUsageData = payload.rows || [];
    adminSummaryData = payload.summaryRows || adminUsageData;
    adminEmployees = payload.employees || [];
    lastAdminUsageCacheHit = Boolean(payload.cache?.hit);
    if (payload.truncated) {
      el("adminLimitHint").textContent = `默认按 Token 从高到低排序；日志读取达到上限（已读 ${payload.pagesRead || 0}/${payload.totalPages || "?"} 页），员工排行可能不完整`;
    } else {
      el("adminLimitHint").textContent = `默认按 Token 从高到低排序；已读取 ${payload.pagesRead || 0} 页日志，按当前筛选范围统计`;
    }
  } catch (error) {
    showToast(error.message || "全员数据加载失败");
    adminUsageData = [];
    adminSummaryData = [];
    adminEmployees = [];
  } finally {
    isAdminLoading = false;
    renderAdmin();
  }
}

async function loadDepartmentData(forceRefresh = false) {
  if (!currentUser?.isAdmin || isDepartmentLoading) return;
  isDepartmentLoading = true;
  renderDepartment();
  const { startDate, endDate } = selectedDateRange();
  const source = el("sourceSelect").value;
  const search = el("departmentEmployeeSearch").value.trim();
  const department = selectedDepartment || search;
  const query = new URLSearchParams({ start_date: startDate, end_date: endDate, source });
  if (department) query.set("department", department);
  if (forceRefresh) query.set("refresh", "1");
  try {
    const payload = await api(`/api/admin/departments/usage?${query.toString()}`);
    departmentUsageData = payload.rows || [];
    departmentSummaryData = payload.summaryRows || departmentUsageData;
    departmentRankings = payload.departments || [];
    departmentEmployees = payload.employees || [];
    if (!department) departmentPickerOptions = departmentRankings;
    lastDepartmentUsageCacheHit = Boolean(payload.cache?.hit);
    const rankingSubject = selectedDepartment ? "员工排行" : "部门排行";
    if (payload.truncated) {
      el("departmentLimitHint").textContent = `${rankingSubject}默认按 Token 从高到低排序；日志读取达到上限（已读 ${payload.pagesRead || 0}/${payload.totalPages || "?"} 页），排行可能不完整`;
    } else {
      el("departmentLimitHint").textContent = `${rankingSubject}默认按 Token 从高到低排序；已读取 ${payload.pagesRead || 0} 页日志，按当前筛选范围统计`;
    }
  } catch (error) {
    showToast(error.message || "部门数据加载失败");
    departmentUsageData = [];
    departmentSummaryData = [];
    departmentRankings = [];
    departmentEmployees = [];
    if (!department) departmentPickerOptions = [];
  } finally {
    isDepartmentLoading = false;
    renderDepartment();
  }
}

async function loadModels() {
  try {
    const payload = await api("/api/models");
    modelCatalog = payload.models || [];
    setupModelFilters();
    renderModels();
  } catch (error) {
    modelCatalog = [];
    setupModelFilters();
    renderModels();
    showToast(error.message || "模型列表加载失败");
  }
}

async function showApp(user) {
  currentUser = user;
  el("loginView").classList.add("hidden");
  el("appView").classList.remove("hidden");
  el("adminTab").classList.toggle("hidden", !user.isAdmin);
  el("departmentTab").classList.toggle("hidden", !user.isAdmin);
  el("userEmail").textContent = user.email;
  el("userName").textContent = user.name;
  el("avatar").textContent = user.avatar || initials(user.email, user.name);
  el("welcomeTitle").textContent = `${user.name}您好，今天的 AI 工具消耗一眼看清`;
  el("adminWelcomeTitle").textContent = `${user.name}您好，全员 AI 用量一眼看清`;
  el("departmentWelcomeTitle").textContent = `${user.name}您好，部门 AI 用量一眼看清`;
  switchView(user.isAdmin ? "admin" : "dashboard");
  render();
  await Promise.all([loadCurrentViewData(), loadModels()]);
}

function showLogin() {
  currentUser = null;
  selectedAdminEmployee = "";
  selectedDepartment = "";
  departmentPickerOpen = false;
  usageData = [];
  usageSummary = null;
  adminUsageData = [];
  adminSummaryData = [];
  adminEmployees = [];
  departmentUsageData = [];
  departmentSummaryData = [];
  departmentRankings = [];
  departmentEmployees = [];
  departmentPickerOptions = [];
  el("departmentEmployeeSearch").value = "";
  closeDepartmentPicker();
  el("appView").classList.add("hidden");
  el("loginView").classList.remove("hidden");
}

document.addEventListener("submit", async (event) => {
  if (event.target.id !== "loginForm") return;
  event.preventDefault();
  if (!authConfig.devLoginEnabled) {
    window.location.href = "/api/auth/sso/start";
    return;
  }
  const email = el("emailInput").value.trim();
  try {
    const user = await api("/api/auth/dev-login", { method: "POST", body: JSON.stringify({ email }) });
    await showApp(user);
  } catch (error) {
    showToast(error.message || "登录失败，请确认账号是否存在");
  }
});

el("ssoButton").addEventListener("click", () => {
  if (!authConfig.oidcConfigured) {
    showToast("企业统一认证参数尚未配置");
    return;
  }
  window.location.href = "/api/auth/sso/start";
});

el("logoutButton").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST", body: JSON.stringify({}) });
  } catch {}
  showLogin();
});

document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));

el("rangeSelect").addEventListener("change", async () => {
  selectedAdminEmployee = "";
  selectedDepartment = "";
  el("departmentEmployeeSearch").value = "";
  departmentPickerOptions = [];
  closeDepartmentPicker();
  await loadCurrentViewData();
});

el("sourceSelect").addEventListener("change", async () => {
  selectedAdminEmployee = "";
  selectedDepartment = "";
  el("departmentEmployeeSearch").value = "";
  departmentPickerOptions = [];
  closeDepartmentPicker();
  await loadCurrentViewData();
});

el("refreshButton").addEventListener("click", async () => {
  if (currentView === "models") {
    await loadModels();
    showToast("\u5df2\u5237\u65b0\u6a21\u578b\u5217\u8868");
  } else if (currentView === "admin") {
    await loadAdminData(true);
    showToast(lastAdminUsageCacheHit ? "\u5df2\u52a0\u8f7d\u7f13\u5b58\u5168\u5458\u6570\u636e" : "\u5df2\u5237\u65b0\u5168\u5458\u7528\u91cf\u6570\u636e");
  } else if (currentView === "department") {
    await loadDepartmentData(true);
    showToast(lastDepartmentUsageCacheHit ? "\u5df2\u52a0\u8f7d\u7f13\u5b58\u90e8\u95e8\u6570\u636e" : "\u5df2\u5237\u65b0\u90e8\u95e8\u7528\u91cf\u6570\u636e");
  } else {
    await loadDashboardData(true);
    showToast(lastPersonalUsageCacheHit ? "\u5df2\u52a0\u8f7d\u7f13\u5b58\u7528\u91cf\u6570\u636e" : "\u5df2\u5237\u65b0\u771f\u5b9e\u7528\u91cf\u6570\u636e");
  }
});

el("adminSearchButton").addEventListener("click", async () => {
  selectedAdminEmployee = "";
  await loadAdminData();
});

el("adminEmployeeSearch").addEventListener("keydown", async (event) => {
  if (event.key === "Enter") {
    selectedAdminEmployee = "";
    await loadAdminData();
  }
});

el("adminUserTable").addEventListener("click", async (event) => {
  const row = event.target.closest("[data-employee]");
  if (!row) return;
  selectedAdminEmployee = row.dataset.employee;
  el("adminEmployeeSearch").value = "";
  await loadAdminData();
});

el("adminClearEmployee").addEventListener("click", async () => {
  selectedAdminEmployee = "";
  el("adminEmployeeSearch").value = "";
  await loadAdminData();
});

el("departmentSearchButton").addEventListener("click", async () => {
  await runDepartmentSearch();
});

el("departmentEmployeeSearch").addEventListener("keydown", async (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    await runDepartmentSearch();
  } else if (event.key === "Escape") {
    closeDepartmentPicker();
  }
});

el("departmentEmployeeSearch").addEventListener("focus", openDepartmentPicker);
el("departmentEmployeeSearch").addEventListener("click", openDepartmentPicker);
el("departmentEmployeeSearch").addEventListener("input", () => {
  selectedDepartment = "";
  openDepartmentPicker();
});

document.addEventListener("click", (event) => {
  if (!el("departmentDepartmentPicker").contains(event.target) && event.target !== el("departmentSearchButton")) {
    closeDepartmentPicker();
  }
});

el("departmentUserTable").addEventListener("click", async (event) => {
  const row = event.target.closest("[data-department]");
  if (!row) return;
  selectedDepartment = row.dataset.department;
  el("departmentEmployeeSearch").value = "";
  closeDepartmentPicker();
  await loadDepartmentData();
});

el("departmentClearEmployee").addEventListener("click", async () => {
  selectedDepartment = "";
  el("departmentEmployeeSearch").value = "";
  closeDepartmentPicker();
  await loadDepartmentData();
});

el("departmentBackButton").addEventListener("click", async () => {
  selectedDepartment = "";
  el("departmentEmployeeSearch").value = "";
  closeDepartmentPicker();
  await loadDepartmentData();
});

el("modelSearch").addEventListener("input", renderModels);
el("providerFilter").addEventListener("change", renderModels);
el("capabilityFilter").addEventListener("change", renderModels);
el("modelGrid").addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy-model]");
  if (button) copyText(button.dataset.copyModel, "模型名称已复制");
});

async function init() {
  try {
    authConfig = await api("/api/auth/config");
  } catch {
    authConfig = { devLoginEnabled: false, oidcConfigured: false, providerName: "飞书扫码登录" };
  }
  el("ssoButton").lastChild.textContent = authConfig.providerName || "飞书扫码登录";
  el("devLoginArea").classList.toggle("hidden", !authConfig.devLoginEnabled);
  el("devLoginButton").classList.toggle("hidden", !authConfig.devLoginEnabled);
  el("emailInput").required = Boolean(authConfig.devLoginEnabled);
  el("loginHint").textContent = authConfig.devLoginEnabled
    ? `开发登录已启用，仅允许 ${authConfig.allowedEmailDomain || "公司邮箱"} 账号；生产环境请关闭。`
    : "使用公司飞书账号扫码登录；本页面不会保存真实密码、认证令牌或管理员密钥。";
  setupModelFilters();
  try {
    const user = await api("/api/auth/me");
    await showApp(user);
  } catch {
    showLogin();
  }
}

init();

