"""Shared HTTP foundation for the four external data connectors.

Derived from surge/connectors/base.py, with one contract inverted.

The old connectors did this:

    except Exception as exc:
        logger.error(...)
        return []

That is the most dangerous line in the original codebase. An expired token, a
402 on exhausted credits, a 429, a network blip — every one of them became "no
military flights inbound", indistinguishable from a genuine, correct empty
result. In a warning system, manufacturing negative evidence is the failure
direction that gets people hurt.

So every connector here FAILS LOUD. A request that does not succeed raises a
typed ConnectorError. CollectionAgent catches it, marks the query FAILED, and
correlation then reports the source family as a coverage gap rather than as an
absence of signal — which caps confidence below HIGH and attaches a caveat.

Distinct exception types exist because the operator responses differ: an
AuthError needs a new key, a PaymentRequiredError needs credits, a RateLimitError
means the limiter is misconfigured, and an UpstreamError means wait.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import httpx

from ..services.ratelimit import RateLimiter
from ..services.redact import redact_exception, redact_text

# Callback invoked once per HTTP attempt, success or failure. CollectionAgent
# wires this to BudgetGuard.record so that spend accounting cannot be forgotten
# at a call site.
CallRecorder = Callable[..., Any]


class ConnectorError(RuntimeError):
    """Base for every connector failure. Never swallowed inside a connector."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        endpoint: str = "",
        status_code: int | None = None,
    ) -> None:
        super().__init__(redact_text(message) or message)
        self.provider = provider
        self.endpoint = endpoint
        self.status_code = status_code


class AuthError(ConnectorError):
    """401 or 403. The credential is wrong, expired, or wrong-environment.

    FR24 returns this when a sandbox token is used against a production
    endpoint, which is a configuration mistake worth naming precisely.
    """


class PaymentRequiredError(ConnectorError):
    """402. Credits or quota exhausted at the provider, not locally.

    Distinct from a local BudgetGuard refusal: this one means our ledger drifted
    optimistic and the provider disagreed.
    """


class RateLimitError(ConnectorError):
    """429. The limiter failed to prevent a breach.

    Deliberately not retried inside the connector. A 429 means local pacing is
    misconfigured, and silently retrying would hide that while still losing the
    collection window.
    """

    def __init__(self, *args: Any, retry_after: float | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class UpstreamError(ConnectorError):
    """5xx. The provider is unwell. Retried a bounded number of times first."""


class TransportError(ConnectorError):
    """Network failure, DNS, TLS, or timeout."""


class PlatformUnavailableError(ConnectorError):
    """A requested upstream platform was skipped rather than searched.

    An aggregator fans one request out to several vendors, and a leg can be
    skipped for a reason that has nothing to do with availability — a missing
    parameter, a vendor outage, a plan restriction. The response still arrives
    as HTTP 200 with a shorter list.

    That is the fail-loud contract's exact adversary: it looks like "we searched
    and found less" when it means "we did not search". Raised so the query is
    recorded FAILED and correlation reads a coverage gap.
    """


class SchemaError(ConnectorError):
    """The response parsed as JSON but did not have the expected shape.

    Raised rather than defaulting a missing field, whenever defaulting would
    fabricate evidence. A missing availability count must never become zero,
    because zero reads as scarcity and scarcity is what this system alerts on.
    """


@dataclass
class ApiResponse:
    """A successful HTTP response plus the metadata the ledger needs."""

    data: Any
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    latency_ms: int = 0
    records_returned: int = 0


class BaseConnector(ABC):
    """Common HTTP behaviour: pacing, typed errors, and call accounting."""

    #: Billing provider, matching db.enums.PROVIDERS.
    provider: str = ""
    #: Bounded retries for transient failures only.
    max_retries: int = 2
    retry_backoff_s: float = 1.0

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        timeout: float = 30.0,
        limiter: RateLimiter | None = None,
        on_call: CallRecorder | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            # Failing at construction beats failing at request time: an
            # unauthenticated connector that returns empty results is exactly the
            # false-negative this class exists to prevent.
            raise ConnectorError(
                f"{type(self).__name__} requires an API key",
                provider=self.provider,
            )
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.limiter = limiter
        self._on_call = on_call
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    # ------------------------------------------------------------------
    # Subclass interface
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable connector name, for logs and health output."""

    @abstractmethod
    def auth_headers(self) -> dict[str, str]:
        """Headers carrying the credential. Never logged."""

    def health_check(self) -> dict[str, Any]:
        """Cheapest call that proves the credential works.

        Default implementation reports unknown; connectors with a free endpoint
        (Staying's /account, FR24's /usage) override it. Returns a dict rather
        than a bool so the operator learns *why* a connector is unhealthy.
        """
        return {"provider": self.provider, "healthy": None,
                "detail": "no health endpoint implemented"}

    # ------------------------------------------------------------------
    # Request plumbing
    # ------------------------------------------------------------------

    def _request(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        method: str = "GET",
        count_records: Callable[[Any], int] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        iteration_id: int | None = None,
        query_id: int | None = None,
    ) -> ApiResponse:
        """Issue one paced, accounted, fail-loud request.

        `count_records` computes the billable record count from the parsed body.
        It is passed in rather than computed by the caller so that accounting
        happens in exactly one place, including on the failure paths — FR24 bills
        a flat credit even for an empty response, and a failed call still
        consumed a request against most plans.
        """
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json", **self.auth_headers()}
        if extra_headers:
            headers.update(extra_headers)

        attempt = 0
        while True:
            attempt += 1
            if self.limiter is not None:
                self.limiter.acquire()

            started = time.monotonic()
            status: int | None = None
            error_message: str | None = None
            records = 0
            try:
                response = self._client.request(
                    method, url, params=dict(params or {}), headers=headers,
                    timeout=self.timeout,
                )
                status = response.status_code
                latency_ms = int((time.monotonic() - started) * 1000)

                if status >= 400:
                    error_message = self._error_text(response)
                    exc = self._exception_for(status, path, error_message,
                                              response.headers)
                    if self._should_retry(status) and attempt <= self.max_retries:
                        self._record(path, status, records, latency_ms,
                                     error_message, iteration_id, query_id)
                        self._sleep(self.retry_backoff_s * attempt)
                        continue
                    self._record(path, status, records, latency_ms,
                                 error_message, iteration_id, query_id)
                    raise exc

                data = self._parse_json(response, path)
                records = count_records(data) if count_records else 0
                self._record(path, status, records, latency_ms, None,
                             iteration_id, query_id)
                return ApiResponse(
                    data=data, status_code=status,
                    headers=dict(response.headers), latency_ms=latency_ms,
                    records_returned=records,
                )

            except ConnectorError:
                raise
            except httpx.HTTPError as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                error_message = redact_exception(exc)
                self._record(path, status, 0, latency_ms, error_message,
                             iteration_id, query_id)
                if attempt <= self.max_retries:
                    self._sleep(self.retry_backoff_s * attempt)
                    continue
                raise TransportError(
                    f"{self.provider} {path}: {error_message}",
                    provider=self.provider, endpoint=path,
                ) from exc

    def _should_retry(self, status: int) -> bool:
        """Retry transient server-side failures only.

        Not 429: a rate-limit breach means local pacing is wrong, and retrying
        into it hides the misconfiguration while still burning the window.
        Not 4xx: the request will not become valid by repeating it.
        """
        return status >= 500

    def _exception_for(
        self,
        status: int,
        path: str,
        detail: str,
        headers: Mapping[str, str],
    ) -> ConnectorError:
        prefix = f"{self.provider} {path} -> HTTP {status}"
        message = f"{prefix}: {detail}" if detail else prefix
        common = {"provider": self.provider, "endpoint": path,
                  "status_code": status}
        if status in (401, 403):
            return AuthError(
                f"{message} (check the credential, and whether a sandbox key "
                f"is being used against a production endpoint)", **common
            )
        if status == 402:
            return PaymentRequiredError(
                f"{message} (provider reports credits exhausted; the local "
                f"budget ledger has drifted optimistic)", **common
            )
        if status == 429:
            retry_after = headers.get("Retry-After") or headers.get("retry-after")
            return RateLimitError(
                f"{message} (rate limiter failed to prevent a breach)",
                retry_after=_as_float(retry_after), **common
            )
        if status >= 500:
            return UpstreamError(message, **common)
        return ConnectorError(message, **common)

    def _parse_json(self, response: httpx.Response, path: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise SchemaError(
                f"{self.provider} {path}: response was not valid JSON "
                f"({len(response.content)} bytes)",
                provider=self.provider, endpoint=path,
                status_code=response.status_code,
            ) from exc

    def _record(
        self,
        path: str,
        status: int | None,
        records: int,
        latency_ms: int,
        error_message: str | None,
        iteration_id: int | None,
        query_id: int | None,
    ) -> None:
        if self._on_call is None:
            return
        self._on_call(
            provider=self.provider,
            endpoint=path,
            http_status=status,
            records_returned=records,
            latency_ms=latency_ms,
            error_message=error_message,
            iteration_id=iteration_id,
            query_id=query_id,
        )

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        """A short, scrubbed description of an error body.

        Truncated because provider error pages can be entire HTML documents, and
        scrubbed because some providers echo the submitted key back in the error.
        """
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        if isinstance(payload, dict):
            detail = (
                payload.get("message")
                or payload.get("error")
                or payload.get("detail")
                or payload
            )
        else:
            detail = payload
        return (redact_text(str(detail)) or "")[:300]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def require(payload: Mapping[str, Any], key: str, *, context: str,
            provider: str, endpoint: str) -> Any:
    """Fetch a required field or raise SchemaError.

    Used for fields where a default would fabricate evidence. Everything read
    through this helper is a field whose absence must stop collection rather
    than quietly become zero.
    """
    if key not in payload or payload[key] is None:
        raise SchemaError(
            f"{provider} {endpoint}: {context} is missing required field "
            f"{key!r}. Refusing to default it — a missing availability count "
            f"would be scored as scarcity.",
            provider=provider, endpoint=endpoint,
        )
    return payload[key]
