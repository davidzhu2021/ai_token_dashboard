import base64
import secrets
import sqlite3

import pytest

from backend.key_vault import KeyVault, KeyVaultConfigError, KeyVaultDataError


def master_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def test_vault_encrypts_and_reveals_without_plaintext_on_disk(tmp_path) -> None:
    database = tmp_path / "vault.sqlite3"
    vault = KeyVault(database, master_key())
    plaintext = "sk-super-secret-ABCD"

    vault.store("primary", "user-1", "hash-1", plaintext)

    assert vault.has("primary", "user-1", "hash-1") is True
    assert vault.reveal("primary", "user-1", "hash-1") == plaintext
    assert plaintext.encode("utf-8") not in database.read_bytes()


def test_vault_wrong_master_key_cannot_decrypt(tmp_path) -> None:
    database = tmp_path / "vault.sqlite3"
    KeyVault(database, master_key()).store("primary", "user-1", "hash-1", "sk-super-secret-ABCD")

    with pytest.raises(KeyVaultDataError):
        KeyVault(database, master_key()).reveal("primary", "user-1", "hash-1")


@pytest.mark.parametrize("value", ["", "not-base64", base64.urlsafe_b64encode(b"short").decode("ascii")])
def test_vault_rejects_missing_or_invalid_master_key(tmp_path, value: str) -> None:
    with pytest.raises(KeyVaultConfigError):
        KeyVault(tmp_path / "vault.sqlite3", value)


def test_vault_detects_tampered_ciphertext(tmp_path) -> None:
    database = tmp_path / "vault.sqlite3"
    vault = KeyVault(database, master_key())
    vault.store("primary", "user-1", "hash-1", "sk-super-secret-ABCD")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE key_secrets SET ciphertext = ? WHERE backend_id = ? AND user_id = ? AND key_id = ?",
            (b"tampered", "primary", "user-1", "hash-1"),
        )

    with pytest.raises(KeyVaultDataError):
        vault.reveal("primary", "user-1", "hash-1")


def test_vault_replace_removes_old_identifier(tmp_path) -> None:
    vault = KeyVault(tmp_path / "vault.sqlite3", master_key())
    vault.store("primary", "user-1", "old-hash", "sk-old-secret-ABCD")

    vault.replace("primary", "user-1", "old-hash", "new-hash", "sk-new-secret-EFGH")

    assert vault.reveal("primary", "user-1", "old-hash") is None
    assert vault.reveal("primary", "user-1", "new-hash") == "sk-new-secret-EFGH"


def test_vault_delete_only_removes_matching_scope(tmp_path) -> None:
    vault = KeyVault(tmp_path / "vault.sqlite3", master_key())
    vault.store("primary", "user-1", "hash-1", "sk-user-one-ABCD")
    vault.store("primary", "user-1", "hash-2", "sk-user-one-EFGH")
    vault.store("primary", "user-2", "hash-1", "sk-user-two-IJKL")
    vault.store("history", "user-1", "hash-1", "sk-history-MNOP")

    vault.delete("primary", "user-1", "hash-1")

    assert vault.reveal("primary", "user-1", "hash-1") is None
    assert vault.reveal("primary", "user-1", "hash-2") == "sk-user-one-EFGH"
    assert vault.reveal("primary", "user-2", "hash-1") == "sk-user-two-IJKL"
    assert vault.reveal("history", "user-1", "hash-1") == "sk-history-MNOP"


def test_vault_persists_and_completes_pending_old_key_cleanup(tmp_path) -> None:
    database = tmp_path / "vault.sqlite3"
    key = master_key()
    vault = KeyVault(database, key)
    vault.store("primary", "user-1", "old-hash", "sk-old-secret-ABCD")
    vault.store("primary", "user-1", "new-hash", "sk-new-secret-EFGH")
    vault.record_pending_rotation("primary", "user-1", "old-hash", "new-hash", "old", "delete failed")

    reopened = KeyVault(database, key)
    assert reopened.pending_rotation("primary", "user-1", "old-hash") == {
        "oldKeyId": "old-hash",
        "replacementKeyId": "new-hash",
        "cleanupTarget": "old",
        "lastError": "delete failed",
        "createdAt": reopened.pending_rotation("primary", "user-1", "old-hash")["createdAt"],
        "updatedAt": reopened.pending_rotation("primary", "user-1", "old-hash")["updatedAt"],
    }

    reopened.complete_pending_rotation("primary", "user-1", "old-hash")

    assert reopened.pending_rotation("primary", "user-1", "old-hash") is None
    assert reopened.reveal("primary", "user-1", "old-hash") is None
    assert reopened.reveal("primary", "user-1", "new-hash") == "sk-new-secret-EFGH"
