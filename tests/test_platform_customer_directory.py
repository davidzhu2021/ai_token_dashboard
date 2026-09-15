"""Platform customer directory contract tests."""

from pathlib import Path

from fastapi.testclient import TestClient

from backend.auth import hash_password
from backend.auth_store import AuthStore
from backend import main


def test_auth_store_lists_personal_users_without_password_hash(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite3")
    personal = store.create_user("one@example.com", "One", hash_password("password-123"), True)
    managed = store.create_user("two@example.com", "Two", hash_password("password-123"), True)
    store.set_user_status(str(managed["id"]), "active")

    rows = store.list_personal_users(search="one")

    assert rows["total"] == 1
    assert rows["items"][0]["id"] == personal["id"]
    assert "passwordHash" not in rows["items"][0]


def test_auth_store_filters_upstream_provisioning_status(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite3")
    pending = store.create_user("pending@example.com", "Pending", hash_password("password-123"), True)
    failed = store.create_user("failed@example.com", "Failed", hash_password("password-123"), True)
    with store._lock, store._connection() as connection:
        connection.execute(
            "INSERT INTO auth_upstream_accounts (user_id, backend_id, status, created_at, updated_at) VALUES (?, 'primary', 'provisioning', datetime('now'), datetime('now'))",
            (pending["id"],),
        )
        connection.execute(
            "INSERT INTO auth_upstream_accounts (user_id, backend_id, status, created_at, updated_at) VALUES (?, 'primary', 'provisioning_failed', datetime('now'), datetime('now'))",
            (failed["id"],),
        )

    assert store.list_personal_users(status="provisioning")["total"] == 1
    assert store.list_personal_users(status="provisioning_failed")["total"] == 1


def test_platform_customer_routes_require_platform_admin() -> None:
    client = TestClient(main.app)
    assert client.get("/api/platform/customers/personal").status_code == 401


def test_customer_directory_markup_has_personal_customer_workspace() -> None:
    markup = Path(__file__).parents[1].joinpath("index.html").read_text(encoding="utf-8")
    source = Path(__file__).parents[1].joinpath("assets/app.js").read_text(encoding="utf-8")
    assert 'id="personalCustomersTab"' in markup
    assert 'id="personalCustomersView"' in markup
    assert "/api/platform/customers/personal" in source
    assert "停用后会阻断登录和个人 API 访问" in source
