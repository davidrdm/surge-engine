"""Contract hardening for clients that retry (8.2).

Three things a caller of a money-spending, long-running API needs and did not
have: a safe way to retry a request whose response it lost, a machine-readable
answer to "when should I try again", and a way to stop a run it no longer wants.

Kept out of `routes.py` because each is a rule about the *protocol* rather than
about I&W, and because the idempotency rules in particular are easier to argue
about — and to test — when they are not interleaved with orchestration.
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping

from fastapi import HTTPException
from fastapi.responses import JSONResponse

#: Header names. `Idempotency-Key` follows the IETF draft of the same name;
#: `Idempotent-Replay` is how a client can tell a replay from a fresh run,
#: which matters because the replayed body is byte-identical either way.
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replay"

#: Bounds on a client-supplied key. Long enough for a UUID, short enough that
#: the column cannot be used as free storage.
MIN_KEY_LENGTH = 8
MAX_KEY_LENGTH = 255


class RetryableError(HTTPException):
    """An HTTPException that tells the client when to come back.

    A bare 409 from a busy session is indistinguishable from a permanent
    conflict, so a well-behaved client either gives up or hammers. Both are
    wrong here: iterations are minutes long and the right move is always to
    wait and retry.
    """

    def __init__(self, status_code: int, detail: str, retry_after: int) -> None:
        super().__init__(status_code, detail,
                         headers={"Retry-After": str(int(retry_after))})
        self.retry_after = int(retry_after)


def validate_key(key: str | None) -> str | None:
    """Check a client-supplied key, or None if none was sent.

    Absent is allowed: idempotency is opt-in, because a CLI making one call by
    hand should not have to invent a key. What is not allowed is a key so short
    it is likely to collide with another client's.
    """
    if key is None:
        return None
    key = key.strip()
    if not key:
        return None
    if not (MIN_KEY_LENGTH <= len(key) <= MAX_KEY_LENGTH):
        raise HTTPException(
            422,
            f"{IDEMPOTENCY_HEADER} must be {MIN_KEY_LENGTH}-{MAX_KEY_LENGTH} "
            f"characters; got {len(key)}",
        )
    return key


def request_fingerprint(session_id: int, body: Mapping[str, Any] | None,
                        wait: bool) -> str:
    """What "the same request" means, hashed.

    Includes `wait` because a synchronous and an asynchronous trigger are
    different requests to the client even though they start the same work — a
    replay must not hand a `?wait=true` caller a 202 it never asked for.
    """
    import json
    payload = json.dumps(
        {"session_id": session_id, "body": dict(body or {}), "wait": bool(wait)},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def replay(row: Mapping[str, Any], expected_hash: str) -> JSONResponse:
    """Return the stored response for a repeated key, or refuse the key.

    A key reused with a DIFFERENT body is a client bug, and answering it with
    the old response would silently ignore what was actually asked for — the
    caller would believe it started a run with its new parameters. 422 says so.
    """
    import json
    if row["request_hash"] != expected_hash:
        raise HTTPException(
            422,
            f"{IDEMPOTENCY_HEADER} {row['idempotency_key']!r} was already used "
            "for a different request. Use a new key, or repeat the original "
            "request exactly.",
        )
    return JSONResponse(
        status_code=int(row["status_code"]),
        content=json.loads(row["response_json"]),
        headers={REPLAY_HEADER: "true",
                 IDEMPOTENCY_HEADER: str(row["idempotency_key"])},
    )
