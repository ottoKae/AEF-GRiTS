"""Per-user Google/Earth Engine authorization for shared Web deployments."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import secrets
from typing import Any, MutableMapping

from aef_grits.auth import EARTH_ENGINE_SCOPES, CredentialVerificationError, project_fingerprint
from aef_grits.auth_vault import (
    CredentialVault,
    credentials_from_payload,
    credentials_payload,
    owner_hash,
)


IDENTITY_SCOPES = ("openid", "email", "profile")


@dataclass(frozen=True)
class AuthBinding:
    owner_hash: str
    auth_source: str
    credential_handle: str | None
    credential_version: str
    project_fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class WebAuth:
    """Keep local single-user and shared delegated modes explicitly separate."""

    def __init__(self) -> None:
        self.mode = os.environ.get("AEF_GRITS_WEB_AUTH_MODE", "local").strip().lower()
        if self.mode not in {"local", "oauth"}:
            raise ValueError("AEF_GRITS_WEB_AUTH_MODE must be local or oauth")
        self.client_file = Path(
            os.environ.get("AEF_GRITS_WEB_OAUTH_CLIENT", "")
        ).expanduser()
        self.redirect_uri = os.environ.get("AEF_GRITS_WEB_OAUTH_REDIRECT_URI", "").strip()
        self.allow_insecure = os.environ.get("AEF_GRITS_WEB_ALLOW_INSECURE_OAUTH", "0") == "1"
        self.allowed_domains = {
            value.strip().lower()
            for value in os.environ.get("AEF_GRITS_WEB_ALLOWED_DOMAINS", "").split(",")
            if value.strip()
        }
        self.vault: CredentialVault | None = None
        if self.mode == "oauth":
            if not self.client_file.is_file():
                raise ValueError("AEF_GRITS_WEB_OAUTH_CLIENT must name a readable OAuth client file")
            if not self.redirect_uri:
                raise ValueError("AEF_GRITS_WEB_OAUTH_REDIRECT_URI is required in oauth mode")
            if not self.redirect_uri.startswith("https://") and not self.allow_insecure:
                raise ValueError("OAuth redirect URI must use HTTPS")
            database = os.environ.get("AEF_GRITS_AUTH_VAULT", "").strip()
            key = os.environ.get("AEF_GRITS_AUTH_VAULT_KEY", "").strip()
            if not database or not key:
                raise ValueError("OAuth mode requires AEF_GRITS_AUTH_VAULT and AEF_GRITS_AUTH_VAULT_KEY")
            self.vault = CredentialVault(Path(database), Path(key))

    @property
    def requires_login(self) -> bool:
        return self.mode == "oauth"

    def _subject(self, session: MutableMapping[str, Any]) -> str | None:
        value = str(session.get("aef_google_sub") or "").strip()
        return value or None

    def status(self, session: MutableMapping[str, Any]) -> dict[str, Any]:
        if self.mode == "local":
            return {
                "mode": "local",
                "authenticated": True,
                "owner_hash": owner_hash("local-user"),
                "project_required": True,
            }
        subject = self._subject(session)
        if not subject or self.vault is None:
            return {"mode": "oauth", "authenticated": False, "project_required": True}
        try:
            record = self.vault.load_owner(subject)
        except KeyError:
            session.clear()
            return {"mode": "oauth", "authenticated": False, "project_required": True}
        return {
            "mode": "oauth",
            "authenticated": True,
            "owner_hash": record.owner_hash,
            "credential_version": f"vault_{record.version}",
            "email": session.get("aef_email"),
            "project_required": True,
        }

    def current_owner_hash(self, session: MutableMapping[str, Any]) -> str:
        if self.mode == "local":
            return owner_hash("local-user")
        subject = self._subject(session)
        if not subject:
            raise PermissionError("Google/Earth Engine login is required")
        return owner_hash(subject)

    def authorization_url(self, session: MutableMapping[str, Any]) -> str:
        if self.mode != "oauth":
            raise ValueError("OAuth login is disabled in local mode")
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_secrets_file(
            str(self.client_file),
            scopes=[*IDENTITY_SCOPES, *EARTH_ENGINE_SCOPES],
            redirect_uri=self.redirect_uri,
            autogenerate_code_verifier=True,
        )
        nonce = secrets.token_urlsafe(24)
        url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            nonce=nonce,
        )
        session["aef_oauth_state"] = state
        session["aef_oauth_nonce"] = nonce
        session["aef_oauth_code_verifier"] = flow.code_verifier
        return url

    def complete_callback(
        self, session: MutableMapping[str, Any], authorization_response: str
    ) -> dict[str, Any]:
        if self.mode != "oauth" or self.vault is None:
            raise ValueError("OAuth callback is disabled")
        state = str(session.pop("aef_oauth_state", ""))
        nonce = str(session.pop("aef_oauth_nonce", ""))
        verifier = session.pop("aef_oauth_code_verifier", None)
        if not state or not nonce:
            raise PermissionError("OAuth state is missing or expired")
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_secrets_file(
            str(self.client_file),
            scopes=[*IDENTITY_SCOPES, *EARTH_ENGINE_SCOPES],
            state=state,
            redirect_uri=self.redirect_uri,
            code_verifier=verifier,
        )
        flow.fetch_token(authorization_response=authorization_response)
        credentials = flow.credentials
        if not credentials.refresh_token:
            raise PermissionError("Offline access was not granted; no refresh token was returned")
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(
            credentials.id_token,
            Request(),
            audience=credentials.client_id,
        )
        if claims.get("nonce") != nonce:
            raise PermissionError("OAuth nonce mismatch")
        subject = str(claims.get("sub") or "").strip()
        email = str(claims.get("email") or "").strip().lower()
        domain = str(claims.get("hd") or (email.rsplit("@", 1)[1] if "@" in email else "")).lower()
        if not subject or not email:
            raise PermissionError("Google identity did not include a stable subject and email")
        if self.allowed_domains and domain not in self.allowed_domains:
            raise PermissionError("This Google account is outside the permitted campus domain")
        record = self.vault.put(subject, credentials_payload(credentials))
        session.clear()
        session["aef_google_sub"] = subject
        session["aef_email"] = email
        return {
            "authenticated": True,
            "owner_hash": record.owner_hash,
            "credential_version": f"vault_{record.version}",
        }

    def logout(self, session: MutableMapping[str, Any]) -> None:
        subject = self._subject(session)
        if subject and self.vault is not None:
            self.vault.revoke_owner(subject)
        session.clear()

    def bind_project(self, session: MutableMapping[str, Any], project: str) -> AuthBinding:
        project = project.strip()
        if not project:
            raise ValueError("An Earth Engine project is required")
        if self.mode == "local":
            # Local download processes resolve existing EE/ADC credentials
            # themselves. Planning stays offline and never triggers login.
            return AuthBinding(
                owner_hash=owner_hash("local-user"),
                auth_source="auto",
                credential_handle=None,
                credential_version="local-existing",
                project_fingerprint=project_fingerprint(project),
            )
        subject = self._subject(session)
        if not subject or self.vault is None:
            raise PermissionError("Google/Earth Engine login is required")
        try:
            record = self.vault.load_owner(subject)
        except KeyError as exc:
            raise PermissionError("Stored authorization is missing or revoked") from exc
        credentials = credentials_from_payload(record.payload)
        try:
            import ee

            ee.Initialize(credentials=credentials, project=project)
        except Exception as exc:
            raise CredentialVerificationError(
                "The selected Google account cannot initialize Earth Engine with this project"
            ) from exc
        # Persist a refreshed access token if the auth library changed it.
        refreshed = credentials_payload(credentials)
        if refreshed != record.payload:
            record = self.vault.put(subject, refreshed)
        return AuthBinding(
            owner_hash=record.owner_hash,
            auth_source="web",
            credential_handle=record.handle,
            credential_version=f"vault_{record.version}",
            project_fingerprint=project_fingerprint(project),
        )

    def current_binding(
        self, session: MutableMapping[str, Any], project: str, *, verify: bool = False
    ) -> AuthBinding:
        if verify or self.mode == "local":
            return self.bind_project(session, project)
        subject = self._subject(session)
        if not subject or self.vault is None:
            raise PermissionError("Google/Earth Engine login is required")
        record = self.vault.load_owner(subject)
        return AuthBinding(
            owner_hash=record.owner_hash,
            auth_source="web",
            credential_handle=record.handle,
            credential_version=f"vault_{record.version}",
            project_fingerprint=project_fingerprint(project),
        )
