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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


class AuthStoreError(RuntimeError):
    """Base exception for authentication persistence errors."""


class AuthStoreConfigError(AuthStoreError):
    """Raised when the configured authentication database is unsupported."""


class DuplicateEmailError(AuthStoreError):
    """Raised when attempting to create a user for an existing email."""


class DuplicateLoginNameError(AuthStoreError):
    """Raised when a managed login name is already reserved."""


class MembershipClaimStateError(AuthStoreError):
    """Raised when a membership claim cannot make the requested transition."""


class ManagedAccountPasswordResetError(AuthStoreError):
    """Raised when an account is not eligible for an offline password reset."""


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

    @staticmethod
    def normalize_login_name(login_name: str) -> str:
        value = str(login_name or "").strip().casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", value):
            raise ValueError("账号需为 3-64 位字母、数字、点、下划线或连字符")
        return value

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
                email TEXT,
                login_name TEXT,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                email_verified INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                account_type TEXT NOT NULL DEFAULT 'personal',
                identity_status TEXT NOT NULL DEFAULT 'verified',
                identity_verified_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_users_email_ci
            ON auth_users(lower(email)) WHERE email IS NOT NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_users_login_name_ci
            ON auth_users(lower(login_name)) WHERE login_name IS NOT NULL
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
                claimed_at TEXT,
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
            """
            CREATE TABLE IF NOT EXISTS auth_membership_claims (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                organization_name TEXT NOT NULL,
                department_id TEXT NOT NULL,
                principal_id TEXT,
                member_name TEXT NOT NULL,
                login_name TEXT NOT NULL,
                role TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                auth_user_id TEXT REFERENCES auth_users(id) ON DELETE SET NULL,
                expires_at TEXT NOT NULL,
                token_consumed_at TEXT,
                accepted_at TEXT,
                approved_at TEXT,
                provisioning_at TEXT,
                activated_at TEXT,
                revoked_at TEXT,
                created_by TEXT NOT NULL DEFAULT '',
                approved_by TEXT NOT NULL DEFAULT '',
                revoked_by TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_membership_claim_open_login
            ON auth_membership_claims(lower(login_name))
            WHERE status IN ('pending', 'accepted_pending_approval', 'approved', 'provisioning')
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_auth_membership_claim_organization
            ON auth_membership_claims(organization_id, created_at)
            """,
        )
        with self._lock:
            try:
                with self._connection() as connection:
                    # Rebuilding auth_users is required to remove the legacy
                    # NOT NULL constraint from email. Keep child-table DDL
                    # pointing at auth_users while the table is replaced.
                    connection.execute("PRAGMA foreign_keys = OFF")
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(schema[0])
                    self._migrate_auth_users(connection)
                    for statement in schema[1:]:
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
                    verification_columns = {
                        str(row["name"])
                        for row in connection.execute("PRAGMA table_info(auth_verification_codes)").fetchall()
                    }
                    if "claimed_at" not in verification_columns:
                        connection.execute("ALTER TABLE auth_verification_codes ADD COLUMN claimed_at TEXT")
                    claim_columns = {
                        str(row["name"])
                        for row in connection.execute("PRAGMA table_info(auth_membership_claims)").fetchall()
                    }
                    if "principal_id" not in claim_columns:
                        connection.execute("ALTER TABLE auth_membership_claims ADD COLUMN principal_id TEXT")
                    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                    if violations:
                        raise sqlite3.IntegrityError("authentication migration introduced invalid foreign keys")
                    connection.execute("COMMIT")
                    connection.execute("PRAGMA foreign_keys = ON")
            except sqlite3.Error as exc:
                raise AuthStoreError("无法初始化认证数据库") from exc
        try:
            self.database_path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _migrate_auth_users(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"]): row
            for row in connection.execute("PRAGMA table_info(auth_users)").fetchall()
        }
        required = {"login_name", "account_type", "identity_status", "identity_verified_at"}
        email_not_null = bool(columns.get("email") and int(columns["email"]["notnull"]))
        if required.issubset(columns) and not email_not_null:
            return

        login_name = "login_name" if "login_name" in columns else "NULL"
        account_type = "account_type" if "account_type" in columns else "'personal'"
        identity_status = "identity_status" if "identity_status" in columns else "'verified'"
        identity_verified_at = (
            "identity_verified_at"
            if "identity_verified_at" in columns
            else "COALESCE(updated_at, created_at)"
        )
        connection.execute("DROP TABLE IF EXISTS auth_users_migration_new")
        connection.execute(
            """
            CREATE TABLE auth_users_migration_new (
                id TEXT PRIMARY KEY,
                email TEXT,
                login_name TEXT,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                email_verified INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                account_type TEXT NOT NULL DEFAULT 'personal',
                identity_status TEXT NOT NULL DEFAULT 'verified',
                identity_verified_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            )
            """
        )
        connection.execute(
            f"""
            INSERT INTO auth_users_migration_new
                (id, email, login_name, name, password_hash, email_verified, status,
                 account_type, identity_status, identity_verified_at,
                 created_at, updated_at, last_login_at)
            SELECT id, email, {login_name}, name, password_hash, email_verified, status,
                   {account_type}, {identity_status}, {identity_verified_at},
                   created_at, updated_at, last_login_at
            FROM auth_users
            """
        )
        connection.execute("DROP TABLE auth_users")
        connection.execute("ALTER TABLE auth_users_migration_new RENAME TO auth_users")

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
                        (id, email, name, password_hash, email_verified, status,
                         account_type, identity_status, identity_verified_at,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'personal', 'verified', ?, ?, ?)
                    """,
                    (identifier, normalized, display, password_hash, int(email_verified), status, now, now, now),
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
            row = connection.execute("SELECT * FROM auth_users WHERE lower(email) = ?", (normalized,)).fetchone()
        return self._user_dict(row) if row else None

    def get_user_by_login_name(self, login_name: str) -> dict[str, Any] | None:
        try:
            normalized = self.normalize_login_name(login_name)
        except ValueError:
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM auth_users WHERE lower(login_name) = ?",
                (normalized,),
            ).fetchone()
        return self._user_dict(row) if row else None

    def get_user_by_identifier(self, identifier: str) -> dict[str, Any] | None:
        """Resolve a password-login identifier without guessing identities."""

        value = str(identifier or "").strip()
        if "@" in value:
            return self.get_user_by_email(value)
        return self.get_user_by_login_name(value)

    def delete_unprovisioned_user(self, user_id: str) -> bool:
        """Remove a just-created account only while it has no durable activity.

        Invitation acceptance spans SQLite and PostgreSQL. If the PostgreSQL
        consume loses a race, this narrowly scoped compensation prevents the
        newly-created password account from becoming an orphan. Existing users,
        signed-in users, and accounts with upstream provisioning state are never
        eligible for this cleanup.
        """

        identifier = str(user_id or "").strip()
        if not identifier:
            return False
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            referenced = connection.execute(
                "SELECT "
                "EXISTS(SELECT 1 FROM auth_sessions WHERE user_id=?) OR "
                "EXISTS(SELECT 1 FROM auth_upstream_accounts WHERE user_id=?) OR "
                "EXISTS(SELECT 1 FROM auth_provisioning_jobs WHERE user_id=?) OR "
                "EXISTS(SELECT 1 FROM auth_membership_claims WHERE auth_user_id=?)",
                (identifier, identifier, identifier, identifier),
            ).fetchone()
            if referenced is None or bool(referenced[0]):
                connection.execute("COMMIT")
                return False
            cursor = connection.execute(
                "DELETE FROM auth_users WHERE id=? AND last_login_at IS NULL",
                (identifier,),
            )
            connection.execute("COMMIT")
        return cursor.rowcount == 1

    @staticmethod
    def _user_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        data.update(
            {
                "emailVerified": bool(data["email_verified"]),
                "passwordHash": data["password_hash"],
                "loginName": data.get("login_name"),
                "contactEmail": data.get("email"),
                "displayIdentifier": data.get("email") or data.get("login_name"),
                "accountType": data.get("account_type") or "personal",
                "identityStatus": data.get("identity_status") or "verified",
                "identityVerifiedAt": data.get("identity_verified_at"),
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

    # ------------------------------------------------------ membership claims
    def _expire_membership_claims(
        self,
        connection: sqlite3.Connection,
        *,
        claim_id: str | None = None,
        token_hash: str | None = None,
        login_name: str | None = None,
    ) -> set[str]:
        """Expire due claims and remove their unapproved password identities.

        Claim expiry and account cleanup must share a transaction. Otherwise a
        process interruption can release the claim while leaving its reserved
        username behind indefinitely.
        """

        clauses = ["status IN ('pending', 'accepted_pending_approval')"]
        params: list[Any] = []
        if claim_id is not None:
            clauses.append("id = ?")
            params.append(str(claim_id))
        if token_hash is not None:
            clauses.append("token_hash = ?")
            params.append(str(token_hash))
        if login_name is not None:
            clauses.append("lower(login_name) = ?")
            params.append(str(login_name).casefold())
        rows = connection.execute(
            "SELECT id, status, expires_at, auth_user_id FROM auth_membership_claims "
            f"WHERE {' AND '.join(clauses)}",
            params,
        ).fetchall()
        expired_rows = [row for row in rows if self._is_expired(row["expires_at"])]
        if not expired_rows:
            return set()

        now = self._now().isoformat()
        expired_ids: set[str] = set()
        for row in expired_rows:
            identifier = str(row["id"])
            updated = connection.execute(
                "UPDATE auth_membership_claims SET status='expired', updated_at=? "
                "WHERE id=? AND status IN ('pending', 'accepted_pending_approval')",
                (now, identifier),
            )
            if updated.rowcount != 1:
                continue
            expired_ids.add(identifier)
            user_id = str(row["auth_user_id"] or "")
            if str(row["status"]) != "accepted_pending_approval" or not user_id:
                continue
            # A pending-approval identity is not a durable account. Revoke any
            # anomalous session before deleting it, while preserving identities
            # that have already acquired provisioning or upstream state.
            connection.execute(
                "UPDATE auth_sessions SET revoked_at=COALESCE(revoked_at, ?) WHERE user_id=?",
                (now, user_id),
            )
            connection.execute(
                """
                DELETE FROM auth_users
                WHERE id=? AND account_type='enterprise_managed'
                  AND identity_status='pending_approval'
                  AND status='pending_approval'
                  AND NOT EXISTS(
                      SELECT 1 FROM auth_upstream_accounts WHERE user_id=auth_users.id
                  )
                  AND NOT EXISTS(
                      SELECT 1 FROM auth_provisioning_jobs WHERE user_id=auth_users.id
                  )
                  AND NOT EXISTS(
                      SELECT 1 FROM auth_membership_claims other
                      WHERE other.auth_user_id=auth_users.id AND other.id<>?
                        AND other.status NOT IN ('expired', 'revoked')
                  )
                """,
                (user_id, identifier),
            )
        return expired_ids

    @staticmethod
    def _membership_claim_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item.update(
            {
                "organizationId": item["organization_id"],
                "organizationName": item["organization_name"],
                "departmentId": item["department_id"],
                "principalId": item.get("principal_id"),
                "memberName": item["member_name"],
                "loginName": item["login_name"],
                "authUserId": item["auth_user_id"],
                "expiresAt": item["expires_at"],
                "tokenConsumedAt": item["token_consumed_at"],
                "acceptedAt": item["accepted_at"],
                "approvedAt": item["approved_at"],
                "provisioningAt": item["provisioning_at"],
                "activatedAt": item["activated_at"],
                "revokedAt": item["revoked_at"],
                "createdBy": item["created_by"],
                "approvedBy": item["approved_by"],
                "revokedBy": item["revoked_by"],
                "lastError": item["last_error"],
                "createdAt": item["created_at"],
                "updatedAt": item["updated_at"],
            }
        )
        item.pop("token_hash", None)
        return item

    def create_membership_claim(
        self,
        organization_id: str,
        organization_name: str,
        department_id: str,
        member_name: str,
        login_name: str,
        role: str = "admin",
        expires_at: datetime | str | float | int | None = None,
        created_by: str = "",
        *,
        token: str | None = None,
        claim_id: str | None = None,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Issue a one-time, offline-delivered enterprise account claim.

        Reissuing an untouched claim revokes its former token atomically. Once
        an account has accepted a claim, an explicit revoke/approve decision is
        required so a second link cannot race the pending identity.
        """

        normalized_login = self.normalize_login_name(login_name)
        normalized_role = str(role or "").strip().lower()
        if normalized_role not in {"admin", "member"}:
            raise ValueError("企业角色必须为 admin 或 member")
        required_text = {
            "organization_id": organization_id,
            "organization_name": organization_name,
            "department_id": department_id,
            "member_name": member_name,
        }
        if any(not str(value or "").strip() for value in required_text.values()):
            raise ValueError("企业、部门和成员信息不能为空")
        raw_token = token or secrets.token_urlsafe(32)
        if len(raw_token) < 24:
            raise ValueError("认领 Token 长度不足")
        identifier = str(claim_id or uuid.uuid4())
        now = self._now().isoformat()
        expiry = self._timestamp(expires_at or (self._now() + timedelta(hours=2)))
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._expire_membership_claims(connection, login_name=normalized_login)
                existing_user = connection.execute(
                    "SELECT id FROM auth_users WHERE lower(login_name) = ?",
                    (normalized_login,),
                ).fetchone()
                if existing_user is not None:
                    connection.execute("ROLLBACK")
                    raise DuplicateLoginNameError("该企业账号已存在")
                open_claim = connection.execute(
                    """
                    SELECT id, status, expires_at FROM auth_membership_claims
                    WHERE lower(login_name) = ?
                      AND status IN ('pending', 'accepted_pending_approval', 'approved', 'provisioning')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (normalized_login,),
                ).fetchone()
                if open_claim is not None and str(open_claim["status"]) != "pending":
                    connection.execute("ROLLBACK")
                    raise MembershipClaimStateError("该账号已进入认领或开通流程")
                if open_claim is not None:
                    replacement_status = "expired" if self._is_expired(open_claim["expires_at"]) else "revoked"
                    connection.execute(
                        """
                        UPDATE auth_membership_claims
                        SET status=?, revoked_at=CASE WHEN ?='revoked' THEN ? ELSE revoked_at END,
                            revoked_by=CASE WHEN ?='revoked' THEN ? ELSE revoked_by END,
                            updated_at=?
                        WHERE id=? AND status='pending'
                        """,
                        (
                            replacement_status,
                            replacement_status,
                            now,
                            replacement_status,
                            str(created_by or ""),
                            now,
                            str(open_claim["id"]),
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO auth_membership_claims
                        (id, organization_id, organization_name, department_id, principal_id,
                         member_name, login_name, role, token_hash, status,
                         expires_at, created_by, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        str(organization_id).strip(),
                        str(organization_name).strip(),
                        str(department_id).strip(),
                        str(principal_id or "").strip() or None,
                        str(member_name).strip(),
                        normalized_login,
                        normalized_role,
                        self._token_digest(raw_token),
                        expiry,
                        str(created_by or ""),
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            message = str(exc).lower()
            if "login_name" in message:
                raise DuplicateLoginNameError("该企业账号已存在或正在认领") from exc
            raise AuthStoreError("无法创建企业账号认领") from exc
        claim = self.get_membership_claim(identifier) or {}
        claim["token"] = raw_token
        return claim

    def get_membership_claim(self, claim_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_membership_claims(connection, claim_id=str(claim_id))
            row = connection.execute(
                "SELECT * FROM auth_membership_claims WHERE id = ?",
                (str(claim_id),),
            ).fetchone()
            connection.execute("COMMIT")
        return self._membership_claim_dict(row)

    def get_membership_claim_by_token(self, token: str) -> dict[str, Any] | None:
        digest = self._token_digest(str(token or ""))
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_membership_claims(connection, token_hash=digest)
            row = connection.execute(
                "SELECT * FROM auth_membership_claims WHERE token_hash = ?",
                (digest,),
            ).fetchone()
            connection.execute("COMMIT")
        if row is None or str(row["status"]) != "pending" or row["token_consumed_at"] is not None:
            return None
        return self._membership_claim_dict(row)

    def list_membership_claims(
        self,
        organization_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM auth_membership_claims"
        params: list[Any] = []
        if organization_id:
            query += " WHERE organization_id = ?"
            params.append(str(organization_id))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_membership_claims(connection)
            rows = connection.execute(query, params).fetchall()
            connection.execute("COMMIT")
        return [self._membership_claim_dict(row) or {} for row in rows]

    def accept_membership_claim(self, token: str, password_hash: str) -> dict[str, Any] | None:
        """Consume one claim and create its password identity atomically."""

        if not password_hash:
            raise ValueError("密码哈希不能为空")
        digest = self._token_digest(str(token or ""))
        now = self._now().isoformat()
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._expire_membership_claims(connection, token_hash=digest)
                claim = connection.execute(
                    "SELECT * FROM auth_membership_claims WHERE token_hash = ?",
                    (digest,),
                ).fetchone()
                if (
                    claim is None
                    or str(claim["status"]) != "pending"
                    or claim["token_consumed_at"] is not None
                    or claim["revoked_at"] is not None
                ):
                    connection.execute("COMMIT")
                    return None
                if self._is_expired(claim["expires_at"]):
                    connection.execute(
                        "UPDATE auth_membership_claims SET status='expired', updated_at=? WHERE id=? AND status='pending'",
                        (now, str(claim["id"])),
                    )
                    connection.execute("COMMIT")
                    return None
                existing = connection.execute(
                    "SELECT id FROM auth_users WHERE lower(login_name) = ?",
                    (str(claim["login_name"]).casefold(),),
                ).fetchone()
                if existing is not None:
                    connection.execute("ROLLBACK")
                    raise DuplicateLoginNameError("该企业账号已存在")
                user_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO auth_users
                        (id, email, login_name, name, password_hash, email_verified,
                         status, account_type, identity_status, identity_verified_at,
                         created_at, updated_at)
                    VALUES (?, NULL, ?, ?, ?, 0, 'pending_approval',
                            'enterprise_managed', 'pending_approval', NULL, ?, ?)
                    """,
                    (
                        user_id,
                        str(claim["login_name"]),
                        str(claim["member_name"]),
                        password_hash,
                        now,
                        now,
                    ),
                )
                updated = connection.execute(
                    """
                    UPDATE auth_membership_claims
                    SET status='accepted_pending_approval', auth_user_id=?,
                        token_consumed_at=?, accepted_at=?, updated_at=?
                    WHERE id=? AND status='pending' AND token_consumed_at IS NULL
                    """,
                    (user_id, now, now, now, str(claim["id"])),
                )
                if updated.rowcount != 1:
                    connection.execute("ROLLBACK")
                    return None
                connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            if "login_name" in str(exc).lower():
                raise DuplicateLoginNameError("该企业账号已存在") from exc
            raise AuthStoreError("无法接受企业账号认领") from exc
        return {
            "claim": self.get_membership_claim(str(claim["id"])),
            "user": self.get_user(user_id),
        }

    def approve_membership_claim(self, claim_id: str, approved_by: str) -> dict[str, Any] | None:
        """Verify a claimed identity while keeping login blocked for provisioning."""

        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_membership_claims(connection, claim_id=str(claim_id))
            claim = connection.execute(
                "SELECT * FROM auth_membership_claims WHERE id = ?",
                (str(claim_id),),
            ).fetchone()
            if claim is None:
                connection.execute("COMMIT")
                return None
            status = str(claim["status"])
            if status in {"approved", "provisioning", "active"}:
                result = self._membership_claim_dict(claim)
                connection.execute("COMMIT")
                return result
            if self._is_expired(claim["expires_at"]):
                connection.execute(
                    "UPDATE auth_membership_claims SET status='expired', updated_at=? "
                    "WHERE id=? AND status='accepted_pending_approval'",
                    (now, str(claim_id)),
                )
                connection.execute("COMMIT")
                raise MembershipClaimStateError("该认领已过期")
            if status != "accepted_pending_approval" or not claim["auth_user_id"]:
                connection.execute("ROLLBACK")
                raise MembershipClaimStateError("该认领当前不能批准")
            user_id = str(claim["auth_user_id"])
            updated_user = connection.execute(
                """
                UPDATE auth_users
                SET identity_status='verified', identity_verified_at=?,
                    status='provisioning', updated_at=?
                WHERE id=? AND account_type='enterprise_managed'
                  AND identity_status='pending_approval'
                """,
                (now, now, user_id),
            )
            if updated_user.rowcount != 1:
                connection.execute("ROLLBACK")
                raise MembershipClaimStateError("认领账号状态不一致")
            connection.execute(
                """
                UPDATE auth_membership_claims
                SET status='approved', approved_at=?, approved_by=?, updated_at=?
                WHERE id=? AND status='accepted_pending_approval'
                """,
                (now, str(approved_by or ""), now, str(claim_id)),
            )
            connection.execute("COMMIT")
        return self.get_membership_claim(str(claim_id))

    def mark_membership_claim_provisioning(
        self,
        claim_id: str,
        last_error: str = "",
    ) -> dict[str, Any] | None:
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute(
                "SELECT * FROM auth_membership_claims WHERE id=?",
                (str(claim_id),),
            ).fetchone()
            if claim is None:
                connection.execute("COMMIT")
                return None
            status = str(claim["status"])
            if status == "active":
                result = self._membership_claim_dict(claim)
                connection.execute("COMMIT")
                return result
            if status not in {"approved", "provisioning"}:
                connection.execute("ROLLBACK")
                raise MembershipClaimStateError("该认领尚未获批")
            connection.execute(
                """
                UPDATE auth_membership_claims
                SET status='provisioning', provisioning_at=COALESCE(provisioning_at, ?),
                    last_error=?, updated_at=? WHERE id=?
                """,
                (now, str(last_error or "")[:1000], now, str(claim_id)),
            )
            connection.execute("COMMIT")
        return self.get_membership_claim(str(claim_id))

    def activate_membership_claim(self, claim_id: str) -> dict[str, Any] | None:
        """Activate login only after organization provisioning has succeeded."""

        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute(
                "SELECT * FROM auth_membership_claims WHERE id=?",
                (str(claim_id),),
            ).fetchone()
            if claim is None:
                connection.execute("COMMIT")
                return None
            if str(claim["status"]) == "active":
                result = self._membership_claim_dict(claim)
                connection.execute("COMMIT")
                return result
            if str(claim["status"]) != "provisioning" or not claim["auth_user_id"]:
                connection.execute("ROLLBACK")
                raise MembershipClaimStateError("该认领尚未完成开通")
            user_id = str(claim["auth_user_id"])
            updated_user = connection.execute(
                """
                UPDATE auth_users SET status='active', updated_at=?
                WHERE id=? AND account_type='enterprise_managed'
                  AND identity_status='verified' AND status='provisioning'
                """,
                (now, user_id),
            )
            if updated_user.rowcount != 1:
                connection.execute("ROLLBACK")
                raise MembershipClaimStateError("企业账号尚未具备登录条件")
            connection.execute(
                """
                UPDATE auth_membership_claims
                SET status='active', activated_at=?, last_error='', updated_at=?
                WHERE id=? AND status='provisioning'
                """,
                (now, now, str(claim_id)),
            )
            connection.execute("COMMIT")
        return self.get_membership_claim(str(claim_id))

    def revoke_membership_claim(
        self,
        claim_id: str,
        revoked_by: str = "",
    ) -> dict[str, Any] | None:
        """Revoke an unapproved claim and release its reserved login name."""

        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute(
                "SELECT * FROM auth_membership_claims WHERE id=?",
                (str(claim_id),),
            ).fetchone()
            if claim is None:
                connection.execute("COMMIT")
                return None
            status = str(claim["status"])
            if status == "revoked":
                result = self._membership_claim_dict(claim)
                connection.execute("COMMIT")
                return result
            if status not in {"pending", "accepted_pending_approval"}:
                connection.execute("ROLLBACK")
                raise MembershipClaimStateError("该认领当前不能撤销")
            user_id = str(claim["auth_user_id"] or "")
            connection.execute(
                """
                UPDATE auth_membership_claims
                SET status='revoked', revoked_at=?, revoked_by=?, updated_at=?
                WHERE id=? AND status IN ('pending', 'accepted_pending_approval')
                """,
                (now, str(revoked_by or ""), now, str(claim_id)),
            )
            if user_id:
                # Pending-approval identities must never retain a usable
                # session. Revoke any anomalous row before deleting the
                # disposable account; the FK cascade then removes the rows.
                connection.execute(
                    "UPDATE auth_sessions SET revoked_at=COALESCE(revoked_at, ?) "
                    "WHERE user_id=?",
                    (now, user_id),
                )
                connection.execute(
                    """
                    DELETE FROM auth_users
                    WHERE id=? AND account_type='enterprise_managed'
                      AND identity_status='pending_approval'
                      AND status='pending_approval'
                    """,
                    (user_id,),
                )
            connection.execute("COMMIT")
        return self.get_membership_claim(str(claim_id))

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
        """Atomically reserve one verification code for a registration attempt."""
        normalized = self.normalize_email(email)
        candidates = self._candidate_hashes(code_or_hash)
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM auth_verification_codes
                WHERE email = ? AND purpose = ? AND consumed_at IS NULL AND claimed_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (normalized, str(purpose)),
            ).fetchone()
            if row is None or self._is_expired(row["expires_at"]) or int(row["attempts"]) >= int(row["max_attempts"]):
                connection.execute("COMMIT")
                return None
            attempts = int(row["attempts"]) + 1
            valid = any(secrets.compare_digest(str(row["code_hash"]), candidate) for candidate in candidates)
            if valid:
                connection.execute(
                    "UPDATE auth_verification_codes SET attempts = ?, claimed_at = ? WHERE id = ? AND claimed_at IS NULL",
                    (attempts, now, row["id"]),
                )
            else:
                connection.execute("UPDATE auth_verification_codes SET attempts = ? WHERE id = ?", (attempts, row["id"]))
            connection.execute("COMMIT")
            return str(row["id"]) if valid else None

    def consume_claimed_verification_code(self, code_id: str) -> bool:
        now = self._now().isoformat()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE auth_verification_codes SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL AND claimed_at IS NOT NULL",
                (now, str(code_id)),
            )
        return cursor.rowcount == 1

    def release_claimed_verification_code(self, code_id: str) -> bool:
        """Release a failed registration attempt without reviving consumed codes."""
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE auth_verification_codes SET claimed_at = NULL WHERE id = ? AND consumed_at IS NULL AND claimed_at IS NOT NULL",
                (str(code_id),),
            )
        return cursor.rowcount == 1

    def create_user_from_verification(
        self,
        email: str,
        name: str | None,
        password_hash: str,
        purpose: str,
        code_or_hash: str,
        *,
        status: str = "active",
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Create one user and consume one valid code in the same transaction."""
        normalized = self.normalize_email(email)
        if not password_hash:
            raise ValueError("密码哈希不能为空")
        candidates = self._candidate_hashes(code_or_hash)
        now = self._now().isoformat()
        identifier = user_id or str(uuid.uuid4())
        display = (name or normalized.split("@", 1)[0]).strip() or normalized.split("@", 1)[0]
        try:
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT * FROM auth_verification_codes
                    WHERE email = ? AND purpose = ? AND consumed_at IS NULL AND claimed_at IS NULL
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (normalized, str(purpose)),
                ).fetchone()
                if row is None or self._is_expired(row["expires_at"]) or int(row["attempts"]) >= int(row["max_attempts"]):
                    connection.execute("COMMIT")
                    return None
                attempts = int(row["attempts"]) + 1
                valid = any(secrets.compare_digest(str(row["code_hash"]), candidate) for candidate in candidates)
                if not valid:
                    connection.execute("UPDATE auth_verification_codes SET attempts = ? WHERE id = ?", (attempts, row["id"]))
                    connection.execute("COMMIT")
                    return None
                connection.execute(
                    "UPDATE auth_verification_codes SET attempts = ?, consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                    (attempts, now, row["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO auth_users
                        (id, email, name, password_hash, email_verified, status,
                         account_type, identity_status, identity_verified_at,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'personal', 'verified', ?, ?, ?)
                    """,
                    (identifier, normalized, display, password_hash, 1, status, now, now, now),
                )
                connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            raise DuplicateEmailError("该邮箱已注册") from exc
        return self.get_user(identifier)

    def verify_verification_code(self, email: str, purpose: str, code_or_hash: str) -> bool:
        return self.consume_verification_code(email, purpose, code_or_hash)

    # --------------------------------------------------------------- reset token
    def create_managed_account_password_reset(
        self,
        user_id: str,
        expires_at: datetime | str | float | int | None = None,
        *,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Issue a one-time reset link for a verified managed username account.

        The plaintext token is returned only from this call. Reissuing a link
        atomically invalidates every prior unconsumed link for the same account.
        """

        raw_token = token or secrets.token_urlsafe(32)
        if len(raw_token) < 24:
            raise ValueError("重置 Token 长度不足")
        identifier = str(uuid.uuid4())
        account_id = str(user_id or "").strip()
        if not account_id:
            raise ManagedAccountPasswordResetError("未找到可重置的企业账号")
        now = self._now()
        expiry = self._timestamp(expires_at or (now + timedelta(hours=2)))
        now_text = now.isoformat()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute(
                "SELECT account_type, identity_status FROM auth_users WHERE id=?",
                (account_id,),
            ).fetchone()
            if (
                user is None
                or str(user["account_type"]) != "enterprise_managed"
                or str(user["identity_status"]) != "verified"
            ):
                connection.execute("ROLLBACK")
                raise ManagedAccountPasswordResetError("仅已核验的企业账号可签发线下重置链接")
            connection.execute(
                "UPDATE auth_password_reset_tokens SET used_at=? "
                "WHERE user_id=? AND used_at IS NULL",
                (now_text, account_id),
            )
            connection.execute(
                """
                INSERT INTO auth_password_reset_tokens
                    (id, user_id, token_hash, expires_at, activated_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    account_id,
                    self._token_digest(raw_token),
                    expiry,
                    now_text,
                    now_text,
                ),
            )
            connection.execute("COMMIT")
        return {
            "id": identifier,
            "user_id": account_id,
            "userId": account_id,
            "expires_at": expiry,
            "expiresAt": expiry,
            "token": raw_token,
        }

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
        email_hash: str | None = None
        audit_email = str(email or "").strip()
        if audit_email:
            try:
                email_hash = self._token_digest(self.normalize_email(audit_email))
            except ValueError:
                # Username-only enterprise accounts have no email. Audit
                # persistence must not fail the completed security operation
                # when a legacy caller stringifies that NULL value.
                email_hash = None
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
