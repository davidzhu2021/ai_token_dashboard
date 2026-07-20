from types import SimpleNamespace

from backend import main


def test_oidc_redirect_uri_uses_explicit_env(monkeypatch) -> None:
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://myai.carher.net/api/auth/callback")
    request = SimpleNamespace(headers={"host": "internal:8000"}, url=SimpleNamespace(scheme="http"))

    assert main.oidc_redirect_uri(request) == "https://myai.carher.net/api/auth/callback"


def test_oidc_redirect_uri_uses_forwarded_public_host(monkeypatch) -> None:
    monkeypatch.delenv("OIDC_REDIRECT_URI", raising=False)
    request = SimpleNamespace(
        headers={"host": "myai.carher.net", "x-forwarded-proto": "https"},
        url=SimpleNamespace(scheme="http"),
    )

    assert main.oidc_redirect_uri(request) == "https://myai.carher.net/api/auth/callback"
