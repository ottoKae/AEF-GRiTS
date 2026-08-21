"""Deterministic, non-interactive credential resolution for AEF-GRiTS.

Download commands only resolve credentials that already exist.  Interactive
authentication is deliberately confined to :mod:`aef_grits.auth_cli`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal


AuthSource = Literal["auto", "earthengine", "adc", "web"]
AUTH_SOURCES: tuple[AuthSource, ...] = ("auto", "earthengine", "adc", "web")
EARTH_ENGINE_SCOPES = (
    "https://www.googleapis.com/auth/earthengine",
    "https://www.googleapis.com/auth/cloud-platform",
)


class AuthenticationError(RuntimeError):
    """Base class for terminal authentication/configuration failures."""


class CredentialsNotFoundError(AuthenticationError):
    """No credentials exist for the selected non-interactive source."""


class ProjectRequiredError(AuthenticationError):
    """No Earth Engine quota project could be resolved."""


class CredentialVerificationError(AuthenticationError):
    """Credentials or project permissions failed online verification."""


@dataclass(frozen=True)
class CredentialCandidate:
    source: str
    available: bool
    configured_by: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResolvedAuth:
    source: str
    project: str
    credentials: Any
    credential_type: str
    credential_version: str
    principal_fingerprint: str
    project_fingerprint: str

    def public_summary(self) -> dict[str, str]:
        return {
            "source": self.source,
            "credential_type": self.credential_type,
            "credential_version": self.credential_version,
            "principal_fingerprint": self.principal_fingerprint,
            "project_fingerprint": self.project_fingerprint,
        }


def _fingerprint(value: str, *, prefix: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def project_fingerprint(project: str) -> str:
    return _fingerprint(project, prefix="project")


def earth_engine_credentials_path() -> Path:
    """Return the Earth Engine client's platform-aware persistent path."""
    try:
        import ee.oauth

        return Path(ee.oauth.get_credentials_path()).expanduser().resolve()
    except (ImportError, AttributeError, OSError):
        return (Path.home() / ".config" / "earthengine" / "credentials").resolve()


def _adc_well_known_path() -> Path:
    try:
        from google.auth import _cloud_sdk

        return Path(_cloud_sdk.get_application_default_credentials_path()).resolve()
    except (ImportError, AttributeError, OSError):
        return (
            Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        ).resolve()


def discover_credentials() -> list[CredentialCandidate]:
    explicit_adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    candidates = [
        CredentialCandidate(
            "adc",
            bool(explicit_adc and Path(explicit_adc).expanduser().is_file()),
            "GOOGLE_APPLICATION_CREDENTIALS",
        ),
        CredentialCandidate(
            "earthengine",
            earth_engine_credentials_path().is_file(),
            "Earth Engine persistent credentials",
        ),
        CredentialCandidate(
            "adc",
            _adc_well_known_path().is_file(),
            "local ADC well-known file",
        ),
        # Metadata credentials cannot be detected without a network request.
        CredentialCandidate("adc", False, "attached service account (online discovery)"),
    ]
    if os.environ.get("AEF_GRITS_CREDENTIAL_HANDLE"):
        candidates.append(CredentialCandidate("web", True, "opaque Web credential handle"))
    return candidates


def _project_from_json(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    for key in ("project", "quota_project_id", "project_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return None


def _file_version(path: Path) -> str:
    try:
        stat = path.stat()
        identity = f"{path}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        identity = str(path)
    return _fingerprint(identity, prefix="cred")


def _principal(credentials: Any, source: str) -> str:
    for name in ("service_account_email", "_service_account_email", "account"):
        value = str(getattr(credentials, name, "") or "").strip()
        if value:
            return _fingerprint(value, prefix="principal")
    return _fingerprint(f"{source}:{type(credentials).__module__}.{type(credentials).__name__}", prefix="principal")


def _resolve_earthengine(project: str | None) -> ResolvedAuth:
    path = earth_engine_credentials_path()
    if not path.is_file():
        raise CredentialsNotFoundError(
            "Earth Engine credentials were not found. Run `aef-grits-auth login "
            "--source earthengine` before starting a download."
        )
    try:
        import ee

        credentials = ee.data.get_persistent_credentials()
    except Exception as exc:
        raise CredentialsNotFoundError(
            "Earth Engine persistent credentials exist but cannot be loaded. "
            "Run `aef-grits-auth login --source earthengine --force`."
        ) from exc
    resolved_project = project or _project_from_json(path)
    if not resolved_project:
        raise ProjectRequiredError(
            "No Earth Engine project is configured. Pass --project, set a private "
            "AEF_GRITS_PROJECT, or run `earthengine set_project YOUR_GEE_PROJECT`."
        )
    return ResolvedAuth(
        source="earthengine",
        project=resolved_project,
        credentials=credentials,
        credential_type=type(credentials).__name__,
        credential_version=_file_version(path),
        principal_fingerprint=_principal(credentials, "earthengine"),
        project_fingerprint=project_fingerprint(resolved_project),
    )


def _resolve_adc(project: str | None) -> ResolvedAuth:
    try:
        import google.auth

        credentials, discovered_project = google.auth.default(scopes=EARTH_ENGINE_SCOPES)
    except Exception as exc:
        raise CredentialsNotFoundError(
            "Application Default Credentials were not found or are invalid. Run "
            "`aef-grits-auth login --source adc` before starting a download."
        ) from exc
    resolved_project = (
        project
        or str(discovered_project or "").strip()
        or str(getattr(credentials, "quota_project_id", "") or "").strip()
    )
    if not resolved_project:
        raise ProjectRequiredError(
            "ADC is available but no quota project is configured. Pass --project or "
            "configure a private local quota project."
        )
    explicit = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    version_path = Path(explicit).expanduser() if explicit else _adc_well_known_path()
    return ResolvedAuth(
        source="adc",
        project=resolved_project,
        credentials=credentials,
        credential_type=type(credentials).__name__,
        credential_version=_file_version(version_path),
        principal_fingerprint=_principal(credentials, "adc"),
        project_fingerprint=project_fingerprint(resolved_project),
    )


def _resolve_web(project: str | None, credential_handle: str | None) -> ResolvedAuth:
    from .auth_vault import CredentialVault, credentials_from_payload

    handle = credential_handle or os.environ.get("AEF_GRITS_CREDENTIAL_HANDLE", "").strip()
    vault_path = os.environ.get("AEF_GRITS_AUTH_VAULT", "").strip()
    key_path = os.environ.get("AEF_GRITS_AUTH_VAULT_KEY", "").strip()
    if not handle or not vault_path or not key_path:
        raise CredentialsNotFoundError(
            "The Web worker has no credential handle or vault configuration. "
            "Reauthenticate in the Web application and resubmit the plan."
        )
    record = CredentialVault(Path(vault_path), Path(key_path)).load(handle)
    credentials = credentials_from_payload(record.payload)
    resolved_project = project or str(record.payload.get("project") or "").strip()
    if not resolved_project:
        raise ProjectRequiredError("The Web credential is not bound to an Earth Engine project")
    return ResolvedAuth(
        source="web",
        project=resolved_project,
        credentials=credentials,
        credential_type=type(credentials).__name__,
        credential_version=f"vault_{record.version}",
        principal_fingerprint=record.owner_hash,
        project_fingerprint=project_fingerprint(resolved_project),
    )


def resolve_auth(
    *,
    source: str | None = None,
    project: str | None = None,
    credential_handle: str | None = None,
) -> ResolvedAuth:
    """Resolve existing credentials without ever starting an auth flow."""
    selected = str(source or os.environ.get("AEF_GRITS_AUTH_SOURCE", "auto")).strip().lower()
    if selected not in AUTH_SOURCES:
        raise AuthenticationError(
            f"Unknown auth source {selected!r}; choose from {', '.join(AUTH_SOURCES)}"
        )
    explicit_project = str(project or os.environ.get("AEF_GRITS_PROJECT", "")).strip() or None
    if selected == "earthengine":
        return _resolve_earthengine(explicit_project)
    if selected == "adc":
        return _resolve_adc(explicit_project)
    if selected == "web":
        return _resolve_web(explicit_project, credential_handle)

    # Explicit ADC configuration is an operator decision.  If it is invalid,
    # fail closed rather than silently executing under another user.
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip():
        return _resolve_adc(explicit_project)
    if earth_engine_credentials_path().is_file():
        return _resolve_earthengine(explicit_project)
    return _resolve_adc(explicit_project)


def initialize_earth_engine(
    *,
    source: str | None = None,
    project: str | None = None,
    credential_handle: str | None = None,
    opt_url: str | None = None,
) -> tuple[Any, ResolvedAuth]:
    """Resolve credentials and initialize Earth Engine without interaction."""
    resolved = resolve_auth(
        source=source, project=project, credential_handle=credential_handle
    )
    try:
        import ee

        kwargs: dict[str, Any] = {
            "credentials": resolved.credentials,
            "project": resolved.project,
        }
        if opt_url:
            kwargs["opt_url"] = opt_url
        ee.Initialize(**kwargs)
    except Exception as exc:
        raise CredentialVerificationError(
            "Earth Engine rejected the selected credentials or project. Authentication "
            "and network retries are intentionally separate; verify access explicitly."
        ) from exc
    return ee, resolved


def refresh_credentials(credentials: Any) -> None:
    """Refresh one credential explicitly; used by status/verification workflows."""
    try:
        from google.auth.transport.requests import Request

        credentials.refresh(Request())
    except Exception as exc:
        raise CredentialVerificationError("Credential refresh failed") from exc


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
