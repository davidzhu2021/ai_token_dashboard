import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from backend.auth import generate_auth_token, hash_auth_token, hash_password, verify_password
from backend.auth_store import AuthStore, DuplicateEmailError


def future(seconds: int = 300) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def test_password_hash_is_not_plaintext_and_verifies() -> None:
    encoded = hash_password("correct horse battery staple")

    assert encoded != "correct horse battery staple"
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong password", encoded)


def test_user_email_is_normalized_and_unique(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.create_user(" Person@Example.COM ", "Person", hash_password("password-123"))

    assert user["email"] == "person@example.com"
    assert user["email_verified"] == 0
    assert user["emailVerified"] is False
    assert user["password_hash"] != "password-123"
    assert store.get_user_by_email("PERSON@example.com")["id"] == user["id"]

    with pytest.raises(DuplicateEmailError):
        store.create_user("person@example.com", "Duplicate", hash_password("password-456"))


@pytest.mark.parametrize(
    "email",
    [
        "person@@example.com",
        ".person@example.com",
        "person.@example.com",
        "person..name@example.com",
        "person name@example.com",
        "person@example..com",
        "person@-example.com",
        "person@example-.com",
        "person@example_com",
    ],
)
def test_invalid_email_formats_are_rejected(tmp_path, email: str) -> None:
    store = AuthStore(tmp_path / "auth.sqlite3")

    with pytest.raises(ValueError):
        store.create_user(email, "Person", hash_password("password-123"))


def test_verification_code_is_single_use_and_limits_attempts(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite3")
    store.create_verification_code("person@example.com", "register", hash_auth_token("123456"), future(), max_attempts=2)

    assert not store.consume_verification_code("person@example.com", "register", "000000")
    assert store.consume_verification_code("person@example.com", "register", "123456")
    assert not store.consume_verification_code("person@example.com", "register", "123456")

    store.create_verification_code("person@example.com", "register", hash_auth_token("654321"), future(), max_attempts=1)
    assert not store.consume_verification_code("person@example.com", "register", "000000")
    assert not store.consume_verification_code("person@example.com", "register", "654321")


def test_claimed_verification_code_is_consumed_after_followup_succeeds(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite3")
    store.create_verification_code("person@example.com", "signup", hash_auth_token("123456"), future())

    code_id = store.claim_verification_code("person@example.com", "signup", hash_auth_token("123456"))

    assert code_id
    assert store.consume_claimed_verification_code(code_id)
    assert store.claim_verification_code("person@example.com", "signup", hash_auth_token("123456")) is None


def test_expired_verification_code_is_rejected(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite3")
    store.create_verification_code(
        "person@example.com",
        "register",
        hash_auth_token("123456"),
        datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    assert not store.consume_verification_code("person@example.com", "register", "123456")


def test_password_reset_token_is_hashed_and_single_use(tmp_path) -> None:
    database = tmp_path / "auth.sqlite3"
    store = AuthStore(database)
    user = store.create_user("person@example.com", "Person", hash_password("password-123"), email_verified=True)
    raw_token = generate_auth_token()
    store.create_password_reset_token(user["id"], hash_auth_token(raw_token), future())

    with sqlite3.connect(database) as connection:
        stored = connection.execute("SELECT token_hash FROM auth_password_reset_tokens").fetchone()[0]
    assert raw_token not in stored

    consumed = store.consume_password_reset_token(raw_token)
    assert consumed is not None
    assert consumed["user_id"] == user["id"]
    assert store.consume_password_reset_token(raw_token) is None


def test_pending_reset_token_does_not_replace_previous_token_until_activated(tmp_path) -> None:
    database = tmp_path / "auth.sqlite3"
    store = AuthStore(database)
    user = store.create_user("person@example.com", "Person", hash_password("password-123"), email_verified=True)
    old_token = "old-reset-token-that-is-long-enough"
    new_token = "new-reset-token-that-is-long-enough"
    store.create_password_reset_token(user["id"], hash_auth_token(old_token), future())

    pending = store.create_password_reset_token(
        user["id"],
        hash_auth_token(new_token),
        future(),
        activate=False,
    )

    assert store.consume_password_reset_token(new_token) is None
    assert store.delete_password_reset_token(pending["id"])
    assert store.consume_password_reset_token(old_token) is not None
    assert store.consume_password_reset_token(new_token) is None


def test_legacy_reset_tokens_are_activated_during_schema_migration(tmp_path) -> None:
    database = tmp_path / "auth.sqlite3"
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE auth_users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                email_verified INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE auth_password_reset_tokens (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO auth_users VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("user-1", "person@example.com", "Person", "hash", 1, "active", now, now, None),
        )
        connection.execute(
            "INSERT INTO auth_password_reset_tokens VALUES (?, ?, ?, ?, ?, ?)",
            ("token-1", "user-1", hash_auth_token("legacy-token"), future().isoformat(), None, now),
        )

    store = AuthStore(database)

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(auth_password_reset_tokens)")}
        activated_at = connection.execute(
            "SELECT activated_at FROM auth_password_reset_tokens WHERE id = 'token-1'"
        ).fetchone()[0]
    assert "activated_at" in columns
    assert activated_at == now
    assert store.consume_password_reset_token("legacy-token") is not None


def test_activating_delivered_reset_token_invalidates_previous_token(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.create_user("person@example.com", "Person", hash_password("password-123"), email_verified=True)
    old_token = "old-reset-token-that-is-long-enough"
    new_token = "new-reset-token-that-is-long-enough"
    store.create_password_reset_token(user["id"], hash_auth_token(old_token), future())
    pending = store.create_password_reset_token(
        user["id"],
        hash_auth_token(new_token),
        future(),
        activate=False,
    )

    assert store.activate_password_reset_token(pending["id"], user["id"])
    assert store.consume_password_reset_token(old_token) is None
    assert store.consume_password_reset_token(new_token) is not None


def test_activating_one_pending_reset_does_not_invalidate_another_pending_reset(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.create_user("person@example.com", "Person", hash_password("password-123"), email_verified=True)
    first_token = "first-pending-token-that-is-long-enough"
    second_token = "second-pending-token-that-is-long-enough"
    first = store.create_password_reset_token(
        user["id"],
        hash_auth_token(first_token),
        future(),
        activate=False,
    )
    second = store.create_password_reset_token(
        user["id"],
        hash_auth_token(second_token),
        future(),
        activate=False,
    )

    assert store.activate_password_reset_token(first["id"], user["id"])
    assert store.activate_password_reset_token(second["id"], user["id"])
    assert store.consume_password_reset_token(first_token) is None
    assert store.consume_password_reset_token(second_token) is not None


def test_password_reset_update_is_atomic_and_revokes_sessions(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.create_user("person@example.com", "Person", hash_password("password-123"), email_verified=True)
    session = store.create_session(user["id"], future())
    raw_token = generate_auth_token()
    store.create_password_reset_token(user["id"], hash_auth_token(raw_token), future())
    new_hash = hash_password("new-password-456")

    assert store.reset_password_with_token(hash_auth_token(raw_token), new_hash) == user["id"]
    assert verify_password("new-password-456", store.get_user(user["id"])["password_hash"])
    assert store.get_session(session["token"]) is None
    assert store.reset_password_with_token(hash_auth_token(raw_token), hash_password("another-password")) is None


def test_invalid_reset_does_not_change_password(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.create_user("person@example.com", "Person", hash_password("password-123"), email_verified=True)

    assert store.reset_password_with_token(hash_auth_token("missing-token"), hash_password("new-password-456")) is None
    assert verify_password("password-123", store.get_user(user["id"])["password_hash"])


def test_server_session_can_be_loaded_and_revoked(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.create_user("person@example.com", "Person", hash_password("password-123"), email_verified=True)
    created = store.create_session(user["id"], future(), "127.0.0.1", "pytest")

    session = store.get_session(created["token"])
    assert session is not None
    assert session["user_id"] == user["id"]
    assert store.revoke_session(created["token"])
    assert store.get_session(created["token"]) is None

    first = store.create_session(user["id"], future())
    second = store.create_session(user["id"], future())
    assert store.revoke_all_sessions(user["id"]) == 2
    assert store.get_session(first["token"]) is None
    assert store.get_session(second["token"]) is None


def test_rate_limit_is_atomic_across_threads(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite3")

    def hit() -> dict:
        return store.check_rate_limit("login", "127.0.0.1", 5, 60)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: hit(), range(8)))

    assert sorted(item["count"] for item in results) == list(range(1, 9))
    assert sum(item["allowed"] for item in results) == 5
    assert sum(item["limited"] for item in results) == 3


def test_audit_does_not_store_plain_email(tmp_path) -> None:
    database = tmp_path / "auth.sqlite3"
    store = AuthStore(database)
    event_id = store.record_audit_event("login_failed", email="person@example.com", success=False, metadata={"reason": "password"})

    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT email_hash, metadata_json FROM auth_audit_events WHERE id = ?", (event_id,)).fetchone()
    assert row is not None
    assert row[0] != "person@example.com"
    assert "password" in row[1]


def test_upstream_account_status_is_persisted(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.create_user("person@example.com", "Person", hash_password("password-123"), email_verified=True)

    pending = store.set_provisioning_status(user["id"], "pending")
    assert pending["status"] == "pending"
    provisioned = store.set_provisioning_status(user["id"], "provisioned", upstream_user_id="local-123")
    assert provisioned["upstream_user_id"] == "local-123"
    assert store.get_upstream_account(user["id"])["status"] == "provisioned"


def test_provisioning_job_enqueue_is_deduplicated_and_completable(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.create_user("person@example.com", "Person", hash_password("password-123"), email_verified=True)

    first = store.enqueue_provisioning(user["id"], "primary", "timeout")
    second = store.enqueue_provisioning(user["id"], "primary", "still unavailable")

    assert second["id"] == first["id"]
    assert second["status"] == "retry"
    assert second["attempts"] == 1
    assert len(store.pending_provisioning()) == 1
    assert store.complete_provisioning_jobs(user["id"], "primary") == 1
    assert store.pending_provisioning() == []


def test_from_environment_uses_isolated_configured_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_DATABASE_PATH", "custom/auth.sqlite3")
    monkeypatch.delenv("AUTH_DATABASE_URL", raising=False)

    store = AuthStore.from_environment(tmp_path)

    assert store.database_path == tmp_path / "custom" / "auth.sqlite3"
    assert store.database_path.exists()
    assert store.health()["status"] == "ok"
