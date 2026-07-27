from pathlib import Path

from fastapi.testclient import TestClient

from backend import main


APP_JS = Path(__file__).parents[1] / "assets" / "app.js"
INDEX_HTML = Path(__file__).parents[1] / "index.html"


def test_auth_page_exposes_password_registration_and_recovery_flows() -> None:
    response = TestClient(main.app).get("/")

    assert response.status_code == 200
    assert 'id="passwordLoginScreen"' in response.text
    assert 'id="registerScreen"' in response.text
    assert 'id="forgotPasswordScreen"' in response.text
    assert 'id="resetPasswordScreen"' in response.text
    assert 'id="sendRegisterCodeButton"' in response.text
    assert 'data-password-toggle="loginPasswordInput"' in response.text
    assert 'id="ssoButton"' in response.text
    assert 'id="devLoginButton"' in response.text
    assert 'id="authLoadingView" class="login-shell"' in response.text
    assert 'id="loginView" class="login-shell hidden"' in response.text


def test_frontend_calls_local_auth_endpoints_and_attaches_csrf() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'api("/api/auth/csrf")' in source
    assert 'let csrfRefreshPromise = null;' in source
    assert 'headers["X-CSRF-Token"] = requestCsrfToken' in source
    assert 'code === "AUTH_CSRF_INVALID"' in source
    assert '(options.body === undefined || typeof options.body === "string")' in source
    assert 'recoverCsrfToken(requestCsrfToken)' in source
    assert 'return api(path, options, { csrfRetryAttempted: true });' in source
    assert 'api("/api/auth/verification/request"' in source
    assert 'api("/api/auth/register"' in source
    assert 'api("/api/auth/login"' in source
    assert 'api("/api/auth/password/forgot"' in source
    assert 'api("/api/auth/password/reset"' in source
    assert 'resetPasswordToken = takeResetPasswordTokenFromUrl(callbackParams)' in source
    assert 'params.delete("reset_token")' in source
    assert 'await ensureCsrfToken();\n    await api("/api/auth/logout"' in source


def test_frontend_loads_auth_config_before_session_state() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    init_source = source[source.index("async function init()") :]

    assert init_source.index('authConfig = await api("/api/auth/config")') < init_source.index('const user = await api("/api/auth/me")')
    assert "Promise.allSettled" not in init_source


def test_frontend_renders_account_provisioning_and_entitlement_states() -> None:
    response = TestClient(main.app).get("/")
    source = APP_JS.read_text(encoding="utf-8")

    assert 'id="accountAccessState"' in response.text
    assert 'id="accountAccessRetryButton"' in response.text
    assert 'accountStatus = user?.accountStatus || "provisioned"' in source
    assert 'entitlementStatus = user?.entitlementStatus || "active"' in source
    assert 'dashboard.classList.toggle("account-limited", Boolean(state))' in source


def test_frontend_keeps_session_state_on_transient_auth_failures() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'if (meError?.status !== 401)' in source
    assert 'el("authLoadingRetryButton").classList.remove("hidden")' in source
    assert 'el("authLoadingRetryButton").addEventListener("click", () => window.location.reload())' in source
    assert 'showToast(error.message || "退出登录失败，请检查网络后重试")' in source
    assert 'await api("/api/auth/logout"' in source
    assert 'showLogin();\n  } catch (error)' in source
    assert 'turnstileRenderPromises[mode]' in source


def test_frontend_disables_unavailable_auth_methods_and_handles_turnstile_config() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    markup = INDEX_HTML.read_text(encoding="utf-8")

    assert 'else authAccess = "none"' in source
    assert 'control.disabled = !passwordEnabled' in source
    assert 'if (!passwordLoginAvailable())' in source
    assert 'function publicSignupAvailable()' in source
    assert 'function passwordRecoveryAvailable()' in source
    assert 'passwordRecoveryAvailable: false' in source
    assert 'turnstileConfigured: false' in source
    assert '!authConfig.turnstileConfigured || !authConfig.turnstileSiteKey' in source
    assert '安全验证尚未正确配置，邮箱登录与注册暂时不可用' in source
    assert 'id="authAvailabilityNotice"' in markup
    assert 'if (!publicSignupAvailable()) {' in source
    assert 'forgotPasswordButton").classList.toggle("hidden", !recoveryEnabled)' in source
    assert '邮箱登录仍可用。' in source


def test_frontend_explains_signup_domains_and_inactive_access_policy() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    markup = INDEX_HTML.read_text(encoding="utf-8")

    assert 'allowedSignupDomains: []' in source
    assert 'function signupEmailAllowed(email)' in source
    assert '当前仅支持 ${formatSignupDomains()} 邮箱注册' in source
    assert 'id="registerPolicyNote"' in markup
    assert "gmail.com、qq.com、163.com 和 auto-link.com.cn" in markup
    assert "账号已创建，请使用刚刚设置的密码登录；充值或由管理员开通后方可使用模型和额度。" in source
    assert 'title: "账号已创建，等待开通"' in source
    assert 'description: "充值或由管理员开通后方可使用模型和额度。"' in source


def test_frontend_clears_reset_tokens_and_exposes_accessible_tabs() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    markup = INDEX_HTML.read_text(encoding="utf-8")

    assert 'function clearResetPasswordToken()' in source
    assert 'if (authMode === "reset" && requestedMode !== "reset") clearResetPasswordToken()' in source
    assert 'if (error.code === "AUTH_RESET_TOKEN_INVALID")' in source
    assert 'role="tab" aria-selected="true" aria-controls="personalAuthPanel" tabindex="0"' in markup
    assert 'role="tabpanel" aria-labelledby="personalAccessTab" aria-hidden="false"' in markup
    assert 'role="tabpanel" aria-labelledby="loginFlowTab" aria-hidden="false"' in markup
    assert 'tablist.addEventListener("keydown"' in source
