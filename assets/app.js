const sourceColors = {
  Cursor: "#0673d2",
  "Claude Code": "#b88727",
  Her: "#d45d42",
  "其他": "#7a8ba0",
};

const sourceLabels = {
  Cursor: "Codex",
  Her: "Her",
};

let currentUser = null;
let currentView = "dashboard";
let usageData = [];
let usageSummary = null;
let usageTableFilters = { date: "all", model: "all", status: "all", keyword: "" };
let lastPersonalUsageCacheHit = false;
let lastAdminUsageCacheHit = false;
let lastDepartmentUsageCacheHit = false;
let lastTeamUsageCacheHit = false;
let personalDataFreshness = null;
let adminDataFreshness = null;
let departmentDataFreshness = null;
let teamDataFreshness = null;
let adminUsageData = [];
let adminSummaryData = [];
let adminEmployees = [];
let selectedAdminEmployee = "";
let departmentUsageData = [];
let departmentSummaryData = [];
let departmentRankings = [];
let departmentEmployees = [];
let teamUsageData = [];
let teamSummaryData = [];
let teamEmployees = [];
let teamInfo = null;
let teamMemberUsageData = [];
let teamMemberUsageSummary = null;
let selectedTeamEmployee = "";
let teamMemberUsageRequestId = 0;
let teamMemberUsageFilters = { date: "all", model: "all", status: "all", keyword: "" };
let leaderTeams = [];
let selectedTeamRef = "";
let selectedDepartment = "";
let departmentPickerOpen = false;
let departmentPickerOptions = [];
let modelCatalog = [];
let personalKeys = [];
let availableKeyModels = [];
let unrestrictedKeyModels = false;
let isKeysLoading = false;
let keyLoadError = "";
let pendingRegenerateKeyId = "";
let pendingDeleteKeyId = "";
let pendingDeleteKeyName = "";
let currentPlainKey = "";
let currentPlainKeyCleanup = null;
let revealedKeys = new Map();
let revealTimers = new Map();
let revealingKeyIds = new Set();
let disablingOldKeyIds = new Set();
let isCreatingKey = false;
let isRegeneratingKey = false;
let isDeletingKey = false;
let isDashboardLoading = false;
let isAdminLoading = false;
let isDepartmentLoading = false;
let isTeamLoading = false;
let isTeamMemberLoading = false;
let isSsoRedirecting = false;
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

function startSsoLogin() {
  if (isSsoRedirecting) return;
  if (!authConfig.oidcConfigured) {
    showToast("企业统一认证参数尚未配置");
    return;
  }
  isSsoRedirecting = true;
  const ssoButton = el("ssoButton");
  const devLoginButton = el("devLoginButton");
  ssoButton.disabled = true;
  devLoginButton.disabled = true;
  ssoButton.lastChild.textContent = "正在前往飞书登录";
  el("loginHint").textContent = "请在新打开的飞书认证页面完成扫码。";
  window.location.href = "/api/auth/sso/start";
}

function showLoginCallbackMessage() {
  const params = new URLSearchParams(window.location.search);
  const authError = params.get("auth_error");
  if (!authError) return;
  const message = authError === "state" ? "登录状态已失效，请重新点击飞书扫码登录。" : "登录没有完成，请重新扫码。";
  el("loginHint").textContent = message;
  showToast(message);
  params.delete("auth_error");
  const cleanQuery = params.toString();
  const cleanUrl = `${window.location.pathname}${cleanQuery ? `?${cleanQuery}` : ""}${window.location.hash}`;
  window.history.replaceState({}, "", cleanUrl);
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
      requestCount: sum(grouped[date], "requestCount"),
      successCount: sum(grouped[date], "successCount"),
      failureCount: sum(grouped[date], "failureCount"),
      spend: sum(grouped[date], "spend"),
    }));
}

function successRateText(requests, successes) {
  return requests ? `${Math.round((successes / requests) * 1000) / 10}%` : "0%";
}

function latestUsageDay(data, summary = null) {
  return summary?.latestDay || aggregateByDate(data).slice(-1)[0] || {};
}

function overviewContext(latestDate) {
  const dateText = latestDate || "暂无日期";
  return `${rangeLabel()} · ${sourceText()} · 最新数据日 ${dateText}`;
}

function freshnessText(freshness) {
  if (!freshness) return "数据更新时间：实时查询";
  if (!freshness.lastSyncedAt) return "数据更新时间：暂未同步";
  const parsed = new Date(freshness.lastSyncedAt);
  if (Number.isNaN(parsed.getTime())) return "数据更新时间：未知";
  const timeText = parsed.toLocaleString("zh-CN", { hour12: false });
  return `${freshness.stale ? "数据更新时间（待刷新）" : "数据更新时间"}：${timeText}`;
}

function selectedDateRangeText() {
  const { startDate, endDate, days } = selectedDateRange();
  const shortDate = (value) => value.slice(5).replace("-", "/");
  return days === 1 ? shortDate(endDate) : `${shortDate(startDate)} - ${shortDate(endDate)}`;
}

function setText(id, value) {
  const node = el(id);
  if (node) node.textContent = value;
}

function setDailyTokenValue(id, value) {
  const node = el(id);
  if (!node) return;

  const text = String(value);
  node.textContent = text;
  node.classList.toggle("is-compact", text.length >= 10 && text.length < 13);
  node.classList.toggle("is-extra-compact", text.length >= 13);
}

function setHtml(id, value) {
  const node = el(id);
  if (!node) return false;
  node.innerHTML = value;
  return true;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function displayModelName(model) {
  const rawName = String(model ?? "").trim();
  if (!rawName) return "未知模型";
  const separatorIndex = rawName.lastIndexOf("/");
  const shortName = separatorIndex >= 0 ? rawName.slice(separatorIndex + 1).trim() : rawName;
  return shortName || rawName;
}

function renderDailyOverview(config) {
  const {
    prefix,
    data,
    summary = null,
    title,
    totalLabel = `${rangeLabel()} Token`,
    sideLabel,
    sideValue,
    sideSub = "当前筛选范围",
    showShare = false,
    compactSingleDay = false,
  } = config;
  const latest = latestUsageDay(data, summary);
  const latestDate = latest.date || "";
  const rangeTokens = sum(data, "totalTokens");
  const rangeSpend = sum(data, "spend");
  const rangeRequests = sum(data, "requestCount");
  const rangeSuccesses = sum(data, "successCount");
  const baseId = prefix ? `${prefix}Hero` : "hero";
  const personalOverview = el("personalDailyOverview");
  const teamOverview = el("teamDailyOverview");

  if (showShare && personalOverview) {
    personalOverview.classList.toggle("personal-single-day", selectedDateRange().days === 1);
  }
  if (compactSingleDay && teamOverview) {
    teamOverview.classList.toggle("personal-single-day", selectedDateRange().days === 1);
  }

  setText(`${baseId}TotalLabel`, totalLabel);
  setDailyTokenValue(`${baseId}Total`, formatTokens(rangeTokens));
  setText(`${baseId}Spend`, money.format(rangeSpend));
  setText(`${baseId}Requests`, fmt.format(rangeRequests));
  setText(`${baseId}RequestsSub`, "所选范围累计");
  setText(`${baseId}Success`, successRateText(rangeRequests, rangeSuccesses));
  setText(`${baseId}SuccessSub`, `${fmt.format(rangeSuccesses)} / ${fmt.format(rangeRequests)} 次成功`);
  setText(`${baseId}Context`, overviewContext(latestDate));
  setText(`${baseId}Date`, selectedDateRangeText());

  if (prefix === "admin") setText("adminHeroTitle", title);
  if (prefix === "team" || prefix === "department") setText(`${prefix}WelcomeTitle`, title);

  if (showShare) {
    const days = selectedDateRange().days || 1;
    const dailyAvg = Math.round(rangeTokens / days);
    const dailyAvgSpend = rangeSpend / days;
    setText("heroShare", formatTokens(dailyAvg));
    setText("heroAvgSpend", money.format(dailyAvgSpend));
    setText("heroShareSub", "所选范围日均");
  } else {
    setText(`${prefix}ActiveUsers`, fmt.format(sideValue || 0));
    setText(`${prefix}ActiveUsersSub`, sideSub);
    if (sideLabel) setText(`${prefix}ActiveLabel`, sideLabel);
  }
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

function modelRankGroup(mode = "personal") {
  const prefix = mode === "personal" ? "" : mode;
  const titleId = prefix ? `${prefix}ModelTitle` : "";
  const descId = prefix ? `${prefix}ModelDesc` : "";
  const barsId = prefix ? `${prefix}ModelBars` : "modelBars";
  const defaultTitle = mode === "admin" ? "全员模型使用排行" : mode === "team" ? "团队模型使用排行" : mode === "department" ? "全部部门模型使用排行" : "模型使用排行";
  const defaultDesc = mode === "admin" ? "按全员总 Token 消耗排序。" : mode === "team" ? "按团队总 Token 消耗排序。" : mode === "department" ? "按全部部门总 Token 消耗排序。" : "按总 Token 消耗排序。";
  return `
    <section class="metric-group model-rank-group">
      <div class="metric-group-head">
        <div class="panel-heading">
          <span class="panel-icon">${icon("model")}</span>
          <div>
            <h3${titleId ? ` id="${titleId}"` : ""}>${defaultTitle}</h3>
            <p${descId ? ` id="${descId}"` : ""}>${defaultDesc}</p>
          </div>
        </div>
      </div>
      <div id="${barsId}" class="bars"></div>
    </section>
  `;
}

function sourceText() {
  const source = el("sourceSelect").value;
  return source === "all" ? "全部来源" : displaySource(source);
}

function scrollToDetailCard(id) {
  const card = el(id);
  if (!card || card.classList.contains("hidden") || getComputedStyle(card).display === "none") return;
  const behavior = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ? "auto" : "smooth";
  card.scrollIntoView({ behavior, block: "start", inline: "nearest" });
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
  const container = el(containerId);
  if (!container) return;
  const cursor = sum(splitData.filter((item) => item.source === "Cursor"), "totalTokens");
  const cc = sum(splitData.filter((item) => item.source === "Claude Code"), "totalTokens");
  const requests = sum(data, "requestCount");
  const successes = sum(data, "successCount");
  const successRate = requests ? Math.round((successes / requests) * 1000) / 10 : 0;
  const label = rangeLabel();
  const source = sourceText();
  const scopeSuffix = metricScopeSuffix(mode);

  container.innerHTML = [
    metricGroup("所选范围请求", `${label} · ${source}${scopeSuffix}`, [
      metric(`${label} 请求次数`, fmt.format(requests), "按当前筛选累计", "请求", "blue", "request"),
      metric(`${label} 请求成功率`, `${successRate}%`, `${fmt.format(successes)} / ${fmt.format(requests)} 次成功`, "稳定", "", "success"),
    ]),
    metricGroup("工具消耗拆分", `${label} · ${source}${scopeSuffix}`, [
      metric(`${label} Codex Token`, formatTokens(cursor), "Codex 相关消耗", "Codex", "", "cursor"),
      metric(`${label} Claude Code Token`, formatTokens(cc), "终端工具相关消耗", "Claude Code", "blue", "terminal"),
    ]),
    modelRankGroup(mode),
  ].join("");
}

function renderPersonalMetrics(data) {
  const label = rangeLabel();
  const source = sourceText();
  renderDailyOverview({
    prefix: "",
    data,
    summary: usageSummary,
    showShare: true,
  });
  el("trendBadge").textContent = `${label} · ${source}`;
  el("spendBadge").textContent = `${label} · ${source}`;
  renderMetricGroups("metrics", data, "personal", usageSummary);
  setText("heroFreshness", freshnessText(personalDataFreshness));
}

function selectedAdminEmployeeInfo() {
  if (!selectedAdminEmployee) return null;
  return adminEmployees.find((item) => item.employeeEmail === selectedAdminEmployee || item.employeeId === selectedAdminEmployee) || null;
}

function selectedAdminEmployeeLabel() {
  const employee = selectedAdminEmployeeInfo();
  return employee?.employeeName || employee?.employeeEmail || employee?.employeeId || selectedAdminEmployee || "员工";
}

function updateAdminChartTitles() {
  const scopeName = selectedAdminEmployee ? selectedAdminEmployeeLabel() : "全员";
  setText("adminTrendTitle", `${scopeName}每日 Token 趋势`);
  setText("adminTrendDesc", `按日期汇总${scopeName} Prompt 与 Completion Token。`);
  setText("adminSpendTitle", `${scopeName}每日金额消费趋势`);
  setText("adminSpendDesc", `按日期汇总${scopeName}预估消费金额。`);
  setText("adminSourceTitle", `${scopeName}用量占比`);
  setText("adminSourceDesc", selectedAdminEmployee
    ? `按${scopeName} Codex、Claude Code 与其他来源拆分用量。`
    : "按 Codex、Claude Code 与其他来源拆分用量。");
}

function renderAdminMetrics(data) {
  const totalData = adminSummaryData.length ? adminSummaryData : data;
  const label = rangeLabel();
  const source = sourceText();
  renderDailyOverview({
    prefix: "admin",
    data: totalData,
    title: "所选范围 · 管理员视图",
    totalLabel: "所选范围全员 Token",
    sideLabel: "活跃员工",
    sideValue: adminEmployees.length,
    sideSub: "当前筛选范围",
  });
  el("adminAvgSpendWrap")?.classList.add("hidden");
  el("adminDailyOverview")?.classList.remove("personal-single-day");
  el("adminTrendBadge").textContent = `${label} · ${source}`;
  el("adminSpendBadge").textContent = `${label} · ${source}`;
  updateAdminChartTitles();
  renderMetricGroups("adminMetrics", totalData, "admin", null, data);
  setText("adminHeroFreshness", freshnessText(adminDataFreshness));
}

function renderAdminMemberMetrics(data) {
  const label = rangeLabel();
  const source = sourceText();
  const employee = selectedAdminEmployeeInfo();
  const { days } = selectedDateRange();
  const isSingleDay = days === 1;
  const dailyAvgSpend = days ? sum(data, "spend") / days : 0;
  renderDailyOverview({
    prefix: "admin",
    data,
    title: "所选范围 · 员工视图",
    totalLabel: "所选范围员工 Token",
    sideLabel: "当前员工",
    sideValue: 1,
    sideSub: employee?.employeeEmail || employee?.employeeId || selectedAdminEmployee,
  });
  el("adminAvgSpendWrap")?.classList.toggle("hidden", isSingleDay);
  el("adminDailyOverview")?.classList.toggle("personal-single-day", isSingleDay);
  setText("adminActiveLabel", "日均 Token");
  setText("adminActiveUsers", formatTokens(Math.round(sum(data, "totalTokens") / (days || 1))));
  setText("adminActiveUsersSub", "所选范围日均");
  setText("adminAvgSpend", money.format(dailyAvgSpend));
  el("adminTrendBadge").textContent = `${label} · ${source}`;
  el("adminSpendBadge").textContent = `${label} · ${source}`;
  updateAdminChartTitles();
  renderMetricGroups("adminMetrics", data, "admin", null, data);
  setText("adminHeroFreshness", freshnessText(adminDataFreshness));
}

function renderDepartmentMetrics(data) {
  const label = rangeLabel();
  const source = sourceText();
  const scopeLabel = departmentScopeLabel();
  renderDailyOverview({
    prefix: "department",
    data,
    title: `所选范围 · ${scopeLabel}`,
    totalLabel: "所选范围 Token",
    sideLabel: selectedDepartment ? "活跃员工" : "活跃部门",
    sideValue: selectedDepartment ? departmentEmployees.length : departmentRankings.length,
    sideSub: selectedDepartment ? "当前部门" : "当前筛选范围",
  });
  el("departmentTrendBadge").textContent = `${label} · ${source}`;
  el("departmentSpendBadge").textContent = `${label} · ${source}`;
  el("departmentTrendTitle").textContent = `${scopeLabel}每日 Token 趋势`;
  el("departmentTrendDesc").textContent = `按日期汇总${scopeLabel} Prompt 与 Completion Token。`;
  el("departmentSpendTitle").textContent = `${scopeLabel}每日金额消费趋势`;
  el("departmentSpendDesc").textContent = `按日期汇总${scopeLabel}预估消费金额。`;
  el("departmentSourceTitle").textContent = `${scopeLabel}用量占比`;
  el("departmentSourceDesc").textContent = `按${scopeLabel} Codex、Claude Code 与其他来源拆分用量。`;
  renderMetricGroups("departmentMetrics", data, "department");
  setText("departmentHeroFreshness", freshnessText(departmentDataFreshness));
  setText("departmentModelTitle", `${scopeLabel}模型使用排行`);
  setText("departmentModelDesc", `按${scopeLabel}总 Token 消耗排序。`);
}

function setDepartmentOverviewVisible(visible) {
  [
    "departmentOverviewHero",
    "departmentMetrics",
    "departmentTrendGrid",
  ].forEach((id) => el(id)?.classList.toggle("hidden", !visible));
}

function teamScopeLabel() {
  const selected = leaderTeams.find((item) => item.teamRef === selectedTeamRef);
  return teamInfo?.name || selected?.name || currentUser?.team?.name || "团队";
}

function normalizeLeaderTeams(user) {
  const teams = Array.isArray(user?.leaderTeams) ? user.leaderTeams : [];
  if (teams.length) return teams.filter((item) => item?.teamRef);
  return user?.team?.teamRef ? [user.team] : [];
}

function ensureSelectedTeamRef() {
  if (!selectedTeamRef || !leaderTeams.some((item) => item.teamRef === selectedTeamRef)) {
    selectedTeamRef = currentUser?.team?.teamRef || leaderTeams[0]?.teamRef || "";
  }
  teamInfo = leaderTeams.find((item) => item.teamRef === selectedTeamRef) || currentUser?.team || null;
}

function renderTeamSelector() {
  const selector = el("teamSelector");
  if (!selector) return;
  ensureSelectedTeamRef();
  selector.classList.toggle("hidden", leaderTeams.length <= 1);
  const select = el("teamSelect");
  select.innerHTML = leaderTeams
    .map((team) => `<option value="${team.teamRef}">${team.name || team.id || "团队"} · ${fmt.format(team.memberCount || 0)} 人</option>`)
    .join("");
  select.value = selectedTeamRef;
}

function renderTeamMetrics(data) {
  const label = rangeLabel();
  const source = sourceText();
  const scopeLabel = teamScopeLabel();
  const activeMembers = teamEmployees.filter((item) => Number(item.totalTokens || 0) > 0 || Number(item.requestCount || 0) > 0).length;
  const days = selectedDateRange().days || 1;
  renderDailyOverview({
    prefix: "team",
    data,
    title: `所选范围 · ${scopeLabel}`,
    totalLabel: "所选范围 Token",
    sideValue: activeMembers,
    sideSub: "当前筛选范围",
    compactSingleDay: false,
  });
  el("teamDailyOverview")?.classList.remove("personal-single-day");
  el("teamAvgSpendWrap")?.classList.add("hidden");
  setText("teamActiveLabel", "活跃成员");
  setText("teamActiveUsers", fmt.format(activeMembers));
  setText("teamActiveUsersSub", "当前筛选范围");
  setText("teamAvgSpend", money.format(sum(data, "spend") / days));
  setText("teamHeroDateSub", "当前筛选下最新日期");
  el("teamTrendBadge").textContent = `${label} · ${source}`;
  el("teamSpendBadge").textContent = `${label} · ${source}`;
  el("teamTrendTitle").textContent = `${scopeLabel}每日 Token 趋势`;
  el("teamTrendDesc").textContent = `按日期汇总${scopeLabel} Prompt 与 Completion Token。`;
  el("teamSpendTitle").textContent = `${scopeLabel}每日金额消费趋势`;
  el("teamSpendDesc").textContent = `按日期汇总${scopeLabel}预估消费金额。`;
  el("teamSourceTitle").textContent = `${scopeLabel}用量占比`;
  el("teamSourceDesc").textContent = `按${scopeLabel} Codex、Claude Code 与其他来源拆分用量。`;
  renderMetricGroups("teamMetrics", data, "team");
  setText("teamHeroFreshness", freshnessText(teamDataFreshness));
  setText("teamModelTitle", `${scopeLabel}模型使用排行`);
  setText("teamModelDesc", `按${scopeLabel}总 Token 消耗排序。`);
}

function selectedTeamEmployeeInfo() {
  if (!selectedTeamEmployee) return null;
  return teamEmployees.find((item) => item.employeeEmail === selectedTeamEmployee || item.employeeId === selectedTeamEmployee) || null;
}

function selectedTeamEmployeeLabel() {
  const employee = selectedTeamEmployeeInfo();
  return employee?.employeeName || employee?.employeeEmail || employee?.employeeId || selectedTeamEmployee || "团队成员";
}

function updateTeamMemberLoadingLabels() {
  const employee = selectedTeamEmployeeInfo();
  const employeeName = selectedTeamEmployeeLabel();
  setText("teamDetailTitle", `${employeeName} 的用量详情`);
  setText("teamDetailSubtitle", employee?.employeeEmail || employee?.employeeId || selectedTeamEmployee || "");
  el("teamTrendTitle").textContent = `${employeeName}每日 Token 趋势`;
  el("teamTrendDesc").textContent = `按日期汇总${employeeName} Prompt 与 Completion Token。`;
  el("teamSpendTitle").textContent = `${employeeName}每日金额消费趋势`;
  el("teamSpendDesc").textContent = `按日期汇总${employeeName}预估消费金额。`;
  el("teamSourceTitle").textContent = `${employeeName}用量占比`;
  el("teamSourceDesc").textContent = `按${employeeName} Codex、Claude Code 与其他来源拆分用量。`;
  setText("teamModelTitle", `${employeeName}模型使用排行`);
  setText("teamModelDesc", `按${employeeName}总 Token 消耗排序。`);
}

function renderTeamMemberMetrics(data) {
  const label = rangeLabel();
  const source = sourceText();
  const employee = selectedTeamEmployeeInfo();
  const employeeName = selectedTeamEmployeeLabel();
  const { days, endDate } = selectedDateRange();
  const isSingleDay = days === 1;
  const dailyAvgSpend = days ? sum(data, "spend") / days : 0;
  renderDailyOverview({
    prefix: "team",
    data,
    summary: teamMemberUsageSummary,
    title: `所选范围 · 成员视图`,
    totalLabel: "所选成员 Token",
    sideLabel: "当前成员",
    sideValue: 1,
    sideSub: employee?.employeeEmail || employee?.employeeId || selectedTeamEmployee,
    compactSingleDay: true,
  });
  el("teamDailyOverview")?.classList.toggle("personal-single-day", isSingleDay);
  el("teamAvgSpendWrap")?.classList.toggle("hidden", isSingleDay);
  setText("teamActiveLabel", "日均 Token");
  setText("teamActiveUsers", formatTokens(Math.round(sum(data, "totalTokens") / (days || 1))));
  setText("teamActiveUsersSub", "所选范围日均");
  setText("teamAvgSpend", money.format(dailyAvgSpend));
  setText("teamHeroDate", isSingleDay ? endDate.slice(5).replace("-", "/") : selectedDateRangeText());
  setText("teamHeroDateSub", "当前筛选下最新日期");
  el("teamTrendBadge").textContent = `${label} · ${source}`;
  el("teamSpendBadge").textContent = `${label} · ${source}`;
  el("teamTrendTitle").textContent = `${employeeName}每日 Token 趋势`;
  el("teamTrendDesc").textContent = `按日期汇总${employeeName} Prompt 与 Completion Token。`;
  el("teamSpendTitle").textContent = `${employeeName}每日金额消费趋势`;
  el("teamSpendDesc").textContent = `按日期汇总${employeeName}预估消费金额。`;
  el("teamSourceTitle").textContent = `${employeeName}用量占比`;
  el("teamSourceDesc").textContent = `按${employeeName} Codex、Claude Code 与其他来源拆分用量。`;
  renderMetricGroups("teamMetrics", data, "team", teamMemberUsageSummary);
  setText("teamHeroFreshness", freshnessText(teamDataFreshness));
  setText("teamModelTitle", `${employeeName}模型使用排行`);
  setText("teamModelDesc", `按${employeeName}总 Token 消耗排序。`);
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
  if (!svg) return;
  svg.setAttribute("viewBox", "0 0 900 280");
  svg.innerHTML = `<rect width="900" height="280" rx="8" fill="#fbfdff"/><text x="450" y="140" fill="#66748a" font-size="16" text-anchor="middle">${label}</text>`;
}

function renderLineChart({ svg, points, valueField, color, fill, axisFormatter, tooltipRows }) {
  if (!svg) return;
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
      return `<line x1="${pad.left}" y1="${yy}" x2="${width - pad.right}" y2="${yy}" stroke="#dde5ee" stroke-dasharray="4 7"/><text x="12" y="${yy + 4}" fill="#66748a" font-size="12">${axisFormatter(max * ratio)}</text>`;
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
      return `<text x="${x(originalIndex)}" y="${height - 16}" fill="#66748a" font-size="12" text-anchor="${index === arr.length - 1 ? "end" : "middle"}">${p.date.slice(5)}</text>`;
    })
    .join("");

  svg.innerHTML = `<rect width="${width}" height="${height}" rx="8" fill="#fbfdff"/>${grid}<path d="${area}" fill="${fill}"/><path d="${path}" fill="none" stroke="${color}" stroke-width="4"/>${dots}${labels}`;
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
    color: "#0673d2",
    fill: "rgba(6,115,210,.13)",
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
  const svg = el(svgId);
  const totalNode = el(totalId);
  const legend = el(legendId);
  if (!svg || !totalNode || !legend) return;
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
  svg.innerHTML = `<circle cx="90" cy="90" r="${radius}" fill="none" stroke="#e8eef5" stroke-width="18"/>${circles}`;
  totalNode.textContent = formatTokens(total);
  legend.innerHTML = totals
    .map((item) => {
      const pct = total ? Math.round((item.value / total) * 100) : 0;
      return `<div class="legend-item"><span><i class="dot" style="background:${sourceColors[item.source]}"></i>${displaySource(item.source)}</span><strong>${pct}%</strong></div>`;
    })
    .join("");
}

function renderModelBarsTo(containerId, data) {
  const container = el(containerId);
  if (!container) return;
  const grouped = groupBy(data, "model");
  const rows = Object.keys(grouped)
    .map((model) => ({ model, value: sum(grouped[model], "totalTokens") }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 5);
  const max = Math.max(1, ...rows.map((row) => row.value));
  container.innerHTML = rows.length
    ? rows
        .map((row) => `<div class="bar-row"><strong>${escapeHtml(displayModelName(row.model))}</strong><div class="bar-track"><div class="bar-fill" style="width:${Math.max(3, (row.value / max) * 100)}%"></div></div><span class="num">${formatTokens(row.value)}</span></div>`)
        .join("")
    : `<div class="model-empty">当前筛选范围暂无模型用量</div>`;
}

function uniqueSorted(data, field) {
  return Array.from(new Set(data.map((item) => String(item[field] || "").trim()).filter(Boolean))).sort((a, b) => a.localeCompare(b, "zh-CN"));
}

function optionMarkup(value, label) {
  return `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`;
}

function setupUsageTableFilters(data) {
  const dateSelect = el("usageDetailDateFilter");
  const modelSelect = el("usageDetailModelFilter");
  if (!dateSelect || !modelSelect) return;

  const dates = uniqueSorted(data, "date").reverse();
  const models = uniqueSorted(data, "model");
  if (usageTableFilters.date !== "all" && !dates.includes(usageTableFilters.date)) usageTableFilters.date = "all";
  if (usageTableFilters.model !== "all" && !models.includes(usageTableFilters.model)) usageTableFilters.model = "all";

  dateSelect.innerHTML = optionMarkup("all", "全部日期") + dates.map((date) => optionMarkup(date, date)).join("");
  modelSelect.innerHTML = optionMarkup("all", "全部模型") + models.map((model) => optionMarkup(model, model)).join("");
  dateSelect.value = usageTableFilters.date;
  modelSelect.value = usageTableFilters.model;
  const statusSelect = el("usageDetailStatusFilter");
  const searchInput = el("usageDetailSearch");
  if (statusSelect) statusSelect.value = usageTableFilters.status;
  if (searchInput) searchInput.value = usageTableFilters.keyword;
}

function setupTeamMemberUsageFilters(data) {
  const dateSelect = el("teamMemberUsageDetailDateFilter");
  const modelSelect = el("teamMemberUsageDetailModelFilter");
  if (!dateSelect || !modelSelect) return;

  const dates = uniqueSorted(data, "date").reverse();
  const models = uniqueSorted(data, "model");
  if (teamMemberUsageFilters.date !== "all" && !dates.includes(teamMemberUsageFilters.date)) teamMemberUsageFilters.date = "all";
  if (teamMemberUsageFilters.model !== "all" && !models.includes(teamMemberUsageFilters.model)) teamMemberUsageFilters.model = "all";

  dateSelect.innerHTML = optionMarkup("all", "全部日期") + dates.map((date) => optionMarkup(date, date)).join("");
  modelSelect.innerHTML = optionMarkup("all", "全部模型") + models.map((model) => optionMarkup(model, model)).join("");
  dateSelect.value = teamMemberUsageFilters.date;
  modelSelect.value = teamMemberUsageFilters.model;
  const statusSelect = el("teamMemberUsageDetailStatusFilter");
  const searchInput = el("teamMemberUsageDetailSearch");
  if (statusSelect) {
    statusSelect.innerHTML = optionMarkup("all", "全部状态") + optionMarkup("正常", "正常") + optionMarkup("有失败", "有失败");
    statusSelect.value = teamMemberUsageFilters.status;
  }
  if (searchInput) searchInput.value = teamMemberUsageFilters.keyword;
}

function filteredUsageRows(data = usageData) {
  const keyword = usageTableFilters.keyword.trim().toLowerCase();
  return data.filter((item) => {
    const hasFailure = Number(item.failureCount || 0) > 0;
    const displayStatus = hasFailure ? "有失败" : "正常";
    const matchesDate = usageTableFilters.date === "all" || item.date === usageTableFilters.date;
    const matchesModel = usageTableFilters.model === "all" || item.model === usageTableFilters.model;
    const matchesStatus = usageTableFilters.status === "all" || usageTableFilters.status === displayStatus;
    const text = `${item.model || ""} ${displaySource(item.source)}`.toLowerCase();
    return matchesDate && matchesModel && matchesStatus && (!keyword || text.includes(keyword));
  });
}

function filteredTeamMemberUsageRows() {
  const keyword = teamMemberUsageFilters.keyword.trim().toLowerCase();
  return teamMemberUsageData.filter((item) => {
    const hasFailure = Number(item.failureCount || 0) > 0;
    const displayStatus = hasFailure ? "有失败" : "正常";
    const matchesDate = teamMemberUsageFilters.date === "all" || item.date === teamMemberUsageFilters.date;
    const matchesModel = teamMemberUsageFilters.model === "all" || item.model === teamMemberUsageFilters.model;
    const matchesStatus = teamMemberUsageFilters.status === "all" || teamMemberUsageFilters.status === displayStatus;
    const text = `${item.model || ""} ${displaySource(item.source)}`.toLowerCase();
    return matchesDate && matchesModel && matchesStatus && (!keyword || text.includes(keyword));
  });
}

function updateTeamMemberUsageFilters() {
  teamMemberUsageFilters = {
    date: el("teamMemberUsageDetailDateFilter").value,
    model: el("teamMemberUsageDetailModelFilter").value,
    status: el("teamMemberUsageDetailStatusFilter").value,
    keyword: el("teamMemberUsageDetailSearch").value.trim(),
  };
  renderTeam();
}

function resetTeamMemberUsageFilters() {
  teamMemberUsageFilters = { date: "all", model: "all", status: "all", keyword: "" };
  setupTeamMemberUsageFilters(teamMemberUsageData);
  renderTeam();
}

function updateUsageTableFilters() {
  usageTableFilters = {
    date: el("usageDetailDateFilter").value,
    model: el("usageDetailModelFilter").value,
    status: el("usageDetailStatusFilter").value,
    keyword: el("usageDetailSearch").value.trim(),
  };
  renderPersonal();
}

function resetUsageTableFilters() {
  usageTableFilters = { date: "all", model: "all", status: "all", keyword: "" };
  setupUsageTableFilters(usageData);
  renderPersonal();
}

function renderTable(data, tableId = "usageTable", countId = "tableCount") {
  setText(countId, `${data.length} 条`);
  setHtml(
    tableId,
    data.length
    ? data
        .slice()
        .reverse()
        .map((item) => {
          const status = item.failureCount > 0 ? `<span class="chip rose">${item.failureCount} 次失败</span>` : `<span class="chip">正常</span>`;
          return `<tr><td>${escapeHtml(item.date)}</td><td>${escapeHtml(displaySource(item.source))}</td><td>${escapeHtml(item.model)}</td><td class="num">${fmt.format(item.requestCount || 0)}</td><td class="num">${fmt.format(item.promptTokens || 0)}</td><td class="num">${fmt.format(item.completionTokens || 0)}</td><td class="num"><strong>${fmt.format(item.totalTokens || 0)}</strong></td><td>${status}</td></tr>`;
        })
        .join("")
    : `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:26px">当前明细筛选条件下暂无用量记录</td></tr>`,
  );
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
  const loading = loadDepartmentData();
  scrollToDetailCard("departmentDetailCard");
  await loading;
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
            <tr class="admin-employee-row ${tableId === "teamUserTable" && selectedTeamEmployee === (item.employeeEmail || item.employeeId) ? "active" : ""}" data-employee="${escapeHtml(item.employeeEmail || item.employeeId)}">
              <td><strong>${item.employeeName || item.employeeId}</strong></td>
              <td>${item.employeeEmail || "未绑定邮箱"}</td>
              <td>${tableId === "teamUserTable" ? (item.teamRole === "admin" ? "负责人" : "成员") : displaySource(item.primarySource)}</td>
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
    el("departmentRankingDesc").textContent = "点击部门查看该部门用量看板和员工排行。";
    renderDepartmentRanking("departmentUserTable", "departmentUserCount", departmentRankings, "当前筛选范围暂无部门用量");
  }
}

function renderTeamUsers() {
  renderEmployeeRanking("teamUserTable", "teamUserCount", teamEmployees, "当前团队暂无成员用量");
  renderTeamMemberTable();
}

function renderTeamMemberTable() {
  const table = el("teamMemberUsageTable");
  const count = el("teamMemberTableCount");
  const detailCard = el("teamMemberDetailCard");
  if (!table || !count || !detailCard) return;
  const visible = Boolean(selectedTeamEmployee);
  detailCard.classList.toggle("hidden", !visible);
  if (!visible) {
    table.innerHTML = "";
    count.textContent = "0 条";
    return;
  }
  setupTeamMemberUsageFilters(teamMemberUsageData);
  const rows = filteredTeamMemberUsageRows();
  setText("teamMemberTableCount", `${rows.length} 条`);
  setHtml(
    "teamMemberUsageTable",
    rows.length
      ? rows
          .slice()
          .reverse()
          .map((item) => {
            const status = item.failureCount > 0 ? `<span class="chip rose">${item.failureCount} 次失败</span>` : `<span class="chip">正常</span>`;
            return `<tr><td>${escapeHtml(item.date)}</td><td>${escapeHtml(displaySource(item.source))}</td><td>${escapeHtml(item.model)}</td><td class="num">${fmt.format(item.requestCount || 0)}</td><td class="num">${fmt.format(item.promptTokens || 0)}</td><td class="num">${fmt.format(item.completionTokens || 0)}</td><td class="num"><strong>${fmt.format(item.totalTokens || 0)}</strong></td><td>${status}</td></tr>`;
          })
          .join("")
      : `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:26px">当前成员在所选范围内暂无用量记录</td></tr>`,
  );
}

function resetTeamMemberSelection() {
  selectedTeamEmployee = "";
  teamMemberUsageRequestId += 1;
  teamMemberUsageData = [];
  teamMemberUsageSummary = null;
  teamMemberUsageFilters = { date: "all", model: "all", status: "all", keyword: "" };
  el("teamDailyOverview")?.classList.remove("personal-single-day");
  el("teamAvgSpendWrap")?.classList.add("hidden");
}

function loadingLine(width = "100%") {
  return `<div class="loading-line" style="width:${width}"></div>`;
}

function renderMetricSkeleton(containerId) {
  setHtml(
    containerId,
    Array.from({ length: 3 })
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
    .join(""),
  );
}

function renderChartSkeleton(svgId) {
  const svg = el(svgId);
  if (!svg) return;
  svg.setAttribute("viewBox", "0 0 900 280");
  svg.innerHTML = `
    <rect width="900" height="280" rx="8" fill="#fbfdff"/>
    <text x="450" y="126" fill="#66748a" font-size="16" font-weight="800" text-anchor="middle">数据加载中</text>
    <text x="450" y="154" fill="#8894a5" font-size="13" text-anchor="middle">正在从后端汇总当前筛选范围</text>
    <rect x="64" y="196" width="772" height="14" rx="7" fill="#e3e9f1"/>
    <rect x="64" y="224" width="512" height="10" rx="5" fill="#eaeff6"/>
  `;
}

function renderDonutSkeleton(totalId, legendId) {
  setText(totalId, "--");
  setHtml(
    legendId,
    `
    <div class="loading-status"><span class="loading-pill"></span><span>数据加载中</span></div>
    <div style="margin-top:18px">${loadingLine("86%")}</div>
    <div style="margin-top:14px">${loadingLine("72%")}</div>
    <div style="margin-top:14px">${loadingLine("64%")}</div>
  `,
  );
}

function renderBarsSkeleton(containerId) {
  setHtml(
    containerId,
    Array.from({ length: 5 })
    .map(
      (_, index) => `
        <div class="bar-row">
          <strong><span class="loading-line" style="display:block;width:${70 - index * 6}px"></span></strong>
          <div class="bar-track"><div class="bar-fill" style="width:${78 - index * 10}%;background:#dee5ee"></div></div>
          <span class="num">--</span>
        </div>
      `,
    )
    .join(""),
  );
}

function renderTableSkeleton(tableId, countId, colSpan, label = "数据加载中") {
  if (countId) setText(countId, label);
  setHtml(
    tableId,
    Array.from({ length: 5 })
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
    .join(""),
  );
}

function renderPersonalLoading() {
  const label = rangeLabel();
  const source = sourceText();
  el("personalDailyOverview")?.classList.toggle("personal-single-day", selectedDateRange().days === 1);
  setDailyTokenValue("heroTotal", "加载中");
  setText("heroSpend", "--");
  setText("heroSuccess", "--");
  setText("heroSuccessSub", "-- / -- 次成功");
  setText("heroRequests", "--");
  setText("heroRequestsSub", "数据加载中");
  setText("heroShare", "--");
  setText("heroAvgSpend", "--");
  setText("heroShareSub", "所选范围日均");
  setText("heroDate", "加载中");
  setText("heroContext", `${label} · ${source} · 数据加载中`);
  setText("heroTotalLabel", `${label} Token`);
  setText("trendBadge", `${label} · ${source}`);
  setText("spendBadge", `${label} · ${source}`);
  renderMetricSkeleton("metrics");
  renderChartSkeleton("trendChart");
  renderChartSkeleton("spendChart");
  renderDonutSkeleton("donutTotal", "sourceLegend");
  renderBarsSkeleton("modelBars");
  renderTableSkeleton("usageTable", "tableCount", 8);
}

function renderAdminLoading() {
  const label = rangeLabel();
  const source = sourceText();
  setDailyTokenValue("adminHeroTotal", "加载中");
  setText("adminHeroSpend", "--");
  setText("adminHeroTotalLabel", selectedAdminEmployee ? "所选范围员工 Token" : "所选范围全员 Token");
  setText("adminHeroTitle", selectedAdminEmployee ? "所选范围 · 员工视图" : "所选范围 · 管理员视图");
  setText("adminHeroRequests", "--");
  setText("adminHeroRequestsSub", "数据加载中");
  setText("adminHeroSuccess", "--");
  setText("adminHeroSuccessSub", "-- / -- 次成功");
  setText("adminHeroDate", "加载中");
  setText("adminHeroContext", `${label} · ${source} · 数据加载中`);
  setText("adminActiveUsers", "--");
  setText("adminActiveLabel", selectedAdminEmployee ? "日均 Token" : "活跃员工");
  setText("adminActiveUsersSub", selectedAdminEmployee ? "所选范围日均" : "当前筛选范围");
  el("adminAvgSpendWrap")?.classList.add("hidden");
  updateAdminChartTitles();
  setText("adminTrendBadge", `${label} · ${source}`);
  setText("adminSpendBadge", `${label} · ${source}`);
  setText("adminLimitHint", "数据加载中");
  renderMetricSkeleton("adminMetrics");
  renderChartSkeleton("adminTrendChart");
  renderChartSkeleton("adminSpendChart");
  renderDonutSkeleton("adminDonutTotal", "adminSourceLegend");
  renderBarsSkeleton("adminModelBars");
  renderTableSkeleton("adminUserTable", "adminUserCount", 8);
  renderAdminDetailCard();
}

function renderDepartmentLoading() {
  setDepartmentOverviewVisible(Boolean(selectedDepartment));
  const label = rangeLabel();
  const source = sourceText();
  const scopeLabel = departmentScopeLabel();
  el("departmentBackButton").classList.toggle("hidden", !selectedDepartment);
  setText("departmentRankingTitle", selectedDepartment ? `${scopeLabel}员工排行` : "部门用量排行");
  setText("departmentRankingDesc", selectedDepartment
    ? `当前展示 ${scopeLabel} 内员工用量，默认按 Token 从高到低排序。`
    : "点击部门查看该部门用量看板和员工排行。");
  setDailyTokenValue("departmentHeroTotal", "加载中");
  setText("departmentHeroSpend", "--");
  setText("departmentHeroTotalLabel", "所选范围 Token");
  setText("departmentWelcomeTitle", `所选范围 · ${scopeLabel}`);
  setText("departmentHeroRequests", "--");
  setText("departmentHeroRequestsSub", "数据加载中");
  setText("departmentHeroSuccess", "--");
  setText("departmentHeroSuccessSub", "-- / -- 次成功");
  setText("departmentHeroDate", "加载中");
  setText("departmentHeroContext", `${label} · ${source} · 数据加载中`);
  setText("departmentActiveUsers", "--");
  setText("departmentActiveLabel", selectedDepartment ? "活跃员工" : "活跃部门");
  setText("departmentActiveUsersSub", selectedDepartment ? "当前部门" : "当前筛选范围");
  setText("departmentTrendBadge", `${label} · ${source}`);
  setText("departmentSpendBadge", `${label} · ${source}`);
  setText("departmentLimitHint", "数据加载中");
  renderDepartmentDetailCard();
  renderMetricSkeleton("departmentMetrics");
  renderChartSkeleton("departmentTrendChart");
  renderChartSkeleton("departmentSpendChart");
  renderDonutSkeleton("departmentDonutTotal", "departmentSourceLegend");
  renderBarsSkeleton("departmentModelBars");
  renderTableSkeleton("departmentUserTable", "departmentUserCount", 8);
}

function renderTeamLoading() {
  const label = rangeLabel();
  const source = sourceText();
  const scopeLabel = teamScopeLabel();
  const memberLabel = selectedTeamEmployee ? selectedTeamEmployeeLabel() : "";
  setDailyTokenValue("teamHeroTotal", "加载中");
  setText("teamHeroSpend", "--");
  setText("teamHeroTotalLabel", selectedTeamEmployee ? "所选成员 Token" : "所选范围 Token");
  setText("teamHeroRequests", "--");
  setText("teamHeroRequestsSub", "数据加载中");
  setText("teamHeroSuccess", "--");
  setText("teamHeroSuccessSub", "-- / -- 次成功");
  setText("teamHeroDate", "加载中");
  setText("teamHeroContext", `${label} · ${source} · 数据加载中`);
  setText("teamActiveUsers", "--");
  setText("teamActiveUsersSub", selectedTeamEmployee ? (memberLabel || "当前成员") : "当前筛选范围");
  setText("teamWelcomeTitle", selectedTeamEmployee ? "所选范围 · 成员视图" : `所选范围 · ${scopeLabel}`);
  setText("teamTrendBadge", `${label} · ${source}`);
  setText("teamSpendBadge", `${label} · ${source}`);
  setText("teamLimitHint", "数据加载中");
  renderTeamDetailCard();
  if (selectedTeamEmployee) {
    const isSingleDay = selectedDateRange().days === 1;
    el("teamDailyOverview")?.classList.toggle("personal-single-day", isSingleDay);
    el("teamAvgSpendWrap")?.classList.toggle("hidden", isSingleDay);
    setText("teamActiveLabel", "日均 Token");
    setText("teamHeroDateSub", "当前筛选下最新日期");
    updateTeamMemberLoadingLabels();
  } else {
    el("teamDailyOverview")?.classList.remove("personal-single-day");
    el("teamAvgSpendWrap")?.classList.add("hidden");
    setText("teamActiveLabel", "活跃成员");
    setText("teamHeroDateSub", "当前筛选下最新日期");
  }
  renderMetricSkeleton("teamMetrics");
  renderChartSkeleton("teamTrendChart");
  renderChartSkeleton("teamSpendChart");
  renderDonutSkeleton("teamDonutTotal", "teamSourceLegend");
  renderBarsSkeleton("teamModelBars");
  renderTableSkeleton(selectedTeamEmployee ? "teamMemberUsageTable" : "teamUserTable", selectedTeamEmployee ? "teamMemberTableCount" : "teamUserCount", 8);
}

function renderPersonal() {
  if (isDashboardLoading) {
    renderPersonalLoading();
    return;
  }
  setupUsageTableFilters(usageData);
  renderPersonalMetrics(usageData);
  renderTrendTo("trendChart", usageData);
  renderSpendTrendTo("spendChart", usageData);
  renderDonutTo("sourceDonut", "donutTotal", "sourceLegend", usageData);
  renderModelBarsTo("modelBars", usageData);
  renderTable(filteredUsageRows());
}

function renderAdmin() {
  if (isAdminLoading) {
    renderAdminLoading();
    return;
  }
  if (selectedAdminEmployee) {
    renderAdminMemberMetrics(adminUsageData);
    renderTrendTo("adminTrendChart", adminUsageData);
    renderSpendTrendTo("adminSpendChart", adminUsageData);
    renderDonutTo("adminSourceDonut", "adminDonutTotal", "adminSourceLegend", adminUsageData);
    renderModelBarsTo("adminModelBars", adminUsageData);
    renderAdminUsers();
    renderAdminDetailCard();
    return;
  }
  const totalData = adminSummaryData.length ? adminSummaryData : adminUsageData;
  renderAdminMetrics(adminUsageData);
  renderTrendTo("adminTrendChart", totalData);
  renderSpendTrendTo("adminSpendChart", totalData);
  renderDonutTo("adminSourceDonut", "adminDonutTotal", "adminSourceLegend", adminUsageData);
  renderModelBarsTo("adminModelBars", adminUsageData);
  renderAdminUsers();

  renderAdminDetailCard();
}

function renderAdminDetailCard() {
  const detailCard = el("adminDetailCard");
  if (!detailCard) return;
  detailCard.classList.toggle("show", Boolean(selectedAdminEmployee));
  if (!selectedAdminEmployee) return;
  const employee = adminEmployees.find((item) => item.employeeEmail === selectedAdminEmployee || item.employeeId === selectedAdminEmployee);
  el("adminDetailTitle").textContent = `${employee?.employeeName || selectedAdminEmployee} 的用量详情`;
  el("adminDetailSubtitle").textContent = employee?.employeeEmail || employee?.employeeId || selectedAdminEmployee;
}

function renderDepartment() {
  if (isDepartmentLoading) {
    renderDepartmentLoading();
    return;
  }
  setDepartmentOverviewVisible(Boolean(selectedDepartment));
  const totalData = departmentSummaryData.length ? departmentSummaryData : departmentUsageData;
  if (selectedDepartment) {
    renderDepartmentMetrics(totalData);
    renderTrendTo("departmentTrendChart", totalData);
    renderSpendTrendTo("departmentSpendChart", totalData);
    renderDonutTo("departmentSourceDonut", "departmentDonutTotal", "departmentSourceLegend", departmentUsageData);
    renderModelBarsTo("departmentModelBars", departmentUsageData);
  }
  renderDepartmentUsers();
  renderDepartmentPickerOptions();

  renderDepartmentDetailCard();
}

function renderDepartmentDetailCard() {
  const detailCard = el("departmentDetailCard");
  if (!detailCard) return;
  detailCard.classList.toggle("show", Boolean(selectedDepartment));
  if (!selectedDepartment) return;
  const department = selectedDepartmentInfo();
  el("departmentDetailTitle").textContent = `${department.name} 的部门详情`;
  el("departmentDetailSubtitle").textContent = `部门 ID：${department.id} · 数据来源：${department.bindStatus} · 下方排行已切换为该部门员工用量`;
}

function renderTeamBlocked() {
  const status = currentUser?.teamBoardStatus || "none";
  const allowed = currentUser?.isTeamLeader && leaderTeams.length > 0 && status !== "none";
  el("teamDashboardContent").classList.toggle("hidden", !allowed);
  el("teamBlockedState").classList.toggle("hidden", allowed);
  if (!allowed) el("teamBlockedDesc").textContent = "当前账号还没有团队负责人权限。";
}

function renderTeamDetailCard() {
  const detailCard = el("teamDetailCard");
  if (!detailCard) return;
  detailCard.classList.toggle("show", Boolean(selectedTeamEmployee));
  const backButton = el("teamBackButton");
  backButton?.classList.toggle("hidden", !selectedTeamEmployee);
  if (selectedTeamEmployee) {
    const employee = selectedTeamEmployeeInfo();
    el("teamDetailTitle").textContent = `${employee?.employeeName || selectedTeamEmployee} 的用量详情`;
    el("teamDetailSubtitle").textContent = employee?.employeeEmail || employee?.employeeId || selectedTeamEmployee;
  }
}

function renderTeam() {
  renderTeamBlocked();
  if (!currentUser?.isTeamLeader || !leaderTeams.length) return;
  renderTeamSelector();
  if (isTeamLoading || isTeamMemberLoading) {
    renderTeamLoading();
    return;
  }
  renderTeamDetailCard();
  if (selectedTeamEmployee) {
    renderTeamMemberMetrics(teamMemberUsageData);
    renderTrendTo("teamTrendChart", teamMemberUsageData);
    renderSpendTrendTo("teamSpendChart", teamMemberUsageData);
    renderDonutTo("teamSourceDonut", "teamDonutTotal", "teamSourceLegend", teamMemberUsageData);
    renderModelBarsTo("teamModelBars", teamMemberUsageData);
    renderTeamUsers();
    return;
  }
  const totalData = teamSummaryData.length ? teamSummaryData : teamUsageData;
  renderTeamMetrics(totalData);
  renderTrendTo("teamTrendChart", totalData);
  renderSpendTrendTo("teamSpendChart", totalData);
  renderDonutTo("teamSourceDonut", "teamDonutTotal", "teamSourceLegend", teamUsageData);
  renderModelBarsTo("teamModelBars", teamUsageData);
  renderTeamUsers();
}

function render() {
  renderPersonal();
  if (currentUser?.isAdmin) renderAdmin();
  if (currentUser?.isAdmin) renderDepartment();
  if (currentUser?.isTeamLeader) renderTeam();
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

function keyStatusClass(status) {
  if (status === "正常") return "";
  if (status === "已过期") return "gold";
  return "rose";
}

function keyModelText(key) {
  const models = Array.isArray(key.models) ? key.models.filter(Boolean) : [];
  if (!models.length) return "全部可用模型";
  if (models.length <= 2) return models.join("、");
  return `${models.slice(0, 2).join("、")} 等 ${models.length} 个模型`;
}

function keySecretMarkup(key) {
  const keyId = String(key.id || "");
  const revealedValue = revealedKeys.get(keyId) || "";
  const isRevealed = Boolean(revealedValue);
  const isLoading = revealingKeyIds.has(keyId);
  const canReveal = Boolean(key.revealable);
  const title = canReveal
    ? isRevealed
      ? "隐藏完整密钥"
      : isLoading
        ? "正在读取完整密钥"
        : "查看完整密钥"
    : "该密钥创建时未保管完整值，请更新后查看";
  const help = canReveal ? "" : `<span class="key-reveal-help">更新后可查看完整密钥</span>`;
  return `
    <span class="key-secret-wrap">
      <span class="key-secret-control ${isRevealed ? "revealed" : ""}">
        <code class="key-masked-value">${escapeHtml(isRevealed ? revealedValue : key.masked || "sk-...----")}</code>
        <button
          class="key-reveal-button"
          type="button"
          data-reveal-key="${escapeHtml(keyId)}"
          aria-label="${escapeHtml(title)}"
          title="${escapeHtml(title)}"
          ${canReveal && !isLoading ? "" : "disabled"}
        ><svg aria-hidden="true"><use href="#icon-${isRevealed ? "eye-off" : "eye"}"></use></svg></button>
      </span>
      ${help}
    </span>
  `;
}

function hideRevealedKey(keyId) {
  const timer = revealTimers.get(keyId);
  if (timer) window.clearTimeout(timer);
  revealTimers.delete(keyId);
  revealedKeys.delete(keyId);
  revealingKeyIds.delete(keyId);
  if (currentView === "keys") renderKeys();
}

function clearRevealedKeys() {
  revealTimers.forEach((timer) => window.clearTimeout(timer));
  revealTimers = new Map();
  revealedKeys = new Map();
  revealingKeyIds = new Set();
  if (currentView === "keys" && el("keysView") && !el("keysView").classList.contains("hidden")) renderKeys();
}

async function toggleKeyReveal(keyId) {
  if (revealedKeys.has(keyId)) {
    hideRevealedKey(keyId);
    return;
  }
  const key = personalKeys.find((item) => String(item.id || "") === keyId);
  if (!key?.revealable) {
    showToast("该密钥创建时未保管完整值，请更新后查看");
    return;
  }
  if (revealingKeyIds.has(keyId)) return;
  revealingKeyIds.add(keyId);
  renderKeys();
  try {
    const payload = await api(`/api/me/keys/${encodeURIComponent(keyId)}/reveal`, {
      method: "POST",
      body: JSON.stringify({}),
      cache: "no-store",
    });
    if (!String(payload.key || "").startsWith("sk-")) throw new Error("服务未返回有效的完整密钥");
    revealedKeys.set(keyId, String(payload.key));
    const previousTimer = revealTimers.get(keyId);
    if (previousTimer) window.clearTimeout(previousTimer);
    revealTimers.set(keyId, window.setTimeout(() => hideRevealedKey(keyId), 30000));
  } catch (error) {
    showToast(error.message || "完整密钥读取失败");
  } finally {
    revealingKeyIds.delete(keyId);
    renderKeys();
  }
}

function renderKeys() {
  const countText = `${fmt.format(personalKeys.length)} 个密钥`;
  setText("keyCount", isKeysLoading ? "加载中" : countText);
  const tableBody = el("keyTableBody");
  const cardList = el("keyCardList");

  if (isKeysLoading) {
    tableBody.innerHTML = `<tr><td colspan="8" class="key-loading">正在加载个人密钥...</td></tr>`;
    cardList.innerHTML = `<article class="panel key-loading">正在加载个人密钥...</article>`;
    return;
  }
  if (keyLoadError) {
    const message = escapeHtml(keyLoadError);
    tableBody.innerHTML = `<tr><td colspan="8" class="key-empty">${message}</td></tr>`;
    cardList.innerHTML = `<article class="panel key-empty">${message}</article>`;
    return;
  }
  if (!personalKeys.length) {
    const emptyMessage = "还没有个人密钥，点击“添加密钥”创建第一个。";
    tableBody.innerHTML = `<tr><td colspan="8" class="key-empty">${emptyMessage}</td></tr>`;
    cardList.innerHTML = `<article class="panel key-empty">${emptyMessage}</article>`;
    return;
  }

  tableBody.innerHTML = personalKeys
    .map((key) => {
      const id = escapeHtml(key.id);
      const name = escapeHtml(key.name || "个人访问密钥");
      const purpose = escapeHtml(key.purpose || "用于个人 AI 工具访问。");
      const status = escapeHtml(key.status || "正常");
      const cleanupRequired = Boolean(key.cleanupRequired);
      const oldKeyId = escapeHtml(key.oldKeyId || key.id || "");
      const replacementKeyId = escapeHtml(key.replacementKeyId || "");
      const isDisabling = disablingOldKeyIds.has(String(key.oldKeyId || key.id || ""));
      const cleanupState = cleanupRequired
        ? `<span class="key-cleanup-state">旧密钥仍有效，请完成停用</span>`
        : "";
      const rotationAction = cleanupRequired
        ? `<button class="ghost-btn retry-disable-key-btn" type="button" data-disable-old-key="${oldKeyId}" data-replacement-key="${replacementKeyId}" ${isDisabling ? "disabled" : ""}>${isDisabling ? "停用中..." : "重试停用旧密钥"}</button>`
        : "";
      return `
        <tr>
          <td><div class="key-name-cell"><strong>${name}</strong><span>${purpose}</span>${cleanupState}</div></td>
          <td><span class="chip ${keyStatusClass(key.status)}">${status}</span></td>
          <td>${keySecretMarkup(key)}</td>
          <td><span class="key-model-summary">${escapeHtml(keyModelText(key))}</span></td>
          <td>${escapeHtml(key.createdAt || "-")}</td>
          <td>${escapeHtml(key.lastUsed || "-")}</td>
          <td>${escapeHtml(key.expiresAt || "永不过期")}</td>
          <td>
            <div class="key-row-actions">
              <button class="ghost-btn key-regenerate-btn" type="button" data-regenerate-key="${id}" ${cleanupRequired ? "disabled title=\"请先停用旧密钥\"" : ""}>更新</button>
              ${rotationAction}
              <button class="danger-outline-btn" type="button" data-delete-key="${id}">删除</button>
            </div>
          </td>
        </tr>
      `;
    })
    .join("");

  cardList.innerHTML = personalKeys
    .map((key) => {
      const cleanupRequired = Boolean(key.cleanupRequired);
      const keyId = escapeHtml(key.id);
      const oldKeyId = escapeHtml(key.oldKeyId || key.id || "");
      const replacementKeyId = escapeHtml(key.replacementKeyId || "");
      const isDisabling = disablingOldKeyIds.has(String(key.oldKeyId || key.id || ""));
      return `
      <article class="panel key-mobile-card">
        <div class="key-mobile-head">
          <div class="key-name-cell">
            <strong>${escapeHtml(key.name || "个人访问密钥")}</strong>
            <span>${escapeHtml(key.purpose || "用于个人 AI 工具访问。")}</span>
            ${cleanupRequired ? `<span class="key-cleanup-state">旧密钥仍有效，请完成停用</span>` : ""}
          </div>
          <span class="chip ${keyStatusClass(key.status)}">${escapeHtml(key.status || "正常")}</span>
        </div>
        <div class="key-mobile-row"><span>密钥</span>${keySecretMarkup(key)}</div>
        <div class="key-mobile-row"><span>可用模型</span><strong>${escapeHtml(keyModelText(key))}</strong></div>
        <div class="key-mobile-row"><span>创建时间</span><strong>${escapeHtml(key.createdAt || "-")}</strong></div>
        <div class="key-mobile-row"><span>最近使用</span><strong>${escapeHtml(key.lastUsed || "-")}</strong></div>
        <div class="key-mobile-row"><span>过期时间</span><strong>${escapeHtml(key.expiresAt || "永不过期")}</strong></div>
        <div class="key-mobile-actions">
          <button class="ghost-btn" type="button" data-regenerate-key="${keyId}" ${cleanupRequired ? "disabled title=\"请先停用旧密钥\"" : ""}>更新</button>
          ${cleanupRequired ? `<button class="ghost-btn retry-disable-key-btn" type="button" data-disable-old-key="${oldKeyId}" data-replacement-key="${replacementKeyId}" ${isDisabling ? "disabled" : ""}>${isDisabling ? "停用中..." : "重试停用旧密钥"}</button>` : ""}
          <button class="danger-outline-btn" type="button" data-delete-key="${keyId}">删除</button>
        </div>
      </article>
    `;
    })
    .join("");
}

function renderKeyModelChoices() {
  const choices = el("keyModelChoices");
  if (!availableKeyModels.length) {
    choices.innerHTML = `<div class="key-model-empty">当前账号没有可选的指定模型。</div>`;
    return;
  }
  choices.innerHTML = availableKeyModels
    .map((model) => `
      <label class="model-choice">
        <input type="checkbox" name="keyModel" value="${escapeHtml(model)}" />
        <span>${escapeHtml(model)}</span>
      </label>
    `)
    .join("");
}

function updateKeyModelMode() {
  const custom = el("keyModelMode").value === "custom";
  el("keyModelChoices").classList.toggle("hidden", !custom);
  if (!custom) {
    el("keyModelChoices").querySelectorAll("input").forEach((input) => {
      input.checked = false;
    });
  }
}

function openCreateKeyModal() {
  if (!availableKeyModels.length) {
    showToast("当前账号没有可用于创建访问密钥的模型权限，请联系管理员开通模型权限。");
    return;
  }
  el("createKeyForm").reset();
  el("keyModelMode").value = "all";
  renderKeyModelChoices();
  updateKeyModelMode();
  const scopeText = unrestrictedKeyModels
    ? "全部可用模型会跟随当前账号的全模型权限。"
    : "全部可用模型会限制在你当前账号已授权的模型范围内。";
  setText("keyModelHint", scopeText);
  el("createKeyModal").classList.remove("hidden");
  window.setTimeout(() => el("keyNameInput").focus(), 0);
}

function closeCreateKeyModal() {
  if (isCreatingKey) return;
  el("createKeyModal").classList.add("hidden");
  el("createKeyForm").reset();
  updateKeyModelMode();
}

function closeRegenerateKeyModal() {
  if (isRegeneratingKey) return;
  pendingRegenerateKeyId = "";
  el("regenerateKeyModal").classList.add("hidden");
}

function updateDeleteKeyConfirmation() {
  const matches = el("deleteKeyConfirmInput").value.trim() === pendingDeleteKeyName;
  el("confirmDeleteKey").disabled = isDeletingKey || !pendingDeleteKeyName || !matches;
}

function closeDeleteKeyModal() {
  if (isDeletingKey) return;
  pendingDeleteKeyId = "";
  pendingDeleteKeyName = "";
  el("deleteKeyConfirmInput").value = "";
  setText("deleteKeyName", "-");
  setText("deleteKeyMasked", "sk-...----");
  setText("deleteKeyExpectedName", "-");
  el("confirmDeleteKey").disabled = true;
  el("deleteKeyModal").classList.add("hidden");
}

function showPlainKey(key, expiry = "", options = {}) {
  currentPlainKey = String(key || "");
  const cleanupRequired = Boolean(options.cleanupRequired && options.oldKeyDisabled !== true);
  currentPlainKeyCleanup = cleanupRequired
    ? {
        oldKeyId: String(options.oldKeyId || ""),
        replacementKeyId: String(options.replacementKeyId || options.id || ""),
      }
    : null;
  setText("newKeyValue", currentPlainKey);
  setText("newKeyExpiry", expiry ? `过期时间：${expiry}` : "");
  const warning = String(options.warning || "");
  const isRotation = Boolean(options.rotationMode);
  setText("newKeyTitle", isRotation ? "新密钥已创建" : "请立即保存新密钥");
  setText(
    "newKeyNotice",
    warning || (cleanupRequired
      ? "新密钥已经创建并可以立即配置使用，但旧密钥尚未停用。"
      : options.revealable === false
        ? "完整密钥只显示这一次。关闭窗口后无法再次查看，请立即复制并安全保存。"
        : isRotation
          ? "新密钥已加密保管，旧密钥已停用。请将使用旧密钥的工具更新为新密钥。"
          : "密钥已加密保管，关闭窗口后仍可在列表中通过眼睛按钮查看。"),
  );
  el("newKeyNoticeBox").classList.toggle("success", !warning && !cleanupRequired && options.revealable !== false);
  el("rotationCleanupPanel").classList.toggle("hidden", !cleanupRequired);
  setText(
    "rotationCleanupMessage",
    warning || "新密钥已经可以使用，但旧密钥目前仍然有效。请先替换工具中的配置，然后重试停用旧密钥。",
  );
  el("retryDisableOldKey").disabled = false;
  el("retryDisableOldKey").textContent = "重试停用旧密钥";
  el("newKeyModal").classList.remove("hidden");
}

function clearPlainKey() {
  currentPlainKey = "";
  currentPlainKeyCleanup = null;
  setText("newKeyValue", "");
  setText("newKeyExpiry", "");
  el("rotationCleanupPanel").classList.add("hidden");
  el("newKeyModal").classList.add("hidden");
}

async function loadKeys(forceRefresh = false) {
  if (!currentUser || isKeysLoading) return;
  if (revealedKeys.size || revealTimers.size || revealingKeyIds.size) clearRevealedKeys();
  isKeysLoading = true;
  keyLoadError = "";
  renderKeys();
  try {
    const payload = await api(`/api/me/keys${forceRefresh ? "?refresh=1" : ""}`);
    personalKeys = Array.isArray(payload.keys) ? payload.keys : [];
    availableKeyModels = Array.isArray(payload.availableModels) ? payload.availableModels : [];
    unrestrictedKeyModels = Boolean(payload.unrestrictedModels);
  } catch (error) {
    personalKeys = [];
    availableKeyModels = [];
    unrestrictedKeyModels = false;
    keyLoadError = error.message || "个人密钥加载失败，请稍后重试。";
    showToast(keyLoadError);
  } finally {
    isKeysLoading = false;
    renderKeys();
  }
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
  if (view === "team" && !currentUser?.isTeamLeader) view = "dashboard";
  if (currentView === "keys" && view !== "keys") clearRevealedKeys();
  currentView = view;
  el("dashboardView").classList.toggle("hidden", view !== "dashboard");
  el("adminView").classList.toggle("hidden", view !== "admin");
  el("teamView").classList.toggle("hidden", view !== "team");
  el("departmentView").classList.toggle("hidden", view !== "department");
  el("keysView").classList.toggle("hidden", view !== "keys");
  el("modelsView").classList.toggle("hidden", view !== "models");
  el("dashboardFilters").classList.toggle("hidden", view === "models" || view === "keys");
  let activeButton = null;
  document.querySelectorAll("[data-view]").forEach((button) => {
    const isActive = button.dataset.view === view;
    button.classList.toggle("active", isActive);
    if (isActive) {
      activeButton = button;
      button.setAttribute("aria-current", "page");
    } else button.removeAttribute("aria-current");
  });
  if (activeButton && window.innerWidth <= 820) {
    requestAnimationFrame(() => {
      const navZone = activeButton.closest(".nav-zone");
      if (!navZone) return;
      const targetLeft = activeButton.offsetLeft - (navZone.clientWidth - activeButton.offsetWidth) / 2;
      navZone.scrollLeft = Math.max(0, Math.min(targetLeft, navZone.scrollWidth - navZone.clientWidth));
    });
  }
  if (view === "models") {
    renderModels();
    if (!modelCatalog.length) loadModels();
  }
  if (view === "keys") {
    renderKeys();
    if (!personalKeys.length && !isKeysLoading) loadKeys();
  }
  if (view === "dashboard" && !usageData.length) loadDashboardData();
  if (view === "admin" && !adminUsageData.length) loadAdminData();
  if (view === "team" && currentUser?.isTeamLeader && !teamUsageData.length) loadTeamData();
  if (view === "department" && !departmentUsageData.length) loadDepartmentData();
}

async function loadCurrentViewData(forceRefresh = false) {
  if (currentView === "keys") return loadKeys();
  if (currentView === "models") return loadModels();
  if (currentView === "admin") return loadAdminData(forceRefresh);
  if (currentView === "team") return loadTeamData(forceRefresh);
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
    personalDataFreshness = payload.dataFreshness || null;
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
    adminDataFreshness = payload.dataFreshness || null;
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
    departmentDataFreshness = payload.dataFreshness || null;
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

async function loadTeamData(forceRefresh = false) {
  if (!currentUser?.isTeamLeader || !leaderTeams.length || isTeamLoading) return;
  ensureSelectedTeamRef();
  resetTeamMemberSelection();
  isTeamLoading = true;
  renderTeam();
  const { startDate, endDate } = selectedDateRange();
  const source = el("sourceSelect").value;
  const query = new URLSearchParams({ start_date: startDate, end_date: endDate, source });
  if (selectedTeamRef) query.set("team_ref", selectedTeamRef);
  if (forceRefresh) query.set("refresh", "1");
  try {
    const payload = await api(`/api/team/usage?${query.toString()}`);
    teamUsageData = payload.rows || [];
    teamSummaryData = payload.summaryRows || teamUsageData;
    teamEmployees = payload.employees || [];
    teamInfo = payload.team || currentUser.team || null;
    teamDataFreshness = payload.dataFreshness || null;
    lastTeamUsageCacheHit = Boolean(payload.cache?.hit);
    if (payload.truncated) {
      el("teamLimitHint").textContent = "成员排行按团队成员账号用量汇总，当前数据读取达到上限，排行可能不完整";
    } else {
      el("teamLimitHint").textContent = "成员排行按团队成员账号用量汇总，包含零用量成员";
    }
  } catch (error) {
    showToast(error.message || "团队数据加载失败");
    teamUsageData = [];
    teamSummaryData = [];
    teamEmployees = [];
  } finally {
    isTeamLoading = false;
    renderTeam();
  }
}

async function loadTeamMemberData(employee, forceRefresh = false, scrollToCard = true) {
  if (!currentUser?.isTeamLeader || !leaderTeams.length) return;
  ensureSelectedTeamRef();
  const keepFilters = forceRefresh && selectedTeamEmployee === employee;
  selectedTeamEmployee = employee;
  const requestId = ++teamMemberUsageRequestId;
  teamMemberUsageData = [];
  teamMemberUsageSummary = null;
  if (!keepFilters) teamMemberUsageFilters = { date: "all", model: "all", status: "all", keyword: "" };
  isTeamMemberLoading = true;
  updateTeamMemberLoadingLabels();
  renderTeam();
  if (scrollToCard) scrollToDetailCard("teamDetailCard");
  const { startDate, endDate } = selectedDateRange();
  const source = el("sourceSelect").value;
  const query = new URLSearchParams({ start_date: startDate, end_date: endDate, source, employee });
  if (selectedTeamRef) query.set("team_ref", selectedTeamRef);
  if (forceRefresh) query.set("refresh", "1");
  try {
    const payload = await api(`/api/team/member/usage?${query.toString()}`);
    if (requestId !== teamMemberUsageRequestId) return;
    teamMemberUsageData = payload.rows || [];
    teamMemberUsageSummary = payload.summary || null;
    teamDataFreshness = payload.dataFreshness || null;
    const employeePayload = payload.employee || {};
    const employeeId = employeePayload.employeeEmail || employeePayload.employeeId || employee;
    if (employeeId && employeeId !== selectedTeamEmployee) selectedTeamEmployee = employeeId;
    setText("teamLimitHint", "成员排行保留当前团队汇总，已切换上方看板为所选成员明细");
  } catch (error) {
    if (requestId !== teamMemberUsageRequestId) return;
    showToast(error.message || "成员用量明细加载失败");
    teamMemberUsageData = [];
    teamMemberUsageSummary = null;
  } finally {
    if (requestId === teamMemberUsageRequestId) {
      isTeamMemberLoading = false;
      renderTeam();
    }
  }
}

function clearTeamMemberSelection() {
  resetTeamMemberSelection();
  renderTeam();
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
  leaderTeams = normalizeLeaderTeams(user);
  selectedTeamRef = user.team?.teamRef || leaderTeams[0]?.teamRef || "";
  resetTeamMemberSelection();
  ensureSelectedTeamRef();
  el("authLoadingView").classList.add("hidden");
  el("loginView").classList.add("hidden");
  el("appView").classList.remove("hidden");
  el("adminTab").classList.add("hidden");
  el("teamTab").classList.add("hidden");
  el("departmentTab").classList.add("hidden");
  el("userEmail").textContent = user.email;
  el("userName").textContent = user.name;
  el("avatar").textContent = user.avatar || initials(user.email, user.name);
  el("teamWelcomeTitle").textContent = `所选范围 · ${teamScopeLabel()}`;
  el("departmentWelcomeTitle").textContent = "所选范围 · 全部部门";
  switchView("dashboard");
  render();
  const scopePromise = loadAuthScope();
  await Promise.all([loadCurrentViewData(), loadModels()]);
  await scopePromise;
}

async function loadAuthScope() {
  try {
    const scope = await api("/api/auth/scope");
    Object.assign(currentUser, scope);
    leaderTeams = normalizeLeaderTeams(currentUser);
    selectedTeamRef = currentUser.team?.teamRef || leaderTeams[0]?.teamRef || "";
    el("adminTab").classList.toggle("hidden", !currentUser.isAdmin);
    el("teamTab").classList.toggle("hidden", !currentUser.isTeamLeader);
    el("departmentTab").classList.toggle("hidden", !currentUser.isAdmin);
    el("teamWelcomeTitle").textContent = `所选范围 · ${teamScopeLabel()}`;
    render();
  } catch (error) {
    showToast("部分权限信息加载失败，请刷新重试");
  }
}

function showLogin() {
  currentUser = null;
  isSsoRedirecting = false;
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
  teamUsageData = [];
  teamSummaryData = [];
  teamEmployees = [];
  teamMemberUsageData = [];
  teamMemberUsageSummary = null;
  resetTeamMemberSelection();
  teamInfo = null;
  leaderTeams = [];
  selectedTeamRef = "";
  departmentPickerOptions = [];
  personalKeys = [];
  availableKeyModels = [];
  unrestrictedKeyModels = false;
  keyLoadError = "";
  pendingRegenerateKeyId = "";
  pendingDeleteKeyId = "";
  pendingDeleteKeyName = "";
  isDeletingKey = false;
  el("deleteKeyModal").classList.add("hidden");
  el("deleteKeyConfirmInput").value = "";
  el("confirmDeleteKey").disabled = true;
  el("confirmDeleteKey").textContent = "确认删除";
  el("cancelDeleteKey").disabled = false;
  clearRevealedKeys();
  clearPlainKey();
  el("departmentEmployeeSearch").value = "";
  closeDepartmentPicker();
  el("appView").classList.add("hidden");
  el("loginView").classList.remove("hidden");
  el("authLoadingView").classList.add("hidden");
  el("ssoButton").disabled = false;
  el("devLoginButton").disabled = false;
  el("ssoButton").lastChild.textContent = authConfig.providerName || "飞书扫码登录";
}

document.addEventListener("submit", async (event) => {
  if (event.target.id !== "loginForm") return;
  event.preventDefault();
  if (!authConfig.devLoginEnabled) {
    startSsoLogin();
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

el("ssoButton").addEventListener("click", startSsoLogin);

el("logoutButton").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST", body: JSON.stringify({}) });
  } catch {}
  showLogin();
});

document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));

async function reloadForFilterChange() {
  // 保留当前下钻选择:切换时间范围/来源时应停留在已下钻的员工/成员/部门,
  // 而不是退回聚合看板。各 load 函数已把选择变量透传给后端查询。
  if (currentView === "team") {
    // loadTeamData 内部会 resetTeamMemberSelection,故成员下钻时须走成员加载。
    if (selectedTeamEmployee) {
      await loadTeamMemberData(selectedTeamEmployee, false, false);
    } else {
      await loadTeamData();
    }
    return;
  }
  await loadCurrentViewData();
}

el("rangeSelect").addEventListener("change", reloadForFilterChange);

el("sourceSelect").addEventListener("change", reloadForFilterChange);

["usageDetailDateFilter", "usageDetailModelFilter", "usageDetailStatusFilter"].forEach((id) => {
  el(id).addEventListener("change", updateUsageTableFilters);
});
el("usageDetailSearch").addEventListener("input", updateUsageTableFilters);
el("usageDetailReset").addEventListener("click", resetUsageTableFilters);

el("refreshButton").addEventListener("click", async () => {
  if (currentView === "keys") {
    await loadKeys(true);
    showToast(keyLoadError ? "密钥列表刷新失败" : "已刷新密钥列表");
  } else if (currentView === "models") {
    await loadModels();
    showToast("\u5df2\u5237\u65b0\u6a21\u578b\u5217\u8868");
  } else if (currentView === "admin") {
    await loadAdminData(true);
    showToast("已刷新全员用量");
  } else if (currentView === "team") {
    if (selectedTeamEmployee) {
      await loadTeamMemberData(selectedTeamEmployee, true, false);
      showToast("已刷新成员明细");
    } else {
      await loadTeamData(true);
      showToast("已刷新团队用量");
    }
  } else if (currentView === "department") {
    await loadDepartmentData(true);
    showToast("已刷新部门用量");
  } else {
    await loadDashboardData(true);
    showToast("已刷新个人用量");
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
  const loading = loadAdminData();
  scrollToDetailCard("adminDetailCard");
  await loading;
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
  const loading = loadDepartmentData();
  scrollToDetailCard("departmentDetailCard");
  await loading;
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

el("teamSelect").addEventListener("change", async (event) => {
  selectedTeamRef = event.target.value;
  teamInfo = leaderTeams.find((item) => item.teamRef === selectedTeamRef) || null;
  teamUsageData = [];
  teamSummaryData = [];
  teamEmployees = [];
  resetTeamMemberSelection();
  await loadTeamData();
});

el("teamUserTable").addEventListener("click", async (event) => {
  const row = event.target.closest("[data-employee]");
  if (!row) return;
  await loadTeamMemberData(row.dataset.employee);
});

el("teamBackButton").addEventListener("click", clearTeamMemberSelection);

["teamMemberUsageDetailDateFilter", "teamMemberUsageDetailModelFilter", "teamMemberUsageDetailStatusFilter"].forEach((id) => {
  el(id).addEventListener("change", updateTeamMemberUsageFilters);
});
el("teamMemberUsageDetailSearch").addEventListener("input", updateTeamMemberUsageFilters);
el("teamMemberUsageDetailReset").addEventListener("click", resetTeamMemberUsageFilters);

el("modelSearch").addEventListener("input", renderModels);
el("providerFilter").addEventListener("change", renderModels);
el("capabilityFilter").addEventListener("change", renderModels);
el("modelGrid").addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy-model]");
  if (button) copyText(button.dataset.copyModel, "模型名称已复制");
});

el("addKeyButton").addEventListener("click", openCreateKeyModal);
el("cancelCreateKey").addEventListener("click", closeCreateKeyModal);
el("keyModelMode").addEventListener("change", updateKeyModelMode);

el("createKeyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (isCreatingKey) return;
  const name = el("keyNameInput").value.trim();
  const purpose = el("keyPurposeInput").value.trim();
  const duration = el("keyDurationSelect").value;
  const customModels = [...el("keyModelChoices").querySelectorAll('input[name="keyModel"]:checked')].map((input) => input.value);
  if (!availableKeyModels.length) {
    showToast("当前账号没有可用于创建访问密钥的模型权限，请联系管理员开通模型权限。");
    return;
  }
  if (name.length < 2) {
    showToast("密钥名称至少需要 2 个字符");
    el("keyNameInput").focus();
    return;
  }
  if (el("keyModelMode").value === "custom" && !customModels.length) {
    showToast("请至少选择一个模型");
    return;
  }
  isCreatingKey = true;
  el("submitCreateKey").disabled = true;
  el("submitCreateKey").textContent = "创建中...";
  try {
    const payload = await api("/api/me/keys", {
      method: "POST",
      body: JSON.stringify({
        name,
        purpose,
        duration,
        models: el("keyModelMode").value === "custom" ? customModels : [],
      }),
    });
    el("createKeyModal").classList.add("hidden");
    el("createKeyForm").reset();
    updateKeyModelMode();
    personalKeys = [];
    await loadKeys(true);
    showPlainKey(payload.key, payload.expiresAt || "", payload);
  } catch (error) {
    showToast(error.message || "创建密钥失败");
  } finally {
    isCreatingKey = false;
    el("submitCreateKey").disabled = false;
    el("submitCreateKey").textContent = "创建密钥";
  }
});

function requestRegenerateKey(keyId) {
  const key = personalKeys.find((item) => String(item.id || "") === String(keyId || ""));
  if (key?.cleanupRequired) {
    showToast("请先停用上次更新留下的旧密钥");
    return;
  }
  pendingRegenerateKeyId = keyId;
  el("regenerateKeyModal").classList.remove("hidden");
}

async function disableOldKey(oldKeyId, replacementKeyId, options = {}) {
  const normalizedOldKeyId = String(oldKeyId || "");
  const normalizedReplacementKeyId = String(replacementKeyId || "");
  if (!normalizedOldKeyId || !normalizedReplacementKeyId) {
    showToast("缺少密钥更新信息，请刷新后重试");
    return;
  }
  if (disablingOldKeyIds.has(normalizedOldKeyId)) return;
  disablingOldKeyIds.add(normalizedOldKeyId);
  renderKeys();
  if (options.fromModal) {
    el("retryDisableOldKey").disabled = true;
    el("retryDisableOldKey").textContent = "停用中...";
  }
  try {
    const payload = await api(`/api/me/keys/${encodeURIComponent(normalizedOldKeyId)}/disable-old`, {
      method: "POST",
      body: JSON.stringify({ replacementKeyId: normalizedReplacementKeyId }),
    });
    personalKeys = personalKeys.map((key) => (
      String(key.oldKeyId || key.id || "") === normalizedOldKeyId
        ? { ...key, cleanupRequired: false, oldKeyDisabled: true }
        : key
    ));
    if (currentPlainKeyCleanup?.oldKeyId === normalizedOldKeyId) {
      currentPlainKeyCleanup = null;
      el("rotationCleanupPanel").classList.add("hidden");
      el("newKeyNoticeBox").classList.add("success");
      setText("newKeyNotice", "新密钥已加密保管，旧密钥现已停用。请将使用旧密钥的工具更新为新密钥。");
    }
    await loadKeys(true);
    showToast(payload.warning || "旧密钥已停用");
  } catch (error) {
    showToast(error.message || "旧密钥停用失败，请稍后重试");
  } finally {
    disablingOldKeyIds.delete(normalizedOldKeyId);
    if (options.fromModal && currentPlainKeyCleanup?.oldKeyId === normalizedOldKeyId) {
      el("retryDisableOldKey").disabled = false;
      el("retryDisableOldKey").textContent = "重试停用旧密钥";
    }
    renderKeys();
  }
}

function requestDeleteKey(keyId) {
  const key = personalKeys.find((item) => String(item.id || "") === String(keyId || ""));
  if (!key) {
    showToast("未找到要删除的密钥，请刷新后重试");
    return;
  }
  hideRevealedKey(String(keyId));
  pendingDeleteKeyId = String(keyId);
  pendingDeleteKeyName = String(key.name || "个人访问密钥");
  el("deleteKeyConfirmInput").value = "";
  setText("deleteKeyName", pendingDeleteKeyName);
  setText("deleteKeyMasked", key.masked || "sk-...----");
  setText("deleteKeyExpectedName", pendingDeleteKeyName);
  updateDeleteKeyConfirmation();
  el("deleteKeyModal").classList.remove("hidden");
  window.setTimeout(() => el("deleteKeyConfirmInput").focus(), 0);
}

el("keysView").addEventListener("click", (event) => {
  const revealButton = event.target.closest("[data-reveal-key]");
  if (revealButton) {
    toggleKeyReveal(revealButton.dataset.revealKey);
    return;
  }
  const deleteButton = event.target.closest("[data-delete-key]");
  if (deleteButton) {
    requestDeleteKey(deleteButton.dataset.deleteKey);
    return;
  }
  const disableButton = event.target.closest("[data-disable-old-key]");
  if (disableButton) {
    disableOldKey(disableButton.dataset.disableOldKey, disableButton.dataset.replacementKey);
    return;
  }
  const button = event.target.closest("[data-regenerate-key]");
  if (button) requestRegenerateKey(button.dataset.regenerateKey);
});

el("deleteKeyConfirmInput").addEventListener("input", updateDeleteKeyConfirmation);
el("cancelDeleteKey").addEventListener("click", closeDeleteKeyModal);
el("deleteKeyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!pendingDeleteKeyId || isDeletingKey || el("deleteKeyConfirmInput").value.trim() !== pendingDeleteKeyName) return;
  const keyId = pendingDeleteKeyId;
  isDeletingKey = true;
  el("deleteKeyConfirmInput").disabled = true;
  el("cancelDeleteKey").disabled = true;
  el("confirmDeleteKey").disabled = true;
  el("confirmDeleteKey").textContent = "删除中...";
  try {
    const payload = await api(`/api/me/keys/${encodeURIComponent(keyId)}`, { method: "DELETE" });
    hideRevealedKey(keyId);
    pendingDeleteKeyId = "";
    pendingDeleteKeyName = "";
    el("deleteKeyModal").classList.add("hidden");
    el("deleteKeyConfirmInput").value = "";
    personalKeys = [];
    await loadKeys(true);
    showToast(payload.warning || "密钥已删除并立即失效");
  } catch (error) {
    showToast(error.message || "删除密钥失败");
  } finally {
    isDeletingKey = false;
    el("deleteKeyConfirmInput").disabled = false;
    el("cancelDeleteKey").disabled = false;
    el("confirmDeleteKey").textContent = "确认删除";
    updateDeleteKeyConfirmation();
  }
});

el("cancelRegenerateKey").addEventListener("click", closeRegenerateKeyModal);
el("confirmRegenerateKey").addEventListener("click", async () => {
  if (!pendingRegenerateKeyId || isRegeneratingKey) return;
  const oldKeyId = pendingRegenerateKeyId;
  isRegeneratingKey = true;
  el("confirmRegenerateKey").disabled = true;
  el("confirmRegenerateKey").textContent = "更新中...";
  try {
    const payload = await api(`/api/me/keys/${encodeURIComponent(oldKeyId)}/regenerate`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    pendingRegenerateKeyId = "";
    el("regenerateKeyModal").classList.add("hidden");
    personalKeys = [];
    await loadKeys(true);
    showPlainKey(payload.key, payload.expiresAt || "", {
      ...payload,
      oldKeyId: payload.oldKeyId || oldKeyId,
      replacementKeyId: payload.replacementKeyId || payload.id || "",
    });
  } catch (error) {
    showToast(error.message || "更新密钥失败");
  } finally {
    isRegeneratingKey = false;
    el("confirmRegenerateKey").disabled = false;
    el("confirmRegenerateKey").textContent = "确认更新";
  }
});

el("copyNewKey").addEventListener("click", () => {
  if (currentPlainKey) copyText(currentPlainKey, "完整密钥已复制");
});
el("retryDisableOldKey").addEventListener("click", () => {
  if (!currentPlainKeyCleanup) {
    showToast("未找到需要停用的旧密钥，请刷新列表后重试");
    return;
  }
  disableOldKey(currentPlainKeyCleanup.oldKeyId, currentPlainKeyCleanup.replacementKeyId, { fromModal: true });
});
el("closeNewKey").addEventListener("click", clearPlainKey);

document.querySelectorAll(".modal-backdrop").forEach((backdrop) => {
  backdrop.addEventListener("click", (event) => {
    if (event.target !== backdrop) return;
    if (backdrop.id === "createKeyModal") closeCreateKeyModal();
    if (backdrop.id === "regenerateKeyModal") closeRegenerateKeyModal();
    if (backdrop.id === "deleteKeyModal") closeDeleteKeyModal();
    if (backdrop.id === "newKeyModal") clearPlainKey();
  });
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!el("newKeyModal").classList.contains("hidden")) clearPlainKey();
  else if (!el("deleteKeyModal").classList.contains("hidden")) closeDeleteKeyModal();
  else if (!el("regenerateKeyModal").classList.contains("hidden")) closeRegenerateKeyModal();
  else if (!el("createKeyModal").classList.contains("hidden")) closeCreateKeyModal();
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearRevealedKeys();
});

window.addEventListener("beforeunload", clearRevealedKeys);

async function init() {
  const callbackParams = new URLSearchParams(window.location.search);
  const hasAuthCallback = callbackParams.get("auth_callback") === "success";
  if (hasAuthCallback) {
    el("loginView").classList.add("hidden");
    el("authLoadingView").classList.remove("hidden");
    callbackParams.delete("auth_callback");
    window.history.replaceState({}, "", `${window.location.pathname}${callbackParams.toString() ? `?${callbackParams}` : ""}${window.location.hash}`);
  }
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
    : "使用公司飞书账号扫码登录；本页面不会保存真实密码或登录凭据。";
  showLoginCallbackMessage();
  setupModelFilters();
  try {
    const user = await api("/api/auth/me");
    await showApp(user);
  } catch {
    el("authLoadingView").classList.add("hidden");
    showLogin();
  }
}

init();
