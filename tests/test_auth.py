from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aef_grits import auth


def _resolved(source: str):
    return SimpleNamespace(source=source)


def test_auto_prefers_explicit_adc_and_fails_closed(monkeypatch, tmp_path):
    missing = tmp_path / "missing-adc.json"
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(missing))
    monkeypatch.setattr(auth, "_resolve_adc", lambda project: _resolved("adc"))
    monkeypatch.setattr(
        auth,
        "_resolve_earthengine",
        lambda project: pytest.fail("must not fall back to Earth Engine credentials"),
    )
    assert auth.resolve_auth(source="auto", project="gee-test").source == "adc"


def test_auto_prefers_existing_earth_engine_user_credentials(monkeypatch, tmp_path):
    path = tmp_path / "credentials"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setattr(auth, "earth_engine_credentials_path", lambda: path)
    monkeypatch.setattr(auth, "_resolve_earthengine", lambda project: _resolved("earthengine"))
    monkeypatch.setattr(
        auth, "_resolve_adc", lambda project: pytest.fail("ADC should not be selected")
    )
    assert auth.resolve_auth(source="auto", project="gee-test").source == "earthengine"


def test_explicit_source_never_falls_back(monkeypatch):
    monkeypatch.setattr(
        auth,
        "_resolve_adc",
        lambda project: (_ for _ in ()).throw(auth.CredentialsNotFoundError("invalid ADC")),
    )
    monkeypatch.setattr(
        auth,
        "_resolve_earthengine",
        lambda project: pytest.fail("explicit ADC must not fall back"),
    )
    with pytest.raises(auth.CredentialsNotFoundError, match="invalid ADC"):
        auth.resolve_auth(source="adc", project="gee-test")


def test_public_auth_summary_contains_no_project_or_token():
    resolved = auth.ResolvedAuth(
        source="earthengine",
        project="gee-test-private",
        credentials=object(),
        credential_type="Credentials",
        credential_version="cred_1",
        principal_fingerprint="principal_1",
        project_fingerprint="project_1",
    )
    summary = resolved.public_summary()
    assert "project" not in summary
    assert "token" not in str(summary).lower()
    assert summary["project_fingerprint"] == "project_1"


def test_initialize_passes_resolved_credentials_explicitly(monkeypatch):
    calls = []
    credentials = object()
    resolved = auth.ResolvedAuth(
        source="adc",
        project="gee-test",
        credentials=credentials,
        credential_type="Credentials",
        credential_version="v1",
        principal_fingerprint="p1",
        project_fingerprint="g1",
    )
    monkeypatch.setattr(auth, "resolve_auth", lambda **kwargs: resolved)
    fake_ee = SimpleNamespace(Initialize=lambda **kwargs: calls.append(kwargs))
    monkeypatch.setitem(__import__("sys").modules, "ee", fake_ee)
    module, returned = auth.initialize_earth_engine(project="gee-test")
    assert module is fake_ee
    assert returned is resolved
    assert calls == [{"credentials": credentials, "project": "gee-test"}]


def test_discovery_does_not_return_credential_contents(monkeypatch, tmp_path):
    credential = tmp_path / "credentials"
    credential.write_text('{"refresh_token":"do-not-return"}', encoding="utf-8")
    monkeypatch.setattr(auth, "earth_engine_credentials_path", lambda: credential)
    result = [item.as_dict() for item in auth.discover_credentials()]
    assert "do-not-return" not in str(result)
