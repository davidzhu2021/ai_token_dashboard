import base64
import binascii
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class KeyVaultError(RuntimeError):
    pass


class KeyVaultConfigError(KeyVaultError):
    pass


class KeyVaultDataError(KeyVaultError):
    pass


def decode_master_key(value: str) -> bytes:
    text = value.strip()
    if not text:
        raise KeyVaultConfigError("未配置密钥保管主密钥")
    try:
        padding = "=" * (-len(text) % 4)
        key = base64.urlsafe_b64decode(text + padding)
    except (ValueError, binascii.Error) as exc:
        raise KeyVaultConfigError("密钥保管主密钥格式无效") from exc
    if len(key) != 32:
        raise KeyVaultConfigError("密钥保管主密钥必须是 32 字节")
    return key


class KeyVault:
    def __init__(self, database_path: Path, master_key: str) -> None:
        self.database_path = database_path
        self._cipher = AESGCM(decode_master_key(master_key))
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._restrict_permissions(self.database_path.parent, 0o700)
        self._initialize()

    @classmethod
    def from_environment(cls, root_dir: Path) -> "KeyVault":
        configured_path = os.getenv("KEY_VAULT_DATABASE_PATH", ".data/key-vault.sqlite3").strip()
        path = Path(configured_path)
        if not path.is_absolute():
            path = root_dir / path
        return cls(path, os.getenv("KEY_VAULT_MASTER_KEY", ""))

    @staticmethod
    def _restrict_permissions(path: Path, mode: int) -> None:
        try:
            path.chmod(mode)
        except OSError:
            pass

    @staticmethod
    def _aad(backend_id: str, user_id: str, key_id: str) -> bytes:
        return f"key-vault-v1\0{backend_id}\0{user_id}\0{key_id}".encode("utf-8")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS key_secrets (
                        backend_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        key_id TEXT NOT NULL,
                        nonce BLOB NOT NULL,
                        ciphertext BLOB NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (backend_id, user_id, key_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pending_key_rotations (
                        backend_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        old_key_id TEXT NOT NULL,
                        replacement_key_id TEXT NOT NULL,
                        cleanup_target TEXT NOT NULL CHECK (cleanup_target IN ('old', 'replacement')),
                        last_error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (backend_id, user_id, old_key_id),
                        UNIQUE (backend_id, user_id, replacement_key_id)
                    )
                    """
                )
        except sqlite3.Error as exc:
            raise KeyVaultError("无法初始化密钥保管数据库") from exc
        self._restrict_permissions(self.database_path, 0o600)

    def has(self, backend_id: str, user_id: str, key_id: str) -> bool:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT 1 FROM key_secrets WHERE backend_id = ? AND user_id = ? AND key_id = ?",
                    (backend_id, user_id, key_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise KeyVaultError("无法读取密钥保管数据库") from exc
        return row is not None

    def store(self, backend_id: str, user_id: str, key_id: str, plaintext: str) -> None:
        if not plaintext.startswith("sk-"):
            raise KeyVaultDataError("只能保管有效的访问密钥")
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(nonce, plaintext.encode("utf-8"), self._aad(backend_id, user_id, key_id))
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO key_secrets (backend_id, user_id, key_id, nonce, ciphertext, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (backend_id, user_id, key_id) DO UPDATE SET
                        nonce = excluded.nonce,
                        ciphertext = excluded.ciphertext,
                        updated_at = excluded.updated_at
                    """,
                    (backend_id, user_id, key_id, nonce, ciphertext, now, now),
                )
        except sqlite3.Error as exc:
            raise KeyVaultError("无法保存加密访问密钥") from exc

    def replace(self, backend_id: str, user_id: str, old_key_id: str, new_key_id: str, plaintext: str) -> None:
        if not plaintext.startswith("sk-"):
            raise KeyVaultDataError("只能保管有效的访问密钥")
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(nonce, plaintext.encode("utf-8"), self._aad(backend_id, user_id, new_key_id))
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM key_secrets WHERE backend_id = ? AND user_id = ? AND key_id = ?",
                    (backend_id, user_id, old_key_id),
                )
                connection.execute(
                    """
                    INSERT INTO key_secrets (backend_id, user_id, key_id, nonce, ciphertext, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (backend_id, user_id, key_id) DO UPDATE SET
                        nonce = excluded.nonce,
                        ciphertext = excluded.ciphertext,
                        updated_at = excluded.updated_at
                    """,
                    (backend_id, user_id, new_key_id, nonce, ciphertext, now, now),
                )
                connection.execute(
                    "DELETE FROM pending_key_rotations WHERE backend_id = ? AND user_id = ? AND old_key_id = ?",
                    (backend_id, user_id, old_key_id),
                )
        except sqlite3.Error as exc:
            raise KeyVaultError("无法更新加密访问密钥") from exc

    def delete(self, backend_id: str, user_id: str, key_id: str) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM key_secrets WHERE backend_id = ? AND user_id = ? AND key_id = ?",
                    (backend_id, user_id, key_id),
                )
                connection.execute(
                    """
                    DELETE FROM pending_key_rotations
                    WHERE backend_id = ? AND user_id = ?
                      AND (old_key_id = ? OR replacement_key_id = ?)
                    """,
                    (backend_id, user_id, key_id, key_id),
                )
        except sqlite3.Error as exc:
            raise KeyVaultError("无法删除加密访问密钥") from exc

    def record_pending_rotation(
        self,
        backend_id: str,
        user_id: str,
        old_key_id: str,
        replacement_key_id: str,
        cleanup_target: Literal["old", "replacement"],
        last_error: str = "",
    ) -> None:
        if cleanup_target not in {"old", "replacement"}:
            raise KeyVaultDataError("无效的密钥更新清理目标")
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO pending_key_rotations (
                        backend_id, user_id, old_key_id, replacement_key_id,
                        cleanup_target, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (backend_id, user_id, old_key_id) DO UPDATE SET
                        replacement_key_id = excluded.replacement_key_id,
                        cleanup_target = excluded.cleanup_target,
                        last_error = excluded.last_error,
                        updated_at = excluded.updated_at
                    """,
                    (
                        backend_id,
                        user_id,
                        old_key_id,
                        replacement_key_id,
                        cleanup_target,
                        last_error[:500],
                        now,
                        now,
                    ),
                )
        except sqlite3.Error as exc:
            raise KeyVaultError("无法记录待完成的密钥更新") from exc

    def pending_rotation(self, backend_id: str, user_id: str, old_key_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT old_key_id, replacement_key_id, cleanup_target, last_error, created_at, updated_at
                    FROM pending_key_rotations
                    WHERE backend_id = ? AND user_id = ? AND old_key_id = ?
                    """,
                    (backend_id, user_id, old_key_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise KeyVaultError("无法读取待完成的密钥更新") from exc
        return self._pending_rotation_dict(row) if row is not None else None

    def pending_rotations(self, backend_id: str, user_id: str) -> list[dict[str, Any]]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT old_key_id, replacement_key_id, cleanup_target, last_error, created_at, updated_at
                    FROM pending_key_rotations
                    WHERE backend_id = ? AND user_id = ?
                    ORDER BY created_at
                    """,
                    (backend_id, user_id),
                ).fetchall()
        except sqlite3.Error as exc:
            raise KeyVaultError("无法读取待完成的密钥更新") from exc
        return [self._pending_rotation_dict(row) for row in rows]

    @staticmethod
    def _pending_rotation_dict(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
        return {
            "oldKeyId": str(row[0]),
            "replacementKeyId": str(row[1]),
            "cleanupTarget": str(row[2]),
            "lastError": str(row[3] or ""),
            "createdAt": str(row[4]),
            "updatedAt": str(row[5]),
        }

    def complete_pending_rotation(self, backend_id: str, user_id: str, old_key_id: str) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM key_secrets WHERE backend_id = ? AND user_id = ? AND key_id = ?",
                    (backend_id, user_id, old_key_id),
                )
                connection.execute(
                    "DELETE FROM pending_key_rotations WHERE backend_id = ? AND user_id = ? AND old_key_id = ?",
                    (backend_id, user_id, old_key_id),
                )
        except sqlite3.Error as exc:
            raise KeyVaultError("无法完成密钥更新清理") from exc

    def discard_pending_rotation(self, backend_id: str, user_id: str, old_key_id: str) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM pending_key_rotations WHERE backend_id = ? AND user_id = ? AND old_key_id = ?",
                    (backend_id, user_id, old_key_id),
                )
        except sqlite3.Error as exc:
            raise KeyVaultError("无法清理待完成的密钥更新记录") from exc

    def reveal(self, backend_id: str, user_id: str, key_id: str) -> str | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT nonce, ciphertext FROM key_secrets WHERE backend_id = ? AND user_id = ? AND key_id = ?",
                    (backend_id, user_id, key_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise KeyVaultError("无法读取加密访问密钥") from exc
        if row is None:
            return None
        try:
            plaintext = self._cipher.decrypt(bytes(row[0]), bytes(row[1]), self._aad(backend_id, user_id, key_id)).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError, ValueError) as exc:
            raise KeyVaultDataError("访问密钥无法解密，可能是主密钥变更或保管数据损坏") from exc
        if not plaintext.startswith("sk-"):
            raise KeyVaultDataError("解密后的访问密钥格式无效")
        return plaintext
