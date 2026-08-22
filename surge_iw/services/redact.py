"""Credential scrubbing for anything that leaves the process.

Three consumers write text that a human — or an AI assistant reading tool output
— will later see: agent_log.message/extra_json, api_calls.error_message, and
raw_results.payload_json. An HTTP client exception can carry a request URL with
a key in the query string, and a misconfigured connector can echo a header. Once
such a string is printed it is effectively published: into a log file, into a
database that gets copied around, or into an assistant's context window and from
there to a model provider.

Two layers, because neither is sufficient alone:

  * Exact-value redaction. Credentials read from the environment at startup are
    registered here, and any occurrence of one is replaced. This is the reliable
    layer — it catches a key no matter how it was embedded.

  * Pattern redaction. Authorization headers, X-API-Key, x-rapidapi-key, and
    api_key/token query parameters are matched structurally. This catches
    credentials the process never held, such as one echoed back by a provider.

Also strips vendor session material that is not a credential but behaves like
one: Priceline's checkoutUrl and detailsKey embed a booking refCode and session
tokens, and they arrive inside every rental-car response.
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable

PLACEHOLDER = "***REDACTED***"

# Below this length a "secret" is more likely to be a common substring, and
# redacting it would corrupt legitimate text without protecting anything.
MIN_SECRET_LENGTH = 8

# JSON keys whose values are dropped wholesale, regardless of content.
SENSITIVE_KEYS: frozenset[str] = frozenset({
    "authorization", "x-api-key", "x-rapidapi-key", "api_key", "apikey",
    "api-key", "token", "access_token", "refresh_token", "password", "secret",
    "client_secret", "bearer",
    # Not credentials, but session-bearing and present in every Priceline
    # rental-car offer.
    "checkouturl", "detailskey",
})

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Authorization: Bearer <token>
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)(\S+)"),
    # X-API-Key: <key> / x-rapidapi-key: <key>
    re.compile(r"(?i)((?:x-)?(?:api|rapidapi)[-_]?key\s*[:=]\s*)(\S+)"),
    # ?api_key=... or &token=... in a URL
    re.compile(r"(?i)([?&](?:api_?key|apikey|token|access_token|key)=)([^&\s\"']+)"),
    # Bearer token appearing on its own
    re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._\-]{16,})"),
)


class Redactor:
    """Holds the live credential set and scrubs text and payloads."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, value: str | None) -> None:
        """Register a literal secret value to be scrubbed on sight."""
        if value and len(value) >= MIN_SECRET_LENGTH:
            self._secrets.add(value)

    def register_from_env(self, var_names: Iterable[str]) -> list[str]:
        """Register values of the named environment variables.

        Returns the names that were found, never the values — the return value
        is safe to log.
        """
        found: list[str] = []
        for name in var_names:
            value = os.environ.get(name)
            if value:
                self.register(value)
                found.append(name)
        return found

    def register_from_config(self, config: dict[str, Any]) -> list[str]:
        """Register every credential the config points at.

        Walks the config for *_env / *_key_env fields, which hold the NAMES of
        environment variables, and registers whatever those variables contain.
        """
        names: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(value, str) and (
                        key.endswith("_env") or key.endswith("_key_env")
                    ):
                        names.add(value)
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(config)
        return self.register_from_env(sorted(names))

    @property
    def secret_count(self) -> int:
        """How many secrets are registered. Safe to log; the values are not."""
        return len(self._secrets)

    # ------------------------------------------------------------------
    # Scrubbing
    # ------------------------------------------------------------------

    def text(self, value: str | None) -> str | None:
        """Scrub a string: exact secrets first, then structural patterns."""
        if not value:
            return value
        result = value
        # Longest first, so a key that contains another registered value as a
        # substring is replaced whole rather than leaving a fragment behind.
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in result:
                result = result.replace(secret, PLACEHOLDER)
        for pattern in _PATTERNS:
            result = pattern.sub(lambda m: m.group(1) + PLACEHOLDER, result)
        return result

    def payload(self, value: Any, _depth: int = 0) -> Any:
        """Recursively scrub a decoded JSON structure.

        Values under a sensitive key are replaced outright; everything else is
        scrubbed as text. Depth is bounded so a pathological or cyclic structure
        cannot hang the collector.
        """
        if _depth > 12:
            return value
        if isinstance(value, dict):
            cleaned: dict[Any, Any] = {}
            for key, item in value.items():
                if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
                    cleaned[key] = PLACEHOLDER
                else:
                    cleaned[key] = self.payload(item, _depth + 1)
            return cleaned
        if isinstance(value, (list, tuple)):
            return [self.payload(item, _depth + 1) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value

    def exception(self, exc: BaseException) -> str:
        """A scrubbed one-line description of an exception.

        Connector failures are recorded in api_calls.error_message and surfaced
        in the API. An httpx error can carry the full request URL, which for
        some providers includes the key as a query parameter.
        """
        return self.text(f"{type(exc).__name__}: {exc}") or type(exc).__name__


# Module-level default. Connectors and the database layer use this instance;
# tests construct their own to stay isolated.
_default = Redactor()


def default_redactor() -> Redactor:
    return _default


def install(config: dict[str, Any]) -> int:
    """Register every credential the config references. Returns the count.

    Call once at startup, before any connector is constructed.
    """
    _default.register_from_config(config)
    return _default.secret_count


def redact_text(value: str | None) -> str | None:
    return _default.text(value)


def redact_payload(value: Any) -> Any:
    return _default.payload(value)


def redact_exception(exc: BaseException) -> str:
    return _default.exception(exc)
