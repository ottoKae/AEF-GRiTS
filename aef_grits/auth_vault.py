"""Encrypted native-filesystem credential vault for shared Web deployments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
from typing import Any

from .storage_safety import mount_for_path


@dataclass(frozen=True)
class VaultRecord:
    handle: str
    owner_hash: str
    version: int
    payload: dict[str, Any]
    updated_at: str


def owner_hash(subject: str) -> str:
    return "user_" + hashlib.sha256(subject.encode("utf-8")).hexdigest()[:20]


def _native_path(path: Path, label: str) -> Path:
    resolved = path.expanduser().absolute()
    mount = mount_for_path(resolved)
    if mount and mount.is_linux_ntfs:
        raise ValueError(f"{label} must be stored on a native filesystem, not NTFS3")
    return resolved


def create_vault_key(path: Path, *, overwrite: bool = False) -> Path:
    """Create one Fernet key with owner-only permissions."""
    from cryptography.fernet import Fernet

    target = _native_path(path, "Credential vault key")
    if target.exists() and not overwrite:
        raise FileExistsError(f"Vault key already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(Fernet.generate_key() + b"\n")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


class CredentialVault:
    """Store refreshable OAuth records as encrypted SQLite blobs."""

    def __init__(self, database: Path, key_file: Path):
        self.database = _native_path(database, "Credential vault")
        self.key_file = _native_path(key_file, "Credential vault key")
        if not self.key_file.is_file():
            raise FileNotFoundError(f"Credential vault key does not exist: {self.key_file}")
        from cryptography.fernet import Fernet

        self._fernet = Fernet(self.key_file.read_bytes().strip())
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS credentials (
                    handle TEXT PRIMARY KEY,
                    owner_hash TEXT NOT NULL UNIQUE,
                    version INTEGER NOT NULL,
                    ciphertext BLOB NOT NULL,
                    updated_at TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        try:
            self.database.chmod(0o600)
        except OSError:
            pass

    def put(self, subject: str, payload: dict[str, Any]) -> VaultRecord:
        hashed = owner_hash(subject)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ciphertext = self._fernet.encrypt(plaintext)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT handle, version FROM credentials WHERE owner_hash = ?", (hashed,)
            ).fetchone()
            handle = existing[0] if existing else "cred_" + secrets.token_urlsafe(24)
            version = int(existing[1]) + 1 if existing else 1
            connection.execute(
                """
                INSERT INTO credentials(handle, owner_hash, version, ciphertext, updated_at, revoked)
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(owner_hash) DO UPDATE SET
                    version=excluded.version,
                    ciphertext=excluded.ciphertext,
                    updated_at=excluded.updated_at,
                    revoked=0
                """,
                (handle, hashed, version, ciphertext, now),
            )
        return VaultRecord(handle, hashed, version, dict(payload), now)

    def load(self, handle: str) -> VaultRecord:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT handle, owner_hash, version, ciphertext, updated_at, revoked
                   FROM credentials WHERE handle = ?""",
                (handle,),
            ).fetchone()
        if row is None or bool(row[5]):
            raise KeyError("Credential handle is missing or revoked")
        payload = json.loads(self._fernet.decrypt(row[3]).decode("utf-8"))
        return VaultRecord(str(row[0]), str(row[1]), int(row[2]), payload, str(row[4]))

    def load_owner(self, subject: str) -> VaultRecord:
        hashed = owner_hash(subject)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT handle FROM credentials WHERE owner_hash = ? AND revoked = 0",
                (hashed,),
            ).fetchone()
        if row is None:
            raise KeyError("No active credential exists for this user")
        return self.load(str(row[0]))

    def revoke_owner(self, subject: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE credentials SET revoked = 1 WHERE owner_hash = ?",
                (owner_hash(subject),),
            )


def credentials_payload(credentials: Any, *, project: str | None = None) -> dict[str, Any]:
    """Serialize only fields needed to reconstruct refreshable user credentials."""
    expiry = getattr(credentials, "expiry", None)
    return {
        "token": getattr(credentials, "token", None),
        "refresh_token": getattr(credentials, "refresh_token", None),
        "token_uri": getattr(credentials, "token_uri", "https://oauth2.googleapis.com/token"),
        "client_id": getattr(credentials, "client_id", None),
        "client_secret": getattr(credentials, "client_secret", None),
        "scopes": list(getattr(credentials, "scopes", None) or ()),
        "expiry": expiry.isoformat() if expiry else None,
        "project": project,
    }


def credentials_from_payload(payload: dict[str, Any]):
    from google.oauth2.credentials import Credentials

    credentials = Credentials(
        token=payload.get("token"),
        refresh_token=payload.get("refresh_token"),
        token_uri=payload.get("token_uri") or "https://oauth2.googleapis.com/token",
        client_id=payload.get("client_id"),
        client_secret=payload.get("client_secret"),
        scopes=payload.get("scopes") or None,
    )
    expiry = payload.get("expiry")
    if expiry:
        value = str(expiry).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        credentials.expiry = parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    return credentials
