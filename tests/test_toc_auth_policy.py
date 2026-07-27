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
