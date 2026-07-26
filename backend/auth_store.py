"""SQLite-backed persistence for local authentication.

The dashboard historically kept the authenticated user in Starlette's signed
session cookie.  This module provides the durable pieces needed by password
authentication while keeping the storage boundary independent from FastAPI.
Each operation uses a short-lived SQLite connection, so the store is safe to
use from the synchronous worker threads used by the current application and
can later be replaced by a PostgreSQL implementation with the same methods.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class AuthStoreError(RuntimeError):
    """Base exception for authentication persistence errors."""


class AuthStoreConfigError(AuthStoreError):
    """Raised when the configured authentication database is unsupported."""


class DuplicateEmailError(AuthStoreError):
    """Raised when attempting to create a user for an existing email."""


class AuthStore:
    """Small, thread-safe SQLite repository for local authentication data."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @classmethod
    def from_environment(cls, root_dir: str | Path | None = None) -> "AuthStore":
        """Build a store from ``AUTH_DATABASE_PATH``/``AUTH_DATABASE_URL``.

        SQLite is deliberately the default for the current single-service
        deployment.  ``AUTH_DATABASE_URL`` accepts ``sqlite:///...`` URLs;
        non-SQLite URLs fail loudly instead of silently writing to another
        location.
        """

        root = Path(root_dir or Path.cwd())
        configured_path = os.getenv("AUTH_DATABASE_PATH", "").strip()
        configured_url = os.getenv("AUTH_DATABASE_URL", "").strip()
        if configured_path:
            path = Path(configured_path)
        elif configured_url:
            path = cls._sqlite_path_from_url(configured_url)
        else:
            path = Path(".data") / "auth.sqlite3"
        if not path.is_absolute():
            path = root / path
        return cls(path)

    @staticmethod
    def _sqlite_path_from_url(url: str) -> Path:
        if url in {":memory:", "sqlite:///:memory:", "sqlite://"}:
            # A shared in-memory URI is unsuitable for independent short-lived
            # connections; use a private temporary file for predictable CRUD.
            return Path(".data") / "auth.sqlite3"
        if url.startswith("sqlite:////"):
            return Path("/" + url.removeprefix("sqlite:////"))
        if url.startswith("sqlite:///"):
            return Path(url.removeprefix("sqlite:///"))
        if url.startswith("sqlite://"):
            return Path(url.removeprefix("sqlite://"))
        raise AuthStoreConfigError("AUTH_DATABASE_URL 目前只支持 SQLite 数据库")

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _timestamp(cls, value: datetime | str | float | int | None) -> str:
        if value is None:
            return cls._now().isoformat()
        if isinstance(value, datetime):
            current = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return current.astimezone(timezone.utc).isoformat()
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        text = str(value).strip()
        if not text:
            return cls._now().isoformat()
        return text

    @classmethod
    def _is_expired(cls, value: str | None) -> bool:
        if not value:
            return True
        try:
            text = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed <= cls._now()
        except (TypeError, ValueError):
            return True

    @staticmethod
    def normalize_email(email: str) -> str:
        value = str(email or "").strip().lower()
        if value.count("@") != 1 or len(value) > 254:
            raise ValueError("请输入有效邮箱")
        local, domain = value.split("@", 1)
        if (
            not local
            or len(local) > 64
            or local.startswith(".")
            or local.endswith(".")
            or ".." in local
            or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
        ):
            raise ValueError("请输入有效邮箱")
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("请输入有效邮箱") from exc
        if len(domain) > 253 or not all(
            label
            and len(label) <= 63
            and not label.startswith("-")
            and not label.endswith("-")
            and re.fullmatch(r"[a-z0-9-]+", label)
            for label in domain.split(".")
        ):
            raise ValueError("请输入有效邮箱")
        return f"{local}@{domain}"

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database_path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection
        except sqlite3.IntegrityError:
            # Callers translate constraint violations into domain errors such
            # as DuplicateEmailError, so preserve the concrete exception.
            raise
        except sqlite3.Error as exc:
            raise AuthStoreError("认证数据存储操作失败") from exc
        finally:
            connection.close()

    def _initialize(self) -> None:
        schema = (
            """
            CREATE TABLE IF NOT EXISTS auth_users (
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
            """,
            """
            CREATE TABLE IF NOT EXISTS auth_identities (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                subject TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(provider, subject)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS auth_verification_codes (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                purpose TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                consumed_at TEXT,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_auth_verification_lookup
            ON auth_verification_codes(email, purpose, created_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS auth_password_reset_tokens (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                activated_at TEXT,
                used_at TEXT,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS auth_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                created_at TEXT NOT NULL,
                ip_address TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT ''
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id, expires_at)",
            """
            CREATE TABLE IF NOT EXISTS auth_rate_limits (
                action TEXT NOT NULL,
                key TEXT NOT NULL,
                window_started_at TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(action, key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS auth_audit_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                user_id TEXT,
                email_hash TEXT,
                ip_address TEXT NOT NULL DEFAULT '',
                success INTEGER NOT NULL DEFAULT 1,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS auth_upstream_accounts (
                user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
                backend_id TEXT NOT NULL,
                upstream_user_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(user_id, backend_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS auth_provisioning_jobs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
                backend_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        )
        with self._lock:
            try:
                with self._connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    for statement in schema:
                        connection.execute(statement)
                    reset_columns = {
                        str(row["name"])
                        for row in connection.execute("PRAGMA table_info(auth_password_reset_tokens)").fetchall()
                    }
                    if "activated_at" not in reset_columns:
                        connection.execute("ALTER TABLE auth_password_reset_tokens ADD COLUMN activated_at TEXT")
                        # Legacy rows were created before pending delivery existed,
                        # so preserve already-issued links during migration.
                        connection.execute(
                            "UPDATE auth_password_reset_tokens SET activated_at = created_at WHERE activated_at IS NULL"
                        )
                    connection.execute("COMMIT")
            except sqlite3.Error as exc:
                raise AuthStoreError("无法初始化认证数据库") from exc
        try:
            self.database_path.chmod(0o600)
        except OSError:
            pass

    def health(self) -> dict[str, Any]:
        """Return a small readiness probe without exposing database details."""

        try:
            with self._connection() as connection:
                connection.execute("SELECT 1").fetchone()
            return {"status": "ok", "backend": "sqlite"}
        except AuthStoreError:
            return {"status": "error", "backend": "sqlite"}

    def close(self) -> None:
        """Compatibility no-op; operations do not retain open connections."""

        return None

    # ------------------------------------------------------------------ users
    def create_user(
        self,
        email: str,
        name: str | None = None,
        password_hash: str | None = None,
        email_verified: bool = False,
        status: str = "active",
        *,
        password: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = self.normalize_email(email)
        if password_hash is None and password is not None:
            # Import lazily to keep this repository usable without FastAPI.
            from .auth import hash_password

            password_hash = hash_password(password)
        if not password_hash:
            raise ValueError("密码哈希不能为空")
        now = self._now().isoformat()
        identifier = user_id or str(uuid.uuid4())
        display = (name or normalized.split("@", 1)[0]).strip() or normalized.split("@", 1)[0]
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO auth_users
                        (id, email, name, password_hash, email_verified, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (identifier, normalized, display, password_hash, int(email_verified), status, now, now),
                )
                connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            raise DuplicateEmailError("该邮箱已注册") from exc
        return self.get_user(identifier) or {}

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM auth_users WHERE id = ?", (str(user_id),)).fetchone()
        return self._user_dict(row) if row else None

    def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        return self.get_user(user_id)

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        try:
            normalized = self.normalize_email(email)
        except ValueError:
            return None
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM auth_users WHERE email = ?", (normalized,)).fetchone()
        return self._user_dict(row) if row else None

    @staticmethod
    def _user_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        data.update(
            {
                "emailVerified": bool(data["email_verified"]),
                "passwordHash": data["password_hash"],
                "createdAt": data["created_at"],
                "updatedAt": data["updated_at"],
                "lastLoginAt": data["last_login_at"],
            }
        )
        return data

    def mark_email_verified(self, user_id: str) -> dict[str, Any] | None:
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE auth_users SET email_verified = 1, updated_at = ? WHERE id = ?",
                (now, str(user_id)),
            )
        return self.get_user(user_id)

    def update_password(self, user_id: str, password_hash: str) -> dict[str, Any] | None:
        if not password_hash:
            raise ValueError("密码哈希不能为空")
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE auth_users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (password_hash, now, str(user_id)),
            )
            connection.execute(
                "UPDATE auth_password_reset_tokens SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
                (now, str(user_id)),
            )
        return self.get_user(user_id)

    def set_user_status(self, user_id: str, status: str) -> dict[str, Any] | None:
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE auth_users SET status = ?, updated_at = ? WHERE id = ?",
                (str(status), now, str(user_id)),
            )
            if str(status).lower() not in {"active", "provisioning", "provisioned"}:
                connection.execute(
                    "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                    (now, str(user_id)),
                )
        return self.get_user(user_id)

    def touch_last_login(self, user_id: str) -> None:
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE auth_users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (now, now, str(user_id)),
            )

    # ------------------------------------------------------------- verification
    @staticmethod
    def _token_digest(value: str) -> str:
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

    @classmethod
    def _candidate_hashes(cls, value: str) -> tuple[str, ...]:
        text = str(value)
        digest = cls._token_digest(text)
        return (text, digest) if text == digest else (digest, text)

    def create_verification_code(
        self,
        email: str,
        purpose: str,
        code_hash: str,
        expires_at: datetime | str | float | int,
        max_attempts: int = 5,
        *,
        code: str | None = None,
    ) -> dict[str, Any]:
        normalized = self.normalize_email(email)
        value = code if code is not None else str(code_hash)
        # Raw six-digit codes are never persisted; callers may also pass a
        # pre-hashed value, which is retained for compatibility.
        stored_hash = self._token_digest(value) if value.isdigit() and len(value) <= 12 else str(code_hash)
        now = self._now().isoformat()
        identifier = str(uuid.uuid4())
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE auth_verification_codes SET consumed_at = ?
                WHERE email = ? AND purpose = ? AND consumed_at IS NULL
                """,
                (now, normalized, str(purpose)),
            )
            connection.execute(
                """
                INSERT INTO auth_verification_codes
                    (id, email, purpose, code_hash, expires_at, max_attempts, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (identifier, normalized, str(purpose), stored_hash, self._timestamp(expires_at), max(1, int(max_attempts)), now),
            )
            connection.execute("COMMIT")
        return {
            "id": identifier,
            "email": normalized,
            "purpose": str(purpose),
            "expires_at": self._timestamp(expires_at),
            "expiresAt": self._timestamp(expires_at),
            "max_attempts": max(1, int(max_attempts)),
        }

    def consume_verification_code(self, email: str, purpose: str, code_or_hash: str) -> bool:
        normalized = self.normalize_email(email)
        candidates = self._candidate_hashes(code_or_hash)
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM auth_verification_codes
                WHERE email = ? AND purpose = ? AND consumed_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (normalized, str(purpose)),
            ).fetchone()
            if row is None or self._is_expired(row["expires_at"]) or int(row["attempts"]) >= int(row["max_attempts"]):
                connection.execute("COMMIT")
                return False
            attempts = int(row["attempts"]) + 1
            valid = any(secrets.compare_digest(str(row["code_hash"]), candidate) for candidate in candidates)
            if valid:
                connection.execute("UPDATE auth_verification_codes SET attempts = ?, consumed_at = ? WHERE id = ?", (attempts, now, row["id"]))
            else:
                connection.execute("UPDATE auth_verification_codes SET attempts = ? WHERE id = ?", (attempts, row["id"]))
            connection.execute("COMMIT")
            return valid

    def claim_verification_code(self, email: str, purpose: str, code_or_hash: str) -> str | None:
        """Validate a code without consuming it before account creation succeeds."""
        normalized = self.normalize_email(email)
        candidates = self._candidate_hashes(code_or_hash)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM auth_verification_codes
                WHERE email = ? AND purpose = ? AND consumed_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (normalized, str(purpose)),
            ).fetchone()
            if row is None or self._is_expired(row["expires_at"]) or int(row["attempts"]) >= int(row["max_attempts"]):
                connection.execute("COMMIT")
                return None
            attempts = int(row["attempts"]) + 1
            valid = any(secrets.compare_digest(str(row["code_hash"]), candidate) for candidate in candidates)
            connection.execute("UPDATE auth_verification_codes SET attempts = ? WHERE id = ?", (attempts, row["id"]))
            connection.execute("COMMIT")
            return str(row["id"]) if valid else None

    def consume_claimed_verification_code(self, code_id: str) -> bool:
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE auth_verification_codes SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (now, str(code_id)),
            )
        return cursor.rowcount == 1

    def verify_verification_code(self, email: str, purpose: str, code_or_hash: str) -> bool:
        return self.consume_verification_code(email, purpose, code_or_hash)

    # --------------------------------------------------------------- reset token
    def create_password_reset_token(
        self,
        user_id: str,
        token_hash: str,
        expires_at: datetime | str | float | int,
        *,
        token: str | None = None,
        activate: bool = True,
    ) -> dict[str, Any]:
        value = token if token is not None else str(token_hash)
        stored_hash = self._token_digest(value) if token is not None or len(value) < 64 else value
        now = self._now().isoformat()
        identifier = str(uuid.uuid4())
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if activate:
                connection.execute(
                    "UPDATE auth_password_reset_tokens SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
                    (now, str(user_id)),
                )
            connection.execute(
                """
                INSERT INTO auth_password_reset_tokens
                    (id, user_id, token_hash, expires_at, activated_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (identifier, str(user_id), stored_hash, self._timestamp(expires_at), now if activate else None, now),
            )
            connection.execute("COMMIT")
        return {"id": identifier, "user_id": str(user_id), "userId": str(user_id), "expires_at": self._timestamp(expires_at), "expiresAt": self._timestamp(expires_at)}

    def activate_password_reset_token(self, token_id: str, user_id: str) -> bool:
        """Make a delivered reset token current without invalidating links early."""
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, activated_at FROM auth_password_reset_tokens
                WHERE id = ? AND user_id = ? AND used_at IS NULL
                """,
                (str(token_id), str(user_id)),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return False
            if row["activated_at"] is not None:
                connection.execute("COMMIT")
                return True
            connection.execute(
                """
                UPDATE auth_password_reset_tokens SET used_at = ?
                WHERE user_id = ? AND id != ? AND activated_at IS NOT NULL AND used_at IS NULL
                """,
                (now, str(user_id), str(token_id)),
            )
            connection.execute(
                "UPDATE auth_password_reset_tokens SET activated_at = ? WHERE id = ? AND used_at IS NULL",
                (now, str(token_id)),
            )
            connection.execute("COMMIT")
            return True

    def delete_password_reset_token(self, token_id: str) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM auth_password_reset_tokens WHERE id = ? AND used_at IS NULL",
                (str(token_id),),
            )
        return cursor.rowcount == 1

    def create_reset_token(self, user_id: str, token_hash: str, expires_at: datetime | str | float | int, *, token: str | None = None) -> dict[str, Any]:
        return self.create_password_reset_token(user_id, token_hash, expires_at, token=token)

    def consume_password_reset_token(self, token_or_hash: str) -> dict[str, Any] | None:
        candidates = self._candidate_hashes(token_or_hash)
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM auth_password_reset_tokens
                WHERE activated_at IS NOT NULL AND used_at IS NULL
                ORDER BY created_at DESC
                """
            ).fetchall()
            match = next((item for item in row if not self._is_expired(item["expires_at"]) and any(secrets.compare_digest(str(item["token_hash"]), candidate) for candidate in candidates)), None)
            if match is None:
                connection.execute("COMMIT")
                return None
            connection.execute("UPDATE auth_password_reset_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL", (now, match["id"]))
            connection.execute("COMMIT")
        result = dict(match)
        result.update({"userId": result["user_id"], "expiresAt": result["expires_at"]})
        return result

    def reset_password_with_token(self, token_or_hash: str, password_hash: str) -> str | None:
        """Update the password and consume its reset token in one transaction."""
        if not password_hash:
            raise ValueError("密码哈希不能为空")
        candidates = self._candidate_hashes(token_or_hash)
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM auth_password_reset_tokens
                WHERE activated_at IS NOT NULL AND used_at IS NULL AND token_hash IN (?, ?)
                ORDER BY created_at DESC
                """,
                candidates,
            ).fetchall()
            match = next((item for item in rows if not self._is_expired(item["expires_at"])), None)
            if match is None:
                connection.execute("COMMIT")
                return None
            user_id = str(match["user_id"])
            updated = connection.execute(
                "UPDATE auth_users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (password_hash, now, user_id),
            )
            if updated.rowcount != 1:
                connection.execute("ROLLBACK")
                return None
            connection.execute(
                "UPDATE auth_password_reset_tokens SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
                (now, user_id),
            )
            connection.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            connection.execute("COMMIT")
        return user_id

    def consume_reset_token(self, token_or_hash: str) -> dict[str, Any] | None:
        return self.consume_password_reset_token(token_or_hash)

    # ---------------------------------------------------------------- sessions
    def create_session(
        self,
        user_id: str,
        expires_at: datetime | str | float | int,
        ip_address: str = "",
        user_agent: str = "",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        raw_token = session_id or secrets.token_urlsafe(32)
        identifier = str(uuid.uuid4())
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO auth_sessions
                    (id, user_id, token_hash, expires_at, created_at, ip_address, user_agent)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (identifier, str(user_id), self._token_digest(raw_token), self._timestamp(expires_at), now, str(ip_address or "")[:128], str(user_agent or "")[:512]),
            )
        return {"id": identifier, "session_id": identifier, "sessionId": identifier, "token": raw_token, "user_id": str(user_id), "userId": str(user_id), "expires_at": self._timestamp(expires_at), "expiresAt": self._timestamp(expires_at)}

    def get_session(self, token_or_id: str) -> dict[str, Any] | None:
        candidates = self._candidate_hashes(token_or_id)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM auth_sessions
                WHERE (id = ? OR token_hash = ?) AND revoked_at IS NULL
                LIMIT 1
                """,
                (str(token_or_id), candidates[0]),
            ).fetchone()
        if row is None or self._is_expired(row["expires_at"]):
            if row is not None and self._is_expired(row["expires_at"]):
                self.revoke_session(token_or_id)
            return None
        result = dict(row)
        result.update({"userId": result["user_id"], "expiresAt": result["expires_at"], "createdAt": result["created_at"]})
        return result

    def revoke_session(self, token_or_id: str) -> bool:
        candidates = self._candidate_hashes(token_or_id)
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE revoked_at IS NULL AND (id = ? OR token_hash = ?)",
                (now, str(token_or_id), candidates[0]),
            )
        return cursor.rowcount > 0

    def revoke_user_sessions(self, user_id: str) -> int:
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            cursor = connection.execute("UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (now, str(user_id)))
        return int(cursor.rowcount)

    def revoke_all_sessions(self, user_id: str) -> int:
        return self.revoke_user_sessions(user_id)

    # --------------------------------------------------------------- rate limit
    def check_rate_limit(
        self,
        action: str,
        key: str,
        limit: int,
        window_seconds: int | float,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        max_count = max(1, int(limit))
        window = max(1.0, float(window_seconds))
        current = now or self._now()
        current_text = self._timestamp(current)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM auth_rate_limits WHERE action = ? AND key = ?", (str(action), str(key))).fetchone()
            reset = False
            if row is None:
                started = current
                count = 0
                connection.execute("INSERT INTO auth_rate_limits(action, key, window_started_at, count) VALUES (?, ?, ?, 0)", (str(action), str(key), current_text))
            else:
                try:
                    started = datetime.fromisoformat(str(row["window_started_at"]).replace("Z", "+00:00"))
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                except ValueError:
                    started = current
                count = int(row["count"])
                if (current - started).total_seconds() >= window:
                    started = current
                    count = 0
                    reset = True
            count += 1
            started_text = self._timestamp(started)
            connection.execute("UPDATE auth_rate_limits SET window_started_at = ?, count = ? WHERE action = ? AND key = ?", (started_text, count, str(action), str(key)))
            connection.execute("COMMIT")
        elapsed = max(0.0, (current - started).total_seconds())
        retry_after = max(0, int(window - elapsed))
        return {"allowed": count <= max_count, "limited": count > max_count, "count": count, "limit": max_count, "windowSeconds": int(window), "retryAfter": retry_after, "reset": reset}

    def is_rate_limited(self, action: str, key: str, limit: int, window_seconds: int | float) -> bool:
        return bool(self.check_rate_limit(action, key, limit, window_seconds)["limited"])

    def rate_limit(self, action: str, key: str, limit: int, window_seconds: int | float) -> dict[str, Any]:
        return self.check_rate_limit(action, key, limit, window_seconds)

    # ------------------------------------------------------------------- audit
    def record_audit_event(
        self,
        event_type: str,
        user_id: str | None = None,
        email: str | None = None,
        ip_address: str = "",
        success: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        identifier = str(uuid.uuid4())
        email_hash = self._token_digest(self.normalize_email(email)) if email else None
        try:
            metadata_json = json.dumps(metadata or {}, ensure_ascii=True, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            metadata_json = "{}"
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO auth_audit_events
                    (id, event_type, user_id, email_hash, ip_address, success, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (identifier, str(event_type), user_id, email_hash, str(ip_address or "")[:128], int(success), metadata_json, self._now().isoformat()),
            )
        return identifier

    def list_audit_events(self, user_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM auth_audit_events"
        params: list[Any] = []
        if user_id:
            query += " WHERE user_id = ?"
            params.append(str(user_id))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except (TypeError, ValueError):
                item["metadata"] = {}
            output.append(item)
        return output

    # -------------------------------------------------------- upstream mapping
    def get_upstream_account(self, user_id: str, backend_id: str = "primary") -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM auth_upstream_accounts WHERE user_id = ? AND backend_id = ?", (str(user_id), str(backend_id))).fetchone()
        return self._upstream_dict(row) if row else None

    @staticmethod
    def _upstream_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item.update({"userId": item["user_id"], "backendId": item["backend_id"], "upstreamUserId": item["upstream_user_id"], "lastError": item["last_error"], "createdAt": item["created_at"], "updatedAt": item["updated_at"]})
        return item

    def upsert_upstream_account(
        self,
        user_id: str,
        backend_id: str = "primary",
        upstream_user_id: str | None = None,
        status: str = "pending",
        last_error: str = "",
    ) -> dict[str, Any]:
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO auth_upstream_accounts
                    (user_id, backend_id, upstream_user_id, status, last_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, backend_id) DO UPDATE SET
                    upstream_user_id = COALESCE(excluded.upstream_user_id, auth_upstream_accounts.upstream_user_id),
                    status = excluded.status,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (str(user_id), str(backend_id), upstream_user_id, str(status), str(last_error or "")[:1000], now, now),
            )
        return self.get_upstream_account(user_id, backend_id) or {}

    def set_provisioning_status(
        self,
        user_id: str,
        status: str,
        backend_id: str = "primary",
        upstream_user_id: str | None = None,
        last_error: str = "",
    ) -> dict[str, Any]:
        return self.upsert_upstream_account(user_id, backend_id, upstream_user_id, status, last_error)

    def enqueue_provisioning(self, user_id: str, backend_id: str = "primary", last_error: str = "") -> dict[str, Any]:
        identifier = str(uuid.uuid4())
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM auth_provisioning_jobs
                WHERE user_id = ? AND backend_id = ? AND status IN ('pending', 'retry', 'running')
                ORDER BY created_at LIMIT 1
                """,
                (str(user_id), str(backend_id)),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE auth_provisioning_jobs
                    SET status = 'retry', attempts = attempts + 1, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(last_error or "")[:1000], now, existing["id"]),
                )
                connection.execute("COMMIT")
                row = connection.execute("SELECT * FROM auth_provisioning_jobs WHERE id = ?", (existing["id"],)).fetchone()
                return dict(row) if row is not None else {}
            connection.execute(
                """
                INSERT INTO auth_provisioning_jobs
                    (id, user_id, backend_id, status, attempts, last_error, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (identifier, str(user_id), str(backend_id), str(last_error or "")[:1000], now, now),
            )
            connection.execute("COMMIT")
        return {"id": identifier, "user_id": str(user_id), "backend_id": str(backend_id), "status": "pending", "attempts": 0, "last_error": str(last_error or "")[:1000], "created_at": now, "updated_at": now}

    def complete_provisioning_jobs(self, user_id: str, backend_id: str = "primary") -> int:
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE auth_provisioning_jobs
                SET status = 'completed', last_error = '', updated_at = ?
                WHERE user_id = ? AND backend_id = ? AND status IN ('pending', 'retry', 'running')
                """,
                (now, str(user_id), str(backend_id)),
            )
        return int(cursor.rowcount)

    def pending_provisioning(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM auth_provisioning_jobs WHERE status IN ('pending', 'retry') ORDER BY created_at LIMIT ?", (max(1, min(int(limit), 500)),)).fetchall()
        return [dict(row) for row in rows]
