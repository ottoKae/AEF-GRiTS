from __future__ import annotations

import http.client
import ssl
from types import SimpleNamespace

import pytest

from aef_grits.earth_engine import (
    EarthEngineRequestError,
    RequestPolicy,
    classify_request_error,
    execute_with_retry,
    initialize,
)


class StatusError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status_code = status


def test_request_error_classification_is_conservative():
    assert classify_request_error(TimeoutError("late")) == "timeout"
    assert classify_request_error(StatusError(429, "quota")) == "retryable"
    assert classify_request_error(StatusError(503, "backend")) == "retryable"
    assert classify_request_error(StatusError(403, "permission denied")) == "permanent"
    assert classify_request_error(ValueError("bad local expression")) == "permanent"


@pytest.mark.parametrize(
    "error",
    [
        http.client.IncompleteRead(b"partial", 100),
        http.client.RemoteDisconnected("remote end closed connection"),
        ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
        RuntimeError("HTTPS response ended prematurely"),
        RuntimeError("ProtocolError: connection broken by IncompleteRead"),
    ],
)
def test_truncated_https_responses_are_retryable(error):
    assert classify_request_error(error) == "retryable"


def test_certificate_verification_failure_is_permanent():
    assert (
        classify_request_error(RuntimeError("certificate verify failed"))
        == "permanent"
    )


def test_retry_policy_emits_attempts_and_bounded_delays():
    calls = []
    events = []
    delays = []

    def request():
        calls.append(1)
        if len(calls) < 3:
            raise StatusError(503, "service unavailable")
        return "ok"

    result = execute_with_retry(
        "test",
        request,
        RequestPolicy(max_retries=3, base_backoff_seconds=2, jitter_fraction=0),
        event=lambda name, payload: events.append((name, payload)),
        sleep=delays.append,
    )
    assert result == "ok"
    assert len(calls) == 3
    assert delays == [2, 4]
    assert [name for name, _ in events] == [
        "request_started",
        "request_retry",
        "request_started",
        "request_retry",
        "request_started",
        "request_succeeded",
    ]


def test_permanent_error_is_not_retried_when_raised():
    calls = []

    def request():
        calls.append(1)
        raise StatusError(403, "permission denied")

    with pytest.raises(EarthEngineRequestError) as captured:
        execute_with_retry("test", request, RequestPolicy(max_retries=6))
    assert calls == [1]
    assert captured.value.classification == "permanent"
    assert not captured.value.resumable


def test_timeout_exhaustion_is_resumable():
    calls = []

    def request():
        calls.append(1)
        raise TimeoutError("deadline exceeded")

    with pytest.raises(EarthEngineRequestError) as captured:
        execute_with_retry(
            "test",
            request,
            RequestPolicy(max_retries=1, base_backoff_seconds=0),
        )
    assert len(calls) == 2
    assert captured.value.classification == "timeout"
    assert captured.value.resumable


def test_initialize_sets_explicit_deadline_and_disables_hidden_retries(monkeypatch):
    calls = []
    fake_data = SimpleNamespace(
        setDeadline=lambda value: calls.append(("deadline", value)),
        setMaxRetries=lambda value: calls.append(("retries", value)),
    )
    fake_ee = SimpleNamespace(
        Initialize=lambda **kwargs: calls.append(("initialize", kwargs)),
        data=fake_data,
    )
    monkeypatch.setitem(__import__("sys").modules, "ee", fake_ee)
    assert initialize("project", high_volume=True, request_timeout_seconds=12) is fake_ee
    assert calls[0][0] == "initialize"
    assert calls[1:] == [("deadline", 12000.0), ("retries", 0)]
