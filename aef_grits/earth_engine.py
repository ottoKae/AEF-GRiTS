"""Shared Earth Engine expressions for direct AEF retrieval."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
import http.client
import random
import socket
import ssl
import time
from typing import Any, Callable


DATASET = "GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL"
AEF_BANDS = tuple(f"A{index:02d}" for index in range(64))
AEF_RESOLUTION_M = 10.0
AEF_FIRST_YEAR = 2017
AEF_LAST_YEAR = 2025
HIGH_VOLUME_URL = "https://earthengine-highvolume.googleapis.com"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class RequestPolicy:
    """Explicit retry and deadline policy for synchronous Earth Engine calls."""

    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    max_retries: int = 6
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0
    jitter_fraction: float = 0.20

    def validate(self) -> "RequestPolicy":
        if self.timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        if self.max_retries < 0:
            raise ValueError("max retries must be non-negative")
        if self.base_backoff_seconds < 0 or self.max_backoff_seconds < 0:
            raise ValueError("retry backoff must be non-negative")
        if not 0 <= self.jitter_fraction <= 1:
            raise ValueError("retry jitter fraction must be between 0 and 1")
        return self

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class EarthEngineRequestError(RuntimeError):
    """A classified terminal request error with resumability metadata."""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        classification: str,
        attempts: int,
        cause: Exception,
    ):
        super().__init__(message)
        self.operation = operation
        self.classification = classification
        self.attempts = attempts
        self.cause = cause

    @property
    def resumable(self) -> bool:
        return self.classification in {"retryable", "timeout"}


class EarthEngineResponseError(RuntimeError):
    """A transient response-body/schema failure safe to retry."""


def _http_status(exc: Exception) -> int | None:
    for candidate in (exc, getattr(exc, "response", None), getattr(exc, "resp", None)):
        if candidate is None:
            continue
        for name in ("status_code", "status", "code"):
            value = getattr(candidate, name, None)
            try:
                if value is not None and not callable(value):
                    return int(value)
            except (TypeError, ValueError):
                pass
    return None


def classify_request_error(exc: Exception) -> str:
    """Return ``timeout``, ``retryable`` or ``permanent`` for a request error."""

    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if "timeout" in name or "timed out" in message or "deadline exceeded" in message:
        return "timeout"
    status = _http_status(exc)
    if status in {408, 409, 425, 429} or (status is not None and 500 <= status <= 599):
        return "retryable"
    permanent_tokens = (
        "authentication",
        "authenticate",
        "invalid credential",
        "permission denied",
        "not authorized",
        "unauthorized",
        "invalid argument",
        "unknown band",
        "schema",
        "not found",
        "certificate verify failed",
        "hostname mismatch",
    )
    if status in {400, 401, 403, 404} or any(token in message for token in permanent_tokens):
        return "permanent"
    if isinstance(
        exc,
        (
            ConnectionError,
            EarthEngineResponseError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
            ssl.SSLEOFError,
        ),
    ):
        return "retryable"
    retryable_names = (
        "chunkedencodingerror",
        "connectionerror",
        "incompleteread",
        "protocolerror",
        "readtimeouterror",
        "remotedisconnected",
        "responsevalidationerror",
        "ssleoferror",
    )
    if any(token in name for token in retryable_names):
        return "retryable"
    retryable_tokens = (
        "connection reset",
        "connection aborted",
        "connection refused",
        "temporarily unavailable",
        "internal error",
        "backend error",
        "rate limit",
        "too many requests",
        "quota exceeded",
        "service unavailable",
        "remote end closed",
        "connection broken",
        "eof occurred in violation of protocol",
        "incomplete read",
        "peer closed connection",
        "response ended prematurely",
        "tls/ssl connection has been closed",
        "unexpected eof while reading",
    )
    if any(token in message for token in retryable_tokens):
        return "retryable"
    # Unknown failures are not retried automatically. This prevents malformed
    # expressions and local programming errors from being repeated for minutes.
    return "permanent"


def execute_with_retry(
    operation: str,
    request: Callable[[], Any],
    policy: RequestPolicy,
    *,
    event: Callable[[str, dict[str, Any]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    random_value: Callable[[], float] = random.random,
) -> Any:
    """Execute one request with explicit classification and bounded retries."""

    policy.validate()
    for attempt in range(1, policy.max_retries + 2):
        if event:
            event("request_started", {"operation": operation, "attempt": attempt})
        started = time.perf_counter()
        try:
            result = request()
            if event:
                event(
                    "request_succeeded",
                    {
                        "operation": operation,
                        "attempt": attempt,
                        "elapsed_seconds": time.perf_counter() - started,
                    },
                )
            return result
        except Exception as exc:
            classification = classify_request_error(exc)
            elapsed = time.perf_counter() - started
            can_retry = classification in {"retryable", "timeout"} and attempt <= policy.max_retries
            payload = {
                "operation": operation,
                "attempt": attempt,
                "classification": classification,
                "elapsed_seconds": elapsed,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            }
            if can_retry:
                base = min(
                    policy.max_backoff_seconds,
                    policy.base_backoff_seconds * (2 ** (attempt - 1)),
                )
                jitter = base * policy.jitter_fraction * (2 * random_value() - 1)
                delay = max(0.0, min(policy.max_backoff_seconds, base + jitter))
                if event:
                    event("request_retry", {**payload, "retry_delay_seconds": delay})
                sleep(delay)
                continue
            event_name = "request_timeout" if classification == "timeout" else "request_failed"
            if event:
                event(event_name, payload)
            raise EarthEngineRequestError(
                f"Earth Engine {operation} failed after {attempt} attempt(s) "
                f"[{classification}]: {exc}",
                operation=operation,
                classification=classification,
                attempts=attempt,
                cause=exc,
            ) from exc
    raise AssertionError("unreachable retry loop")


def initialize(
    project: str,
    *,
    high_volume: bool = False,
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
):
    """Initialize Earth Engine and return its imported module."""
    import ee

    kwargs = {"project": project}
    if high_volume:
        kwargs["opt_url"] = HIGH_VOLUME_URL
    ee.Initialize(**kwargs)
    if request_timeout_seconds <= 0:
        raise ValueError("request_timeout_seconds must be positive")
    ee.data.setDeadline(float(request_timeout_seconds) * 1000.0)
    # AEF-GRiTS owns retry classification, events and backoff. Disable the
    # client's hidden retry loop so retry counts and deadlines are auditable.
    ee.data.setMaxRetries(0)
    return ee


def annual_image(year: int, bounds=None, *, renamed: bool = False):
    """Build one annual 64-band AEF mosaic."""
    import ee

    collection = (
        ee.ImageCollection(DATASET)
        .filterDate(f"{int(year)}-01-01", f"{int(year) + 1}-01-01")
        .select(list(AEF_BANDS))
    )
    if bounds is not None:
        collection = collection.filterBounds(bounds)
    image = collection.mosaic().toFloat()
    if renamed:
        image = image.rename(
            [f"aef{int(year)}_center_{band}" for band in AEF_BANDS]
        )
    return image


def multiyear_image(years: Sequence[int], bounds=None):
    """Concatenate annual mosaics into one uniquely named wide image."""
    import ee

    normalized = sorted(set(int(year) for year in years))
    if not normalized:
        raise ValueError("At least one year is required")
    return ee.Image.cat(
        [annual_image(year, bounds, renamed=True) for year in normalized]
    )


def feature_columns(years: Sequence[int]) -> list[str]:
    return [
        f"aef{int(year)}_center_{band}"
        for year in sorted(set(int(value) for value in years))
        for band in AEF_BANDS
    ]
