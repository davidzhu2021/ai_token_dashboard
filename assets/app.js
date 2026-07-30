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
let adminUsageScopeKey = "";
let adminUsageLoadingScopeKey = "";
let adminUsageRequestId = 0;
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
let teamUsageRequestController = null;
let teamUsageRequestId = 0;
let teamRankingRequestController = null;
let teamRankingRequestId = 0;
let isTeamRankingLoading = false;
let teamRankingError = "";
let teamRankingHint = "";
const teamUsagePayloadCache = new Map();
let teamMemberUsageFilters = { date: "all", model: "all", status: "all", keyword: "" };
let leaderTeams = [];
let selectedTeamRef = "";
let selectedDepartment = "";
let departmentPickerOpen = false;
let departmentPickerOptions = [];
let departmentUsageScopeKey = "";
let departmentUsageLoadingScopeKey = "";
let departmentUsageRequestId = 0;
let modelCatalog = [];
let modelViewMode = "card";
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
let organizationSnapshot = null;
let organizationMembers = [];
let organizationMemberTotal = 0;
let organizationMemberPage = 1;
const organizationMemberPageSize = 20;
let organizationMemberFilters = { search: "", departmentId: "", role: "", status: "" };
let isOrganizationLoading = false;
let isOrganizationMemberLoading = false;
let organizationDataLoadingScopeKey = "";
let organizationMemberLoadingScopeKey = "";
let organizationDataRequestId = 0;
let organizationMemberRequestId = 0;
let isOrganizationDepartmentSaving = false;
let isOrganizationMemberSaving = false;
let editingOrganizationDepartmentId = "";
let editingOrganizationMemberId = "";
let organizationSearchTimer = null;
let customerOrganizations = [];
let customerOrganizationsTotal = 0;
let customerOrganizationsPage = 1;
const customerOrganizationsPageSize = 12;
let customerOrganizationsFilters = { search: "", status: "" };
let isCustomerOrganizationsLoading = false;
let customerOrganizationsSearchTimer = null;
let selectedCustomerOrganization = null;
let customerOrganizationDetailTab = "info";
let isCustomerOrganizationSaving = false;
let editingCustomerOrganizationId = "";
let isSsoRedirecting = false;
let billingConfig = null;
let billingAccount = null;
let billingOrders = [];
let billingOrderTotal = 0;
let isBillingLoading = false;
let isCreatingTopup = false;
let isSubmittingManualPay = false;
let billingLoadError = "";
let billingAvailable = false;
let selectedTopupAmount = 0;
let pendingTopupTradeNo = "";
let topupPollTimer = null;
let authConfig = {
  devLoginEnabled: false,
  oidcConfigured: false,
  providerName: "飞书扫码登录",
  passwordLoginEnabled: false,
  publicSignupEnabled: false,
  emailVerificationRequired: true,
  turnstileEnabled: false,
  turnstileConfigured: false,
  turnstileSiteKey: "",
  passwordLoginConfigured: false,
  passwordLoginAvailable: false,
  passwordLoginUnavailableCode: "",
  passwordLoginUnavailableReason: "",
  publicSignupConfigured: false,
  publicSignupAvailable: false,
  publicSignupUnavailableCode: "",
  publicSignupUnavailableReason: "",
  passwordRecoveryEnabled: false,
  passwordRecoveryConfigured: false,
  passwordRecoveryAvailable: false,
  passwordRecoveryUnavailableCode: "",
  passwordRecoveryUnavailableReason: "",
  allowedSignupDomains: [],
};
let authAccess = "personal";
let authMode = "login";
let authSubmitting = false;
let authCsrfToken = "";
let csrfRefreshPromise = null;
let authSessionGeneration = 0;
let resetPasswordToken = "";
let verificationCountdown = 0;
let verificationTimer = null;
let turnstileLoadPromise = null;
const turnstileTokens = { login: "", register: "", forgot: "" };
const turnstileWidgets = { login: null, register: null, forgot: null };
const turnstileRenderPromises = { login: null, register: null, forgot: null };

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
  setButtonLabel(ssoButton, "正在前往企业登录");
  el("enterpriseLoginHint").textContent = "请在新打开的企业认证页面完成登录。";
  window.location.href = "/api/auth/sso/start";
}

function showLoginCallbackMessage() {
  const params = new URLSearchParams(window.location.search);
  const authError = params.get("auth_error");
  if (!authError) return;
  const message = authError === "state" ? "登录状态已失效，请重新点击飞书扫码登录。" : "登录没有完成，请重新扫码。";
  // SSO 回调失败时提示挂在登录页上，需要切到登录页，否则用户停在营销首页看不到提示。
  showAuthPage("login");
  setAuthAccess("enterprise");
  el("enterpriseLoginHint").textContent = message;
  showToast(message);
  params.delete("auth_error");
  const cleanQuery = params.toString();
  const cleanUrl = `${window.location.pathname}${cleanQuery ? `?${cleanQuery}` : ""}${window.location.hash}`;
  window.history.replaceState({}, "", cleanUrl);
}

async function api(path, options = {}, requestState = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const isWriteRequest = !["GET", "HEAD", "OPTIONS"].includes(method);
  const requestCsrfToken = isWriteRequest ? authCsrfToken : "";
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (requestCsrfToken) headers["X-CSRF-Token"] = requestCsrfToken;
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers,
  });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    let code = "";
    try {
      const payload = await response.json();
      message = typeof payload.detail === "string" ? payload.detail : payload.detail?.error || payload.error || message;
      code = payload.code || payload.detail?.code || "";
    } catch {}
    if (
      isWriteRequest
      && code === "AUTH_CSRF_INVALID"
      && !requestState.csrfRetryAttempted
      && (options.body === undefined || typeof options.body === "string")
    ) {
      await recoverCsrfToken(requestCsrfToken);
      return api(path, options, { csrfRetryAttempted: true });
    }
    const error = new Error(message);
    error.status = response.status;
    error.code = code;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

async function ensureCsrfToken() {
  if (authCsrfToken) return authCsrfToken;
  if (!csrfRefreshPromise) {
    const refreshGeneration = authSessionGeneration;
    let refreshPromise;
    refreshPromise = (async () => {
      const payload = await api("/api/auth/csrf");
      const token = payload?.csrfToken || "";
      if (!token) throw new Error("页面安全凭证获取失败，请刷新后重试");
      if (authSessionGeneration === refreshGeneration) authCsrfToken = token;
      return token;
    })().finally(() => {
      if (csrfRefreshPromise === refreshPromise) csrfRefreshPromise = null;
    });
    csrfRefreshPromise = refreshPromise;
  }
  return csrfRefreshPromise;
}

async function recoverCsrfToken(invalidToken = "") {
  // A later response may report the same stale request after another request
  // has already refreshed the session. Reuse that token instead of rotating it again.
  if (authCsrfToken && authCsrfToken !== invalidToken) return authCsrfToken;
  if (!csrfRefreshPromise && authCsrfToken === invalidToken) authCsrfToken = "";
  return ensureCsrfToken();
}

function normalizeAuthUser(payload) {
  return payload?.user || payload;
}

function replaceCurrentQuery(params) {
  const query = params.toString();
  window.history.replaceState({}, "", `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`);
}

function takeResetPasswordTokenFromUrl(params = new URLSearchParams(window.location.search)) {
  const token = params.get("reset_token") || params.get("token") || "";
  const hadSensitiveParam = params.has("reset_token") || params.has("token");
  params.delete("reset_token");
  params.delete("token");
  if (hadSensitiveParam) replaceCurrentQuery(params);
  return token;
}

function clearResetPasswordToken() {
  resetPasswordToken = "";
  const params = new URLSearchParams(window.location.search);
  const hadSensitiveParam = params.has("reset_token") || params.has("token");
  params.delete("reset_token");
  params.delete("token");
  if (hadSensitiveParam) replaceCurrentQuery(params);
  el("resetPasswordInput").value = "";
  el("resetConfirmPasswordInput").value = "";
  el("resetPasswordButton").disabled = true;
}

function turnstileContainerId(mode) {
  return `${mode}Turnstile`;
}

async function loadTurnstile() {
  if (!authConfig.turnstileEnabled || !authConfig.turnstileConfigured || !authConfig.turnstileSiteKey) return null;
  if (window.turnstile) return window.turnstile;
  if (turnstileLoadPromise) return turnstileLoadPromise;
  turnstileLoadPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
    script.async = true;
    script.defer = true;
    script.onload = () => {
      if (window.turnstile) resolve(window.turnstile);
      else {
        script.remove();
        reject(new Error("安全验证组件初始化失败，请刷新后重试"));
      }
    };
    script.onerror = () => {
      script.remove();
      reject(new Error("安全验证组件加载失败，请检查网络后重试"));
    };
    document.head.appendChild(script);
  });
  return turnstileLoadPromise;
}

async function renderTurnstile(mode) {
  if (!authConfig.turnstileEnabled || !authConfig.turnstileConfigured || !authConfig.turnstileSiteKey) return;
  const container = el(turnstileContainerId(mode));
  if (!container) return;
  container.classList.remove("hidden");
  if (turnstileWidgets[mode] !== null) return;
  if (turnstileRenderPromises[mode]) return turnstileRenderPromises[mode];
  turnstileRenderPromises[mode] = (async () => {
    try {
      const widget = await loadTurnstile();
      if (!widget || turnstileWidgets[mode] !== null) return;
      turnstileWidgets[mode] = widget.render(container, {
        sitekey: authConfig.turnstileSiteKey,
        theme: "light",
        callback: (token) => { turnstileTokens[mode] = token; },
        "expired-callback": () => { turnstileTokens[mode] = ""; },
        "error-callback": () => {
          turnstileTokens[mode] = "";
          setAuthStatus("安全验证没有完成，请刷新后重试。", "error");
        },
      });
    } catch (error) {
      turnstileLoadPromise = null;
      setAuthStatus(error.message || "安全验证组件加载失败", "error");
    } finally {
      turnstileRenderPromises[mode] = null;
    }
  })();
  return turnstileRenderPromises[mode];
}

function resetTurnstile(mode) {
  turnstileTokens[mode] = "";
  if (turnstileWidgets[mode] !== null && window.turnstile) window.turnstile.reset(turnstileWidgets[mode]);
}

function requireTurnstile(mode) {
  if (!authConfig.turnstileEnabled) return true;
  if (!authConfig.turnstileConfigured || !authConfig.turnstileSiteKey) {
    setAuthStatus("安全验证尚未正确配置，请联系管理员。", "error");
    return false;
  }
  if (turnstileTokens[mode]) return true;
  setAuthStatus("请先完成安全验证。", "error");
  return false;
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

function toggleTrendGrid(gridId) {
  el(gridId)?.classList.toggle("hidden", selectedDateRange().days === 1);
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

function setButtonLabel(buttonOrId, label) {
  const button = typeof buttonOrId === "string" ? el(buttonOrId) : buttonOrId;
  if (!button) return;
  const labelNode = [...button.children].reverse().find((node) => node.matches("span:not(.app-icon)"));
  if (labelNode) {
    labelNode.textContent = label;
    return;
  }
  const textNode = [...button.childNodes].reverse().find((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
  if (textNode) {
    textNode.textContent = ` ${label}`;
    return;
  }
  button.textContent = label;
}

function setButtonLoading(buttonOrId, loading, loadingLabel = "处理中") {
  const button = typeof buttonOrId === "string" ? el(buttonOrId) : buttonOrId;
  if (!button) return;
  if (loading) {
    button.dataset.idleLabel = button.querySelector(":scope > span:last-child")?.textContent || button.textContent.trim();
    button.disabled = true;
    button.classList.add("is-loading");
    setButtonLabel(button, loadingLabel);
    return;
  }
  button.disabled = false;
  button.classList.remove("is-loading");
  setButtonLabel(button, button.dataset.idleLabel || button.textContent.trim());
  delete button.dataset.idleLabel;
}

function setAuthStatus(message = "", tone = "info") {
  const status = el("authStatus");
  if (!status) return;
  status.textContent = message;
  status.className = `auth-status${message ? ` show ${tone}` : ""}`;
  status.setAttribute("role", tone === "error" ? "alert" : "status");
  status.setAttribute("aria-live", tone === "error" ? "assertive" : "polite");
}

function fieldError(fieldId, message = "") {
  const input = el(fieldId);
  const errorNode = el(`${fieldId.replace(/Input$/, "")}Error`);
  if (input) input.setAttribute("aria-invalid", message ? "true" : "false");
  if (errorNode) errorNode.textContent = message;
}

function clearAuthErrors() {
  document.querySelectorAll("#loginForm .field-error").forEach((node) => { node.textContent = ""; });
  document.querySelectorAll("#loginForm .input[aria-invalid]").forEach((input) => input.setAttribute("aria-invalid", "false"));
  setAuthStatus();
}

function validEmail(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) && email.length <= 254;
}

function signupDomains() {
  if (!Array.isArray(authConfig.allowedSignupDomains)) return [];
  return [...new Set(authConfig.allowedSignupDomains
    .map((domain) => String(domain || "").trim().toLowerCase().replace(/^@/, ""))
    .filter(Boolean))];
}

function formatSignupDomains(domains = signupDomains()) {
  return domains.join("、");
}

function signupEmailAllowed(email) {
  const domains = signupDomains();
  if (!domains.length || !validEmail(email)) return true;
  return domains.includes(email.slice(email.lastIndexOf("@") + 1).toLowerCase());
}

function localAuthUnavailableMessage(code) {
  return {
    AUTH_PASSWORD_LOGIN_DISABLED: "邮箱密码登录暂未开放。",
    AUTH_PASSWORD_LOGIN_NOT_CONFIGURED: "邮箱登录服务尚未配置完成，暂时不可用。",
    AUTH_PASSWORD_EMAIL_NOT_CONFIGURED: "账号邮件服务尚未配置完成，密码找回暂时不可用。",
    AUTH_TURNSTILE_NOT_CONFIGURED: "安全验证尚未配置完成，邮箱登录与注册暂时不可用。",
    AUTH_DATABASE_NOT_CONFIGURED: "账号服务尚未配置完成，邮箱登录与注册暂时不可用。",
    AUTH_SIGNUP_DISABLED: "邮箱注册暂未开放。",
    AUTH_SIGNUP_NOT_CONFIGURED: "注册服务尚未配置完成，暂时无法创建账号。",
    AUTH_SIGNUP_EMAIL_NOT_CONFIGURED: "注册邮件服务尚未配置完成，暂时无法发送验证码。",
    AUTH_SIGNUP_DOMAINS_NOT_CONFIGURED: "注册邮箱范围尚未配置完成，暂时无法创建账号。",
    AUTH_SIGNUP_EMAIL_VERIFICATION_REQUIRED: "生产注册必须启用邮箱验证。",
  }[String(code || "").trim()] || "";
}

function configuredLocalAuthMessage(fallback = "") {
  return localAuthUnavailableMessage(authConfig.passwordLoginUnavailableCode)
    || String(authConfig.passwordLoginUnavailableReason || "").trim()
    || fallback;
}

function configuredSignupMessage(fallback = "") {
  return localAuthUnavailableMessage(authConfig.publicSignupUnavailableCode)
    || String(authConfig.publicSignupUnavailableReason || "").trim()
    || fallback;
}

function configuredPasswordRecoveryMessage(fallback = "") {
  return localAuthUnavailableMessage(authConfig.passwordRecoveryUnavailableCode)
    || String(authConfig.passwordRecoveryUnavailableReason || "").trim()
    || fallback;
}

function turnstileMisconfigured() {
  return Boolean(authConfig.turnstileEnabled) && (!authConfig.turnstileConfigured || !authConfig.turnstileSiteKey);
}

function passwordLoginAvailable() {
  return Boolean(authConfig.passwordLoginEnabled)
    && authConfig.passwordLoginAvailable !== false
    && authConfig.passwordLoginConfigured !== false
    && !turnstileMisconfigured();
}

function publicSignupAvailable() {
  return passwordLoginAvailable()
    && Boolean(authConfig.publicSignupEnabled)
    && authConfig.publicSignupAvailable !== false
    && authConfig.publicSignupConfigured !== false;
}

function passwordRecoveryAvailable() {
  return passwordLoginAvailable()
    && authConfig.passwordRecoveryEnabled !== false
    && authConfig.passwordRecoveryAvailable !== false
    && authConfig.passwordRecoveryConfigured !== false;
}

function setAuthAvailabilityNotice(message = "", tone = "info") {
  const notice = el("authAvailabilityNotice");
  if (!notice) return;
  notice.textContent = message;
  notice.className = `auth-status${message ? ` show ${tone}` : ""}`;
  notice.setAttribute("role", tone === "error" ? "alert" : "status");
  notice.setAttribute("aria-live", tone === "error" ? "assertive" : "polite");
}

function updateSignupPolicyCopy() {
  const domains = signupDomains();
  const domainCopy = domains.length ? `支持 ${formatSignupDomains(domains)} 邮箱注册。` : "";
  const note = el("registerPolicyNote");
  if (note) note.textContent = `${domainCopy}账号创建后，充值或由管理员开通后方可使用模型和额度。`;
}

function setGlobalPage(page) {
  document.querySelectorAll("[data-global-page]").forEach((item) => {
    const isActive = item.dataset.globalPage === page;
    item.classList.toggle("active", isActive);
    if (isActive) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
}

function setAuthAccess(access) {
  const personalAvailable = passwordLoginAvailable();
  const enterpriseAvailable = Boolean(authConfig.oidcConfigured);
  if (access === "personal" && personalAvailable) authAccess = "personal";
  else if (access === "enterprise" && enterpriseAvailable) authAccess = "enterprise";
  else if (personalAvailable) authAccess = "personal";
  else if (enterpriseAvailable) authAccess = "enterprise";
  else authAccess = "none";
  const personal = authAccess === "personal";
  const enterprise = authAccess === "enterprise";
  el("personalAuthPanel").classList.toggle("hidden", !personal);
  el("personalAuthPanel").setAttribute("aria-hidden", String(!personal));
  el("enterpriseAuthPanel").classList.toggle("hidden", !enterprise);
  el("enterpriseAuthPanel").setAttribute("aria-hidden", String(!enterprise));
  document.querySelectorAll("[data-auth-access]").forEach((button) => {
    const selected = button.dataset.authAccess === authAccess;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  if (!personal && authMode === "reset") {
    clearResetPasswordToken();
    setAuthMode("login", { focus: false });
  }
  if (enterprise) setAuthStatus();
  else if (["login", "register", "forgot"].includes(authMode)) renderTurnstile(authMode);
}

function authScreenForMode(mode) {
  return {
    login: "passwordLoginScreen",
    register: "registerScreen",
    forgot: "forgotPasswordScreen",
    reset: "resetPasswordScreen",
  }[mode] || "passwordLoginScreen";
}

function syncVerificationRequired() {
  // Every auth screen shares one <form>, so native validation also inspects the
  // fields of whichever screens are hidden. A required control inside a hidden
  // screen can never be focused, and the browser then aborts submission with
  // "An invalid form control is not focusable" without firing a submit event --
  // which silently breaks login and password reset. Mark the code required only
  // while the register screen is the active one.
  const field = el("registerVerificationInput");
  if (field) field.required = Boolean(authConfig.emailVerificationRequired) && authMode === "register";
}

function setAuthMode(mode, { focus = true } = {}) {
  let requestedMode = ["login", "register", "forgot", "reset"].includes(mode) ? mode : "login";
  if (requestedMode === "register" && !publicSignupAvailable()) requestedMode = "login";
  if (requestedMode === "forgot" && !passwordRecoveryAvailable()) requestedMode = "login";
  if (requestedMode === "reset" && !passwordLoginAvailable()) requestedMode = "login";
  if (authMode === "reset" && requestedMode !== "reset") clearResetPasswordToken();
  authMode = requestedMode;
  clearAuthErrors();
  ["login", "register", "forgot", "reset"].forEach((candidate) => {
    const active = candidate === authMode;
    const screen = el(authScreenForMode(candidate));
    screen.classList.toggle("hidden", !active);
    screen.setAttribute("aria-hidden", String(!active));
  });
  el("authFlowSwitch").classList.toggle("hidden", ["forgot", "reset"].includes(authMode));
  syncVerificationRequired();
  document.querySelectorAll("[data-auth-mode]").forEach((button) => {
    const selected = button.dataset.authMode === authMode;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  if (["login", "register", "forgot"].includes(authMode)) renderTurnstile(authMode);
  if (!focus) return;
  const firstField = el(authScreenForMode(authMode)).querySelector("input");
  requestAnimationFrame(() => firstField?.focus());
}

function updateAuthAvailability() {
  const passwordEnabled = passwordLoginAvailable();
  const signupEnabled = publicSignupAvailable();
  const recoveryEnabled = passwordRecoveryAvailable();
  const ssoEnabled = Boolean(authConfig.oidcConfigured);
  const securityUnavailable = turnstileMisconfigured();
  el("personalAccessTab").classList.toggle("hidden", !passwordEnabled);
  el("personalAccessTab").disabled = !passwordEnabled;
  el("personalAccessTab").setAttribute("aria-hidden", String(!passwordEnabled));
  el("registerFlowTab").classList.toggle("hidden", !signupEnabled);
  el("registerVerificationField").classList.toggle("hidden", !authConfig.emailVerificationRequired);
  syncVerificationRequired();
  el("authFlowSwitch").style.gridTemplateColumns = signupEnabled ? "repeat(2, minmax(0, 1fr))" : "1fr";
  if (!signupEnabled && authMode === "register") setAuthMode("login", { focus: false });
  el("enterpriseAccessTab").classList.toggle("hidden", !ssoEnabled);
  el("enterpriseAccessTab").disabled = !ssoEnabled;
  el("enterpriseAccessTab").setAttribute("aria-hidden", String(!ssoEnabled));
  el("authAccessSwitch").classList.toggle("hidden", !(passwordEnabled && ssoEnabled));
  el("devLoginArea").classList.toggle("hidden", !authConfig.devLoginEnabled);

  document.querySelectorAll("#personalAuthPanel input, #personalAuthPanel button").forEach((control) => {
    control.disabled = !passwordEnabled;
  });
  document.querySelectorAll("#registerScreen input, #registerScreen button").forEach((control) => {
    control.disabled = !signupEnabled;
  });
  el("registerFlowTab").disabled = !signupEnabled;
  el("registerButton").disabled = !signupEnabled;
  el("sendRegisterCodeButton").disabled = !signupEnabled;
  el("passwordLoginButton").disabled = !passwordEnabled;
  el("forgotPasswordButton").classList.toggle("hidden", !recoveryEnabled);
  el("forgotPasswordButton").disabled = !recoveryEnabled;
  el("forgotPasswordButton").setAttribute("aria-hidden", String(!recoveryEnabled));
  el("forgotSubmitButton").disabled = !recoveryEnabled;
  el("resetPasswordButton").disabled = !passwordEnabled || !resetPasswordToken;

  if (!passwordEnabled && resetPasswordToken) clearResetPasswordToken();
  setAuthAccess(resetPasswordToken && passwordEnabled ? "personal" : (passwordEnabled ? "personal" : "enterprise"));
  setAuthMode(resetPasswordToken && passwordEnabled ? "reset" : (authMode === "reset" ? "login" : authMode), { focus: false });

  if (passwordEnabled && ssoEnabled) el("guestAuthDescription").textContent = "使用邮箱账号或企业账号进入控制台。";
  else if (passwordEnabled) el("guestAuthDescription").textContent = "使用邮箱账号进入控制台。";
  else if (ssoEnabled) el("guestAuthDescription").textContent = "使用企业统一认证进入控制台。";
  else if (authConfig.devLoginEnabled) el("guestAuthDescription").textContent = "使用下方开发环境邮箱入口登录。";
  else el("guestAuthDescription").textContent = "当前没有可用的登录方式，请联系管理员。";

  updateSignupPolicyCopy();
  const passwordReason = configuredLocalAuthMessage();
  const signupReason = configuredSignupMessage();
  const recoveryReason = configuredPasswordRecoveryMessage();
  if (passwordReason && !passwordEnabled) {
    setAuthAvailabilityNotice(passwordReason, "error");
  } else if (securityUnavailable && authConfig.passwordLoginEnabled) {
    setAuthAvailabilityNotice("安全验证尚未正确配置，邮箱登录与注册暂时不可用，请联系管理员。", "error");
  } else if (passwordEnabled && !signupEnabled && !recoveryEnabled) {
    setAuthAvailabilityNotice(`邮箱登录仍可用。${signupReason || "邮箱注册暂未开放。"}${recoveryReason || "密码找回暂不可用。"}`, "info");
  } else if (passwordEnabled && !signupEnabled) {
    setAuthAvailabilityNotice(`邮箱登录和密码找回仍可用。${signupReason || "邮箱注册暂未开放。"}`, "info");
  } else if (passwordEnabled && !recoveryEnabled) {
    setAuthAvailabilityNotice(`邮箱登录仍可用。${recoveryReason || "密码找回暂不可用。"}`, "info");
  } else if (!passwordEnabled && !ssoEnabled && !authConfig.devLoginEnabled) {
    setAuthAvailabilityNotice("当前没有可用的登录方式，请联系管理员。", "error");
  } else {
    setAuthAvailabilityNotice();
  }
}

function setVerificationCountdown(seconds) {
  verificationCountdown = Math.max(0, Number(seconds) || 0);
  if (verificationTimer) window.clearInterval(verificationTimer);
  const button = el("sendRegisterCodeButton");
  button.classList.remove("is-loading");
  delete button.dataset.idleLabel;
  const update = () => {
    const active = verificationCountdown > 0;
    button.disabled = active || !publicSignupAvailable();
    button.textContent = active ? `${verificationCountdown} 秒后重发` : "重新发送";
    if (!active && verificationTimer) {
      window.clearInterval(verificationTimer);
      verificationTimer = null;
    }
    verificationCountdown -= 1;
  };
  update();
  if (verificationCountdown > 0) verificationTimer = window.setInterval(update, 1000);
}

function authFieldValues() {
  return {
    loginEmail: el("loginEmailInput").value.trim().toLowerCase(),
    loginPassword: el("loginPasswordInput").value,
    registerName: el("registerNameInput").value.trim(),
    registerEmail: el("registerEmailInput").value.trim().toLowerCase(),
    verificationCode: el("registerVerificationInput").value.trim(),
    registerPassword: el("registerPasswordInput").value,
    registerConfirmPassword: el("registerConfirmPasswordInput").value,
    forgotEmail: el("forgotEmailInput").value.trim().toLowerCase(),
    resetPassword: el("resetPasswordInput").value,
    resetConfirmPassword: el("resetConfirmPasswordInput").value,
  };
}

function validateAuthMode(mode) {
  clearAuthErrors();
  const values = authFieldValues();
  let valid = true;
  const reject = (id, message) => { fieldError(id, message); valid = false; };
  if (mode === "login") {
    if (!validEmail(values.loginEmail)) reject("loginEmailInput", "请输入有效的邮箱地址");
    if (values.loginPassword.length < 8) reject("loginPasswordInput", "密码至少需要 8 个字符");
  }
  if (mode === "register") {
    if (values.registerName.length < 2) reject("registerNameInput", "请输入至少 2 个字符的姓名");
    if (!validEmail(values.registerEmail)) reject("registerEmailInput", "请输入有效的邮箱地址");
    else if (!signupEmailAllowed(values.registerEmail)) reject("registerEmailInput", `当前仅支持 ${formatSignupDomains()} 邮箱注册`);
    if (authConfig.emailVerificationRequired && !/^\d{6}$/.test(values.verificationCode)) reject("registerVerificationInput", "请输入邮件中的 6 位验证码");
    if (values.registerPassword.length < 8) reject("registerPasswordInput", "密码至少需要 8 个字符");
    if (values.registerPassword !== values.registerConfirmPassword) reject("registerConfirmPasswordInput", "两次输入的密码不一致");
  }
  if (mode === "forgot" && !validEmail(values.forgotEmail)) reject("forgotEmailInput", "请输入有效的邮箱地址");
  if (mode === "reset") {
    if (!resetPasswordToken) {
      setAuthStatus("重置链接缺少有效凭据，请重新申请。", "error");
      valid = false;
    }
    if (values.resetPassword.length < 8) reject("resetPasswordInput", "密码至少需要 8 个字符");
    if (values.resetPassword !== values.resetConfirmPassword) reject("resetConfirmPasswordInput", "两次输入的密码不一致");
  }
  return valid;
}

function accountAccessCopy(user) {
  const organizationAccessStatus = String(user?.organizationAccessStatus || "");
  if (user?.organizationDemoEnabled && user?.isKnownDemoCustomerIdentity) {
    if (organizationAccessStatus === "invited") {
      return {
        title: "企业邀请等待启用",
        description: "你的企业演示邀请尚未启用，暂时不能查看用量或使用企业资源。",
        retry: false,
      };
    }
    if (organizationAccessStatus === "suspended") {
      return {
        title: "企业访问已暂停",
        description: "你的企业演示访问已暂停，请联系企业管理员或平台运营人员确认后续安排。",
        retry: false,
      };
    }
    if (["archived", "organization_suspended"].includes(organizationAccessStatus)) {
      return {
        title: "所属客户企业暂不可用",
        description: "所属客户企业已归档或暂停，当前不能访问企业演示数据。",
        retry: false,
      };
    }
  }
  const accountStatus = user?.accountStatus || "provisioned";
  const entitlementStatus = user?.entitlementStatus || "active";
  if (["provisioning", "pending", "provisioning_failed"].includes(accountStatus)) {
    return accountStatus === "provisioning_failed"
      ? { title: "账号开通暂未完成", description: "账号同步遇到问题。你可以稍后重新检查，或联系管理员协助处理。", retry: true }
      : { title: "账号正在开通", description: "我们正在同步你的个人用量空间，通常只需要一点时间。", retry: true };
  }
  if (["inactive", "expired", "suspended"].includes(entitlementStatus)) {
    if (entitlementStatus === "inactive") {
      return {
        title: "账号已创建，等待开通",
        description: "充值或由管理员开通后方可使用模型和额度。",
        retry: false,
        topup: true,
      };
    }
    return {
      title: entitlementStatus === "suspended" ? "访问权限已暂停" : "暂未获得使用权限",
      description: entitlementStatus === "expired" ? "当前访问权限已到期，请联系管理员续期。" : "当前访问权限已暂停，请联系管理员确认后续安排。",
      retry: false,
    };
  }
  return null;
}

function renderAccountAccessState() {
  const state = accountAccessCopy(currentUser);
  const dashboard = el("dashboardView");
  const stateNode = el("accountAccessState");
  dashboard.classList.toggle("account-limited", Boolean(state));
  stateNode.classList.toggle("hidden", !state);
  if (!state) return;
  el("accountAccessTitle").textContent = state.title;
  el("accountAccessDescription").textContent = state.description;
  el("accountAccessRetryButton").classList.toggle("hidden", !state.retry);
  // 只有充值真的开放时才给出这个入口，否则等于把用户引到死路。
  el("accountAccessTopupButton").classList.toggle("hidden", !(state.topup && billingAvailable));
}

function updateHomeCard() {
  const isLoggedIn = Boolean(currentUser);
  el("authenticatedHome").classList.toggle("hidden", !isLoggedIn);
  el("authGuestContent").classList.toggle("hidden", isLoggedIn);
  if (isLoggedIn) {
    el("loginTitle").textContent = `欢迎回来，${currentUser.name || currentUser.email}`;
    el("loginDescription").textContent = accountAccessCopy(currentUser)
      ? "账号已完成认证。进入控制台可查看当前开通状态。"
      : "你已完成账号认证，可以继续进入控制台查看个人 AI 用量。";
    el("loginHint").textContent = `当前登录账号：${currentUser.email}`;
    return;
  }
  isSsoRedirecting = false;
  el("ssoButton").disabled = false;
  el("devLoginButton").disabled = false;
  setButtonLabel("ssoButton", authConfig.providerName || "飞书扫码登录");
  el("enterpriseLoginHint").textContent = "本页面不会保存企业登录凭据。";
  updateAuthAvailability();
}

// 首页顶栏按登录态切换：未登录显示登录/注册入口，已登录显示账号与进入控制台。
function updateLandingAuthState() {
  const isLoggedIn = Boolean(currentUser);
  el("landingGuestActions").classList.toggle("hidden", isLoggedIn);
  el("landingUserActions").classList.toggle("hidden", !isLoggedIn);
  el("landingUserEmail").textContent = isLoggedIn ? currentUser.email || "" : "";
  el("landingPrimaryCta").textContent = isLoggedIn ? "进入控制台" : "立即开始使用";
  el("landingGuestRegisterButton").classList.toggle("hidden", !publicSignupAvailable());
}

function showLanding() {
  if (currentView === "keys") clearRevealedKeys();
  el("authLoadingView").classList.add("hidden");
  el("appView").classList.add("hidden");
  el("loginView").classList.add("hidden");
  el("landingView").classList.remove("hidden");
  updateLandingAuthState();
  setGlobalPage("home");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showHome() {
  showLanding();
}

// 打开独立登录页；mode 决定落在登录还是注册 tab。
function showAuthPage(mode = "login") {
  el("authLoadingView").classList.add("hidden");
  el("appView").classList.add("hidden");
  el("landingView").classList.add("hidden");
  el("loginView").classList.remove("hidden");
  updateHomeCard();
  setGlobalPage("");
  if (!currentUser) {
    setAuthAccess(passwordLoginAvailable() ? "personal" : "enterprise");
    setAuthMode(mode, { focus: false });
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
  const firstLoginControl = passwordLoginAvailable()
    ? el(mode === "register" ? "registerEmailInput" : "loginEmailInput")
    : authConfig.oidcConfigured
      ? el("ssoButton")
      : authConfig.devLoginEnabled
        ? el("emailInput")
        : null;
  firstLoginControl?.focus({ preventScroll: true });
}

function promptForLogin() {
  showAuthPage("login");
  showToast("请先登录后访问控制台和模型广场");
}

function showAuthenticatedPage(page) {
  if (!currentUser) {
    promptForLogin();
    return;
  }
  el("authLoadingView").classList.add("hidden");
  el("landingView").classList.add("hidden");
  el("loginView").classList.add("hidden");
  el("appView").classList.remove("hidden");
  switchView(page === "models" ? "models" : "dashboard");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function navigateGlobalPage(page) {
  if (page === "home") {
    showLanding();
    return;
  }
  showAuthenticatedPage(page);
}

function setDailyMiniValue(id, value, isTokenValue = false) {
  const node = el(id);
  if (!node) return;
  node.textContent = value;
  node.classList.toggle("daily-token-value", isTokenValue);
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

function normalizeModelKey(model) {
  // 与后端 normalize_model_display_name 保持一致：
  // 去掉路由、账号别名和供应商前缀，使同一模型聚合为一条。
  let name = String(model ?? "").trim();
  if (!name) return "";
  // 上游路由可能有多层 provider/ 前缀，模型名始终取最后一段。
  name = name.split("/").pop().trim() || name;
  name = name.replace(/^[A-Za-z][A-Za-z0-9]*-acct-\d+-/i, "");
  name = name.replace(/^[A-Za-z][A-Za-z0-9]*\./i, "");
  return name;
}

function displayModelName(model) {
  const normalized = normalizeModelKey(model);
  if (!normalized) return "未知模型";
  return normalized;
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
  const matched = departmentRankings.find((item) => item.departmentKey === selectedDepartment || item.departmentId === selectedDepartment || item.departmentName === selectedDepartment);
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
  const customerName = organizationUsageScope()?.kind === "platformCustomer" ? organizationUsageScope()?.name : "";
  const scopedName = customerName ? `${customerName} · ${scopeName}` : scopeName;
  setText("adminTrendTitle", `${scopedName}每日 Token 趋势`);
  setText("adminTrendDesc", `按日期汇总${scopedName} Prompt 与 Completion Token。`);
  setText("adminSpendTitle", `${scopedName}每日金额消费趋势`);
  setText("adminSpendDesc", `按日期汇总${scopedName}预估消费金额。`);
  setText("adminSourceTitle", `${scopedName}用量占比`);
  setText("adminSourceDesc", selectedAdminEmployee
    ? `按${scopedName} Codex、Claude Code 与其他来源拆分用量。`
    : `按${scopedName} Codex、Claude Code 与其他来源拆分用量。`);
}

function renderAdminMetrics(data) {
  const totalData = adminSummaryData.length ? adminSummaryData : data;
  const label = rangeLabel();
  const source = sourceText();
  const scope = organizationUsageScope();
  const isOrganizationScope = ["platformCustomer", "currentOrganization"].includes(scope?.kind);
  const scopePrefix = scope?.kind === "platformCustomer" ? "客户企业" : "企业范围";
  renderDailyOverview({
    prefix: "admin",
    data: totalData,
    title: isOrganizationScope ? `${scopePrefix} · ${scope.name}` : "所选范围 · 管理员视图",
    totalLabel: isOrganizationScope ? `${scope.name} 全员 Token` : "所选范围全员 Token",
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
  const scope = organizationUsageScope();
  const isOrganizationScope = ["platformCustomer", "currentOrganization"].includes(scope?.kind);
  const scopePrefix = scope?.kind === "platformCustomer" ? "客户企业" : "企业范围";
  renderDailyOverview({
    prefix: "admin",
    data,
    title: isOrganizationScope ? `${scopePrefix} · ${scope.name}` : "所选范围 · 员工视图",
    totalLabel: isOrganizationScope ? `${scope.name} 成员 Token` : "所选范围员工 Token",
    sideLabel: "当前员工",
    sideValue: 1,
    sideSub: employee?.employeeEmail || employee?.employeeId || selectedAdminEmployee,
  });
  el("adminAvgSpendWrap")?.classList.toggle("hidden", isSingleDay);
  el("adminDailyOverview")?.classList.toggle("personal-single-day", isSingleDay);
  setText("adminActiveLabel", "日均 Token");
  setDailyMiniValue("adminActiveUsers", formatTokens(Math.round(sum(data, "totalTokens") / (days || 1))), true);
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
  const scope = organizationUsageScope();
  const isOrganizationScope = ["platformCustomer", "currentOrganization"].includes(scope?.kind);
  const scopePrefix = scope?.kind === "platformCustomer" ? "客户企业" : "企业范围";
  renderDailyOverview({
    prefix: "department",
    data,
    title: isOrganizationScope ? `${scopePrefix} · ${scope.name} · ${scopeLabel}` : `所选范围 · ${scopeLabel}`,
    totalLabel: isOrganizationScope ? `${scope.name} Token` : "所选范围 Token",
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
  setDailyMiniValue("teamActiveUsers", fmt.format(activeMembers));
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
  setDailyMiniValue("teamActiveUsers", formatTokens(Math.round(sum(data, "totalTokens") / (days || 1))), true);
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
  // 按规范化后的模型名称聚合，合并带不同供应商/账号前缀的同一模型。
  const grouped = {};
  data.forEach((item) => {
    const key = normalizeModelKey(item.model) || "未知模型";
    (grouped[key] = grouped[key] || []).push(item);
  });
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

function renderDepartmentBarsTo(containerId, departments) {
  const container = el(containerId);
  if (!container) return;
  const sorted = departments
    .filter((d) => d.totalTokens > 0)
    .sort((a, b) => b.totalTokens - a.totalTokens)
    .slice(0, 10);
  const max = Math.max(1, ...sorted.map((d) => d.totalTokens));
  container.innerHTML = sorted.length
    ? sorted
        .map((dept) => `<div class="bar-row"><strong>${escapeHtml(dept.departmentName)}</strong><div class="bar-track"><div class="bar-fill" style="width:${Math.max(3, (dept.totalTokens / max) * 100)}%"></div></div><span class="num">${formatTokens(dept.totalTokens)}</span></div>`)
        .join("")
    : `<div class="model-empty">当前筛选范围暂无部门用量</div>`;
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

// 排行表默认按 Token 降序；用户可点击表头在四个数值列上切换升降序。
const DEFAULT_RANKING_SORT = { key: "totalTokens", direction: "desc" };
const RANKING_SORT_KEYS = ["requestCount", "totalTokens", "spend", "successRate"];
const RANKING_SORT_TIP = "默认按 Token 从高到低排序，点击请求数 / Token / 金额 / 成功率表头可切换升降序";
const rankingSortState = new Map();

function rankingSort(tableId) {
  return rankingSortState.get(tableId) || DEFAULT_RANKING_SORT;
}

function rankingSortValue(item, key) {
  if (key === "successRate") {
    const requests = Number(item.requestCount || 0);
    return requests ? Number(item.successCount || 0) / requests : 0;
  }
  return Number(item[key] || 0);
}

function rankingComparator(tableId, nameOf) {
  const { key, direction } = rankingSort(tableId);
  const sign = direction === "asc" ? 1 : -1;
  return (a, b) => {
    const primary = rankingSortValue(a, key) - rankingSortValue(b, key);
    if (primary) return primary * sign;
    // 主排序列相同时沿用默认降序链，保证名次稳定。
    for (const fallback of ["totalTokens", "spend", "requestCount"]) {
      if (fallback === key) continue;
      const diff = Number(b[fallback] || 0) - Number(a[fallback] || 0);
      if (diff) return diff;
    }
    return nameOf(a).localeCompare(nameOf(b), "zh-CN");
  };
}

function employeeRankingName(item) {
  return item.employeeName || item.employeeEmail || item.employeeId || "";
}

function departmentRankingName(item) {
  return item.departmentName || item.departmentId || "";
}

function sortedRankingRows(tableId, items, nameOf) {
  return items.slice().sort(rankingComparator(tableId, nameOf));
}

function rankingHead(tableId) {
  return el(tableId)?.closest("table")?.querySelector("thead") || null;
}

function updateRankingSortIndicators(tableId) {
  const head = rankingHead(tableId);
  if (!head) return;
  const { key, direction } = rankingSort(tableId);
  head.querySelectorAll("th.sortable").forEach((th) => {
    const active = th.dataset.sortKey === key;
    th.setAttribute("aria-sort", active ? (direction === "asc" ? "ascending" : "descending") : "none");
    const arrow = th.querySelector(".sort-arrow");
    if (arrow) arrow.textContent = active ? (direction === "asc" ? "↑" : "↓") : "↕";
  });
}

function setupRankingSorting(tableId, rerender) {
  const head = rankingHead(tableId);
  if (!head) return;
  head.addEventListener("click", (event) => {
    const th = event.target.closest("th.sortable");
    const key = th?.dataset.sortKey;
    if (!key || !RANKING_SORT_KEYS.includes(key)) return;
    const current = rankingSort(tableId);
    // 换列时先给降序（排行更常看高值），同列再次点击才切升序。
    const direction = current.key === key && current.direction === "desc" ? "asc" : "desc";
    rankingSortState.set(tableId, { key, direction });
    rerender();
  });
  updateRankingSortIndicators(tableId);
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
  return item.departmentKey || item.departmentId || item.departmentName || "";
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
  const sorted = sortedRankingRows(tableId, employees, employeeRankingName);
  const isTeamTable = tableId === "teamUserTable";
  updateRankingSortIndicators(tableId);
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
              <td>${displaySource(item.primarySource || "其他")}</td>
              ${isTeamTable ? `<td>${item.teamRole === "admin" ? "负责人" : "成员"}</td>` : ""}
              <td class="num">${fmt.format(requests)}</td>
              <td class="num"><strong>${formatTokens(item.totalTokens || 0)}</strong></td>
              <td class="num">${money.format(item.spend || 0)}</td>
              <td class="num">${successRate}%</td>
              <td><span class="chip ${item.bindStatus === "未绑定邮箱" ? "rose" : "blue"}">${item.bindStatus || "已绑定邮箱"}</span></td>
            </tr>
          `;
        })
        .join("")
    : `<tr><td colspan="${isTeamTable ? 9 : 8}" style="text-align:center;color:var(--muted);padding:26px">${emptyText}</td></tr>`;
}

function renderDepartmentRanking(tableId, countId, departments, emptyText) {
  const sorted = sortedRankingRows(tableId, departments, departmentRankingName);
  updateRankingSortIndicators(tableId);
  el(countId).textContent = `${sorted.length} 个部门`;
  el(tableId).innerHTML = sorted.length
    ? sorted
        .map((item) => {
          const requests = Number(item.requestCount || 0);
          const successRate = requests ? Math.round((Number(item.successCount || 0) / requests) * 1000) / 10 : 0;
          return `
            <tr class="admin-employee-row" data-department="${escapeHtml(departmentOptionKey(item))}">
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
    el("departmentRankingDesc").textContent = `当前展示 ${scopeLabel} 内员工用量，点击表头可切换排序。`;
    renderEmployeeRanking("departmentUserTable", "departmentUserCount", departmentEmployees, "当前筛选范围暂无部门员工用量");
  } else {
    el("departmentRankingTitle").textContent = "部门用量排行";
    el("departmentRankingDesc").textContent = "点击部门查看该部门用量看板和员工排行。";
    renderDepartmentRanking("departmentUserTable", "departmentUserCount", departmentRankings, "当前筛选范围暂无部门用量");
  }
}

function renderTeamUsers() {
  if (isTeamRankingLoading) {
    renderTableSkeleton("teamUserTable", "teamUserCount", 9);
  } else {
    setText("teamLimitHint", teamRankingError || teamRankingHint || "按当前筛选范围统计");
    renderEmployeeRanking("teamUserTable", "teamUserCount", teamEmployees, teamRankingError || "当前团队暂无成员用量");
  }
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

function applyTeamUsagePayload(payload, cacheKey = "") {
  teamUsageData = Array.isArray(payload.rows) ? payload.rows : [];
  teamSummaryData = Array.isArray(payload.summaryRows) ? payload.summaryRows : teamUsageData;
  teamEmployees = Array.isArray(payload.employees) ? payload.employees : [];
  teamInfo = payload.team || currentUser?.team || teamInfo;
  teamDataFreshness = payload.dataFreshness || null;
  lastTeamUsageCacheHit = Boolean(payload.cache?.hit);
  teamRankingError = "";
  teamRankingHint = "";
  if (cacheKey) teamUsagePayloadCache.set(cacheKey, payload);
}

function setTeamRankingHint(payload) {
  teamRankingHint = payload.truncated
    ? "成员排行按团队成员账号用量汇总，当前数据读取达到上限，排行可能不完整"
    : "成员排行只统计当前团队归属用量，包含零用量成员";
  setText("teamLimitHint", teamRankingHint);
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
  toggleTrendGrid("personalTrendGrid");
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
  setDailyMiniValue("adminActiveUsers", "--", Boolean(selectedAdminEmployee));
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
  toggleTrendGrid("adminTrendGrid");
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
    ? `当前展示 ${scopeLabel} 内员工用量，点击表头可切换排序。`
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
  if (selectedDepartment) toggleTrendGrid("departmentTrendGrid");
  renderDonutSkeleton("departmentDonutTotal", "departmentSourceLegend");
  renderBarsSkeleton("departmentModelBars");
  renderBarsSkeleton("departmentBars");
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
  setDailyMiniValue("teamActiveUsers", "--", Boolean(selectedTeamEmployee));
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
  toggleTrendGrid("teamTrendGrid");
  renderDonutSkeleton("teamDonutTotal", "teamSourceLegend");
  renderBarsSkeleton("teamModelBars");
  if (selectedTeamEmployee) {
    if (isTeamRankingLoading) {
      renderTableSkeleton("teamUserTable", "teamUserCount", 8);
    } else {
      setText("teamLimitHint", teamRankingError || teamRankingHint || "按当前筛选范围统计");
      renderEmployeeRanking("teamUserTable", "teamUserCount", teamEmployees, teamRankingError || "当前团队暂无成员用量");
    }
    renderTableSkeleton("teamMemberUsageTable", "teamMemberTableCount", 8);
  } else {
    renderTableSkeleton("teamUserTable", "teamUserCount", 8);
  }
}

function renderPersonal() {
  if (isDashboardLoading) {
    renderPersonalLoading();
    return;
  }
  toggleTrendGrid("personalTrendGrid");
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
  toggleTrendGrid("adminTrendGrid");
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

  if (selectedDepartment) toggleTrendGrid("departmentTrendGrid");

  const barsPanel = el("departmentBars")?.closest(".panel");

  if (selectedDepartment) {
    renderDepartmentMetrics(totalData);
    renderTrendTo("departmentTrendChart", totalData);
    renderSpendTrendTo("departmentSpendChart", totalData);
    renderDonutTo("departmentSourceDonut", "departmentDonutTotal", "departmentSourceLegend", departmentUsageData);
    renderModelBarsTo("departmentModelBars", departmentUsageData);
    barsPanel?.classList.add("hidden");
  } else {
    renderDepartmentBarsTo("departmentBars", departmentRankings);
    const count = departmentRankings.filter((d) => d.totalTokens > 0).length;
    el("departmentBarsCount").textContent = `${count} 个部门`;
    barsPanel?.classList.remove("hidden");
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
  if (isTeamMemberLoading || (isTeamLoading && !teamUsageData.length)) {
    renderTeamLoading();
    return;
  }
  renderTeamDetailCard();
  toggleTrendGrid("teamTrendGrid");
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
  renderAccountAccessState();
  if (accountAccessCopy(currentUser)) return;
  renderPersonal();
  if (canViewAdminUsage()) renderAdmin();
  if (canViewDepartmentUsage()) renderDepartment();
  if (currentUser?.isTeamLeader) renderTeam();
  if (customerOrganizationsAvailable()) renderCustomerOrganizations();
  if (organizationCanView()) renderOrganization();
}

const VENDOR_ICON_KEYS = new Set(["openai", "anthropic", "google", "deepseek", "zhipu", "moonshot", "qwen", "minimax", "baai", "other"]);

function modelFamilyLabel(model) {
  return model.familyLabel || model.provider || "其他";
}

// 卡片标题用后端脱敏后的展示名，复制按钮必须给上游原始模型名，否则复制到的
// 名字调不通。老后端没有 displayName 字段时退回原始名。
function modelDisplayName(model) {
  return model.displayName || model.modelName || "";
}

function vendorIconMarkup(model, extraClass = "") {
  const key = VENDOR_ICON_KEYS.has(model.familyKey) ? model.familyKey : "other";
  return `<span class="vendor-icon vendor-${key}${extraClass ? ` ${extraClass}` : ""}" aria-hidden="true">${icon(`vendor-${key}`)}</span>`;
}

// 后端已把每 token 单价换算成每百万 Token 单价；无价格的档位不展示。
function formatPricePerMillion(value) {
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount <= 0) return "";
  return `$${amount.toFixed(4)} / 1M Tokens`;
}

function modelContextText(model) {
  const tokens = Number(model.contextWindow);
  if (!Number.isFinite(tokens) || tokens <= 0) return "未标注";
  if (tokens >= 1000) return `${Math.round(tokens / 1000)}K`;
  return fmt.format(tokens);
}

function modelPriceRows(model) {
  return [
    ["输入价格", model.inputPricePerMillion],
    ["补全价格", model.outputPricePerMillion],
    ["缓存读取", model.cacheReadPricePerMillion],
    ["缓存写入", model.cacheWritePricePerMillion],
  ]
    .map(([label, value]) => [label, formatPricePerMillion(value)])
    .filter(([, text]) => text);
}

function filterOptionsMarkup(values, allLabel, selected) {
  const options = [`<option value="all">${escapeHtml(allLabel)}</option>`];
  values.forEach(([value, count]) => {
    options.push(`<option value="${escapeHtml(value)}"${value === selected ? " selected" : ""}>${escapeHtml(value)}（${fmt.format(count)}）</option>`);
  });
  return options.join("");
}

function countedValues(items, getter) {
  const counts = new Map();
  items.forEach((item) => {
    const value = getter(item);
    if (!value) return;
    counts.set(value, (counts.get(value) || 0) + 1);
  });
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "zh-CN"));
}

function setupModelFilters() {
  const providers = countedValues(modelCatalog, (item) => modelFamilyLabel(item));
  const billingTypes = countedValues(modelCatalog, (item) => item.billingType);
  const currentProvider = el("providerFilter").value || "all";
  const currentBilling = el("billingFilter").value || "all";
  const keepProvider = providers.some(([value]) => value === currentProvider) ? currentProvider : "all";
  const keepBilling = billingTypes.some(([value]) => value === currentBilling) ? currentBilling : "all";
  el("providerFilter").innerHTML = filterOptionsMarkup(providers, "全部厂商", keepProvider);
  el("billingFilter").innerHTML = filterOptionsMarkup(billingTypes, "全部计费类型", keepBilling);
}

function filteredModels() {
  const keyword = el("modelSearch").value.trim().toLowerCase();
  const provider = el("providerFilter").value;
  const billingType = el("billingFilter").value;
  return modelCatalog.filter((model) => {
    const matchesKeyword =
      !keyword ||
      modelDisplayName(model).toLowerCase().includes(keyword) ||
      String(model.modelName || "").toLowerCase().includes(keyword) ||
      modelFamilyLabel(model).toLowerCase().includes(keyword);
    const matchesProvider = provider === "all" || modelFamilyLabel(model) === provider;
    const matchesBilling = billingType === "all" || model.billingType === billingType;
    return matchesKeyword && matchesProvider && matchesBilling;
  });
}

function setModelViewMode(mode) {
  modelViewMode = mode === "table" ? "table" : "card";
  document.querySelectorAll("[data-model-view]").forEach((button) => {
    const isActive = button.dataset.modelView === modelViewMode;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-pressed", String(isActive));
  });
  renderModels();
}

function renderModelCards(models) {
  el("modelGrid").innerHTML = models
    .map((model) => {
      const name = escapeHtml(modelDisplayName(model));
      const copyName = escapeHtml(model.modelName);
      const priceRows = modelPriceRows(model);
      const priceMarkup = priceRows.length
        ? priceRows.map(([label, text]) => `<div class="model-price-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(text)}</strong></div>`).join("")
        : `<div class="model-price-row"><span>价格</span><strong>未标注</strong></div>`;
      return `
        <article class="model-card">
          <div class="model-card-head">
            <div class="model-identity">
              ${vendorIconMarkup(model)}
              <div>
                <h3 class="model-name">${name}</h3>
                <div class="provider">${escapeHtml(modelFamilyLabel(model))}</div>
              </div>
            </div>
            <button class="model-copy-btn" type="button" data-copy-model="${copyName}" title="复制模型名称" aria-label="复制模型名称 ${name}">${icon("copy")}</button>
          </div>
          <div class="model-price-list">${priceMarkup}</div>
          <div class="model-card-foot">
            <span class="chip blue">${escapeHtml(model.billingType || "按量计费")}</span>
            <span class="provider">上下文 ${escapeHtml(modelContextText(model))}</span>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderModelTable(models) {
  el("modelTableBody").innerHTML = models
    .map((model) => {
      const name = escapeHtml(modelDisplayName(model));
      const copyName = escapeHtml(model.modelName);
      const price = (value) => escapeHtml(formatPricePerMillion(value) || "-");
      return `
        <tr>
          <td><div class="model-table-name">${vendorIconMarkup(model, "vendor-icon-sm")}<code>${name}</code></div></td>
          <td>${escapeHtml(modelFamilyLabel(model))}</td>
          <td><span class="chip blue">${escapeHtml(model.billingType || "按量计费")}</span></td>
          <td class="num">${price(model.inputPricePerMillion)}</td>
          <td class="num">${price(model.outputPricePerMillion)}</td>
          <td class="num">${price(model.cacheReadPricePerMillion)}</td>
          <td class="num">${escapeHtml(modelContextText(model))}</td>
          <td><button class="model-copy-btn" type="button" data-copy-model="${copyName}" title="复制模型名称" aria-label="复制模型名称 ${name}">${icon("copy")}</button></td>
        </tr>
      `;
    })
    .join("");
}

function renderModels() {
  const models = filteredModels();
  el("modelCount").textContent = fmt.format(models.length);
  const isTable = modelViewMode === "table";
  el("modelTablePanel").classList.toggle("hidden", !isTable || !models.length);
  el("modelGrid").classList.toggle("hidden", isTable && Boolean(models.length));
  if (!models.length) {
    el("modelGrid").innerHTML = `<article class="panel model-empty">没有找到匹配的模型，请调整筛选条件。</article>`;
    el("modelTableBody").innerHTML = "";
    return;
  }
  if (isTable) {
    renderModelTable(models);
    el("modelGrid").innerHTML = "";
    return;
  }
  renderModelCards(models);
  el("modelTableBody").innerHTML = "";
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
    await ensureCsrfToken();
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
  showToast("管理员已暂时关闭新增访问密钥");
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

// ---- 充值中心 ----

async function refreshEntitlementAfterTopup() {
  // 首次充值会解除模型权限限制，重新拉一次身份让受限提示立即消失。
  try {
    currentUser = await api("/api/auth/me");
    renderAccountAccessState();
    updateHomeCard();
  } catch {
    // 刷新失败不影响充值结果，下次进页面会自然纠正。
  }
}

const BILLING_CHANNEL_LABELS = {
  redemption: "兑换码",
  epay: "在线支付",
  manual_qr: "扫码转账",
  manual: "人工补单",
};
const BILLING_STATUS_LABELS = { success: "已到账", pending: "待支付", failed: "已失败", expired: "已过期" };
const BILLING_METHOD_LABELS = { alipay: "支付宝", wxpay: "微信支付" };

function billingManualConfig() {
  const manual = billingConfig?.manualPay;
  return manual && typeof manual === "object" ? manual : { enabled: false, methods: [] };
}

function billingManualMethods() {
  const methods = billingManualConfig().methods;
  return Array.isArray(methods) ? methods : [];
}

function billingChannel() {
  // 有自动支付就优先用，否则退到收款码转账。
  const channels = Array.isArray(billingConfig?.channels) ? billingConfig.channels : [];
  if (channels.includes("epay")) return "epay";
  if (channels.includes("manual_qr")) return "manual_qr";
  return "";
}

function billingExchangeRate() {
  const rate = Number(billingConfig?.exchangeRate || 0);
  return rate > 0 ? rate : 7.3;
}

function formatCny(value) {
  return `¥${Number(value || 0).toFixed(2)}`;
}

function topupPayableAmount() {
  const raw = Number(el("topupAmount")?.value || 0);
  if (!Number.isFinite(raw) || raw <= 0) return 0;
  return raw * billingExchangeRate();
}

function setFieldError(id, message) {
  const node = el(id);
  if (node) node.textContent = message || "";
}

function updateTopupPayable() {
  const payable = topupPayableAmount();
  setText("topupPayable", payable > 0 ? `应付 ${formatCny(payable)}` : "应付 ¥0.00");
}

function renderTopupOptions() {
  const container = el("topupOptions");
  if (!container) return;
  const options = Array.isArray(billingConfig?.amountOptions) ? billingConfig.amountOptions : [];
  container.innerHTML = options
    .map(
      (amount) =>
        `<button type="button" class="billing-amount-option${
          Number(amount) === Number(selectedTopupAmount) ? " active" : ""
        }" data-topup-amount="${escapeHtml(amount)}">$${escapeHtml(Number(amount).toFixed(0))}</button>`,
    )
    .join("");
}

function renderBillingOrders() {
  const body = el("billingOrderBody");
  if (!body) return;
  setText("billingOrderCount", `${billingOrderTotal} 条`);
  if (billingLoadError) {
    body.innerHTML = `<tr><td colspan="6" class="empty">${escapeHtml(billingLoadError)}</td></tr>`;
    return;
  }
  if (isBillingLoading && !billingOrders.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">正在加载充值记录…</td></tr>';
    return;
  }
  if (!billingOrders.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">暂无充值记录</td></tr>';
    return;
  }
  body.innerHTML = billingOrders
    .map((order) => {
      const status = String(order.status || "");
      const statusClass = status === "success" ? "success" : status === "pending" ? "pending" : "failed";
      const channel = BILLING_CHANNEL_LABELS[order.channel] || order.channel || "-";
      const method = BILLING_METHOD_LABELS[order.paymentMethod] || "";
      const methodText = method ? `${channel} · ${method}` : channel;
      const paid = Number(order.moneyCny || 0) > 0 ? formatCny(order.moneyCny) : "-";
      // 已提交凭证的待付订单实际在等人工确认，直接说"待支付"会让用户以为没提交成功。
      const statusText =
        status === "pending" && order.submittedAt
          ? "待确认"
          : BILLING_STATUS_LABELS[status] || status || "-";
      const note = status === "failed" && order.reviewNote ? order.reviewNote : "";
      return `<tr>
        <td>${escapeHtml(formatBillingTime(order.createdAt))}</td>
        <td>${escapeHtml(order.tradeNo || "-")}</td>
        <td>${escapeHtml(methodText)}</td>
        <td class="num">${escapeHtml(money.format(order.amountUsd || 0))}</td>
        <td class="num">${escapeHtml(paid)}</td>
        <td><span class="billing-order-status ${statusClass}">${escapeHtml(statusText)}</span>${
          note ? `<span class="billing-review-note">${escapeHtml(note)}</span>` : ""
        }</td>
      </tr>`;
    })
    .join("");
}

function formatBillingTime(value) {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "-";
  const pad = (input) => String(input).padStart(2, "0");
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(
    parsed.getHours(),
  )}:${pad(parsed.getMinutes())}`;
}

function renderTopupMethods() {
  const row = el("topupMethodRow");
  if (!row) return;
  const channel = billingChannel();
  const methods =
    channel === "manual_qr"
      ? billingManualMethods()
      : [
          { method: "alipay", label: "支付宝" },
          { method: "wxpay", label: "微信支付" },
        ];
  const current = row.querySelector('input[name="paymentMethod"]:checked')?.value;
  const selected = methods.some((item) => item.method === current) ? current : methods[0]?.method;
  row.innerHTML = methods
    .map(
      (item) =>
        `<label class="billing-method"><input type="radio" name="paymentMethod" value="${escapeHtml(
          item.method,
        )}"${item.method === selected ? " checked" : ""} /><span>${escapeHtml(
          item.label || item.method,
        )}</span></label>`,
    )
    .join("");
}

function renderBilling() {
  const balance = Number(billingAccount?.balanceUsd || 0);
  const topupTotal = Number(billingAccount?.topupTotalUsd || 0);
  const spent = Number.isFinite(Number(billingAccount?.spentUsd))
    ? Math.max(0, Number(billingAccount.spentUsd))
    : Math.max(0, topupTotal - balance);
  setText("billingBalance", money.format(balance));
  setText("billingBalanceChip", `余额 ${money.format(balance)}`);
  setText("billingTopupTotal", money.format(topupTotal));
  setText("billingSpent", money.format(spent));
  setText("billingRateChip", `汇率 ${formatCny(billingExchangeRate())} / $1`);

  const channel = billingChannel();
  const onlinePanel = el("billingOnlinePanel");
  // 没有任何可用支付渠道时整卡隐藏，避免给出走不通的入口。
  if (onlinePanel) onlinePanel.classList.toggle("hidden", !channel);
  setText(
    "billingOnlineDesc",
    channel === "manual_qr"
      ? "选择额度后扫码付款，提交凭证后由管理员确认到账。"
      : "选择额度后完成支付，额度到账即可使用。",
  );
  setButtonLabel("topupSubmit", channel === "manual_qr" ? "生成付款二维码" : "立即充值");

  const minTopup = Number(billingConfig?.minTopupUsd || 0);
  const amountInput = el("topupAmount");
  if (amountInput && minTopup > 0) amountInput.min = String(minTopup);
  const minHint = minTopup > 0 ? `单笔最低 ${money.format(minTopup)}。` : "";
  setText(
    "topupHint",
    channel === "manual_qr"
      ? `${minHint}扫码付款后请提交凭证，管理员核对收款后额度即到账。`
      : `${minHint}支付完成后请返回本页，系统会自动确认到账结果。`,
  );

  renderTopupMethods();
  renderTopupOptions();
  updateTopupPayable();
  renderBillingOrders();
}

async function loadBillingData() {
  if (!currentUser || isBillingLoading) return;
  isBillingLoading = true;
  billingLoadError = "";
  renderBillingOrders();
  try {
    const payload = await api("/api/me/billing");
    billingConfig = payload.config || null;
    billingAccount = payload.account || null;
    billingOrders = Array.isArray(payload.orders?.items) ? payload.orders.items : [];
    billingOrderTotal = Number(payload.orders?.total || 0);
  } catch (error) {
    billingOrders = [];
    billingOrderTotal = 0;
    billingLoadError = error.message || "充值信息加载失败，请稍后重试。";
    if (error.status !== 404) showToast(billingLoadError);
  } finally {
    isBillingLoading = false;
    renderBilling();
  }
}

async function refreshBillingAvailability() {
  // 后端未开放充值时接口返回 404，据此决定导航项是否出现。
  if (!currentUser?.id) {
    billingAvailable = false;
    updateBillingNav();
    return;
  }
  try {
    const payload = await api("/api/me/billing");
    billingConfig = payload.config || null;
    billingAccount = payload.account || null;
    billingOrders = Array.isArray(payload.orders?.items) ? payload.orders.items : [];
    billingOrderTotal = Number(payload.orders?.total || 0);
    billingAvailable = Boolean(billingConfig?.enabled);
  } catch {
    billingAvailable = false;
  }
  updateBillingNav();
  if (billingAvailable) renderBilling();
}

function updateBillingNav() {
  const tab = el("billingTab");
  if (tab) tab.classList.toggle("hidden", !billingAvailable);
  // 受限提示里的"前往充值"依赖 billingAvailable，可用性变化后要重渲染。
  renderAccountAccessState();
  renderAdminBilling();
}

function showManualPayPanel(payload) {
  const panel = el("billingPayPanel");
  if (!panel) return;
  const qr = el("billingPayQr");
  if (qr) {
    qr.src = String(payload.qrUrl || "");
    qr.alt = `${payload.methodLabel || "收款"}二维码`;
  }
  setText("billingPayMethod", payload.methodLabel || "");
  setText("billingPayMoney", formatCny(payload.moneyCny));
  setText("billingPayAmount", money.format(payload.amountUsd || 0));
  setText("billingPayTradeNo", payload.tradeNo || "-");
  const contact = String(payload.contact || "");
  setText(
    "billingPayNotice",
    [payload.notice || "", contact ? `如有疑问请联系 ${contact}` : ""].filter(Boolean).join(" "),
  );
  setText(
    "manualPayHint",
    `提交后管理员会在约 ${Number(payload.reviewMinutes || 30)} 分钟内核对到账，额度确认后自动开通。`,
  );
  setFieldError("manualPayError", "");
  const note = el("manualPayNote");
  if (note) note.value = "";
  const submit = el("manualPaySubmit");
  if (submit) {
    submit.disabled = false;
    setButtonLabel("manualPaySubmit", "我已付款，提交确认");
  }
  panel.classList.remove("hidden");
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function hideManualPayPanel() {
  el("billingPayPanel")?.classList.add("hidden");
  pendingTopupTradeNo = "";
  stopTopupPolling();
}

async function submitManualPayment(event) {
  event?.preventDefault();
  if (isSubmittingManualPay || !pendingTopupTradeNo) return;
  const payerNote = String(el("manualPayNote")?.value || "").trim();
  setFieldError("manualPayError", "");
  isSubmittingManualPay = true;
  setButtonLabel("manualPaySubmit", "正在提交");
  el("manualPaySubmit").disabled = true;
  try {
    const payload = await api(
      `/api/me/billing/orders/${encodeURIComponent(pendingTopupTradeNo)}/submit`,
      { method: "POST", body: JSON.stringify({ payerNote }) },
    );
    showToast(
      `已提交，管理员将在约 ${Number(payload.reviewMinutes || 30)} 分钟内确认到账`,
    );
    el("billingPayPanel")?.classList.add("hidden");
    await loadBillingData();
    // 保持轮询：管理员确认后前端能自动感知到账。
    startTopupPolling();
  } catch (error) {
    setFieldError("manualPayError", error.message || "提交失败，请稍后重试");
  } finally {
    isSubmittingManualPay = false;
    setButtonLabel("manualPaySubmit", "我已付款，提交确认");
    el("manualPaySubmit").disabled = false;
  }
}

async function submitTopup(event) {
  event?.preventDefault();
  if (isCreatingTopup) return;
  const amount = Number(el("topupAmount")?.value || 0);
  setFieldError("topupError", "");
  if (!Number.isFinite(amount) || amount <= 0) {
    setFieldError("topupError", "请输入有效的充值额度");
    return;
  }
  const minTopup = Number(billingConfig?.minTopupUsd || 0);
  if (minTopup > 0 && amount < minTopup) {
    setFieldError("topupError", `单笔充值额度不得低于 ${money.format(minTopup)}`);
    return;
  }
  const channel = billingChannel();
  if (!channel) {
    setFieldError("topupError", "当前暂无可用支付方式，请联系管理员");
    return;
  }
  const methodRow = el("topupMethodRow");
  const method =
    methodRow?.querySelector('input[name="paymentMethod"]:checked')?.value || "alipay";
  const originalLabel = channel === "manual_qr" ? "生成付款二维码" : "立即充值";
  isCreatingTopup = true;
  setButtonLabel("topupSubmit", "正在创建订单");
  el("topupSubmit").disabled = true;
  try {
    const payload = await api("/api/me/billing/orders", {
      method: "POST",
      body: JSON.stringify({ amount, paymentMethod: method, channel }),
    });
    pendingTopupTradeNo = String(payload.tradeNo || "");
    if (payload.channel === "manual_qr") {
      showManualPayPanel(payload);
    } else {
      // 用表单 POST 跳转收银台：部分网关不接受 GET 携带全部参数。
      submitGatewayForm(payload.submitUrl, payload.params);
    }
    startTopupPolling();
    await loadBillingData();
  } catch (error) {
    setFieldError("topupError", error.message || "创建充值订单失败，请稍后重试");
  } finally {
    isCreatingTopup = false;
    setButtonLabel("topupSubmit", originalLabel);
    el("topupSubmit").disabled = false;
  }
}

function submitGatewayForm(submitUrl, params) {
  if (!submitUrl || !params) return;
  const form = document.createElement("form");
  form.method = "POST";
  form.action = submitUrl;
  form.target = "_blank";
  form.rel = "noopener";
  Object.entries(params).forEach(([name, value]) => {
    const field = document.createElement("input");
    field.type = "hidden";
    field.name = name;
    field.value = String(value ?? "");
    form.appendChild(field);
  });
  document.body.appendChild(form);
  form.submit();
  form.remove();
}

function stopTopupPolling() {
  if (topupPollTimer) {
    window.clearInterval(topupPollTimer);
    topupPollTimer = null;
  }
}

function startTopupPolling() {
  stopTopupPolling();
  if (!pendingTopupTradeNo) return;
  let attempts = 0;
  topupPollTimer = window.setInterval(async () => {
    attempts += 1;
    // 自动支付在新标签完成、收款码转账等管理员确认，这里统一轮询到账结果；
    // 五分钟未见结果即停止，避免页面长期空转（用户刷新页面会看到最新状态）。
    if (attempts > 100 || !pendingTopupTradeNo) {
      stopTopupPolling();
      return;
    }
    try {
      const payload = await api(`/api/me/billing/orders/${encodeURIComponent(pendingTopupTradeNo)}`);
      if (payload.order?.status === "success") {
        stopTopupPolling();
        pendingTopupTradeNo = "";
        el("billingPayPanel")?.classList.add("hidden");
        showToast(`充值成功，到账 ${money.format(payload.order.amountUsd || 0)}`);
        await loadBillingData();
        await refreshEntitlementAfterTopup();
      } else if (["failed", "expired"].includes(String(payload.order?.status || ""))) {
        stopTopupPolling();
        pendingTopupTradeNo = "";
        el("billingPayPanel")?.classList.add("hidden");
        showToast(payload.order?.reviewNote || "本次支付未完成，如已付款请联系管理员");
        await loadBillingData();
      }
    } catch {
      // 轮询失败不打扰用户，下一次继续尝试。
    }
  }, 3000);
}

// ---- 充值管理（仅管理员） ----

let adminRedemptions = [];
let adminRedemptionTotal = 0;
let adminBillingOrders = [];
let adminBillingReviews = [];
let adminBillingPendingSync = 0;
let adminBillingPendingReview = 0;
let adminBillingKeyword = "";
let isAdminBillingLoading = false;
let isGeneratingRedemptions = false;

function adminBillingVisible() {
  return Boolean(currentUser?.isAdmin && billingAvailable && !isViewingCustomerOrganization());
}

function renderAdminRedemptions() {
  const body = el("adminRedemptionBody");
  if (!body) return;
  setText("adminRedemptionCount", `${adminRedemptionTotal} 张`);
  if (!adminRedemptions.length) {
    body.innerHTML = `<tr><td colspan="7" class="empty">${
      isAdminBillingLoading ? "正在加载兑换码…" : "暂无兑换码"
    }</td></tr>`;
    return;
  }
  const statusLabels = { enabled: "可用", used: "已使用", disabled: "已停用" };
  body.innerHTML = adminRedemptions
    .map((item) => {
      const status = String(item.status || "");
      const statusClass = status === "enabled" ? "pending" : status === "used" ? "success" : "failed";
      const action =
        status === "enabled"
          ? `<button class="ghost-btn" type="button" data-disable-redemption="${escapeHtml(
              item.id,
            )}">停用</button>`
          : "-";
      return `<tr>
        <td>${escapeHtml(formatBillingTime(item.createdAt))}</td>
        <td>${escapeHtml(item.name || "-")}</td>
        <td>••••${escapeHtml(item.codeHint || "")}</td>
        <td class="num">${escapeHtml(money.format(item.amountUsd || 0))}</td>
        <td><span class="billing-order-status ${statusClass}">${escapeHtml(
          statusLabels[status] || status,
        )}</span></td>
        <td>${escapeHtml(item.usedBy || "-")}</td>
        <td>${action}</td>
      </tr>`;
    })
    .join("");
}

function renderAdminBillingReviews() {
  const body = el("adminBillingReviewBody");
  if (!body) return;
  setText("adminBillingPendingReview", `${adminBillingPendingReview} 笔待确认`);
  if (!adminBillingReviews.length) {
    body.innerHTML = `<tr><td colspan="8" class="empty">${
      isAdminBillingLoading ? "正在加载待确认订单…" : "暂无待确认订单"
    }</td></tr>`;
    return;
  }
  body.innerHTML = adminBillingReviews
    .map((order) => {
      const method = BILLING_METHOD_LABELS[order.paymentMethod] || order.paymentMethod || "-";
      const tradeNo = escapeHtml(order.tradeNo || "");
      return `<tr>
        <td>${escapeHtml(formatBillingTime(order.submittedAt))}</td>
        <td>${tradeNo}</td>
        <td>${escapeHtml(order.userId || "-")}</td>
        <td>${escapeHtml(method)}</td>
        <td class="num">${escapeHtml(money.format(order.amountUsd || 0))}</td>
        <td class="num">${escapeHtml(formatCny(order.moneyCny))}</td>
        <td>${escapeHtml(order.payerNote || "-")}</td>
        <td>
          <button class="ghost-btn" type="button" data-complete-order="${tradeNo}">确认到账</button>
          <button class="ghost-btn" type="button" data-reject-order="${tradeNo}">驳回</button>
        </td>
      </tr>`;
    })
    .join("");
}

function renderAdminBillingOrders() {
  const body = el("adminBillingOrderBody");
  if (!body) return;
  const pendingChip = el("adminBillingPendingSync");
  const retryButton = el("adminBillingRetrySync");
  if (pendingChip) {
    pendingChip.textContent = `${adminBillingPendingSync} 笔待同步`;
    pendingChip.classList.toggle("hidden", adminBillingPendingSync <= 0);
  }
  if (retryButton) retryButton.classList.toggle("hidden", adminBillingPendingSync <= 0);

  if (!adminBillingOrders.length) {
    body.innerHTML = `<tr><td colspan="8" class="empty">${
      isAdminBillingLoading ? "正在加载充值订单…" : "暂无充值订单"
    }</td></tr>`;
    return;
  }
  body.innerHTML = adminBillingOrders
    .map((order) => {
      const status = String(order.status || "");
      const statusClass = status === "success" ? "success" : status === "pending" ? "pending" : "failed";
      const channel = BILLING_CHANNEL_LABELS[order.channel] || order.channel || "-";
      const paid = Number(order.moneyCny || 0) > 0 ? formatCny(order.moneyCny) : "-";
      const action =
        status === "pending"
          ? `<button class="ghost-btn" type="button" data-complete-order="${escapeHtml(
              order.tradeNo,
            )}">补单</button>`
          : order.syncState === "pending"
            ? '<span class="hint">额度待同步</span>'
            : "-";
      return `<tr>
        <td>${escapeHtml(formatBillingTime(order.createdAt))}</td>
        <td>${escapeHtml(order.tradeNo || "-")}</td>
        <td>${escapeHtml(order.userId || "-")}</td>
        <td>${escapeHtml(channel)}</td>
        <td class="num">${escapeHtml(money.format(order.amountUsd || 0))}</td>
        <td class="num">${escapeHtml(paid)}</td>
        <td><span class="billing-order-status ${statusClass}">${escapeHtml(
          BILLING_STATUS_LABELS[status] || status || "-",
        )}</span></td>
        <td>${action}</td>
      </tr>`;
    })
    .join("");
}

function renderAdminBilling() {
  const section = el("adminBillingSection");
  if (section) section.classList.toggle("hidden", !adminBillingVisible());
  renderAdminBillingReviews();
  renderAdminRedemptions();
  renderAdminBillingOrders();
}

async function loadAdminBillingData() {
  if (!adminBillingVisible() || isAdminBillingLoading) return;
  isAdminBillingLoading = true;
  renderAdminBilling();
  try {
    const [redemptions, orders] = await Promise.all([
      api("/api/admin/billing/redemptions?limit=50"),
      api(`/api/admin/billing/orders?limit=50&keyword=${encodeURIComponent(adminBillingKeyword)}`),
    ]);
    adminRedemptions = Array.isArray(redemptions.items) ? redemptions.items : [];
    adminRedemptionTotal = Number(redemptions.total || 0);
    adminBillingOrders = Array.isArray(orders.items) ? orders.items : [];
    adminBillingReviews = Array.isArray(orders.pendingReviews) ? orders.pendingReviews : [];
    adminBillingPendingSync = Number(orders.pendingSyncCount || 0);
    adminBillingPendingReview = Number(orders.pendingReviewCount || 0);
  } catch (error) {
    adminRedemptions = [];
    adminBillingOrders = [];
    adminBillingReviews = [];
    showToast(error.message || "充值管理数据加载失败");
  } finally {
    isAdminBillingLoading = false;
    renderAdminBilling();
  }
}

async function generateRedemptions(event) {
  event?.preventDefault();
  if (isGeneratingRedemptions) return;
  const amount = Number(el("adminRedemptionAmount")?.value || 0);
  const count = Number(el("adminRedemptionCount2")?.value || 0);
  const name = String(el("adminRedemptionName")?.value || "").trim();
  const expiresInDays = Number(el("adminRedemptionExpiry")?.value || 0);
  setFieldError("adminRedemptionError", "");
  if (!Number.isFinite(amount) || amount <= 0) {
    setFieldError("adminRedemptionError", "请输入有效的单张额度");
    return;
  }
  if (!Number.isInteger(count) || count < 1 || count > 200) {
    setFieldError("adminRedemptionError", "生成数量需在 1 到 200 之间");
    return;
  }
  isGeneratingRedemptions = true;
  setButtonLabel("adminRedemptionSubmit", "正在生成");
  el("adminRedemptionSubmit").disabled = true;
  try {
    const payload = await api("/api/admin/billing/redemptions", {
      method: "POST",
      body: JSON.stringify({ count, amount, name, expiresInDays }),
    });
    renderGeneratedCodes(Array.isArray(payload.items) ? payload.items : []);
    showToast(`已生成 ${payload.count || 0} 张兑换码`);
    await loadAdminBillingData();
  } catch (error) {
    setFieldError("adminRedemptionError", error.message || "生成兑换码失败");
  } finally {
    isGeneratingRedemptions = false;
    setButtonLabel("adminRedemptionSubmit", "生成兑换码");
    el("adminRedemptionSubmit").disabled = false;
  }
}

function renderGeneratedCodes(items) {
  const node = el("adminRedemptionResult");
  if (!node) return;
  if (!items.length) {
    node.classList.add("hidden");
    node.innerHTML = "";
    return;
  }
  // 明文兑换码只在这次响应里存在，刷新后无法再次取回，必须提示保存。
  node.classList.remove("hidden");
  node.innerHTML = `
    <p>以下兑换码仅显示这一次，请立即复制保存。刷新页面后将无法再次查看。</p>
    <ul class="billing-code-list">${items
      .map((item) => `<li>${escapeHtml(item.code)} · ${escapeHtml(money.format(item.amountUsd || 0))}</li>`)
      .join("")}</ul>
    <button class="ghost-btn" type="button" data-copy-codes="1">复制全部</button>
  `;
  node.dataset.codes = items.map((item) => item.code).join("\n");
}

async function disableRedemption(redemptionId) {
  if (!redemptionId) return;
  try {
    await api(`/api/admin/billing/redemptions/${encodeURIComponent(redemptionId)}/disable`, {
      method: "POST",
    });
    showToast("兑换码已停用");
    await loadAdminBillingData();
  } catch (error) {
    showToast(error.message || "停用兑换码失败");
  }
}

async function completeBillingOrder(tradeNo) {
  if (!tradeNo) return;
  // 确认即等于放款，务必先核对收款流水，所以这里保留一道二次确认。
  if (!window.confirm(`已在收款账户中核对到该笔款项？确认后将立即为订单 ${tradeNo} 发放额度。`)) return;
  try {
    const payload = await api(`/api/admin/billing/orders/${encodeURIComponent(tradeNo)}/complete`, {
      method: "POST",
      body: JSON.stringify({ note: "" }),
    });
    showToast(payload.entitlementSynced ? "已确认到账，额度已发放" : "已确认到账，额度同步待重试");
    await loadAdminBillingData();
  } catch (error) {
    showToast(error.message || "确认到账失败");
  }
}

async function rejectBillingOrder(tradeNo) {
  if (!tradeNo) return;
  const note = window.prompt(`驳回订单 ${tradeNo}，请填写原因（用户可见）`, "未查到该笔付款");
  if (note === null) return;
  try {
    await api(`/api/admin/billing/orders/${encodeURIComponent(tradeNo)}/reject`, {
      method: "POST",
      body: JSON.stringify({ note: String(note).trim().slice(0, 500) }),
    });
    showToast("已驳回该笔订单");
    await loadAdminBillingData();
  } catch (error) {
    showToast(error.message || "驳回失败");
  }
}

async function retryBillingSync() {
  try {
    const payload = await api("/api/admin/billing/sync/retry", { method: "POST" });
    showToast(`已重试 ${payload.repaired || 0} 笔额度同步`);
    await loadAdminBillingData();
  } catch (error) {
    showToast(error.message || "重试同步失败");
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

const ORGANIZATION_ROLE_LABELS = {
  owner: "企业主",
  admin: "企业管理员",
  member: "成员",
};

const ORGANIZATION_STATUS_LABELS = {
  active: "已启用",
  invited: "待邀请",
  suspended: "已暂停",
};

function isPlatformAdmin() {
  // `isAdmin` remains a compatibility fallback while V2 rolls out its
  // explicit platform capability. Organization roles never grant this access.
  return Boolean(currentUser?.isPlatformAdmin ?? currentUser?.isAdmin);
}

function canManageCustomerOrganizations() {
  // The API exposes the concise V2 capability name. Keep the longer alias
  // while mixed-version deployments finish rolling forward.
  return Boolean(currentUser?.canManageCustomerOrganizations || currentUser?.canManageCustomers);
}

function canViewCurrentOrganizationUsage() {
  if (currentUser?.canViewOrganizationUsage !== undefined) {
    return Boolean(currentUser.canViewOrganizationUsage);
  }
  // Keep older demo payloads usable without giving ordinary members the
  // organization-wide boards while a server bundle is being upgraded.
  return Boolean(
    currentUser?.organizationDemoEnabled
    && ["owner", "admin"].includes(String(currentUser?.organizationRole || "")),
  );
}

function selectedCustomerOrganizationId() {
  const organization = selectedCustomerOrganization?.organization || selectedCustomerOrganization;
  return String(organization?.id || "");
}

function selectedCustomerOrganizationName() {
  const organization = selectedCustomerOrganization?.organization || selectedCustomerOrganization;
  return String(organization?.name || "客户企业");
}

function customerOrganizationsAvailable() {
  return canManageCustomerOrganizations();
}

function isViewingCustomerOrganization() {
  return customerOrganizationsAvailable() && Boolean(selectedCustomerOrganizationId());
}

function customerOrganizationPath(organizationId = selectedCustomerOrganizationId()) {
  const id = String(organizationId || "").trim();
  return `/api/platform/organizations/${encodeURIComponent(id)}`;
}

function customerOrganizationRecord(item) {
  if (!item || typeof item !== "object") return {};
  return item.organization && typeof item.organization === "object" ? item.organization : item;
}

function customerOrganizationId(item) {
  const record = customerOrganizationRecord(item);
  return String(organizationField(record, "id", "organization_id") || organizationField(record, "organizationId", "organization_id") || "");
}

function customerOrganizationStats(item) {
  const record = customerOrganizationRecord(item);
  const stats = item?.stats && typeof item.stats === "object" ? item.stats : record?.stats || {};
  const numericValue = (camelCase, snakeCase) => {
    const value = organizationField(stats, camelCase, snakeCase)
      ?? organizationField(record, camelCase, snakeCase);
    return Number.isFinite(Number(value)) ? Number(value) : 0;
  };
  return {
    departmentCount: numericValue("departmentCount", "department_count"),
    memberCount: numericValue("memberCount", "member_count"),
    activeMemberCount: numericValue("activeMemberCount", "active_member_count"),
    invitedMemberCount: numericValue("invitedMemberCount", "invited_member_count"),
  };
}

function customerOrganizationStatus(item) {
  const record = customerOrganizationRecord(item);
  return String(organizationField(record, "status", "status") || "active");
}

function customerOrganizationUpdatedAt(item) {
  const record = customerOrganizationRecord(item);
  return organizationField(record, "updatedAt", "updated_at") || organizationField(item, "updatedAt", "updated_at");
}

function customerOrganizationsUrl() {
  const params = new URLSearchParams({
    page: String(customerOrganizationsPage),
    pageSize: String(customerOrganizationsPageSize),
  });
  if (customerOrganizationsFilters.search) params.set("search", customerOrganizationsFilters.search);
  if (customerOrganizationsFilters.status) params.set("status", customerOrganizationsFilters.status);
  return `/api/platform/organizations?${params.toString()}`;
}

function organizationUsageScope() {
  const customerOrganizationId = selectedCustomerOrganizationId();
  if (customerOrganizationsAvailable() && customerOrganizationId) {
    return {
      kind: "platformCustomer",
      organizationId: customerOrganizationId,
      name: selectedCustomerOrganizationName(),
      usagePath: `/api/platform/organizations/${encodeURIComponent(customerOrganizationId)}/usage`,
      departmentsUsagePath: `/api/platform/organizations/${encodeURIComponent(customerOrganizationId)}/departments/usage`,
    };
  }
  if (canViewCurrentOrganizationUsage()) {
    return {
      kind: "currentOrganization",
      organizationId: String(currentUser?.organizationId || ""),
      // The auth bootstrap already has the tenant id, while the directory
      // snapshot is intentionally unavailable to ordinary customer members.
      // Prefer the resolved organization name when it exists so the company
      // scope never flashes as a generic placeholder for an admin/owner.
      name: String(
        organizationSnapshot?.organization?.name
        || currentUser?.organization?.name
        || currentUser?.organizationName
        || "我的企业",
      ),
      usagePath: "/api/organization/current/usage",
      departmentsUsagePath: "/api/organization/current/departments/usage",
    };
  }
  return null;
}

function organizationUsageScopeKey() {
  const scope = organizationUsageScope();
  return scope ? `${scope.kind}:${scope.organizationId || ""}` : "platform";
}

function resetOrganizationUsageViews() {
  // Customer selection changes the server-side tenant. Clear every derived
  // view so an in-flight or cached platform response cannot appear in it.
  adminUsageRequestId += 1;
  departmentUsageRequestId += 1;
  selectedAdminEmployee = "";
  selectedDepartment = "";
  departmentPickerOpen = false;
  departmentPickerOptions = [];
  adminUsageData = [];
  adminSummaryData = [];
  adminEmployees = [];
  adminDataFreshness = null;
  departmentUsageData = [];
  departmentSummaryData = [];
  departmentRankings = [];
  departmentEmployees = [];
  departmentDataFreshness = null;
  adminUsageScopeKey = "";
  adminUsageLoadingScopeKey = "";
  departmentUsageScopeKey = "";
  departmentUsageLoadingScopeKey = "";
  el("adminEmployeeSearch").value = "";
  el("departmentEmployeeSearch").value = "";
  closeDepartmentPicker();
}

function canViewAdminUsage() {
  return Boolean(organizationUsageScope() || isPlatformAdmin());
}

function canViewDepartmentUsage() {
  return Boolean(organizationUsageScope() || isPlatformAdmin());
}

function organizationUsageScopeLabel() {
  return organizationUsageScope()?.name || "全员";
}

function isMockCustomerIdentity() {
  return Boolean(
    currentUser?.organizationDemoEnabled
    && (currentUser?.organizationRole || currentUser?.isKnownDemoCustomerIdentity),
  );
}

function syncNavigationVisibility() {
  const canBrowseCustomers = customerOrganizationsAvailable();
  const canViewAdmin = canViewAdminUsage();
  const canViewDepartments = canViewDepartmentUsage();
  const isCustomer = isMockCustomerIdentity();
  el("customersTab").classList.toggle("hidden", !canBrowseCustomers);
  el("adminTab").classList.toggle("hidden", !canViewAdmin);
  el("departmentTab").classList.toggle("hidden", !canViewDepartments);
  // Customer identities use their demo-scoped views only; never expose
  // seller account functions that lack a customer-local contract.
  document.querySelectorAll('[data-view="keys"], [data-view="billing"]').forEach((button) => {
    button.classList.toggle("hidden", isCustomer);
  });
  document.querySelectorAll('[data-global-page="models"]').forEach((button) => {
    button.classList.toggle("hidden", isCustomer);
  });
}

function renderCustomerUsageBreadcrumbs(view = currentView) {
  const isCustomerUsage = isViewingCustomerOrganization() && ["admin", "department"].includes(view);
  const scopeLabel = isCustomerUsage ? `客户企业：${selectedCustomerOrganizationName()}` : "";
  el("customerUsageBreadcrumb")?.classList.toggle("hidden", !(isCustomerUsage && view === "admin"));
  el("customerDepartmentBreadcrumb")?.classList.toggle("hidden", !(isCustomerUsage && view === "department"));
  if (isCustomerUsage) {
    setText("customerUsageBreadcrumbLabel", scopeLabel);
    setText("customerDepartmentBreadcrumbLabel", scopeLabel);
  }
}

function organizationApiBasePath() {
  const customerOrganizationId = selectedCustomerOrganizationId();
  if (customerOrganizationsAvailable() && customerOrganizationId) {
    return customerOrganizationPath(customerOrganizationId);
  }
  return "/api/organization/current";
}

function organizationApiPath(suffix = "") {
  return `${organizationApiBasePath()}${suffix}`;
}

function organizationField(record, camelCase, snakeCase = "") {
  if (!record || typeof record !== "object") return undefined;
  if (record[camelCase] !== undefined) return record[camelCase];
  return snakeCase && record[snakeCase] !== undefined ? record[snakeCase] : undefined;
}

function organizationDepartments() {
  const departments = organizationSnapshot?.departments || organizationSnapshot?.departmentList || [];
  return Array.isArray(departments) ? departments : [];
}

function organizationDepartmentId(department) {
  return String(organizationField(department, "id", "department_id") || organizationField(department, "departmentId", "department_id") || "");
}

function organizationMemberId(member) {
  return String(organizationField(member, "id", "member_id") || organizationField(member, "memberId", "member_id") || "");
}

function organizationDate(value) {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleDateString("zh-CN", { year: "numeric", month: "numeric", day: "numeric" });
}

function organizationRoleLabel(role) {
  return ORGANIZATION_ROLE_LABELS[String(role || "")] || "成员";
}

function organizationStatusLabel(status) {
  return ORGANIZATION_STATUS_LABELS[String(status || "")] || "未知";
}

function organizationStats() {
  const stats = organizationSnapshot?.stats || {};
  const members = organizationMembers;
  const departments = organizationDepartments();
  const value = (camelCase, snakeCase, fallback) => {
    const statValue = organizationField(stats, camelCase, snakeCase);
    return Number.isFinite(Number(statValue)) ? Number(statValue) : fallback;
  };
  return {
    departmentCount: value("departmentCount", "department_count", departments.length),
    memberCount: value("memberCount", "member_count", organizationMemberTotal || members.length),
    activeMemberCount: value("activeMemberCount", "active_member_count", members.filter((member) => member.status === "active").length),
    invitedMemberCount: value("invitedMemberCount", "invited_member_count", members.filter((member) => member.status === "invited").length),
  };
}

function organizationCanManage() {
  if (customerOrganizationsAvailable()) {
    return Boolean(selectedCustomerOrganizationId())
      && customerOrganizationStatus(selectedCustomerOrganization) !== "archived";
  }
  const capabilities = organizationSnapshot?.capabilities || {};
  const serverCapability = capabilities.canManageOrganization ?? capabilities.canManage;
  if (organizationSnapshot && serverCapability === false) return false;
  return Boolean(currentUser?.canManageOrganization);
}

function organizationCanView() {
  // The organization detail is a seller-side customer-management surface.
  // Customer identities use the organization-scoped usage boards instead;
  // never expose the underlying member/department directory to them.
  return isViewingCustomerOrganization();
}

function renderOrganizationDepartmentOptions(selectId, selectedId = "", placeholder = "请选择部门") {
  const select = el(selectId);
  if (!select) return;
  const activeDepartments = organizationDepartments().filter((department) => String(department.status || "active") === "active");
  const options = activeDepartments.map((department) => {
    const id = organizationDepartmentId(department);
    return `<option value="${escapeHtml(id)}">${escapeHtml(department.name || "未命名部门")}</option>`;
  }).join("");
  select.innerHTML = placeholder === null ? options : `<option value="">${escapeHtml(placeholder)}</option>${options}`;
  select.value = selectedId;
  if (select.value !== selectedId) select.value = "";
}

function renderOrganizationFilters() {
  const currentSelection = organizationMemberFilters.departmentId || "";
  renderOrganizationDepartmentOptions("organizationDepartmentFilter", currentSelection, "全部部门");
  el("organizationRoleFilter").value = organizationMemberFilters.role;
  el("organizationStatusFilter").value = organizationMemberFilters.status;
  if (el("organizationMemberSearch").value !== organizationMemberFilters.search) {
    el("organizationMemberSearch").value = organizationMemberFilters.search;
  }
}

function renderOrganizationDepartments() {
  const container = el("organizationDepartmentList");
  const departments = organizationDepartments();
  if (isOrganizationLoading && !departments.length) {
    container.innerHTML = '<div class="organization-empty">正在加载部门…</div>';
    return;
  }
  if (!departments.length) {
    container.innerHTML = '<div class="organization-empty">还没有部门。创建一个部门后即可邀请成员加入。</div>';
    return;
  }
  const canManage = organizationCanManage();
  container.innerHTML = departments.map((department) => {
    const id = organizationDepartmentId(department);
    const memberCount = Number(organizationField(department, "memberCount", "member_count") || 0);
    const activeMemberCount = Number(organizationField(department, "activeMemberCount", "active_member_count") || 0);
    const invitedMemberCount = Number(organizationField(department, "invitedMemberCount", "invited_member_count") || 0);
    return `
      <article class="organization-department-card">
        <div class="organization-department-card-head">
          <div>
            <h3>${escapeHtml(department.name || "未命名部门")}</h3>
            <p>${escapeHtml(id || "部门")}</p>
          </div>
          <span class="chip">${fmt.format(memberCount)} 人</span>
        </div>
        <div class="organization-department-metrics">
          <div><strong>${fmt.format(memberCount)}</strong><span>全部成员</span></div>
          <div><strong>${fmt.format(activeMemberCount)}</strong><span>已启用</span></div>
          <div><strong>${fmt.format(invitedMemberCount)}</strong><span>待邀请</span></div>
        </div>
        <div class="organization-department-card-actions">
          <button class="ghost-btn" type="button" data-organization-department-edit="${escapeHtml(id)}" ${canManage ? "" : "disabled"}>改名</button>
          <button class="danger-outline-btn" type="button" data-organization-department-archive="${escapeHtml(id)}" ${canManage ? "" : "disabled"}>归档</button>
        </div>
      </article>
    `;
  }).join("");
}

function renderOrganizationMembers() {
  const table = el("organizationMemberTable");
  const totalPages = Math.max(1, Math.ceil(organizationMemberTotal / organizationMemberPageSize));
  const page = Math.min(organizationMemberPage, totalPages);
  if (organizationMemberPage !== page) organizationMemberPage = page;
  const stats = organizationStats();
  setText("organizationMemberCountChip", `${fmt.format(organizationMemberTotal)} / ${fmt.format(stats.memberCount)} 人`);
  setText("organizationPageInfo", `第 ${page} / ${totalPages} 页`);
  el("organizationPreviousPageButton").disabled = page <= 1 || isOrganizationMemberLoading;
  el("organizationNextPageButton").disabled = page >= totalPages || isOrganizationMemberLoading;
  if (isOrganizationMemberLoading && !organizationMembers.length) {
    table.innerHTML = '<tr><td colspan="6"><div class="organization-empty">正在加载成员…</div></td></tr>';
    return;
  }
  if (!organizationMembers.length) {
    table.innerHTML = '<tr><td colspan="6"><div class="organization-empty">没有符合当前筛选条件的成员。</div></td></tr>';
    return;
  }
  const canManage = organizationCanManage();
  table.innerHTML = organizationMembers.map((member) => {
    const id = organizationMemberId(member);
    const name = member.name || "未命名成员";
    const email = member.email || "-";
    const role = String(member.role || "member");
    const status = String(member.status || "invited");
    const departmentName = organizationField(member, "departmentName", "department_name") || "未分配部门";
    const joinedAt = organizationField(member, "createdAt", "created_at");
    const toggleStatus = status === "suspended" ? "active" : "suspended";
    const toggleLabel = status === "suspended" ? "恢复" : "暂停";
    return `
      <tr>
        <td>
          <div class="organization-member-name">
            <span class="organization-member-avatar" aria-hidden="true">${escapeHtml(initials(email, name))}</span>
            <div><strong>${escapeHtml(name)}</strong><span>${escapeHtml(email)}</span></div>
          </div>
        </td>
        <td>${escapeHtml(departmentName)}</td>
        <td><span class="organization-role ${escapeHtml(role)}">${escapeHtml(organizationRoleLabel(role))}</span></td>
        <td><span class="organization-status ${escapeHtml(status)}">${escapeHtml(organizationStatusLabel(status))}</span></td>
        <td>${escapeHtml(organizationDate(joinedAt))}</td>
        <td>
          <div class="organization-member-actions">
            <button class="ghost-btn" type="button" data-organization-member-edit="${escapeHtml(id)}" ${canManage ? "" : "disabled"}>编辑</button>
            <button class="${toggleStatus === "suspended" ? "danger-outline-btn" : "ghost-btn"}" type="button" data-organization-member-status="${escapeHtml(id)}" data-organization-member-next-status="${escapeHtml(toggleStatus)}" ${canManage ? "" : "disabled"}>${toggleLabel}</button>
          </div>
        </td>
      </tr>
    `;
  }).join("");
}

function renderCustomerOrganizations() {
  const grid = el("customerOrganizationGrid");
  if (!grid) return;
  const totalPages = Math.max(1, Math.ceil(customerOrganizationsTotal / customerOrganizationsPageSize));
  const page = Math.min(customerOrganizationsPage, totalPages);
  if (customerOrganizationsPage !== page) customerOrganizationsPage = page;
  setText("customerOrganizationCountChip", `${fmt.format(customerOrganizationsTotal)} 家`);
  setText("customerOrganizationPageInfo", `第 ${page} / ${totalPages} 页`);
  el("customerOrganizationPreviousPageButton").disabled = page <= 1 || isCustomerOrganizationsLoading;
  el("customerOrganizationNextPageButton").disabled = page >= totalPages || isCustomerOrganizationsLoading;
  if (isCustomerOrganizationsLoading && !customerOrganizations.length) {
    grid.innerHTML = '<div class="customer-directory-empty">正在加载客户企业…</div>';
    return;
  }
  if (!customerOrganizations.length) {
    grid.innerHTML = '<div class="customer-directory-empty">没有符合当前筛选条件的客户企业。新增企业后，可继续维护其部门和成员。</div>';
    return;
  }
  grid.innerHTML = customerOrganizations.map((item) => {
    const organization = customerOrganizationRecord(item);
    const id = customerOrganizationId(item);
    const name = organization.name || "未命名企业";
    const status = customerOrganizationStatus(item);
    const isArchived = status === "archived";
    const stats = customerOrganizationStats(item);
    return `
      <article class="customer-organization-card ${isArchived ? "archived" : ""}">
        <div class="customer-organization-card-head">
          <div>
            <h3>${escapeHtml(name)}</h3>
            <p>${escapeHtml(id || "企业")}</p>
          </div>
          <span class="customer-organization-status ${isArchived ? "archived" : ""}">${isArchived ? "已归档" : "正常"}</span>
        </div>
        <div class="customer-organization-metrics">
          <div><strong>${fmt.format(stats.departmentCount)}</strong><span>部门</span></div>
          <div><strong>${fmt.format(stats.memberCount)}</strong><span>成员</span></div>
          <div><strong>${fmt.format(stats.activeMemberCount)}</strong><span>已启用</span></div>
          <div><strong>${fmt.format(stats.invitedMemberCount)}</strong><span>待邀请</span></div>
        </div>
        <p>更新于 ${escapeHtml(organizationDate(customerOrganizationUpdatedAt(item)))}</p>
        <div class="customer-organization-card-actions">
          <button class="primary-btn" type="button" data-customer-organization-open="${escapeHtml(id)}">进入企业</button>
          <button class="ghost-btn" type="button" data-customer-organization-edit="${escapeHtml(id)}" ${isArchived ? "disabled" : ""}>改名</button>
          <button class="danger-outline-btn" type="button" data-customer-organization-archive="${escapeHtml(id)}" ${isArchived ? "disabled" : ""}>归档</button>
        </div>
      </article>
    `;
  }).join("");
}

function renderCustomerOrganizationFilters() {
  const search = el("customerOrganizationSearch");
  const status = el("customerOrganizationStatusFilter");
  if (search && search.value !== customerOrganizationsFilters.search) search.value = customerOrganizationsFilters.search;
  if (status) status.value = customerOrganizationsFilters.status;
}

async function loadCustomerOrganizations() {
  if (!customerOrganizationsAvailable() || isCustomerOrganizationsLoading) return;
  isCustomerOrganizationsLoading = true;
  renderCustomerOrganizations();
  try {
    const payload = await api(customerOrganizationsUrl());
    customerOrganizations = Array.isArray(payload?.items) ? payload.items : Array.isArray(payload?.organizations) ? payload.organizations : [];
    customerOrganizationsTotal = Number(payload?.total ?? customerOrganizations.length);
    customerOrganizationsPage = Number(payload?.page || customerOrganizationsPage || 1);
  } catch (error) {
    customerOrganizations = [];
    customerOrganizationsTotal = 0;
    showToast(error.message || "客户企业列表加载失败");
  } finally {
    isCustomerOrganizationsLoading = false;
    renderCustomerOrganizationFilters();
    renderCustomerOrganizations();
  }
}

async function openCustomerOrganization(organizationId) {
  if (!customerOrganizationsAvailable() || !organizationId) return;
  const id = String(organizationId);
  selectedCustomerOrganization = customerOrganizations.find((item) => customerOrganizationId(item) === id) || { id };
  resetOrganizationUsageViews();
  organizationDataRequestId += 1;
  organizationMemberRequestId += 1;
  isOrganizationLoading = false;
  isOrganizationMemberLoading = false;
  organizationDataLoadingScopeKey = "";
  organizationMemberLoadingScopeKey = "";
  organizationSnapshot = null;
  organizationMembers = [];
  organizationMemberTotal = 0;
  organizationMemberPage = 1;
  organizationMemberFilters = { search: "", departmentId: "", role: "", status: "" };
  customerOrganizationDetailTab = "info";
  syncNavigationVisibility();
  switchView("organization");
  await loadOrganizationData();
}

function closeCustomerOrganization() {
  selectedCustomerOrganization = null;
  resetOrganizationUsageViews();
  organizationDataRequestId += 1;
  organizationMemberRequestId += 1;
  isOrganizationLoading = false;
  isOrganizationMemberLoading = false;
  organizationDataLoadingScopeKey = "";
  organizationMemberLoadingScopeKey = "";
  organizationSnapshot = null;
  organizationMembers = [];
  organizationMemberTotal = 0;
  organizationMemberPage = 1;
  organizationMemberFilters = { search: "", departmentId: "", role: "", status: "" };
  syncNavigationVisibility();
  switchView("customers");
  loadCustomerOrganizations();
}

function renderOrganization() {
  if (!organizationCanView()) return;
  const organization = organizationSnapshot?.organization || {};
  const currentRole = organizationSnapshot?.organizationRole || organizationSnapshot?.currentMember?.role || currentUser.organizationRole || "admin";
  const name = organization.name || "企业组织";
  const stats = organizationStats();
  const canManage = organizationCanManage();
  const isPlatformCustomer = customerOrganizationsAvailable() && Boolean(selectedCustomerOrganizationId());
  setText("organizationTitle", name);
  setText(
    "organizationSubtitle",
    isPlatformCustomer
      ? `${name} · 平台运营视图。可维护客户资料、部门和成员，并切换查看企业全员或部门用量。`
      : `${name} · 当前身份：${organizationRoleLabel(currentRole)}。这里的内容为演示数据，不会创建真实账号或发送邮件。`,
  );
  setText("organizationDepartmentCount", fmt.format(stats.departmentCount));
  setText("organizationMemberCount", fmt.format(stats.memberCount));
  setText("organizationActiveMemberCount", fmt.format(stats.activeMemberCount));
  setText("organizationInvitedMemberCount", fmt.format(stats.invitedMemberCount));
  const createDepartmentButton = el("createOrganizationDepartmentButton");
  const inviteMemberButton = el("inviteOrganizationMemberButton");
  const resetDemoButton = el("resetOrganizationDemoButton");
  if (createDepartmentButton) createDepartmentButton.disabled = !canManage;
  if (inviteMemberButton) inviteMemberButton.disabled = !canManage;
  if (resetDemoButton) resetDemoButton.disabled = !canManage;
  el("backToCustomersButton")?.classList.toggle("hidden", !isPlatformCustomer);
  if (resetDemoButton) resetDemoButton.classList.toggle("hidden", isPlatformCustomer);
  if (isPlatformCustomer && customerOrganizationStatus(selectedCustomerOrganization) === "archived") {
    setText("organizationSubtitle", `${name} · 已归档客户企业，仅可查看历史组织信息。`);
  }
  el("organizationUsageTabs")?.classList.toggle("hidden", !isPlatformCustomer && !canViewCurrentOrganizationUsage());
  el("organizationManagementWorkspace")?.classList.toggle("hidden", !isPlatformCustomer && !currentUser?.canManageOrganization);
  renderOrganizationUsageTabs();
  renderOrganizationFilters();
  renderOrganizationDepartments();
  renderOrganizationMembers();
}

function organizationMembersUrl() {
  const params = new URLSearchParams({
    page: String(organizationMemberPage),
    pageSize: String(organizationMemberPageSize),
  });
  if (organizationMemberFilters.search) params.set("search", organizationMemberFilters.search);
  if (organizationMemberFilters.departmentId) params.set("departmentId", organizationMemberFilters.departmentId);
  if (organizationMemberFilters.role) params.set("role", organizationMemberFilters.role);
  if (organizationMemberFilters.status) params.set("status", organizationMemberFilters.status);
  return `${organizationApiPath("/members")}?${params.toString()}`;
}

function renderOrganizationUsageTabs() {
  const scope = organizationUsageScope();
  const tabs = el("organizationUsageTabs");
  if (!tabs) return;
  tabs.classList.toggle("hidden", !scope);
  if (!scope) return;
  const scopeName = scope.name || "企业";
  const selected = customerOrganizationDetailTab || "info";
  tabs.querySelectorAll("[data-organization-usage-view]").forEach((button) => {
    const isActive = button.dataset.organizationUsageView === selected;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-selected", isActive ? "true" : "false");
  });
  setText("organizationUsageScopeLabel", scope.kind === "platformCustomer" ? `客户：${scopeName}` : `企业：${scopeName}`);
}

function showOrganizationUsage(view) {
  const scope = organizationUsageScope();
  if (!scope) return;
  customerOrganizationDetailTab = ["info", "usage", "departments-usage"].includes(view) ? view : "info";
  renderOrganizationUsageTabs();
  if (customerOrganizationDetailTab === "info") {
    switchView("organization");
  } else if (customerOrganizationDetailTab === "usage") {
    switchView("admin");
  } else {
    switchView("department");
  }
}

async function loadOrganizationMembers() {
  const scopeKey = organizationUsageScopeKey();
  if (!organizationCanView() || isOrganizationMemberLoading) return;
  const requestId = ++organizationMemberRequestId;
  isOrganizationMemberLoading = true;
  organizationMemberLoadingScopeKey = scopeKey;
  renderOrganizationMembers();
  try {
    const payload = await api(organizationMembersUrl());
    if (requestId !== organizationMemberRequestId || scopeKey !== organizationUsageScopeKey()) return;
    organizationMembers = Array.isArray(payload?.items) ? payload.items : [];
    organizationMemberTotal = Number(payload?.total || 0);
    organizationMemberPage = Number(payload?.page || organizationMemberPage || 1);
  } catch (error) {
    if (requestId !== organizationMemberRequestId || scopeKey !== organizationUsageScopeKey()) return;
    organizationMembers = [];
    organizationMemberTotal = 0;
    showToast(error.message || "成员列表加载失败");
  } finally {
    if (requestId !== organizationMemberRequestId) return;
    isOrganizationMemberLoading = false;
    organizationMemberLoadingScopeKey = "";
    renderOrganization();
  }
}

async function loadOrganizationData() {
  const scopeKey = organizationUsageScopeKey();
  if (!organizationCanView() || isOrganizationLoading) return;
  const requestId = ++organizationDataRequestId;
  isOrganizationLoading = true;
  organizationDataLoadingScopeKey = scopeKey;
  renderOrganization();
  try {
    const [currentPayload, membersPayload] = await Promise.all([
      api(organizationApiPath()),
      api(organizationMembersUrl()),
    ]);
    if (requestId !== organizationDataRequestId || scopeKey !== organizationUsageScopeKey()) return;
    organizationSnapshot = currentPayload || null;
    organizationMembers = Array.isArray(membersPayload?.items) ? membersPayload.items : [];
    organizationMemberTotal = Number(membersPayload?.total || 0);
    organizationMemberPage = Number(membersPayload?.page || organizationMemberPage || 1);
  } catch (error) {
    if (requestId !== organizationDataRequestId || scopeKey !== organizationUsageScopeKey()) return;
    organizationSnapshot = null;
    organizationMembers = [];
    organizationMemberTotal = 0;
    showToast(error.message || "企业组织加载失败");
  } finally {
    if (requestId !== organizationDataRequestId) return;
    isOrganizationLoading = false;
    organizationDataLoadingScopeKey = "";
    renderOrganization();
  }
}

function closeCustomerOrganizationModal(options = {}) {
  if (isCustomerOrganizationSaving && !options.force) return;
  editingCustomerOrganizationId = "";
  el("customerOrganizationForm").reset();
  el("customerOrganizationModal").classList.add("hidden");
}

function openCustomerOrganizationModal(organizationId = "") {
  if (!customerOrganizationsAvailable()) return;
  const id = String(organizationId || "");
  const listItem = customerOrganizations.find((item) => customerOrganizationId(item) === id);
  const organization = listItem ? customerOrganizationRecord(listItem) : {};
  editingCustomerOrganizationId = listItem ? customerOrganizationId(listItem) : "";
  el("customerOrganizationForm").reset();
  const isEditing = Boolean(editingCustomerOrganizationId);
  el("customerOrganizationOwnerNameField").classList.toggle("hidden", isEditing);
  el("customerOrganizationOwnerEmailField").classList.toggle("hidden", isEditing);
  el("customerOrganizationOwnerNameInput").required = !isEditing;
  el("customerOrganizationOwnerEmailInput").required = !isEditing;
  setText("customerOrganizationModalTitle", isEditing ? "修改客户企业名称" : "新增客户企业");
  setText(
    "customerOrganizationModalDescription",
    isEditing ? "修改后会立即显示在企业目录和详情页。" : "创建后可进入企业详情，继续维护部门和成员。",
  );
  setText("submitCustomerOrganizationButton", isEditing ? "保存修改" : "创建企业");
  el("customerOrganizationNameInput").value = organization.name || "";
  el("customerOrganizationOwnerNameInput").value = "";
  el("customerOrganizationOwnerEmailInput").value = "";
  el("customerOrganizationModal").classList.remove("hidden");
  window.setTimeout(() => el("customerOrganizationNameInput").focus(), 0);
}

async function archiveCustomerOrganization(organizationId) {
  if (!customerOrganizationsAvailable() || !organizationId) return;
  const item = customerOrganizations.find((candidate) => customerOrganizationId(candidate) === String(organizationId));
  const organization = customerOrganizationRecord(item);
  const name = organization.name || "这家客户企业";
  if (!window.confirm(`归档“${name}”？已归档企业将无法继续管理或访问演示数据。`)) return;
  try {
    await ensureCsrfToken();
    await api(`${customerOrganizationPath(organizationId)}/archive`, { method: "POST", body: JSON.stringify({}) });
    if (selectedCustomerOrganizationId() === String(organizationId)) {
      selectedCustomerOrganization = null;
      resetOrganizationUsageViews();
      organizationDataRequestId += 1;
      organizationMemberRequestId += 1;
      isOrganizationLoading = false;
      isOrganizationMemberLoading = false;
      organizationDataLoadingScopeKey = "";
      organizationMemberLoadingScopeKey = "";
      syncNavigationVisibility();
    }
    await loadCustomerOrganizations();
    showToast("客户企业已归档");
  } catch (error) {
    showToast(error.message || "客户企业归档失败");
  }
}

async function resetCustomerOrganizationsDemo() {
  if (!customerOrganizationsAvailable()) return;
  if (!window.confirm("重置后会清除本次演示中的所有客户企业、部门与成员变更，并恢复初始样例。确定继续吗？")) return;
  setButtonLoading("resetCustomerOrganizationsDemoButton", true, "重置中");
  try {
    await ensureCsrfToken();
    await api("/api/platform/organizations/demo/reset", { method: "POST", body: JSON.stringify({}) });
    selectedCustomerOrganization = null;
    resetOrganizationUsageViews();
    organizationDataRequestId += 1;
    organizationMemberRequestId += 1;
    isOrganizationLoading = false;
    isOrganizationMemberLoading = false;
    organizationDataLoadingScopeKey = "";
    organizationMemberLoadingScopeKey = "";
    syncNavigationVisibility();
    customerOrganizationsPage = 1;
    customerOrganizationsFilters = { search: "", status: "" };
    await loadCustomerOrganizations();
    showToast("演示数据已重置");
  } catch (error) {
    showToast(error.message || "演示数据重置失败");
  } finally {
    setButtonLoading("resetCustomerOrganizationsDemoButton", false);
  }
}

function closeOrganizationDepartmentModal(options = {}) {
  if (isOrganizationDepartmentSaving && !options.force) return;
  editingOrganizationDepartmentId = "";
  el("organizationDepartmentForm").reset();
  el("organizationDepartmentModal").classList.add("hidden");
}

function openOrganizationDepartmentModal(departmentId = "") {
  if (!organizationCanManage()) return;
  const department = organizationDepartments().find((item) => organizationDepartmentId(item) === String(departmentId));
  editingOrganizationDepartmentId = department ? organizationDepartmentId(department) : "";
  el("organizationDepartmentForm").reset();
  setText("organizationDepartmentModalTitle", department ? "修改部门名称" : "新增部门");
  setText("organizationDepartmentModalDescription", department ? "修改后会立即更新成员列表中的部门名称。" : "创建后可邀请成员加入这个部门。");
  setText("submitOrganizationDepartmentButton", department ? "保存修改" : "创建部门");
  el("organizationDepartmentNameInput").value = department?.name || "";
  el("organizationDepartmentModal").classList.remove("hidden");
  window.setTimeout(() => el("organizationDepartmentNameInput").focus(), 0);
}

function closeOrganizationMemberModal(options = {}) {
  if (isOrganizationMemberSaving && !options.force) return;
  editingOrganizationMemberId = "";
  el("organizationMemberForm").reset();
  el("organizationMemberEmailInput").disabled = false;
  el("organizationMemberStatusField").classList.add("hidden");
  el("organizationMemberModal").classList.add("hidden");
}

function openOrganizationMemberModal(memberId = "") {
  if (!organizationCanManage()) return;
  const member = organizationMembers.find((item) => organizationMemberId(item) === String(memberId));
  editingOrganizationMemberId = member ? organizationMemberId(member) : "";
  el("organizationMemberForm").reset();
  renderOrganizationDepartmentOptions(
    "organizationMemberDepartmentInput",
    member ? String(organizationField(member, "departmentId", "department_id") || "") : "",
    null,
  );
  const isEditing = Boolean(member);
  setText("organizationMemberModalTitle", isEditing ? "编辑成员" : "邀请成员");
  setText("organizationMemberModalDescription", isEditing ? "更新角色、部门或访问状态后会立即生效。" : "成员会以待邀请状态加入演示企业，不会发送真实邮件。");
  setText("submitOrganizationMemberButton", isEditing ? "保存修改" : "发送邀请");
  el("organizationMemberStatusField").classList.toggle("hidden", !isEditing);
  el("organizationMemberNameInput").value = member?.name || "";
  el("organizationMemberEmailInput").value = member?.email || "";
  el("organizationMemberEmailInput").disabled = isEditing;
  el("organizationMemberRoleInput").value = member?.role || "member";
  el("organizationMemberStatusInput").value = member?.status || "invited";
  el("organizationMemberModal").classList.remove("hidden");
  window.setTimeout(() => el("organizationMemberNameInput").focus(), 0);
}

async function archiveOrganizationDepartment(departmentId) {
  if (!organizationCanManage()) return;
  const department = organizationDepartments().find((item) => organizationDepartmentId(item) === String(departmentId));
  if (!department || !window.confirm(`归档“${department.name}”？归档前需要先迁移或暂停该部门所有已启用和待邀请成员。`)) return;
  try {
    await ensureCsrfToken();
    await api(organizationApiPath(`/departments/${encodeURIComponent(departmentId)}/archive`), {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (organizationMemberFilters.departmentId === String(departmentId)) {
      organizationMemberFilters = { ...organizationMemberFilters, departmentId: "" };
    }
    organizationMemberPage = 1;
    await loadOrganizationData();
    showToast("部门已归档");
  } catch (error) {
    showToast(error.message || "部门归档失败");
  }
}

async function updateOrganizationMemberStatus(memberId, status) {
  if (!organizationCanManage()) return;
  try {
    await ensureCsrfToken();
    await api(organizationApiPath(`/members/${encodeURIComponent(memberId)}`), {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
    await loadOrganizationData();
    showToast(status === "suspended" ? "成员已暂停" : "成员已恢复");
  } catch (error) {
    showToast(error.message || "成员状态更新失败");
  }
}

function switchView(view) {
  if (view === "customers" && !customerOrganizationsAvailable()) view = "dashboard";
  if (view === "admin" && !canViewAdminUsage()) view = "dashboard";
  if (view === "department" && !canViewDepartmentUsage()) view = "dashboard";
  if (view === "team" && !currentUser?.isTeamLeader) view = "dashboard";
  if (view === "organization" && !organizationCanView()) view = "dashboard";
  if (isMockCustomerIdentity() && (view === "keys" || view === "billing" || view === "models")) view = "dashboard";
  if (view === "billing" && !billingAvailable) view = "dashboard";
  if (currentView === "keys" && view !== "keys") clearRevealedKeys();
  // 离开充值页就停掉支付轮询与二维码，避免后台空转和收款码久留在页面上。
  if (currentView === "billing" && view !== "billing") hideManualPayPanel();
  currentView = view;
  setGlobalPage(view === "models" ? "models" : "console");
  el("appShell").classList.toggle("models-layout", view === "models");
  el("dashboardView").classList.toggle("hidden", view !== "dashboard");
  el("adminView").classList.toggle("hidden", view !== "admin");
  el("teamView").classList.toggle("hidden", view !== "team");
  el("departmentView").classList.toggle("hidden", view !== "department");
  el("customersView").classList.toggle("hidden", view !== "customers");
  el("organizationView").classList.toggle("hidden", view !== "organization");
  el("keysView").classList.toggle("hidden", view !== "keys");
  el("billingView").classList.toggle("hidden", view !== "billing");
  el("modelsView").classList.toggle("hidden", view !== "models");
  el("dashboardFilters").classList.toggle("hidden", view === "models" || view === "keys" || view === "billing" || view === "customers" || view === "organization");
  renderCustomerUsageBreadcrumbs(view);
  const isCustomerDetailView = isViewingCustomerOrganization()
    && ["organization", "admin", "department"].includes(view);
  let activeButton = null;
  document.querySelectorAll("[data-view]").forEach((button) => {
    // Customer detail is a child workspace of the customer directory, not a
    // second top-level destination in the sidebar.
    const isActive = isCustomerDetailView
      ? button.dataset.view === "customers"
      : button.dataset.view === view;
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
  if (view === "billing") {
    renderBilling();
    if (!isBillingLoading) loadBillingData();
  }
  if (view === "dashboard" && !usageData.length) loadDashboardData();
  if (view === "customers" && !isCustomerOrganizationsLoading) loadCustomerOrganizations();
  if (view === "admin") {
    renderAdminBilling();
    if (!adminUsageData.length || adminUsageScopeKey !== organizationUsageScopeKey()) loadAdminData();
    if (adminBillingVisible() && !adminRedemptions.length && !adminBillingOrders.length) loadAdminBillingData();
  }
  if (view === "team" && currentUser?.isTeamLeader && !teamUsageData.length) loadTeamData();
  if (view === "department" && (!departmentUsageData.length || departmentUsageScopeKey !== organizationUsageScopeKey())) loadDepartmentData();
  if (view === "organization" && !organizationSnapshot && !isOrganizationLoading) loadOrganizationData();
}

async function loadCurrentViewData(forceRefresh = false) {
  if (currentView === "customers") return loadCustomerOrganizations();
  if (currentView === "keys") return loadKeys();
  if (currentView === "billing") return loadBillingData();
  if (currentView === "models") return loadModels();
  if (currentView === "admin") return loadAdminData(forceRefresh);
  if (currentView === "team") return loadTeamData(forceRefresh);
  if (currentView === "department") return loadDepartmentData(forceRefresh);
  if (currentView === "organization") return loadOrganizationData();
  return loadDashboardData(forceRefresh);
}

async function loadDashboardData(forceRefresh = false) {
  if (!currentUser || isDashboardLoading) return;
  if (accountAccessCopy(currentUser)) {
    renderAccountAccessState();
    return;
  }
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
  const scopeKey = organizationUsageScopeKey();
  if (!canViewAdminUsage() || (isAdminLoading && adminUsageLoadingScopeKey === scopeKey)) return;
  const requestId = ++adminUsageRequestId;
  isAdminLoading = true;
  adminUsageLoadingScopeKey = scopeKey;
  renderAdmin();
  const { startDate, endDate } = selectedDateRange();
  const source = el("sourceSelect").value;
  const search = el("adminEmployeeSearch").value.trim();
  const employee = selectedAdminEmployee || search;
  const query = new URLSearchParams({ start_date: startDate, end_date: endDate, source });
  if (employee) query.set("employee", employee);
  if (forceRefresh) query.set("refresh", "1");
  try {
    const scope = organizationUsageScope();
    const usagePath = scope?.usagePath || "/api/admin/usage";
    const payload = await api(`${usagePath}?${query.toString()}`);
    if (requestId !== adminUsageRequestId || scopeKey !== organizationUsageScopeKey()) return;
    adminUsageData = payload.rows || [];
    adminSummaryData = payload.summaryRows || adminUsageData;
    adminEmployees = payload.employees || [];
    adminDataFreshness = payload.dataFreshness || null;
    adminUsageScopeKey = scopeKey;
    lastAdminUsageCacheHit = Boolean(payload.cache?.hit);
    if (payload.truncated) {
      el("adminLimitHint").textContent = `${RANKING_SORT_TIP}；日志读取达到上限（已读 ${payload.pagesRead || 0}/${payload.totalPages || "?"} 页），员工排行可能不完整`;
    } else {
      el("adminLimitHint").textContent = `${RANKING_SORT_TIP}；已读取 ${payload.pagesRead || 0} 页日志，按当前筛选范围统计`;
    }
  } catch (error) {
    if (requestId !== adminUsageRequestId || scopeKey !== organizationUsageScopeKey()) return;
    showToast(error.message || "全员数据加载失败");
    adminUsageData = [];
    adminSummaryData = [];
    adminEmployees = [];
  } finally {
    if (requestId !== adminUsageRequestId) return;
    isAdminLoading = false;
    adminUsageLoadingScopeKey = "";
    renderAdmin();
  }
}

async function loadDepartmentData(forceRefresh = false) {
  const scopeKey = organizationUsageScopeKey();
  if (!canViewDepartmentUsage() || (isDepartmentLoading && departmentUsageLoadingScopeKey === scopeKey)) return;
  const requestId = ++departmentUsageRequestId;
  isDepartmentLoading = true;
  departmentUsageLoadingScopeKey = scopeKey;
  renderDepartment();
  const { startDate, endDate } = selectedDateRange();
  const source = el("sourceSelect").value;
  const search = el("departmentEmployeeSearch").value.trim();
  const department = selectedDepartment || search;
  const query = new URLSearchParams({ start_date: startDate, end_date: endDate, source });
  if (department) query.set("department", department);
  if (forceRefresh) query.set("refresh", "1");
  try {
    const scope = organizationUsageScope();
    const usagePath = scope?.departmentsUsagePath || "/api/admin/departments/usage";
    const payload = await api(`${usagePath}?${query.toString()}`);
    if (requestId !== departmentUsageRequestId || scopeKey !== organizationUsageScopeKey()) return;
    departmentUsageData = payload.rows || [];
    departmentSummaryData = payload.summaryRows || departmentUsageData;
    departmentRankings = payload.departments || [];
    departmentEmployees = payload.employees || [];
    departmentDataFreshness = payload.dataFreshness || null;
    departmentUsageScopeKey = scopeKey;
    if (!department) departmentPickerOptions = departmentRankings;
    lastDepartmentUsageCacheHit = Boolean(payload.cache?.hit);
    const rankingSubject = selectedDepartment ? "员工排行" : "部门排行";
    if (payload.truncated) {
      el("departmentLimitHint").textContent = `${rankingSubject}${RANKING_SORT_TIP}；日志读取达到上限（已读 ${payload.pagesRead || 0}/${payload.totalPages || "?"} 页），排行可能不完整`;
    } else {
      el("departmentLimitHint").textContent = `${rankingSubject}${RANKING_SORT_TIP}；已读取 ${payload.pagesRead || 0} 页日志，按当前筛选范围统计`;
    }
  } catch (error) {
    if (requestId !== departmentUsageRequestId || scopeKey !== organizationUsageScopeKey()) return;
    showToast(error.message || "部门数据加载失败");
    departmentUsageData = [];
    departmentSummaryData = [];
    departmentRankings = [];
    departmentEmployees = [];
    if (!department) departmentPickerOptions = [];
  } finally {
    if (requestId !== departmentUsageRequestId) return;
    isDepartmentLoading = false;
    departmentUsageLoadingScopeKey = "";
    renderDepartment();
  }
}

async function loadTeamRankingData(forceRefresh = false) {
  if (!currentUser?.isTeamLeader || !leaderTeams.length) return;
  ensureSelectedTeamRef();
  const requestId = ++teamRankingRequestId;
  if (teamRankingRequestController) teamRankingRequestController.abort();
  teamRankingRequestController = new AbortController();
  isTeamRankingLoading = true;
  teamRankingError = "";
  renderTeam();

  const { startDate, endDate } = selectedDateRange();
  const source = el("sourceSelect").value;
  const query = new URLSearchParams({
    start_date: startDate,
    end_date: endDate,
    source,
    include_member_rankings: "true",
  });
  if (selectedTeamRef) query.set("team_ref", selectedTeamRef);
  if (forceRefresh) query.set("refresh", "1");
  const cacheKey = `${selectedTeamRef}|${startDate}|${endDate}|${source}`;
  const cached = !forceRefresh ? teamUsagePayloadCache.get(cacheKey) : null;

  try {
    const payload = cached || await api(`/api/team/usage?${query.toString()}`, { signal: teamRankingRequestController.signal });
    if (requestId !== teamRankingRequestId) return;
    applyTeamUsagePayload(payload, cacheKey);
    setTeamRankingHint(payload);
  } catch (error) {
    if (error.name === "AbortError") return;
    if (requestId !== teamRankingRequestId) return;
    teamEmployees = [];
    teamRankingError = "成员排行加载失败，请重试";
    teamRankingHint = "";
    setText("teamLimitHint", teamRankingError);
    showToast(error.message || "团队成员排行加载失败");
  } finally {
    if (requestId === teamRankingRequestId) {
      isTeamRankingLoading = false;
      renderTeam();
    }
  }
}

async function loadTeamData(forceRefresh = false) {
  if (!currentUser?.isTeamLeader || !leaderTeams.length) return;
  ensureSelectedTeamRef();
  resetTeamMemberSelection();
  const requestId = ++teamUsageRequestId;
  if (teamUsageRequestController) teamUsageRequestController.abort();
  if (teamRankingRequestController) teamRankingRequestController.abort();
  teamRankingRequestId += 1;
  isTeamRankingLoading = false;
  teamUsageRequestController = new AbortController();
  teamUsageData = [];
  teamSummaryData = [];
  teamEmployees = [];
  teamRankingError = "";
  teamRankingHint = "";
  isTeamLoading = true;
  renderTeam();

  const { startDate, endDate } = selectedDateRange();
  const source = el("sourceSelect").value;
  const query = new URLSearchParams({
    start_date: startDate,
    end_date: endDate,
    source,
    include_member_rankings: "false",
  });
  if (selectedTeamRef) query.set("team_ref", selectedTeamRef);
  if (forceRefresh) query.set("refresh", "1");
  const cacheKey = `${selectedTeamRef}|${startDate}|${endDate}|${source}`;
  const cached = !forceRefresh ? teamUsagePayloadCache.get(cacheKey) : null;

  if (cached) {
    applyTeamUsagePayload(cached, cacheKey);
    setTeamRankingHint(cached);
    isTeamLoading = false;
    renderTeam();
    return;
  }

  try {
    // 先加载团队摘要，再独立补齐成员排行，避免首屏被成员聚合阻塞。
    const payload = await api(`/api/team/usage?${query.toString()}`, { signal: teamUsageRequestController.signal });
    if (requestId !== teamUsageRequestId) return;
    applyTeamUsagePayload(payload);
    isTeamLoading = false;
    renderTeam();
    // 摘要请求已刷新并缓存同一批排名数据，排行请求直接复用，避免重复 SQL。
    await loadTeamRankingData(false);
  } catch (error) {
    if (error.name === "AbortError") return;
    if (requestId !== teamUsageRequestId) return;
    showToast(error.message || "团队数据加载失败");
    teamUsageData = [];
    teamSummaryData = [];
    teamEmployees = [];
  } finally {
    if (requestId === teamUsageRequestId) {
      isTeamLoading = false;
      renderTeam();
    }
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
  // Customer demo identities intentionally have no model-catalog contract.
  // Do not make a forbidden request merely because an older navigation event
  // tries to prefetch the global page.
  if (isMockCustomerIdentity()) {
    modelCatalog = [];
    setupModelFilters();
    renderModels();
    return;
  }
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
  clearResetPasswordToken();
  currentUser = normalizeAuthUser(user);
  if (user?.csrfToken) authCsrfToken = user.csrfToken;
  if (currentUser?.csrfToken) authCsrfToken = currentUser.csrfToken;
  leaderTeams = normalizeLeaderTeams(currentUser);
  selectedTeamRef = currentUser.team?.teamRef || leaderTeams[0]?.teamRef || "";
  resetTeamMemberSelection();
  ensureSelectedTeamRef();
  el("authLoadingView").classList.add("hidden");
  el("landingView").classList.add("hidden");
  el("loginView").classList.add("hidden");
  el("appView").classList.remove("hidden");
  el("teamTab").classList.add("hidden");
  el("billingTab").classList.add("hidden");
  syncNavigationVisibility();
  el("userEmail").textContent = currentUser.email;
  el("userName").textContent = currentUser.name || currentUser.email;
  el("avatar").textContent = currentUser.avatar || initials(currentUser.email, currentUser.name);
  el("teamWelcomeTitle").textContent = `所选范围 · ${teamScopeLabel()}`;
  el("departmentWelcomeTitle").textContent = "所选范围 · 全部部门";
  switchView("dashboard");
  render();
  const isDemoCustomer = isMockCustomerIdentity();
  if (isDemoCustomer) {
    // The Mock customer path is deliberately limited to personal, team and
    // organization usage. Billing and model catalog requests would touch
    // seller-only integrations and are rejected by the API.
    billingAvailable = false;
    modelCatalog = [];
    const scopePromise = loadAuthScope();
    await Promise.all([loadCurrentViewData(), scopePromise]);
    return;
  }
  // 充值入口要在权限受限时也可用——新用户正是靠充值开通，不能被这道 return 拦掉。
  const billingPromise = refreshBillingAvailability();
  if (accountAccessCopy(currentUser)) {
    await billingPromise;
    return;
  }
  const scopePromise = loadAuthScope();
  await billingPromise;
  await Promise.all([loadCurrentViewData(), loadModels()]);
  await scopePromise;
}

async function loadAuthScope() {
  try {
    const scope = await api("/api/auth/scope");
    Object.assign(currentUser, scope);
    leaderTeams = normalizeLeaderTeams(currentUser);
    selectedTeamRef = currentUser.team?.teamRef || leaderTeams[0]?.teamRef || "";
    el("teamTab").classList.toggle("hidden", !currentUser.isTeamLeader);
    syncNavigationVisibility();
    el("teamWelcomeTitle").textContent = `所选范围 · ${teamScopeLabel()}`;
    // isAdmin 到这里才确定，充值管理面板的可见性随之更新。
    renderAdminBilling();
    render();
  } catch (error) {
    showToast("部分权限信息加载失败，请刷新重试");
  }
}

function showLogin() {
  currentUser = null;
  authSessionGeneration += 1;
  authCsrfToken = "";
  csrfRefreshPromise = null;
  isSsoRedirecting = false;
  selectedAdminEmployee = "";
  selectedDepartment = "";
  departmentPickerOpen = false;
  usageData = [];
  usageSummary = null;
  adminUsageData = [];
  adminSummaryData = [];
  adminEmployees = [];
  adminUsageScopeKey = "";
  adminUsageLoadingScopeKey = "";
  adminUsageRequestId += 1;
  departmentUsageData = [];
  departmentSummaryData = [];
  departmentRankings = [];
  departmentEmployees = [];
  departmentUsageScopeKey = "";
  departmentUsageLoadingScopeKey = "";
  departmentUsageRequestId += 1;
  teamUsageData = [];
  teamSummaryData = [];
  teamEmployees = [];
  teamMemberUsageData = [];
  teamMemberUsageSummary = null;
  // 换账号时清空充值状态，避免上一个账号的余额与订单残留在页面上。
  stopTopupPolling();
  billingConfig = null;
  billingAccount = null;
  billingOrders = [];
  billingOrderTotal = 0;
  billingAvailable = false;
  billingLoadError = "";
  selectedTopupAmount = 0;
  pendingTopupTradeNo = "";
  el("billingPayPanel")?.classList.add("hidden");
  adminRedemptions = [];
  adminRedemptionTotal = 0;
  adminBillingOrders = [];
  adminBillingReviews = [];
  adminBillingPendingSync = 0;
  adminBillingPendingReview = 0;
  adminBillingKeyword = "";
  if (teamUsageRequestController) teamUsageRequestController.abort();
  if (teamRankingRequestController) teamRankingRequestController.abort();
  teamUsageRequestId += 1;
  teamRankingRequestId += 1;
  teamUsageRequestController = null;
  teamRankingRequestController = null;
  isTeamLoading = false;
  isTeamRankingLoading = false;
  teamRankingError = "";
  teamRankingHint = "";
  teamUsagePayloadCache.clear();
  resetTeamMemberSelection();
  teamInfo = null;
  leaderTeams = [];
  selectedTeamRef = "";
  departmentPickerOptions = [];
  organizationSnapshot = null;
  organizationMembers = [];
  organizationMemberTotal = 0;
  organizationMemberPage = 1;
  organizationMemberFilters = { search: "", departmentId: "", role: "", status: "" };
  isOrganizationLoading = false;
  isOrganizationMemberLoading = false;
  organizationDataLoadingScopeKey = "";
  organizationMemberLoadingScopeKey = "";
  organizationDataRequestId += 1;
  organizationMemberRequestId += 1;
  isOrganizationDepartmentSaving = false;
  isOrganizationMemberSaving = false;
  editingOrganizationDepartmentId = "";
  editingOrganizationMemberId = "";
  customerOrganizations = [];
  customerOrganizationsTotal = 0;
  customerOrganizationsPage = 1;
  customerOrganizationsFilters = { search: "", status: "" };
  selectedCustomerOrganization = null;
  customerOrganizationDetailTab = "info";
  editingCustomerOrganizationId = "";
  window.clearTimeout(customerOrganizationsSearchTimer);
  el("customersTab").classList.add("hidden");
  el("organizationDepartmentModal").classList.add("hidden");
  el("organizationMemberModal").classList.add("hidden");
  el("customerOrganizationModal").classList.add("hidden");
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
  el("authLoadingView").classList.add("hidden");
  updateHomeCard();
  setAuthAccess(passwordLoginAvailable() ? "personal" : "enterprise");
  setAuthMode(resetPasswordToken ? "reset" : "login", { focus: false });
  // 带重置密码 token 时直接停在登录页的重置态，其余情况（含退出登录）回到营销首页。
  if (resetPasswordToken) {
    el("landingView").classList.add("hidden");
    el("loginView").classList.remove("hidden");
    setGlobalPage("");
    return;
  }
  showLanding();
}

async function submitPasswordLogin() {
  if (!passwordLoginAvailable()) {
    setAuthStatus(configuredLocalAuthMessage("邮箱密码登录暂未启用。"), "error");
    return;
  }
  if (!validateAuthMode("login") || !requireTurnstile("login")) return;
  const values = authFieldValues();
  const button = el("passwordLoginButton");
  authSubmitting = true;
  setButtonLoading(button, true, "正在登录");
  try {
    await ensureCsrfToken();
    const payload = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        email: values.loginEmail,
        password: values.loginPassword,
        turnstileToken: turnstileTokens.login || undefined,
      }),
    });
    await showApp(payload);
  } catch (error) {
    setAuthStatus(error.message || "登录失败，请检查邮箱和密码。", "error");
    resetTurnstile("login");
  } finally {
    authSubmitting = false;
    setButtonLoading(button, false);
  }
}

async function submitRegistration() {
  if (!publicSignupAvailable()) {
    setAuthStatus(configuredSignupMessage("公开注册暂未开放。"), "error");
    return;
  }
  if (!validateAuthMode("register") || !requireTurnstile("register")) return;
  const values = authFieldValues();
  const button = el("registerButton");
  authSubmitting = true;
  setButtonLoading(button, true, "正在创建");
  try {
    await ensureCsrfToken();
    await api("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({
        email: values.registerEmail,
        name: values.registerName,
        password: values.registerPassword,
        verificationCode: values.verificationCode,
        turnstileToken: turnstileTokens.register || undefined,
      }),
    });
    el("loginEmailInput").value = values.registerEmail;
    el("loginPasswordInput").value = "";
    setAuthMode("login");
    setAuthStatus("账号已创建，请使用刚刚设置的密码登录；充值或由管理员开通后方可使用模型和额度。", "success");
    resetTurnstile("register");
  } catch (error) {
    setAuthStatus(error.message || "账号创建失败，请检查填写内容后重试。", "error");
    resetTurnstile("register");
  } finally {
    authSubmitting = false;
    setButtonLoading(button, false);
  }
}

async function submitForgotPassword() {
  if (!passwordRecoveryAvailable()) {
    setAuthStatus(configuredPasswordRecoveryMessage("密码找回暂未开放。"), "error");
    return;
  }
  if (!validateAuthMode("forgot") || !requireTurnstile("forgot")) return;
  const values = authFieldValues();
  const button = el("forgotSubmitButton");
  authSubmitting = true;
  setButtonLoading(button, true, "正在发送");
  try {
    await ensureCsrfToken();
    await api("/api/auth/password/forgot", {
      method: "POST",
      body: JSON.stringify({ email: values.forgotEmail, turnstileToken: turnstileTokens.forgot || undefined }),
    });
    setAuthStatus("如果该邮箱已注册，你会收到一封重置密码邮件。请同时检查垃圾邮件。", "success");
    resetTurnstile("forgot");
  } catch (error) {
    setAuthStatus(error.message || "重置邮件发送失败，请稍后重试。", "error");
    resetTurnstile("forgot");
  } finally {
    authSubmitting = false;
    setButtonLoading(button, false);
  }
}

async function submitPasswordReset() {
  if (!validateAuthMode("reset")) return;
  const values = authFieldValues();
  const button = el("resetPasswordButton");
  authSubmitting = true;
  setButtonLoading(button, true, "正在更新");
  try {
    await ensureCsrfToken();
    await api("/api/auth/password/reset", {
      method: "POST",
      body: JSON.stringify({ token: resetPasswordToken, newPassword: values.resetPassword }),
    });
    clearResetPasswordToken();
    setAuthMode("login");
    setAuthStatus("密码已更新，请使用新密码登录。", "success");
  } catch (error) {
    if (error.code === "AUTH_RESET_TOKEN_INVALID") {
      clearResetPasswordToken();
      setAuthMode("forgot");
      setAuthStatus("重置链接已失效，请重新申请。", "error");
    } else {
      setAuthStatus(error.message || "密码更新失败，重置链接可能已经失效。", "error");
    }
  } finally {
    authSubmitting = false;
    setButtonLoading(button, false);
  }
}

async function sendRegistrationCode() {
  clearAuthErrors();
  if (!publicSignupAvailable()) {
    setAuthStatus(configuredSignupMessage("公开注册暂未开放。"), "error");
    return;
  }
  const email = el("registerEmailInput").value.trim().toLowerCase();
  if (!validEmail(email)) {
    fieldError("registerEmailInput", "请先输入有效的邮箱地址");
    el("registerEmailInput").focus();
    return;
  }
  if (!signupEmailAllowed(email)) {
    fieldError("registerEmailInput", `当前仅支持 ${formatSignupDomains()} 邮箱注册`);
    el("registerEmailInput").focus();
    return;
  }
  const button = el("sendRegisterCodeButton");
  setButtonLoading(button, true, "正在发送");
  try {
    await ensureCsrfToken();
    if (!requireTurnstile("register")) {
      setButtonLoading(button, false);
      return;
    }
    const payload = await api("/api/auth/verification/request", {
      method: "POST",
      body: JSON.stringify({ email, purpose: "signup", turnstileToken: turnstileTokens.register || undefined }),
    });
    setVerificationCountdown(Math.min(Number(payload?.expiresIn) || 60, 60));
    setAuthStatus(payload?.message || "验证码已发送，请检查邮箱。", "success");
    resetTurnstile("register");
  } catch (error) {
    setAuthStatus(error.message || "验证码发送失败，请稍后重试。", "error");
    resetTurnstile("register");
  } finally {
    if (!verificationTimer) setButtonLoading(button, false);
  }
}

async function devLogin() {
  const email = el("emailInput").value.trim();
  if (!validEmail(email)) {
    showToast("请输入有效的开发登录邮箱");
    el("emailInput").focus();
    return;
  }
  setButtonLoading("devLoginButton", true, "正在登录");
  try {
    await ensureCsrfToken();
    const user = await api("/api/auth/dev-login", { method: "POST", body: JSON.stringify({ email }) });
    await showApp(user);
  } catch (error) {
    showToast(error.message || "登录失败，请确认账号是否存在");
  } finally {
    setButtonLoading("devLoginButton", false);
  }
}

document.addEventListener("submit", async (event) => {
  if (event.target.id !== "loginForm") return;
  event.preventDefault();
  if (currentUser) {
    showAuthenticatedPage("console");
    return;
  }
  if (authSubmitting || authAccess !== "personal") return;
  if (authMode === "register") await submitRegistration();
  else if (authMode === "forgot") await submitForgotPassword();
  else if (authMode === "reset") await submitPasswordReset();
  else await submitPasswordLogin();
});

document.querySelectorAll("[data-auth-access]").forEach((button) => {
  button.addEventListener("click", () => setAuthAccess(button.dataset.authAccess));
});

document.querySelectorAll("[data-auth-mode]").forEach((button) => {
  button.addEventListener("click", () => setAuthMode(button.dataset.authMode));
});

document.querySelectorAll('[role="tablist"]').forEach((tablist) => {
  tablist.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = [...tablist.querySelectorAll('[role="tab"]')].filter((tab) => !tab.hidden && !tab.classList.contains("hidden") && !tab.disabled);
    if (!tabs.length) return;
    const currentIndex = Math.max(0, tabs.indexOf(document.activeElement));
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? tabs.length - 1
        : (currentIndex + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    event.preventDefault();
    tabs[nextIndex].focus();
    tabs[nextIndex].click();
  });
});

document.querySelectorAll("[data-auth-back]").forEach((button) => {
  button.addEventListener("click", () => setAuthMode(button.dataset.authBack));
});

document.querySelectorAll("[data-password-toggle]").forEach((button) => {
  button.addEventListener("click", () => {
    const input = el(button.dataset.passwordToggle);
    const isVisible = input.type === "text";
    input.type = isVisible ? "password" : "text";
    button.setAttribute("aria-pressed", String(!isVisible));
    button.setAttribute("aria-label", isVisible ? "显示密码" : "隐藏密码");
    button.querySelector("use").setAttribute("href", isVisible ? "#icon-eye" : "#icon-eye-off");
  });
});

el("forgotPasswordButton").addEventListener("click", () => {
  el("forgotEmailInput").value = el("loginEmailInput").value;
  setAuthMode("forgot");
});
el("sendRegisterCodeButton").addEventListener("click", sendRegistrationCode);
el("devLoginButton").addEventListener("click", devLogin);
el("enterConsoleButton").addEventListener("click", () => showAuthenticatedPage("console"));

el("ssoButton").addEventListener("click", () => {
  if (currentUser) showAuthenticatedPage("console");
  else startSsoLogin();
});

el("logoutButton").addEventListener("click", async () => {
  setButtonLoading("logoutButton", true, "正在退出");
  try {
    // Dev-login sessions do not return a CSRF token; initialize one before logout.
    await ensureCsrfToken();
    await api("/api/auth/logout", { method: "POST", body: JSON.stringify({}) });
    clearResetPasswordToken();
    showLogin();
  } catch (error) {
    showToast(error.message || "退出登录失败，请检查网络后重试");
  } finally {
    setButtonLoading("logoutButton", false);
  }
});

el("authLoadingRetryButton").addEventListener("click", () => window.location.reload());

el("accountAccessRetryButton").addEventListener("click", async () => {
  setButtonLoading("accountAccessRetryButton", true, "正在检查");
  try {
    const user = await api("/api/auth/me");
    await showApp(user);
  } catch (error) {
    showToast(error.message || "账号状态检查失败，请稍后重试");
  } finally {
    setButtonLoading("accountAccessRetryButton", false);
  }
});

el("accountAccessTopupButton").addEventListener("click", () => switchView("billing"));
el("createCustomerOrganizationButton").addEventListener("click", () => openCustomerOrganizationModal());
el("resetCustomerOrganizationsDemoButton").addEventListener("click", resetCustomerOrganizationsDemo);
el("cancelCustomerOrganizationButton").addEventListener("click", closeCustomerOrganizationModal);
el("backToCustomersButton").addEventListener("click", closeCustomerOrganization);
el("backToCustomerOrganizationButton").addEventListener("click", () => showOrganizationUsage("info"));
el("backToCustomerOrganizationDepartmentButton").addEventListener("click", () => showOrganizationUsage("info"));
el("createOrganizationDepartmentButton").addEventListener("click", () => openOrganizationDepartmentModal());
el("inviteOrganizationMemberButton").addEventListener("click", () => openOrganizationMemberModal());
el("cancelOrganizationDepartmentButton").addEventListener("click", closeOrganizationDepartmentModal);
el("cancelOrganizationMemberButton").addEventListener("click", closeOrganizationMemberModal);

el("customerOrganizationForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (isCustomerOrganizationSaving || !customerOrganizationsAvailable()) return;
  const name = el("customerOrganizationNameInput").value.trim();
  const ownerName = el("customerOrganizationOwnerNameInput").value.trim();
  const ownerEmail = el("customerOrganizationOwnerEmailInput").value.trim();
  if (!name || !ownerName) {
    showToast("请填写企业名称和首位企业主姓名");
    return;
  }
  if (!validEmail(ownerEmail)) {
    showToast("请输入有效的首位企业主邮箱");
    el("customerOrganizationOwnerEmailInput").focus();
    return;
  }
  const isEditing = Boolean(editingCustomerOrganizationId);
  isCustomerOrganizationSaving = true;
  setButtonLoading("submitCustomerOrganizationButton", true, isEditing ? "保存中" : "创建中");
  try {
    await ensureCsrfToken();
    const payload = await api(
      isEditing ? customerOrganizationPath(editingCustomerOrganizationId) : "/api/platform/organizations",
      {
        method: isEditing ? "PATCH" : "POST",
        body: JSON.stringify(isEditing ? { name } : { name, ownerName, ownerEmail }),
      },
    );
    const created = payload?.organization || payload;
    const createdId = customerOrganizationId(created);
    closeCustomerOrganizationModal({ force: true });
    customerOrganizationsPage = 1;
    await loadCustomerOrganizations();
    showToast(isEditing ? "客户企业名称已更新" : "客户企业已创建");
    if (!isEditing && createdId) await openCustomerOrganization(createdId);
  } catch (error) {
    showToast(error.message || (isEditing ? "客户企业更新失败" : "客户企业创建失败"));
  } finally {
    isCustomerOrganizationSaving = false;
    setButtonLoading("submitCustomerOrganizationButton", false);
  }
});

el("customerOrganizationGrid").addEventListener("click", (event) => {
  const openButton = event.target.closest("[data-customer-organization-open]");
  if (openButton) {
    openCustomerOrganization(openButton.dataset.customerOrganizationOpen);
    return;
  }
  const editButton = event.target.closest("[data-customer-organization-edit]");
  if (editButton) {
    openCustomerOrganizationModal(editButton.dataset.customerOrganizationEdit);
    return;
  }
  const archiveButton = event.target.closest("[data-customer-organization-archive]");
  if (archiveButton) archiveCustomerOrganization(archiveButton.dataset.customerOrganizationArchive);
});

el("customerOrganizationSearch").addEventListener("input", () => {
  window.clearTimeout(customerOrganizationsSearchTimer);
  customerOrganizationsSearchTimer = window.setTimeout(() => {
    customerOrganizationsFilters = {
      ...customerOrganizationsFilters,
      search: el("customerOrganizationSearch").value.trim(),
    };
    customerOrganizationsPage = 1;
    loadCustomerOrganizations();
  }, 260);
});

el("customerOrganizationStatusFilter").addEventListener("change", () => {
  customerOrganizationsFilters = {
    ...customerOrganizationsFilters,
    status: el("customerOrganizationStatusFilter").value,
  };
  customerOrganizationsPage = 1;
  loadCustomerOrganizations();
});

el("resetCustomerOrganizationFiltersButton").addEventListener("click", () => {
  customerOrganizationsFilters = { search: "", status: "" };
  customerOrganizationsPage = 1;
  renderCustomerOrganizationFilters();
  loadCustomerOrganizations();
});

el("customerOrganizationPreviousPageButton").addEventListener("click", () => {
  if (customerOrganizationsPage <= 1) return;
  customerOrganizationsPage -= 1;
  loadCustomerOrganizations();
});

el("customerOrganizationNextPageButton").addEventListener("click", () => {
  if (customerOrganizationsPage * customerOrganizationsPageSize >= customerOrganizationsTotal) return;
  customerOrganizationsPage += 1;
  loadCustomerOrganizations();
});

el("organizationDepartmentForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (isOrganizationDepartmentSaving || !organizationCanManage()) return;
  const name = el("organizationDepartmentNameInput").value.trim();
  if (!name) {
    showToast("请输入部门名称");
    el("organizationDepartmentNameInput").focus();
    return;
  }
  const isEditing = Boolean(editingOrganizationDepartmentId);
  isOrganizationDepartmentSaving = true;
  setButtonLoading("submitOrganizationDepartmentButton", true, isEditing ? "保存中" : "创建中");
  try {
    await ensureCsrfToken();
    await api(
      isEditing
        ? organizationApiPath(`/departments/${encodeURIComponent(editingOrganizationDepartmentId)}`)
        : organizationApiPath("/departments"),
      {
        method: isEditing ? "PATCH" : "POST",
        body: JSON.stringify({ name }),
      },
    );
    closeOrganizationDepartmentModal({ force: true });
    await loadOrganizationData();
    showToast(isEditing ? "部门名称已更新" : "部门已创建");
  } catch (error) {
    showToast(error.message || (isEditing ? "部门更新失败" : "部门创建失败"));
  } finally {
    isOrganizationDepartmentSaving = false;
    setButtonLoading("submitOrganizationDepartmentButton", false);
  }
});

el("organizationMemberForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (isOrganizationMemberSaving || !organizationCanManage()) return;
  const name = el("organizationMemberNameInput").value.trim();
  const email = el("organizationMemberEmailInput").value.trim();
  const departmentId = el("organizationMemberDepartmentInput").value;
  const role = el("organizationMemberRoleInput").value;
  const status = el("organizationMemberStatusInput").value;
  if (!name || !departmentId) {
    showToast("请填写姓名并选择部门");
    return;
  }
  if (!editingOrganizationMemberId && !validEmail(email)) {
    showToast("请输入有效的工作邮箱");
    el("organizationMemberEmailInput").focus();
    return;
  }
  const isEditing = Boolean(editingOrganizationMemberId);
  isOrganizationMemberSaving = true;
  setButtonLoading("submitOrganizationMemberButton", true, isEditing ? "保存中" : "邀请中");
  try {
    await ensureCsrfToken();
    const body = isEditing
      ? { name, departmentId, role, status }
      : { name, email, departmentId, role };
    await api(
      isEditing
        ? organizationApiPath(`/members/${encodeURIComponent(editingOrganizationMemberId)}`)
        : organizationApiPath("/members"),
      { method: isEditing ? "PATCH" : "POST", body: JSON.stringify(body) },
    );
    closeOrganizationMemberModal({ force: true });
    organizationMemberPage = 1;
    await loadOrganizationData();
    showToast(isEditing ? "成员信息已更新" : "成员邀请已创建");
  } catch (error) {
    showToast(error.message || (isEditing ? "成员更新失败" : "成员邀请失败"));
  } finally {
    isOrganizationMemberSaving = false;
    setButtonLoading("submitOrganizationMemberButton", false);
  }
});

el("organizationDepartmentList").addEventListener("click", (event) => {
  const editButton = event.target.closest("[data-organization-department-edit]");
  if (editButton) {
    openOrganizationDepartmentModal(editButton.dataset.organizationDepartmentEdit);
    return;
  }
  const archiveButton = event.target.closest("[data-organization-department-archive]");
  if (archiveButton) archiveOrganizationDepartment(archiveButton.dataset.organizationDepartmentArchive);
});

el("organizationMemberTable").addEventListener("click", (event) => {
  const editButton = event.target.closest("[data-organization-member-edit]");
  if (editButton) {
    openOrganizationMemberModal(editButton.dataset.organizationMemberEdit);
    return;
  }
  const statusButton = event.target.closest("[data-organization-member-status]");
  if (statusButton) {
    updateOrganizationMemberStatus(
      statusButton.dataset.organizationMemberStatus,
      statusButton.dataset.organizationMemberNextStatus,
    );
  }
});

el("organizationMemberSearch").addEventListener("input", () => {
  window.clearTimeout(organizationSearchTimer);
  organizationSearchTimer = window.setTimeout(() => {
    organizationMemberFilters.search = el("organizationMemberSearch").value.trim();
    organizationMemberPage = 1;
    loadOrganizationMembers();
  }, 260);
});

["organizationDepartmentFilter", "organizationRoleFilter", "organizationStatusFilter"].forEach((id) => {
  el(id).addEventListener("change", () => {
    organizationMemberFilters = {
      ...organizationMemberFilters,
      departmentId: el("organizationDepartmentFilter").value,
      role: el("organizationRoleFilter").value,
      status: el("organizationStatusFilter").value,
    };
    organizationMemberPage = 1;
    loadOrganizationMembers();
  });
});

el("resetOrganizationMemberFiltersButton").addEventListener("click", () => {
  organizationMemberFilters = { search: "", departmentId: "", role: "", status: "" };
  organizationMemberPage = 1;
  renderOrganizationFilters();
  loadOrganizationMembers();
});

el("organizationPreviousPageButton").addEventListener("click", () => {
  if (organizationMemberPage <= 1) return;
  organizationMemberPage -= 1;
  loadOrganizationMembers();
});

el("organizationNextPageButton").addEventListener("click", () => {
  if (organizationMemberPage * organizationMemberPageSize >= organizationMemberTotal) return;
  organizationMemberPage += 1;
  loadOrganizationMembers();
});

el("resetOrganizationDemoButton").addEventListener("click", async () => {
  if (!organizationCanManage() || !window.confirm("重置后会清除本次演示中的部门与成员变更，并恢复初始样例。确定继续吗？")) return;
  setButtonLoading("resetOrganizationDemoButton", true, "重置中");
  try {
    await ensureCsrfToken();
    await api("/api/organization/current/demo/reset", { method: "POST", body: JSON.stringify({}) });
    organizationMemberPage = 1;
    organizationMemberFilters = { search: "", departmentId: "", role: "", status: "" };
    await loadOrganizationData();
    showToast("演示数据已重置");
  } catch (error) {
    showToast(error.message || "演示数据重置失败");
  } finally {
    setButtonLoading("resetOrganizationDemoButton", false);
  }
});

document.querySelectorAll("[data-organization-usage-view]").forEach((button) => {
  button.addEventListener("click", () => showOrganizationUsage(button.dataset.organizationUsageView));
});

el("adminRedemptionForm").addEventListener("submit", generateRedemptions);
el("adminBillingRetrySync").addEventListener("click", retryBillingSync);
el("adminBillingSearchButton").addEventListener("click", () => {
  adminBillingKeyword = String(el("adminBillingSearch")?.value || "").trim();
  loadAdminBillingData();
});
el("adminBillingSearch").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  adminBillingKeyword = String(el("adminBillingSearch")?.value || "").trim();
  loadAdminBillingData();
});
el("adminBillingSection").addEventListener("click", (event) => {
  const disableTarget = event.target.closest("[data-disable-redemption]");
  if (disableTarget) {
    disableRedemption(disableTarget.dataset.disableRedemption);
    return;
  }
  const completeTarget = event.target.closest("[data-complete-order]");
  if (completeTarget) {
    completeBillingOrder(completeTarget.dataset.completeOrder);
    return;
  }
  const rejectTarget = event.target.closest("[data-reject-order]");
  if (rejectTarget) {
    rejectBillingOrder(rejectTarget.dataset.rejectOrder);
    return;
  }
  if (event.target.closest("[data-copy-codes]")) {
    copyText(el("adminRedemptionResult")?.dataset.codes || "", "兑换码已复制");
  }
});
el("topupForm").addEventListener("submit", submitTopup);
el("manualPayForm").addEventListener("submit", submitManualPayment);
el("billingPayCancel").addEventListener("click", hideManualPayPanel);
el("topupAmount").addEventListener("input", () => {
  // 手输金额与快选档位互斥，输入后取消高亮。
  selectedTopupAmount = 0;
  renderTopupOptions();
  updateTopupPayable();
  setFieldError("topupError", "");
});
el("topupOptions").addEventListener("click", (event) => {
  const option = event.target.closest("[data-topup-amount]");
  if (!option) return;
  selectedTopupAmount = Number(option.dataset.topupAmount || 0);
  const amountInput = el("topupAmount");
  if (amountInput) amountInput.value = String(selectedTopupAmount);
  renderTopupOptions();
  updateTopupPayable();
  setFieldError("topupError", "");
});

document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
document.querySelectorAll("[data-global-page]").forEach((item) => item.addEventListener("click", () => navigateGlobalPage(item.dataset.globalPage)));
document.querySelectorAll("[data-auth-entry]").forEach((item) => item.addEventListener("click", () => showAuthPage(item.dataset.authEntry)));

// 首页 Hero 主按钮：已登录进控制台，未登录按是否开放注册决定落在注册或登录。
el("landingPrimaryCta").addEventListener("click", () => {
  if (currentUser) {
    showAuthenticatedPage("console");
    return;
  }
  showAuthPage(publicSignupAvailable() ? "register" : "login");
});

el("landingLogoutButton").addEventListener("click", async () => {
  setButtonLoading("landingLogoutButton", true, "正在退出");
  try {
    await ensureCsrfToken();
    await api("/api/auth/logout", { method: "POST", body: JSON.stringify({}) });
    clearResetPasswordToken();
    showLogin();
  } catch (error) {
    showToast(error.message || "退出登录失败，请检查网络后重试");
  } finally {
    setButtonLoading("landingLogoutButton", false);
  }
});

async function reloadForFilterChange() {
  // 保留当前下钻选择:切换时间范围/来源时应停留在已下钻的员工/成员/部门,
  // 而不是退回聚合看板。各 load 函数已把选择变量透传给后端查询。
  if (currentView === "team") {
    // 成员下钻时同时刷新个人明细和团队排行，且保留当前成员选择。
    if (selectedTeamEmployee) {
      await Promise.all([
        loadTeamMemberData(selectedTeamEmployee, false, false),
        loadTeamRankingData(false),
      ]);
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
      await Promise.all([
        loadTeamMemberData(selectedTeamEmployee, true, false),
        loadTeamRankingData(true),
      ]);
      showToast("已刷新成员明细");
    } else {
      await loadTeamData(true);
      showToast("已刷新团队用量");
    }
  } else if (currentView === "department") {
    await loadDepartmentData(true);
    showToast("已刷新部门用量");
  } else if (currentView === "organization") {
    await loadOrganizationData();
    showToast("已刷新企业组织");
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

setupRankingSorting("adminUserTable", renderAdminUsers);
setupRankingSorting("departmentUserTable", renderDepartmentUsers);
setupRankingSorting("teamUserTable", renderTeamUsers);

el("teamBackButton").addEventListener("click", clearTeamMemberSelection);

["teamMemberUsageDetailDateFilter", "teamMemberUsageDetailModelFilter", "teamMemberUsageDetailStatusFilter"].forEach((id) => {
  el(id).addEventListener("change", updateTeamMemberUsageFilters);
});
el("teamMemberUsageDetailSearch").addEventListener("input", updateTeamMemberUsageFilters);
el("teamMemberUsageDetailReset").addEventListener("click", resetTeamMemberUsageFilters);

el("modelSearch").addEventListener("input", renderModels);
el("providerFilter").addEventListener("change", renderModels);
el("billingFilter").addEventListener("change", renderModels);
el("modelsView").addEventListener("click", (event) => {
  const viewButton = event.target.closest("[data-model-view]");
  if (viewButton) {
    setModelViewMode(viewButton.dataset.modelView);
    return;
  }
  const copyButton = event.target.closest("[data-copy-model]");
  if (copyButton) copyText(copyButton.dataset.copyModel, "模型名称已复制");
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
    await ensureCsrfToken();
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
    await ensureCsrfToken();
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
    await ensureCsrfToken();
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
    await ensureCsrfToken();
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
    if (backdrop.id === "customerOrganizationModal") closeCustomerOrganizationModal();
    if (backdrop.id === "organizationDepartmentModal") closeOrganizationDepartmentModal();
    if (backdrop.id === "organizationMemberModal") closeOrganizationMemberModal();
  });
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!el("newKeyModal").classList.contains("hidden")) clearPlainKey();
  else if (!el("deleteKeyModal").classList.contains("hidden")) closeDeleteKeyModal();
  else if (!el("regenerateKeyModal").classList.contains("hidden")) closeRegenerateKeyModal();
  else if (!el("createKeyModal").classList.contains("hidden")) closeCreateKeyModal();
  else if (!el("customerOrganizationModal").classList.contains("hidden")) closeCustomerOrganizationModal();
  else if (!el("organizationMemberModal").classList.contains("hidden")) closeOrganizationMemberModal();
  else if (!el("organizationDepartmentModal").classList.contains("hidden")) closeOrganizationDepartmentModal();
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearRevealedKeys();
});

window.addEventListener("beforeunload", clearRevealedKeys);

async function init() {
  const callbackParams = new URLSearchParams(window.location.search);
  resetPasswordToken = takeResetPasswordTokenFromUrl(callbackParams);
  const hasAuthCallback = callbackParams.get("auth_callback") === "success";
  if (hasAuthCallback) {
    el("landingView").classList.add("hidden");
    el("loginView").classList.add("hidden");
    el("authLoadingView").classList.remove("hidden");
    callbackParams.delete("auth_callback");
    replaceCurrentQuery(callbackParams);
  }
  let configError = null;
  try {
    authConfig = await api("/api/auth/config");
  } catch (error) {
    configError = error;
    authConfig = {
      devLoginEnabled: false,
      oidcConfigured: false,
      providerName: "飞书扫码登录",
      passwordLoginEnabled: false,
      publicSignupEnabled: false,
      emailVerificationRequired: true,
      turnstileEnabled: false,
      turnstileConfigured: false,
      turnstileSiteKey: "",
      passwordLoginConfigured: false,
      passwordLoginAvailable: false,
      passwordLoginUnavailableCode: "AUTH_PASSWORD_LOGIN_DISABLED",
      passwordLoginUnavailableReason: "邮箱密码登录暂未开放。",
      publicSignupConfigured: false,
      publicSignupAvailable: false,
      publicSignupUnavailableCode: "AUTH_SIGNUP_DISABLED",
      publicSignupUnavailableReason: "邮箱注册暂未开放。",
      passwordRecoveryEnabled: false,
      passwordRecoveryConfigured: false,
      passwordRecoveryAvailable: false,
      passwordRecoveryUnavailableCode: "AUTH_PASSWORD_LOGIN_DISABLED",
      passwordRecoveryUnavailableReason: "密码找回暂未开放。",
      allowedSignupDomains: [],
    };
  }
  setupModelFilters();
  try {
    const user = await api("/api/auth/me");
    await showApp(user);
  } catch (meError) {
    if (meError?.status !== 401) {
      el("authLoadingView").classList.remove("hidden");
      el("landingView").classList.add("hidden");
      el("loginView").classList.add("hidden");
      el("authLoadingHint").textContent = meError?.message || "账号状态检查失败，请检查网络后重新加载。";
      el("authLoadingRetryButton").classList.remove("hidden");
      return;
    }
    if (configError) {
      el("authLoadingView").classList.remove("hidden");
      el("landingView").classList.add("hidden");
      el("loginView").classList.add("hidden");
      el("authLoadingHint").textContent = configError.message || "登录配置加载失败，请检查网络后重新加载。";
      el("authLoadingRetryButton").classList.remove("hidden");
      return;
    }
    el("authLoadingView").classList.add("hidden");
    showLogin();
    showLoginCallbackMessage();
    if (resetPasswordToken) {
      setAuthAccess("personal");
      setAuthMode("reset", { focus: false });
    }
  }
}

init();
