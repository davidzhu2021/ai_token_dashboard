import pytest
from fastapi import HTTPException

from backend import main
from backend.auth_store import AuthStore


TOC_EMAIL_DOMAINS = (
    "auto-link.com.cn",
    "gmail.com",
    "qq.com",
    "163.com",
)


@pytest.mark.parametrize("domain", TOC_EMAIL_DOMAINS)
def test_toc_signup_accepts_each_configured_email_domain(tmp_path, monkeypatch, domain: str) -> None:
    monkeypatch.setattr(main, "_auth_store", AuthStore(tmp_path / f"auth-{domain}.sqlite3"))
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", ",".join(TOC_EMAIL_DOMAINS))

    assert main.validate_public_signup_email(f"Person@{domain.upper()}") == f"person@{domain}"


def _configure_public_signup_env(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("PASSWORD_LOGIN_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("EMAIL_VERIFICATION_REQUIRED", "true")
    monkeypatch.setenv("APP_BASE_URL", "https://myai.carher.net")
    monkeypatch.setenv("AUTH_DATABASE_PATH", ".data/auth.sqlite3")
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", ",".join(TOC_EMAIL_DOMAINS))
    monkeypatch.setenv("DEV_LOGIN_ENABLED", "false")
    monkeypatch.setenv("AUTH_EMAIL_DEBUG", "false")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.net")
    monkeypatch.setenv("SMTP_FROM", "no-reply@auto-link.com.cn")
    monkeypatch.setenv("SMTP_USERNAME", "smtp-user")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-password")
    monkeypatch.setenv("SMTP_SSL", "true")
    monkeypatch.setenv("SMTP_STARTTLS", "false")
    monkeypatch.setenv("TURNSTILE_ENABLED", "false")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "")


def test_public_signup_blocks_missing_bot_protection_by_default(monkeypatch) -> None:
    _configure_public_signup_env(monkeypatch)
    monkeypatch.delenv("AUTH_ALLOW_NO_BOT_PROTECTION", raising=False)

    assert main.bot_protection_opt_out() is False
    assert main.signup_unavailable_code() == "AUTH_TURNSTILE_NOT_CONFIGURED"
    assert main.password_recovery_unavailable_code() == "AUTH_TURNSTILE_NOT_CONFIGURED"


def test_public_signup_allows_missing_bot_protection_only_when_opted_out(monkeypatch) -> None:
    _configure_public_signup_env(monkeypatch)
    monkeypatch.setenv("AUTH_ALLOW_NO_BOT_PROTECTION", "true")

    assert main.bot_protection_opt_out() is True
    assert main.signup_unavailable_code() == ""
    assert main.password_recovery_unavailable_code() == ""
    assert main.local_signup_ready() is True


def test_bot_protection_opt_out_does_not_bypass_other_signup_gates(monkeypatch) -> None:
    """Opting out of Turnstile must not relax HTTPS or mail-delivery requirements."""
    _configure_public_signup_env(monkeypatch)
    monkeypatch.setenv("AUTH_ALLOW_NO_BOT_PROTECTION", "true")
    monkeypatch.setenv("APP_BASE_URL", "http://myai.carher.net")

    assert main.signup_unavailable_code() == "AUTH_PASSWORD_LOGIN_HTTPS_REQUIRED"

    monkeypatch.setenv("APP_BASE_URL", "https://myai.carher.net")
    monkeypatch.setenv("SMTP_HOST", "")

    assert main.signup_unavailable_code() == "AUTH_SIGNUP_EMAIL_NOT_CONFIGURED"


@pytest.mark.parametrize("host", ("127.0.0.1", "10.68.13.188", "172.28.0.5", "192.168.10.4"))
def test_local_relay_accepts_private_smtp_host_without_credentials(monkeypatch, host: str) -> None:
    _configure_public_signup_env(monkeypatch)
    monkeypatch.setenv("AUTH_ALLOW_NO_BOT_PROTECTION", "true")
    monkeypatch.setenv("SMTP_LOCAL_RELAY", "true")
    monkeypatch.setenv("SMTP_HOST", host)
    monkeypatch.setenv("SMTP_PORT", "25")
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)

    assert main.smtp_host_is_private_relay() is True
    assert main.smtp_local_relay_enabled() is True
    assert main.smtp_configured() is True
    assert main.signup_unavailable_code() == ""


@pytest.mark.parametrize("host", ("8.8.8.8", "smtp.263.net", "gmail-smtp-in.l.google.com"))
def test_local_relay_refuses_public_smtp_host(monkeypatch, host: str) -> None:
    """A public host must never get the credential-free, cleartext treatment."""
    _configure_public_signup_env(monkeypatch)
    monkeypatch.setenv("AUTH_ALLOW_NO_BOT_PROTECTION", "true")
    monkeypatch.setenv("SMTP_LOCAL_RELAY", "true")
    monkeypatch.setenv("SMTP_HOST", host)
    monkeypatch.setenv("SMTP_PORT", "25")
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)

    assert main.smtp_local_relay_enabled() is False
    assert main.smtp_configured() is False
    assert main.signup_unavailable_code() == "AUTH_SIGNUP_EMAIL_NOT_CONFIGURED"


def test_local_relay_requires_explicit_opt_in(monkeypatch) -> None:
    _configure_public_signup_env(monkeypatch)
    monkeypatch.setenv("AUTH_ALLOW_NO_BOT_PROTECTION", "true")
    monkeypatch.delenv("SMTP_LOCAL_RELAY", raising=False)
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)

    assert main.smtp_local_relay_enabled() is False
    assert main.smtp_configured() is False


def test_local_relay_send_path_drops_credentials_and_tls(monkeypatch) -> None:
    """The private hop must go out in the clear with no AUTH, whatever env says."""
    _configure_public_signup_env(monkeypatch)
    monkeypatch.setenv("SMTP_LOCAL_RELAY", "true")
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", "25")
    # Stale third-party settings must not leak onto the local hop.
    monkeypatch.setenv("SMTP_USERNAME", "stale-user")
    monkeypatch.setenv("SMTP_PASSWORD", "stale-password")
    monkeypatch.setenv("SMTP_SSL", "true")
    monkeypatch.setenv("SMTP_STARTTLS", "false")

    calls: dict[str, object] = {"login": False, "starttls": False, "ssl": False}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls["host"] = host
            calls["port"] = port

        def starttls(self, context=None):
            calls["starttls"] = True

        def login(self, username, password):
            calls["login"] = True

        def send_message(self, message):
            calls["sent_to"] = message["To"]
            calls["sent_from"] = message["From"]

        def quit(self):
            calls["quit"] = True

    def fail_ssl(*args, **kwargs):
        calls["ssl"] = True
        raise AssertionError("local relay must not use implicit TLS")

    monkeypatch.setattr(main.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(main.smtplib, "SMTP_SSL", fail_ssl)

    main.send_auth_email_sync("person@auto-link.com.cn", "验证码", "123456")

    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 25
    assert calls["ssl"] is False
    assert calls["starttls"] is False
    assert calls["login"] is False
    assert calls["sent_to"] == "person@auto-link.com.cn"
    assert calls["quit"] is True


@pytest.mark.parametrize(
    "email",
    (
        "person@example.com",
        "person@mail.gmail.com",
        "person@gmail.com.example.com",
        "person@auto-link.com.cn.example.com",
    ),
)
def test_toc_signup_rejects_domains_outside_exact_allowlist(tmp_path, monkeypatch, email: str) -> None:
    monkeypatch.setattr(main, "_auth_store", AuthStore(tmp_path / "auth-blocked.sqlite3"))
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", ",".join(TOC_EMAIL_DOMAINS))

    with pytest.raises(HTTPException) as exc_info:
        main.validate_public_signup_email(email)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "AUTH_EMAIL_DOMAIN_NOT_ALLOWED"
