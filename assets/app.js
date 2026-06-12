const sourceColors = {
  Cursor: "#1f7a5b",
  "Claude Code": "#b88727",
  "其他": "#2e6f9f",
};

let currentUser = null;
let usageData = [];
let accessKeys = [];
let modelCatalog = [];
let currentKeyId = null;
let currentNewKey = "";
let isLoading = false;
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

function renderMetrics(data) {
  const sorted = [...data].sort((a, b) => a.date.localeCompare(b.date));
  const today = sorted[sorted.length - 1] || {};
  const total = sum(data, "totalTokens");
  const cursor = sum(data.filter((item) => item.source === "Cursor"), "totalTokens");
  const cc = sum(data.filter((item) => item.source === "Claude Code"), "totalTokens");
  const requests = sum(data, "requestCount");
  const successes = sum(data, "successCount");
  const successRate = requests ? Math.round((successes / requests) * 1000) / 10 : 0;
  const spend = sum(data, "spend");
  const rangeDays = el("rangeSelect").value;
  const sourceText = el("sourceSelect").value === "all" ? "全部来源" : el("sourceSelect").value;
  const rangeLabel = `近 ${rangeDays} 天`;

  el("heroTotal").textContent = formatTokens(total);
  el("heroSuccess").textContent = `${successRate}%`;
  el("heroRequests").textContent = fmt.format(requests);
  el("heroTotalLabel").textContent = `${rangeLabel} Token`;
  el("trendBadge").textContent = `${rangeLabel} · ${sourceText}`;
  el("spendBadge").textContent = `${rangeLabel} · ${sourceText}`;

  el("metrics").innerHTML = [
    metric("最近一天 Token", formatTokens(today.totalTokens || 0), `${today.date || "-"} 的个人消耗`, "最近", "", "token"),
    metric("最近一天消耗金额", money.format(today.spend || 0), `${today.date || "-"} 的预估金额`, "最近", "gold", "cost"),
    metric(`${rangeLabel} Token`, formatTokens(total), "按当前日期与来源筛选累计", sourceText, "gold", "trend"),
    metric("请求次数", fmt.format(requests), "按当前筛选累计", "请求", "blue", "request"),
    metric("Cursor Token", formatTokens(cursor), "编辑器相关消耗", "Cursor", "", "cursor"),
    metric("Claude Code Token", formatTokens(cc), "终端工具相关消耗", "Claude Code", "blue", "terminal"),
    metric("请求成功率", `${successRate}%`, `${fmt.format(successes)} / ${fmt.format(requests)} 次成功`, "稳定", "", "success"),
    metric("预估成本", money.format(spend), "按上游记录汇总", "估算", "gold", "cost"),
  ].join("");
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
  return `
    <div class="tooltip-date">${date}</div>
    ${rows.map((row) => `<div class="tooltip-row"><span>${row.label}</span><strong>${row.value}</strong></div>`).join("")}
  `;
}

function renderEmptyChart(svg, label) {
  svg.setAttribute("viewBox", "0 0 900 280");
  svg.innerHTML = `
    <rect width="900" height="280" rx="8" fill="#fffdf6"/>
    <text x="450" y="140" fill="#65736f" font-size="16" text-anchor="middle">${label}</text>
  `;
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
  const x = (index) => points.length > 1 ? pad.left + index * xStep : width / 2;
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
      return `
        <circle cx="${cx}" cy="${cy}" r="4.5" fill="${color}"/>
        <circle class="chart-hit" cx="${cx}" cy="${cy}" r="16" fill="transparent" data-tooltip="${encodeURIComponent(tooltipMarkup(p.date, tooltipRows(p)))}"/>
      `;
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

  svg.innerHTML = `
    <rect width="${width}" height="${height}" rx="8" fill="#fffdf6"/>
    ${grid}
    <path d="${area}" fill="${fill}"/>
    <path d="${path}" fill="none" stroke="${color}" stroke-width="4"/>
    ${dots}
    ${labels}
  `;
  svg.querySelectorAll(".chart-hit").forEach((node) => {
    node.addEventListener("pointermove", (event) => showChartTooltip(event, decodeURIComponent(node.dataset.tooltip)));
    node.addEventListener("pointerleave", hideChartTooltip);
  });
  svg.addEventListener("pointerleave", hideChartTooltip);
}

function renderTrend(data) {
  const points = aggregateByDate(data);
  renderLineChart({
    svg: el("trendChart"),
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

function renderSpendTrend(data) {
  const points = aggregateByDate(data);
  renderLineChart({
    svg: el("spendChart"),
    points,
    valueField: "spend",
    color: "#b17916",
    fill: "rgba(177,121,22,.13)",
    axisFormatter: (value) => money.format(value),
    tooltipRows: (p) => [{ label: "预估金额", value: money.format(p.spend) }],
  });
}

function renderDonut(data) {
  const grouped = groupBy(data, "source");
  const totals = Object.keys(sourceColors).map((source) => ({
    source,
    value: grouped[source] ? sum(grouped[source], "totalTokens") : 0,
  }));
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
  el("sourceDonut").innerHTML = `<circle cx="90" cy="90" r="${radius}" fill="none" stroke="#edf0e8" stroke-width="18"/>${circles}`;
  el("donutTotal").textContent = formatTokens(total);
  el("sourceLegend").innerHTML = totals
    .map((item) => {
      const pct = total ? Math.round((item.value / total) * 100) : 0;
      return `<div class="legend-item"><span><i class="dot" style="background:${sourceColors[item.source]}"></i>${item.source}</span><strong>${pct}%</strong></div>`;
    })
    .join("");
}

function renderModelBars(data) {
  const grouped = groupBy(data, "model");
  const rows = Object.keys(grouped)
    .map((model) => ({ model, value: sum(grouped[model], "totalTokens") }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 5);
  const max = Math.max(1, ...rows.map((row) => row.value));
  el("modelBars").innerHTML = rows.length
    ? rows
        .map((row) => {
          const width = Math.max(3, (row.value / max) * 100);
          return `<div class="bar-row"><strong>${row.model}</strong><div class="bar-track"><div class="bar-fill" style="width:${width}%"></div></div><span class="num">${formatTokens(row.value)}</span></div>`;
        })
        .join("")
    : `<div class="model-empty">当前筛选范围暂无模型用量</div>`;
}

function renderSplit(data) {
  const svg = el("splitChart");
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
          return `
            <tr>
              <td>${item.date}</td>
              <td>${item.source}</td>
              <td>${item.model}</td>
              <td class="num">${fmt.format(item.requestCount || 0)}</td>
              <td class="num">${fmt.format(item.promptTokens || 0)}</td>
              <td class="num">${fmt.format(item.completionTokens || 0)}</td>
              <td class="num"><strong>${fmt.format(item.totalTokens || 0)}</strong></td>
              <td>${status}</td>
            </tr>
          `;
        })
        .join("")
    : `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:26px">当前筛选范围暂无用量记录</td></tr>`;
}

function renderKeys() {
  el("keyGrid").innerHTML = accessKeys.length
    ? accessKeys
        .map(
          (key) => `
        <article class="key-card">
          <div class="key-head">
            <div class="key-title">
              <span class="key-icon">${icon("key")}</span>
              <div>
                <h4>${key.name}</h4>
                <div class="masked">${key.masked}</div>
              </div>
            </div>
            <span class="chip ${key.status === "正常" ? "" : "blue"}">${key.status}</span>
          </div>
          <p class="key-purpose">${key.purpose || "用于个人 AI 工具访问。"}</p>
          <div class="key-meta">
            <div class="meta-box"><span>最后使用</span><strong>${key.lastUsed || "-"}</strong></div>
            <div class="meta-box"><span>累计消耗</span><strong>${formatTokens(key.monthTokens || 0)}</strong></div>
          </div>
          <button class="danger-btn" type="button" data-regenerate="${encodeURIComponent(key.id)}">
            <span class="app-icon">${icon("refresh")}</span>
            更新密钥
          </button>
        </article>
      `,
        )
        .join("")
    : `<article class="panel model-empty">当前账号暂无可管理的访问密钥。</article>`;
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
    const matchesKeyword =
      !keyword ||
      model.modelName.toLowerCase().includes(keyword) ||
      model.provider.toLowerCase().includes(keyword) ||
      String(model.recommendedFor || "").toLowerCase().includes(keyword);
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
              <div>
                <h3 class="model-name">${model.modelName}</h3>
                <div class="provider">${model.provider}</div>
              </div>
            </div>
            <span class="chip ${model.status === "推荐" || model.status === "默认" ? "" : "blue"}">${model.status || "可用"}</span>
          </div>
          <p class="model-desc">${model.description || "当前账号可用模型。"}</p>
          <div class="tag-row">${(model.capabilities || ["通用"]).map((capability) => `<span class="chip blue">${capability}</span>`).join("")}</div>
          <div class="model-meta">
            <div class="meta-box"><span>上下文长度</span><strong>${model.contextWindow || "未标注"}</strong></div>
            <div class="meta-box"><span>推荐场景</span><strong>${model.recommendedFor || "按任务需求复制模型名称后使用"}</strong></div>
          </div>
          <button class="copy-btn" type="button" data-copy-model="${model.modelName}">
            <span class="app-icon">${icon("copy")}</span>
            复制模型名称
          </button>
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
  const isModels = view === "models";
  el("dashboardView").classList.toggle("hidden", isModels);
  el("modelsView").classList.toggle("hidden", !isModels);
  el("dashboardFilters").classList.toggle("hidden", isModels);
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  if (isModels) renderModels();
}

function render() {
  renderMetrics(usageData);
  renderTrend(usageData);
  renderSpendTrend(usageData);
  renderDonut(usageData);
  renderModelBars(usageData);
  renderSplit(usageData);
  renderTable(usageData);
  renderKeys();
}

async function loadDashboardData() {
  if (!currentUser || isLoading) return;
  isLoading = true;
  const { startDate, endDate } = selectedDateRange();
  const source = el("sourceSelect").value;
  try {
    const payload = await api(`/api/me/usage?start_date=${encodeURIComponent(startDate)}&end_date=${encodeURIComponent(endDate)}&source=${encodeURIComponent(source)}`);
    usageData = payload.rows || [];
    render();
  } catch (error) {
    showToast(error.message || "用量数据加载失败");
    usageData = [];
    render();
  } finally {
    isLoading = false;
  }
}

async function loadKeys() {
  try {
    const payload = await api("/api/me/keys");
    accessKeys = payload.keys || [];
    renderKeys();
  } catch (error) {
    accessKeys = [];
    renderKeys();
    showToast(error.message || "访问密钥加载失败");
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
  el("userEmail").textContent = user.email;
  el("userName").textContent = user.name;
  el("avatar").textContent = user.avatar || initials(user.email, user.name);
  el("welcomeTitle").textContent = `${user.name}您好，今天的 AI 工具消耗一眼看清`;
  switchView("dashboard");
  render();
  await Promise.all([loadDashboardData(), loadKeys(), loadModels()]);
}

function showLogin() {
  currentUser = null;
  el("appView").classList.add("hidden");
  el("loginView").classList.remove("hidden");
}

function maskKey(value) {
  if (!value) return "";
  return `${value.slice(0, 14)}...${value.slice(-4)}`;
}

function openKeyModal(keyId) {
  const decodedKeyId = decodeURIComponent(keyId);
  const key = accessKeys.find((item) => item.id === decodedKeyId);
  if (!key) return;
  currentKeyId = decodedKeyId;
  currentNewKey = "";
  el("modalTitle").textContent = `更新访问密钥：${key.name}`;
  el("modalSubtitle").textContent = "此操作会让旧密钥失效，需要同步更新本地工具配置。";
  el("confirmInput").value = "";
  el("regenerateButton").disabled = true;
  el("confirmArea").classList.remove("hidden");
  el("newKeyArea").classList.add("hidden");
  el("keyModal").classList.remove("hidden");
}
window.openKeyModal = openKeyModal;

function closeKeyModal() {
  if (currentNewKey && currentKeyId) {
    accessKeys = accessKeys.map((key) =>
      key.id === currentKeyId
        ? {
            ...key,
            masked: maskKey(currentNewKey),
            lastUsed: "刚刚更新",
            status: "正常",
          }
        : key,
    );
    renderKeys();
  }
  currentKeyId = null;
  currentNewKey = "";
  el("keyModal").classList.add("hidden");
}

async function regenerateKey() {
  if (!currentKeyId) return;
  el("regenerateButton").disabled = true;
  try {
    const payload = await api(`/api/me/keys/${encodeURIComponent(currentKeyId)}/regenerate`, { method: "POST", body: JSON.stringify({}) });
    currentNewKey = payload.key;
    el("newKeyValue").textContent = currentNewKey;
    el("confirmArea").classList.add("hidden");
    el("newKeyArea").classList.remove("hidden");
    showToast("新访问密钥已生成");
  } catch (error) {
    showToast(error.message || "访问密钥更新失败");
    el("regenerateButton").disabled = false;
  }
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

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view));
});
el("rangeSelect").addEventListener("change", loadDashboardData);
el("sourceSelect").addEventListener("change", loadDashboardData);
el("refreshButton").addEventListener("click", async () => {
  if (el("modelsView").classList.contains("hidden")) {
    await Promise.all([loadDashboardData(), loadKeys()]);
    showToast("已刷新真实用量数据");
  } else {
    await loadModels();
    showToast("已刷新模型列表");
  }
});
el("keyGrid").addEventListener("click", (event) => {
  const button = event.target.closest("[data-regenerate]");
  if (button) openKeyModal(button.dataset.regenerate);
});
el("modelSearch").addEventListener("input", renderModels);
el("providerFilter").addEventListener("change", renderModels);
el("capabilityFilter").addEventListener("change", renderModels);
el("modelGrid").addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy-model]");
  if (button) copyText(button.dataset.copyModel, "模型名称已复制");
});

el("confirmInput").addEventListener("input", (event) => {
  el("regenerateButton").disabled = event.target.value.trim() !== "确认更新";
});
el("cancelModalButton").addEventListener("click", () => el("keyModal").classList.add("hidden"));
el("regenerateButton").addEventListener("click", regenerateKey);
el("closeModalButton").addEventListener("click", closeKeyModal);
el("copyNewKeyButton").addEventListener("click", async () => {
  await copyText(currentNewKey, "新访问密钥已复制");
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
  render();
  try {
    const user = await api("/api/auth/me");
    await showApp(user);
  } catch {
    showLogin();
  }
}

init();
