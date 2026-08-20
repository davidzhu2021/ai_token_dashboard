import pytest

from backend import main


def test_loopback_defaults_to_mock(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.delenv("LOCAL_DATA_MODE", raising=False)
    assert main.local_data_mode() == "mock"


def test_loopback_can_explicitly_use_real(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("LOCAL_DATA_MODE", "real")
    assert main.local_data_mode() == "real"


def test_existing_real_organization_mode_keeps_local_integration_real(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.delenv("LOCAL_DATA_MODE", raising=False)
    monkeypatch.setenv("ORGANIZATION_MODE", "real")
    assert main.local_data_mode() == "real"


def test_existing_demo_organization_mode_keeps_legacy_demo_semantics(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.delenv("LOCAL_DATA_MODE", raising=False)
    monkeypatch.setenv("ORGANIZATION_MODE", "demo")
    assert main.local_data_mode() == "real"


def test_existing_local_password_auth_keeps_local_integration_real(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.delenv("LOCAL_DATA_MODE", raising=False)
    monkeypatch.delenv("ORGANIZATION_MODE", raising=False)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    assert main.local_data_mode() == "real"


def test_remote_mock_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://myai.carher.net")
    monkeypatch.setenv("LOCAL_DATA_MODE", "mock")
    with pytest.raises(RuntimeError, match="LOCAL_DATA_MODE=mock"):
        main.validate_local_data_mode()
