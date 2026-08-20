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
let personalDataQuality = null;
let personalCoverage = null;
let adminDataFreshness = null;
let adminDataQuality = null;
let adminCoverage = null;
let departmentDataFreshness = null;
let departmentDataQuality = null;
let departmentCoverage = null;
let teamDataFreshness = null;
let teamDataQuality = null;
let teamCoverage = null;
let teamMemberDataQuality = null;
let teamMemberCoverage = null;
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
let selectedDepartmentEmployee = "";
let selectedDepartmentEmployeeSnapshot = null;
let departmentEmployeeUsageFilters = { date: "all", model: "all", status: "all", keyword: "" };
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
let modelCatalogRequest = null;
let modelViewMode = "card";
let personalKeys = [];
let availableKeyModels = [];
let unrestrictedKeyModels = false;
const PERSONAL_KEY_CACHE_TTL_MS = 30_000;
const PERSONAL_KEY_CACHE_PREFIX = "tongqu:personal-keys:v1:";
const CACHEABLE_PERSONAL_KEY_FIELDS = [
  "id",
  "keyType",
  "name",
  "purpose",
  "masked",
  "models",
  "createdAt",
  "lastUsed",
  "expiresAt",
  "monthTokens",
  "spend",
  "status",
  "revealable",
  "cleanupRequired",
  "recoveryRequired",
  "oldKeyId",
  "replacementKeyId",
];
let hasLoadedPersonalKeys = false;
let personalKeysLoadedAt = 0;
let isKeysLoading = false;
let keyLoadError = "";
let keyRefreshError = "";
let keyListRequest = null;
let keyRefreshRequest = null;
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
let teamMemberKeys = [];
let teamKeyTeams = [];
let selectedTeamKeyRef = "";
let teamKeyFilters = { search: "", status: "all" };
let isTeamKeysLoading = false;
let teamKeyLoadError = "";
let teamKeyRequestId = 0;
let revokingTeamKeyId = "";
let deletingTeamKeyId = "";
let teamKeySearchTimer = null;
let isTeamKeyRevoking = false;
let isTeamKeyDeleting = false;
let isDashboardLoading = false;
let dashboardRequestController = null;
let dashboardRequestId = 0;
let dashboardRequestKey = "";
let dashboardInFlight = null;
let isAdminLoading = false;
let adminUsageRequestController = null;
let adminUsageQueryKey = "";
let adminUsageInFlight = null;
let isDepartmentLoading = false;
let departmentUsageRequestController = null;
let departmentUsageQueryKey = "";
let departmentUsageInFlight = null;
let isTeamLoading = false;
let isTeamMemberLoading = false;
let teamMemberUsageRequestController = null;
let teamMemberUsageQueryKey = "";
let teamMemberUsageInFlight = null;
let usageAutoRefreshTimer = null;
let usageAutoRefreshPromise = null;
let lastUsageAutoRefreshAt = 0;
let organizationSnapshot = null;
let organizationMembers = [];
let organizationMemberTotal = 0;
let organizationMemberPage = 1;
const organizationMemberPageSize = 20;
let organizationMemberFilters = { search: "", departmentId: "", role: "", status: "" };
let isOrganizationLoading = false;
let isOrganizationMemberLoading = false;
let organizationLoadError = "";
let organizationMemberLoadError = "";
let organizationDataLoadingScopeKey = "";
let organizationMemberLoadingScopeKey = "";
let organizationDataRequestId = 0;
let organizationMemberRequestId = 0;
let isOrganizationDepartmentSaving = false;
let isOrganizationMemberSaving = false;
let editingOrganizationDepartmentId = "";
let editingOrganizationMemberId = "";
let organizationMemberIdentityId = "";
let organizationMemberIdentity = null;
let isOrganizationMemberIdentityBusy = false;
let organizationSearchTimer = null;
let customerOrganizations = [];
let customerOrganizationsTotal = 0;
let customerOrganizationsPage = 1;
const customerOrganizationsPageSize = 12;
let customerOrganizationsFilters = { search: "", status: "" };
let isCustomerOrganizationsLoading = false;
let customerOrganizationsLoadError = "";
let customerOrganizationsSearchTimer = null;
let pendingAdoptionOrganizations = [];
let pendingAdoptionUnavailable = false;
let selectedCustomerOrganization = null;
let customerOrganizationDetailTab = "info";
let isCustomerOrganizationSaving = false;
let editingCustomerOrganizationId = "";
let organizationClaims = [];
let organizationClaimLoadError = "";
let isOrganizationClaimLoading = false;
let isOrganizationClaimSaving = false;
let organizationClaimLastUrl = "";
// Adoption preview data is deliberately ephemeral. In particular, the
// preview fingerprint is never written to localStorage or the URL.
let organizationAdoptionPreview = null;
let organizationAdoptionFingerprint = "";
let organizationAdoptionIdempotencyKey = "";
let organizationAdoptionLoadError = "";
let isOrganizationAdoptionLoading = false;
let isOrganizationAdoptionApplying = false;
let isSsoRedirecting = false;
let billingConfig = null;
let billingAccount = null;
let billingOrders = [];
let billingOrderTotal = 0;
let isBillingLoading = false;
let billingRequest = null;
let billingLoadedAt = 0;
const BILLING_CACHE_TTL_MS = 10_000;
let isCreatingTopup = false;
let isSubmittingManualPay = false;
let billingLoadError = "";
let billingAvailable = false;
let stabilityOverview = null;
let costOverview = null;
let costBudgets = [];
let isStabilityLoading = false;
let isCostOverviewLoading = false;
let stabilityLoadError = "";
let costOverviewLoadError = "";
let stabilityOverviewRequestId = 0;
let costOverviewRequestId = 0;
let stabilityOverviewController = null;
let costOverviewController = null;
let stabilityScenarioRequestId = 0;
let costLedgerRequestId = 0;
let selectedCostModelSeries = "";
let costModelShareReturnFocus = null;
let stabilityDrawerReturnFocus = null;
let costDrawerReturnFocus = null;
let stabilityScenarioState = {
  page: 1,
  pageSize: 20,
  total: 0,
  items: [],
  filters: { model: "", scenario: "", errorCode: "" },
  modelOptions: [],
  loading: false,
  error: "",
};
const observabilityDetailCache = new Map();
let costLedgerState = {
  page: 1,
  pageSize: 20,
  total: 0,
  items: [],
  filters: {},
  loading: false,
  error: "",
  selectedId: "",
};
let costFiltersOpen = false;
let governanceWorkbenchTab = "actual-ledger";
let governanceWorkbenchData = { planVersions: [], savingsMeasurements: [] };
let governanceWorkbenchLoading = false;
let governanceWorkbenchLoadError = "";
// 登录身份落地后立即揭示已知入口；异步权限结果随后只补充团队/企业入口。
let isNavigationRevealed = false;
let selectedTopupAmount = 0;
let pendingTopupTradeNo = "";
let topupPollTimer = null;
// Enterprise demo billing is intentionally separate from the legacy personal
// billing account. It never falls through to the personal payment endpoints.
let organizationBillingData = null;
let organizationBillingLoading = false;
let organizationBillingLoadError = "";
let organizationBillingPage = 1;
const organizationBillingPageSize = 20;
let organizationBillingScopeKey = "";
let organizationBillingRequestId = 0;
let organizationBillingRequest = null;
let organizationBillingLoadedAt = 0;
const ORGANIZATION_BILLING_CACHE_TTL_MS = 10_000;
let isOrganizationTopupSaving = false;
let selectedOrganizationTopupAmount = 0;
let isOrganizationCreditAdjusting = false;
let organizationCreditAdjustmentOperation = "grant";
// Customer-scoped access tokens. These live entirely inside the enterprise
// workspace and never reuse the personal access-key state above.
let organizationTokens = [];
let organizationTokenTotal = 0;
let organizationTokenStats = null;
let organizationTokenPage = 1;
const organizationTokenPageSize = 20;
let organizationTokenFilters = { search: "", status: "" };
let organizationTokenModels = [];
let organizationTokenBindableMembers = [];
let isOrganizationTokenLoading = false;
let isOrganizationTokenSaving = false;
let isOrganizationTokenRevoking = false;
let isOrganizationTokenDeleting = false;
let organizationTokenLoadError = "";
let organizationTokenLoadErrorCode = "";
let organizationTokenScopeKey = "";
let organizationTokenRequestId = 0;
let organizationTokenSearchTimer = null;
let revokingOrganizationTokenId = "";
let deletingOrganizationTokenId = "";
let authConfig = {
  devLoginEnabled: false,
  remoteDemoReadOnly: false,
  remoteDemoUsageSnapshotOnly: false,
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
let organizationInvitationToken = "";
let organizationInvitation = null;
let organizationInvitationExistingAccount = false;
let organizationInvitationLoading = false;
let organizationInvitationAccepting = false;
let organizationClaimToken = "";
let organizationClaim = null;
let organizationClaimLoading = false;
let organizationClaimAccepting = false;
let verificationCountdown = 0;
let verificationTimer = null;
let turnstileLoadPromise = null;
const turnstileTokens = { login: "", register: "", forgot: "", organizationClaim: "" };
const turnstileWidgets = { login: null, register: null, forgot: null, organizationClaim: null };
const turnstileRenderPromises = { login: null, register: null, forgot: null, organizationClaim: null };

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

const AVATAR_TONE_COUNT = 5;

function avatarTone(seed) {
  const text = String(seed || "").trim().toLowerCase();
  if (!text) return "tone-1";
  let total = 0;
  for (let index = 0; index < text.length; index += 1) {
    total = (total * 31 + text.charCodeAt(index)) % 9973;
  }
  return `tone-${(total % AVATAR_TONE_COUNT) + 1}`;
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
  const {
    timeoutMs = 15_000,
    signal: callerSignal,
    ...fetchOptions
  } = options;
  const headers = { "Content-Type": "application/json", ...(fetchOptions.headers || {}) };
  if (requestCsrfToken) headers["X-CSRF-Token"] = requestCsrfToken;
  const requestController = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => requestController.abort(callerSignal?.reason);
  if (callerSignal?.aborted) abortFromCaller();
  else callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  const timeoutId = Number(timeoutMs) > 0
    ? globalThis.setTimeout(() => {
        timedOut = true;
        requestController.abort();
      }, Number(timeoutMs))
    : null;
  let response;
  try {
    response = await fetch(path, {
      credentials: "same-origin",
      ...fetchOptions,
      headers,
      signal: requestController.signal,
    });
  } catch (error) {
    if (timedOut) {
      const timeoutError = new Error("请求超时，请稍后重试");
      timeoutError.name = "TimeoutError";
      timeoutError.code = "REQUEST_TIMEOUT";
      throw timeoutError;
    }
    throw error;
  } finally {
    if (timeoutId !== null) globalThis.clearTimeout(timeoutId);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
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

function authDisplayIdentifier(user = currentUser) {
  if (!user) return "";
  return String(
    user.displayIdentifier
      || user.display_identifier
      || user.loginName
      || user.login_name
      || user.contactEmail
      || user.email
      || "",
  ).trim();
}

function authContactEmail(user = currentUser) {
  if (!user) return "";
  return String(user.contactEmail || user.contact_email || user.email || "").trim();
}

// The organization mode is server-owned. Keep the legacy demo flag as a
// fallback so a rolling deploy can upgrade the browser before the API bundle.
function organizationMode(user = currentUser) {
  const configured = String(user?.organizationMode || "").trim().toLowerCase();
  if (["disabled", "demo", "real"].includes(configured)) return configured;
  if (user?.organizationDemoEnabled) return "demo";
  return user?.organizationEnabled ? "real" : "disabled";
}

function organizationEnabled(user = currentUser) {
  return organizationMode(user) !== "disabled";
}

function isDemoOrganizationMode(user = currentUser) {
  return organizationMode(user) === "demo";
}

function isRealOrganizationMode(user = currentUser) {
  return organizationMode(user) === "real";
}

function isKnownOrganizationIdentity(user = currentUser) {
  // The generic capability is authoritative. The demo-named fallback only
  // keeps older API bundles usable during a rolling deploy.
  return Boolean(user?.isKnownOrganizationIdentity ?? user?.isKnownDemoCustomerIdentity);
}

function isOrganizationCustomerIdentity(user = currentUser) {
  return Boolean(
    organizationEnabled(user)
    && (user?.organizationRole || isKnownOrganizationIdentity(user)),
  );
}

function syncOrganizationDemoChrome() {
  const demo = isDemoOrganizationMode();
  const realDescription = "企业账号、邀请与权限变更会写入正式业务数据。";
  document.querySelectorAll("[data-organization-demo-badge]").forEach((badge) => {
    badge.classList.toggle("hidden", !demo);
  });
  const resetButton = el("resetCustomerOrganizationsDemoButton");
  if (resetButton) resetButton.classList.toggle("hidden", !demo);
  const topupModal = el("organizationTopupModal");
  if (topupModal && !demo) {
    topupModal.classList.add("hidden");
    closeOrganizationTopupModal({ force: true });
  }
  const description = el("customersDescription");
  if (description) {
    description.textContent = demo
      ? "为每家客户企业维护独立的部门、成员和访问状态。当前数据仅用于演示，不会创建真实账号或发送邮件。"
      : `为每家客户企业维护独立的部门、成员和访问状态。${realDescription}`;
  }
  // Real customers receive credit from platform operations; only demo mode
  // enables the local simulation form.
  const topupButton = el("openOrganizationTopupModalButton");
  if (topupButton && !demo) topupButton.classList.add("hidden");
  setButtonLabel("openOrganizationTopupModalButton", demo ? "模拟充值" : "联系平台运营授信");
  setText("organizationTopupModalTitle", demo ? "模拟充值" : "企业额度由平台运营维护");
  const topupDescription = document.querySelector("#organizationTopupModal form > p");
  if (topupDescription) {
    topupDescription.textContent = demo
      ? "立即为当前企业增加演示额度，不会调用支付、收款、邮件或任何真实充值服务。"
      : "真实模式不提供客户自助充值，请联系平台运营人员授予或调整企业额度。";
  }
  const topupOptions = el("organizationTopupOptions");
  if (topupOptions) topupOptions.setAttribute("aria-label", demo ? "模拟充值快速金额" : "企业额度维护说明");
  const topupAmount = el("organizationTopupAmount");
  if (topupAmount) {
    topupAmount.placeholder = demo ? "请输入模拟充值金额" : "请联系平台运营授信";
    topupAmount.disabled = !demo;
  }
  const topupNote = document.querySelector("#organizationTopupModal .organization-modal-note");
  if (topupNote) {
    topupNote.textContent = demo
      ? "金额范围为 $1.00 至 $100,000.00，最多两位小数。提交成功后余额和记录会立即刷新。"
      : "真实模式下，企业额度由平台运营依据合同、订单或审批结果维护。";
  }
  const topupSubmit = el("submitOrganizationTopupButton");
  setButtonLabel("submitOrganizationTopupButton", demo ? "确认模拟充值" : "联系平台运营授信");
  if (topupSubmit) topupSubmit.disabled = !demo;
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

function takeOrganizationInvitationTokenFromUrl(params = new URLSearchParams(window.location.search)) {
  const token = String(params.get("organization_invitation") || "").trim();
  const hadSensitiveParam = params.has("organization_invitation");
  params.delete("organization_invitation");
  if (hadSensitiveParam) replaceCurrentQuery(params);
  return token;
}

function takeOrganizationClaimTokenFromUrl(params = new URLSearchParams(window.location.search)) {
  const token = String(params.get("organization_claim") || "").trim();
  const hadSensitiveParam = params.has("organization_claim");
  params.delete("organization_claim");
  if (hadSensitiveParam) replaceCurrentQuery(params);
  return token;
}

function clearOrganizationInvitationSecret() {
  organizationInvitationToken = "";
  el("organizationInvitationPassword").value = "";
  el("organizationInvitationConfirmPassword").value = "";
}

function clearOrganizationClaimSecret() {
  organizationClaimToken = "";
  const password = el("organizationClaimPassword");
  const confirm = el("organizationClaimConfirmPassword");
  if (password) password.value = "";
  if (confirm) confirm.value = "";
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

// 自定义范围只允许落在后台回填窗口内，避免用户选到根本没有快照的区间。
const CUSTOM_RANGE_MAX_DAYS = 90;
let customDateRange = null;
let lastPresetRangeValue = "1";
let stabilityCustomDateRange = null;
let costCustomDateRange = null;

function daysBetween(startDate, endDate) {
  // 用 UTC 解析 YYYY-MM-DD 求含首尾天数，绕开本地时区与夏令时带来的偏移。
  const toUtc = (value) => {
    const [year, month, day] = String(value).split("-").map(Number);
    return Date.UTC(year, (month || 1) - 1, day || 1);
  };
  const span = Math.round((toUtc(endDate) - toUtc(startDate)) / 86400000) + 1;
  return span > 0 ? span : 1;
}

function customRangeBounds() {
  const today = new Date();
  const earliest = new Date(today);
  earliest.setDate(today.getDate() - (CUSTOM_RANGE_MAX_DAYS - 1));
  return { min: localDate(earliest), max: localDate(today) };
}

function isCustomRangeActive() {
  return el("rangeSelect").value === "custom" && Boolean(customDateRange);
}

function selectedDateRange() {
  if (isCustomRangeActive()) {
    const { startDate, endDate } = customDateRange;
    return { startDate, endDate, days: daysBetween(startDate, endDate) };
  }
  // 弹层打开但尚未应用时仍按上一个预设取数，避免出现无意义的中间态。
  const days = Number(el("rangeSelect").value) || Number(lastPresetRangeValue) || 30;
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
  if (!freshness) return "数据更新时间：最近一次同步";
  if (freshness.verifiedThrough) {
    const verified = new Date(freshness.verifiedThrough);
    if (!Number.isNaN(verified.getTime())) {
      const label = freshness.settlementState === "settled" ? "已核验截至" : "数据核验中，已核验截至";
      return `${label}：${verified.toLocaleString("zh-CN", { hour12: false })}`;
    }
  }
  if (!freshness.lastSyncedAt) return "数据更新时间：暂未同步";
  const parsed = new Date(freshness.lastSyncedAt);
  if (Number.isNaN(parsed.getTime())) return "数据更新时间：未知";
  const timeText = parsed.toLocaleString("zh-CN", { hour12: false });
  const prefix = freshness.source === "realtime" ? "数据核验中" : "数据更新时间";
  return `${freshness.stale || freshness.degraded ? `${prefix}（同步延迟）` : prefix}：${timeText}`;
}

function usageStatusState(freshness = null, dataQuality = null, coverage = null) {
  const quality = dataQuality && typeof dataQuality === "object" ? dataQuality : {};
  const range = coverage && typeof coverage === "object" ? coverage : {};
  if (quality.snapshotUnavailable) {
    return {
      tone: "danger",
      title: "同步快照暂不可用",
      description: "当前展示的是可用汇总，成员排行和明细可能缺失。请稍后刷新后再核对。",
    };
  }
  if (range.complete === false) {
    return {
      tone: "warning",
      title: "数据覆盖不完整",
      description: "所选日期范围尚未全部同步，当前合计和排行可能低于实际用量。",
    };
  }
  const departmentSnapshot = quality.departmentSnapshot || {};
  if (departmentSnapshot.source === "latest_before_end_date" || departmentSnapshot.source === "mixed") {
    const dateText = departmentSnapshot.latestFallbackDate || departmentSnapshot.latestDate || "最近一次";
    return {
      tone: "warning",
      title: "部门归属使用最近目录快照",
      description: `所选范围内的部分成员目录尚未同步，部门列暂按 ${dateText} 的最近有效快照展示。`,
    };
  }
  return null;
}

function renderUsageStatus(containerId, freshness = null, dataQuality = null, coverage = null) {
  const container = el(containerId);
  if (!container) return;
  const state = usageStatusState(freshness, dataQuality, coverage);
  container.classList.toggle("hidden", !state);
  if (!state) {
    container.innerHTML = "";
    return;
  }
  container.className = `operational-status ${state.tone}`;
  container.innerHTML = `
    <span class="operational-status-icon" aria-hidden="true">${icon("warning")}</span>
    <div><strong>${escapeHtml(state.title)}</strong><p>${escapeHtml(state.description)}</p></div>
  `;
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

  if (passwordEnabled && ssoEnabled) el("guestAuthDescription").textContent = "使用邮箱、企业账号或统一认证进入控制台。";
  else if (passwordEnabled) el("guestAuthDescription").textContent = "使用邮箱或企业账号进入控制台。";
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
    setAuthAvailabilityNotice("安全验证尚未正确配置，密码登录与注册暂时不可用，请联系管理员。", "error");
  } else if (passwordEnabled && !signupEnabled && !recoveryEnabled) {
    setAuthAvailabilityNotice(`密码登录仍可用。${signupReason || "邮箱注册暂未开放。"}${recoveryReason || "密码找回暂不可用。"}`, "info");
  } else if (passwordEnabled && !signupEnabled) {
    setAuthAvailabilityNotice(`密码登录和密码找回仍可用。${signupReason || "邮箱注册暂未开放。"}`, "info");
  } else if (passwordEnabled && !recoveryEnabled) {
    setAuthAvailabilityNotice(`密码登录仍可用。${recoveryReason || "密码找回暂不可用。"}`, "info");
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
    loginIdentifier: el("loginEmailInput").value.trim(),
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
    if (values.loginIdentifier.length < 2) reject("loginEmailInput", "请输入邮箱或企业账号");
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
  const identityStatus = String(user?.identityStatus || user?.identity_status || "").trim().toLowerCase();
  if (["pending_approval", "accepted_pending_approval"].includes(identityStatus)) {
    return {
      title: "企业账号认领待审核",
      description: "账号密码已设置，平台运营审核通过后才能访问企业数据和令牌。",
      retry: true,
    };
  }
  if (["revoked", "rejected"].includes(identityStatus)) {
    return {
      title: "企业账号认领未通过",
      description: "当前认领申请已撤销或未通过，请联系平台运营人员重新核对账号归属。",
      retry: false,
    };
  }
  const organizationAccessStatus = String(user?.organizationAccessStatus || "");
  if (isOrganizationCustomerIdentity(user)) {
    const demoCopy = isDemoOrganizationMode(user);
    if (organizationAccessStatus === "invited") {
      return {
        title: "企业邀请等待启用",
        description: demoCopy
          ? "你的企业演示邀请尚未启用，暂时不能查看用量或使用企业资源。"
          : "你的企业邀请尚未启用，暂时不能查看用量或使用企业资源。",
        retry: false,
      };
    }
    if (organizationAccessStatus === "suspended") {
      return {
        title: "企业访问已暂停",
        description: demoCopy
          ? "你的企业演示访问已暂停，请联系企业管理员或平台运营人员确认后续安排。"
          : "你的企业访问已暂停，请联系企业管理员或平台运营人员确认后续安排。",
        retry: false,
      };
    }
    if (["archived", "organization_suspended"].includes(organizationAccessStatus)) {
      return {
        title: "所属客户企业暂不可用",
        description: demoCopy
          ? "所属客户企业已归档或暂停，当前不能访问企业演示数据。"
          : "所属客户企业已归档或暂停，当前不能访问企业资源。",
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
  el("accountAccessTopupButton").classList.toggle(
    "hidden",
    !(state.topup && billingAvailable && !isOrganizationCustomerIdentity()),
  );
}

function updateHomeCard() {
  const isLoggedIn = Boolean(currentUser);
  el("authenticatedHome").classList.toggle("hidden", !isLoggedIn);
  el("authGuestContent").classList.toggle("hidden", isLoggedIn);
  if (isLoggedIn) {
    const identifier = authDisplayIdentifier();
    el("loginTitle").textContent = `欢迎回来，${currentUser.name || identifier}`;
    el("loginDescription").textContent = accountAccessCopy(currentUser)
      ? "账号已完成认证。进入控制台可查看当前开通状态。"
      : "你已完成账号认证，可以继续进入控制台查看个人 AI 用量。";
    el("loginHint").textContent = `当前登录账号：${identifier}`;
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
  el("landingUserEmail").textContent = isLoggedIn ? authDisplayIdentifier() : "";
  el("landingPrimaryCta").textContent = isLoggedIn ? "进入控制台" : "立即开始使用";
  el("landingGuestRegisterButton").classList.toggle("hidden", !publicSignupAvailable());
}

function showLanding() {
  if (organizationClaimToken) {
    showOrganizationClaimScreen();
    return;
  }
  if (organizationInvitationToken) {
    showOrganizationInvitationScreen();
    return;
  }
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
  if (organizationClaimToken) {
    el("organizationClaimScreen")?.classList.remove("hidden");
    el("organizationClaimScreen")?.setAttribute("aria-hidden", "false");
    el("organizationInvitationScreen")?.classList.add("hidden");
    el("organizationInvitationScreen")?.setAttribute("aria-hidden", "true");
    el("authAccessSwitch")?.classList.add("hidden");
    el("personalAuthPanel")?.classList.add("hidden");
    el("personalAuthPanel")?.setAttribute("aria-hidden", "true");
    el("enterpriseAuthPanel")?.classList.add("hidden");
    el("enterpriseAuthPanel")?.setAttribute("aria-hidden", "true");
    el("devLoginArea")?.classList.add("hidden");
    window.scrollTo({ top: 0, behavior: "smooth" });
    return;
  }
  if (organizationInvitationToken) {
    el("organizationInvitationScreen")?.classList.remove("hidden");
    el("organizationInvitationScreen")?.setAttribute("aria-hidden", "false");
    el("authAccessSwitch")?.classList.add("hidden");
    el("personalAuthPanel")?.classList.add("hidden");
    el("personalAuthPanel")?.setAttribute("aria-hidden", "true");
    el("enterpriseAuthPanel")?.classList.add("hidden");
    el("enterpriseAuthPanel")?.setAttribute("aria-hidden", "true");
    el("devLoginArea")?.classList.add("hidden");
    window.scrollTo({ top: 0, behavior: "smooth" });
    return;
  }
  el("organizationInvitationScreen")?.classList.add("hidden");
  el("organizationInvitationScreen")?.setAttribute("aria-hidden", "true");
  el("organizationClaimScreen")?.classList.add("hidden");
  el("organizationClaimScreen")?.setAttribute("aria-hidden", "true");
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

function setOrganizationInvitationError(message = "") {
  const node = el("organizationInvitationError");
  if (!node) return;
  node.textContent = message;
  node.className = `auth-status${message ? " show error" : ""}`;
}

function setOrganizationInvitationSuccess(message = "") {
  const node = el("organizationInvitationSuccess");
  if (!node) return;
  node.textContent = message;
  node.className = `auth-status${message ? " show success" : " success"}`;
}

function renderOrganizationInvitation() {
  const invitation = organizationInvitation || {};
  const existing = organizationInvitationExistingAccount;
  const organizationName = String(invitation.organizationName || "你的企业").trim();
  const email = String(invitation.email || invitation.contactEmail || "").trim().toLowerCase();
  setText("organizationInvitationOrganization", organizationName || "企业邀请");
  setText("organizationInvitationEmail", email ? `邀请邮箱：${email}` : "");
  setText(
    "organizationInvitationDescription",
    existing
      ? "确认后，这个邮箱会绑定到企业成员关系；原有密码和个人账号设置不会改变。"
      : "确认邀请并设置密码后，企业账号会进入开通流程。",
  );
  el("organizationInvitationExistingAccount")?.classList.toggle("hidden", !existing);
  el("organizationInvitationNewAccount")?.classList.toggle("hidden", existing);
  el("organizationInvitationPasswordFields")?.classList.toggle("hidden", existing);
  el("organizationInvitationAcceptButton")?.classList.toggle(
    "hidden",
    organizationInvitationLoading || organizationInvitationAccepting || !organizationInvitation,
  );
}

function showOrganizationInvitationScreen() {
  showAuthPage("login");
  el("organizationInvitationScreen")?.classList.remove("hidden");
  el("organizationInvitationScreen")?.setAttribute("aria-hidden", "false");
  el("authAccessSwitch")?.classList.add("hidden");
  el("personalAuthPanel")?.classList.add("hidden");
  el("personalAuthPanel")?.setAttribute("aria-hidden", "true");
  el("enterpriseAuthPanel")?.classList.add("hidden");
  el("enterpriseAuthPanel")?.setAttribute("aria-hidden", "true");
  el("devLoginArea")?.classList.add("hidden");
  setAuthStatus();
  renderOrganizationInvitation();
}

function resetOrganizationInvitationScreen() {
  organizationInvitation = null;
  organizationInvitationExistingAccount = false;
  organizationInvitationLoading = false;
  organizationInvitationAccepting = false;
  setOrganizationInvitationError("");
  setOrganizationInvitationSuccess("");
  fieldError("organizationInvitationPassword", "");
  fieldError("organizationInvitationConfirmPassword", "");
  el("organizationInvitationPasswordFields")?.classList.add("hidden");
  el("organizationInvitationExistingAccount")?.classList.add("hidden");
  el("organizationInvitationNewAccount")?.classList.add("hidden");
  el("organizationInvitationAcceptButton")?.classList.remove("hidden");
  el("organizationInvitationRetryButton")?.classList.add("hidden");
  el("organizationInvitationLoginButton")?.classList.add("hidden");
  el("organizationInvitationScreen")?.classList.add("hidden");
  el("organizationInvitationScreen")?.setAttribute("aria-hidden", "true");
  clearOrganizationInvitationSecret();
}

function closeOrganizationInvitationScreen() {
  if (organizationInvitationAccepting) return;
  clearOrganizationInvitationSecret();
  organizationInvitationToken = "";
  resetOrganizationInvitationScreen();
  showAuthPage("login");
}

async function verifyOrganizationInvitation() {
  if (!organizationInvitationToken || organizationInvitationLoading) return;
  organizationInvitationLoading = true;
  setOrganizationInvitationError("");
  setOrganizationInvitationSuccess("");
  el("organizationInvitationRetryButton")?.classList.add("hidden");
  el("organizationInvitationAcceptButton")?.classList.add("hidden");
  setText("organizationInvitationDescription", "正在验证邀请链接，请稍候。");
  try {
    const payload = await api(`/api/auth/invitations/${encodeURIComponent(organizationInvitationToken)}`);
    organizationInvitation = payload?.invitation || null;
    organizationInvitationExistingAccount = payload?.existingAccount !== undefined
      ? Boolean(payload.existingAccount)
      : payload?.passwordRequired !== undefined
        ? !Boolean(payload.passwordRequired)
        : false;
    if (!organizationInvitation) throw new Error("邀请链接无效、已过期或已被撤销");
    renderOrganizationInvitation();
    el("organizationInvitationAcceptButton")?.classList.remove("hidden");
  } catch (error) {
    organizationInvitation = null;
    organizationInvitationExistingAccount = false;
    setOrganizationInvitationError(error.message || "邀请链接验证失败，请稍后重试。");
    setText("organizationInvitationDescription", "无法确认这条邀请链接的状态。");
    if (error.code !== "ORGANIZATION_INVITATION_INVALID") {
      el("organizationInvitationRetryButton")?.classList.remove("hidden");
    }
  } finally {
    organizationInvitationLoading = false;
  }
}

function validateOrganizationInvitationPassword() {
  const password = String(el("organizationInvitationPassword")?.value || "");
  const confirmPassword = String(el("organizationInvitationConfirmPassword")?.value || "");
  fieldError("organizationInvitationPassword", "");
  fieldError("organizationInvitationConfirmPassword", "");
  if (password.length < 8) {
    fieldError("organizationInvitationPassword", "密码至少需要 8 个字符。");
    return null;
  }
  if (password.length > 128) {
    fieldError("organizationInvitationPassword", "密码不能超过 128 个字符。");
    return null;
  }
  if (password !== confirmPassword) {
    fieldError("organizationInvitationConfirmPassword", "两次输入的密码不一致。");
    return null;
  }
  return password;
}

async function acceptOrganizationInvitation() {
  if (!organizationInvitationToken || !organizationInvitation || organizationInvitationAccepting) return;
  let password;
  if (!organizationInvitationExistingAccount) {
    password = validateOrganizationInvitationPassword();
    if (!password) return;
  }
  organizationInvitationAccepting = true;
  setOrganizationInvitationError("");
  setOrganizationInvitationSuccess("");
  setButtonLoading("organizationInvitationAcceptButton", true, "正在开通");
  try {
    await ensureCsrfToken();
    const payload = await api("/api/auth/invitations/accept", {
      method: "POST",
      body: JSON.stringify({
        token: organizationInvitationToken,
        ...(password ? { password } : {}),
      }),
    });
    const email = String(organizationInvitation?.email || organizationInvitation?.contactEmail || "").trim().toLowerCase();
    clearOrganizationInvitationSecret();
    setText("organizationInvitationDescription", "邀请已确认，企业账号正在开通中。");
    setOrganizationInvitationSuccess(
      payload?.message || "账号开通中。完成企业资源配置后，请使用该邮箱登录。",
    );
    el("organizationInvitationAcceptButton")?.classList.add("hidden");
    el("organizationInvitationRetryButton")?.classList.add("hidden");
    el("organizationInvitationLoginButton")?.classList.remove("hidden");
    if (email) el("loginEmailInput").value = email;
    organizationInvitationToken = "";
  } catch (error) {
    if (error.code === "ORGANIZATION_INVITATION_PASSWORD_REQUIRED") {
      organizationInvitationExistingAccount = false;
      renderOrganizationInvitation();
      setOrganizationInvitationError("账号状态刚发生变化，请设置密码后继续。");
    } else {
      setOrganizationInvitationError(error.message || "邀请接受失败，请稍后重试。");
    }
  } finally {
    organizationInvitationAccepting = false;
    setButtonLoading("organizationInvitationAcceptButton", false);
  }
}

function finishOrganizationInvitationAndLogin() {
  const email = String(organizationInvitation?.email || organizationInvitation?.contactEmail || el("loginEmailInput")?.value || "").trim().toLowerCase();
  resetOrganizationInvitationScreen();
  if (email) el("loginEmailInput").value = email;
  showAuthPage("login");
  setAuthStatus("账号正在开通中，请稍后使用原密码登录。", "success");
}

function setOrganizationClaimError(message = "") {
  const node = el("organizationClaimError");
  if (!node) return;
  node.textContent = message;
  node.className = `auth-status${message ? " show error" : ""}`;
}

function setOrganizationClaimSuccess(message = "") {
  const node = el("organizationClaimSuccess");
  if (!node) return;
  node.textContent = message;
  node.className = `auth-status${message ? " show success" : " success"}`;
}

function renderOrganizationClaim() {
  const claim = organizationClaim || {};
  const organizationName = String(claim.organizationName || claim.organization?.name || "你的企业").trim();
  const loginName = String(claim.loginName || claim.login_name || "").trim();
  const memberName = String(claim.memberName || claim.member_name || claim.name || "").trim();
  setText("organizationClaimOrganization", organizationName || "企业账号认领");
  setText(
    "organizationClaimIdentity",
    [memberName, loginName ? `账号：${loginName}` : ""].filter(Boolean).join(" · "),
  );
  setText(
    "organizationClaimDescription",
    claim.status === "accepted_pending_approval"
      ? "认领申请已提交，正在等待平台运营审核。"
      : "核对账号信息并设置密码后，提交企业账号认领申请。",
  );
  el("organizationClaimAcceptButton")?.classList.toggle(
    "hidden",
    organizationClaimLoading || organizationClaimAccepting || !organizationClaim || claim.status === "accepted_pending_approval",
  );
}

function showOrganizationClaimScreen() {
  showAuthPage("login");
  el("organizationClaimScreen")?.classList.remove("hidden");
  el("organizationClaimScreen")?.setAttribute("aria-hidden", "false");
  el("authAccessSwitch")?.classList.add("hidden");
  el("personalAuthPanel")?.classList.add("hidden");
  el("personalAuthPanel")?.setAttribute("aria-hidden", "true");
  el("enterpriseAuthPanel")?.classList.add("hidden");
  el("enterpriseAuthPanel")?.setAttribute("aria-hidden", "true");
  el("devLoginArea")?.classList.add("hidden");
  setAuthStatus();
  renderOrganizationClaim();
  renderTurnstile("organizationClaim");
}

function resetOrganizationClaimScreen() {
  organizationClaim = null;
  organizationClaimLoading = false;
  organizationClaimAccepting = false;
  setOrganizationClaimError("");
  setOrganizationClaimSuccess("");
  fieldError("organizationClaimPassword", "");
  fieldError("organizationClaimConfirmPassword", "");
  el("organizationClaimAcceptButton")?.classList.remove("hidden");
  el("organizationClaimRetryButton")?.classList.add("hidden");
  el("organizationClaimLoginButton")?.classList.add("hidden");
  el("organizationClaimScreen")?.classList.add("hidden");
  el("organizationClaimScreen")?.setAttribute("aria-hidden", "true");
  resetTurnstile("organizationClaim");
  clearOrganizationClaimSecret();
}

function closeOrganizationClaimScreen() {
  if (organizationClaimAccepting) return;
  clearOrganizationClaimSecret();
  organizationClaimToken = "";
  resetOrganizationClaimScreen();
  showAuthPage("login");
}

async function verifyOrganizationClaim() {
  if (!organizationClaimToken || organizationClaimLoading) return;
  organizationClaimLoading = true;
  setOrganizationClaimError("");
  setOrganizationClaimSuccess("");
  el("organizationClaimRetryButton")?.classList.add("hidden");
  el("organizationClaimAcceptButton")?.classList.add("hidden");
  setText("organizationClaimDescription", "正在核对账号认领链接，请稍候。");
  try {
    const payload = await api(`/api/auth/organization-claims/${encodeURIComponent(organizationClaimToken)}`);
    organizationClaim = payload?.claim || payload || null;
    if (!organizationClaim) throw new Error("账号认领链接无效、已过期或已被撤销");
    renderOrganizationClaim();
  } catch (error) {
    organizationClaim = null;
    setOrganizationClaimError(error.message || "账号认领链接核对失败，请稍后重试。");
    setText("organizationClaimDescription", "无法确认这条账号认领链接的状态。");
    if (error.code !== "ORGANIZATION_CLAIM_INVALID") {
      el("organizationClaimRetryButton")?.classList.remove("hidden");
    }
  } finally {
    organizationClaimLoading = false;
  }
}

function validateOrganizationClaimPassword() {
  const password = String(el("organizationClaimPassword")?.value || "");
  const confirmPassword = String(el("organizationClaimConfirmPassword")?.value || "");
  fieldError("organizationClaimPassword", "");
  fieldError("organizationClaimConfirmPassword", "");
  if (password.length < 8) {
    fieldError("organizationClaimPassword", "密码至少需要 8 个字符。");
    return null;
  }
  if (password.length > 128) {
    fieldError("organizationClaimPassword", "密码不能超过 128 个字符。");
    return null;
  }
  if (password !== confirmPassword) {
    fieldError("organizationClaimConfirmPassword", "两次输入的密码不一致。");
    return null;
  }
  return password;
}

async function acceptOrganizationClaim() {
  if (!organizationClaimToken || !organizationClaim || organizationClaimAccepting) return;
  const password = validateOrganizationClaimPassword();
  if (!password || !requireTurnstile("organizationClaim")) return;
  organizationClaimAccepting = true;
  setOrganizationClaimError("");
  setOrganizationClaimSuccess("");
  setButtonLoading("organizationClaimAcceptButton", true, "正在提交");
  try {
    const payload = await api("/api/auth/organization-claims/accept", {
      method: "POST",
      body: JSON.stringify({
        token: organizationClaimToken,
        password,
        turnstileToken: turnstileTokens.organizationClaim || undefined,
      }),
    });
    const loginName = String(payload?.loginName || organizationClaim?.loginName || "").trim();
    clearOrganizationClaimSecret();
    organizationClaim = { ...organizationClaim, ...payload, status: payload?.status || "accepted_pending_approval" };
    setText("organizationClaimDescription", "账号密码已设置，认领申请等待平台运营审核。");
    setOrganizationClaimSuccess(payload?.message || "申请已提交。审核通过后，请使用企业账号和新密码登录。");
    el("organizationClaimAcceptButton")?.classList.add("hidden");
    el("organizationClaimRetryButton")?.classList.add("hidden");
    el("organizationClaimLoginButton")?.classList.remove("hidden");
    if (loginName) el("loginEmailInput").value = loginName;
    organizationClaimToken = "";
  } catch (error) {
    setOrganizationClaimError(error.message || "账号认领申请提交失败，请稍后重试。");
    resetTurnstile("organizationClaim");
  } finally {
    organizationClaimAccepting = false;
    setButtonLoading("organizationClaimAcceptButton", false);
  }
}

function finishOrganizationClaimAndLogin() {
  const loginName = String(organizationClaim?.loginName || el("loginEmailInput")?.value || "").trim();
  resetOrganizationClaimScreen();
  if (loginName) el("loginEmailInput").value = loginName;
  showAuthPage("login");
  setAuthStatus("认领申请正在等待审核。审核通过后即可使用该账号登录。", "success");
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
  return String(model ?? "").trim();
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
    <section class="metric-group model-rank-group model-rank-group-${mode}">
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
  if (isCustomRangeActive()) return selectedDateRangeText();
  return `近 ${el("rangeSelect").value === "custom" ? lastPresetRangeValue : el("rangeSelect").value} 天`;
}

function selectedDepartmentInfo() {
  if (!selectedDepartment) return null;
  const matched = [...departmentRankings, ...departmentPickerOptions].find((item) => item.departmentKey === selectedDepartment || item.departmentId === selectedDepartment || item.departmentName === selectedDepartment);
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
  renderUsageStatus("personalUsageStatus", personalDataFreshness, personalDataQuality, personalCoverage);
}

function selectedAdminEmployeeInfo() {
  if (!selectedAdminEmployee) return null;
  return adminEmployees.find((item) => item.employeeEmail === selectedAdminEmployee || item.employeeId === selectedAdminEmployee) || null;
}

function selectedAdminEmployeeLabel() {
  const employee = selectedAdminEmployeeInfo();
  return employee?.employeeName || employee?.employeeEmail || employee?.employeeId || selectedAdminEmployee || "员工";
}

// 成员可能跨多个部门（多账号或多团队归属），逐个列出比只显示一个更诚实。
function employeeDepartmentText(employee) {
  const names = (employee?.departmentNames || []).map((name) => String(name).trim()).filter(Boolean);
  return names.length ? names.join("、") : "未绑定部门";
}

// 邮箱有三种来源：账号自带、按姓名从另一份员工目录推断、完全没有。推断出来的
// 邮箱可能对应同名的另一个人，必须在界面上和真实邮箱区分开。
const BIND_STATUS_STYLES = {
  未绑定邮箱: { chip: "rose", hint: "该账号在系统里没有邮箱，暂时只能按账号编号统计" },
  邮箱推断: { chip: "gold", hint: "邮箱由员工目录按姓名推断，可能存在同名误差，仅供参考" },
  已绑定邮箱: { chip: "blue", hint: "邮箱来自账号本身" },
};

function bindStatusChip(status) {
  const label = String(status || "已绑定邮箱");
  const style = BIND_STATUS_STYLES[label] || BIND_STATUS_STYLES["已绑定邮箱"];
  return `<span class="chip ${style.chip}" title="${escapeHtml(style.hint)}">${escapeHtml(label)}</span>`;
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
  renderUsageStatus("adminUsageStatus", adminDataFreshness, adminDataQuality, adminCoverage);
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
  renderUsageStatus("adminUsageStatus", adminDataFreshness, adminDataQuality, adminCoverage);
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
  el("departmentAvgSpendWrap")?.classList.add("hidden");
  el("departmentOverviewHero")?.classList.remove("personal-single-day");
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
  renderUsageStatus("departmentUsageStatus", departmentDataFreshness, departmentDataQuality, departmentCoverage);
  setText("departmentModelTitle", `${scopeLabel}模型使用排行`);
  setText("departmentModelDesc", `按${scopeLabel}总 Token 消耗排序。`);
}

function normalizedEmployeeIdentity(value) {
  return String(value || "").trim().toLowerCase();
}

function employeeIdentityKeys(employee) {
  if (!employee) return [];
  const email = normalizedEmployeeIdentity(employee.employeeEmail);
  const userIds = Array.isArray(employee.userIds) ? employee.userIds : [];
  const ids = [employee.employeeId, ...userIds]
    .map(normalizedEmployeeIdentity)
    .filter(Boolean);
  return [email ? `email:${email}` : "", ...ids.map((id) => `id:${id}`)].filter(Boolean);
}

function employeeMatchesIdentity(employee, selected) {
  const employeeKeys = new Set(employeeIdentityKeys(employee));
  const selectedKeys = employeeIdentityKeys(selected);
  const employeeEmail = selectedKeys.find((key) => key.startsWith("email:"));
  const selectedEmail = [...employeeKeys].find((key) => key.startsWith("email:"));
  if (employeeEmail && selectedEmail) return employeeEmail === selectedEmail;
  return selectedKeys.some((key) => employeeKeys.has(key));
}

function selectedDepartmentEmployeeInfo() {
  if (!selectedDepartmentEmployee) return null;
  const snapshot = selectedDepartmentEmployeeSnapshot || {
    employeeId: selectedDepartmentEmployee,
    employeeEmail: selectedDepartmentEmployee.includes("@") ? selectedDepartmentEmployee : "",
  };
  return departmentEmployees.find((item) => employeeMatchesIdentity(item, snapshot)) || snapshot;
}

function selectedDepartmentEmployeeLabel() {
  const employee = selectedDepartmentEmployeeInfo();
  return employee?.employeeName || employee?.employeeEmail || employee?.employeeId || selectedDepartmentEmployee || "员工";
}

function departmentEmployeeUsageRows() {
  const employee = selectedDepartmentEmployeeInfo();
  if (!employee) return [];
  return departmentUsageData.filter((row) => employeeMatchesIdentity(row, employee));
}

function resetDepartmentEmployeeSelection() {
  selectedDepartmentEmployee = "";
  selectedDepartmentEmployeeSnapshot = null;
  departmentEmployeeUsageFilters = { date: "all", model: "all", status: "all", keyword: "" };
  el("departmentOverviewHero")?.classList.remove("personal-single-day");
  el("departmentAvgSpendWrap")?.classList.add("hidden");
}

function selectDepartmentEmployee(employeeKey) {
  const employee = departmentEmployees.find((item) => (item.employeeEmail || item.employeeId) === employeeKey);
  if (!employee) return;
  selectedDepartmentEmployee = employee.employeeEmail || employee.employeeId;
  selectedDepartmentEmployeeSnapshot = { ...employee, userIds: [...(employee.userIds || [])] };
  departmentEmployeeUsageFilters = { date: "all", model: "all", status: "all", keyword: "" };
  renderDepartment();
  scrollToDetailCard("departmentDetailCard");
}

function renderDepartmentMemberMetrics(data) {
  const label = rangeLabel();
  const source = sourceText();
  const employee = selectedDepartmentEmployeeInfo();
  const employeeName = selectedDepartmentEmployeeLabel();
  const { days } = selectedDateRange();
  const isSingleDay = days === 1;
  const dailyAvgSpend = days ? sum(data, "spend") / days : 0;
  renderDailyOverview({
    prefix: "department",
    data,
    title: "所选范围 · 员工视图",
    totalLabel: "所选员工 Token",
    sideLabel: "当前员工",
    sideValue: 1,
    sideSub: employee?.employeeEmail || employee?.employeeId || selectedDepartmentEmployee,
  });
  el("departmentOverviewHero")?.classList.toggle("personal-single-day", isSingleDay);
  el("departmentAvgSpendWrap")?.classList.toggle("hidden", isSingleDay);
  setText("departmentActiveLabel", "日均 Token");
  setDailyMiniValue("departmentActiveUsers", formatTokens(Math.round(sum(data, "totalTokens") / (days || 1))), true);
  setText("departmentActiveUsersSub", "所选范围日均");
  setText("departmentAvgSpend", money.format(dailyAvgSpend));
  setText("departmentTrendBadge", `${label} · ${source}`);
  setText("departmentSpendBadge", `${label} · ${source}`);
  setText("departmentTrendTitle", `${employeeName}每日 Token 趋势`);
  setText("departmentTrendDesc", `按日期汇总${employeeName} Prompt 与 Completion Token。`);
  setText("departmentSpendTitle", `${employeeName}每日金额消费趋势`);
  setText("departmentSpendDesc", `按日期汇总${employeeName}预估消费金额。`);
  setText("departmentSourceTitle", `${employeeName}用量占比`);
  setText("departmentSourceDesc", `按${employeeName} Codex、Claude Code 与其他来源拆分用量。`);
  renderMetricGroups("departmentMetrics", data, "department", null, data);
  setText("departmentHeroFreshness", freshnessText(departmentDataFreshness));
  renderUsageStatus("departmentUsageStatus", departmentDataFreshness, departmentDataQuality, departmentCoverage);
  setText("departmentModelTitle", `${employeeName}模型使用排行`);
  setText("departmentModelDesc", `按${employeeName}总 Token 消耗排序。`);
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
  renderUsageStatus("teamUsageStatus", teamDataFreshness, teamDataQuality, teamCoverage);
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
  renderUsageStatus("teamUsageStatus", teamDataFreshness, teamMemberDataQuality || teamDataQuality, teamMemberCoverage || teamCoverage);
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

function bindChartTooltipEvents(svg) {
  if (!svg || svg.dataset.chartTooltipBound === "true") return;
  svg.dataset.chartTooltipBound = "true";
  svg.addEventListener("pointermove", (event) => {
    const node = event.target.closest?.(".chart-hit");
    if (!node || !svg.contains(node)) {
      hideChartTooltip();
      return;
    }
    showChartTooltip(event, decodeURIComponent(node.dataset.tooltip));
  });
  svg.addEventListener("pointerleave", hideChartTooltip);
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
  bindChartTooltipEvents(svg);
}

function renderMultiLineChart({ svg, points, series, axisFormatter }) {
  if (!svg) return;
  if (!points.length) {
    renderEmptyChart(svg, "当前筛选范围暂无数据");
    return;
  }
  const width = 900;
  const height = 280;
  const pad = { left: 64, right: 18, top: 20, bottom: 42 };
  const numericValues = points.flatMap((point) => series.map((item) => Number(point[item.valueField])).filter(Number.isFinite));
  const max = Math.max(1, ...numericValues);
  const xStep = points.length > 1 ? (width - pad.left - pad.right) / (points.length - 1) : 1;
  const x = (index) => (points.length > 1 ? pad.left + index * xStep : width / 2);
  const y = (value) => height - pad.bottom - (Number(value) / max) * (height - pad.top - pad.bottom);
  const grid = [0, 0.25, 0.5, 0.75, 1].map((ratio) => {
    const yy = y(max * ratio);
    return `<line x1="${pad.left}" y1="${yy}" x2="${width - pad.right}" y2="${yy}" stroke="#dde5ee" stroke-dasharray="4 7"/><text x="12" y="${yy + 4}" fill="#66748a" font-size="12">${axisFormatter(max * ratio)}</text>`;
  }).join("");
  const paths = series.map((item) => {
    let path = "";
    let segmentOpen = false;
    points.forEach((point, index) => {
      const value = Number(point[item.valueField]);
      if (!Number.isFinite(value)) {
        segmentOpen = false;
        return;
      }
      path += `${segmentOpen ? " L" : " M"} ${x(index)} ${y(value)}`;
      segmentOpen = true;
    });
    return path ? `<path d="${path}" fill="none" stroke="${item.color}" stroke-width="${item.width || 4}" stroke-linecap="round" stroke-linejoin="round"${item.dash ? ` stroke-dasharray="${item.dash}"` : ""}/>` : "";
  }).join("");
  const hits = points.map((point, index) => {
    const rows = series.map((item) => {
      const value = Number(point[item.valueField]);
      return { label: item.label, value: Number.isFinite(value) ? axisFormatter(value) : "无数据" };
    });
    const hitLeft = index === 0 ? pad.left : (x(index - 1) + x(index)) / 2;
    const hitRight = index === points.length - 1 ? width - pad.right : (x(index) + x(index + 1)) / 2;
    return `<rect class="chart-hit" x="${hitLeft}" y="${pad.top}" width="${Math.max(1, hitRight - hitLeft)}" height="${height - pad.top - pad.bottom}" fill="transparent" data-tooltip="${encodeURIComponent(tooltipMarkup(point.date, rows))}"/>`;
  }).join("");
  const labelEvery = Math.max(1, Math.ceil(points.length / 5));
  const labels = points.map((point, index) => ({ point, index })).filter(({ index }) => index === 0 || index === points.length - 1 || index % labelEvery === 0).map(({ point, index }, labelIndex, labelsToShow) => `<text x="${x(index)}" y="${height - 16}" fill="#66748a" font-size="12" text-anchor="${labelIndex === labelsToShow.length - 1 ? "end" : labelIndex === 0 ? "start" : "middle"}">${String(point.date || "").slice(5)}</text>`).join("");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = `<rect width="${width}" height="${height}" rx="8" fill="#fbfdff"/>${grid}${paths}${hits}${labels}`;
  bindChartTooltipEvents(svg);
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
  // The API owns canonical model names; the browser only combines identical rows.
  const grouped = {};
  data.forEach((item) => {
    const key = normalizeModelKey(item.model) || "未知模型";
    (grouped[key] = grouped[key] || []).push(item);
  });
  const rows = Object.keys(grouped)
    .map((model) => ({ model, value: sum(grouped[model], "totalTokens") }))
    .filter((row) => row.value > 0)
    .sort((a, b) => b.value - a.value);
  const max = Math.max(1, ...rows.map((row) => row.value));
  container.classList.toggle("is-compact", rows.length > 0 && rows.length <= 4);
  container.innerHTML = rows.length
    ? rows
        .map((row, index) => `<div class="bar-row">${rankingBadge(index)}<strong>${escapeHtml(displayModelName(row.model))}</strong><div class="bar-track"><div class="bar-fill" style="width:${Math.max(3, (row.value / max) * 100)}%"></div></div><span class="num">${formatTokens(row.value)}</span></div>`)
        .join("")
    : `<div class="model-empty">当前筛选范围暂无模型用量</div>`;
}

function renderDepartmentBarsTo(containerId, departments) {
  const container = el(containerId);
  if (!container) return;
  const sorted = departments
    .filter((d) => d.totalTokens > 0)
    .sort((a, b) => b.totalTokens - a.totalTokens);
  const max = Math.max(1, ...sorted.map((d) => d.totalTokens));
  container.classList.toggle("is-compact", sorted.length > 0 && sorted.length <= 4);
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

function setupDepartmentEmployeeUsageFilters(data) {
  const dateSelect = el("departmentEmployeeUsageDetailDateFilter");
  const modelSelect = el("departmentEmployeeUsageDetailModelFilter");
  if (!dateSelect || !modelSelect) return;

  const dates = uniqueSorted(data, "date").reverse();
  const models = uniqueSorted(data, "model");
  if (departmentEmployeeUsageFilters.date !== "all" && !dates.includes(departmentEmployeeUsageFilters.date)) departmentEmployeeUsageFilters.date = "all";
  if (departmentEmployeeUsageFilters.model !== "all" && !models.includes(departmentEmployeeUsageFilters.model)) departmentEmployeeUsageFilters.model = "all";

  dateSelect.innerHTML = optionMarkup("all", "全部日期") + dates.map((date) => optionMarkup(date, date)).join("");
  modelSelect.innerHTML = optionMarkup("all", "全部模型") + models.map((model) => optionMarkup(model, model)).join("");
  dateSelect.value = departmentEmployeeUsageFilters.date;
  modelSelect.value = departmentEmployeeUsageFilters.model;
  const statusSelect = el("departmentEmployeeUsageDetailStatusFilter");
  const searchInput = el("departmentEmployeeUsageDetailSearch");
  if (statusSelect) {
    statusSelect.innerHTML = optionMarkup("all", "全部状态") + optionMarkup("正常", "正常") + optionMarkup("有失败", "有失败");
    statusSelect.value = departmentEmployeeUsageFilters.status;
  }
  if (searchInput) searchInput.value = departmentEmployeeUsageFilters.keyword;
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

function filteredDepartmentEmployeeUsageRows() {
  const keyword = departmentEmployeeUsageFilters.keyword.trim().toLowerCase();
  return departmentEmployeeUsageRows().filter((item) => {
    const hasFailure = Number(item.failureCount || 0) > 0;
    const displayStatus = hasFailure ? "有失败" : "正常";
    const matchesDate = departmentEmployeeUsageFilters.date === "all" || item.date === departmentEmployeeUsageFilters.date;
    const matchesModel = departmentEmployeeUsageFilters.model === "all" || item.model === departmentEmployeeUsageFilters.model;
    const matchesStatus = departmentEmployeeUsageFilters.status === "all" || departmentEmployeeUsageFilters.status === displayStatus;
    const text = `${item.model || ""} ${displaySource(item.source)}`.toLowerCase();
    return matchesDate && matchesModel && matchesStatus && (!keyword || text.includes(keyword));
  });
}

function updateDepartmentEmployeeUsageFilters() {
  departmentEmployeeUsageFilters = {
    date: el("departmentEmployeeUsageDetailDateFilter").value,
    model: el("departmentEmployeeUsageDetailModelFilter").value,
    status: el("departmentEmployeeUsageDetailStatusFilter").value,
    keyword: el("departmentEmployeeUsageDetailSearch").value.trim(),
  };
  renderDepartment();
}

function resetDepartmentEmployeeUsageFilters() {
  departmentEmployeeUsageFilters = { date: "all", model: "all", status: "all", keyword: "" };
  setupDepartmentEmployeeUsageFilters(departmentEmployeeUsageRows());
  renderDepartment();
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

function rankingBadge(index) {
  const rank = index + 1;
  return `<span class="bar-rank${rank <= 3 ? " is-leading" : ""}" aria-label="第 ${rank} 名">${rank}</span>`;
}

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
    const hasUsage = Number(item.totalTokens || 0) > 0 || Number(item.requestCount || 0) > 0;
    meta.textContent = hasUsage
      ? `ID：${item.departmentId || "未绑定部门"} · Token：${formatTokens(item.totalTokens || 0)} · 活跃员工：${fmt.format(item.activeEmployees || 0)}`
      : `ID：${item.departmentId || "未绑定部门"} · 当前范围暂无用量`;

    button.append(title, meta);
    button.addEventListener("click", () => selectDepartmentOption(item));
    optionsEl.appendChild(button);
  });
}

async function selectDepartmentOption(item) {
  resetDepartmentEmployeeSelection();
  selectedDepartment = departmentOptionKey(item);
  el("departmentEmployeeSearch").value = departmentOptionName(item);
  closeDepartmentPicker();
  const loading = loadDepartmentData();
  scrollToDetailCard("departmentDetailCard");
  await loading;
}

async function selectAllDepartments() {
  resetDepartmentEmployeeSelection();
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
  resetDepartmentEmployeeSelection();
  selectedDepartment = "";
  closeDepartmentPicker();
  await loadDepartmentData();
}

function employeeSummariesFromRows(rows) {
  const grouped = {};
  const sourceTotals = {};
  rows.forEach((row) => {
    // Rows without an identity cannot safely be attributed to an employee.
    // Do not invent a synthetic "mock" employee in a real organization view.
    const employeeId = row.employeeId || row.employeeEmail;
    if (!employeeId) return;
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
  // 只有全员看板的排行表有部门列：部门看板已经按部门下钻（整表同属一个部门），
  // 团队看板的表头位置留给了团队角色。列数必须与 index.html 的表头一致。
  const showDepartment = tableId === "adminUserTable";
  updateRankingSortIndicators(tableId);
  el(countId).textContent = `${sorted.length} 人`;
  el(tableId).innerHTML = sorted.length
    ? sorted
        .map((item, index) => {
          const requests = Number(item.requestCount || 0);
          const successRate = requests ? Math.round((Number(item.successCount || 0) / requests) * 1000) / 10 : 0;
          return `
            <tr class="admin-employee-row ${(tableId === "teamUserTable" && selectedTeamEmployee === (item.employeeEmail || item.employeeId)) || (tableId === "departmentUserTable" && employeeMatchesIdentity(item, selectedDepartmentEmployeeInfo())) ? "active" : ""}" data-employee="${escapeHtml(item.employeeEmail || item.employeeId)}">
              <td class="rank-cell">${rankingBadge(index)}</td>
              <td><strong>${item.employeeName || item.employeeId}</strong></td>
              <td>${item.employeeEmail || "未绑定邮箱"}</td>
              ${showDepartment ? `<td>${escapeHtml(employeeDepartmentText(item))}</td>` : ""}
              <td>${displaySource(item.primarySource || "其他")}</td>
              ${isTeamTable ? `<td>${item.teamRole === "admin" ? "负责人" : "成员"}</td>` : ""}
              <td class="num">${fmt.format(requests)}</td>
              <td class="num"><strong>${formatTokens(item.totalTokens || 0)}</strong></td>
              <td class="num">${money.format(item.spend || 0)}</td>
              <td class="num">${successRate}%</td>
              <td>${bindStatusChip(item.bindStatus)}</td>
            </tr>
          `;
        })
        .join("")
    : `<tr><td colspan="${isTeamTable || showDepartment ? 10 : 9}" style="text-align:center;color:var(--muted);padding:26px">${emptyText}</td></tr>`;
}

function renderDepartmentRanking(tableId, countId, departments, emptyText) {
  const sorted = sortedRankingRows(tableId, departments, departmentRankingName);
  updateRankingSortIndicators(tableId);
  el(countId).textContent = `${sorted.length} 个部门`;
  el(tableId).innerHTML = sorted.length
    ? sorted
        .map((item, index) => {
          const requests = Number(item.requestCount || 0);
          const successRate = requests ? Math.round((Number(item.successCount || 0) / requests) * 1000) / 10 : 0;
          return `
            <tr class="admin-employee-row" data-department="${escapeHtml(departmentOptionKey(item))}">
              <td class="rank-cell">${rankingBadge(index)}</td>
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
    : `<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:26px">${emptyText}</td></tr>`;
}

function renderAdminUsers() {
  renderEmployeeRanking("adminUserTable", "adminUserCount", adminEmployees, "当前筛选范围暂无员工用量");
}

function renderDepartmentUsers() {
  const scopeLabel = departmentScopeLabel();
  el("departmentBackButton").classList.toggle("hidden", !selectedDepartment);
  if (selectedDepartment) {
    el("departmentRankingTitle").textContent = `${scopeLabel}员工排行`;
    el("departmentRankingDesc").textContent = `当前展示 ${scopeLabel} 内员工用量，点击员工查看个人用量详情。`;
    renderEmployeeRanking("departmentUserTable", "departmentUserCount", departmentEmployees, "当前筛选范围暂无部门员工用量");
  } else {
    el("departmentRankingTitle").textContent = "部门用量排行";
    el("departmentRankingDesc").textContent = "点击部门查看该部门用量看板和员工排行。";
    renderDepartmentRanking("departmentUserTable", "departmentUserCount", departmentRankings, "当前筛选范围暂无部门用量");
  }
}

function renderTeamUsers() {
  if (isTeamRankingLoading) {
    renderTableSkeleton("teamUserTable", "teamUserCount", 10);
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
  teamMemberUsageRequestController?.abort();
  teamMemberUsageRequestController = null;
  teamMemberUsageQueryKey = "";
  teamMemberUsageInFlight = null;
  isTeamMemberLoading = false;
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
  teamDataQuality = payload.dataQuality || null;
  teamCoverage = payload.coverage || null;
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

function renderBarsSkeleton(containerId, showRank = false) {
  setHtml(
    containerId,
    Array.from({ length: 5 })
    .map(
      (_, index) => `
        <div class="bar-row">
          ${showRank ? '<span class="bar-rank" aria-hidden="true"></span>' : ''}
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
  renderBarsSkeleton("modelBars", true);
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
  renderBarsSkeleton("adminModelBars", true);
  renderTableSkeleton("adminUserTable", "adminUserCount", 10);
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
    ? `当前展示 ${scopeLabel} 内员工用量，点击员工查看个人用量详情。`
    : "点击部门查看该部门用量看板和员工排行。");
  setDailyTokenValue("departmentHeroTotal", "加载中");
  setText("departmentHeroSpend", "--");
  setText("departmentHeroTotalLabel", selectedDepartmentEmployee ? "所选员工 Token" : "所选范围 Token");
  setText("departmentWelcomeTitle", selectedDepartmentEmployee ? "所选范围 · 员工视图" : `所选范围 · ${scopeLabel}`);
  setText("departmentHeroRequests", "--");
  setText("departmentHeroRequestsSub", "数据加载中");
  setText("departmentHeroSuccess", "--");
  setText("departmentHeroSuccessSub", "-- / -- 次成功");
  setText("departmentHeroDate", "加载中");
  setText("departmentHeroContext", `${label} · ${source} · 数据加载中`);
  setText("departmentActiveUsers", "--");
  setText("departmentActiveLabel", selectedDepartmentEmployee ? "日均 Token" : selectedDepartment ? "活跃员工" : "活跃部门");
  setText("departmentActiveUsersSub", selectedDepartmentEmployee ? "所选范围日均" : selectedDepartment ? "当前部门" : "当前筛选范围");
  const isSingleDayEmployee = Boolean(selectedDepartmentEmployee) && selectedDateRange().days === 1;
  el("departmentOverviewHero")?.classList.toggle("personal-single-day", isSingleDayEmployee);
  el("departmentAvgSpendWrap")?.classList.toggle("hidden", !selectedDepartmentEmployee || isSingleDayEmployee);
  setText("departmentTrendBadge", `${label} · ${source}`);
  setText("departmentSpendBadge", `${label} · ${source}`);
  setText("departmentLimitHint", "数据加载中");
  renderDepartmentDetailCard();
  renderMetricSkeleton("departmentMetrics");
  renderChartSkeleton("departmentTrendChart");
  renderChartSkeleton("departmentSpendChart");
  if (selectedDepartment) toggleTrendGrid("departmentTrendGrid");
  renderDonutSkeleton("departmentDonutTotal", "departmentSourceLegend");
  renderBarsSkeleton("departmentModelBars", true);
  renderBarsSkeleton("departmentBars");
  renderTableSkeleton("departmentUserTable", "departmentUserCount", 9);
  if (selectedDepartmentEmployee) renderTableSkeleton("departmentEmployeeUsageTable", "departmentEmployeeTableCount", 8);
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
  renderBarsSkeleton("teamModelBars", true);
  if (selectedTeamEmployee) {
    if (isTeamRankingLoading) {
      renderTableSkeleton("teamUserTable", "teamUserCount", 10);
    } else {
      setText("teamLimitHint", teamRankingError || teamRankingHint || "按当前筛选范围统计");
      renderEmployeeRanking("teamUserTable", "teamUserCount", teamEmployees, teamRankingError || "当前团队暂无成员用量");
    }
    renderTableSkeleton("teamMemberUsageTable", "teamMemberTableCount", 8);
  } else {
    renderTableSkeleton("teamUserTable", "teamUserCount", 10);
  }
}

function renderPersonal() {
  if (isDashboardLoading && !usageData.length) {
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
  if (isAdminLoading && !adminUsageData.length && !adminSummaryData.length && !adminEmployees.length) {
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
  renderAdminMetrics(totalData);
  renderTrendTo("adminTrendChart", totalData);
  renderSpendTrendTo("adminSpendChart", totalData);
  // 饼图按 source 聚合、条形图按 model 聚合，summaryRows 已按 date/source/model
  // 汇总，两张图的结果与逐员工明细完全一致。未筛选员工时后端不再返回明细，
  // 这里必须用聚合行，否则近 14/30 天的两张图会变空。
  renderDonutTo("adminSourceDonut", "adminDonutTotal", "adminSourceLegend", totalData);
  renderModelBarsTo("adminModelBars", totalData);
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
  const identity = employee?.employeeEmail || employee?.employeeId || selectedAdminEmployee;
  el("adminDetailSubtitle").textContent = `${identity} · 部门：${employeeDepartmentText(employee)}`;
}

function renderDepartment() {
  if (isDepartmentLoading && !departmentUsageData.length && !departmentSummaryData.length && !departmentRankings.length) {
    renderDepartmentLoading();
    return;
  }
  setDepartmentOverviewVisible(Boolean(selectedDepartment));
  const totalData = departmentSummaryData.length ? departmentSummaryData : departmentUsageData;

  if (selectedDepartment) toggleTrendGrid("departmentTrendGrid");

  const barsPanel = el("departmentBars")?.closest(".panel");

  if (selectedDepartment) {
    const employeeData = selectedDepartmentEmployee ? departmentEmployeeUsageRows() : null;
    const chartData = employeeData || totalData;
    if (selectedDepartmentEmployee) renderDepartmentMemberMetrics(chartData);
    else renderDepartmentMetrics(chartData);
    renderTrendTo("departmentTrendChart", chartData);
    renderSpendTrendTo("departmentSpendChart", chartData);
    renderDonutTo("departmentSourceDonut", "departmentDonutTotal", "departmentSourceLegend", employeeData || departmentUsageData);
    renderModelBarsTo("departmentModelBars", employeeData || departmentUsageData);
    barsPanel?.classList.add("hidden");
  } else {
    renderDepartmentBarsTo("departmentBars", departmentRankings);
    const count = departmentRankings.filter((d) => d.totalTokens > 0).length;
    el("departmentBarsCount").textContent = `${count} 个部门`;
    barsPanel?.classList.remove("hidden");
  }

  renderDepartmentUsers();
  renderDepartmentEmployeeTable();
  renderDepartmentPickerOptions();

  renderDepartmentDetailCard();
}

function renderDepartmentDetailCard() {
  const detailCard = el("departmentDetailCard");
  if (!detailCard) return;
  detailCard.classList.toggle("show", Boolean(selectedDepartment));
  if (!selectedDepartment) return;
  const department = selectedDepartmentInfo();
  const backLabel = el("departmentDetailBackLabel");
  if (selectedDepartmentEmployee) {
    const employee = selectedDepartmentEmployeeInfo();
    el("departmentDetailTitle").textContent = `${selectedDepartmentEmployeeLabel()} 的用量详情`;
    el("departmentDetailSubtitle").textContent = `${employee?.employeeEmail || employee?.employeeId || selectedDepartmentEmployee} · 部门：${department.name}`;
    if (backLabel) backLabel.textContent = "返回部门总览";
  } else {
    el("departmentDetailTitle").textContent = `${department.name} 的部门详情`;
    el("departmentDetailSubtitle").textContent = `部门 ID：${department.id} · 数据来源：${department.bindStatus} · 下方排行已切换为该部门员工用量`;
    if (backLabel) backLabel.textContent = "返回全部部门";
  }
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
  syncOrganizationDemoChrome();
  renderAccountAccessState();
  if (accountAccessCopy(currentUser)) return;
  renderPersonal();
  if (canViewAdminUsage()) renderAdmin();
  if (canViewDepartmentUsage()) renderDepartment();
  if (currentUser?.isTeamLeader) renderTeam();
  if (customerOrganizationsAvailable()) {
    renderCustomerOrganizations();
    renderPendingAdoptionOrganizations();
  }
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
  setText("keyCount", isKeysLoading ? (hasLoadedPersonalKeys ? "更新中" : "加载中") : countText);
  const tableBody = el("keyTableBody");
  const cardList = el("keyCardList");

  if (isKeysLoading && !hasLoadedPersonalKeys) {
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
    const emptyMessage = escapeHtml(
      keyRefreshError
        ? `暂时无法更新密钥列表：${keyRefreshError}`
        : "还没有个人密钥，点击“添加密钥”创建第一个。",
    );
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

  if (keyRefreshError) {
    const warning = `<tr><td colspan="8" class="key-empty">列表暂未更新：${escapeHtml(keyRefreshError)}</td></tr>`;
    tableBody.insertAdjacentHTML("afterbegin", warning);
    cardList.insertAdjacentHTML(
      "afterbegin",
      `<article class="panel key-empty">列表暂未更新：${escapeHtml(keyRefreshError)}</article>`,
    );
  }
}

function renderDepartmentEmployeeTable() {
  const table = el("departmentEmployeeUsageTable");
  const count = el("departmentEmployeeTableCount");
  const detailCard = el("departmentEmployeeDetailCard");
  if (!table || !count || !detailCard) return;
  const visible = Boolean(selectedDepartmentEmployee);
  detailCard.classList.toggle("hidden", !visible);
  if (!visible) {
    table.innerHTML = "";
    count.textContent = "0 条";
    return;
  }
  const employeeRows = departmentEmployeeUsageRows();
  setupDepartmentEmployeeUsageFilters(employeeRows);
  const rows = filteredDepartmentEmployeeUsageRows();
  count.textContent = `${rows.length} 条`;
  table.innerHTML = rows.length
    ? rows.slice().reverse().map((item) => {
        const status = item.failureCount > 0 ? `<span class="chip rose">${item.failureCount} 次失败</span>` : `<span class="chip">正常</span>`;
        return `<tr><td>${escapeHtml(item.date)}</td><td>${escapeHtml(displaySource(item.source))}</td><td>${escapeHtml(item.model)}</td><td class="num">${fmt.format(item.requestCount || 0)}</td><td class="num">${fmt.format(item.promptTokens || 0)}</td><td class="num">${fmt.format(item.completionTokens || 0)}</td><td class="num"><strong>${fmt.format(item.totalTokens || 0)}</strong></td><td>${status}</td></tr>`;
      }).join("")
    : `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:26px">当前员工在所选范围内暂无用量记录</td></tr>`;
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

function personalKeyCacheIdentity(user = currentUser) {
  if (!user) return "";
  return String(user.id || authContactEmail(user) || authDisplayIdentifier(user) || "")
    .trim()
    .toLowerCase();
}

function personalKeyCacheStorageKey(user = currentUser) {
  const identity = personalKeyCacheIdentity(user);
  return identity ? `${PERSONAL_KEY_CACHE_PREFIX}${encodeURIComponent(identity)}` : "";
}

function cacheablePersonalKey(key) {
  if (!key || typeof key !== "object") return null;
  return Object.fromEntries(
    CACHEABLE_PERSONAL_KEY_FIELDS
      .filter((field) => Object.hasOwn(key, field))
      .map((field) => [field, key[field]]),
  );
}

function clearPersonalKeyCache(user = currentUser) {
  const storageKey = personalKeyCacheStorageKey(user);
  if (!storageKey) return;
  try {
    window.sessionStorage.removeItem(storageKey);
  } catch {}
}

function restorePersonalKeyCache() {
  if (hasLoadedPersonalKeys) return false;
  const storageKey = personalKeyCacheStorageKey();
  if (!storageKey) return false;
  try {
    const cached = JSON.parse(window.sessionStorage.getItem(storageKey) || "null");
    const cachedAt = Number(cached?.cachedAt || 0);
    if (!Array.isArray(cached?.keys) || !cachedAt || Date.now() - cachedAt > PERSONAL_KEY_CACHE_TTL_MS) {
      window.sessionStorage.removeItem(storageKey);
      return false;
    }
    personalKeys = cached.keys.map(cacheablePersonalKey).filter(Boolean);
    availableKeyModels = [];
    unrestrictedKeyModels = false;
    hasLoadedPersonalKeys = true;
    personalKeysLoadedAt = cachedAt;
    keyLoadError = "";
    keyRefreshError = "";
    renderKeys();
    return true;
  } catch {
    try {
      window.sessionStorage.removeItem(storageKey);
    } catch {}
    return false;
  }
}

function storePersonalKeyCache(keys) {
  const storageKey = personalKeyCacheStorageKey();
  if (!storageKey) return;
  try {
    window.sessionStorage.setItem(storageKey, JSON.stringify({
      cachedAt: Date.now(),
      keys: keys.map(cacheablePersonalKey).filter(Boolean),
    }));
  } catch {}
}

async function fetchPersonalKeys(forceRefresh = false, options = {}) {
  const requestIdentity = personalKeyCacheIdentity();
  const requestGeneration = authSessionGeneration;
  const hadLoadedData = hasLoadedPersonalKeys;
  if (revealedKeys.size || revealTimers.size || revealingKeyIds.size) clearRevealedKeys();
  isKeysLoading = true;
  keyLoadError = "";
  keyRefreshError = "";
  renderKeys();
  try {
    const params = new URLSearchParams({ include_models: "0" });
    if (forceRefresh) params.set("refresh", "1");
    const payload = await api(`/api/me/keys?${params}`);
    if (requestGeneration !== authSessionGeneration || requestIdentity !== personalKeyCacheIdentity()) return null;
    personalKeys = Array.isArray(payload.keys) ? payload.keys : [];
    availableKeyModels = [];
    unrestrictedKeyModels = false;
    hasLoadedPersonalKeys = true;
    personalKeysLoadedAt = Date.now();
    storePersonalKeyCache(personalKeys);
    return payload;
  } catch (error) {
    if (requestGeneration !== authSessionGeneration || requestIdentity !== personalKeyCacheIdentity()) return null;
    const message = error.message || "个人密钥加载失败，请稍后重试。";
    if (hadLoadedData || hasLoadedPersonalKeys) keyRefreshError = message;
    else {
      personalKeys = [];
      availableKeyModels = [];
      unrestrictedKeyModels = false;
      keyLoadError = message;
    }
    if (!options.silent) showToast(message);
    return null;
  } finally {
    if (requestGeneration === authSessionGeneration && requestIdentity === personalKeyCacheIdentity()) {
      isKeysLoading = false;
      renderKeys();
    }
  }
}

function loadKeys(forceRefresh = false, options = {}) {
  if (!currentUser) return Promise.resolve(null);
  if (!forceRefresh) {
    restorePersonalKeyCache();
    if (keyRefreshRequest) return keyRefreshRequest;
    if (keyListRequest) return keyListRequest;
    let request;
    request = fetchPersonalKeys(false, options).finally(() => {
      if (keyListRequest === request) keyListRequest = null;
    });
    keyListRequest = request;
    return request;
  }

  clearPersonalKeyCache();
  if (keyRefreshRequest) return keyRefreshRequest;
  const pendingListRequest = keyListRequest;
  let request;
  request = (async () => {
    if (pendingListRequest) {
      try {
        await pendingListRequest;
      } catch {}
    }
    return fetchPersonalKeys(true, options);
  })().finally(() => {
    if (keyRefreshRequest === request) keyRefreshRequest = null;
  });
  keyRefreshRequest = request;
  return request;
}

function personalKeysAreFresh() {
  return hasLoadedPersonalKeys
    && personalKeysLoadedAt > 0
    && Date.now() - personalKeysLoadedAt <= PERSONAL_KEY_CACHE_TTL_MS;
}

function prefetchPersonalKeys() {
  if (!currentUser || isOrganizationCustomerIdentity() || accountAccessCopy(currentUser)) return;
  loadKeys(false, { silent: true });
}

// ---- 团队成员密钥（团队负责人） ----

function canManageTeamKeys() {
  return Boolean(currentUser?.isTeamLeader) && !isOrganizationCustomerIdentity();
}

function renderTeamKeySelector() {
  const field = el("teamKeySelectField");
  const select = el("teamKeySelect");
  if (!field || !select) return;
  const teams = teamKeyTeams.length ? teamKeyTeams : leaderTeams;
  field.classList.toggle("hidden", teams.length <= 1);
  select.innerHTML = teams
    .map(
      (team) =>
        `<option value="${escapeHtml(team.teamRef || "")}">${escapeHtml(team.name || team.id || "团队")}</option>`,
    )
    .join("");
  if (!selectedTeamKeyRef && teams.length) selectedTeamKeyRef = teams[0].teamRef || "";
  if (selectedTeamKeyRef) select.value = selectedTeamKeyRef;
}

function teamKeyEmptyMessage() {
  if (teamKeyFilters.search || teamKeyFilters.status !== "all") return "没有符合筛选条件的成员密钥。";
  return "该团队的普通成员还没有可管理的密钥。";
}

function renderTeamKeys() {
  const panel = el("teamKeysPanel");
  if (!panel) return;
  panel.classList.toggle("hidden", !canManageTeamKeys());
  if (!canManageTeamKeys()) return;
  renderTeamKeySelector();
  const tableBody = el("teamKeyTableBody");
  const cardList = el("teamKeyCardList");
  if (!tableBody || !cardList) return;
  setText("teamKeyCountChip", isTeamKeysLoading ? "加载中" : `${fmt.format(teamMemberKeys.length)} 个`);

  if (isTeamKeysLoading) {
    tableBody.innerHTML = `<tr><td colspan="8" class="key-loading">正在加载团队成员密钥...</td></tr>`;
    cardList.innerHTML = `<article class="panel key-loading">正在加载团队成员密钥...</article>`;
    return;
  }
  if (teamKeyLoadError) {
    const message = escapeHtml(teamKeyLoadError);
    tableBody.innerHTML = `<tr><td colspan="8" class="key-empty">${message}</td></tr>`;
    cardList.innerHTML = `<article class="panel key-empty">${message}</article>`;
    return;
  }
  if (!teamMemberKeys.length) {
    const message = escapeHtml(teamKeyEmptyMessage());
    tableBody.innerHTML = `<tr><td colspan="8" class="key-empty">${message}</td></tr>`;
    cardList.innerHTML = `<article class="panel key-empty">${message}</article>`;
    return;
  }

  const actionsMarkup = (key) => {
    const id = escapeHtml(key.id || "");
    const status = String(key.status || "");
    const canRevoke = status === "正常";
    const canDelete = status === "已禁用" || status === "已过期";
    return `
      <button class="ghost-btn" type="button" data-team-key-revoke="${id}" ${canRevoke ? "" : 'disabled title="该密钥已失效，无需撤销"'}>撤销</button>
      <button class="danger-outline-btn" type="button" data-team-key-delete="${id}" ${canDelete ? "" : 'disabled title="请先撤销该密钥再删除"'}>删除</button>
    `;
  };

  tableBody.innerHTML = teamMemberKeys
    .map((key) => {
      const memberName = escapeHtml(key.memberName || key.memberEmail || "未知成员");
      const memberEmail = escapeHtml(key.memberEmail || "-");
      return `
        <tr>
          <td><div class="key-name-cell"><strong>${memberName}</strong><span>${memberEmail}</span></div></td>
          <td><span class="key-type-label">${escapeHtml(key.keyType || "-")}</span></td>
          <td>${escapeHtml(key.name || "个人访问密钥")}</td>
          <td><span class="chip ${keyStatusClass(key.status)}">${escapeHtml(key.status || "正常")}</span></td>
          <td><code class="key-masked-value">${escapeHtml(key.masked || "sk-...----")}</code></td>
          <td>${escapeHtml(key.createdAt || "-")}</td>
          <td>${escapeHtml(key.lastUsed || "-")}</td>
          <td><div class="key-row-actions">${actionsMarkup(key)}</div></td>
        </tr>
      `;
    })
    .join("");

  cardList.innerHTML = teamMemberKeys
    .map(
      (key) => `
      <article class="panel key-mobile-card">
        <div class="key-mobile-head">
          <div class="key-name-cell">
            <strong>${escapeHtml(key.memberName || key.memberEmail || "未知成员")}</strong>
            <span>${escapeHtml(key.memberEmail || "-")}</span>
          </div>
          <span class="chip ${keyStatusClass(key.status)}">${escapeHtml(key.status || "正常")}</span>
        </div>
        <div class="key-mobile-row"><span>类型</span><strong>${escapeHtml(key.keyType || "-")}</strong></div>
        <div class="key-mobile-row"><span>名称</span><strong>${escapeHtml(key.name || "个人访问密钥")}</strong></div>
        <div class="key-mobile-row"><span>密钥</span><code class="key-masked-value">${escapeHtml(key.masked || "sk-...----")}</code></div>
        <div class="key-mobile-row"><span>创建时间</span><strong>${escapeHtml(key.createdAt || "-")}</strong></div>
        <div class="key-mobile-row"><span>最近使用</span><strong>${escapeHtml(key.lastUsed || "-")}</strong></div>
        <div class="key-mobile-actions">${actionsMarkup(key)}</div>
      </article>
    `,
    )
    .join("");
}

async function loadTeamKeys(forceRefresh = false) {
  if (!canManageTeamKeys()) {
    teamMemberKeys = [];
    renderTeamKeys();
    return;
  }
  const requestId = ++teamKeyRequestId;
  isTeamKeysLoading = true;
  teamKeyLoadError = "";
  renderTeamKeys();
  const params = new URLSearchParams();
  if (selectedTeamKeyRef) params.set("team_ref", selectedTeamKeyRef);
  if (teamKeyFilters.search) params.set("search", teamKeyFilters.search);
  if (teamKeyFilters.status && teamKeyFilters.status !== "all") params.set("status", teamKeyFilters.status);
  if (forceRefresh) params.set("refresh", "1");
  try {
    const payload = await api(`/api/team/keys?${params.toString()}`);
    if (requestId !== teamKeyRequestId) return;
    teamMemberKeys = Array.isArray(payload.keys) ? payload.keys : [];
    teamKeyTeams = Array.isArray(payload.teams) ? payload.teams : [];
    selectedTeamKeyRef = payload.team?.teamRef || selectedTeamKeyRef;
  } catch (error) {
    if (requestId !== teamKeyRequestId) return;
    teamMemberKeys = [];
    // 面板内展示错误，不打断个人密钥表格。
    teamKeyLoadError = error.message || "团队成员密钥加载失败，请稍后重试。";
  } finally {
    if (requestId === teamKeyRequestId) {
      isTeamKeysLoading = false;
      renderTeamKeys();
    }
  }
}

function scheduleTeamKeyReload() {
  window.clearTimeout(teamKeySearchTimer);
  teamKeySearchTimer = window.setTimeout(() => loadTeamKeys(), 260);
}

function resetTeamKeyState() {
  window.clearTimeout(teamKeySearchTimer);
  teamKeySearchTimer = null;
  teamMemberKeys = [];
  teamKeyTeams = [];
  selectedTeamKeyRef = "";
  teamKeyFilters = { search: "", status: "all" };
  isTeamKeysLoading = false;
  teamKeyLoadError = "";
  teamKeyRequestId += 1;
  revokingTeamKeyId = "";
  deletingTeamKeyId = "";
  isTeamKeyRevoking = false;
  isTeamKeyDeleting = false;
  if (el("teamKeySearch")) el("teamKeySearch").value = "";
  if (el("teamKeyStatusFilter")) el("teamKeyStatusFilter").value = "all";
  el("teamKeyRevokeModal")?.classList.add("hidden");
  el("teamKeyDeleteModal")?.classList.add("hidden");
  el("teamKeysPanel")?.classList.add("hidden");
}

function findTeamMemberKey(keyId) {  return teamMemberKeys.find((item) => String(item.id || "") === String(keyId)) || null;
}

function closeTeamKeyRevokeModal(options = {}) {
  if (isTeamKeyRevoking && !options.force) return;
  revokingTeamKeyId = "";
  el("teamKeyRevokeModal")?.classList.add("hidden");
}

function openTeamKeyRevokeModal(keyId) {
  if (!canManageTeamKeys() || !keyId) return;
  const key = findTeamMemberKey(keyId);
  if (!key || String(key.status || "") !== "正常") return;
  revokingTeamKeyId = String(keyId);
  setText("teamKeyRevokeMember", key.memberName || key.memberEmail || "未知成员");
  setText("teamKeyRevokeName", key.name || "个人访问密钥");
  setText("teamKeyRevokeMasked", key.masked || "sk-...----");
  el("teamKeyRevokeModal")?.classList.remove("hidden");
}

async function confirmTeamKeyRevoke() {
  if (!canManageTeamKeys() || !revokingTeamKeyId || isTeamKeyRevoking) return;
  const keyId = revokingTeamKeyId;
  isTeamKeyRevoking = true;
  setButtonLoading("confirmTeamKeyRevokeButton", true, "撤销中");
  try {
    await ensureCsrfToken();
    await api(`/api/team/keys/${encodeURIComponent(keyId)}/revoke`, {
      method: "POST",
      body: JSON.stringify({ teamRef: selectedTeamKeyRef || "" }),
    });
    isTeamKeyRevoking = false;
    closeTeamKeyRevokeModal({ force: true });
    await loadTeamKeys(true);
    showToast("成员密钥已撤销，立即失效。");
  } catch (error) {
    showToast(error.message || "成员密钥撤销失败，请稍后重试。");
  } finally {
    isTeamKeyRevoking = false;
    setButtonLoading("confirmTeamKeyRevokeButton", false);
  }
}

function closeTeamKeyDeleteModal(options = {}) {
  if (isTeamKeyDeleting && !options.force) return;
  deletingTeamKeyId = "";
  el("teamKeyDeleteModal")?.classList.add("hidden");
}

function openTeamKeyDeleteModal(keyId) {
  if (!canManageTeamKeys() || !keyId) return;
  const key = findTeamMemberKey(keyId);
  if (!key) return;
  const status = String(key.status || "");
  if (status !== "已禁用" && status !== "已过期") {
    showToast("请先撤销该密钥再删除。");
    return;
  }
  deletingTeamKeyId = String(keyId);
  setText("teamKeyDeleteMember", key.memberName || key.memberEmail || "未知成员");
  setText("teamKeyDeleteName", key.name || "个人访问密钥");
  setText("teamKeyDeleteMasked", key.masked || "sk-...----");
  el("teamKeyDeleteModal")?.classList.remove("hidden");
}

async function confirmTeamKeyDelete() {
  if (!canManageTeamKeys() || !deletingTeamKeyId || isTeamKeyDeleting) return;
  const keyId = deletingTeamKeyId;
  isTeamKeyDeleting = true;
  setButtonLoading("confirmTeamKeyDeleteButton", true, "删除中");
  try {
    await ensureCsrfToken();
    const payload = await api(`/api/team/keys/${encodeURIComponent(keyId)}/delete`, {
      method: "POST",
      body: JSON.stringify({ teamRef: selectedTeamKeyRef || "" }),
    });
    isTeamKeyDeleting = false;
    closeTeamKeyDeleteModal({ force: true });
    await loadTeamKeys(true);
    showToast(payload.warning || "成员密钥已删除。");
  } catch (error) {
    showToast(error.message || "成员密钥删除失败，请稍后重试。");
  } finally {
    isTeamKeyDeleting = false;
    setButtonLoading("confirmTeamKeyDeleteButton", false);
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

function organizationBillingField(record, camelCase, snakeCase = "") {
  if (!record || typeof record !== "object") return undefined;
  if (record[camelCase] !== undefined) return record[camelCase];
  return snakeCase && record[snakeCase] !== undefined ? record[snakeCase] : undefined;
}

function organizationBillingAccount() {
  const account = organizationBillingData?.account || {};
  const number = (camelCase, snakeCase = "", fallback = 0) => {
    const value = organizationBillingField(account, camelCase, snakeCase);
    return Number.isFinite(Number(value)) ? Number(value) : fallback;
  };
  return {
    initialBalanceUsd: number("initialBalanceUsd", "initial_balance_usd"),
    totalTopupsUsd: number("totalTopupsUsd", "total_topups_usd"),
    totalCreditsUsd: number("totalCreditsUsd", "total_credits_usd"),
    availableBalanceUsd: number("availableBalanceUsd", "available_balance_usd"),
    billingStatus: String(organizationBillingField(account, "billingStatus", "billing_status") || ""),
    pastDue: Boolean(organizationBillingField(account, "pastDue", "past_due")),
  };
}

function organizationBillingUsage(period) {
  const summary = organizationBillingData?.usageSummary || organizationBillingData?.usage_summary || {};
  const value = summary?.[period] || {};
  const number = (camelCase, snakeCase = "") => {
    const raw = organizationBillingField(value, camelCase, snakeCase);
    return Number.isFinite(Number(raw)) ? Number(raw) : 0;
  };
  return {
    spend: number("spend", "spend"),
    tokens: number("tokens", "total_tokens"),
    requests: number("requests", "request_count"),
  };
}

function organizationBillingRecords() {
  const records = organizationBillingData?.records || {};
  const items = Array.isArray(records) ? records : records?.items;
  return Array.isArray(items) ? items : [];
}

function organizationBillingRecordTotal() {
  const records = organizationBillingData?.records || {};
  const total = Array.isArray(records) ? records.length : records?.total;
  return Number.isFinite(Number(total)) ? Number(total) : organizationBillingRecords().length;
}

function organizationBillingRecordPage() {
  const records = organizationBillingData?.records || {};
  const page = Array.isArray(records) ? organizationBillingPage : Number(records?.page || organizationBillingPage || 1);
  return Math.max(1, Math.floor(Number.isFinite(page) ? page : 1));
}

function organizationBillingRecordPageSize() {
  const records = organizationBillingData?.records || {};
  const pageSize = Array.isArray(records)
    ? organizationBillingPageSize
    : Number(records?.pageSize || organizationBillingPageSize);
  return Math.max(1, Math.floor(Number.isFinite(pageSize) ? pageSize : organizationBillingPageSize));
}

function organizationBillingRecordTypeLabel(type) {
  const labels = {
    initial_credit: "初始企业额度",
    simulated_topup: "模拟充值",
    grant: "额度授予",
    credit: "额度授予",
    revoke: "额度扣减",
    charge: "用量扣减",
    refund: "额度退回",
  };
  return labels[String(type || "")] || String(type || "额度调整");
}

function organizationBillingRecordStatusLabel(status) {
  return String(status || "") === "completed" ? "已完成" : String(status || "-");
}

function organizationBillingUsageCard(label, usage) {
  return `<article class="organization-billing-usage-card">
    <span>${escapeHtml(label)}</span>
    <strong>${escapeHtml(money.format(usage.spend))}</strong>
    <small>预估消耗</small>
    <div><span>${escapeHtml(formatTokens(usage.tokens))} Token</span><span>${escapeHtml(fmt.format(usage.requests))} 次请求</span></div>
  </article>`;
}

function renderOrganizationBilling() {
  const context = organizationBillingContext();
  const workspace = el("organizationBillingWorkspace");
  const personalWorkspace = el("personalBillingWorkspace");
  if (workspace) workspace.classList.toggle("hidden", !context);
  if (personalWorkspace) personalWorkspace.classList.toggle("hidden", Boolean(context));
  if (!context) return false;

  const account = organizationBillingAccount();
  const today = organizationBillingUsage("today");
  const last7Days = organizationBillingUsage("last7Days");
  const last30Days = organizationBillingUsage("last30Days");
  const canTopup = !context.readOnly && canSimulateOrganizationTopup();
  const demoMode = isDemoOrganizationMode();
  const organization = organizationBillingData?.organization || {};
  const name = String(organization?.name || context.name || "客户企业");
  const isLoading = organizationBillingLoading && !organizationBillingData;
  const hasLoadError = Boolean(organizationBillingLoadError && !organizationBillingData);
  const unavailableValue = hasLoadError ? "暂不可用" : null;

  setText("organizationBillingScopeName", name);
  renderOrganizationOperationalStatus(
    "organizationBillingOperationalStatus",
    { ...organization, ...account },
  );
  setText("organizationBillingBalance", unavailableValue || (isLoading ? "加载中" : money.format(account.availableBalanceUsd)));
  setText("organizationBillingBalanceChip", unavailableValue || (isLoading ? "企业额度加载中" : `余额 ${money.format(account.availableBalanceUsd)}`));
  setText("organizationBillingTotalCredits", unavailableValue || money.format(account.totalCreditsUsd));
  setText("organizationBillingInitialCredits", unavailableValue || money.format(account.initialBalanceUsd));
  setText("organizationBillingUsageEstimate", unavailableValue || money.format(last30Days.spend));
  setText("organizationBillingUsageTokens", unavailableValue || formatTokens(last30Days.tokens));
  setText("organizationBillingUsageRequests", unavailableValue || fmt.format(last30Days.requests));
  setText(
    "organizationBillingDescription",
    demoMode
      ? `${name} 的额度仅用于本地演示。演示用量不影响企业余额，也不会发起真实付款。`
      : `${name} 的企业额度与实际用量均来自已接通的数据服务。`,
  );
  setText("organizationBillingTotalCreditsHint", demoMode ? "含初始企业额度与模拟充值" : "累计授予与退回的企业额度");
  setText("organizationBillingInitialCreditsHint", demoMode ? "每家演示企业独立初始化" : "企业开户及后续额度授予");
  setText("organizationBillingUsageLabel", demoMode ? "演示用量估算" : "近 30 天用量");
  setText(
    "organizationBillingNoteText",
    demoMode
      ? "演示用量仅帮助查看企业使用情况，不会扣减企业账户余额。本页不包含支付方式、收款码、兑换码或真实订单。"
      : "企业额度由平台运营人员维护；用量数据按实际调用汇总。本页不展示支付方式、收款码或个人充值订单。",
  );
  setText(
    "organizationBillingRecordDescription",
    demoMode ? "记录初始企业额度与后续模拟充值。所有内容均为演示数据。" : "记录平台对企业额度的授予、扣减与调整。",
  );
  document.querySelectorAll("[data-organization-demo-badge]").forEach((badge) => {
    badge.classList.toggle("hidden", !demoMode);
  });
  setHtml(
    "organizationBillingUsageCards",
    hasLoadError
      ? `<div class="organization-empty">${escapeHtml(organizationBillingLoadError)}</div>`
      : [
          organizationBillingUsageCard("今日", today),
          organizationBillingUsageCard("近 7 天", last7Days),
          organizationBillingUsageCard("近 30 天", last30Days),
        ].join(""),
  );
  const topupButton = el("openOrganizationTopupModalButton");
  if (topupButton) topupButton.classList.toggle("hidden", !canTopup || hasLoadError);
  const adjustButton = el("openOrganizationCreditAdjustmentButton");
  if (adjustButton) adjustButton.classList.toggle("hidden", !context.canAdjust || hasLoadError);
  const revokeButton = el("openOrganizationCreditRevokeButton");
  if (revokeButton) revokeButton.classList.toggle("hidden", !context.canAdjust || hasLoadError);
  const readOnlyHint = el("organizationBillingReadOnlyHint");
  if (readOnlyHint) readOnlyHint.classList.toggle("hidden", !hasLoadError && (canTopup || context.canAdjust));

  const recordsBody = el("organizationBillingRecordBody");
  const recordTotal = organizationBillingRecordTotal();
  const recordPage = organizationBillingRecordPage();
  const recordPageSize = organizationBillingRecordPageSize();
  const totalPages = Math.max(1, Math.ceil(recordTotal / recordPageSize));
  if (organizationBillingPage !== recordPage) organizationBillingPage = recordPage;
  setText("organizationBillingRecordCount", `${fmt.format(recordTotal)} 条`);
  setText("organizationBillingPageInfo", `第 ${recordPage} / ${totalPages} 页`);
  const previousButton = el("organizationBillingPreviousPageButton");
  const nextButton = el("organizationBillingNextPageButton");
  if (previousButton) previousButton.disabled = recordPage <= 1 || organizationBillingLoading;
  if (nextButton) nextButton.disabled = recordPage >= totalPages || organizationBillingLoading;
  if (!recordsBody) return true;
  if (organizationBillingLoadError) {
    recordsBody.innerHTML = `<tr><td colspan="6" class="empty">${escapeHtml(organizationBillingLoadError)}</td></tr>`;
    return true;
  }
  if (isLoading) {
    recordsBody.innerHTML = '<tr><td colspan="6" class="empty">正在加载企业额度记录…</td></tr>';
    return true;
  }
  const records = organizationBillingRecords();
  if (!records.length) {
    recordsBody.innerHTML = '<tr><td colspan="6" class="empty">暂无额度变动记录。</td></tr>';
    return true;
  }
  recordsBody.innerHTML = records.map((record) => {
    const timestamp = organizationBillingField(record, "timestamp", "timestamp") || organizationBillingField(record, "createdAt", "created_at");
    const type = organizationBillingField(record, "type", "type");
    const amount = Number(organizationBillingField(record, "amountUsd", "amount_usd") || 0);
    const balance = Number(organizationBillingField(record, "balanceAfterUsd", "balance_after_usd") || 0);
    const operator = organizationBillingField(record, "operator", "operator") || organizationBillingField(record, "operatorEmail", "operator_email") || "-";
    const status = organizationBillingField(record, "status", "status");
    return `<tr>
      <td>${escapeHtml(formatBillingTime(timestamp))}</td>
      <td>${escapeHtml(organizationBillingRecordTypeLabel(type))}</td>
      <td class="num">${escapeHtml(money.format(amount))}</td>
      <td class="num">${escapeHtml(money.format(balance))}</td>
      <td>${escapeHtml(operator)}</td>
      <td><span class="billing-order-status success">${escapeHtml(organizationBillingRecordStatusLabel(status))}</span></td>
    </tr>`;
  }).join("");
  return true;
}

function organizationBillingUrl(context = organizationBillingContext()) {
  if (!context) return "";
  const params = new URLSearchParams({
    page: String(organizationBillingPage),
    pageSize: String(organizationBillingPageSize),
  });
  return `${context.path}?${params.toString()}`;
}

function resetOrganizationBillingData() {
  organizationBillingRequestId += 1;
  organizationBillingData = null;
  organizationBillingLoading = false;
  organizationBillingLoadError = "";
  organizationBillingPage = 1;
  organizationBillingScopeKey = "";
  organizationBillingRequest = null;
  organizationBillingLoadedAt = 0;
  selectedOrganizationTopupAmount = 0;
  isOrganizationTopupSaving = false;
  isOrganizationCreditAdjusting = false;
  organizationCreditAdjustmentOperation = "grant";
  el("organizationTopupModal")?.classList.add("hidden");
  el("organizationCreditAdjustmentModal")?.classList.add("hidden");
}

function loadOrganizationBillingData(forceRefresh = false) {
  const context = organizationBillingContext();
  if (!context) return Promise.resolve();
  const scopeKey = organizationBillingContextKey();
  const queryKey = `${scopeKey}|${organizationBillingUrl(context)}`;
  const hasFreshData = organizationBillingData
    && organizationBillingScopeKey === queryKey
    && Date.now() - organizationBillingLoadedAt < ORGANIZATION_BILLING_CACHE_TTL_MS;
  if (!forceRefresh && hasFreshData) return Promise.resolve(organizationBillingData);
  if (organizationBillingRequest && organizationBillingScopeKey === queryKey) return organizationBillingRequest;
  const requestId = ++organizationBillingRequestId;
  organizationBillingLoading = true;
  organizationBillingLoadError = "";
  organizationBillingScopeKey = queryKey;
  renderOrganizationBilling();
  const request = (async () => {
    try {
      const payload = await api(organizationBillingUrl(context));
      if (requestId !== organizationBillingRequestId || scopeKey !== organizationBillingContextKey()) return null;
      organizationBillingData = payload || null;
      organizationBillingLoadedAt = Date.now();
      return organizationBillingData;
    } catch (error) {
      if (requestId !== organizationBillingRequestId || scopeKey !== organizationBillingContextKey()) return null;
      organizationBillingData = null;
      organizationBillingLoadedAt = 0;
      organizationBillingLoadError = error.message || "企业额度加载失败，请稍后重试。";
      showToast(organizationBillingLoadError);
      return null;
    } finally {
      if (organizationBillingRequest === request) organizationBillingRequest = null;
      if (requestId !== organizationBillingRequestId) return;
      organizationBillingLoading = false;
      renderOrganizationBilling();
    }
  })();
  organizationBillingRequest = request;
  return request;
}

async function changeOrganizationBillingPage(direction) {
  const totalPages = Math.max(1, Math.ceil(organizationBillingRecordTotal() / organizationBillingRecordPageSize()));
  const nextPage = Math.min(totalPages, Math.max(1, organizationBillingRecordPage() + direction));
  if (nextPage === organizationBillingPage || organizationBillingLoading) return;
  organizationBillingPage = nextPage;
  await loadOrganizationBillingData();
}

function closeOrganizationCreditAdjustmentModal(options = {}) {
  if (isOrganizationCreditAdjusting && !options.force) return;
  isOrganizationCreditAdjusting = false;
  organizationCreditAdjustmentOperation = "grant";
  el("organizationCreditAdjustmentForm")?.reset();
  el("organizationCreditAdjustmentModal")?.classList.add("hidden");
  setFieldError("organizationCreditAdjustmentError", "");
}

function openOrganizationCreditAdjustmentModal(operation = "grant") {
  const context = organizationBillingContext();
  if (!context?.canAdjust) return;
  organizationCreditAdjustmentOperation = operation === "revoke" ? "revoke" : "grant";
  el("organizationCreditAdjustmentForm")?.reset();
  setFieldError("organizationCreditAdjustmentError", "");
  setText("organizationCreditAdjustmentModalTitle", organizationCreditAdjustmentOperation === "revoke" ? "扣减企业额度" : "授予企业额度");
  setText("organizationCreditAdjustmentSubmit", organizationCreditAdjustmentOperation === "revoke" ? "确认扣减" : "确认授予");
  el("organizationCreditAdjustmentModal")?.classList.remove("hidden");
  window.setTimeout(() => el("organizationCreditAdjustmentAmount")?.focus(), 0);
}

async function submitOrganizationCreditAdjustment(event) {
  event.preventDefault();
  const context = organizationBillingContext();
  if (!context?.canAdjust || isOrganizationCreditAdjusting) return;
  const amount = Number(el("organizationCreditAdjustmentAmount")?.value || 0);
  const reason = String(el("organizationCreditAdjustmentReason")?.value || "").trim();
  const externalReference = String(el("organizationCreditAdjustmentReference")?.value || "").trim();
  setFieldError("organizationCreditAdjustmentError", "");
  if (!Number.isFinite(amount) || amount <= 0 || amount > 100000 || Math.round(amount * 100) !== amount * 100) {
    setFieldError("organizationCreditAdjustmentError", "请输入 $0.01 至 $100,000.00 的金额，最多两位小数。");
    return;
  }
  if (!reason) {
    setFieldError("organizationCreditAdjustmentError", "请填写调整原因。");
    return;
  }
  isOrganizationCreditAdjusting = true;
  setButtonLoading("organizationCreditAdjustmentSubmit", true, "提交中");
  try {
    await ensureCsrfToken();
    const idempotencyKey = `org-credit-${context.organizationId}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    await api(context.adjustmentPath, {
      method: "POST",
      body: JSON.stringify({
        operation: organizationCreditAdjustmentOperation,
        amountUsd: amount,
        reason,
        externalReference,
        idempotencyKey,
      }),
    });
    closeOrganizationCreditAdjustmentModal({ force: true });
    await loadOrganizationBillingData(true);
    showToast("企业额度已更新");
  } catch (error) {
    setFieldError("organizationCreditAdjustmentError", error.message || "企业额度调整失败，请稍后重试。");
  } finally {
    isOrganizationCreditAdjusting = false;
    setButtonLoading("organizationCreditAdjustmentSubmit", false);
  }
}

function renderOrganizationTopupOptions() {
  const options = [500, 1000, 2000, 5000];
  const container = el("organizationTopupOptions");
  if (!container) return;
  container.innerHTML = options.map((amount) => `
    <button type="button" class="billing-amount-option${amount === selectedOrganizationTopupAmount ? " active" : ""}" data-organization-topup-amount="${amount}">$${amount.toLocaleString("en-US")}</button>
  `).join("");
}

function closeOrganizationTopupModal(options = {}) {
  if (isOrganizationTopupSaving && !options.force) return;
  selectedOrganizationTopupAmount = 0;
  el("organizationTopupForm")?.reset();
  setFieldError("organizationTopupError", "");
  el("organizationTopupModal")?.classList.add("hidden");
}

function openOrganizationTopupModal() {
  const context = organizationBillingContext();
  if (!context || context.readOnly || !canSimulateOrganizationTopup()) return;
  selectedOrganizationTopupAmount = 0;
  el("organizationTopupForm")?.reset();
  setFieldError("organizationTopupError", "");
  renderOrganizationTopupOptions();
  el("organizationTopupModal")?.classList.remove("hidden");
  window.setTimeout(() => el("organizationTopupAmount")?.focus(), 0);
}

async function submitOrganizationTopup(event) {
  event.preventDefault();
  const context = organizationBillingContext();
  if (!context || context.readOnly || !canSimulateOrganizationTopup() || isOrganizationTopupSaving) return;
  if (!isDemoOrganizationMode()) return;
  const amount = Number(el("organizationTopupAmount")?.value || 0);
  setFieldError("organizationTopupError", "");
  if (!Number.isFinite(amount) || amount < 1 || amount > 100000 || Math.round(amount * 100) !== amount * 100) {
    setFieldError("organizationTopupError", "请输入 $1.00 至 $100,000.00 的金额，最多两位小数。");
    return;
  }
  isOrganizationTopupSaving = true;
  setButtonLoading("submitOrganizationTopupButton", true, "充值中");
  try {
    await ensureCsrfToken();
    await api("/api/organization/current/billing/topups", {
      method: "POST",
      body: JSON.stringify({ amountUsd: amount }),
    });
    closeOrganizationTopupModal({ force: true });
    await loadOrganizationBillingData(true);
    showToast("模拟充值已完成，未发起真实付款。");
  } catch (error) {
    setFieldError("organizationTopupError", error.message || "模拟充值失败，请稍后重试。");
  } finally {
    isOrganizationTopupSaving = false;
    setButtonLoading("submitOrganizationTopupButton", false);
  }
}

function renderBilling() {
  if (renderOrganizationBilling()) return;
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

function loadBillingData(forceRefresh = false) {
  if (!currentUser) return Promise.resolve();
  const hasFreshData = billingConfig
    && Date.now() - billingLoadedAt < BILLING_CACHE_TTL_MS;
  if (!forceRefresh && hasFreshData) return Promise.resolve(billingAccount);
  if (billingRequest) return billingRequest;
  isBillingLoading = true;
  billingLoadError = "";
  renderBillingOrders();
  const request = (async () => {
    try {
      const payload = await api("/api/me/billing");
      billingConfig = payload.config || null;
      billingAccount = payload.account || null;
      billingOrders = Array.isArray(payload.orders?.items) ? payload.orders.items : [];
      billingOrderTotal = Number(payload.orders?.total || 0);
      billingLoadedAt = Date.now();
      return billingAccount;
    } catch (error) {
      billingOrders = [];
      billingOrderTotal = 0;
      billingLoadedAt = 0;
      billingLoadError = error.message || "充值信息加载失败，请稍后重试。";
      if (error.status !== 404) showToast(billingLoadError);
      return null;
    } finally {
      if (billingRequest === request) billingRequest = null;
      isBillingLoading = false;
      renderBilling();
    }
  })();
  billingRequest = request;
  return request;
}

async function refreshBillingAvailability() {
  // 后端未开放充值时接口返回 404，据此决定导航项是否出现。
  // Customer identities have a separate organization credit contract. Never
  // use this legacy probe for them because it targets a personal account.
  if (isOrganizationCustomerIdentity()) {
    billingAvailable = false;
    updateBillingNav();
    return;
  }
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
    billingLoadedAt = Date.now();
    billingAvailable = Boolean(billingConfig?.enabled);
  } catch {
    billingAvailable = false;
  }
  updateBillingNav();
  if (billingAvailable) renderBilling();
}

function updateBillingNav() {
  syncNavigationVisibility();
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
    await loadBillingData(true);
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
    await loadBillingData(true);
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
        await loadBillingData(true);
        await refreshEntitlementAfterTopup();
      } else if (["failed", "expired"].includes(String(payload.order?.status || ""))) {
        stopTopupPolling();
        pendingTopupTradeNo = "";
        el("billingPayPanel")?.classList.add("hidden");
        showToast(payload.order?.reviewNote || "本次支付未完成，如已付款请联系管理员");
        await loadBillingData(true);
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
  return Boolean(isPlatformAdmin() && billingAvailable && !isViewingCustomerOrganization());
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

function organizationProvisioningStatus(record) {
  if (!record || typeof record !== "object") return "";
  return String(
    record.provisioningStatus
      ?? record.provisioning_status
      ?? record.upstreamStatus
      ?? record.upstream_status
      ?? "",
  ).trim().toLowerCase();
}

const ORGANIZATION_ROLE_LABELS = {
  admin: "企业管理员",
  member: "成员",
};

const ORGANIZATION_STATUS_LABELS = {
  active: "已启用",
  invited: "待邀请",
  suspended: "已暂停",
  removed: "已移除",
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
  // Keep older payloads usable without giving ordinary members the
    // organization-wide boards while a server bundle is being upgraded.
  return Boolean(
    organizationEnabled()
    && String(currentUser?.organizationRole || "") === "admin",
  );
}

function canViewOrganizationBilling() {
  return Boolean(currentUser?.canViewOrganizationBilling);
}

function canSimulateOrganizationTopup() {
  return Boolean(isDemoOrganizationMode() && currentUser?.canSimulateOrganizationTopup);
}

function organizationBillingContext() {
  if (isViewingCustomerOrganization()) {
    const organizationId = selectedCustomerOrganizationId();
    if (!organizationId) return null;
    const canAdjust = Boolean(
      isRealOrganizationMode()
      && currentUser?.canAdjustOrganizationCredit
      && customerOrganizationStatus(selectedCustomerOrganization) !== "archived",
    );
    return {
      kind: "platformCustomer",
      organizationId,
      name: selectedCustomerOrganizationName(),
      readOnly: true,
      canAdjust,
      path: `${customerOrganizationPath(organizationId)}/billing`,
      adjustmentPath: `${customerOrganizationPath(organizationId)}/billing/adjustments`,
    };
  }
  if (!isOrganizationCustomerIdentity() || !canViewOrganizationBilling()) return null;
  return {
    kind: "currentOrganization",
    organizationId: String(currentUser?.organizationId || ""),
    name: String(currentUser?.organization?.name || currentUser?.organizationName || "我的企业"),
    readOnly: !canSimulateOrganizationTopup(),
    path: "/api/organization/current/billing",
  };
}

function organizationBillingContextKey() {
  const context = organizationBillingContext();
  return context ? `${context.kind}:${context.organizationId}` : "";
}

function isOrganizationBillingView() {
  return Boolean(organizationBillingContext());
}

function canAccessBillingView() {
  // Seller accounts keep their global billing operations; customer accounts
  // use the enterprise credit workspace when the server grants that capability.
  const personalBillingAvailable = Boolean(billingAvailable && !isOrganizationCustomerIdentity());
  return Boolean(isOrganizationBillingView() || personalBillingAvailable);
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

function organizationStatusChip(item) {
  const record = customerOrganizationRecord(item);
  const provisioningStatus = organizationProvisioningStatus(record);
  const billingStatus = String(organizationField(record, "billingStatus", "billing_status") || "").trim().toLowerCase();
  if (["failed", "error", "degraded"].includes(provisioningStatus)) return { label: "同步失败", tone: "rose" };
  if (["pending", "provisioning", "creating"].includes(provisioningStatus)) return { label: "开通中", tone: "gold" };
  if (["past_due", "insufficient", "blocked", "suspended"].includes(billingStatus)) return { label: "余额不足", tone: "rose" };
  return { label: customerOrganizationStatus(item) === "archived" ? "已归档" : "正常", tone: "" };
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
      // scope never flashes as a generic placeholder for an administrator.
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
  adminUsageRequestController?.abort();
  departmentUsageRequestController?.abort();
  adminUsageRequestId += 1;
  departmentUsageRequestId += 1;
  adminUsageRequestController = null;
  adminUsageQueryKey = "";
  adminUsageInFlight = null;
  departmentUsageRequestController = null;
  departmentUsageQueryKey = "";
  departmentUsageInFlight = null;
  isAdminLoading = false;
  isDepartmentLoading = false;
  selectedAdminEmployee = "";
  selectedDepartment = "";
  resetDepartmentEmployeeSelection();
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

// /api/auth/me 已经包含身份与企业权限，因此登录后可以立即决定绝大多数入口。
// 只有旧版 SSO 团队负责人范围需要等待 /api/auth/scope，回来后再补充团队看板。
function syncNavigationVisibility() {
  // 切换账号时先关闸，避免上一身份的入口短暂泄漏给下一身份。
  if (!isNavigationRevealed) return;
  const canBrowseCustomers = customerOrganizationsAvailable();
  const canManageCurrentOrganization = Boolean(currentUser?.canManageOrganization);
  const canViewAdmin = canViewAdminUsage();
  const canViewDepartments = canViewDepartmentUsage();
  const isCustomer = isOrganizationCustomerIdentity();
  // A customer administrator gets an enterprise-credit entry. Seller accounts
  // keep their global billing operations in the admin board, never in this
  // customer-facing sidebar destination.
  const canUseBillingSidebar = isCustomer
    ? canViewOrganizationBilling()
    : Boolean(billingAvailable);
  const remoteDemoSnapshotOnly = Boolean(authConfig.remoteDemoUsageSnapshotOnly || authConfig.remoteDemoReadOnly);
  el("customersTab").classList.toggle("hidden", !canBrowseCustomers);
  el("resetCustomerOrganizationsDemoButton")?.classList.toggle("hidden", !isDemoOrganizationMode());
  el("organizationTab")?.classList.toggle("hidden", !canManageCurrentOrganization);
  // 企业令牌是甲方管理员的一级目的地。乙方运营不在侧边栏出现，他们从客户企业
  // 下钻后在企业详情标签里只读查看。
  el("organizationTokensTab")?.classList.toggle("hidden", !canManageCurrentOrganization);
  if (!canManageCurrentOrganization && currentUser?.canManageOrganizationTokens) {
    el("organizationTokensTab")?.classList.remove("hidden");
  }
  el("adminTab").classList.toggle("hidden", !canViewAdmin);
  el("departmentTab").classList.toggle("hidden", !canViewDepartments);
  el("teamTab").classList.toggle("hidden", !currentUser?.isTeamLeader);
  // 个人用量对每个登录身份都成立，不依赖任何上游权限探测。
  el("dashboardTab")?.classList.remove("hidden");
  // Customer identities use their tenant-scoped views only; never expose
  // seller account functions that lack a customer-local contract.
  document.querySelectorAll('[data-view="keys"]').forEach((button) => {
    button.classList.toggle("hidden", isCustomer || remoteDemoSnapshotOnly);
  });
  el("billingTab")?.classList.toggle("hidden", !canUseBillingSidebar);
  el("stabilityTab")?.classList.toggle("hidden", !canViewStability() || remoteDemoSnapshotOnly);
  el("costControlTab")?.classList.toggle("hidden", !canViewCosts() || remoteDemoSnapshotOnly);
  el("governanceWorkbenchTab")?.classList.toggle("hidden", !(canManageCosts() || canReconcileCosts()) || remoteDemoSnapshotOnly);
  syncMobileViewPicker();
  document.querySelectorAll('[data-global-page="models"]').forEach((button) => {
    button.classList.toggle("hidden", isCustomer || remoteDemoSnapshotOnly);
  });
}

// 身份落地后立即收走骨架。scope 随后到达时重复调用会补齐新增入口。
function revealNavigation() {
  isNavigationRevealed = true;
  syncNavigationVisibility();
  el("navSkeleton")?.classList.add("hidden");
  const tabs = el("viewTabs");
  if (tabs) {
    tabs.classList.remove("nav-pending");
    tabs.removeAttribute("aria-busy");
  }
  syncMobileViewPicker();
}

// 换账号或退出登录时把导航退回骨架态，避免上一个身份的可见项闪现给下一个身份。
function resetNavigationToPending() {
  isNavigationRevealed = false;
  el("navSkeleton")?.classList.remove("hidden");
  const tabs = el("viewTabs");
  if (tabs) {
    tabs.classList.add("nav-pending");
    tabs.setAttribute("aria-busy", "true");
  }
  document.querySelectorAll('.sidebar [data-view]').forEach((button) => {
    button.classList.add("hidden");
  });
}

// 客户/企业范围的五个页面共用正文顶部这一条工作台导航。乙方下钻时它常驻，
// 让「组织信息 ↔ 全员看板 ↔ 部门看板 ↔ 企业额度 ↔ 令牌管理」可以直接互切；
// 甲方只在企业管理页出现，避免和侧边栏的同名一级入口重复。
const ORGANIZATION_WORKSPACE_VIEWS = ["organization", "admin", "department", "billing", "organization-tokens"];

const ORGANIZATION_WORKSPACE_TABS = {
  organization: "info",
  admin: "usage",
  department: "departments-usage",
  billing: "billing",
  "organization-tokens": "tokens",
};

function renderOrganizationWorkspaceBar(view = currentView) {
  const bar = el("organizationWorkspaceBar");
  if (!bar) return;
  const isPlatformCustomer = isViewingCustomerOrganization();
  const visible = isPlatformCustomer
    ? ORGANIZATION_WORKSPACE_VIEWS.includes(view)
    : view === "organization" && canViewCurrentOrganizationUsage();
  bar.classList.toggle("hidden", !visible);
  el("backToCustomersButton")?.classList.toggle("hidden", !isPlatformCustomer);
  if (!visible) return;
  // 高亮按当前视图反推，这样从侧边栏、面包屑或深链进入时标签态都不会错位。
  customerOrganizationDetailTab = ORGANIZATION_WORKSPACE_TABS[view] || "info";
  renderOrganizationUsageTabs();
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

const ORGANIZATION_CLAIM_STATUS_LABELS = {
  pending: "等待激活",
  created: "等待激活",
  pending_activation: "等待激活",
  pending_approval: "待平台审核",
  accepted_pending_approval: "待平台审核",
  approved: "已审核通过",
  provisioning: "账号开通中",
  active: "已生效",
  revoked: "已撤销",
  expired: "已过期",
};

function organizationClaimStatus(claim) {
  return String(organizationField(claim, "status", "status") || "pending").trim().toLowerCase();
}

function organizationClaimStatusLabel(status) {
  return ORGANIZATION_CLAIM_STATUS_LABELS[String(status || "")] || "未知";
}

function organizationClaimStatusTone(status) {
  if (["approved", "active"].includes(status)) return "active";
  if (["revoked", "expired"].includes(status)) return "suspended";
  return "invited";
}

function platformCanManageOrganizationClaims() {
  if (!isViewingCustomerOrganization() || !isRealOrganizationMode()) return false;
  if (customerOrganizationStatus(selectedCustomerOrganization) === "archived") return false;
  if (currentUser?.canManageOrganizationClaims !== undefined) {
    return Boolean(currentUser.canManageOrganizationClaims);
  }
  return isPlatformAdmin();
}

function organizationClaimsPath(suffix = "") {
  const organizationId = selectedCustomerOrganizationId();
  return `${customerOrganizationPath(organizationId)}/membership-claims${suffix}`;
}

function clearOrganizationClaimLastUrl() {
  organizationClaimLastUrl = "";
  setText("organizationClaimLastUrl", "");
  el("organizationClaimLastResult")?.classList.add("hidden");
}

function resetOrganizationClaims() {
  organizationClaims = [];
  organizationClaimLoadError = "";
  isOrganizationClaimLoading = false;
  isOrganizationClaimSaving = false;
  clearOrganizationClaimLastUrl();
}

function platformCanAdoptOrganization() {
  if (!isViewingCustomerOrganization() || !isRealOrganizationMode()) return false;
  if (customerOrganizationStatus(selectedCustomerOrganization) === "archived") return false;
  if (currentUser?.canAdoptCustomerOrganization !== undefined) {
    return Boolean(currentUser.canAdoptCustomerOrganization);
  }
  return isPlatformAdmin();
}

function resetOrganizationAdoption() {
  organizationAdoptionPreview = null;
  organizationAdoptionFingerprint = "";
  organizationAdoptionIdempotencyKey = "";
  organizationAdoptionLoadError = "";
  isOrganizationAdoptionLoading = false;
  isOrganizationAdoptionApplying = false;
}

function organizationAdoptionRequestBody() {
  return {
    organizationName: String(el("organizationAdoptionNameInput")?.value || "").trim(),
    departmentName: String(el("organizationAdoptionDepartmentInput")?.value || "").trim(),
    adminName: String(el("organizationAdoptionAdminNameInput")?.value || "").trim(),
    adminEmail: String(el("organizationAdoptionAdminEmailInput")?.value || "").trim().toLowerCase(),
    principalName: String(el("organizationAdoptionPrincipalInput")?.value || "").trim(),
    // Stable source candidates and the two historical asset aliases are
    // resolved by the server-side pilot configuration. Do not place them in
    // HTML, browser storage, URLs, or user-facing copy.
    organizationCandidates: [],
    teamCandidates: [],
    keyAliases: [],
    effectiveFrom: String(el("organizationAdoptionEffectiveFromInput")?.value || "").trim(),
    effectiveThrough: String(el("organizationAdoptionEffectiveThroughInput")?.value || "").trim(),
  };
}

function organizationAdoptionActionLabel(record, subject) {
  const action = String(organizationField(record, "action", "action") || "").trim().toLowerCase();
  if (["adopt", "reuse", "existing"].includes(action)) return `${subject}将接管现有对象`;
  if (["create", "new"].includes(action)) return `${subject}将创建新对象`;
  return `${subject}已确认`;
}

function renderOrganizationAdoption() {
  const panel = el("organizationAdoptionPanel");
  if (!panel) return;
  const visible = platformCanAdoptOrganization();
  panel.classList.toggle("hidden", !visible);
  if (!visible) return;

  const status = el("organizationAdoptionStatus");
  const result = el("organizationAdoptionResult");
  const previewButton = el("previewOrganizationAdoptionButton");
  const applyButton = el("applyOrganizationAdoptionButton");
  if (previewButton) previewButton.disabled = isOrganizationAdoptionLoading || isOrganizationAdoptionApplying;
  if (applyButton) {
    applyButton.disabled = !organizationAdoptionFingerprint || isOrganizationAdoptionLoading || isOrganizationAdoptionApplying;
    applyButton.classList.toggle("hidden", !organizationAdoptionFingerprint);
  }

  if (isOrganizationAdoptionLoading) {
    status.className = "operational-status warning";
    status.innerHTML = `<span class="operational-status-icon" aria-hidden="true">${icon("warning")}</span><div><strong>正在执行接管预检</strong><p>正在核对企业、部门和历史资产的唯一归属。预检完成前不会写入任何业务数据。</p></div>`;
    status.classList.remove("hidden");
  } else if (isOrganizationAdoptionApplying) {
    status.className = "operational-status warning";
    status.innerHTML = `<span class="operational-status-icon" aria-hidden="true">${icon("warning")}</span><div><strong>正在应用接管</strong><p>正在创建本地映射、David 邀请和历史资产只读归属，请勿重复提交。</p></div>`;
    status.classList.remove("hidden");
  } else if (organizationAdoptionLoadError) {
    status.className = "operational-status danger";
    status.innerHTML = `<span class="operational-status-icon" aria-hidden="true">${icon("warning")}</span><div><strong>接管预检冲突</strong><p>${escapeHtml(organizationAdoptionLoadError)}</p></div>`;
    status.classList.remove("hidden");
  } else if (organizationAdoptionPreview) {
    const applied = ["applied", "idempotent"].includes(String(organizationAdoptionPreview.status || ""));
    status.className = `operational-status ${applied ? "" : "warning"}`;
    status.innerHTML = `<span class="operational-status-icon" aria-hidden="true">${icon(applied ? "check" : "warning")}</span><div><strong>${applied ? "真实接管已完成" : "接管预检已通过"}</strong><p>${applied ? "企业、部门、管理员邀请与历史资产归属已持久化。" : "范围与归属一致，可以应用接管；应用前不会改变现有调用权限。"}</p></div>`;
    status.classList.remove("hidden");
  } else {
    status.classList.add("hidden");
    status.innerHTML = "";
  }

  if (!result) return;
  const preview = organizationAdoptionPreview;
  if (!preview) {
    result.classList.add("hidden");
    result.innerHTML = "";
    return;
  }
  const organization = preview.organization || {};
  const department = preview.department || {};
  const assets = preview.legacyAssets || preview.legacy_assets || {};
  const assetCount = Number(assets.count || 0);
  const identityCount = Number(assets.originalIdentityCount ?? assets.original_identity_count ?? 0);
  const scopeConsistent = Boolean(
    (organizationField(department, "scopeConsistent", "scope_consistent") ?? true)
    && (organizationField(assets, "scopeConsistent", "scope_consistent") ?? true),
  );
  const principalName = String(organizationField(assets, "principalName", "principal_name") || "梁海强");
  result.innerHTML = `
    <div class="organization-adoption-result-head">
      <div><span>企业范围</span><strong>${escapeHtml(organizationAdoptionActionLabel(organization, "企业"))}</strong></div>
      <div><span>部门范围</span><strong>${escapeHtml(organizationAdoptionActionLabel(department, "部门"))}</strong></div>
      <div><span>归属核对</span><strong>${scopeConsistent ? "范围一致" : "需要人工复核"}</strong></div>
    </div>
    <div class="organization-adoption-assets">
      <strong>${fmt.format(assetCount)} 项历史资产已确认</strong>
      <p>${fmt.format(identityCount)} 个原始调用身份将聚合为“${escapeHtml(principalName)}”的企业用量报表；原始审计身份仍分别保留。</p>
      <span class="organization-status invited">历史资产、只读、不计企业额度</span>
    </div>
  `;
  result.classList.remove("hidden");
}

async function previewOrganizationAdoption(event) {
  event?.preventDefault();
  if (!platformCanAdoptOrganization() || isOrganizationAdoptionLoading || isOrganizationAdoptionApplying) return;
  const body = organizationAdoptionRequestBody();
  if (!body.organizationName || !body.departmentName || !body.adminName || !validEmail(body.adminEmail) || !body.principalName || !body.effectiveFrom || !body.effectiveThrough) {
    setFieldError("organizationAdoptionError", "请完整填写企业、部门、管理员、历史资产负责人和有效时间窗。");
    return;
  }
  organizationAdoptionPreview = null;
  organizationAdoptionFingerprint = "";
  organizationAdoptionIdempotencyKey = "";
  organizationAdoptionLoadError = "";
  setFieldError("organizationAdoptionError", "");
  isOrganizationAdoptionLoading = true;
  setButtonLoading("previewOrganizationAdoptionButton", true, "正在预检");
  renderOrganizationAdoption();
  try {
    await ensureCsrfToken();
    const payload = await api("/api/platform/organization-adoptions/preview", {
      method: "POST",
      body: JSON.stringify(body),
    });
    organizationAdoptionPreview = payload || null;
    organizationAdoptionFingerprint = String(payload?.previewFingerprint || payload?.preview_fingerprint || "");
    organizationAdoptionIdempotencyKey = String(payload?.idempotencyKey || payload?.idempotency_key || `baic-pilot-adoption-${Date.now()}`);
    if (!organizationAdoptionFingerprint) throw new Error("预检响应缺少可验证凭据，请重新预检。");
    showToast("接管预检已通过");
  } catch (error) {
    organizationAdoptionPreview = null;
    organizationAdoptionFingerprint = "";
    organizationAdoptionIdempotencyKey = "";
    organizationAdoptionLoadError = error.code === "ORGANIZATION_ADOPTION_CONFLICT"
      ? (error.message || "候选对象存在冲突，未执行任何写入。")
      : (error.message || "接管预检失败，请稍后重试。");
  } finally {
    isOrganizationAdoptionLoading = false;
    setButtonLoading("previewOrganizationAdoptionButton", false);
    renderOrganizationAdoption();
  }
}

async function applyOrganizationAdoption() {
  if (!platformCanAdoptOrganization() || !organizationAdoptionFingerprint || isOrganizationAdoptionApplying) return;
  isOrganizationAdoptionApplying = true;
  organizationAdoptionLoadError = "";
  setButtonLoading("applyOrganizationAdoptionButton", true, "正在应用");
  renderOrganizationAdoption();
  try {
    await ensureCsrfToken();
    const payload = await api("/api/platform/organization-adoptions/apply", {
      method: "POST",
      body: JSON.stringify({
        ...organizationAdoptionRequestBody(),
        previewFingerprint: organizationAdoptionFingerprint,
        idempotencyKey: organizationAdoptionIdempotencyKey,
      }),
    });
    organizationAdoptionPreview = payload || { status: "applied" };
    organizationAdoptionFingerprint = "";
    organizationAdoptionIdempotencyKey = "";
    showToast(payload?.status === "idempotent" ? "该接管已完成，无需重复执行" : "真实接管已完成");
    await Promise.all([loadOrganizationData(), loadOrganizationTokens(), loadOrganizationClaims()]);
  } catch (error) {
    organizationAdoptionLoadError = error.code === "ORGANIZATION_ADOPTION_CONFLICT"
      ? (error.message || "企业或历史资产状态已变化，请重新执行预检。")
      : (error.message || "接管应用失败，请重新预检后再试。");
    organizationAdoptionFingerprint = "";
    organizationAdoptionIdempotencyKey = "";
  } finally {
    isOrganizationAdoptionApplying = false;
    setButtonLoading("applyOrganizationAdoptionButton", false);
    renderOrganizationAdoption();
  }
}

function renderOrganizationClaimDepartmentOptions() {
  const select = el("organizationClaimDepartmentInput");
  if (!select) return;
  const selected = select.value;
  const departments = organizationDepartments().filter((department) => String(department.status || "active") !== "archived");
  select.innerHTML = departments.length
    ? departments.map((department) => {
        const id = organizationDepartmentId(department);
        return `<option value="${escapeHtml(id)}">${escapeHtml(department.name || "未命名部门")}</option>`;
      }).join("")
    : '<option value="">请先创建部门</option>';
  if (departments.some((department) => organizationDepartmentId(department) === selected)) select.value = selected;
  select.disabled = !departments.length || isOrganizationClaimSaving;
}

function renderOrganizationClaims() {
  const panel = el("organizationClaimPanel");
  if (!panel) return;
  const visible = platformCanManageOrganizationClaims();
  panel.classList.toggle("hidden", !visible);
  if (!visible) return;
  setText("organizationClaimCountChip", `${fmt.format(organizationClaims.length)} 条`);
  renderOrganizationClaimDepartmentOptions();
  const submit = el("submitOrganizationClaimButton");
  if (submit) submit.disabled = isOrganizationClaimSaving || !organizationDepartments().length;
  const table = el("organizationClaimTable");
  if (!table) return;
  if (organizationClaimLoadError) {
    table.innerHTML = `<tr><td colspan="6"><div class="organization-empty">${escapeHtml(organizationClaimLoadError)}</div></td></tr>`;
    return;
  }
  if (isOrganizationClaimLoading) {
    table.innerHTML = '<tr><td colspan="6"><div class="organization-empty">正在加载账号认领记录…</div></td></tr>';
    return;
  }
  if (!organizationClaims.length) {
    table.innerHTML = '<tr><td colspan="6"><div class="organization-empty">还没有账号认领记录。可为已有调用账号生成一次性激活链接。</div></td></tr>';
    return;
  }
  table.innerHTML = organizationClaims.map((claim) => {
    const id = String(organizationField(claim, "id", "claim_id") || organizationField(claim, "claimId", "claim_id") || "");
    const loginName = String(organizationField(claim, "loginName", "login_name") || "");
    const name = String(organizationField(claim, "memberName", "member_name") || organizationField(claim, "name", "name") || "-");
    const role = String(organizationField(claim, "role", "role") || "member");
    const status = organizationClaimStatus(claim);
    const expiresAt = organizationField(claim, "expiresAt", "expires_at");
    const canApprove = status === "accepted_pending_approval";
    const canRevoke = ["pending", "created", "accepted_pending_approval"].includes(status);
    return `
      <tr>
        <td><div class="organization-member-identity"><strong>${escapeHtml(loginName || "-")}</strong><span>企业账号</span></div></td>
        <td>${escapeHtml(name)}</td>
        <td>${escapeHtml(organizationRoleLabel(role))}</td>
        <td><span class="organization-status ${escapeHtml(organizationClaimStatusTone(status))}">${escapeHtml(organizationClaimStatusLabel(status))}</span></td>
        <td>${escapeHtml(organizationDate(expiresAt))}</td>
        <td><div class="organization-member-actions">
          <button class="primary-btn" type="button" data-organization-claim-approve="${escapeHtml(id)}" ${canApprove ? "" : "disabled"}>审核通过</button>
          <button class="danger-outline-btn" type="button" data-organization-claim-revoke="${escapeHtml(id)}" ${canRevoke ? "" : "disabled"}>撤销</button>
        </div></td>
      </tr>
    `;
  }).join("");
}

async function loadOrganizationClaims() {
  if (!platformCanManageOrganizationClaims() || isOrganizationClaimLoading) return;
  const scopeKey = organizationUsageScopeKey();
  isOrganizationClaimLoading = true;
  organizationClaimLoadError = "";
  renderOrganizationClaims();
  try {
    const payload = await api(organizationClaimsPath());
    if (scopeKey !== organizationUsageScopeKey()) return;
    organizationClaims = Array.isArray(payload?.items)
      ? payload.items
      : Array.isArray(payload?.claims)
        ? payload.claims
        : Array.isArray(payload)
          ? payload
          : [];
  } catch (error) {
    if (scopeKey !== organizationUsageScopeKey()) return;
    organizationClaims = [];
    organizationClaimLoadError = error.message || "账号认领记录加载失败，请稍后重试。";
  } finally {
    isOrganizationClaimLoading = false;
    renderOrganizationClaims();
  }
}

function organizationClaimUrlFromPayload(payload) {
  const claim = payload?.claim && typeof payload.claim === "object" ? payload.claim : {};
  const rawUrl = String(payload?.activationUrl || payload?.activation_url || payload?.claimUrl || payload?.claim_url || payload?.url || claim.activationUrl || claim.activation_url || claim.claimUrl || claim.claim_url || claim.url || "").trim();
  if (rawUrl) return new URL(rawUrl, window.location.origin).toString();
  const token = String(payload?.token || payload?.claimToken || payload?.claim_token || claim.token || claim.claimToken || claim.claim_token || "").trim();
  if (!token) return "";
  const url = new URL(window.location.origin + window.location.pathname);
  url.searchParams.set("organization_claim", token);
  return url.toString();
}

async function submitOrganizationClaim(event) {
  event.preventDefault();
  if (!platformCanManageOrganizationClaims() || isOrganizationClaimSaving) return;
  const name = el("organizationClaimMemberNameInput").value.trim();
  const loginName = el("organizationClaimLoginNameInput").value.trim().toLowerCase();
  const departmentId = el("organizationClaimDepartmentInput").value;
  const role = el("organizationClaimRoleInput").value;
  setFieldError("organizationClaimFormError", "");
  if (!name || !/^[a-z0-9._-]{2,80}$/i.test(loginName) || !departmentId) {
    setFieldError("organizationClaimFormError", "请填写姓名、2 至 80 位企业账号，并选择所属部门。");
    return;
  }
  isOrganizationClaimSaving = true;
  setButtonLoading("submitOrganizationClaimButton", true, "正在生成");
  renderOrganizationClaims();
  try {
    await ensureCsrfToken();
    const payload = await api(organizationClaimsPath(), {
      method: "POST",
      body: JSON.stringify({ memberName: name, loginName, departmentId, role }),
    });
    organizationClaimLastUrl = organizationClaimUrlFromPayload(payload);
    if (organizationClaimLastUrl) {
      setText("organizationClaimLastUrl", organizationClaimLastUrl);
      el("organizationClaimLastResult")?.classList.remove("hidden");
    }
    showToast("企业账号激活链接已生成");
    await loadOrganizationClaims();
    el("organizationClaimForm").reset();
    el("organizationClaimRoleInput").value = "admin";
  } catch (error) {
    setFieldError("organizationClaimFormError", error.message || "企业账号激活链接生成失败，请稍后重试。");
  } finally {
    isOrganizationClaimSaving = false;
    setButtonLoading("submitOrganizationClaimButton", false);
    renderOrganizationClaims();
  }
}

async function mutateOrganizationClaim(claimId, action) {
  if (!platformCanManageOrganizationClaims() || !claimId) return;
  try {
    await ensureCsrfToken();
    await api(organizationClaimsPath(`/${encodeURIComponent(claimId)}/${action}`), {
      method: "POST",
      body: JSON.stringify({}),
    });
    showToast(action === "approve" ? "账号认领已审核通过" : "账号认领已撤销");
    await Promise.all([loadOrganizationClaims(), loadOrganizationData()]);
  } catch (error) {
    showToast(error.message || (action === "approve" ? "账号认领审核失败" : "账号认领撤销失败"));
  }
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

function organizationOperationalState(organization = {}) {
  const provisioningStatus = organizationProvisioningStatus(organization);
  const billingStatus = String(organizationField(organization, "billingStatus", "billing_status") || "").trim().toLowerCase();
  if (["failed", "error", "degraded"].includes(provisioningStatus)) {
    return { tone: "danger", title: "企业账号同步失败", description: "企业资源尚未完成同步，请联系平台运营人员重试。" };
  }
  if (["pending", "provisioning", "creating"].includes(provisioningStatus)) {
    return { tone: "warning", title: "企业账号开通中", description: "企业资源正在准备，完成后即可使用企业看板与额度。" };
  }
  if (["past_due", "insufficient", "blocked", "suspended"].includes(billingStatus)) {
    return { tone: "danger", title: "企业余额不足", description: "企业额度已不足或待处理，新的调用可能会被暂停，请联系平台运营人员。" };
  }
  return null;
}

function renderOrganizationOperationalStatus(containerId, organization = {}) {
  const container = el(containerId);
  if (!container) return;
  const state = organizationOperationalState(organization);
  container.classList.toggle("hidden", !state);
  if (!state) {
    container.innerHTML = "";
    return;
  }
  container.className = `operational-status ${state.tone}`;
  container.innerHTML = `
    <span class="operational-status-icon" aria-hidden="true">${icon("warning")}</span>
    <div><strong>${escapeHtml(state.title)}</strong><p>${escapeHtml(state.description)}</p></div>
  `;
}

function organizationBillingOperationalState(account = {}, organization = {}) {
  const merged = {
    ...organization,
    billingStatus: organizationField(account, "billingStatus", "billing_status") || organizationField(organization, "billingStatus", "billing_status"),
  };
  const state = organizationOperationalState(merged);
  if (state) return state;
  const pastDue = Boolean(account?.pastDue);
  if (pastDue) return { tone: "danger", title: "企业余额不足", description: "当前额度处于待处理状态，请联系平台运营人员补充或调整额度。" };
  return null;
}

function organizationCanView() {
  return Boolean(isViewingCustomerOrganization() || currentUser?.canManageOrganization);
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
  if (organizationLoadError) {
    container.innerHTML = `<div class="organization-empty">${escapeHtml(organizationLoadError)}</div>`;
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
  const isLoading = isOrganizationLoading || isOrganizationMemberLoading;
  el("organizationPreviousPageButton").disabled = page <= 1 || isLoading;
  el("organizationNextPageButton").disabled = page >= totalPages || isLoading;
  if (isLoading && !organizationMembers.length) {
    table.innerHTML = '<tr><td colspan="6"><div class="organization-empty">正在加载成员…</div></td></tr>';
    return;
  }
  const memberLoadError = organizationMemberLoadError || organizationLoadError;
  if (memberLoadError) {
    table.innerHTML = `<tr><td colspan="6"><div class="organization-empty">${escapeHtml(memberLoadError)}</div></td></tr>`;
    return;
  }
  if (!organizationMembers.length) {
    table.innerHTML = '<tr><td colspan="6"><div class="organization-empty">没有符合当前筛选条件的成员。</div></td></tr>';
    return;
  }
  const canManage = organizationCanManage();
  const canBindIdentity = organizationCanBindMemberIdentity();
  table.innerHTML = organizationMembers.map((member) => {
    const id = organizationMemberId(member);
    const name = member.name || "未命名成员";
    const email = member.email || "-";
    const role = String(member.role || "member");
    const status = String(member.status || "invited");
    const departmentName = organizationField(member, "departmentName", "department_name") || "未分配部门";
    const isTeamLeader = String(organizationField(member, "teamRole", "team_role") || "member") === "leader";
    const joinedAt = organizationField(member, "createdAt", "created_at");
    const realMode = isRealOrganizationMode();
    // 已移除成员是只读墓碑：令牌与账号绑定都已撤销，没有可以就地恢复的操作，
    // 保留这一行只是为了让历史用量有署名、并且管理员能回查移除了谁。
    const isRemoved = status === "removed";
    const removeButton = `<button class="danger-outline-btn" type="button" data-organization-member-remove="${escapeHtml(id)}" ${canManage ? "" : "disabled"}>删除</button>`;
    let statusActions = "";
    if (isRemoved) {
      statusActions = "";
    } else if (status === "invited" && realMode) {
      // 还没接受邀请的成员手上没有任何访问能力，可以直接删除；撤销邀请只作废链接，
      // 人还留在名册里等重发，两个动作解决的不是同一件事。
      statusActions = `
            <button class="ghost-btn" type="button" data-organization-member-invitation-resend="${escapeHtml(id)}" ${canManage ? "" : "disabled"}>重发邀请</button>
            <button class="danger-outline-btn" type="button" data-organization-member-invitation-revoke="${escapeHtml(id)}" ${canManage ? "" : "disabled"}>撤销邀请</button>
            ${removeButton}
          `;
    } else if (status === "suspended") {
      statusActions = realMode
        ? `<button class="ghost-btn" type="button" data-organization-member-reinvite="${escapeHtml(id)}" ${canManage ? "" : "disabled"}>重新邀请</button>${removeButton}`
        : `<button class="ghost-btn" type="button" data-organization-member-status="${escapeHtml(id)}" data-organization-member-next-status="active" ${canManage ? "" : "disabled"}>恢复</button>${removeButton}`;
    } else {
      statusActions = `<button class="danger-outline-btn" type="button" data-organization-member-status="${escapeHtml(id)}" data-organization-member-next-status="suspended" ${canManage ? "" : "disabled"}>暂停</button>`;
    }
    return `
      <tr>
        <td>
          <div class="organization-member-name">
            <span class="organization-member-avatar ${avatarTone(member.email || name)}" aria-hidden="true">${escapeHtml(initials(email, name))}</span>
            <div class="organization-member-identity"><strong>${escapeHtml(name)}</strong><span>${escapeHtml(email)}</span></div>
          </div>
        </td>
        <td>${escapeHtml(departmentName)}${isTeamLeader ? '<span class="organization-team-role">负责人</span>' : ""}</td>
        <td><span class="organization-role ${escapeHtml(role)}">${escapeHtml(organizationRoleLabel(role))}</span></td>
        <td><span class="organization-status ${escapeHtml(status)}">${escapeHtml(organizationStatusLabel(status))}</span></td>
        <td>${escapeHtml(organizationDate(joinedAt))}</td>
        <td>
          <div class="organization-member-actions">
            ${isRemoved ? "" : `<button class="ghost-btn" type="button" data-organization-member-edit="${escapeHtml(id)}" ${canManage ? "" : "disabled"}>编辑</button>`}
            ${!isRemoved && canBindIdentity
              ? `<button class="ghost-btn" type="button" data-organization-member-identity="${escapeHtml(id)}">身份绑定</button>`
              : ""}
            ${statusActions}
            ${isRemoved ? "<span class=\"organization-member-actions-empty\">—</span>" : ""}
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
  if (customerOrganizationsLoadError) {
    grid.innerHTML = `<div class="customer-directory-empty">${escapeHtml(customerOrganizationsLoadError)}</div>`;
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
    const statusChip = organizationStatusChip(item);
    const stats = customerOrganizationStats(item);
    return `
      <article class="customer-organization-card ${isArchived ? "archived" : ""}">
        <div class="customer-organization-card-head">
          <div>
            <h3>${escapeHtml(name)}</h3>
            <p>${escapeHtml(id || "企业")}</p>
          </div>
          <span class="customer-organization-status ${isArchived ? "archived" : ""} ${statusChip.tone}">${escapeHtml(statusChip.label)}</span>
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
          ${isArchived
            ? `<button class="ghost-btn" type="button" data-customer-organization-restore="${escapeHtml(id)}">恢复</button>`
            : `<button class="danger-outline-btn" type="button" data-customer-organization-archive="${escapeHtml(id)}">归档</button>`}
        </div>
      </article>
    `;
  }).join("");
}

function renderPendingAdoptionOrganizations() {
  const panel = el("pendingAdoptionPanel");
  const grid = el("pendingAdoptionGrid");
  if (!panel || !grid) return;
  const items = Array.isArray(pendingAdoptionOrganizations) ? pendingAdoptionOrganizations : [];
  if (!items.length && !pendingAdoptionUnavailable) {
    panel.classList.add("hidden");
    grid.innerHTML = "";
    return;
  }
  panel.classList.remove("hidden");
  setText("pendingAdoptionCountChip", `${fmt.format(items.length)} 家`);
  if (pendingAdoptionUnavailable && !items.length) {
    grid.innerHTML = '<div class="customer-directory-empty">待接管企业暂时无法获取，请稍后重试。</div>';
    return;
  }
  grid.innerHTML = items.map((item) => {
    const name = String(item?.name || "未命名企业");
    const memberCount = Number(item?.memberCount || 0);
    const teamCount = Number(item?.teamCount || 0);
    const spend = Number(item?.spendUsd || 0);
    return `
      <article class="customer-organization-card pending">
        <div class="customer-organization-card-head">
          <div>
            <h3>${escapeHtml(name)}</h3>
            <p>尚未建立企业档案</p>
          </div>
          <span class="customer-organization-status pending">未接管</span>
        </div>
        <div class="customer-organization-metrics">
          <div><strong>${fmt.format(memberCount)}</strong><span>成员</span></div>
          <div><strong>${fmt.format(teamCount)}</strong><span>团队</span></div>
          <div><strong>${escapeHtml(money.format(spend))}</strong><span>累计消费</span></div>
        </div>
        <p class="customer-organization-card-note">接管前需先指定该企业的管理员账号。</p>
        <div class="customer-organization-card-actions">
          <button class="primary-btn" type="button" title="接管前需先指定该企业的管理员账号" disabled>接管</button>
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
  customerOrganizationsLoadError = "";
  renderCustomerOrganizations();
  try {
    const payload = await api(customerOrganizationsUrl());
    customerOrganizations = Array.isArray(payload?.items) ? payload.items : Array.isArray(payload?.organizations) ? payload.organizations : [];
    customerOrganizationsTotal = Number(payload?.total ?? customerOrganizations.length);
    customerOrganizationsPage = Number(payload?.page || customerOrganizationsPage || 1);
    // Only the unfiltered first page carries candidates. Keep the previous
    // result on filtered pages instead of blanking the panel.
    if (payload?.pendingAdoption) {
      pendingAdoptionOrganizations = Array.isArray(payload.pendingAdoption.items) ? payload.pendingAdoption.items : [];
      pendingAdoptionUnavailable = Boolean(payload.pendingAdoption.unavailable);
    }
  } catch (error) {
    customerOrganizations = [];
    customerOrganizationsTotal = 0;
    customerOrganizationsLoadError = error.message || "客户企业列表加载失败，请稍后重试。";
    showToast(customerOrganizationsLoadError);
  } finally {
    isCustomerOrganizationsLoading = false;
    renderCustomerOrganizationFilters();
    renderCustomerOrganizations();
    renderPendingAdoptionOrganizations();
  }
}

async function openCustomerOrganization(organizationId) {
  if (!customerOrganizationsAvailable() || !organizationId) return;
  const id = String(organizationId);
  // 先按退出客户范围清空一遍，再挂上新客户，避免上一家企业的快照、成员筛选或
  // 在途请求泄漏到这一家。
  clearCustomerOrganizationScope();
  selectedCustomerOrganization = customerOrganizations.find((item) => customerOrganizationId(item) === id) || { id };
  syncNavigationVisibility();
  switchView("organization");
  await loadOrganizationData();
  const today = localDate(new Date());
  const adoptionFrom = el("organizationAdoptionEffectiveFromInput");
  const adoptionThrough = el("organizationAdoptionEffectiveThroughInput");
  if (adoptionFrom && !adoptionFrom.value) adoptionFrom.value = "2020-01-01";
  if (adoptionThrough && !adoptionThrough.value) adoptionThrough.value = today;
  if (platformCanManageOrganizationClaims()) await loadOrganizationClaims();
}

// 退出客户下钻的纯状态清理，不切换视图。侧边栏（回到平台全局）和「返回客户企业」
// 共用它，避免任何一条路径漏掉某个客户范围的缓存。
function clearCustomerOrganizationScope() {
  selectedCustomerOrganization = null;
  resetOrganizationUsageViews();
  resetOrganizationBillingData();
  resetOrganizationTokenData();
  resetOrganizationClaims();
  resetOrganizationAdoption();
  organizationDataRequestId += 1;
  organizationMemberRequestId += 1;
  isOrganizationLoading = false;
  isOrganizationMemberLoading = false;
  organizationDataLoadingScopeKey = "";
  organizationMemberLoadingScopeKey = "";
  organizationLoadError = "";
  organizationMemberLoadError = "";
  organizationSnapshot = null;
  organizationMembers = [];
  organizationMemberTotal = 0;
  organizationMemberPage = 1;
  organizationMemberFilters = { search: "", departmentId: "", role: "", status: "" };
  customerOrganizationDetailTab = "info";
}

function closeCustomerOrganization() {
  clearCustomerOrganizationScope();
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
  const demoMode = isDemoOrganizationMode();
  setText("organizationTitle", name);
  renderOrganizationOperationalStatus("organizationOperationalStatus", organization);
  setText(
    "organizationSubtitle",
    isPlatformCustomer
      ? `${name} · 平台运营视图。可维护客户资料、部门和成员，并切换查看企业全员或部门用量。`
      : demoMode
        ? `${name} · 当前身份：${organizationRoleLabel(currentRole)}。这里的内容为演示数据，不会创建真实账号或发送邮件。`
        : `${name} · 当前身份：${organizationRoleLabel(currentRole)}。在这里维护部门、成员与企业范围访问权限。`,
  );
  setText("organizationDepartmentCount", fmt.format(stats.departmentCount));
  setText("organizationMemberCount", fmt.format(stats.memberCount));
  setText("organizationActiveMemberCount", fmt.format(stats.activeMemberCount));
  setText("organizationInvitedMemberCount", fmt.format(stats.invitedMemberCount));
  const createDepartmentButton = el("createOrganizationDepartmentButton");
  const inviteMemberButton = el("inviteOrganizationMemberButton");
  if (createDepartmentButton) createDepartmentButton.disabled = !canManage;
  if (inviteMemberButton) inviteMemberButton.disabled = !canManage;
  if (isPlatformCustomer && customerOrganizationStatus(selectedCustomerOrganization) === "archived") {
    setText("organizationSubtitle", `${name} · 已归档客户企业，仅可查看历史组织信息。`);
  }
  el("organizationManagementWorkspace")?.classList.toggle("hidden", !canManage);
  renderOrganizationClaims();
  renderOrganizationAdoption();
  renderOrganizationWorkspaceBar();
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
  if (!tabs || !scope) return;
  const scopeName = scope.name || "企业";
  const selected = customerOrganizationDetailTab || "info";
  tabs.querySelectorAll("[data-organization-usage-view]").forEach((button) => {
    const isActive = button.dataset.organizationUsageView === selected;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-selected", isActive ? "true" : "false");
  });
  const billingTab = tabs.querySelector('[data-organization-usage-view="billing"]');
  if (billingTab) billingTab.classList.toggle("hidden", !isViewingCustomerOrganization() && !canViewOrganizationBilling());
  // 甲方管理员的令牌管理已经是侧边栏的一级目的地，这里只为乙方下钻保留只读入口。
  const tokensTab = tabs.querySelector('[data-organization-usage-view="tokens"]');
  if (tokensTab) tokensTab.classList.toggle("hidden", !isViewingCustomerOrganization());
  setText("organizationUsageScopeLabel", scope.kind === "platformCustomer" ? `客户：${scopeName}` : `企业：${scopeName}`);
}

function showOrganizationUsage(view) {
  const scope = organizationUsageScope();
  if (!scope) return;
  const tab = ["info", "usage", "departments-usage", "billing", "tokens"].includes(view) ? view : "info";
  // 工作台条的高亮由 switchView → renderOrganizationWorkspaceBar 按落地视图反推，
  // 这里只负责把标签映射成视图。
  if (tab === "info") {
    switchView("organization");
  } else if (tab === "usage") {
    switchView("admin");
  } else if (tab === "billing") {
    switchView("billing");
  } else if (tab === "tokens") {
    switchView("organization-tokens");
  } else {
    switchView("department");
  }
}

async function loadOrganizationMembers() {
  const scopeKey = organizationUsageScopeKey();
  if (!organizationCanView() || isOrganizationMemberLoading) return;
  const requestId = ++organizationMemberRequestId;
  isOrganizationMemberLoading = true;
  organizationMemberLoadError = "";
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
    organizationMemberLoadError = error.message || "成员列表加载失败，请稍后重试。";
    showToast(organizationMemberLoadError);
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
  organizationLoadError = "";
  organizationMemberLoadError = "";
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
    renderOrganizationClaimDepartmentOptions();
  } catch (error) {
    if (requestId !== organizationDataRequestId || scopeKey !== organizationUsageScopeKey()) return;
    organizationSnapshot = null;
    organizationMembers = [];
    organizationMemberTotal = 0;
    organizationLoadError = error.message || "企业组织加载失败，请稍后重试。";
    organizationMemberLoadError = organizationLoadError;
    showToast(organizationLoadError);
  } finally {
    if (requestId !== organizationDataRequestId) return;
    isOrganizationLoading = false;
    organizationDataLoadingScopeKey = "";
    renderOrganization();
  }
}

// ---------------------------------------------------------------------------
// Customer enterprise access tokens
// ---------------------------------------------------------------------------

const ORGANIZATION_TOKEN_STATUS_LABELS = {
  active: "生效中",
  expired: "已过期",
  revoked: "已撤销",
};

function organizationTokenStatusLabel(status) {
  return ORGANIZATION_TOKEN_STATUS_LABELS[String(status || "")] || "未知";
}

function organizationTokenStatusTone(status) {
  if (status === "active") return "active";
  if (status === "revoked") return "suspended";
  return "invited";
}

function canViewOrganizationTokens() {
  // A seller operator drilled into a customer may read the list; a customer's
  // own administrator is the only identity that may also create or revoke.
  return Boolean(
    isViewingCustomerOrganization()
    || currentUser?.canManageOrganizationTokens
    || currentUser?.canManageOrganization,
  );
}

function organizationTokenIsReportOnly(token) {
  const source = String(organizationField(token, "source", "source") || "").trim().toLowerCase();
  const managementMode = String(
    organizationField(token, "managementMode", "management_mode")
      || organizationField(token, "accessMode", "access_mode")
      || "",
  ).trim().toLowerCase();
  const billingMode = String(organizationField(token, "billingMode", "billing_mode") || "").trim().toLowerCase();
  return Boolean(
    organizationField(token, "reportOnly", "report_only")
    || billingMode === "report_only"
    || (source === "imported" && managementMode === "read_only"),
  );
}

function organizationTokenReadOnly() {
  return isViewingCustomerOrganization();
}

function organizationTokenCanManage() {
  if (currentUser?.canManageOrganizationTokens !== undefined) {
    return Boolean(!organizationTokenReadOnly() && currentUser.canManageOrganizationTokens);
  }
  return Boolean(!organizationTokenReadOnly() && organizationCanManage());
}

function organizationTokenOrganizationRecord() {
  const snapshotOrganization = organizationSnapshot?.organization;
  if (snapshotOrganization && typeof snapshotOrganization === "object") return snapshotOrganization;
  if (isViewingCustomerOrganization()) return customerOrganizationRecord(selectedCustomerOrganization);
  const currentOrganization = currentUser?.organization;
  return currentOrganization && typeof currentOrganization === "object" ? currentOrganization : {};
}

function organizationTokenCreationBlockedByOrganization() {
  const organization = organizationTokenOrganizationRecord();
  const provisioningStatus = organizationProvisioningStatus(organization);
  const billingStatus = String(
    organizationField(organization, "billingStatus", "billing_status") || "",
  ).trim().toLowerCase();
  if (["failed", "error", "degraded"].includes(provisioningStatus)) {
    return "企业账号同步失败，暂时不能创建企业 Token";
  }
  if (["pending", "provisioning", "creating"].includes(provisioningStatus)) {
    return "企业账号仍在开通中，暂时不能创建企业 Token";
  }
  if (["past_due", "insufficient", "blocked", "suspended"].includes(billingStatus)) {
    return "企业余额不足或额度尚未生效，暂时不能创建企业 Token";
  }
  return "";
}

function organizationTokensUrl() {
  const params = new URLSearchParams({
    page: String(organizationTokenPage),
    pageSize: String(organizationTokenPageSize),
  });
  if (organizationTokenFilters.search) params.set("search", organizationTokenFilters.search);
  if (organizationTokenFilters.status) params.set("status", organizationTokenFilters.status);
  return `${organizationApiPath("/tokens")}?${params.toString()}`;
}

function resetOrganizationTokenData() {
  organizationTokenRequestId += 1;
  organizationTokens = [];
  organizationTokenTotal = 0;
  organizationTokenStats = null;
  organizationTokenPage = 1;
  organizationTokenFilters = { search: "", status: "" };
  organizationTokenModels = [];
  organizationTokenBindableMembers = [];
  isOrganizationTokenLoading = false;
  isOrganizationTokenSaving = false;
  isOrganizationTokenRevoking = false;
  isOrganizationTokenDeleting = false;
  organizationTokenLoadError = "";
  organizationTokenLoadErrorCode = "";
  organizationTokenScopeKey = "";
  revokingOrganizationTokenId = "";
  deletingOrganizationTokenId = "";
  window.clearTimeout(organizationTokenSearchTimer);
  organizationTokenSearchTimer = null;
  const search = el("organizationTokenSearch");
  if (search) search.value = "";
  const statusFilter = el("organizationTokenStatusFilter");
  if (statusFilter) statusFilter.value = "";
  el("organizationTokenModal")?.classList.add("hidden");
  el("organizationTokenSecretModal")?.classList.add("hidden");
  el("organizationTokenRevokeModal")?.classList.add("hidden");
  el("organizationTokenDeleteModal")?.classList.add("hidden");
}

function renderOrganizationTokenFilters() {
  const search = el("organizationTokenSearch");
  if (search && search.value !== organizationTokenFilters.search) search.value = organizationTokenFilters.search;
  const statusFilter = el("organizationTokenStatusFilter");
  if (statusFilter && statusFilter.value !== organizationTokenFilters.status) {
    statusFilter.value = organizationTokenFilters.status;
  }
}

function renderOrganizationTokens() {
  if (!canViewOrganizationTokens()) return;
  const readOnly = organizationTokenReadOnly();
  const canManage = organizationTokenCanManage();
  const stats = organizationTokenStats || {};
  const scopeName = organizationUsageScope()?.name || "本企业";
  setText("organizationTokenTitle", readOnly ? `${scopeName} · 令牌管理` : "令牌管理");
  setText(
    "organizationTokenSubtitle",
    readOnly
      ? `${scopeName} 的令牌列表仅供运营协助查看。历史资产会明确标注为只读且不计企业额度。`
      : "为本企业签发调用令牌，并指定每个令牌可以使用的模型。令牌完整值仅在创建成功时展示一次。",
  );
  const createButton = el("createOrganizationTokenButton");
  const catalogUnavailable = organizationTokenLoadErrorCode === "ORGANIZATION_MODEL_CATALOG_UNAVAILABLE";
  const organizationBlockedReason = organizationTokenCreationBlockedByOrganization();
  if (createButton) {
    createButton.classList.toggle("hidden", readOnly);
    createButton.disabled = Boolean(
      !canManage
      || isOrganizationTokenLoading
      || catalogUnavailable
      || organizationBlockedReason
      || !organizationTokenModels.length,
    );
    createButton.title = catalogUnavailable
      ? "模型目录暂不可用，当前不能创建企业 Token"
      : organizationBlockedReason;
  }
  el("organizationTokenCatalogStatus")?.classList.toggle("hidden", !catalogUnavailable);
  const retryCatalogButton = el("retryOrganizationTokenCatalogButton");
  if (retryCatalogButton) retryCatalogButton.disabled = isOrganizationTokenLoading;
  el("organizationTokenReadOnlyHint")?.classList.toggle("hidden", !readOnly);
  setText("organizationTokenTotalCount", fmt.format(Number(stats.total || organizationTokenTotal || 0)));
  setText("organizationTokenMaxCount", fmt.format(Number(stats.maxTokenCount || 20)));
  setText("organizationTokenActiveCount", fmt.format(Number(stats.activeCount || 0)));
  setText("organizationTokenRevokedCount", fmt.format(Number(stats.revokedCount || 0)));
  setText("organizationTokenBoundMemberCount", fmt.format(Number(stats.boundMemberCount || 0)));
  setText("organizationTokenCountChip", `${fmt.format(organizationTokenTotal)} 个`);
  renderOrganizationTokenFilters();

  const table = el("organizationTokenTable");
  const reportOnlyHint = el("organizationTokenReportOnlyHint");
  if (reportOnlyHint) reportOnlyHint.remove();
  const totalPages = Math.max(1, Math.ceil(organizationTokenTotal / organizationTokenPageSize));
  const page = Math.min(organizationTokenPage, totalPages);
  if (organizationTokenPage !== page) organizationTokenPage = page;
  setText("organizationTokenPageInfo", `第 ${page} / ${totalPages} 页`);
  const previousButton = el("organizationTokenPreviousPageButton");
  const nextButton = el("organizationTokenNextPageButton");
  if (previousButton) previousButton.disabled = page <= 1 || isOrganizationTokenLoading;
  if (nextButton) nextButton.disabled = page >= totalPages || isOrganizationTokenLoading;
  if (!table) return;
  if (organizationTokenLoadError) {
    table.innerHTML = `<tr><td colspan="8"><div class="organization-empty">${escapeHtml(organizationTokenLoadError)}</div></td></tr>`;
    return;
  }
  if (isOrganizationTokenLoading && !organizationTokens.length) {
    table.innerHTML = '<tr><td colspan="8"><div class="organization-empty">正在加载令牌…</div></td></tr>';
    return;
  }
  if (!organizationTokens.length) {
    table.innerHTML = `<tr><td colspan="8"><div class="organization-empty">${
      readOnly ? "该企业还没有令牌。" : "还没有令牌。点击「新增令牌」为本企业签发第一个令牌。"
    }</div></td></tr>`;
    return;
  }
  const reportOnlyCount = organizationTokens.filter(organizationTokenIsReportOnly).length;
  if (reportOnlyCount) {
    const hint = document.createElement("div");
    hint.id = "organizationTokenReportOnlyHint";
    hint.className = "organization-billing-readonly";
    hint.textContent = `${fmt.format(reportOnlyCount)} 项历史资产：只读、不计企业额度、不可撤销。`;
    table.closest(".panel")?.insertBefore(hint, table.closest(".table-wrap") || table);
  }
  table.innerHTML = organizationTokens.map((token) => {
    const id = String(organizationField(token, "id", "token_id") || "");
    const name = organizationField(token, "name", "name") || "未命名令牌";
    const masked = organizationField(token, "masked", "masked") || "sk-...----";
    const models = Array.isArray(token.models) ? token.models : [];
    const memberName = organizationField(token, "memberName", "member_name") || "";
    const memberEmail = organizationField(token, "memberEmail", "member_email") || "";
    const memberLoginName = organizationField(token, "memberLoginName", "member_login_name") || "";
    const departmentName = organizationField(token, "departmentName", "department_name") || "";
    const status = String(organizationField(token, "status", "status") || "active");
    const budget = Number(organizationField(token, "dailyBudgetUsd", "daily_budget_usd") || 0);
    const createdAt = organizationField(token, "createdAt", "created_at");
    const expiresAt = organizationField(token, "expiresAt", "expires_at");
    const reportOnly = organizationTokenIsReportOnly(token);
    // 展示名由后端按每条令牌单独算并合并同一模型的多条线路：已签发令牌可能引用了
    // 当前目录里已经没有的模型，它仍然是历史事实。旧 bundle 回落到原始名。
    const modelLabels = Array.isArray(token.modelLabels) && token.modelLabels.length
      ? token.modelLabels
      : models;
    const modelChips = modelLabels.length
      ? modelLabels.map((label) => `<span class="chip">${escapeHtml(label)}</span>`).join("")
      : '<span class="chip">未指定</span>';
    const owner = memberName
      ? `<div class="organization-member-identity"><strong>${escapeHtml(memberName)}</strong><span>${escapeHtml(
          departmentName ? `${departmentName} · ${memberEmail || memberLoginName}` : (memberEmail || memberLoginName),
        )}</span></div>`
      : '<span class="chip blue">企业共享</span>';
    const canRevoke = canManage && status === "active" && !reportOnly;
    // 已撤销的令牌上游 key 在撤销时就删掉了，剩下的只是列表里的死记录，所以这里换成
    // 删除入口：它只把记录从列表隐藏，历史用量归属仍然保留。历史资产不给删除入口。
    const canDelete = canManage && status === "revoked" && !reportOnly;
    const statusLabel = reportOnly ? "历史资产、只读" : organizationTokenStatusLabel(status);
    const statusTone = reportOnly ? "invited" : organizationTokenStatusTone(status);
    const actionButton = canDelete
      ? `<button class="danger-outline-btn" type="button" data-organization-token-delete="${escapeHtml(id)}">删除</button>`
      : `<button class="danger-outline-btn" type="button" data-organization-token-revoke="${escapeHtml(id)}" ${canRevoke ? "" : "disabled"}>${reportOnly ? "不可撤销" : "撤销"}</button>`;
    return `
      <tr>
        <td>
          <div class="organization-member-identity"><strong>${escapeHtml(name)}</strong><code>${escapeHtml(masked)}</code></div>
        </td>
        <td><div class="organization-token-models">${modelChips}</div></td>
        <td>${owner}</td>
        <td class="num">${reportOnly ? "不计企业额度" : escapeHtml(money.format(budget))}</td>
        <td><span class="organization-status ${escapeHtml(statusTone)}">${escapeHtml(statusLabel)}</span></td>
        <td>${escapeHtml(organizationDate(createdAt))}</td>
        <td>${escapeHtml(expiresAt ? organizationDate(expiresAt) : "永不过期")}</td>
        <td>
          <div class="organization-member-actions">
            ${actionButton}
          </div>
        </td>
      </tr>
    `;
  }).join("");
}

async function loadOrganizationTokens() {
  const scopeKey = organizationUsageScopeKey();
  if (!canViewOrganizationTokens() || isOrganizationTokenLoading) return;
  const requestId = ++organizationTokenRequestId;
  isOrganizationTokenLoading = true;
  organizationTokenLoadError = "";
  organizationTokenLoadErrorCode = "";
  organizationTokenScopeKey = scopeKey;
  renderOrganizationTokens();
  try {
    const payload = await api(organizationTokensUrl());
    // Discard a response for a customer the operator has since left.
    if (requestId !== organizationTokenRequestId || scopeKey !== organizationUsageScopeKey()) return;
    organizationTokens = Array.isArray(payload?.items) ? payload.items : [];
    organizationTokenTotal = Number(payload?.total || 0);
    organizationTokenPage = Number(payload?.page || organizationTokenPage || 1);
    organizationTokenStats = payload?.stats && typeof payload.stats === "object" ? payload.stats : null;
    organizationTokenModels = normalizeOrganizationTokenModels(payload);
    organizationTokenBindableMembers = Array.isArray(payload?.bindableMembers) ? payload.bindableMembers : [];
    organizationTokenLoadErrorCode = "";
  } catch (error) {
    if (requestId !== organizationTokenRequestId || scopeKey !== organizationUsageScopeKey()) return;
    organizationTokens = [];
    organizationTokenTotal = 0;
    organizationTokenStats = null;
    organizationTokenLoadError = error.message || "令牌列表加载失败，请稍后重试。";
    organizationTokenLoadErrorCode = String(error.code || "");
    showToast(organizationTokenLoadError);
  } finally {
    if (requestId !== organizationTokenRequestId) return;
    isOrganizationTokenLoading = false;
    renderOrganizationTokens();
  }
}

// 可选模型来自网关真实目录。一个选项 = 一个展示名，背后可能是同一模型的多条线路，
// 勾选即授权它名下全部原始模型名（原始名才是调用时可用的值，但按产品边界不可见）。
// 旧字段 availableModels 仍然接受，浏览器缓存着上一版 app.js 的用户不会看到空目录。
function normalizeOrganizationTokenModels(payload) {
  const options = Array.isArray(payload?.availableModelOptions) ? payload.availableModelOptions : null;
  if (options) {
    return options
      .map((option) => ({
        displayName: String(option?.displayName || ""),
        names: (Array.isArray(option?.names) ? option.names : []).map((name) => String(name)).filter(Boolean),
      }))
      .filter((option) => option.displayName && option.names.length);
  }
  const names = Array.isArray(payload?.availableModels) ? payload.availableModels : [];
  return names
    .map((name) => ({ displayName: String(name), names: [String(name)] }))
    .filter((option) => option.displayName);
}

function renderOrganizationTokenModelChoices() {
  const choices = el("organizationTokenModelChoices");
  if (!choices) return;
  if (!organizationTokenModels.length) {
    choices.innerHTML = '<div class="key-model-empty">当前没有可选的模型，请稍后重试。</div>';
    return;
  }
  choices.innerHTML = organizationTokenModels.map((model, index) => `
    <label class="model-choice">
      <input type="checkbox" name="organizationTokenModel" value="${index}" />
      <span>${escapeHtml(model.displayName)}</span>
    </label>
  `).join("");
}

function renderOrganizationTokenMemberOptions() {
  const select = el("organizationTokenMemberInput");
  if (!select) return;
  const options = organizationTokenBindableMembers.map((member) => {
    const id = String(organizationField(member, "id", "member_id") || "");
    const name = organizationField(member, "name", "name") || "未命名成员";
    const departmentName = organizationField(member, "departmentName", "department_name") || "";
    const label = departmentName ? `${name}（${departmentName}）` : name;
    return `<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`;
  }).join("");
  select.innerHTML = `<option value="">企业共享（不绑定成员）</option>${options}`;
}

// 勾选框的 value 是目录下标，展开成该选项覆盖的全部上游原始名后再提交。
function selectedOrganizationTokenModels() {
  const selected = [];
  document
    .querySelectorAll('#organizationTokenModelChoices input[name="organizationTokenModel"]:checked')
    .forEach((input) => {
      const option = organizationTokenModels[Number(input.value)];
      (option?.names || []).forEach((name) => {
        if (!selected.includes(name)) selected.push(name);
      });
    });
  return selected;
}

function closeOrganizationTokenModal(options = {}) {
  if (isOrganizationTokenSaving && !options.force) return;
  el("organizationTokenForm")?.reset();
  setFieldError("organizationTokenError", "");
  el("organizationTokenModal")?.classList.add("hidden");
}

function openOrganizationTokenModal() {
  if (!organizationTokenCanManage()) return;
  el("organizationTokenForm")?.reset();
  setFieldError("organizationTokenError", "");
  renderOrganizationTokenModelChoices();
  renderOrganizationTokenMemberOptions();
  const budgetInput = el("organizationTokenBudgetInput");
  if (budgetInput) budgetInput.value = "100";
  el("organizationTokenModal")?.classList.remove("hidden");
  window.setTimeout(() => el("organizationTokenNameInput")?.focus(), 0);
}

function closeOrganizationTokenSecretModal() {
  // The plaintext value exists only in this dialog, so drop it on close.
  setText("organizationTokenSecretValue", "");
  setText("organizationTokenSecretMeta", "");
  el("organizationTokenSecretModal")?.classList.add("hidden");
}

function showOrganizationTokenSecret(secret, token) {
  const value = String(secret || "");
  if (!value) return false;
  setText("organizationTokenSecretValue", value);
  const name = organizationField(token, "name", "name") || "新令牌";
  const models = Array.isArray(token?.models) ? token.models : [];
  const expiresAt = organizationField(token, "expiresAt", "expires_at");
  setText(
    "organizationTokenSecretMeta",
    `${name} · ${models.length ? models.join("、") : "未指定模型"} · ${
      expiresAt ? `${organizationDate(expiresAt)} 过期` : "永不过期"
    }`,
  );
  el("organizationTokenSecretModal")?.classList.remove("hidden");
  return true;
}

function organizationTokenSecretFromPayload(payload) {
  const token = payload?.token && typeof payload.token === "object" ? payload.token : null;
  return String(payload?.secret || token?.token || token?.secret || "");
}

async function submitOrganizationToken(event) {
  event.preventDefault();
  if (!organizationTokenCanManage() || isOrganizationTokenSaving) return;
  const name = String(el("organizationTokenNameInput")?.value || "").trim();
  const models = selectedOrganizationTokenModels();
  const memberId = String(el("organizationTokenMemberInput")?.value || "");
  const duration = String(el("organizationTokenDurationInput")?.value || "never");
  const budget = Number(el("organizationTokenBudgetInput")?.value || 0);
  setFieldError("organizationTokenError", "");
  if (!name) {
    setFieldError("organizationTokenError", "请填写令牌名称。");
    return;
  }
  if (!models.length) {
    setFieldError("organizationTokenError", "请至少选择一个可用模型。");
    return;
  }
  if (!Number.isFinite(budget) || budget < 1 || budget > 5000 || Math.round(budget * 100) !== budget * 100) {
    setFieldError("organizationTokenError", "请输入 $1.00 至 $5,000.00 的每日额度，最多两位小数。");
    return;
  }
  isOrganizationTokenSaving = true;
  setButtonLoading("submitOrganizationTokenButton", true, "创建中");
  try {
    await ensureCsrfToken();
    const payload = await api("/api/organization/current/tokens", {
      method: "POST",
      body: JSON.stringify({ name, models, memberId, duration, dailyBudgetUsd: budget }),
    });
    closeOrganizationTokenModal({ force: true });
    const secret = organizationTokenSecretFromPayload(payload);
    if (showOrganizationTokenSecret(secret, payload?.token)) {
      showToast("令牌已创建，请立即保存完整令牌。");
    } else {
      // Idempotent retries return the durable projection without replaying the
      // one-time secret; never open an empty secret dialog in that case.
      showToast("令牌已创建，但完整令牌仅在首次成功响应中显示。请确认请求结果后再重试。", "warning");
    }
    organizationTokenPage = 1;
    await loadOrganizationTokens();
  } catch (error) {
    setFieldError("organizationTokenError", error.message || "令牌创建失败，请稍后重试。");
  } finally {
    isOrganizationTokenSaving = false;
    setButtonLoading("submitOrganizationTokenButton", false);
  }
}

function closeOrganizationTokenRevokeModal(options = {}) {
  if (isOrganizationTokenRevoking && !options.force) return;
  revokingOrganizationTokenId = "";
  el("organizationTokenRevokeModal")?.classList.add("hidden");
}

function openOrganizationTokenRevokeModal(tokenId) {
  if (!organizationTokenCanManage() || !tokenId) return;
  const token = organizationTokens.find(
    (item) => String(organizationField(item, "id", "token_id") || "") === String(tokenId),
  );
  if (!token) return;
  revokingOrganizationTokenId = String(tokenId);
  setText("organizationTokenRevokeName", organizationField(token, "name", "name") || "未命名令牌");
  setText("organizationTokenRevokeMasked", organizationField(token, "masked", "masked") || "sk-...----");
  el("organizationTokenRevokeModal")?.classList.remove("hidden");
}

async function confirmOrganizationTokenRevoke() {
  if (!organizationTokenCanManage() || !revokingOrganizationTokenId || isOrganizationTokenRevoking) return;
  const tokenId = revokingOrganizationTokenId;
  isOrganizationTokenRevoking = true;
  setButtonLoading("confirmOrganizationTokenRevokeButton", true, "撤销中");
  try {
    await ensureCsrfToken();
    await api(`/api/organization/current/tokens/${encodeURIComponent(tokenId)}/revoke`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    isOrganizationTokenRevoking = false;
    closeOrganizationTokenRevokeModal({ force: true });
    await loadOrganizationTokens();
    showToast("令牌已撤销，立即失效。");
  } catch (error) {
    showToast(error.message || "令牌撤销失败，请稍后重试。");
  } finally {
    isOrganizationTokenRevoking = false;
    setButtonLoading("confirmOrganizationTokenRevokeButton", false);
  }
}

function closeOrganizationTokenDeleteModal(options = {}) {
  if (isOrganizationTokenDeleting && !options.force) return;
  deletingOrganizationTokenId = "";
  el("organizationTokenDeleteModal")?.classList.add("hidden");
}

function openOrganizationTokenDeleteModal(tokenId) {
  if (!organizationTokenCanManage() || !tokenId) return;
  const token = organizationTokens.find(
    (item) => String(organizationField(item, "id", "token_id") || "") === String(tokenId),
  );
  if (!token) return;
  deletingOrganizationTokenId = String(tokenId);
  setText("organizationTokenDeleteName", organizationField(token, "name", "name") || "未命名令牌");
  setText("organizationTokenDeleteMasked", organizationField(token, "masked", "masked") || "sk-...----");
  el("organizationTokenDeleteModal")?.classList.remove("hidden");
}

async function confirmOrganizationTokenDelete() {
  if (!organizationTokenCanManage() || !deletingOrganizationTokenId || isOrganizationTokenDeleting) return;
  const tokenId = deletingOrganizationTokenId;
  isOrganizationTokenDeleting = true;
  setButtonLoading("confirmOrganizationTokenDeleteButton", true, "删除中");
  try {
    await ensureCsrfToken();
    await api(`/api/organization/current/tokens/${encodeURIComponent(tokenId)}/delete`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    isOrganizationTokenDeleting = false;
    closeOrganizationTokenDeleteModal({ force: true });
    await loadOrganizationTokens();
    showToast("令牌已从列表移除。");
  } catch (error) {
    showToast(error.message || "令牌删除失败，请稍后重试。");
  } finally {
    isOrganizationTokenDeleting = false;
    setButtonLoading("confirmOrganizationTokenDeleteButton", false);
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
  el("customerOrganizationAdminNameField").classList.toggle("hidden", isEditing);
  el("customerOrganizationAdminEmailField").classList.toggle("hidden", isEditing);
  // Editing an existing tenant only changes its name. Admin provisioning is
  // required for creation and is intentionally absent from the PATCH body.
  el("customerOrganizationAdminNameInput").required = !isEditing;
  el("customerOrganizationAdminEmailInput").required = !isEditing;
  setText("customerOrganizationModalTitle", isEditing ? "修改客户企业名称" : "新增客户企业");
  setText(
    "customerOrganizationModalDescription",
    isEditing ? "修改后会立即显示在企业目录和详情页。" : "创建后可进入企业详情，继续维护部门和成员。",
  );
  setText("submitCustomerOrganizationButton", isEditing ? "保存修改" : "创建企业");
  el("customerOrganizationNameInput").value = organization.name || "";
  el("customerOrganizationAdminNameInput").value = "";
  el("customerOrganizationAdminEmailInput").value = "";
  el("customerOrganizationModal").classList.remove("hidden");
  window.setTimeout(() => el("customerOrganizationNameInput").focus(), 0);
}

async function archiveCustomerOrganization(organizationId) {
  if (!customerOrganizationsAvailable() || !organizationId) return;
  const item = customerOrganizations.find((candidate) => customerOrganizationId(candidate) === String(organizationId));
  const organization = customerOrganizationRecord(item);
  const name = organization.name || "这家客户企业";
  if (!window.confirm(`归档“${name}”？已归档企业将无法继续管理或访问企业资源。`)) return;
  try {
    await ensureCsrfToken();
    await api(`${customerOrganizationPath(organizationId)}/archive`, { method: "POST", body: JSON.stringify({}) });
    if (selectedCustomerOrganizationId() === String(organizationId)) {
      selectedCustomerOrganization = null;
      resetOrganizationUsageViews();
      resetOrganizationClaims();
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

async function restoreCustomerOrganization(organizationId) {
  if (!customerOrganizationsAvailable() || !organizationId) return;
  const item = customerOrganizations.find((candidate) => customerOrganizationId(candidate) === String(organizationId));
  const organization = customerOrganizationRecord(item);
  const name = organization.name || "这家客户企业";
  // 归档时该企业的令牌已经真实失效且不可恢复，必须在确认前讲清楚，
  // 否则运营会以为恢复企业就等于恢复调用能力。
  if (!window.confirm(`恢复“${name}”？部门和成员会一并恢复，但归档时已失效的令牌不会恢复，需要由该企业管理员重新签发。`)) return;
  try {
    await ensureCsrfToken();
    await api(`${customerOrganizationPath(organizationId)}/restore`, { method: "POST", body: JSON.stringify({}) });
    await loadCustomerOrganizations();
    showToast("客户企业已恢复");
  } catch (error) {
    showToast(error.message || "客户企业恢复失败");
  }
}

async function resetCustomerOrganizationsDemo() {
  if (!customerOrganizationsAvailable()) return;
  if (!isDemoOrganizationMode()) return;
  if (!window.confirm("重置后会清除本次演示中的所有客户企业、部门与成员变更，并恢复初始样例。确定继续吗？")) return;
  setButtonLoading("resetCustomerOrganizationsDemoButton", true, "重置中");
  try {
    await ensureCsrfToken();
    await api("/api/platform/organizations/demo/reset", { method: "POST", body: JSON.stringify({}) });
    selectedCustomerOrganization = null;
    resetOrganizationUsageViews();
    resetOrganizationClaims();
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
  setText(
    "organizationMemberModalDescription",
    isEditing
      ? "更新角色、部门或访问状态后会立即生效。"
      : isDemoOrganizationMode()
        ? "成员会以待邀请状态加入演示企业，不会发送真实邮件。"
        : "成员会以待邀请状态加入企业；系统会按企业配置发送邀请。",
  );
  setText("submitOrganizationMemberButton", isEditing ? "保存修改" : "发送邀请");
  const statusEditable = isEditing && !isRealOrganizationMode();
  el("organizationMemberStatusField").classList.toggle("hidden", !statusEditable);
  el("organizationMemberNameInput").value = member?.name || "";
  el("organizationMemberEmailInput").value = member?.email || "";
  el("organizationMemberEmailInput").disabled = isEditing;
  el("organizationMemberRoleInput").value = member?.role || "member";
  el("organizationMemberTeamRoleInput").value =
    String(organizationField(member || {}, "teamRole", "team_role") || "member") === "leader" ? "leader" : "member";
  el("organizationMemberStatusInput").value = member?.status || "invited";
  el("organizationMemberModal").classList.remove("hidden");
  window.setTimeout(() => el("organizationMemberNameInput").focus(), 0);
}

// 身份绑定只给平台运营：让客户管理员随意换绑登录账号等于交出账号接管能力。
// 演示目录没有登录账号与用量身份的概念，所以那里连入口都不出现。
function organizationCanBindMemberIdentity() {
  return isViewingCustomerOrganization() && isRealOrganizationMode();
}

function closeOrganizationMemberIdentityModal(options = {}) {
  if (isOrganizationMemberIdentityBusy && !options.force) return;
  organizationMemberIdentityId = "";
  organizationMemberIdentity = null;
  el("organizationMemberAccountInput").value = "";
  setText("organizationMemberIdentityError", "");
  el("organizationMemberIdentityModal").classList.add("hidden");
}

async function openOrganizationMemberIdentityModal(memberId) {
  if (!organizationCanBindMemberIdentity() || !memberId) return;
  organizationMemberIdentityId = String(memberId);
  organizationMemberIdentity = null;
  setText("organizationMemberIdentityError", "");
  el("organizationMemberAccountInput").value = "";
  el("organizationMemberLoginNameInput").value = "";
  el("organizationMemberIdentityList").innerHTML = '<p class="organization-modal-note">正在加载用量身份…</p>';
  el("organizationMemberIdentityModal").classList.remove("hidden");
  await loadOrganizationMemberIdentity();
}

async function loadOrganizationMemberIdentity() {
  const memberId = organizationMemberIdentityId;
  if (!memberId) return;
  try {
    const payload = await api(
      organizationApiPath(`/members/${encodeURIComponent(memberId)}/identity`),
    );
    if (organizationMemberIdentityId !== memberId) return;
    organizationMemberIdentity = payload;
    renderOrganizationMemberIdentity();
  } catch (error) {
    if (organizationMemberIdentityId !== memberId) return;
    organizationMemberIdentity = null;
    el("organizationMemberIdentityList").innerHTML = "";
    setText("organizationMemberIdentityError", error.message || "身份信息加载失败");
  }
}

function renderOrganizationMemberIdentity() {
  const payload = organizationMemberIdentity;
  if (!payload) return;
  const member = payload.member && typeof payload.member === "object" ? payload.member : {};
  const memberId = String(organizationField(member, "id", "member_id") || organizationMemberIdentityId);
  const memberName = String(member.name || "该成员");
  setText("organizationMemberIdentityTitle", `身份绑定 · ${memberName}`);
  el("organizationMemberLoginNameInput").value = String(
    organizationField(member, "loginName", "login_name") || "",
  );
  const account = payload.account && typeof payload.account === "object" ? payload.account : null;
  const accountLabel = account
    ? [account.email, account.loginName].filter(Boolean).join(" / ") || String(account.id || "")
    : payload.accountMissing
      ? "原绑定账号已不存在，请重新绑定"
      : "未绑定";
  setText("organizationMemberIdentityAccount", `当前登录账号：${accountLabel}`);
  const items = Array.isArray(payload.principals?.items) ? payload.principals.items : [];
  const list = el("organizationMemberIdentityList");
  if (!items.length) {
    list.innerHTML = '<p class="organization-modal-note">这家企业还没有用量身份。企业接入历史用量后会自动生成。</p>';
    return;
  }
  list.innerHTML = items.map((item) => {
    const principalId = String(organizationField(item, "id", "principal_id") || "");
    const boundMemberId = String(organizationField(item, "memberId", "member_id") || "");
    const boundMemberName = String(organizationField(item, "memberName", "member_name") || "");
    const isMine = boundMemberId && boundMemberId === memberId;
    const sources = Array.isArray(item.historySources) ? item.historySources : [];
    const binding = isMine
      ? "已关联到当前成员"
      : boundMemberId
        ? `已关联到 ${boundMemberName || boundMemberId}`
        : "未关联";
    const action = isMine
      ? `<button class="danger-outline-btn" type="button" data-organization-principal-unbind="${escapeHtml(principalId)}">解除</button>`
      : `<button class="ghost-btn" type="button" data-organization-principal-bind="${escapeHtml(principalId)}">关联</button>`;
    return `
      <div class="organization-identity-item">
        <div>
          <strong>${escapeHtml(String(item.name || "未命名身份"))}</strong>
          <span>${escapeHtml(binding)}</span>
          ${sources.length ? `<span>历史来源：${escapeHtml(sources.join("、"))}</span>` : ""}
        </div>
        ${action}
      </div>
    `;
  }).join("");
}

async function saveOrganizationMemberLoginName() {
  if (!organizationCanBindMemberIdentity() || isOrganizationMemberIdentityBusy) return;
  const memberId = organizationMemberIdentityId;
  const loginName = el("organizationMemberLoginNameInput").value.trim();
  if (!memberId || !loginName) {
    setText("organizationMemberIdentityError", "请填写登录名");
    return;
  }
  isOrganizationMemberIdentityBusy = true;
  setButtonLoading("saveOrganizationMemberLoginNameButton", true, "保存中");
  setText("organizationMemberIdentityError", "");
  try {
    await ensureCsrfToken();
    await api(organizationApiPath(`/members/${encodeURIComponent(memberId)}`), {
      method: "PATCH",
      body: JSON.stringify({ loginName }),
    });
    await loadOrganizationMembers();
    await loadOrganizationMemberIdentity();
    showToast("登录名已保存");
  } catch (error) {
    setText("organizationMemberIdentityError", error.message || "登录名保存失败");
  } finally {
    isOrganizationMemberIdentityBusy = false;
    setButtonLoading("saveOrganizationMemberLoginNameButton", false);
  }
}

async function bindOrganizationMemberAccount(identifier) {
  if (!organizationCanBindMemberIdentity() || isOrganizationMemberIdentityBusy) return;
  const memberId = organizationMemberIdentityId;
  if (!memberId) return;
  const value = String(identifier || "").trim();
  const hasAccount = Boolean(organizationMemberIdentity?.account);
  if (value && !window.confirm(
    hasAccount
      ? `把该成员的登录账号换成“${value}”？原账号已登录的会话会立即失效。`
      : `把“${value}”绑定为该成员的登录账号？绑定后对方登录即可看到自己的用量。`,
  )) return;
  if (!value && !window.confirm("解除该成员的登录账号绑定？该账号已登录的会话会立即失效。")) return;
  const buttonId = value ? "bindOrganizationMemberAccountButton" : "unbindOrganizationMemberAccountButton";
  isOrganizationMemberIdentityBusy = true;
  setButtonLoading(buttonId, true, value ? "绑定中" : "解绑中");
  setText("organizationMemberIdentityError", "");
  try {
    await ensureCsrfToken();
    const payload = await api(
      organizationApiPath(`/members/${encodeURIComponent(memberId)}/account`),
      { method: "POST", body: JSON.stringify({ identifier: value }) },
    );
    if (organizationMemberIdentityId === memberId) {
      organizationMemberIdentity = payload;
      el("organizationMemberAccountInput").value = "";
      renderOrganizationMemberIdentity();
    }
    await loadOrganizationMembers();
    showToast(value ? "登录账号已绑定" : "登录账号已解绑");
  } catch (error) {
    setText("organizationMemberIdentityError", error.message || "登录账号绑定失败");
  } finally {
    isOrganizationMemberIdentityBusy = false;
    setButtonLoading(buttonId, false);
  }
}

async function bindOrganizationPrincipalMember(principalId, memberId) {
  if (!organizationCanBindMemberIdentity() || isOrganizationMemberIdentityBusy) return;
  if (!principalId) return;
  const target = String(memberId || "");
  const principal = (organizationMemberIdentity?.principals?.items || []).find(
    (item) => String(organizationField(item, "id", "principal_id") || "") === String(principalId),
  );
  const principalName = String(principal?.name || "该用量身份");
  const boundMemberName = String(organizationField(principal || {}, "memberName", "member_name") || "");
  const memberName = String(organizationMemberIdentity?.member?.name || "该成员");
  const question = target
    ? boundMemberName
      ? `“${principalName}”当前归属 ${boundMemberName}，改为归属 ${memberName}？该身份名下的历史用量与令牌会一并转移。`
      : `把“${principalName}”的历史用量与令牌关联到 ${memberName}？`
    : `解除“${principalName}”与 ${memberName} 的关联？解除后这部分历史用量不再计入该成员。`;
  if (!window.confirm(question)) return;
  isOrganizationMemberIdentityBusy = true;
  setText("organizationMemberIdentityError", "");
  try {
    await ensureCsrfToken();
    await api(
      organizationApiPath(`/principals/${encodeURIComponent(principalId)}/member`),
      { method: "POST", body: JSON.stringify({ memberId: target }) },
    );
    await loadOrganizationMemberIdentity();
    showToast(target ? "用量身份已关联" : "用量身份已解除");
  } catch (error) {
    setText("organizationMemberIdentityError", error.message || "用量身份关联失败");
  } finally {
    isOrganizationMemberIdentityBusy = false;
  }
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

async function resendOrganizationMemberInvitation(memberId) {
  if (!organizationCanManage() || !isRealOrganizationMode()) return;
  try {
    await ensureCsrfToken();
    await api(organizationApiPath(`/members/${encodeURIComponent(memberId)}/invitation/resend`), {
      method: "POST",
      body: JSON.stringify({}),
    });
    await loadOrganizationData();
    showToast("邀请已重新发送");
  } catch (error) {
    showToast(error.message || "邀请重发失败");
  }
}

async function revokeOrganizationMemberInvitation(memberId) {
  if (!organizationCanManage() || !isRealOrganizationMode()) return;
  const member = organizationMembers.find((item) => organizationMemberId(item) === String(memberId));
  if (!window.confirm(`撤销发给“${member?.email || "该成员"}”的邀请？撤销后当前链接将立即失效。`)) return;
  try {
    await ensureCsrfToken();
    await api(organizationApiPath(`/members/${encodeURIComponent(memberId)}/invitation/revoke`), {
      method: "POST",
      body: JSON.stringify({}),
    });
    await loadOrganizationData();
    showToast("邀请已撤销");
  } catch (error) {
    showToast(error.message || "邀请撤销失败");
  }
}

async function reinviteOrganizationMember(memberId) {
  if (!organizationCanManage() || !isRealOrganizationMode()) return;
  try {
    await ensureCsrfToken();
    await api(organizationApiPath(`/members/${encodeURIComponent(memberId)}`), {
      method: "PATCH",
      body: JSON.stringify({ status: "invited" }),
    });
    await loadOrganizationData();
    showToast("成员已重新进入邀请流程");
  } catch (error) {
    await loadOrganizationData();
    showToast(error.message || "重新邀请失败");
  }
}

async function removeOrganizationMember(memberId) {
  if (!organizationCanManage()) return;
  const member = organizationMembers.find((item) => organizationMemberId(item) === String(memberId));
  const label = member?.name || member?.email || "该成员";
  // 待邀请成员还没有令牌、也没有绑定登录账号，照抄已暂停成员那套"令牌立即失效"的
  // 警告会把人吓住，只说清"邀请作废、要重新邀请"就够了。
  const question = String(member?.status || "") === "invited"
    ? `删除成员“${label}”？该成员尚未接受邀请，删除后邀请链接立即失效，日后加入需要重新邀请。此操作不可撤销。`
    : `删除成员“${label}”？删除后该成员将移出企业，其访问令牌立即失效、登录账号解除绑定，此操作不可撤销。历史用量仍会保留在报表中。`;
  if (!window.confirm(question)) {
    return;
  }
  try {
    await ensureCsrfToken();
    await api(organizationApiPath(`/members/${encodeURIComponent(memberId)}`), {
      method: "DELETE",
    });
    await loadOrganizationData();
    showToast("成员已移除");
  } catch (error) {
    await loadOrganizationData();
    showToast(error.message || "成员删除失败");
  }
}

function switchView(view) {
  if (view === "customers" && !customerOrganizationsAvailable()) view = "dashboard";
  if (view === "admin" && !canViewAdminUsage()) view = "dashboard";
  if (view === "department" && !canViewDepartmentUsage()) view = "dashboard";
  if (view === "team" && !currentUser?.isTeamLeader) view = "dashboard";
  if (view === "organization" && !organizationCanView()) view = "dashboard";
  if (view === "organization-tokens" && !canViewOrganizationTokens()) view = "dashboard";
  if (isOrganizationCustomerIdentity() && (view === "keys" || view === "models")) view = "dashboard";
  if (view === "billing" && !canAccessBillingView()) view = "dashboard";
  if (view === "stability" && !canViewStability()) view = "dashboard";
  if (view === "cost-control" && !canViewCosts()) view = "dashboard";
  if (view === "governance-workbench" && !(canManageCosts() || canReconcileCosts())) view = "dashboard";
  if (currentView === "keys" && view !== "keys") clearRevealedKeys();
  // 离开充值页就停掉支付轮询与二维码，避免后台空转和收款码久留在页面上。
  if (currentView === "billing" && view !== "billing") {
    hideManualPayPanel();
    closeOrganizationTopupModal({ force: true });
    closeOrganizationCreditAdjustmentModal({ force: true });
  }
  currentView = view;
  setGlobalPage(view === "models" ? "models" : "console");
  el("appShell").classList.toggle("models-layout", view === "models");
  el("dashboardView").classList.toggle("hidden", view !== "dashboard");
  el("adminView").classList.toggle("hidden", view !== "admin");
  el("teamView").classList.toggle("hidden", view !== "team");
  el("departmentView").classList.toggle("hidden", view !== "department");
  el("customersView").classList.toggle("hidden", view !== "customers");
  el("organizationView").classList.toggle("hidden", view !== "organization");
  el("organizationTokensView")?.classList.toggle("hidden", view !== "organization-tokens");
  el("keysView").classList.toggle("hidden", view !== "keys");
  el("billingView").classList.toggle("hidden", view !== "billing");
  el("stabilityView")?.classList.toggle("hidden", view !== "stability");
  el("costControlView")?.classList.toggle("hidden", view !== "cost-control");
  el("governanceWorkbenchView")?.classList.toggle("hidden", view !== "governance-workbench");
  el("modelsView").classList.toggle("hidden", view !== "models");
  const topFilterMode = view === "stability" ? "stability" : view === "cost-control" ? "cost" : "default";
  el("dashboardFilters").classList.toggle("hidden", topFilterMode !== "default");
  el("stabilityTopFilters")?.classList.toggle("hidden", topFilterMode !== "stability");
  el("costTopFilters")?.classList.toggle("hidden", topFilterMode !== "cost");
  closeCustomRangePanel();
  renderOrganizationWorkspaceBar(view);
  const isCustomerDetailView = isViewingCustomerOrganization()
    && ["organization", "organization-tokens", "admin", "department", "billing"].includes(view);
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
    renderTeamKeys();
    if (!personalKeysAreFresh() && !isKeysLoading) loadKeys(false, { silent: hasLoadedPersonalKeys });
    if (canManageTeamKeys() && !teamMemberKeys.length && !isTeamKeysLoading) loadTeamKeys();
  }
  if (view === "billing") {
    renderBilling();
    if (isOrganizationBillingView()) {
      if (!organizationBillingLoading) loadOrganizationBillingData();
    } else if (!isBillingLoading) loadBillingData();
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
  if (view === "organization-tokens") {
    renderOrganizationTokens();
    if (
      !isOrganizationTokenLoading
      && (!organizationTokens.length || organizationTokenScopeKey !== organizationUsageScopeKey())
    ) {
      loadOrganizationTokens();
    }
  }
  syncMobileViewPicker();
  if (view === "stability") {
    renderStabilityOverview();
    if (!isStabilityLoading) loadStabilityOverview();
  }
  if (view === "cost-control") {
    renderCostOverview();
    if (!isCostOverviewLoading) loadCostOverview();
  }
  if (view === "governance-workbench") {
    renderGovernanceWorkbench();
    if (!governanceWorkbenchLoading && !(governanceWorkbenchData.planVersions.length || governanceWorkbenchData.savingsMeasurements.length)) loadGovernanceWorkbench();
  }
}

function observabilityCapabilities() {
  const explicit = currentUser?.observabilityCapabilities;
  if (explicit && typeof explicit === "object") return explicit;
  const legacy = Boolean(isPlatformAdmin() && currentUser?.observabilityDashboardsEnabled);
  return {
    stabilityView: legacy,
    stabilityManage: legacy,
    costView: legacy,
    costManage: legacy,
    costReconcile: legacy,
  };
}

function canViewStability() {
  return Boolean(observabilityCapabilities().stabilityView);
}

function canViewCosts() {
  return Boolean(observabilityCapabilities().costView);
}

function canManageCosts() {
  return Boolean(observabilityCapabilities().costManage);
}

function canReconcileCosts() {
  return Boolean(observabilityCapabilities().costReconcile);
}

function syncMobileViewPicker() {
  const select = el("mobileViewSelect");
  if (!select) return;
  const visibleTabs = [...document.querySelectorAll("#viewTabs [data-view]")].filter((button) => !button.classList.contains("hidden"));
  select.innerHTML = visibleTabs.map((button) => `<option value="${escapeHtml(button.dataset.view || "")}">${escapeHtml(button.textContent.trim())}</option>`).join("");
  if (visibleTabs.some((button) => button.dataset.view === currentView)) select.value = currentView;
  select.disabled = visibleTabs.length < 2;
}

async function loadCurrentViewData(forceRefresh = false) {
  if (currentView === "customers") return loadCustomerOrganizations();
  if (currentView === "keys") {
    // 团队成员密钥独立加载，负责人身份不满足时会自己收起面板。
    loadTeamKeys();
    return loadKeys(forceRefresh);
  }
  if (currentView === "billing") return isOrganizationBillingView() ? loadOrganizationBillingData() : loadBillingData();
  if (currentView === "stability") return loadStabilityOverview(forceRefresh);
  if (currentView === "cost-control") return loadCostOverview(forceRefresh);
  if (currentView === "governance-workbench") {
    return Promise.all([
      canViewStability() ? loadStabilityOverview(forceRefresh) : Promise.resolve(),
      canViewCosts() ? loadCostOverview(forceRefresh) : Promise.resolve(),
    ]);
  }
  if (currentView === "models") return loadModels();
  if (currentView === "admin") return loadAdminData(forceRefresh);
  if (currentView === "team") return loadTeamData(forceRefresh);
  if (currentView === "department") return loadDepartmentData(forceRefresh);
  if (currentView === "organization") return loadOrganizationData();
  if (currentView === "organization-tokens") return loadOrganizationTokens();
  return loadDashboardData(forceRefresh);
}

function isUsageView(view = currentView) {
  return ["dashboard", "admin", "department", "team"].includes(view);
}

async function refreshVisibleUsageData() {
  if (document.hidden || !currentUser || !isUsageView() || usageAutoRefreshPromise) return;
  usageAutoRefreshPromise = (async () => {
    try {
      if (currentView === "team" && selectedTeamEmployee) {
        await Promise.all([
          loadTeamMemberData(selectedTeamEmployee, true, false),
          loadTeamRankingData(true),
        ]);
      } else {
        await loadCurrentViewData(false);
      }
      lastUsageAutoRefreshAt = Date.now();
    } finally {
      usageAutoRefreshPromise = null;
    }
  })();
  return usageAutoRefreshPromise;
}

function scheduleUsageAutoRefresh() {
  if (usageAutoRefreshTimer) clearInterval(usageAutoRefreshTimer);
  usageAutoRefreshTimer = setInterval(refreshVisibleUsageData, 30_000);
}

function observabilityPercent(value) {
  return value === null || value === undefined ? "暂无数据" : `${(Number(value) * 100).toFixed(2)}%`;
}

function observabilityMoney(value) {
  return value === null || value === undefined ? "暂无数据" : `$${Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
}

function observabilityMetricObject(candidate, fallback = {}) {
  if (candidate && typeof candidate === "object" && !Array.isArray(candidate) && "value" in candidate) {
    return { ...fallback, ...candidate };
  }
  return { ...fallback, value: candidate };
}

function observabilityMetricStatus(metric) {
  const raw = String(metric?.status || "").toLowerCase();
  if (["good", "stable", "observed", "available", "verified", "actual", "approved", "active"].includes(raw)) return { label: raw === "observed" ? "已观测" : raw === "verified" ? "已核验" : "可用", tone: "good" };
  if (["danger", "failed", "error", "critical"].includes(raw)) return { label: "需处理", tone: "danger" };
  if (["warning", "partial", "derived", "low_coverage", "pending"].includes(raw)) return { label: raw === "derived" ? "推导口径" : "需关注", tone: "warning" };
  if (metric?.value === null || metric?.value === undefined) return { label: "暂不可用", tone: "" };
  return { label: "已计算", tone: "" };
}

function observabilityMetricCard({ label, metric, formatter, action = "", hint = "" }) {
  const normalized = observabilityMetricObject(metric);
  const status = observabilityMetricStatus(normalized);
  const value = normalized.value === null || normalized.value === undefined ? "暂无数据" : formatter(normalized.value);
  const coverage = normalized.coverageRate ?? normalized.completeness;
  const periodValue = normalized.period || "";
  const period = typeof periodValue === "object" ? [periodValue.startDate || periodValue.start, periodValue.endDate || periodValue.end].filter(Boolean).join(" 至 ") : String(periodValue);
  const asOf = normalized.asOf || normalized.as_of || "";
  const source = normalized.source || "";
  const sampleCount = normalized.sampleCount;
  const definitionVersion = normalized.definitionVersion || normalized.definitionsVersion || "";
  const metadata = [
    period,
    asOf ? `截至 ${asOf}` : "",
    coverage == null ? "" : `覆盖 ${(Number(coverage) * 100).toFixed(0)}%`,
    sampleCount == null ? "" : `样本 ${Number(sampleCount).toLocaleString("zh-CN")}`,
    source,
    definitionVersion ? `口径 ${definitionVersion}` : "",
  ].filter(Boolean);
  if (!metadata.length && Array.isArray(normalized.missingReasons) && normalized.missingReasons.length) metadata.push(`缺失：${normalized.missingReasons.join("、")}`);
  const tag = action ? "button" : "article";
  const actionAttrs = action ? ` type="button" data-observability-action="${escapeHtml(action)}"` : "";
  return `<${tag} class="observability-metric${action ? " is-action" : ""}"${actionAttrs}><div class="observability-metric-label"><span>${escapeHtml(label)}</span><span class="observability-metric-status ${status.tone}">${escapeHtml(status.label)}</span></div><strong>${escapeHtml(value)}</strong><div class="observability-metric-meta">${metadata.length ? metadata.map((item) => `<span>${escapeHtml(item)}</span>`).join("") : `<span>${escapeHtml(hint || "当前接口未返回完整审计元数据")}</span>`}</div></${tag}>`;
}

function stabilityScenarioMetricCard({ metric, scenario, action = "" }) {
  const normalized = observabilityMetricObject(metric);
  const status = observabilityMetricStatus(normalized);
  const count = normalized.value === null || normalized.value === undefined ? "暂无数据" : `${Number(normalized.value).toLocaleString("zh-CN")} 次`;
  const periodValue = normalized.period || "";
  const period = typeof periodValue === "object" ? [periodValue.startDate || periodValue.start, periodValue.endDate || periodValue.end].filter(Boolean).join(" 至 ") : String(periodValue);
  const metadata = [
    period,
    normalized.sampleCount == null ? "" : `样本 ${Number(normalized.sampleCount).toLocaleString("zh-CN")}`,
    normalized.definitionVersion || normalized.definitionsVersion ? `口径 ${normalized.definitionVersion || normalized.definitionsVersion}` : "",
  ].filter(Boolean);
  const tag = action ? "button" : "article";
  const actionAttrs = action ? ` type="button" data-observability-action="${escapeHtml(action)}"` : "";
  return `<${tag} class="observability-metric stability-scenario-metric-card${action ? " is-action" : ""}"${actionAttrs}><div class="observability-metric-label"><span>Top 异常场景</span><span class="observability-metric-status ${status.tone}">${escapeHtml(status.label)}</span></div><strong class="stability-scenario-metric-name">${escapeHtml(scenario || "未知场景")}</strong><strong class="stability-scenario-metric-count">${escapeHtml(count)}</strong><div class="observability-metric-meta">${metadata.length ? metadata.map((item) => `<span>${escapeHtml(item)}</span>`).join("") : "<span>当前接口未返回完整审计元数据</span>"}</div></${tag}>`;
}

function observabilityEmptyState(title, detail, actions = []) {
  return `<div class="observability-empty-state"><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p>${actions.length ? `<div class="observability-empty-actions">${actions.map((action) => `<button class="ghost-btn" type="button" ${action.attr}>${escapeHtml(action.label)}</button>`).join("")}</div>` : ""}</div>`;
}

function observabilityPayloadMeta(payload, data = {}) {
  const asOf = data.asOf || data.as_of || payload?.asOf || payload?.as_of || data.throughDate || data.annual?.throughDate || payload?.cache?.generatedAt?.slice?.(0, 10) || payload?.generatedAt?.slice?.(0, 10) || "";
  const source = payload?.source || data.source || "";
  const period = data.period || (data.month ? data.month : payload?.startDate && payload?.endDate ? `${payload.startDate} 至 ${payload.endDate}` : "");
  return { asOf, source, period };
}

function renderObservabilityContext(targetId, payload, data, scope) {
  const target = el(targetId);
  if (!target) return;
  const meta = observabilityPayloadMeta(payload, data);
  const active = activeObservabilityFilters(scope).map((item) => `${item.label}：${item.selectedLabel}`);
  const scopeText = active.length ? active.join(" · ") : "全部范围";
  target.innerHTML = `<strong>${scope === "cost" ? "截止日与范围" : "统计期间与来源"}</strong>${meta.period ? `<span class="observability-context-chip">${escapeHtml(meta.period)}</span>` : ""}${meta.asOf ? `<span class="observability-context-chip">截至 ${escapeHtml(meta.asOf)}</span>` : ""}<span class="observability-context-chip">${escapeHtml(scopeText)}</span>${meta.source ? `<span>数据来源：${escapeHtml(meta.source)}</span>` : ""}`;
}

function observabilityFilterConfig(scope) {
  if (scope === "stability") {
    return {
      resetId: "stabilityResetFiltersButton",
      activeId: "stabilityActiveFilters",
      filters: [{ id: "stabilityModel", label: "模型" }],
    };
  }
  return {
    buttonId: "costFiltersButton",
    panelId: "costFilterPanel",
    countId: "costFilterCount",
    resetId: "costResetFiltersButton",
    activeId: "costActiveFilters",
    filters: [
      { id: "costCategory", label: "成本项" },
      { id: "costBucket", label: "成本桶" },
      { id: "costModel", label: "模型" },
      { id: "costVendor", label: "来源" },
      { id: "costProvider", label: "供应渠道" },
      { id: "costAccount", label: "账号" },
      { id: "costReconciliation", label: "对账状态" },
      { id: "costRecognition", label: "确认状态" },
    ],
  };
}

function activeObservabilityFilters(scope) {
  return observabilityFilterConfig(scope).filters.flatMap(({ id, label }) => {
    const select = el(id);
    if (!select?.value) return [];
    const selectedLabel = select.options?.[select.selectedIndex]?.text || select.value;
    return [{ id, label, value: select.value, selectedLabel }];
  });
}

function setObservabilityFilterPanel(scope, open) {
  const config = observabilityFilterConfig(scope);
  const button = el(config.buttonId);
  const panel = el(config.panelId);
  if (!button || !panel) return;
  costFiltersOpen = open;
  button.classList.toggle("is-open", open);
  button.setAttribute("aria-expanded", String(open));
  panel.classList.toggle("is-open", open);
  panel.setAttribute("aria-hidden", String(!open));
  panel.inert = !open;
}

function renderObservabilityFilterState(scope) {
  const config = observabilityFilterConfig(scope);
  const active = activeObservabilityFilters(scope);
  const count = el(config.countId);
  const reset = el(config.resetId);
  const container = el(config.activeId);
  if (count) {
    count.textContent = String(active.length);
    count.setAttribute("aria-label", `${active.length} 个筛选条件`);
    count.classList.toggle("hidden", active.length === 0);
  }
  if (reset) reset.disabled = active.length === 0;
  if (!container) return;
  container.classList.toggle("hidden", active.length === 0);
  container.innerHTML = active.map((item) => `<button class="observability-active-filter" type="button" data-observability-clear="${escapeHtml(item.id)}" data-observability-scope="${scope}" aria-label="清除${escapeHtml(item.label)}筛选：${escapeHtml(item.selectedLabel)}"><span>${escapeHtml(item.label)}：${escapeHtml(item.selectedLabel)}</span><svg aria-hidden="true"><use href="#icon-close"></use></svg></button>`).join("");
}

function resetObservabilityFilters(scope) {
  observabilityFilterConfig(scope).filters.forEach(({ id }) => {
    const input = el(id);
    if (input) input.value = "";
  });
  renderObservabilityFilterState(scope);
  if (scope === "stability") loadStabilityOverview();
  else loadCostOverview();
}

function clearObservabilityFilter(scope, id) {
  const allowed = observabilityFilterConfig(scope).filters.some((filter) => filter.id === id);
  if (!allowed) return;
  const input = el(id);
  if (input) input.value = "";
  renderObservabilityFilterState(scope);
  if (scope === "stability") loadStabilityOverview();
  else loadCostOverview();
}

function currentStabilityWindow() {
  return selectedObservabilityRange("stability");
}

function selectedObservabilityRange(scope) {
  const select = el(`${scope}RangeSelect`);
  const custom = scope === "stability" ? stabilityCustomDateRange : costCustomDateRange;
  if (select?.value === "custom" && custom) return { ...custom, days: daysBetween(custom.startDate, custom.endDate) };
  const days = Number(select?.value || 7);
  const end = new Date();
  const start = new Date(end);
  start.setDate(end.getDate() - days + 1);
  return { startDate: localDate(start), endDate: localDate(end), days };
}

function currentCostWindow() {
  return selectedObservabilityRange("cost");
}

function currentCostMonth() {
  return currentCostWindow().endDate.slice(0, 7);
}

function observabilityReasonCopy(payload, scope) {
  const hasPayload = Boolean(payload?.data);
  const cache = payload?.cache || {};
  const loading = scope === "stability" ? isStabilityLoading : isCostOverviewLoading;
  const loadError = scope === "stability" ? stabilityLoadError : costOverviewLoadError;
  if (loadError) {
    return {
      title: hasPayload ? "刷新失败，继续显示最近成功快照" : `${scope === "stability" ? "稳定性" : "费用"}数据加载失败`,
      detail: loadError,
      action: "重新加载",
    };
  }
  if (loading && hasPayload) {
    return { title: "正在后台刷新", detail: "当前图表保持可用，刷新完成后会自动替换为最新数据。", action: "" };
  }
  if (scope === "stability" && loading) {
    return { title: "正在加载稳定性数据", detail: "正在读取当前窗口的事件与覆盖状态。", action: "" };
  }
  if (scope === "cost" && loading) {
    return { title: "正在加载费用数据", detail: "正在汇总成本账本、预算与节省动作。", action: "" };
  }
  if (["stale", "refreshing"].includes(String(cache.state || "").toLowerCase())) {
    const age = Number.isFinite(Number(cache.ageSeconds)) ? `，快照生成于 ${Math.max(0, Math.round(Number(cache.ageSeconds) / 60))} 分钟前` : "";
    return {
      title: cache.refreshing ? "正在刷新，显示最近成功快照" : "显示最近成功快照",
      detail: `${cache.lastRefreshError ? `上次刷新失败：${cache.lastRefreshError}` : "后台正在准备最新数据"}${age}。`,
      action: "再次刷新",
    };
  }
  const coverage = payload?.coverage || {};
  const freshness = payload?.freshness || {};
  const reasons = Array.isArray(coverage.missingReasons) ? coverage.missingReasons : [];
  const reasonText = {
    not_synced: "尚未完成数据同步",
    partial_scan: "同步仍在进行，当前窗口只有部分数据",
    backfill_pending: "所选窗口早于已回填范围",
    sync_error: "数据同步出现异常",
    no_events_or_filter_match: "当前窗口无事件或筛选无结果",
    field_missing: "上游记录缺少必要字段",
  };
  if (scope === "stability" && (reasons.length || coverage.incomplete || coverage.partial)) {
    return null;
  }
  if (reasons.length) {
    return {
      title: scope === "stability" ? "稳定性数据覆盖提示" : "费用数据覆盖提示",
      detail: reasons.map((item) => reasonText[item] || item).join("；"),
      action: scope === "stability" ? "重新加载" : "查看明细",
    };
  }
  if (["empty", "unavailable", "not_synced"].includes(String(freshness.status || "").toLowerCase()) && !payload?.data) {
    return {
      title: scope === "stability" ? "稳定性数据尚未接入" : "费用账本尚未同步",
      detail: scope === "stability" ? "当前没有可审计的稳定性事件，缺失指标不会显示为 0。" : "当前没有可审计的费用记录，缺失金额不会显示为 0。",
      action: "重新加载",
    };
  }
  if (coverage.incomplete || coverage.partial) {
    return {
      title: scope === "stability" ? "当前窗口覆盖不足" : "当前月份覆盖不足",
      detail: scope === "stability"
        ? "缺失字段不会按 0 计算；兜底指标需要上游显式事件。"
        : "没有记录的成本维度会保留为暂无数据，不会伪造为 0。",
      action: scope === "stability" ? "重新加载" : "查看明细",
    };
  }
  return null;
}

function renderObservabilityQuality(elementId, payload, scope) {
  const target = el(elementId);
  if (!target) return;
  const state = observabilityReasonCopy(payload, scope);
  const isError = Boolean((scope === "stability" && stabilityLoadError) || (scope === "cost" && costOverviewLoadError));
  target.classList.toggle("hidden", !state);
  target.classList.toggle("danger", isError);
  target.setAttribute("role", isError ? "alert" : "status");
  target.setAttribute("aria-live", isError ? "assertive" : "polite");
  target.setAttribute("aria-atomic", "true");
  if (!state) {
    target.innerHTML = "";
    return;
  }
  target.innerHTML = `<span class="operational-status-icon" aria-hidden="true">!</span><div><strong>${escapeHtml(state.title)}</strong><p>${escapeHtml(state.detail)}</p></div>${state.action ? `<button class="ghost-btn operational-status-action" type="button" data-observability-retry="${scope}">${escapeHtml(state.action)}</button>` : ""}`;
}

function stabilityMetricValue(overview, primary, legacy = null) {
  const value = overview?.[primary];
  return value !== undefined ? value : overview?.[legacy];
}

function stabilityMetricContract(overview, data, payload, primary, legacy, defaults = {}) {
  const contracts = overview?.metricEnvelopes || data?.metricEnvelopes || data?.metrics || data?.metricContracts || {};
  const candidate = contracts[primary] ?? contracts[legacy] ?? stabilityMetricValue(overview, primary, legacy);
  return observabilityMetricObject(candidate, {
    period: payload?.startDate && payload?.endDate ? `${payload.startDate} 至 ${payload.endDate}` : "",
    source: payload?.source || "",
    definitionVersion: overview?.definitionsVersion || data?.definitionsVersion || "",
    ...defaults,
  });
}

function renderStabilityTrendChart(container, daily) {
  if (!container) return;
  const points = [...(daily || [])]
    .map((item) => ({
      date: String(item.date || ""),
      upstream: item.upstreamExceptionCount,
      failures: item.finalRequestFailureCount ?? item.userVisibleFailureCount,
    }))
    .sort((a, b) => a.date.localeCompare(b.date));
  const trendValues = points.flatMap((item) => [item.upstream, item.failures]).filter((value) => value !== null && value !== undefined).map(Number);
  const hasTrendValues = trendValues.length > 0;
  const hasNonZeroTrend = trendValues.some((value) => value > 0);
  const isEmpty = !points.length || !hasTrendValues || !hasNonZeroTrend;
  container.classList.toggle("is-empty", isEmpty);
  container.classList.toggle("is-chart", !isEmpty);
  if (isEmpty) {
    container.innerHTML = hasTrendValues && !hasNonZeroTrend
      ? observabilityEmptyState("本期确无异常记录", "当前统计期间内，已接入指标均为真实零值。", [{ label: "调整筛选", attr: 'data-observability-empty-action="filters" data-observability-scope="stability"' }])
      : observabilityEmptyState("异常趋势暂不可用", "尝试事件尚未接入或当前窗口尚未同步，缺失数据不会绘制为零值。", [{ label: "重新加载", attr: 'data-observability-retry="stability"' }]);
    return;
  }
  const width = 900;
  const height = 280;
  const pad = { left: 54, right: 18, top: 20, bottom: 42 };
  const max = Math.max(1, ...trendValues);
  const xStep = points.length > 1 ? (width - pad.left - pad.right) / (points.length - 1) : 1;
  const y = (value) => height - pad.bottom - (Number(value) / max) * (height - pad.top - pad.bottom);
  const x = (index) => (points.length > 1 ? pad.left + index * xStep : width / 2);
  const seriesPath = (field) => {
    let path = "";
    let penDown = false;
    points.forEach((item, index) => {
      const value = item[field];
      if (value === null || value === undefined) {
        penDown = false;
        return;
      }
      path += `${penDown ? "L" : "M"}${x(index).toFixed(2)} ${y(Number(value)).toFixed(2)} `;
      penDown = true;
    });
    return path.trim();
  };
  const grid = [0, 0.25, 0.5, 0.75, 1]
    .map((ratio) => {
      const yy = y(max * ratio);
      const label = Math.round(max * ratio).toLocaleString("zh-CN");
      const isBaseline = ratio === 0;
      return `<line x1="${pad.left}" y1="${yy.toFixed(2)}" x2="${width - pad.right}" y2="${yy.toFixed(2)}" stroke="${isBaseline ? "#b9c6d6" : "#dde5ee"}" stroke-width="${isBaseline ? "1.5" : "1"}"${isBaseline ? "" : ' stroke-dasharray="4 7"'}/><text x="12" y="${(yy + 4).toFixed(2)}" fill="#66748a" font-size="12">${label}</text>`;
    })
    .join("");
  const axisLine = `<line x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}" stroke="#dde5ee"/>`;
  const labelEvery = Math.max(1, Math.ceil(points.length / 5));
  const labels = points
    .map((item, index) => ({ item, index }))
    .filter(({ index }) => index === 0 || index === points.length - 1 || index % labelEvery === 0)
    .map(({ item, index }, position, arr) => {
      const anchor = position === arr.length - 1 ? "end" : "middle";
      const cx = x(index);
      const textX = anchor === "end" ? Math.min(cx, width - pad.right) : Math.max(cx, pad.left);
      return `<text x="${textX.toFixed(2)}" y="${height - 14}" fill="#66748a" font-size="12" text-anchor="${anchor}">${escapeHtml(item.date.slice(5))}</text>`;
    })
    .join("");
  const seriesDots = (field, color) => points
    .map((item, index) => {
      const value = item[field];
      if (value === null || value === undefined) return "";
      return `<circle cx="${x(index).toFixed(2)}" cy="${y(Number(value)).toFixed(2)}" r="4.5" fill="${color}"/>`;
    })
    .join("");
  const hits = points.map((item, index) => {
    const html = tooltipMarkup(item.date, [
      { label: "调用过程中出错（上游异常）", value: item.upstream == null ? "无数据" : Number(item.upstream).toLocaleString("zh-CN") },
      { label: "用户最终失败", value: item.failures == null ? "无数据" : Number(item.failures).toLocaleString("zh-CN") },
    ]);
    const hitWidth = Math.max(xStep, 16);
    return `<rect class="chart-hit" x="${(x(index) - hitWidth / 2).toFixed(2)}" y="0" width="${hitWidth}" height="${height - pad.bottom}" fill="transparent" data-tooltip="${encodeURIComponent(html)}"/>`;
  }).join("");
  container.innerHTML = `<svg class="observability-trend-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="调用过程中出错与用户最终失败按天趋势">${grid}${axisLine}${labels}<path d="${seriesPath("upstream")}" fill="none" stroke="#1f5fd0" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/><path d="${seriesPath("failures")}" fill="none" stroke="#e2783d" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>${seriesDots("upstream", "#1f5fd0")}${seriesDots("failures", "#e2783d")}${hits}</svg>`;
  bindChartTooltipEvents(container.querySelector("svg"));
}

function renderStabilityOverview() {
  const payload = stabilityOverview;
  if (!payload) {
    renderObservabilityQuality("stabilityQuality", payload, "stability");
    if (isStabilityLoading) {
      el("stabilityMetrics").innerHTML = observabilityEmptyState("正在读取稳定性指标", "正在汇总最终请求、尝试事件与覆盖率。", []);
    }
    return;
  }
  const data = payload.data || {};
  const overview = data.overview || {};
  const quality = overview.quality || data.quality || {};
  const finalFailure = stabilityMetricContract(overview, data, payload, "finalRequestFailureRate", "userVisibleFailureRate", { coverageRate: quality.finalRequestFailure?.completeness, sampleCount: overview.requestCount, status: quality.finalRequestFailure?.status });
  const fallbackRecovery = stabilityMetricContract(overview, data, payload, "fallbackRecoveryRate", "fallbackSuccessRate", { coverageRate: quality.fallbackRecovery?.completeness, sampleCount: overview.fallbackAttemptCount, status: quality.fallbackRecovery?.status });
  const ttft = stabilityMetricContract(overview, data, payload, "ttftP95Ms", "ttftP95Ms", { coverageRate: overview.ttftCoverageRate ?? quality.ttft?.completeness, sampleCount: overview.ttftSampleCount ?? quality.ttft?.sampleCount, status: (overview.ttftCoverageRate ?? quality.ttft?.completeness ?? 0) < 0.8 || Number(overview.ttftSampleCount ?? quality.ttft?.sampleCount ?? 0) < 30 ? "low_coverage" : quality.ttft?.status });
  const topScenario = (data.topScenarios || [])[0];
  const topScenarioMetric = observabilityMetricObject(topScenario?.count, { period: finalFailure.period, source: payload.source, sampleCount: topScenario?.count, status: topScenario ? "observed" : "unavailable", definitionVersion: overview.definitionsVersion || data.definitionsVersion });
  el("stabilityMetrics").innerHTML = [
    observabilityMetricCard({ label: "用户最终失败率", metric: finalFailure, formatter: observabilityPercent, action: "stability-final-failures" }),
    observabilityMetricCard({ label: "兜底成功率", metric: fallbackRecovery, formatter: observabilityPercent, action: "stability-fallbacks", hint: "需接入显式兜底尝试事件" }),
    observabilityMetricCard({ label: "TTFT P95", metric: ttft, formatter: (value) => `${(Number(value) / 1000).toFixed(2)}s`, action: "stability-ttft" }),
    stabilityScenarioMetricCard({ metric: topScenarioMetric, scenario: topScenario?.scenario, action: "stability-top-scenario" }),
  ].join("");
  renderObservabilityContext("stabilityContext", payload, data, "stability");
  renderStabilityTrendChart(el("stabilityTrend"), data.daily || []);
  const stabilityRankings = data.modelRankings || [];
  const visibleStabilityRankings = stabilityRankings.filter((item) => {
    const failureRate = item.finalRequestFailureRate ?? item.userVisibleFailureRate;
    if (failureRate === null || failureRate === undefined) return true;
    const numericFailureRate = Number(failureRate);
    return !Number.isFinite(numericFailureRate) || (numericFailureRate !== 0 && numericFailureRate !== 1);
  });
  const stabilityRanking = el("stabilityRanking");
  stabilityRanking.classList.toggle("is-compact", visibleStabilityRankings.length > 0 && visibleStabilityRankings.length <= 4);
  const formatStabilityTtft = (value) => {
    if (value == null || !Number.isFinite(Number(value))) return "暂无数据";
    return Number(value) >= 1000 ? `${(Number(value) / 1000).toFixed(1)}s` : `${Math.round(Number(value))}ms`;
  };
  const formatStabilityFallback = (item) => {
    if (item.fallbackRecoveryStatus === "not_triggered") return "未触发";
    if (item.fallbackRecoveryStatus !== "observed") return "暂无数据";
    return observabilityPercent(item.fallbackRecoveryRate);
  };
  const stabilityRankingStateClass = (state) => ({ "稳定": "stable", "观察": "observe", "需治理": "repair" }[state] || "unknown");
  const stabilityScore = (item) => {
    const failureRate = Number(item.finalRequestFailureRate ?? item.userVisibleFailureRate);
    const stateWeight = { "稳定": 100, "观察": 64, "需治理": 28 }[item.state] ?? 44;
    if (!Number.isFinite(failureRate)) return stateWeight;
    return Math.max(8, Math.min(100, Math.round(stateWeight - failureRate * 100)));
  };
  stabilityRanking.innerHTML = visibleStabilityRankings.length ? `<div class="observability-ranking-list">${visibleStabilityRankings.map((item, index) => {
    const failureRate = item.finalRequestFailureRate ?? item.userVisibleFailureRate;
    const modelName = item.requestedModelGroup || item.model || "未知模型";
    const state = item.state || "暂无数据";
    const score = stabilityScore(item);
    return `<button class="observability-rank-card is-${stabilityRankingStateClass(state)}" type="button" title="查看 ${escapeHtml(modelName)} 的稳定性详情" data-stability-model="${escapeHtml(item.model || item.requestedModelGroup || "")}">${rankingBadge(index)}<div class="observability-rank-identity"><strong class="observability-rank-model">${escapeHtml(modelName)}</strong><div class="observability-rank-score"><span>综合稳定度</span><div class="observability-rank-track" aria-hidden="true"><div class="observability-rank-fill" style="width:${score}%"></div></div><strong>${score}</strong></div></div><div class="observability-rank-metrics"><span class="observability-rank-metric"><span>失败</span><strong>${escapeHtml(observabilityPercent(failureRate))}</strong></span><span class="observability-rank-metric"><span>兜底</span><strong>${escapeHtml(formatStabilityFallback(item))}</strong></span><span class="observability-rank-metric"><span>TTFT</span><strong>${escapeHtml(formatStabilityTtft(item.ttftP95Ms))}</strong></span></div><span class="observability-rank-status">${escapeHtml(state)}</span></button>`;
  }).join("")}</div>` : observabilityEmptyState("暂无模型排名", "当前窗口没有可比较的模型样本。", [{ label: "调整筛选", attr: 'data-observability-empty-action="filters" data-observability-scope="stability"' }]);
  const topScenarios = data.topScenarios || [];
  const scenarioCountChip = el("stabilityScenarioCount");
  if (scenarioCountChip) scenarioCountChip.textContent = `${topScenarios.length} 个场景`;
  const maxScenarioCount = Math.max(1, ...topScenarios.map((item) => Number(item.count) || 0));
  el("stabilityScenarioRanking").innerHTML = topScenarios.length ? topScenarios.map((item, index) => {
    const failureRate = item.finalRequestFailureRate ?? item.userVisibleFailureRate;
    const count = Number(item.count) || 0;
    const modelName = item.requestedModelGroup || item.model || "-";
    const errorCode = item.errorCode || "-";
    const countPercent = Math.max(3, count / maxScenarioCount * 100);
    return `<button class="stability-scenario-card" type="button" title="查看 ${escapeHtml(item.scenario || "未知场景")} 的异常样本" data-stability-scenario="${escapeHtml(item.scenario || "")}" data-stability-model="${escapeHtml(item.requestedModelGroup || item.model || "")}" data-stability-error-code="${escapeHtml(item.errorCode || "")}">${rankingBadge(index)}<div class="stability-scenario-identity"><strong class="stability-scenario-name">${escapeHtml(item.scenario || "未知场景")}</strong><div class="stability-scenario-count"><span>异常次数</span><div class="stability-scenario-track" aria-hidden="true"><div class="stability-scenario-fill" style="width:${countPercent}%"></div></div><strong>${count.toLocaleString("zh-CN")}</strong></div></div><div class="stability-scenario-metrics"><span class="stability-scenario-metric"><span>最终失败率</span><strong>${escapeHtml(observabilityPercent(failureRate))}</strong></span><span class="stability-scenario-metric"><span>模型组</span><strong title="${escapeHtml(modelName)}">${escapeHtml(modelName)}</strong></span><span class="stability-scenario-metric"><span>错误码</span><strong title="${escapeHtml(errorCode)}">${escapeHtml(errorCode)}</strong></span></div><span class="stability-scenario-action">查看样本</span></button>`;
  }).join("") : observabilityEmptyState(hasTrendValues && !hasNonZeroTrend ? "本期确无异常场景" : "异常场景数据暂不可用", hasTrendValues && !hasNonZeroTrend ? "当前窗口没有需要下钻的异常请求。" : "请稍后刷新，或调整筛选范围后重试。", [{ label: "调整筛选", attr: 'data-observability-empty-action="filters" data-observability-scope="stability"' }]);
  renderObservabilityQuality("stabilityQuality", payload, "stability");
  const models = [...new Set((data.modelRankings || []).map((item) => item.model))];
  const select = el("stabilityModel");
  const selected = select.value;
  select.innerHTML = `<option value="">全部模型</option>${models.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("")}`;
  select.value = models.includes(selected) ? selected : "";
  renderObservabilityFilterState("stability");
}

async function loadStabilityOverview(forceRefresh = false) {
  if (!canViewStability()) return;
  const requestId = ++stabilityOverviewRequestId;
  stabilityOverviewController?.abort();
  stabilityOverviewController = new AbortController();
  isStabilityLoading = true;
  stabilityLoadError = "";
  renderObservabilityQuality("stabilityQuality", stabilityOverview, "stability");
  try {
    const { startDate, endDate } = currentStabilityWindow();
    const model = el("stabilityModel")?.value || "";
    const nextOverview = await api(`/api/admin/stability/overview?start_date=${startDate}&end_date=${endDate}&model=${encodeURIComponent(model)}${forceRefresh ? "&refresh=1" : ""}`, { signal: stabilityOverviewController.signal, cache: "no-store" });
    if (requestId !== stabilityOverviewRequestId) return;
    stabilityOverview = nextOverview;
  } catch (error) {
    if (error.name !== "AbortError" && requestId === stabilityOverviewRequestId) {
      stabilityLoadError = error.message || "稳定性看板加载失败";
      showToast(stabilityLoadError);
    }
  } finally {
    if (requestId === stabilityOverviewRequestId) {
      isStabilityLoading = false;
      renderStabilityOverview();
      if (currentView === "governance-workbench") renderGovernanceWorkbench();
    }
  }
}

function renderCostOverview() {
  if (!costOverview) {
    renderObservabilityQuality("costQuality", costOverview, "cost");
    if (isCostOverviewLoading) el("costMetrics").innerHTML = observabilityEmptyState("正在读取费用口径", "正在汇总实际账本、计划版本、预算与节省证明。", []);
    return;
  }
  const data = costOverview.data || {};
  const metrics = data.metrics || {};
  const annual = data.annual || {};
  document.querySelectorAll("#governanceWorkbenchView [data-cost-manage]").forEach((node) => node.classList.toggle("hidden", !canManageCosts()));
  document.querySelectorAll("#governanceWorkbenchView input, #governanceWorkbenchView textarea, #governanceWorkbenchView select").forEach((node) => {
    if (node.closest(".observability-filter-bar") || node.closest(".observability-filter-panel")) return;
    if (node.closest("[data-cost-manage]")) node.disabled = !canManageCosts();
  });
  const meta = observabilityPayloadMeta(costOverview, data);
  const contractMetrics = data.metricContracts || data.coreMetrics || metrics.metricEnvelopes || {};
  const annualContracts = annual.metricEnvelopes || {};
  const annualActual = observabilityMetricObject(annualContracts.actualToDate ?? contractMetrics.yearToDateActual ?? metrics.yearToDateActual ?? annual.actualToDate ?? annual.actual, { period: `${String(data.month || currentCostMonth()).slice(0, 4)} 年`, asOf: meta.asOf, source: costOverview.source, status: "actual" });
  const activePlan = data.activePlanVersion || data.activePlan || annual.activePlanVersion || annual.activePlan || governanceWorkbenchData.planVersions.find((item) => (item.active || item.isActive || item.activatedAt) && ["approved", "active"].includes(String(item.status || "").toLowerCase()));
  const hasOfficialForecastContract = contractMetrics.officialYearForecast !== undefined || metrics.officialYearForecast !== undefined || annual.officialForecast !== undefined;
  const officialForecastValue = annualContracts.officialForecast ?? (hasOfficialForecastContract ? (contractMetrics.officialYearForecast ?? metrics.officialYearForecast ?? annual.officialForecast) : activePlan ? (metrics.yearForecast ?? annual.forecast) : null);
  const officialForecast = observabilityMetricObject(officialForecastValue, { period: `${String(data.month || currentCostMonth()).slice(0, 4)} 年`, asOf: meta.asOf, source: activePlan?.name || activePlan?.version || "生效基准计划", status: officialForecastValue == null ? "unavailable" : "approved", missingReasons: officialForecastValue == null ? ["active_approved_baseline_plan_missing"] : [] });
  const verifiedSavings = observabilityMetricObject(contractMetrics.verifiedSavings ?? metrics.verifiedSavingsToDate ?? metrics.realizedSavingsToDate ?? metrics.verifiedSavings, { period: `${String(data.month || currentCostMonth()).slice(0, 4)} 年`, asOf: meta.asOf, source: "已复核节省证明", status: "verified", sampleCount: data.savingsMeasurements?.reviewedCount ?? data.savingsMeasurements?.filter?.((item) => item.reviewedAt || item.financeReviewedAt)?.length });
  const budgetOrTarget = observabilityMetricObject(contractMetrics.budget ?? metrics.intervalBudget ?? metrics.monthBudget ?? metrics.budget ?? metrics.dailyTarget, { period: meta.period || `${data.startDate || ""} ～ ${data.endDate || ""}`, asOf: meta.asOf, source: metrics.intervalBudget != null || metrics.monthBudget != null || metrics.budget != null ? "预算账本" : "日均目标", status: "available" });
  el("costMetrics").innerHTML = [
    observabilityMetricCard({ label: "年度累计实际", metric: annualActual, formatter: observabilityMoney, action: "cost-actual-ledger" }),
    observabilityMetricCard({ label: "全年官方预测", metric: officialForecast, formatter: observabilityMoney, action: "cost-plan-versions", hint: "没有生效基准计划时不提供官方预测" }),
    observabilityMetricCard({ label: "已核验累计节省", metric: verifiedSavings, formatter: observabilityMoney, action: "cost-savings" }),
    observabilityMetricCard({ label: metrics.intervalBudget != null || metrics.monthBudget != null || metrics.budget != null ? "区间预算" : "日均目标", metric: budgetOrTarget, formatter: observabilityMoney, action: "cost-budget" }),
  ].join("");
  renderObservabilityContext("costContext", costOverview, data, "cost");
  el("savingsActionList").innerHTML = (data.savingsActions || []).map((item) => `<article class="observability-action"><div><strong>${escapeHtml(item.name)}</strong><p class="hint">${escapeHtml(item.owner || "未指定负责人")} · ${escapeHtml(item.status)}${item.evidenceUrl ? " · 有证据" : " · 缺证据"}</p></div><div class="observability-table-actions"><span>${item.status === "verified" ? `${observabilityMoney(item.realizedSavingsToDate ?? Math.max(0, Number(item.baselineDailyCost) - Number(item.verifiedDailyCost || 0)))}/日` : item.forecastSavingsRemaining == null ? "尚未计入节省" : `预计 ${observabilityMoney(item.forecastSavingsRemaining)}`}</span>${canManageCosts() ? `<button class="ghost-btn" type="button" data-edit-savings-action="${escapeHtml(item.id)}">编辑</button>` : "只读"}</div></article>`).join("") || `<p class="empty">暂无降本动作</p>`;
  renderCostModelShare(data.modelCostShare || data.modelSplit || []);
  renderCostTrendBreakdown(data);
  const targetMonth = String(data.month || currentCostMonth()).slice(0, 7);
  const budget = costBudgets.find((item) => String(item.month).slice(0, 7) === targetMonth);
  if (el("costBudgetAmount")) el("costBudgetAmount").value = String(budget?.budgetUsd ?? metrics.budget ?? "");
  if (el("costDailyTarget")) el("costDailyTarget").value = String(budget?.dailyTargetUsd ?? metrics.dailyTarget ?? "");
  if (el("costBudgetDelta")) el("costBudgetDelta").textContent = metrics.budgetDelta == null ? "暂无预算差额" : `预测与预算差额：${observabilityMoney(metrics.budgetDelta)}`;
  el("costItemBody").innerHTML = (data.costItems || []).map((item) => `<tr><td>${escapeHtml(item.name)}</td><td>${escapeHtml(item.costBucket || item.category)}</td><td>${escapeHtml(item.accountName || item.provider || item.vendor || item.businessScope || "-")}</td><td>${escapeHtml(item.serviceStartDate)} ～ ${escapeHtml(item.serviceEndDate)}</td><td>${observabilityMoney(item.amountUsd)} <span class="hint">${escapeHtml(item.currency)}</span></td><td>${escapeHtml(item.reconciliationStatus || (item.enabled ? "未对账" : "停用"))}</td><td>${canManageCosts() ? `<button class="ghost-btn" type="button" data-edit-cost-item="${escapeHtml(item.id)}">编辑</button><button class="danger-outline-btn" type="button" data-delete-cost-item="${escapeHtml(item.id)}">删除</button>` : "只读"}</td></tr>`).join("") || `<tr><td colspan="7" class="empty">暂无人工成本项</td></tr>`;
  const planVersions = data.planVersions || annual.planVersions || (governanceWorkbenchData.planVersions.length ? governanceWorkbenchData.planVersions : []) || (activePlan ? [activePlan] : []);
  el("costPlanVersions").innerHTML = planVersions.map((item) => `<article class="observability-action"><div><strong>${escapeHtml(item.name || item.version || `${item.year || ""} 基准计划`)}</strong><p class="hint">${escapeHtml(item.scenario || "基准")} · ${escapeHtml(item.status || "未知状态")}${item.asOf ? ` · 截止 ${escapeHtml(item.asOf)}` : ""}${item.approvedBy ? ` · ${escapeHtml(item.approvedBy)} 批准` : ""}</p></div><div class="observability-table-actions"><span class="chip ${item.active || item.status === "active" || item.status === "approved" ? "green" : "gold"}">${item.active ? "生效中" : escapeHtml(item.status || "草稿")}</span>${canManageCosts() ? `<button class="ghost-btn" type="button" data-edit-cost-plan="${escapeHtml(item.id || "")}">编辑</button>${item.status === "draft" ? `<button class="ghost-btn" type="button" data-cost-plan-state="approve" data-cost-plan-id="${escapeHtml(item.id || "")}">批准</button>` : ""}${item.status === "approved" ? `<button class="ghost-btn" type="button" data-cost-plan-state="activate" data-cost-plan-id="${escapeHtml(item.id || "")}">激活</button>` : ""}${item.status !== "archived" ? `<button class="ghost-btn" type="button" data-cost-plan-state="archive" data-cost-plan-id="${escapeHtml(item.id || "")}">归档</button>` : ""}` : ""}</div></article>`).join("") || observabilityEmptyState("暂无计划版本", "创建并批准基准计划后，费用看板才会展示官方全年预测。", []);
  renderObservabilityQuality("costQuality", costOverview, "cost");
  const filters = data.filters || {};
  [["costCategory", "全部成本项", filters.categories || []], ["costBucket", "全部成本桶", filters.costBuckets || filters.buckets || []], ["costModel", "全部模型", filters.models || []], ["costVendor", "全部来源", filters.vendors || []], ["costProvider", "全部供应渠道", filters.providers || []], ["costAccount", "全部账号", filters.accounts || []], ["costReconciliation", "全部对账状态", filters.reconciliationStatuses || []], ["costRecognition", "全部确认状态", filters.recognitionStatuses || []]].forEach(([id, label, options]) => {
    const select = el(id);
    if (!select) return;
    const selected = select.value;
    select.innerHTML = `<option value="">${label}</option>${options.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("")}`;
    select.value = options.includes(selected) ? selected : "";
  });
  renderObservabilityFilterState("cost");
}

function buildCostTrendBreakdownPoints(data) {
  const trend = Array.isArray(data?.trend) ? data.trend : [];
  const opportunityByDate = new Map();
  for (const item of Array.isArray(data?.modelCostShare) ? data.modelCostShare : []) {
    for (const point of Array.isArray(item?.daily) ? item.daily : []) {
      const date = String(point?.date || "");
      if (!date) continue;
      opportunityByDate.set(date, (opportunityByDate.get(date) || 0) + Math.max(0, Number(point?.opportunity || 0)));
    }
  }
  let cumulativeVolatility = 0;
  return trend.map((point) => {
    const date = String(point?.date || "");
    const dailyOpportunity = opportunityByDate.get(date) || 0;
    cumulativeVolatility += dailyOpportunity;
    const actual = point?.actual;
    const forecast = point?.forecast;
    return {
      date,
      actualSpend: actual !== null && actual !== undefined && Number.isFinite(Number(actual)) ? Number(actual) : null,
      forecastSpend: forecast !== null && forecast !== undefined && Number.isFinite(Number(forecast)) ? Number(forecast) : null,
      cumulativeVolatility,
    };
  });
}

function renderCostTrendBreakdown(data) {
  const points = buildCostTrendBreakdownPoints(data);
  renderMultiLineChart({
    svg: el("costTrendBreakdownChart"),
    points,
    axisFormatter: observabilityMoney,
    series: [
      { valueField: "actualSpend", label: "实际支出", color: "#2d6cdf" },
      { valueField: "forecastSpend", label: "预测支出", color: "#d94a45", dash: "9 7" },
      { valueField: "cumulativeVolatility", label: "累计高支出波动", color: "#199b55" },
    ],
  });
}

function renderCostModelShare(items) {
  const target = el("costModelShare");
  if (!target) return;
  const series = Array.isArray(items) ? items : [];
  if (selectedCostModelSeries && !series.some((item) => item.model === selectedCostModelSeries)) closeCostModelShareModal();
  if (!series.length) {
    target.innerHTML = '<div class="model-empty">当前筛选范围暂无 API 模型成本</div>';
    return;
  }
  const rows = series.map((item, index) => {
    const share = Math.max(0, Math.min(1, Number(item.share || 0)));
    const opportunity = Number(item.optimizationSpace || 0);
    const opportunityLabel = opportunity > 0 ? `波动 ${observabilityMoney(opportunity)}` : "支出平稳";
    return `<div class="bar-row" role="button" tabindex="0" data-cost-model-series="${escapeHtml(item.model || "")}" aria-label="查看 ${escapeHtml(item.model || "模型系列")} 高支出波动与每日支出">${rankingBadge(index)}<strong>${escapeHtml(item.model || "未知模型")}</strong><div class="bar-track" aria-hidden="true"><div class="bar-fill" style="width:${Math.max(share ? 3 : 0, share * 100)}%"></div></div><span class="num"><strong class="cost-model-opportunity ${opportunity > 0 ? "is-positive" : "is-none"}">${opportunityLabel}</strong><small>支出 ${observabilityMoney(item.spend)} · ${(share * 100).toFixed(1)}%</small></span></div>`;
  }).join("");
  target.innerHTML = `<div class="cost-model-share-head" aria-hidden="true"><span>排名</span><span>模型系列</span><span>占比</span><span>高支出波动</span></div><div class="cost-model-share-rows${series.length <= 4 ? " is-compact" : ""}">${rows}</div>`;
  if (selectedCostModelSeries) openCostModelShareModal(series.find((item) => item.model === selectedCostModelSeries));
}

function openCostModelShareModal(item, returnFocus = document.activeElement) {
  if (!item) return;
  selectedCostModelSeries = item.model || "";
  costModelShareReturnFocus = returnFocus;
  el("costModelShareModalTitle").textContent = `${item.model || "未知模型"}成本详情`;
  const opportunity = Number(item.optimizationSpace || 0);
  el("costModelShareModalSubtitle").textContent = `所选范围支出 ${observabilityMoney(item.spend)} · 占比 ${(Number(item.share || 0) * 100).toFixed(1)}%${opportunity > 0 ? ` · 高支出波动 ${observabilityMoney(opportunity)}` : " · 支出平稳"}`;
  el("costModelShareModalBody").innerHTML = renderCostModelShareDetail(item);
  const modal = el("costModelShareModal");
  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
  el("costModelShareModalClose")?.focus();
  const chart = el("costModelShareChart");
  if (chart) renderLineChart({
    svg: chart,
    points: item.daily || [],
    valueField: "spend",
    color: "#0673d2",
    fill: "rgba(6,115,210,.13)",
    axisFormatter: observabilityMoney,
    tooltipRows: (point) => [{ label: "支出", value: observabilityMoney(point.spend) }, ...(Number(point.opportunity || 0) > 0 ? [{ label: "高支出波动", value: observabilityMoney(point.opportunity) }] : [])],
  });
}

function closeCostModelShareModal() {
  selectedCostModelSeries = "";
  const modal = el("costModelShareModal");
  modal?.classList.add("hidden");
  modal?.setAttribute("aria-hidden", "true");
  costModelShareReturnFocus?.focus?.();
  costModelShareReturnFocus = null;
}

function renderCostModelShareDetail(item) {
  if (!item) return "";
  const daily = Array.isArray(item.daily) ? item.daily : [];
  return `<section class="cost-model-share-detail" aria-label="${escapeHtml(item.model || "模型系列")}每日支出与高支出波动"><div class="cost-model-share-detail-head"><div><h4>${escapeHtml(item.model || "未知模型")}每日支出与高支出波动</h4><p>点击日期可查看当天费用明细；高支出波动表示高于该模型典型日支出的金额，仅用于定位值得排查的高峰。</p></div></div><svg id="costModelShareChart" class="cost-model-share-chart" role="img" aria-label="${escapeHtml(item.model || "模型系列")}每日支出与高支出波动折线图"></svg><div class="cost-model-share-daily">${daily.map((point) => { const opportunity = Number(point.opportunity || 0); return `<button class="cost-model-share-daily-row" type="button" data-cost-model-series-day="${escapeHtml(point.date || "")}" data-cost-model-series-name="${escapeHtml(item.model || "")}"><span>${escapeHtml(point.date || "")}</span><span class="daily-values"><strong>支出 ${observabilityMoney(point.spend)}</strong><small class="opportunity ${opportunity > 0 ? "is-positive" : "is-none"}">${opportunity > 0 ? `波动 ${observabilityMoney(opportunity)}` : "支出平稳"}</small></span></button>`; }).join("")}</div></section>`;
}

function normalizeWorkbenchList(payload, ...keys) {
  const data = payload?.data ?? payload ?? {};
  for (const key of keys) {
    if (Array.isArray(data?.[key])) return data[key];
  }
  if (Array.isArray(data)) return data;
  return [];
}

function renderGovernanceSavings() {
  const target = el("savingsActionList");
  if (!target) return;
  const measurements = governanceWorkbenchData.savingsMeasurements || [];
  const legacyActions = costOverview?.data?.savingsActions || [];
  const rows = measurements.length ? measurements : legacyActions;
  target.innerHTML = rows.map((item) => {
    const reviewed = Boolean(item.reviewedAt || item.financeReviewedAt || item.financeReviewer || item.status === "verified");
    const evidence = item.evidenceUrl || item.evidenceLink;
    const realized = item.savingsUsd ?? item.realizedSavings ?? item.realizedSavingsToDate;
    return `<article class="observability-action"><div><strong>${escapeHtml(item.name || item.actionName || item.scope || "节省核验")}</strong><p class="hint">${escapeHtml(item.owner || item.financeReviewer || "未指定复核人")} · ${evidence ? "有证明" : "缺证明"} · ${reviewed ? "已复核" : "待复核"}${item.measurementWindow ? ` · ${escapeHtml(item.measurementWindow)}` : ""}</p></div><div class="observability-table-actions"><span>${realized == null ? "尚未计入正式节省" : observabilityMoney(realized)}</span>${measurements.length && canReconcileCosts() ? `<button class="ghost-btn" type="button" data-edit-savings-measurement="${escapeHtml(item.id || "")}">编辑</button>` : !measurements.length && canManageCosts() ? `<button class="ghost-btn" type="button" data-edit-savings-action="${escapeHtml(item.id || "")}">编辑</button>` : ""}</div></article>`;
  }).join("") || observabilityEmptyState("暂无降本核验", "提交证据并完成财务复核后，节省金额才会进入主看板。", []);
}

function renderGovernanceWorkbench() {
  const canCost = canManageCosts() || canReconcileCosts();
  const allowedTabs = new Set(canCost ? ["actual-ledger", "plans", "savings"] : []);
  if (!allowedTabs.has(governanceWorkbenchTab)) governanceWorkbenchTab = [...allowedTabs][0] || "actual-ledger";
  document.querySelectorAll("[data-governance-tab]").forEach((button) => {
    const allowed = allowedTabs.has(button.dataset.governanceTab);
    button.classList.toggle("hidden", !allowed);
    button.setAttribute("aria-selected", String(allowed && button.dataset.governanceTab === governanceWorkbenchTab));
    button.tabIndex = allowed && button.dataset.governanceTab === governanceWorkbenchTab ? 0 : -1;
  });
  document.querySelectorAll("[data-governance-panel]").forEach((panel) => panel.classList.toggle("hidden", panel.dataset.governancePanel !== governanceWorkbenchTab || !allowedTabs.has(panel.dataset.governancePanel)));
  const badge = el("governancePermissionBadge");
  if (badge) badge.textContent = "费用治理";
  const status = el("governanceWorkbenchStatus");
  if (status) {
    status.classList.toggle("hidden", !governanceWorkbenchLoading && !governanceWorkbenchLoadError);
    status.classList.toggle("danger", Boolean(governanceWorkbenchLoadError));
    status.setAttribute("role", governanceWorkbenchLoadError ? "alert" : "status");
    status.setAttribute("aria-live", governanceWorkbenchLoadError ? "assertive" : "polite");
    status.innerHTML = governanceWorkbenchLoadError
      ? `<div><strong>治理数据加载不完整</strong><p>${escapeHtml(governanceWorkbenchLoadError)}</p></div><button class="ghost-btn" type="button" data-governance-retry>重新加载</button>`
      : governanceWorkbenchLoading ? `<div><strong>正在加载治理工作台</strong><p>正在读取计划与节省证明。</p></div>` : "";
  }
  if (costOverview) renderCostOverview();
  renderGovernanceSavings();
}

async function optionalGovernanceEndpoint(path, keys) {
  try {
    const payload = await api(path);
    return normalizeWorkbenchList(payload, ...keys);
  } catch (error) {
    if ([404, 405].includes(Number(error?.status))) return [];
    throw error;
  }
}

async function loadGovernanceWorkbench(force = false) {
  if (!(canManageCosts() || canReconcileCosts())) return;
  if (governanceWorkbenchLoading && !force) return;
  governanceWorkbenchLoading = true;
  governanceWorkbenchLoadError = "";
  renderGovernanceWorkbench();
  const errors = [];
  const load = async (name, promise) => {
    try { return await promise; } catch (error) { errors.push(`${name}：${error.message || "加载失败"}`); return []; }
  };
  const [planVersions, savingsMeasurements] = await Promise.all([
    canManageCosts() ? load("计划版本", optionalGovernanceEndpoint(`/api/admin/costs/plan-versions?year=${encodeURIComponent(currentCostMonth().slice(0, 4))}`, ["items", "planVersions", "versions"])) : [],
    canReconcileCosts() || canManageCosts() ? load("节省证明", optionalGovernanceEndpoint("/api/admin/costs/savings-measurements", ["items", "measurements"])) : [],
  ]);
  governanceWorkbenchData = { planVersions, savingsMeasurements };
  governanceWorkbenchLoading = false;
  governanceWorkbenchLoadError = errors.join("；");
  renderGovernanceWorkbench();
}

function openGovernanceWorkbench(tab) {
  governanceWorkbenchTab = tab || governanceWorkbenchTab;
  switchView("governance-workbench");
  renderGovernanceWorkbench();
  if (!governanceWorkbenchLoading && !(governanceWorkbenchData.planVersions.length || governanceWorkbenchData.savingsMeasurements.length)) loadGovernanceWorkbench();
}

async function loadCostOverview(forceRefresh = false) {
  if (!canViewCosts()) return;
  const requestId = ++costOverviewRequestId;
  costOverviewController?.abort();
  costOverviewController = new AbortController();
  isCostOverviewLoading = true;
  costOverviewLoadError = "";
  try {
    const { startDate, endDate } = currentCostWindow();
    const category = el("costCategory")?.value || "";
    const costBucket = el("costBucket")?.value || "";
    const model = el("costModel")?.value || "";
    const vendor = el("costVendor")?.value || "";
    const provider = el("costProvider")?.value || "";
    const accountId = el("costAccount")?.value || "";
    const reconciliationStatus = el("costReconciliation")?.value || "";
    const recognitionStatus = el("costRecognition")?.value || "";
    const asOf = new Date().toISOString().slice(0, 10);
    const query = `start_date=${encodeURIComponent(startDate)}&end_date=${encodeURIComponent(endDate)}&as_of=${encodeURIComponent(asOf)}&category=${encodeURIComponent(category)}&cost_bucket=${encodeURIComponent(costBucket)}&model=${encodeURIComponent(model)}&vendor=${encodeURIComponent(vendor)}&provider=${encodeURIComponent(provider)}&account_id=${encodeURIComponent(accountId)}&reconciliation_status=${encodeURIComponent(reconciliationStatus)}&recognition_status=${encodeURIComponent(recognitionStatus)}${forceRefresh ? "&refresh=1" : ""}`;
    const nextOverview = await api(`/api/admin/costs/overview?${query}`, { signal: costOverviewController.signal });
    if (requestId !== costOverviewRequestId) return;
    costOverview = nextOverview;
    costBudgets = Array.isArray(nextOverview?.data?.budgets) ? nextOverview.data.budgets : costBudgets;
  } catch (error) {
    if (error.name !== "AbortError" && requestId === costOverviewRequestId) {
      costOverviewLoadError = error.message || "费用看板加载失败";
      showToast(costOverviewLoadError);
    }
  } finally {
    if (requestId === costOverviewRequestId) {
      isCostOverviewLoading = false;
      renderCostOverview();
      if (currentView === "governance-workbench") renderGovernanceWorkbench();
    }
  }
}

function focusDrawer(drawerId, returnFocus = document.activeElement) {
  const drawer = el(drawerId);
  if (!drawer) return;
  drawer.classList.remove("hidden");
  drawer.setAttribute("aria-hidden", "false");
  const backdrop = el(drawerId === "stabilityDrawer" ? "stabilityDrawerBackdrop" : "costDetailDrawerBackdrop");
  backdrop?.classList.remove("hidden");
  backdrop?.setAttribute("aria-hidden", "false");
  if (returnFocus) {
    if (drawerId === "stabilityDrawer") stabilityDrawerReturnFocus = returnFocus;
    if (drawerId === "costDetailDrawer") costDrawerReturnFocus = returnFocus;
  }
  const first = drawer.querySelector("button, [href], input, select, textarea");
  first?.focus();
}

function closeStabilityDrawer() {
  const drawer = el("stabilityDrawer");
  if (!drawer) return;
  drawer.classList.add("hidden");
  drawer.setAttribute("aria-hidden", "true");
  el("stabilityDrawerBackdrop")?.classList.add("hidden");
  el("stabilityDrawerBackdrop")?.setAttribute("aria-hidden", "true");
  stabilityDrawerReturnFocus?.focus?.();
  stabilityDrawerReturnFocus = null;
}

function setStabilityDrawerMode(mode) {
  const drawer = el("stabilityDrawer");
  if (!drawer) return;
  const detailMode = mode === "detail";
  drawer.dataset.stabilityDrawerMode = detailMode ? "detail" : "samples";
  el("stabilityDrawerBackToSamples")?.classList.toggle("hidden", !detailMode);
}

function updateStabilityScenarioTitle(filters) {
  const parts = [filters.scenario || "全部异常"];
  if (filters.model) parts.push(filters.model);
  el("stabilityDrawerTitle").textContent = `场景样本 · ${parts.join(" · ")}`;
}

function renderStabilityScenarioModelFilter() {
  const select = el("stabilityScenarioModel");
  if (!select) return;
  const options = stabilityScenarioState.modelOptions || [];
  const selected = stabilityScenarioState.filters.model || "";
  const names = options.map((item) => String(item.name || "")).filter((name) => name);
  if (selected && !names.includes(selected)) names.unshift(selected);
  select.innerHTML = `<option value="">全部模型</option>${names.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("")}`;
  select.value = names.includes(selected) ? selected : "";
}

function renderStabilityScenarioSamples() {
  const list = el("stabilityRequestList");
  if (!list) return;
  if (stabilityScenarioState.loading) {
    list.innerHTML = '<p class="empty" aria-live="polite">正在加载样本…</p>';
    return;
  }
  if (stabilityScenarioState.error) {
    list.innerHTML = `<div class="operational-status danger"><div><strong>样本加载失败</strong><p>${escapeHtml(stabilityScenarioState.error)}</p></div><button class="ghost-btn" type="button" data-stability-scenario-retry>重试</button></div>`;
    return;
  }
  const items = stabilityScenarioState.items || [];
  const pageCount = Math.max(1, Math.ceil(stabilityScenarioState.total / stabilityScenarioState.pageSize));
  list.innerHTML = `${items.length ? `<div class="observability-sample-list">${items.map((item) => `<button class="observability-sample-row" type="button" data-stability-request="${escapeHtml(item.requestId || "")}" data-stability-backend="${escapeHtml(item.backendId || "")}"><span><strong>${escapeHtml(item.requestId || "暂无 ID")}</strong><small>${escapeHtml(item.eventTime || "暂无时间")} · ${escapeHtml(item.model || "-")}</small></span><span class="observability-sample-state"><strong>${escapeHtml(item.status || "unknown")}</strong><span>${item.userVisibleFailure == null ? "最终失败暂无" : item.userVisibleFailure ? "最终失败" : "已成功"}</span></span></button>`).join("")}</div>` : '<p class="empty">当前筛选没有样本</p>'}<div class="observability-pagination"><button class="ghost-btn" type="button" data-stability-page="prev" ${stabilityScenarioState.page <= 1 ? "disabled" : ""}>上一页</button><span>第 ${stabilityScenarioState.page} / ${pageCount} 页 · 共 ${stabilityScenarioState.total} 条</span><button class="ghost-btn" type="button" data-stability-page="next" ${stabilityScenarioState.page >= pageCount ? "disabled" : ""}>下一页</button></div>`;
}

async function loadStabilityScenarioSamples(filters = stabilityScenarioState.filters, page = 1) {
  if (!canViewStability()) return;
  const requestId = ++stabilityScenarioRequestId;
  stabilityScenarioState = { ...stabilityScenarioState, filters, page, loading: true, error: "" };
  renderStabilityScenarioSamples();
  const { startDate, endDate } = currentStabilityWindow();
  try {
    const query = new URLSearchParams({ start_date: startDate, end_date: endDate, model: filters.model || "", scenario: filters.scenario || "", error_code: filters.errorCode || "", page: String(page), page_size: String(stabilityScenarioState.pageSize) });
    const cacheKey = `stability-scenarios:${query.toString()}`;
    const payload = observabilityDetailCache.get(cacheKey) || await api(`/api/admin/stability/scenarios?${query.toString()}`);
    observabilityDetailCache.set(cacheKey, payload);
    if (requestId !== stabilityScenarioRequestId) return;
    const data = payload.data || {};
    stabilityScenarioState = { ...stabilityScenarioState, items: data.items || [], total: Number(data.total || 0), page: Number(data.page || page), modelOptions: data.modelOptions || [], loading: false, error: "" };
    renderStabilityScenarioModelFilter();
    renderStabilityScenarioSamples();
  } catch (error) {
    if (requestId !== stabilityScenarioRequestId) return;
    stabilityScenarioState = { ...stabilityScenarioState, loading: false, error: error.message || "样本加载失败" };
    renderStabilityScenarioSamples();
  }
}

function openStabilityScenario(button) {
  const filters = { model: button.dataset.stabilityModel || "", scenario: button.dataset.stabilityScenario || "", errorCode: button.dataset.stabilityErrorCode || "" };
  updateStabilityScenarioTitle(filters);
  renderStabilityScenarioModelFilter();
  el("stabilityRequestDetail").innerHTML = '<p class="empty">选择左侧样本查看请求元数据。</p>';
  el("stabilityAttemptTimeline").innerHTML = "";
  setStabilityDrawerMode("samples");
  focusDrawer("stabilityDrawer", button);
  loadStabilityScenarioSamples(filters, 1);
}

async function openStabilityRequest(requestId, backendId = "") {
  try {
    setStabilityDrawerMode("detail");
    const suffix = backendId ? `?backend_id=${encodeURIComponent(backendId)}` : "";
    const cacheKey = `stability-request:${requestId}:${backendId}`;
    const payload = observabilityDetailCache.get(cacheKey) || await api(`/api/admin/stability/requests/${encodeURIComponent(requestId)}${suffix}`);
    observabilityDetailCache.set(cacheKey, payload);
    const detail = payload.data || {};
    const request = detail.request || detail.finalRequest || detail;
    const labels = { request_id: "请求 ID", requestId: "请求 ID", event_time: "请求时间", eventTime: "请求时间", model: "模型", requestedModelGroup: "请求模型组", provider: "来源", model_group: "模型组", modelGroup: "模型组", model_id: "模型线路", modelId: "模型线路", status: "最终状态", error_code: "错误码", errorCode: "错误码", error_class: "错误分类", errorClass: "错误分类", error_message: "脱敏错误信息", errorMessage: "脱敏错误信息", scenario: "异常场景", request_duration_ms: "请求时长（毫秒）", requestDurationMs: "请求时长（毫秒）", ttft_ms: "TTFT（毫秒）", ttftMs: "TTFT（毫秒）", prompt_tokens: "输入 Token", promptTokens: "输入 Token", completion_tokens: "输出 Token", completionTokens: "输出 Token", total_tokens: "总 Token", totalTokens: "总 Token", attempted_retries: "重试次数", attemptedRetries: "重试次数", max_retries: "最大重试次数", maxRetries: "最大重试次数", trace_id: "调用链标识", traceId: "调用链标识", user_visible_failure: "用户最终失败", finalRequestFailure: "用户最终失败", finalRequestFailureSource: "最终失败口径", organization_id: "组织归属", team_id: "团队归属", principal_id: "归属主体", collected_at: "采集时间", collectedAt: "采集时间" };
    el("stabilityRequestDetail").innerHTML = Object.entries(request).filter(([key]) => labels[key]).map(([key, value]) => `<div class="observability-drawer-row"><dt>${labels[key]}</dt><dd>${escapeHtml(value == null || value === "" ? "暂无数据" : String(value))}</dd></div>`).join("");
    const attempts = detail.attempts || detail.timeline || request.attempts || [];
    const timeline = el("stabilityAttemptTimeline");
    if (timeline) timeline.innerHTML = attempts.length ? `<div class="observability-detail-section"><h4>尝试时间线</h4><div class="observability-timeline">${attempts.map((item, index) => {
      const status = String(item.status || item.eventType || "unknown").toLowerCase();
      const failure = status.includes("fail") || status.includes("error");
      const label = item.eventType || item.type || (index === 0 ? "首次尝试" : item.fallbackFrom || item.fallbackTo ? "兜底尝试" : "重试");
      return `<div class="observability-timeline-item ${failure ? "failure" : ""}"><span class="observability-timeline-dot" aria-hidden="true"></span><div class="observability-timeline-copy"><strong>${escapeHtml(label)} · ${escapeHtml(item.actualModel || item.model || item.route || "未知线路")}</strong><small>${escapeHtml(item.startedAt || item.startTime || item.eventTime || "暂无时间")} · ${escapeHtml(item.provider || "来源未知")} · ${escapeHtml(item.status || "未知状态")}${item.errorCode ? ` · ${escapeHtml(item.errorCode)}` : ""}${item.ttftMs == null ? "" : ` · TTFT ${escapeHtml(String(item.ttftMs))}ms`}</small></div></div>`;
    }).join("")}</div></div>` : observabilityEmptyState("尝试时间线暂不可用", "历史消费日志只保留最终请求事实，不会伪造重试或兜底链路。", []);
    focusDrawer("stabilityDrawer");
  } catch (error) {
    showToast(error.message || "请求样本加载失败");
  }
}

function setCostDrawerMode(mode) {
  const drawer = el("costDetailDrawer");
  if (!drawer) return;
  const detailMode = mode === "detail";
  drawer.dataset.costDrawerMode = detailMode ? "detail" : "ledger";
  el("costDetailDrawerBackToLedger")?.classList.toggle("hidden", !detailMode);
}

function openCostLedger(filters = {}, returnFocus = document.activeElement) {
  if (!canViewCosts()) return;
  const normalizedFilters = {
    ...(filters.startDate ? { start_date: filters.startDate } : {}),
    ...(filters.endDate ? { end_date: filters.endDate } : {}),
    ...(filters.category ? { category: filters.category } : {}),
    ...(filters.costBucket ? { cost_bucket: filters.costBucket } : {}),
    ...(filters.model ? { model: filters.model } : {}),
    ...(filters.canonicalModel ? { canonical_model: filters.canonicalModel } : {}),
    ...(filters.vendor ? { vendor: filters.vendor } : {}),
    ...(filters.provider ? { provider: filters.provider } : {}),
    ...(filters.accountId ? { account_id: filters.accountId } : {}),
    ...(filters.reconciliationStatus ? { reconciliation_status: filters.reconciliationStatus } : {}),
    ...(filters.recognitionStatus ? { recognition_status: filters.recognitionStatus } : {}),
  };
  costLedgerState = { ...costLedgerState, filters: normalizedFilters, page: 1, selectedId: "" };
  el("costDetailDrawerTitle").textContent = filters.costBucket ? `费用明细 · ${filters.costBucket}` : filters.canonicalModel ? `费用明细 · ${filters.canonicalModel}` : filters.model ? `费用明细 · ${filters.model}` : "费用明细";
  el("costLedgerDetail").innerHTML = '<p class="empty">选择左侧账本行查看来源与对账信息。</p>';
  setCostDrawerMode("ledger");
  focusDrawer("costDetailDrawer", returnFocus);
  loadCostLedger();
}

function currentCostLedgerFilters(overrides = {}) {
  return {
    category: el("costCategory")?.value || "",
    costBucket: el("costBucket")?.value || "",
    vendor: el("costVendor")?.value || "",
    provider: el("costProvider")?.value || "",
    accountId: el("costAccount")?.value || "",
    reconciliationStatus: el("costReconciliation")?.value || "",
    recognitionStatus: el("costRecognition")?.value || "",
    ...overrides,
  };
}

async function loadCostLedger() {
  const requestId = ++costLedgerRequestId;
  costLedgerState = { ...costLedgerState, loading: true, error: "" };
  const list = el("costLedgerList");
  if (list) list.innerHTML = '<p class="empty">正在加载费用明细…</p>';
  try {
    const { startDate, endDate } = currentCostWindow();
    const asOf = costOverview?.data?.asOf || costOverview?.asOf || new Date().toISOString().slice(0, 10);
    const query = new URLSearchParams({ start_date: startDate, end_date: endDate, as_of: asOf, page: String(costLedgerState.page), page_size: String(costLedgerState.pageSize), ...(costLedgerState.filters || {}) });
    const cacheKey = `cost-ledger:${query.toString()}`;
    const payload = observabilityDetailCache.get(cacheKey) || await api(`/api/admin/costs/ledger?${query.toString()}`);
    observabilityDetailCache.set(cacheKey, payload);
    if (requestId !== costLedgerRequestId) return;
    const data = payload.data || {};
    costLedgerState = { ...costLedgerState, items: data.items || [], total: Number(data.total || 0), page: Number(data.page || costLedgerState.page), loading: false };
    renderCostLedger();
  } catch (error) {
    if (requestId !== costLedgerRequestId) return;
    costLedgerState = { ...costLedgerState, loading: false, error: error.message || "费用明细加载失败" };
    if (list) list.innerHTML = `<div class="operational-status danger"><div><strong>费用明细加载失败</strong><p>${escapeHtml(costLedgerState.error)}</p></div><button class="ghost-btn" type="button" data-cost-ledger-retry>重试</button></div>`;
  }
}

function renderCostLedger() {
  const list = el("costLedgerList");
  if (!list) return;
  const items = costLedgerState.items || [];
  const pageCount = Math.max(1, Math.ceil(costLedgerState.total / costLedgerState.pageSize));
  list.innerHTML = `${items.length ? items.map((item) => `<button class="observability-ledger-row" type="button" data-cost-ledger-id="${escapeHtml(item.id || item.requestId || "")}"><span><strong>${escapeHtml(item.name || item.model || item.costBucket || item.requestId || "成本明细")}</strong><small>${escapeHtml(item.date || "暂无日期")} · ${escapeHtml(item.sourceType || "来源未知")} · ${escapeHtml(item.provider || item.vendor || "暂无渠道")}</small></span><span class="observability-ledger-state"><strong>${observabilityMoney(item.amountUsd)}</strong><span>${escapeHtml(item.reconciliationStatus || "未对账")}</span></span></button>`).join("") : '<p class="empty">当前筛选没有费用明细</p>'}<div class="observability-pagination"><button class="ghost-btn" type="button" data-cost-ledger-page="prev" ${costLedgerState.page <= 1 ? "disabled" : ""}>上一页</button><span>第 ${costLedgerState.page} / ${pageCount} 页 · 共 ${costLedgerState.total} 条</span><button class="ghost-btn" type="button" data-cost-ledger-page="next" ${costLedgerState.page >= pageCount ? "disabled" : ""}>下一页</button></div>`;
}

function showCostLedgerDetail(itemId) {
  const item = (costLedgerState.items || []).find((value) => String(value.id || value.requestId) === String(itemId));
  if (!item) return;
  setCostDrawerMode("detail");
  const labels = { date: "日期", asOf: "截止日", sourceType: "来源类型", costBucket: "成本桶", category: "类别", name: "名称", provider: "供应渠道", vendor: "供应商", model: "模型", accountId: "账号标识", accountName: "账号", amountUsd: "金额（USD）", currency: "原币种", amount: "原币金额", originalAmount: "原币金额", exchangeRate: "汇率快照", exchangeRateSnapshot: "汇率快照", financeBucket: "财务分类", voucherNo: "凭证号", invoiceNo: "发票号", evidenceUrl: "证明链接", planVersion: "计划版本", planVersionId: "计划版本", scenario: "计划情景", reconciliationStatus: "对账状态", recognitionStatus: "确认状态", requestId: "请求标识", coverage: "覆盖质量" };
  el("costLedgerDetail").innerHTML = Object.entries(item).filter(([key]) => labels[key]).map(([key, value]) => `<div class="observability-drawer-row"><dt>${labels[key]}</dt><dd>${escapeHtml(value == null || value === "" ? "暂无数据" : typeof value === "object" ? JSON.stringify(value) : String(value))}</dd></div>`).join("");
}

function closeCostItemModal() {
  el("costItemModal")?.classList.add("hidden");
  el("costItemForm")?.reset();
  el("costItemId").value = "";
}

function openCostItemModal(item = null) {
  const value = item || {};
  el("costItemId").value = value.id || "";
  el("costItemModalTitle").textContent = value.id ? "编辑成本项" : "新增成本项";
  el("costItemName").value = value.name || "";
  el("costItemCategory").value = value.category || "";
  el("costItemVendor").value = value.vendor || "";
  el("costItemModel").value = value.model || "";
  el("costItemBusinessScope").value = value.businessScope || "";
  el("costItemFinanceBucket").value = value.financeBucket || "";
  const bucketSelect = el("costItemCostBucket");
  const bucketValue = value.costBucket || "other";
  bucketSelect.value = [...bucketSelect.options].some((option) => option.value === bucketValue) ? bucketValue : "other";
  el("costItemProvider").value = value.provider || "";
  el("costItemAccountId").value = value.accountId || "";
  el("costItemAccountName").value = value.accountName || "";
  el("costItemVoucherNo").value = value.voucherNo || "";
  el("costItemInvoiceNo").value = value.invoiceNo || "";
  el("costItemReconciliationStatus").value = value.reconciliationStatus || "unreconciled";
  el("costItemRecognitionStatus").value = value.recognitionStatus || "actual";
  const planSelect = el("costItemPlanVersionId");
  const planVersions = governanceWorkbenchData.planVersions || [];
  planSelect.innerHTML = `<option value="">不属于计划</option>${planVersions.map((item) => `<option value="${escapeHtml(item.id || "")}">${escapeHtml(item.version || item.name || item.id || "计划版本")}</option>`).join("")}`;
  planSelect.value = value.planVersionId || "";
  el("costItemScenario").value = value.scenario || "";
  el("costItemSourceEvidence").value = value.sourceEvidence || "";
  el("costItemAmount").value = value.amount ?? "";
  el("costItemCurrency").value = value.currency || "USD";
  el("costItemExchangeRate").value = value.exchangeRate ?? "7.3";
  el("costItemEnabled").value = String(value.enabled !== false);
  el("costItemStartDate").value = value.serviceStartDate || new Date().toISOString().slice(0, 10);
  el("costItemEndDate").value = value.serviceEndDate || new Date().toISOString().slice(0, 10);
  el("costItemNotes").value = value.notes || "";
  el("costItemModal")?.classList.remove("hidden");
}

function closeSavingsActionModal() {
  el("savingsActionModal")?.classList.add("hidden");
  el("savingsActionForm")?.reset();
  el("savingsActionId").value = "";
}

function openSavingsActionModal(item = null) {
  const value = item || {};
  el("savingsActionId").value = value.id || "";
  el("savingsActionModalTitle").textContent = value.id ? "编辑降本动作" : "新增降本动作";
  el("savingsActionName").value = value.name || "";
  el("savingsActionOwner").value = value.owner || "";
  el("savingsBaselineDailyCost").value = value.baselineDailyCost ?? "";
  el("savingsImplementedDate").value = value.implementedDate || new Date().toISOString().slice(0, 10);
  el("savingsActionStatus").value = value.status || "planned";
  el("savingsVerifiedDate").value = value.verifiedDate || "";
  el("savingsVerifiedDailyCost").value = value.verifiedDailyCost ?? "";
  el("savingsExpectedDailyCost").value = value.expectedDailyCost ?? "";
  el("savingsExpectedStartDate").value = value.expectedStartDate || "";
  el("savingsProvider").value = value.provider || "";
  el("savingsModel").value = value.model || "";
  el("savingsCostBucket").value = value.costBucket || "";
  el("savingsEvidenceUrl").value = value.evidenceUrl || "";
  el("savingsFinanceReviewer").value = value.financeReviewer || "";
  el("savingsActionNotes").value = value.notes || "";
  el("savingsActionModal")?.classList.remove("hidden");
}

function closeGovernanceModal(id, formId) {
  el(id)?.classList.add("hidden");
  el(formId)?.reset();
}

function openCostPlanModal(item = null) {
  const value = item || {};
  el("costPlanId").value = value.id || "";
  el("costPlanModalTitle").textContent = value.id ? "编辑计划草稿" : "新增计划草稿";
  el("costPlanYear").value = value.year || currentCostMonth().slice(0, 4);
  el("costPlanVersion").value = value.version || value.name || "";
  el("costPlanScenario").value = value.scenario || "baseline";
  el("costPlanAsOf").value = value.asOf || new Date().toISOString().slice(0, 10);
  el("costPlanCoverageComplete").value = String(value.coverageComplete || value.coverageStatus === "complete");
  el("costPlanNotes").value = value.notes || "";
  el("costPlanModal")?.classList.remove("hidden");
}

function openSavingsMeasurementModal(item = null) {
  const value = item || {};
  el("savingsMeasurementId").value = value.id || "";
  el("savingsMeasurementModalTitle").textContent = value.id ? "编辑降本核验" : "录入降本核验";
  el("savingsMeasurementActionId").value = value.actionId || "";
  el("savingsMeasurementScope").value = value.scope || value.scopeKey || "";
  el("savingsMeasurementProvider").value = value.provider || "";
  el("savingsMeasurementModel").value = value.model || "";
  el("savingsMeasurementBucket").value = value.costBucket || "";
  el("savingsMeasurementBaselineStart").value = value.baselineStart || value.baselineStartDate || "";
  el("savingsMeasurementBaselineEnd").value = value.baselineEnd || value.baselineEndDate || "";
  el("savingsMeasurementStart").value = value.measurementStart || value.measurementStartDate || "";
  el("savingsMeasurementEnd").value = value.measurementEnd || value.measurementEndDate || "";
  el("savingsMeasurementBaselineAmount").value = value.baselineAmountUsd ?? "";
  el("savingsMeasurementActualAmount").value = value.actualAmountUsd ?? "";
  el("savingsMeasurementEvidence").value = value.evidenceUrl || "";
  el("savingsMeasurementReviewer").value = value.financeReviewer || "";
  el("savingsMeasurementReviewedAt").value = value.reviewedAt ? String(value.reviewedAt).slice(0, 16) : "";
  el("savingsMeasurementStatus").value = value.status || "pending_evidence";
  el("savingsMeasurementNotes").value = value.notes || "";
  el("savingsMeasurementModal")?.classList.remove("hidden");
}

async function saveCostPlan(event) {
  event.preventDefault();
  const id = el("costPlanId").value;
  const body = { year: Number(el("costPlanYear").value), version: el("costPlanVersion").value.trim(), scenario: el("costPlanScenario").value, asOf: el("costPlanAsOf").value, status: "draft", coverageComplete: el("costPlanCoverageComplete").value === "true", notes: el("costPlanNotes").value.trim() };
  try { await ensureCsrfToken(); await api(id ? `/api/admin/costs/plan-versions/${encodeURIComponent(id)}` : "/api/admin/costs/plan-versions", { method: id ? "PATCH" : "POST", body: JSON.stringify(body) }); closeGovernanceModal("costPlanModal", "costPlanForm"); await loadGovernanceWorkbench(true); await loadCostOverview(); showToast("计划草稿已保存"); } catch (error) { showToast(error.message || "计划草稿保存失败"); }
}

async function changeCostPlanState(planId, operation) {
  const copy = { approve: "批准", activate: "激活", archive: "归档" }[operation] || operation;
  try { await ensureCsrfToken(); await api(`/api/admin/costs/plan-versions/${encodeURIComponent(planId)}/${operation}`, { method: "POST", body: JSON.stringify({}) }); await loadGovernanceWorkbench(true); await loadCostOverview(); showToast(`计划已${copy}`); } catch (error) { showToast(error.message || `计划${copy}失败`); }
}

async function saveSavingsMeasurement(event) {
  event.preventDefault();
  const id = el("savingsMeasurementId").value;
  const reviewedAt = el("savingsMeasurementReviewedAt").value;
  const body = { actionId: el("savingsMeasurementActionId").value.trim(), scope: el("savingsMeasurementScope").value.trim(), provider: el("savingsMeasurementProvider").value.trim(), model: el("savingsMeasurementModel").value.trim(), costBucket: el("savingsMeasurementBucket").value.trim(), baselineStart: el("savingsMeasurementBaselineStart").value, baselineEnd: el("savingsMeasurementBaselineEnd").value, measurementStart: el("savingsMeasurementStart").value, measurementEnd: el("savingsMeasurementEnd").value, baselineAmountUsd: Number(el("savingsMeasurementBaselineAmount").value), actualAmountUsd: Number(el("savingsMeasurementActualAmount").value), evidenceUrl: el("savingsMeasurementEvidence").value.trim(), financeReviewer: el("savingsMeasurementReviewer").value.trim(), reviewedAt: reviewedAt ? new Date(reviewedAt).toISOString() : null, status: el("savingsMeasurementStatus").value, notes: el("savingsMeasurementNotes").value.trim() };
  try { await ensureCsrfToken(); await api(id ? `/api/admin/costs/savings-measurements/${encodeURIComponent(id)}` : "/api/admin/costs/savings-measurements", { method: id ? "PATCH" : "POST", body: JSON.stringify(body) }); closeGovernanceModal("savingsMeasurementModal", "savingsMeasurementForm"); await loadGovernanceWorkbench(true); await loadCostOverview(); showToast("降本核验已保存"); } catch (error) { showToast(error.message || "降本核验保存失败"); }
}

async function saveCostItem(event) {
  if (!canManageCosts()) return;
  event.preventDefault();
  const id = el("costItemId").value;
  const body = { category: el("costItemCategory").value.trim(), name: el("costItemName").value.trim(), vendor: el("costItemVendor").value.trim(), model: el("costItemModel").value.trim(), businessScope: el("costItemBusinessScope").value.trim(), amount: Number(el("costItemAmount").value), currency: el("costItemCurrency").value, exchangeRate: Number(el("costItemExchangeRate").value), serviceStartDate: el("costItemStartDate").value, serviceEndDate: el("costItemEndDate").value, financeBucket: el("costItemFinanceBucket").value.trim(), costBucket: el("costItemCostBucket").value, provider: el("costItemProvider").value.trim(), accountId: el("costItemAccountId").value.trim(), accountName: el("costItemAccountName").value.trim(), voucherNo: el("costItemVoucherNo").value.trim(), invoiceNo: el("costItemInvoiceNo").value.trim(), reconciliationStatus: el("costItemReconciliationStatus").value, recognitionStatus: el("costItemRecognitionStatus").value, planVersionId: el("costItemPlanVersionId").value, scenario: el("costItemScenario").value.trim(), sourceEvidence: el("costItemSourceEvidence").value.trim(), notes: el("costItemNotes").value.trim(), enabled: el("costItemEnabled").value === "true" };
  try {
    await ensureCsrfToken();
    await api(id ? `/api/admin/costs/items/${encodeURIComponent(id)}` : "/api/admin/costs/items", { method: id ? "PATCH" : "POST", body: JSON.stringify(body) });
    closeCostItemModal();
    await loadCostOverview();
    showToast("成本项已保存");
  } catch (error) { showToast(error.message || "成本项保存失败"); }
}

async function saveSavingsAction(event) {
  if (!canManageCosts()) return;
  event.preventDefault();
  const id = el("savingsActionId").value;
  const body = { name: el("savingsActionName").value.trim(), owner: el("savingsActionOwner").value.trim(), baselineDailyCost: Number(el("savingsBaselineDailyCost").value), implementedDate: el("savingsImplementedDate").value, status: el("savingsActionStatus").value, verifiedDate: el("savingsVerifiedDate").value || null, verifiedDailyCost: el("savingsVerifiedDailyCost").value === "" ? null : Number(el("savingsVerifiedDailyCost").value), expectedDailyCost: el("savingsExpectedDailyCost").value === "" ? null : Number(el("savingsExpectedDailyCost").value), expectedStartDate: el("savingsExpectedStartDate").value || null, provider: el("savingsProvider").value.trim(), model: el("savingsModel").value.trim(), costBucket: el("savingsCostBucket").value.trim(), evidenceUrl: el("savingsEvidenceUrl").value.trim(), financeReviewer: el("savingsFinanceReviewer").value.trim(), notes: el("savingsActionNotes").value.trim() };
  try {
    await ensureCsrfToken();
    await api(id ? `/api/admin/costs/savings-actions/${encodeURIComponent(id)}` : "/api/admin/costs/savings-actions", { method: id ? "PATCH" : "POST", body: JSON.stringify(body) });
    closeSavingsActionModal();
    await loadCostOverview();
    showToast("降本动作已保存");
  } catch (error) { showToast(error.message || "降本动作保存失败"); }
}

async function saveCostBudget(event) {
  if (!canManageCosts()) return;
  event.preventDefault();
  const month = currentCostMonth();
  try {
    await ensureCsrfToken();
    await api(`/api/admin/costs/budgets/${encodeURIComponent(month)}`, { method: "PUT", body: JSON.stringify({ budgetUsd: Number(el("costBudgetAmount").value), dailyTargetUsd: Number(el("costDailyTarget").value) }) });
    await loadCostOverview();
    showToast("预算已保存");
  } catch (error) { showToast(error.message || "预算保存失败"); }
}

function loadDashboardData(forceRefresh = false) {
  if (!currentUser) return Promise.resolve();
  if (accountAccessCopy(currentUser)) {
    renderAccountAccessState();
    return Promise.resolve();
  }
  const { startDate, endDate } = selectedDateRange();
  const source = el("sourceSelect").value;
  const queryKey = `${startDate}|${endDate}|${source}`;
  if (dashboardInFlight && dashboardRequestKey === queryKey) return dashboardInFlight;
  dashboardRequestController?.abort();
  const controller = new AbortController();
  dashboardRequestController = controller;
  dashboardRequestKey = queryKey;
  const requestId = ++dashboardRequestId;
  isDashboardLoading = true;
  renderPersonal();
  const request = (async () => {
    try {
      const payload = await api(
        `/api/me/usage?start_date=${encodeURIComponent(startDate)}&end_date=${encodeURIComponent(endDate)}&source=${encodeURIComponent(source)}${forceRefresh ? "&refresh=1" : ""}`,
        { signal: controller.signal },
      );
      if (requestId !== dashboardRequestId || dashboardRequestKey !== queryKey) return;
      usageData = payload.rows || [];
      usageSummary = payload.summary || null;
      personalDataFreshness = payload.dataFreshness || null;
      personalDataQuality = payload.dataQuality || null;
      personalCoverage = payload.coverage || null;
      lastPersonalUsageCacheHit = Boolean(payload.cache?.hit);
    } catch (error) {
      if (error.name !== "AbortError" && requestId === dashboardRequestId) {
        showToast(error.message || "用量数据加载失败");
      }
    } finally {
      if (dashboardInFlight === request) dashboardInFlight = null;
      if (requestId !== dashboardRequestId) return;
      isDashboardLoading = false;
      renderPersonal();
    }
  })();
  dashboardInFlight = request;
  return request;
}

function loadAdminData(forceRefresh = false) {
  const scopeKey = organizationUsageScopeKey();
  if (!canViewAdminUsage()) return Promise.resolve();
  const { startDate, endDate } = selectedDateRange();
  const source = el("sourceSelect").value;
  const search = el("adminEmployeeSearch").value.trim();
  const employee = selectedAdminEmployee || search;
  const query = new URLSearchParams({ start_date: startDate, end_date: endDate, source });
  if (employee) query.set("employee", employee);
  if (forceRefresh) query.set("refresh", "1");
  const scope = organizationUsageScope();
  const usagePath = scope?.usagePath || "/api/admin/usage";
  const queryKey = `${scopeKey}|${usagePath}|${query.toString()}`;
  if (adminUsageInFlight && adminUsageQueryKey === queryKey) return adminUsageInFlight;
  adminUsageRequestController?.abort();
  const controller = new AbortController();
  adminUsageRequestController = controller;
  adminUsageQueryKey = queryKey;
  const requestId = ++adminUsageRequestId;
  isAdminLoading = true;
  adminUsageLoadingScopeKey = scopeKey;
  renderAdmin();
  const request = (async () => {
    try {
      const payload = await api(`${usagePath}?${query.toString()}`, { signal: controller.signal });
      if (requestId !== adminUsageRequestId || scopeKey !== organizationUsageScopeKey()) return;
      adminUsageData = payload.rows || [];
      adminSummaryData = payload.summaryRows || adminUsageData;
      adminEmployees = payload.employees || [];
      adminDataFreshness = payload.dataFreshness || null;
      adminDataQuality = payload.dataQuality || null;
      adminCoverage = payload.coverage || null;
      adminUsageScopeKey = scopeKey;
      lastAdminUsageCacheHit = Boolean(payload.cache?.hit);
      if (payload.truncated) {
        el("adminLimitHint").textContent = `${RANKING_SORT_TIP}；日志读取达到上限（已读 ${payload.pagesRead || 0}/${payload.totalPages || "?"} 页），员工排行可能不完整`;
      } else {
        el("adminLimitHint").textContent = `${RANKING_SORT_TIP}；已读取 ${payload.pagesRead || 0} 页日志，按当前筛选范围统计`;
      }
    } catch (error) {
      if (error.name !== "AbortError" && requestId === adminUsageRequestId && scopeKey === organizationUsageScopeKey()) {
        showToast(error.message || "全员数据加载失败");
      }
    } finally {
      if (adminUsageInFlight === request) adminUsageInFlight = null;
      if (requestId !== adminUsageRequestId) return;
      isAdminLoading = false;
      adminUsageLoadingScopeKey = "";
      renderAdmin();
    }
  })();
  adminUsageInFlight = request;
  return request;
}

function loadDepartmentData(forceRefresh = false) {
  const scopeKey = organizationUsageScopeKey();
  if (!canViewDepartmentUsage()) return Promise.resolve();
  const { startDate, endDate } = selectedDateRange();
  const source = el("sourceSelect").value;
  const search = el("departmentEmployeeSearch").value.trim();
  const department = selectedDepartment || search;
  const query = new URLSearchParams({ start_date: startDate, end_date: endDate, source });
  if (department) query.set("department", department);
  if (forceRefresh) query.set("refresh", "1");
  const scope = organizationUsageScope();
  const usagePath = scope?.departmentsUsagePath || "/api/admin/departments/usage";
  const queryKey = `${scopeKey}|${usagePath}|${query.toString()}`;
  if (departmentUsageInFlight && departmentUsageQueryKey === queryKey) return departmentUsageInFlight;
  departmentUsageRequestController?.abort();
  const controller = new AbortController();
  departmentUsageRequestController = controller;
  departmentUsageQueryKey = queryKey;
  const requestId = ++departmentUsageRequestId;
  isDepartmentLoading = true;
  departmentUsageLoadingScopeKey = scopeKey;
  renderDepartment();
  const request = (async () => {
    try {
      const payload = await api(`${usagePath}?${query.toString()}`, { signal: controller.signal });
      if (requestId !== departmentUsageRequestId || scopeKey !== organizationUsageScopeKey()) return;
      departmentUsageData = payload.rows || [];
      departmentSummaryData = payload.summaryRows || departmentUsageData;
      departmentRankings = payload.departments || [];
      departmentEmployees = payload.employees || [];
      if (selectedDepartmentEmployee) {
        const matchedEmployee = departmentEmployees.find((item) => employeeMatchesIdentity(item, selectedDepartmentEmployeeSnapshot));
        if (matchedEmployee) {
          selectedDepartmentEmployee = matchedEmployee.employeeEmail || matchedEmployee.employeeId;
          selectedDepartmentEmployeeSnapshot = { ...matchedEmployee, userIds: [...(matchedEmployee.userIds || [])] };
        }
      }
      departmentDataFreshness = payload.dataFreshness || null;
      departmentDataQuality = payload.dataQuality || null;
      departmentCoverage = payload.coverage || null;
      departmentUsageScopeKey = scopeKey;
      departmentPickerOptions = payload.departmentOptions || (department ? departmentPickerOptions : departmentRankings);
      lastDepartmentUsageCacheHit = Boolean(payload.cache?.hit);
      const rankingSubject = selectedDepartment ? "员工排行" : "部门排行";
      if (payload.truncated) {
        el("departmentLimitHint").textContent = `${rankingSubject}${RANKING_SORT_TIP}；日志读取达到上限（已读 ${payload.pagesRead || 0}/${payload.totalPages || "?"} 页），排行可能不完整`;
      } else {
        el("departmentLimitHint").textContent = `${rankingSubject}${RANKING_SORT_TIP}；已读取 ${payload.pagesRead || 0} 页日志，按当前筛选范围统计`;
      }
    } catch (error) {
      if (error.name !== "AbortError" && requestId === departmentUsageRequestId && scopeKey === organizationUsageScopeKey()) {
        showToast(error.message || "部门数据加载失败");
      }
    } finally {
      if (departmentUsageInFlight === request) departmentUsageInFlight = null;
      if (requestId !== departmentUsageRequestId) return;
      isDepartmentLoading = false;
      departmentUsageLoadingScopeKey = "";
      renderDepartment();
    }
  })();
  departmentUsageInFlight = request;
  return request;
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
  const cacheKey = `${organizationUsageScopeKey()}|${selectedTeamRef}|${startDate}|${endDate}|${source}`;
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
  const preserveCurrentData = Boolean(forceRefresh || usageAutoRefreshPromise);
  if (teamUsageRequestController) teamUsageRequestController.abort();
  if (teamRankingRequestController) teamRankingRequestController.abort();
  teamRankingRequestId += 1;
  isTeamRankingLoading = false;
  teamUsageRequestController = new AbortController();
  if (!preserveCurrentData) {
    teamUsageData = [];
    teamSummaryData = [];
    teamEmployees = [];
    teamDataQuality = null;
    teamCoverage = null;
  }
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
    include_member_rankings: "true",
  });
  if (selectedTeamRef) query.set("team_ref", selectedTeamRef);
  if (forceRefresh) query.set("refresh", "1");
  const cacheKey = `${organizationUsageScopeKey()}|${selectedTeamRef}|${startDate}|${endDate}|${source}`;
  const cached = !forceRefresh ? teamUsagePayloadCache.get(cacheKey) : null;

  if (cached) {
    applyTeamUsagePayload(cached, cacheKey);
    setTeamRankingHint(cached);
    isTeamLoading = false;
    renderTeam();
    return;
  }

  try {
    const payload = await api(`/api/team/usage?${query.toString()}`, { signal: teamUsageRequestController.signal });
    if (requestId !== teamUsageRequestId) return;
    applyTeamUsagePayload(payload, cacheKey);
    setTeamRankingHint(payload);
  } catch (error) {
    if (error.name === "AbortError") return;
    if (requestId !== teamUsageRequestId) return;
    showToast(error.message || "团队数据加载失败");
  } finally {
    if (requestId === teamUsageRequestId) {
      isTeamLoading = false;
      teamUsageRequestController = null;
      renderTeam();
    }
  }
}

function loadTeamMemberData(employee, forceRefresh = false, scrollToCard = true) {
  if (!currentUser?.isTeamLeader || !leaderTeams.length) return Promise.resolve();
  ensureSelectedTeamRef();
  const keepFilters = forceRefresh && selectedTeamEmployee === employee;
  selectedTeamEmployee = employee;
  const { startDate, endDate } = selectedDateRange();
  const source = el("sourceSelect").value;
  const query = new URLSearchParams({ start_date: startDate, end_date: endDate, source, employee });
  if (selectedTeamRef) query.set("team_ref", selectedTeamRef);
  if (forceRefresh) query.set("refresh", "1");
  const queryKey = `${organizationUsageScopeKey()}|${selectedTeamRef}|${query.toString()}`;
  if (teamMemberUsageInFlight && teamMemberUsageQueryKey === queryKey) return teamMemberUsageInFlight;
  teamMemberUsageRequestController?.abort();
  const controller = new AbortController();
  teamMemberUsageRequestController = controller;
  teamMemberUsageQueryKey = queryKey;
  const requestId = ++teamMemberUsageRequestId;
  if (!keepFilters) teamMemberUsageFilters = { date: "all", model: "all", status: "all", keyword: "" };
  isTeamMemberLoading = true;
  updateTeamMemberLoadingLabels();
  renderTeam();
  if (scrollToCard) scrollToDetailCard("teamDetailCard");
  const request = (async () => {
    try {
      const payload = await api(`/api/team/member/usage?${query.toString()}`, { signal: controller.signal });
      if (requestId !== teamMemberUsageRequestId || teamMemberUsageQueryKey !== queryKey) return;
      teamMemberUsageData = payload.rows || [];
      teamMemberUsageSummary = payload.summary || null;
      teamDataFreshness = payload.dataFreshness || null;
      teamMemberDataQuality = payload.dataQuality || null;
      teamMemberCoverage = payload.coverage || null;
      const employeePayload = payload.employee || {};
      const employeeId = employeePayload.employeeEmail || employeePayload.employeeId || employee;
      if (employeeId && employeeId !== selectedTeamEmployee) selectedTeamEmployee = employeeId;
    } catch (error) {
      if (error.name !== "AbortError" && requestId === teamMemberUsageRequestId) {
        showToast(error.message || "成员用量明细加载失败");
      }
    } finally {
      if (teamMemberUsageInFlight === request) teamMemberUsageInFlight = null;
      if (requestId === teamMemberUsageRequestId) {
        isTeamMemberLoading = false;
        teamMemberUsageRequestController = null;
        renderTeam();
      }
    }
  })();
  teamMemberUsageInFlight = request;
  return request;
}

function clearTeamMemberSelection() {
  resetTeamMemberSelection();
  renderTeam();
}

async function loadModels({ silent = false } = {}) {
  // Customer identities intentionally use their tenant-scoped model grants.
  // Do not make a forbidden request merely because an older navigation event
  // tries to prefetch the global page.
  if (isOrganizationCustomerIdentity()) {
    modelCatalog = [];
    setupModelFilters();
    renderModels();
    return;
  }
  // 引导期的预取与切到模型广场时的按需加载可能撞在一起，复用同一个在途请求，
  // 避免对上游目录接口发两次。
  if (!modelCatalogRequest) {
    modelCatalogRequest = (async () => {
      try {
        const payload = await api("/api/models");
        modelCatalog = payload.models || [];
        return null;
      } catch (error) {
        modelCatalog = [];
        return error;
      } finally {
        modelCatalogRequest = null;
        setupModelFilters();
        renderModels();
      }
    })();
  }
  const error = await modelCatalogRequest;
  // 报错与否按调用方决定，而不是按发起预取时的身份：后台预取静默失败，
  // 但用户已经打开模型广场时，同一个在途请求失败仍要给出提示。
  if (error && !silent) showToast(error.message || "模型列表加载失败");
}

async function showApp(user) {
  const previousUser = currentUser;
  const nextUser = normalizeAuthUser(user);
  const previousKeyIdentity = personalKeyCacheIdentity(previousUser);
  const nextKeyIdentity = personalKeyCacheIdentity(nextUser);
  if (previousKeyIdentity && previousKeyIdentity !== nextKeyIdentity) {
    clearPersonalKeyCache(previousUser);
    authSessionGeneration += 1;
    personalKeys = [];
    availableKeyModels = [];
    unrestrictedKeyModels = false;
    hasLoadedPersonalKeys = false;
    personalKeysLoadedAt = 0;
    isKeysLoading = false;
    keyLoadError = "";
    keyRefreshError = "";
    keyListRequest = null;
    keyRefreshRequest = null;
    clearRevealedKeys();
    clearPlainKey();
  }
  clearResetPasswordToken();
  currentUser = nextUser;
  syncOrganizationDemoChrome();
  if (user?.csrfToken) authCsrfToken = user.csrfToken;
  if (currentUser?.csrfToken) authCsrfToken = currentUser.csrfToken;
  leaderTeams = normalizeLeaderTeams(currentUser);
  selectedTeamRef = currentUser.team?.teamRef || leaderTeams[0]?.teamRef || "";
  selectedTeamKeyRef = selectedTeamRef;
  resetTeamMemberSelection();
  ensureSelectedTeamRef();
  el("authLoadingView").classList.add("hidden");
  el("landingView").classList.add("hidden");
  el("loginView").classList.add("hidden");
  el("appView").classList.remove("hidden");
  // 先清掉上一身份，再用 /api/auth/me 已知字段立即生成本次导航。
  resetNavigationToPending();
  revealNavigation();
  const organizationName = String(currentUser.organizationName || currentUser.organization?.name || "").trim();
  const displayIdentifier = authDisplayIdentifier();
  el("userEmail").textContent = organizationName && currentUser.organizationRole
    ? `${displayIdentifier} · ${organizationName}`
    : displayIdentifier;
  el("userName").textContent = currentUser.name || displayIdentifier;
  el("avatar").textContent = currentUser.avatar || initials(authContactEmail(), currentUser.name || displayIdentifier);
  el("teamWelcomeTitle").textContent = `所选范围 · ${teamScopeLabel()}`;
  el("departmentWelcomeTitle").textContent = "所选范围 · 全部部门";
  switchView("dashboard");
  render();
  const isCustomer = isOrganizationCustomerIdentity();
  if (isCustomer) {
    // Customer identities use only their tenant-safe usage and enterprise
    // credit APIs. In particular, never probe the personal billing endpoint.
    billingAvailable = false;
    modelCatalog = [];
    // auth/me 已经给出客户身份的企业权限；scope 在后台补充兼容字段。
    const scopePromise = loadAuthScope();
    await Promise.all([loadCurrentViewData(), scopePromise]);
    return;
  }
  // 团队负责人范围在 /api/auth/scope 里异步补齐；基础导航已经可用。
  // 余额与订单仍留给充值页按需加载，不在引导路径上等它。
  const scopePromise = loadAuthScope();
  if (accountAccessCopy(currentUser)) {
    // 权限受限的新用户照样要看到充值入口——他们正是靠充值开通。
    await scopePromise;
    return;
  }
  // 模型目录只在进入模型广场时加载，避免登录首屏产生无关的上游请求。
  await Promise.all([loadCurrentViewData(), scopePromise]);
  prefetchPersonalKeys();
}

async function loadAuthScope() {
  try {
    const scope = await api("/api/auth/scope");
    Object.assign(currentUser, scope);
    syncOrganizationDemoChrome();
    leaderTeams = normalizeLeaderTeams(currentUser);
    selectedTeamRef = currentUser.team?.teamRef || leaderTeams[0]?.teamRef || "";
    // 充值入口的可见性由这里的零成本判断给出；老后端没有该字段时退回按需探测，
    // 避免混合版本部署期间入口凭空消失。
    if (scope?.billingAvailable !== undefined) {
      billingAvailable = Boolean(scope.billingAvailable);
    }
    el("teamWelcomeTitle").textContent = `所选范围 · ${teamScopeLabel()}`;
    // 补齐团队与充值入口；不会阻塞已经显示的基础导航。
    revealNavigation();
    // isAdmin 到这里才确定，充值管理面板的可见性随之更新。
    renderAdminBilling();
    renderAccountAccessState();
    render();
    if (scope?.billingAvailable === undefined && !isOrganizationCustomerIdentity()) {
      await refreshBillingAvailability();
    }
  } catch (error) {
    // 权限拿不到也必须放开导航，否则用户会一直卡在骨架态、连个人用量都点不到。
    revealNavigation();
    showToast("部分权限信息加载失败，请刷新重试");
  }
}

function showLogin() {
  const previousUser = currentUser;
  clearPersonalKeyCache(previousUser);
  currentUser = null;
  authSessionGeneration += 1;
  stabilityOverviewRequestId += 1;
  costOverviewRequestId += 1;
  stabilityScenarioRequestId += 1;
  costLedgerRequestId += 1;
  stabilityOverviewController?.abort();
  costOverviewController?.abort();
  stabilityOverviewController = null;
  costOverviewController = null;
  stabilityDrawerReturnFocus = null;
  costDrawerReturnFocus = null;
  stabilityOverview = null;
  costOverview = null;
  costBudgets = [];
  governanceWorkbenchData = { planVersions: [], savingsMeasurements: [] };
  governanceWorkbenchLoading = false;
  governanceWorkbenchLoadError = "";
  governanceWorkbenchTab = "actual-ledger";
  stabilityLoadError = "";
  costOverviewLoadError = "";
  isStabilityLoading = false;
  isCostOverviewLoading = false;
  stabilityScenarioState = { page: 1, pageSize: 20, total: 0, items: [], filters: { model: "", scenario: "", errorCode: "" }, loading: false, error: "" };
  costLedgerState = { page: 1, pageSize: 20, total: 0, items: [], filters: {}, loading: false, error: "", selectedId: "" };
  el("stabilityDrawer")?.classList.add("hidden");
  el("stabilityDrawer")?.setAttribute("aria-hidden", "true");
  el("stabilityDrawerBackdrop")?.classList.add("hidden");
  el("costDetailDrawer")?.classList.add("hidden");
  el("costDetailDrawer")?.setAttribute("aria-hidden", "true");
  el("costDetailDrawerBackdrop")?.classList.add("hidden");
  closeCostItemModal();
  closeSavingsActionModal();
  authCsrfToken = "";
  csrfRefreshPromise = null;
  isSsoRedirecting = false;
  selectedAdminEmployee = "";
  selectedDepartment = "";
  resetDepartmentEmployeeSelection();
  departmentPickerOpen = false;
  usageData = [];
  usageSummary = null;
  personalDataQuality = null;
  personalCoverage = null;
  dashboardRequestController?.abort();
  dashboardRequestId += 1;
  dashboardRequestController = null;
  dashboardRequestKey = "";
  dashboardInFlight = null;
  isDashboardLoading = false;
  adminUsageRequestController?.abort();
  adminUsageData = [];
  adminSummaryData = [];
  adminEmployees = [];
  adminDataQuality = null;
  adminCoverage = null;
  adminUsageScopeKey = "";
  adminUsageLoadingScopeKey = "";
  adminUsageRequestId += 1;
  adminUsageRequestController = null;
  adminUsageQueryKey = "";
  adminUsageInFlight = null;
  isAdminLoading = false;
  departmentUsageRequestController?.abort();
  departmentUsageData = [];
  departmentSummaryData = [];
  departmentRankings = [];
  departmentEmployees = [];
  departmentDataQuality = null;
  departmentCoverage = null;
  departmentUsageScopeKey = "";
  departmentUsageLoadingScopeKey = "";
  departmentUsageRequestId += 1;
  departmentUsageRequestController = null;
  departmentUsageQueryKey = "";
  departmentUsageInFlight = null;
  isDepartmentLoading = false;
  teamUsageData = [];
  teamSummaryData = [];
  teamEmployees = [];
  teamDataQuality = null;
  teamCoverage = null;
  teamMemberUsageData = [];
  teamMemberUsageSummary = null;
  teamMemberDataQuality = null;
  teamMemberCoverage = null;
  // 换账号时清空充值状态，避免上一个账号的余额与订单残留在页面上。
  stopTopupPolling();
  billingConfig = null;
  billingAccount = null;
  billingOrders = [];
  billingOrderTotal = 0;
  billingRequest = null;
  billingLoadedAt = 0;
  billingAvailable = false;
  billingLoadError = "";
  selectedTopupAmount = 0;
  pendingTopupTradeNo = "";
  el("billingPayPanel")?.classList.add("hidden");
  resetOrganizationBillingData();
  resetOrganizationTokenData();
  resetTeamKeyState();
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
  organizationLoadError = "";
  organizationMemberLoadError = "";
  organizationDataRequestId += 1;
  organizationMemberRequestId += 1;
  isOrganizationDepartmentSaving = false;
  isOrganizationMemberSaving = false;
  editingOrganizationDepartmentId = "";
  editingOrganizationMemberId = "";
  customerOrganizations = [];
  customerOrganizationsTotal = 0;
  customerOrganizationsLoadError = "";
  customerOrganizationsPage = 1;
  customerOrganizationsFilters = { search: "", status: "" };
  selectedCustomerOrganization = null;
  customerOrganizationDetailTab = "info";
  editingCustomerOrganizationId = "";
  resetOrganizationClaims();
  resetOrganizationAdoption();
  window.clearTimeout(customerOrganizationsSearchTimer);
  // 导航退回骨架态，下一个登录身份不会先看到上一个身份的可见项。
  resetNavigationToPending();
  el("organizationDepartmentModal").classList.add("hidden");
  el("organizationMemberModal").classList.add("hidden");
  el("customerOrganizationModal").classList.add("hidden");
  personalKeys = [];
  availableKeyModels = [];
  unrestrictedKeyModels = false;
  hasLoadedPersonalKeys = false;
  personalKeysLoadedAt = 0;
  isKeysLoading = false;
  keyLoadError = "";
  keyRefreshError = "";
  keyListRequest = null;
  keyRefreshRequest = null;
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
        identifier: values.loginIdentifier,
        // Keep email in the request during the rolling deploy; older servers
        // ignore identifier and continue to authenticate by email.
        email: values.loginIdentifier,
        password: values.loginPassword,
        turnstileToken: turnstileTokens.login || undefined,
      }),
    });
    await showApp(payload);
  } catch (error) {
    setAuthStatus(error.message || "登录失败，请检查邮箱或企业账号和密码。", "error");
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
  if (organizationClaimToken) return;
  if (organizationInvitationToken) return;
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
  const identifier = el("loginEmailInput").value.trim();
  el("forgotEmailInput").value = validEmail(identifier) ? identifier : "";
  setAuthMode("forgot");
});

el("organizationInvitationAcceptButton")?.addEventListener("click", acceptOrganizationInvitation);
el("organizationInvitationRetryButton")?.addEventListener("click", verifyOrganizationInvitation);
el("organizationInvitationLoginButton")?.addEventListener("click", finishOrganizationInvitationAndLogin);
el("organizationInvitationCancelButton")?.addEventListener("click", closeOrganizationInvitationScreen);
el("organizationClaimAcceptButton")?.addEventListener("click", acceptOrganizationClaim);
el("organizationClaimRetryButton")?.addEventListener("click", verifyOrganizationClaim);
el("organizationClaimLoginButton")?.addEventListener("click", finishOrganizationClaimAndLogin);
el("organizationClaimCancelButton")?.addEventListener("click", closeOrganizationClaimScreen);
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
el("resetCustomerOrganizationsDemoButton")?.addEventListener("click", resetCustomerOrganizationsDemo);
el("cancelCustomerOrganizationButton").addEventListener("click", closeCustomerOrganizationModal);
el("backToCustomersButton").addEventListener("click", closeCustomerOrganization);
el("createOrganizationDepartmentButton").addEventListener("click", () => openOrganizationDepartmentModal());
el("inviteOrganizationMemberButton").addEventListener("click", () => openOrganizationMemberModal());
el("cancelOrganizationDepartmentButton").addEventListener("click", closeOrganizationDepartmentModal);
el("cancelOrganizationMemberButton").addEventListener("click", closeOrganizationMemberModal);
el("closeOrganizationMemberIdentityButton")?.addEventListener("click", () => closeOrganizationMemberIdentityModal());
el("saveOrganizationMemberLoginNameButton")?.addEventListener("click", saveOrganizationMemberLoginName);
el("bindOrganizationMemberAccountButton")?.addEventListener("click", () => {
  bindOrganizationMemberAccount(el("organizationMemberAccountInput").value);
});
el("unbindOrganizationMemberAccountButton")?.addEventListener("click", () => bindOrganizationMemberAccount(""));
el("organizationMemberIdentityList")?.addEventListener("click", (event) => {
  const bindButton = event.target.closest("[data-organization-principal-bind]");
  if (bindButton) {
    bindOrganizationPrincipalMember(
      bindButton.dataset.organizationPrincipalBind,
      organizationMemberIdentityId,
    );
    return;
  }
  const unbindButton = event.target.closest("[data-organization-principal-unbind]");
  if (unbindButton) bindOrganizationPrincipalMember(unbindButton.dataset.organizationPrincipalUnbind, "");
});
el("openOrganizationTopupModalButton")?.addEventListener("click", openOrganizationTopupModal);
el("openOrganizationCreditAdjustmentButton")?.addEventListener("click", () => openOrganizationCreditAdjustmentModal("grant"));
el("openOrganizationCreditRevokeButton")?.addEventListener("click", () => openOrganizationCreditAdjustmentModal("revoke"));
el("cancelOrganizationTopupButton")?.addEventListener("click", closeOrganizationTopupModal);
el("cancelOrganizationCreditAdjustmentButton")?.addEventListener("click", closeOrganizationCreditAdjustmentModal);
el("organizationTopupForm")?.addEventListener("submit", submitOrganizationTopup);
el("organizationCreditAdjustmentForm")?.addEventListener("submit", submitOrganizationCreditAdjustment);
el("organizationTopupAmount")?.addEventListener("input", () => {
  selectedOrganizationTopupAmount = 0;
  renderOrganizationTopupOptions();
  setFieldError("organizationTopupError", "");
});
el("organizationTopupOptions")?.addEventListener("click", (event) => {
  const option = event.target.closest("[data-organization-topup-amount]");
  if (!option) return;
  selectedOrganizationTopupAmount = Number(option.dataset.organizationTopupAmount || 0);
  el("organizationTopupAmount").value = String(selectedOrganizationTopupAmount);
  renderOrganizationTopupOptions();
  setFieldError("organizationTopupError", "");
});
el("organizationBillingPreviousPageButton").addEventListener("click", () => changeOrganizationBillingPage(-1));
el("organizationBillingNextPageButton").addEventListener("click", () => changeOrganizationBillingPage(1));

el("customerOrganizationForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (isCustomerOrganizationSaving || !customerOrganizationsAvailable()) return;
  const name = el("customerOrganizationNameInput").value.trim();
  const adminName = el("customerOrganizationAdminNameInput").value.trim();
  const adminEmail = el("customerOrganizationAdminEmailInput").value.trim();
  if (!name || !adminName) {
    showToast("请填写企业名称和首位企业管理员姓名");
    return;
  }
  if (!validEmail(adminEmail)) {
    showToast("请输入有效的首位企业管理员邮箱");
    el("customerOrganizationAdminEmailInput").focus();
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
        body: JSON.stringify(isEditing ? { name } : { name, adminName, adminEmail }),
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

el("organizationClaimForm")?.addEventListener("submit", submitOrganizationClaim);
el("copyOrganizationClaimUrlButton")?.addEventListener("click", () => {
  if (organizationClaimLastUrl) copyText(organizationClaimLastUrl, "企业账号激活链接已复制");
});
el("organizationClaimTable")?.addEventListener("click", (event) => {
  const approveButton = event.target.closest("[data-organization-claim-approve]");
  if (approveButton) {
    mutateOrganizationClaim(approveButton.dataset.organizationClaimApprove, "approve");
    return;
  }
  const revokeButton = event.target.closest("[data-organization-claim-revoke]");
  if (revokeButton) mutateOrganizationClaim(revokeButton.dataset.organizationClaimRevoke, "revoke");
});
el("organizationAdoptionForm")?.addEventListener("submit", previewOrganizationAdoption);
el("applyOrganizationAdoptionButton")?.addEventListener("click", applyOrganizationAdoption);

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
  if (archiveButton) {
    archiveCustomerOrganization(archiveButton.dataset.customerOrganizationArchive);
    return;
  }
  const restoreButton = event.target.closest("[data-customer-organization-restore]");
  if (restoreButton) restoreCustomerOrganization(restoreButton.dataset.customerOrganizationRestore);
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
  const teamRole = el("organizationMemberTeamRoleInput").value;
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
      ? {
          name,
          departmentId,
          role,
          teamRole,
          ...(!isRealOrganizationMode() ? { status } : {}),
        }
      : { name, email, departmentId, role, teamRole };
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
  const identityButton = event.target.closest("[data-organization-member-identity]");
  if (identityButton) {
    openOrganizationMemberIdentityModal(identityButton.dataset.organizationMemberIdentity);
    return;
  }
  const statusButton = event.target.closest("[data-organization-member-status]");
  if (statusButton) {
    updateOrganizationMemberStatus(
      statusButton.dataset.organizationMemberStatus,
      statusButton.dataset.organizationMemberNextStatus,
    );
    return;
  }
  const resendButton = event.target.closest("[data-organization-member-invitation-resend]");
  if (resendButton) {
    resendOrganizationMemberInvitation(resendButton.dataset.organizationMemberInvitationResend);
    return;
  }
  const revokeButton = event.target.closest("[data-organization-member-invitation-revoke]");
  if (revokeButton) {
    revokeOrganizationMemberInvitation(revokeButton.dataset.organizationMemberInvitationRevoke);
    return;
  }
  const reinviteButton = event.target.closest("[data-organization-member-reinvite]");
  if (reinviteButton) {
    reinviteOrganizationMember(reinviteButton.dataset.organizationMemberReinvite);
    return;
  }
  const removeButton = event.target.closest("[data-organization-member-remove]");
  if (removeButton) removeOrganizationMember(removeButton.dataset.organizationMemberRemove);
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

document.querySelectorAll("[data-organization-usage-view]").forEach((button) => {
  button.addEventListener("click", () => showOrganizationUsage(button.dataset.organizationUsageView));
});

el("createOrganizationTokenButton").addEventListener("click", openOrganizationTokenModal);
el("retryOrganizationTokenCatalogButton")?.addEventListener("click", loadOrganizationTokens);
el("cancelOrganizationTokenButton").addEventListener("click", () => closeOrganizationTokenModal());
el("organizationTokenForm").addEventListener("submit", submitOrganizationToken);
el("closeOrganizationTokenSecret").addEventListener("click", closeOrganizationTokenSecretModal);
el("copyOrganizationTokenSecret").addEventListener("click", () => {
  const value = String(el("organizationTokenSecretValue")?.textContent || "");
  if (value) copyText(value, "完整令牌已复制");
});
el("cancelOrganizationTokenRevokeButton").addEventListener("click", () => closeOrganizationTokenRevokeModal());
el("confirmOrganizationTokenRevokeButton").addEventListener("click", confirmOrganizationTokenRevoke);
el("cancelOrganizationTokenDeleteButton").addEventListener("click", () => closeOrganizationTokenDeleteModal());
el("confirmOrganizationTokenDeleteButton").addEventListener("click", confirmOrganizationTokenDelete);

el("organizationTokenTable").addEventListener("click", (event) => {
  const revokeButton = event.target.closest("[data-organization-token-revoke]");
  if (revokeButton) {
    openOrganizationTokenRevokeModal(revokeButton.dataset.organizationTokenRevoke);
    return;
  }
  const deleteButton = event.target.closest("[data-organization-token-delete]");
  if (deleteButton) openOrganizationTokenDeleteModal(deleteButton.dataset.organizationTokenDelete);
});

el("organizationTokenSearch").addEventListener("input", () => {
  window.clearTimeout(organizationTokenSearchTimer);
  organizationTokenSearchTimer = window.setTimeout(() => {
    organizationTokenFilters.search = el("organizationTokenSearch").value.trim();
    organizationTokenPage = 1;
    loadOrganizationTokens();
  }, 260);
});

el("organizationTokenStatusFilter").addEventListener("change", () => {
  organizationTokenFilters.status = el("organizationTokenStatusFilter").value;
  organizationTokenPage = 1;
  loadOrganizationTokens();
});

el("resetOrganizationTokenFiltersButton").addEventListener("click", () => {
  organizationTokenFilters = { search: "", status: "" };
  organizationTokenPage = 1;
  renderOrganizationTokenFilters();
  loadOrganizationTokens();
});

el("organizationTokenPreviousPageButton").addEventListener("click", () => {
  if (organizationTokenPage <= 1) return;
  organizationTokenPage -= 1;
  loadOrganizationTokens();
});

el("organizationTokenNextPageButton").addEventListener("click", () => {
  if (organizationTokenPage * organizationTokenPageSize >= organizationTokenTotal) return;
  organizationTokenPage += 1;
  loadOrganizationTokens();
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

// 侧边栏永远是平台全局入口：下钻中的客户范围先退出，再进入目标页面，
// 否则「全员看板」会静默变成某一家客户的看板。
document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => {
  if (isViewingCustomerOrganization()) {
    clearCustomerOrganizationScope();
    syncNavigationVisibility();
  }
  switchView(button.dataset.view);
}));
el("stabilityRangeSelect")?.addEventListener("change", () => handleObservabilityRangeChange("stability"));
el("stabilityModel")?.addEventListener("change", () => {
  renderObservabilityFilterState("stability");
  loadStabilityOverview();
});
el("costRangeSelect")?.addEventListener("change", () => handleObservabilityRangeChange("cost"));
["stability", "cost"].forEach((scope) => {
  el(`${scope}RangeSelect`)?.addEventListener("mousedown", (event) => {
    const ids = observabilityRangeIds(scope);
    if (el(ids.select).value !== "custom") return;
    event.preventDefault();
    if (el(ids.panel).classList.contains("hidden")) openObservabilityRangePanel(scope);
    else closeObservabilityRangePanel(scope);
  });
  el(`${scope}RangeApply`)?.addEventListener("click", () => applyObservabilityCustomRange(scope));
  el(`${scope}RangeCancel`)?.addEventListener("click", () => closeObservabilityRangePanel(scope, true));
});
el("costCategory")?.addEventListener("change", () => {
  renderObservabilityFilterState("cost");
  loadCostOverview();
});
el("costModel")?.addEventListener("change", () => {
  renderObservabilityFilterState("cost");
  loadCostOverview();
});
el("costVendor")?.addEventListener("change", () => {
  renderObservabilityFilterState("cost");
  loadCostOverview();
});
["costBucket", "costProvider", "costAccount", "costReconciliation", "costRecognition"].forEach((id) => el(id)?.addEventListener("change", () => {
  renderObservabilityFilterState("cost");
  loadCostOverview();
}));
el("costFiltersButton")?.addEventListener("click", () => setObservabilityFilterPanel("cost", !costFiltersOpen));
el("stabilityResetFiltersButton")?.addEventListener("click", () => resetObservabilityFilters("stability"));
el("costResetFiltersButton")?.addEventListener("click", () => resetObservabilityFilters("cost"));
document.querySelectorAll(".observability-active-filters").forEach((container) => container.addEventListener("click", (event) => {
  const button = event.target.closest("[data-observability-clear]");
  if (button) clearObservabilityFilter(button.dataset.observabilityScope, button.dataset.observabilityClear);
}));
el("stabilityDrawerClose")?.addEventListener("click", closeStabilityDrawer);
el("stabilityDrawerBackToSamples")?.addEventListener("click", () => setStabilityDrawerMode("samples"));
el("stabilityScenarioModel")?.addEventListener("change", (event) => {
  const filters = { ...stabilityScenarioState.filters, model: event.target.value || "" };
  updateStabilityScenarioTitle(filters);
  loadStabilityScenarioSamples(filters, 1);
});
el("stabilityScenarioResetButton")?.addEventListener("click", () => {
  const filters = { model: "", scenario: "", errorCode: "" };
  updateStabilityScenarioTitle(filters);
  loadStabilityScenarioSamples(filters, 1);
});
el("stabilityScenarioRanking")?.addEventListener("click", (event) => {
  const scenarioButton = event.target.closest("[data-stability-scenario]");
  if (scenarioButton) {
    openStabilityScenario(scenarioButton);
    return;
  }
  const requestButton = event.target.closest("[data-stability-request]");
  if (requestButton) openStabilityRequest(requestButton.dataset.stabilityRequest, requestButton.dataset.stabilityBackend || "");
});
el("stabilityRequestList")?.addEventListener("click", (event) => {
  const retry = event.target.closest("[data-stability-scenario-retry]");
  if (retry) {
    loadStabilityScenarioSamples(stabilityScenarioState.filters, stabilityScenarioState.page);
    return;
  }
  const requestButton = event.target.closest("[data-stability-request]");
  if (requestButton) openStabilityRequest(requestButton.dataset.stabilityRequest, requestButton.dataset.stabilityBackend || "");
  const pageButton = event.target.closest("[data-stability-page]");
  if (pageButton) {
    const next = pageButton.dataset.stabilityPage === "next" ? stabilityScenarioState.page + 1 : stabilityScenarioState.page - 1;
    loadStabilityScenarioSamples(stabilityScenarioState.filters, next);
  }
});
el("stabilityRanking")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-stability-model]");
  if (button) openStabilityScenario({ dataset: { stabilityModel: button.dataset.stabilityModel, stabilityScenario: "", stabilityErrorCode: "" } });
});
el("addCostItemButton")?.addEventListener("click", () => openCostItemModal());
el("cancelCostItemButton")?.addEventListener("click", closeCostItemModal);
el("costItemForm")?.addEventListener("submit", saveCostItem);
el("addSavingsActionButton")?.addEventListener("click", () => openSavingsActionModal());
el("cancelSavingsActionButton")?.addEventListener("click", closeSavingsActionModal);
el("savingsActionForm")?.addEventListener("submit", saveSavingsAction);
el("costBudgetForm")?.addEventListener("submit", saveCostBudget);
el("costModelShare")?.addEventListener("click", (event) => {
  const row = event.target.closest("[data-cost-model-series]");
  if (!row) return;
  openCostModelShareModal((costOverview?.data?.modelCostShare || []).find((item) => item.model === row.dataset.costModelSeries), row);
});
el("costModelShare")?.addEventListener("keydown", (event) => {
  if (!['Enter', ' '].includes(event.key)) return;
  const row = event.target.closest("[data-cost-model-series]");
  if (!row) return;
  event.preventDefault();
  openCostModelShareModal((costOverview?.data?.modelCostShare || []).find((item) => item.model === row.dataset.costModelSeries), row);
});
el("costModelShareModalBody")?.addEventListener("click", (event) => {
  const day = event.target.closest("[data-cost-model-series-day]");
  if (!day) return;
  closeCostModelShareModal();
  openCostLedger(currentCostLedgerFilters({ canonicalModel: day.dataset.costModelSeriesName, startDate: day.dataset.costModelSeriesDay, endDate: day.dataset.costModelSeriesDay }), day);
});
el("mobileViewSelect")?.addEventListener("change", (event) => switchView(event.currentTarget.value));
el("governanceWorkbenchTabs")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-governance-tab]");
  if (!button) return;
  governanceWorkbenchTab = button.dataset.governanceTab;
  renderGovernanceWorkbench();
});
el("addCostPlanButton")?.addEventListener("click", () => openCostPlanModal());
el("cancelCostPlanButton")?.addEventListener("click", () => closeGovernanceModal("costPlanModal", "costPlanForm"));
el("costPlanForm")?.addEventListener("submit", saveCostPlan);
el("addSavingsMeasurementButton")?.addEventListener("click", () => openSavingsMeasurementModal());
el("cancelSavingsMeasurementButton")?.addEventListener("click", () => closeGovernanceModal("savingsMeasurementModal", "savingsMeasurementForm"));
el("savingsMeasurementForm")?.addEventListener("submit", saveSavingsMeasurement);
el("costPlanVersions")?.addEventListener("click", (event) => {
  const edit = event.target.closest("[data-edit-cost-plan]");
  if (edit) { openCostPlanModal((governanceWorkbenchData.planVersions || []).find((item) => String(item.id) === String(edit.dataset.editCostPlan))); return; }
  const state = event.target.closest("[data-cost-plan-state]");
  if (state) changeCostPlanState(state.dataset.costPlanId, state.dataset.costPlanState);
});
el("savingsActionList")?.addEventListener("click", (event) => {
  const measurement = event.target.closest("[data-edit-savings-measurement]");
  if (measurement) openSavingsMeasurementModal((governanceWorkbenchData.savingsMeasurements || []).find((item) => String(item.id) === String(measurement.dataset.editSavingsMeasurement)));
});
el("costItemPlanVersionId")?.addEventListener("focus", () => {
  const select = el("costItemPlanVersionId");
  if (!select || select.options.length > 1) return;
  select.innerHTML = `<option value="">不属于计划</option>${(governanceWorkbenchData.planVersions || []).map((item) => `<option value="${escapeHtml(item.id || "")}">${escapeHtml(item.version || item.name || item.id || "计划版本")}</option>`).join("")}`;
});
document.addEventListener("click", (event) => {
  const governanceButton = event.target.closest("[data-open-governance-tab]");
  if (governanceButton) {
    openGovernanceWorkbench(governanceButton.dataset.openGovernanceTab);
    return;
  }
  const emptyAction = event.target.closest("[data-observability-empty-action='filters']");
  if (emptyAction) {
    setObservabilityFilterPanel(emptyAction.dataset.observabilityScope, true);
    el(emptyAction.dataset.observabilityScope === "stability" ? "stabilityModel" : "costCategory")?.focus();
  }
  const metricAction = event.target.closest("[data-observability-action]");
  if (!metricAction) return;
  const action = metricAction.dataset.observabilityAction;
  if (action === "stability-final-failures") openStabilityScenario({ dataset: { stabilityModel: "", stabilityScenario: "", stabilityErrorCode: "" } });
  else if (action === "stability-ttft") el("stabilityTrend")?.scrollIntoView({ behavior: "smooth", block: "center" });
  else if (action === "stability-top-scenario") {
    const scenario = stabilityOverview?.data?.topScenarios?.[0];
    if (scenario) openStabilityScenario({ dataset: { stabilityModel: scenario.requestedModelGroup || scenario.model || "", stabilityScenario: scenario.scenario || "", stabilityErrorCode: scenario.errorCode || "" } });
  } else if (action === "cost-actual-ledger") openCostLedger({ recognitionStatus: "actual" }, metricAction);
  else if (action === "cost-plan-versions" || action === "cost-budget") openGovernanceWorkbench("plans");
  else if (action === "cost-savings") openGovernanceWorkbench("savings");
});
el("governanceWorkbenchStatus")?.addEventListener("click", (event) => {
  if (event.target.closest("[data-governance-retry]")) loadGovernanceWorkbench(true);
});
el("costLedgerList")?.addEventListener("click", (event) => {
  const retry = event.target.closest("[data-cost-ledger-retry]");
  if (retry) {
    loadCostLedger();
    return;
  }
  const pageButton = event.target.closest("[data-cost-ledger-page]");
  if (pageButton) {
    costLedgerState = { ...costLedgerState, page: pageButton.dataset.costLedgerPage === "next" ? costLedgerState.page + 1 : costLedgerState.page - 1 };
    loadCostLedger();
    return;
  }
  const row = event.target.closest("[data-cost-ledger-id]");
  if (row) showCostLedgerDetail(row.dataset.costLedgerId);
});
el("costDetailDrawerClose")?.addEventListener("click", () => {
  const drawer = el("costDetailDrawer");
  drawer?.classList.add("hidden");
  drawer?.setAttribute("aria-hidden", "true");
  el("costDetailDrawerBackdrop")?.classList.add("hidden");
  el("costDetailDrawerBackdrop")?.setAttribute("aria-hidden", "true");
  costDrawerReturnFocus?.focus?.();
  costDrawerReturnFocus = null;
});
el("costDetailDrawerBackToLedger")?.addEventListener("click", () => setCostDrawerMode("ledger"));
el("stabilityDrawerBackdrop")?.addEventListener("click", closeStabilityDrawer);
el("costDetailDrawerBackdrop")?.addEventListener("click", () => el("costDetailDrawerClose")?.click());
el("costItemBody")?.addEventListener("click", (event) => {
  const edit = event.target.closest("[data-edit-cost-item]");
  const remove = event.target.closest("[data-delete-cost-item]");
  const item = (costOverview?.data?.costItems || []).find((value) => value.id === (edit?.dataset.editCostItem || remove?.dataset.deleteCostItem));
  if (edit && item) openCostItemModal(item);
  if (remove && item) {
    if (!window.confirm("确认删除这个成本项吗？")) return;
    ensureCsrfToken().then(() => api(`/api/admin/costs/items/${encodeURIComponent(item.id)}`, { method: "DELETE" })).then(loadCostOverview).then(() => showToast("成本项已删除")).catch((error) => showToast(error.message || "成本项删除失败"));
  }
});
document.querySelectorAll("#stabilityQuality, #costQuality").forEach((container) => container.addEventListener("click", (event) => {
  const button = event.target.closest("[data-observability-retry]");
  if (!button) return;
  if (button.dataset.observabilityRetry === "stability") loadStabilityOverview();
  else if (costOverviewLoadError) loadCostOverview();
  else openCostLedger({}, button);
}));
el("savingsActionList")?.addEventListener("click", (event) => {
  const action = event.target.closest("[data-edit-savings-action]");
  if (!action) return;
  const item = (costOverview?.data?.savingsActions || []).find((value) => value.id === action.dataset.editSavingsAction);
  if (item) openSavingsActionModal(item);
});
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

function observabilityRangeState(scope) {
  return scope === "stability" ? stabilityCustomDateRange : costCustomDateRange;
}

function setObservabilityRangeState(scope, value) {
  if (scope === "stability") stabilityCustomDateRange = value;
  else costCustomDateRange = value;
}

function observabilityRangeIds(scope) {
  return { panel: `${scope}RangePanel`, select: `${scope}RangeSelect`, start: `${scope}RangeStart`, end: `${scope}RangeEnd`, hint: `${scope}RangeHint` };
}

function openObservabilityRangePanel(scope) {
  const ids = observabilityRangeIds(scope);
  const bounds = customRangeBounds();
  const current = observabilityRangeState(scope) || selectedObservabilityRange(scope);
  for (const id of [ids.start, ids.end]) {
    el(id).min = bounds.min;
    el(id).max = bounds.max;
  }
  el(ids.start).value = current.startDate;
  el(ids.end).value = current.endDate;
  el(ids.hint).textContent = `最多可查询最近 ${CUSTOM_RANGE_MAX_DAYS} 天，且不能选择未来日期。`;
  el(ids.hint).classList.remove("is-error");
  el(ids.panel).classList.remove("hidden");
}

function closeObservabilityRangePanel(scope, revert = false) {
  const ids = observabilityRangeIds(scope);
  el(ids.panel)?.classList.add("hidden");
  if (revert && !observabilityRangeState(scope)) el(ids.select).value = "7";
}

async function handleObservabilityRangeChange(scope) {
  const ids = observabilityRangeIds(scope);
  if (el(ids.select).value === "custom") {
    openObservabilityRangePanel(scope);
    return;
  }
  setObservabilityRangeState(scope, null);
  el(ids.select).querySelector('option[value="custom"]').textContent = "自定义…";
  closeObservabilityRangePanel(scope);
  if (scope === "stability") await loadStabilityOverview();
  else await loadCostOverview();
}

async function applyObservabilityCustomRange(scope) {
  const ids = observabilityRangeIds(scope);
  const startDate = el(ids.start).value;
  const endDate = el(ids.end).value;
  const error = customRangeError(startDate, endDate);
  if (error) {
    el(ids.hint).textContent = error.message;
    el(ids.hint).classList.add("is-error");
    showToast(error.message);
    return;
  }
  setObservabilityRangeState(scope, { startDate, endDate });
  el(ids.select).value = "custom";
  el(ids.select).querySelector('option[value="custom"]').textContent = `${startDate} ～ ${endDate}`;
  closeObservabilityRangePanel(scope);
  if (scope === "stability") await loadStabilityOverview();
  else await loadCostOverview();
}

function setCustomRangeHint(message, isError = false) {
  const hint = el("customRangeHint");
  if (!hint) return;
  hint.textContent = message;
  hint.classList.toggle("is-error", isError);
}

function resetCustomRangeValidation() {
  el("customRangeStart").removeAttribute("aria-invalid");
  el("customRangeEnd").removeAttribute("aria-invalid");
  setCustomRangeHint(`最多可查询最近 ${CUSTOM_RANGE_MAX_DAYS} 天，且不能选择未来日期。`);
}

function openCustomRangePanel() {
  const panel = el("customRangePanel");
  if (!panel) return;
  const bounds = customRangeBounds();
  const current = customDateRange || selectedDateRange();
  [el("customRangeStart"), el("customRangeEnd")].forEach((input) => {
    input.min = bounds.min;
    input.max = bounds.max;
  });
  el("customRangeStart").value = current.startDate;
  el("customRangeEnd").value = current.endDate;
  resetCustomRangeValidation();
  panel.classList.remove("hidden");
  el("customRangeStart").focus();
}

function closeCustomRangePanel(revert = false) {
  const panel = el("customRangePanel");
  if (!panel || panel.classList.contains("hidden")) return;
  panel.classList.add("hidden");
  // 只有在还没有生效的自定义区间时才回退下拉框，否则会丢掉已应用的选择。
  if (revert && !customDateRange) el("rangeSelect").value = lastPresetRangeValue;
}

function applyPresetRange(value) {
  customDateRange = null;
  lastPresetRangeValue = value;
  el("customRangeOption").textContent = "自定义…";
  el("rangeSelect").value = value;
  closeCustomRangePanel();
}

function customRangeError(startDate, endDate) {
  if (!startDate || !endDate) return { message: "请选择完整的开始与结束日期。", field: startDate ? "end" : "start" };
  const bounds = customRangeBounds();
  if (startDate > endDate) return { message: "开始日期不能晚于结束日期。", field: "start" };
  if (endDate > bounds.max) return { message: "结束日期不能晚于今天。", field: "end" };
  if (startDate < bounds.min) return { message: `开始日期最早为 ${bounds.min}。`, field: "start" };
  if (daysBetween(startDate, endDate) > CUSTOM_RANGE_MAX_DAYS) {
    return { message: `查询跨度最多 ${CUSTOM_RANGE_MAX_DAYS} 天。`, field: "start" };
  }
  return null;
}

async function applyCustomRange() {
  const startDate = el("customRangeStart").value;
  const endDate = el("customRangeEnd").value;
  const error = customRangeError(startDate, endDate);
  resetCustomRangeValidation();
  if (error) {
    el(error.field === "end" ? "customRangeEnd" : "customRangeStart").setAttribute("aria-invalid", "true");
    setCustomRangeHint(error.message, true);
    showToast(error.message);
    return;
  }
  customDateRange = { startDate, endDate };
  el("rangeSelect").value = "custom";
  el("customRangeOption").textContent = selectedDateRangeText();
  closeCustomRangePanel();
  await reloadForFilterChange();
}

el("rangeSelect").addEventListener("mousedown", (event) => {
  // 已处于自定义态时直接弹面板，省去"再选一次自定义"这一步；键盘操作仍走原生列表。
  if (el("rangeSelect").value !== "custom") return;
  event.preventDefault();
  const panel = el("customRangePanel");
  if (panel.classList.contains("hidden")) openCustomRangePanel();
  else closeCustomRangePanel();
});

el("rangeSelect").addEventListener("change", async () => {
  if (el("rangeSelect").value === "custom") {
    openCustomRangePanel();
    return;
  }
  applyPresetRange(el("rangeSelect").value);
  await reloadForFilterChange();
});

el("customRangeApply").addEventListener("click", applyCustomRange);

el("customRangeCancel").addEventListener("click", () => closeCustomRangePanel(true));

el("customRangePanel").querySelectorAll("[data-range-preset]").forEach((button) => {
  button.addEventListener("click", async () => {
    applyPresetRange(button.dataset.rangePreset);
    await reloadForFilterChange();
  });
});

// 用 mousedown 而不是 click 判定"点到外面"：原生下拉列表与日期日历都是浏览器控件，
// 在它们上面的点击不会冒泡成 document 事件，避免刚打开就被自己的 click 关掉。
document.addEventListener("mousedown", (event) => {
  if (el("customRangePanel").classList.contains("hidden")) return;
  if (event.target.closest("#customRangePanel") || event.target.closest("#rangeSelect")) return;
  closeCustomRangePanel(true);
});

document.addEventListener("mousedown", (event) => {
  ["stability", "cost"].forEach((scope) => {
    const ids = observabilityRangeIds(scope);
    if (el(ids.panel)?.classList.contains("hidden")) return;
    if (event.target.closest(`#${ids.panel}`) || event.target.closest(`#${ids.select}`)) return;
    closeObservabilityRangePanel(scope, true);
  });
});

el("sourceSelect").addEventListener("change", reloadForFilterChange);

["usageDetailDateFilter", "usageDetailModelFilter", "usageDetailStatusFilter"].forEach((id) => {
  el(id).addEventListener("change", updateUsageTableFilters);
});
el("usageDetailSearch").addEventListener("input", updateUsageTableFilters);
el("usageDetailReset").addEventListener("click", resetUsageTableFilters);

el("refreshButton").addEventListener("click", async () => {
  if (currentView === "keys") {
    await loadKeys(true);
    await loadTeamKeys(true);
    showToast(keyLoadError || keyRefreshError ? "密钥列表刷新失败" : "已刷新密钥列表");
  } else if (currentView === "models") {
    await loadModels();
    showToast("\u5df2\u5237\u65b0\u6a21\u578b\u5217\u8868");
  } else if (currentView === "admin") {
    await loadAdminData(true);
    showToast("已读取最近一次同步数据");
  } else if (currentView === "team") {
    if (selectedTeamEmployee) {
      await Promise.all([
        loadTeamMemberData(selectedTeamEmployee, true, false),
        loadTeamRankingData(true),
      ]);
      showToast("已读取最近一次同步数据");
    } else {
      await loadTeamData(true);
      showToast("已读取最近一次同步数据");
    }
  } else if (currentView === "department") {
    await loadDepartmentData(true);
    showToast("已读取最近一次同步数据");
  } else if (currentView === "organization") {
    await loadOrganizationData();
    showToast("已刷新企业组织");
  } else {
    await loadDashboardData(true);
    showToast("已读取最近一次同步数据");
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
  resetDepartmentEmployeeSelection();
  selectedDepartment = "";
  openDepartmentPicker();
});

document.addEventListener("click", (event) => {
  if (!el("departmentDepartmentPicker").contains(event.target) && event.target !== el("departmentSearchButton")) {
    closeDepartmentPicker();
  }
});

el("departmentUserTable").addEventListener("click", async (event) => {
  const employeeRow = event.target.closest("[data-employee]");
  if (employeeRow && selectedDepartment) {
    selectDepartmentEmployee(employeeRow.dataset.employee);
    return;
  }
  const row = event.target.closest("[data-department]");
  if (!row) return;
  resetDepartmentEmployeeSelection();
  selectedDepartment = row.dataset.department;
  el("departmentEmployeeSearch").value = "";
  closeDepartmentPicker();
  const loading = loadDepartmentData();
  scrollToDetailCard("departmentDetailCard");
  await loading;
});

el("departmentClearEmployee").addEventListener("click", async () => {
  if (selectedDepartmentEmployee) {
    resetDepartmentEmployeeSelection();
    renderDepartment();
    return;
  }
  selectedDepartment = "";
  el("departmentEmployeeSearch").value = "";
  closeDepartmentPicker();
  await loadDepartmentData();
});

el("departmentBackButton").addEventListener("click", async () => {
  resetDepartmentEmployeeSelection();
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

["departmentEmployeeUsageDetailDateFilter", "departmentEmployeeUsageDetailModelFilter", "departmentEmployeeUsageDetailStatusFilter"].forEach((id) => {
  el(id).addEventListener("change", updateDepartmentEmployeeUsageFilters);
});
el("departmentEmployeeUsageDetailSearch").addEventListener("input", updateDepartmentEmployeeUsageFilters);
el("departmentEmployeeUsageDetailReset").addEventListener("click", resetDepartmentEmployeeUsageFilters);

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
  const teamRevokeButton = event.target.closest("[data-team-key-revoke]");
  if (teamRevokeButton) {
    openTeamKeyRevokeModal(teamRevokeButton.dataset.teamKeyRevoke);
    return;
  }
  const teamDeleteButton = event.target.closest("[data-team-key-delete]");
  if (teamDeleteButton) {
    openTeamKeyDeleteModal(teamDeleteButton.dataset.teamKeyDelete);
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

el("teamKeySelect")?.addEventListener("change", (event) => {
  selectedTeamKeyRef = event.target.value;
  loadTeamKeys();
});
el("teamKeySearch")?.addEventListener("input", (event) => {
  teamKeyFilters.search = event.target.value.trim();
  scheduleTeamKeyReload();
});
el("teamKeyStatusFilter")?.addEventListener("change", (event) => {
  teamKeyFilters.status = event.target.value || "all";
  loadTeamKeys();
});
el("resetTeamKeyFiltersButton")?.addEventListener("click", () => {
  teamKeyFilters = { search: "", status: "all" };
  if (el("teamKeySearch")) el("teamKeySearch").value = "";
  if (el("teamKeyStatusFilter")) el("teamKeyStatusFilter").value = "all";
  loadTeamKeys();
});
el("cancelTeamKeyRevokeButton")?.addEventListener("click", () => closeTeamKeyRevokeModal());
el("confirmTeamKeyRevokeButton")?.addEventListener("click", confirmTeamKeyRevoke);
el("teamKeyRevokeModal")?.addEventListener("click", (event) => {
  if (event.target === el("teamKeyRevokeModal")) closeTeamKeyRevokeModal();
});
el("cancelTeamKeyDeleteButton")?.addEventListener("click", () => closeTeamKeyDeleteModal());
el("confirmTeamKeyDeleteButton")?.addEventListener("click", confirmTeamKeyDelete);
el("teamKeyDeleteModal")?.addEventListener("click", (event) => {
  if (event.target === el("teamKeyDeleteModal")) closeTeamKeyDeleteModal();
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
el("costModelShareModalClose")?.addEventListener("click", closeCostModelShareModal);
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
    if (backdrop.id === "organizationMemberIdentityModal") closeOrganizationMemberIdentityModal();
    if (backdrop.id === "organizationTopupModal") closeOrganizationTopupModal();
    if (backdrop.id === "organizationCreditAdjustmentModal") closeOrganizationCreditAdjustmentModal();
    if (backdrop.id === "costModelShareModal") closeCostModelShareModal();
  });
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!el("costModelShareModal")?.classList.contains("hidden")) {
    closeCostModelShareModal();
    return;
  }
  if (!el("stabilityDrawer")?.classList.contains("hidden")) {
    closeStabilityDrawer();
    return;
  }
  if (!el("costDetailDrawer")?.classList.contains("hidden")) {
    el("costDetailDrawer").classList.add("hidden");
    el("costDetailDrawer").setAttribute("aria-hidden", "true");
    el("costDetailDrawerBackdrop")?.classList.add("hidden");
    el("costDetailDrawerBackdrop")?.setAttribute("aria-hidden", "true");
    costDrawerReturnFocus?.focus?.();
    costDrawerReturnFocus = null;
    return;
  }
  if (!el("customRangePanel").classList.contains("hidden")) {
    closeCustomRangePanel(true);
    return;
  }
  if (!el("newKeyModal").classList.contains("hidden")) clearPlainKey();
  else if (!el("deleteKeyModal").classList.contains("hidden")) closeDeleteKeyModal();
  else if (!el("regenerateKeyModal").classList.contains("hidden")) closeRegenerateKeyModal();
  else if (!el("createKeyModal").classList.contains("hidden")) closeCreateKeyModal();
  else if (!el("customerOrganizationModal").classList.contains("hidden")) closeCustomerOrganizationModal();
  else if (!el("organizationMemberModal").classList.contains("hidden")) closeOrganizationMemberModal();
  else if (!el("organizationMemberIdentityModal").classList.contains("hidden")) closeOrganizationMemberIdentityModal();
  else if (!el("organizationDepartmentModal").classList.contains("hidden")) closeOrganizationDepartmentModal();
  else if (!el("organizationTopupModal").classList.contains("hidden")) closeOrganizationTopupModal();
  else if (!el("organizationCreditAdjustmentModal")?.classList.contains("hidden")) closeOrganizationCreditAdjustmentModal();
  else if (!el("costItemModal")?.classList.contains("hidden")) closeCostItemModal();
  else if (!el("savingsActionModal")?.classList.contains("hidden")) closeSavingsActionModal();
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearRevealedKeys();
    clearOrganizationClaimLastUrl();
  } else if (isUsageView() && Date.now() - lastUsageAutoRefreshAt >= 30_000) {
    refreshVisibleUsageData();
  }
});

window.addEventListener("beforeunload", clearRevealedKeys);

async function init() {
  scheduleUsageAutoRefresh();
  const callbackParams = new URLSearchParams(window.location.search);
  organizationClaimToken = takeOrganizationClaimTokenFromUrl(callbackParams);
  organizationInvitationToken = takeOrganizationInvitationTokenFromUrl(callbackParams);
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
      remoteDemoReadOnly: false,
      remoteDemoUsageSnapshotOnly: false,
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
  if (organizationClaimToken) {
    el("authLoadingView").classList.add("hidden");
    showOrganizationClaimScreen();
    await verifyOrganizationClaim();
    return;
  }
  if (organizationInvitationToken) {
    el("authLoadingView").classList.add("hidden");
    showOrganizationInvitationScreen();
    await verifyOrganizationInvitation();
    return;
  }
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
