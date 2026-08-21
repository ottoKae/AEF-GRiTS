from __future__ import annotations

import sqlite3

from aef_grits.auth_vault import CredentialVault, create_vault_key, owner_hash


def test_vault_encrypts_payload_and_versions_reauthentication(tmp_path):
    database = tmp_path / "vault.sqlite"
    key = create_vault_key(tmp_path / "vault.key")
    vault = CredentialVault(database, key)
    first = vault.put("google-subject", {"refresh_token": "secret-one", "project": "gee-test"})
    second = vault.put("google-subject", {"refresh_token": "secret-two", "project": "gee-test"})
    assert first.handle == second.handle
    assert second.version == 2
    assert second.owner_hash == owner_hash("google-subject")
    assert vault.load(second.handle).payload["refresh_token"] == "secret-two"
    assert b"secret-two" not in database.read_bytes()


def test_vault_revocation_is_terminal(tmp_path):
    key = create_vault_key(tmp_path / "vault.key")
    vault = CredentialVault(tmp_path / "vault.sqlite", key)
    record = vault.put("subject", {"refresh_token": "secret"})
    vault.revoke_owner("subject")
    try:
        vault.load(record.handle)
    except KeyError:
        pass
    else:
        raise AssertionError("revoked credential remained readable")
