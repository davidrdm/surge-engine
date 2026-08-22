"""Bearer-token authentication.

A single static token read from the environment, checked with a constant-time
comparison. Per-user keys in the database were rejected as over-engineering for
"a small number of end users" on a local deployment — but the token is the only
thing between an unauthenticated caller and a list of named facilities with
timings attached, so it is checked properly rather than with `==`.

Two rules the deployment depends on:

  * The server binds 127.0.0.1. Binding 0.0.0.0 needs TLS and a real identity
    layer in front, because this token travels in a header in clear text.
  * A missing or empty SURGE_API_TOKEN fails every request rather than
    disabling the check. An auth layer that switches itself off when
    unconfigured is worse than none, because it looks like one.

**The scheme is declared, not just enforced (9.3 / issue #12).** Enforcement was
always correct; the contract said nothing about it. `docs/api/openapi.json` had
no `components.securitySchemes` and no operation carried a `security` block, so
a client generated from the artifact omitted the header and failed every
operational request with a 401 it had no way to anticipate. That is a pure
interoperability defect: the server was right and unusable.

The fix is to route enforcement through FastAPI's own `HTTPBearer` primitive so
the declaration is a consequence of the check rather than prose written beside
it. `authenticated` is the dependency routes use; `require_token` stays for the
one route — `/v1/healthz` — that authenticates on some request paths and not
others, and must therefore not declare the requirement unconditionally.
"""
from __future__ import annotations

import os
import secrets
from typing import Any, Mapping

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

#: Sent on a 401 so a client knows what scheme to use.
_CHALLENGE = {"WWW-Authenticate": "Bearer"}

#: The declared scheme. `auto_error=False` because FastAPI's own failure is a
#: bare 403 with no `WWW-Authenticate` header — this API answers 401 with a
#: challenge, and 503 when no token is configured at all, and those
#: distinctions are the documented contract. The primitive is here for what it
#: declares; the decision stays in `_verify`.
bearer_scheme = HTTPBearer(
    scheme_name="BearerToken",
    description=(
        "Static bearer token, read by the server from the environment variable "
        "named in `api.token_env` (default `SURGE_API_TOKEN`). Required by "
        "every operation except `GET /v1/healthz`, which is anonymous for "
        "liveness but requires the token when called with `?deep=true`."
    ),
    auto_error=False,
)


def configured_token(config: Mapping[str, Any]) -> str:
    """The expected token, from the variable named in config.

    Follows the api_key_env convention used for every other credential: the
    config holds the NAME of an environment variable, never the value.
    """
    var = (config.get("api") or {}).get("token_env", "SURGE_API_TOKEN")
    return os.environ.get(var, "")


def _verify(expected: str, presented: str | None) -> None:
    """The decision, in one place, whoever parsed the header.

    Order matters: an unconfigured server answers 503 even to a caller with no
    token at all, because "we cannot authenticate you" and "you are not
    authenticated" are different operational problems and only one of them is
    the client's to fix.
    """
    if not expected:
        raise HTTPException(
            503,
            "SURGE_API_TOKEN is not set; the API refuses to serve without "
            "authentication configured.",
            headers=_CHALLENGE,
        )
    if not presented:
        raise HTTPException(401, "Bearer token required.", headers=_CHALLENGE)
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(401, "Invalid token.", headers=_CHALLENGE)


def require_token(request: Request) -> None:
    """Imperative check, for a route that authenticates only on some paths.

    `/v1/healthz` calls this from inside its handler when `deep=true`. It is
    not a dependency and deliberately declares nothing: an operation whose
    schema said "bearer required" would make a generated client send a token
    for plain liveness, and the anonymity of the cheap check is a property this
    deployment wants to keep.
    """
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    _verify(request.app.state.api_token,
            presented.strip() if scheme.lower() == "bearer" else None)


def authenticated(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    """FastAPI dependency for every protected operation.

    Taking the credential through `bearer_scheme` rather than reading the
    header directly is what puts `securitySchemes` in the generated document
    and `security` on each operation that depends on this. The declaration is
    therefore a consequence of enforcing the rule, and cannot drift away from
    it the way a hand-written note beside the artifact did.
    """
    _verify(request.app.state.api_token,
            credentials.credentials.strip() if credentials else None)
