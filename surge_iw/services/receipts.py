"""Classification receipts — how a judgement was reached (8.1).

Before this, nothing recorded *how*. `triage_decisions.model` held the config
string, so a vendor silently repointing `gemini-3.5-flash` left no trace;
`SYSTEM_PROMPT` was an unversioned module constant, so editing it moved the
criteria for every future decision with no record that they had moved and no way
to segregate the decisions made under the old wording; and `_call_llm_json`
rewrites the prompt between retries without recording which variant the accepted
answer came from.

A receipt is one row per model **call**, referenced by every decision that call
produced. Not columns on the decision: a batch of ten posts shares one call, and
duplicating fourteen provenance columns ten times invites them to disagree.

**The hash is the truth; the version label is a convenience.** An edit to a
prompt without bumping its `PROMPT_VERSION` still changes `prompt_hash`, so
decisions made under different wordings remain separable no matter how careless
the labelling was. A label can lie. A hash cannot, and `tests/test_receipts.py`
asserts exactly that.

Everything the provider tells us about what it actually served is optional,
because most OpenAI-compatible endpoints omit most of it. An absent
`system_fingerprint` is recorded as absent — never as a default that would later
read as a fact.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import __version__

#: Kinds of judgement a receipt can cover. Mirrored in schema.sql.
RECEIPT_KINDS = frozenset({"TRIAGE", "ALERT"})

#: Config sections excluded from `config_fingerprint`.
#:
#: The fingerprint answers "what analytical configuration was in force", so
#: deployment plumbing is left out deliberately: `database.path` and the API
#: host/port differ between a laptop and a server that are reasoning
#: identically, and including them would make two runs of the same analysis
#: incomparable. Everything that can move a judgement — the lexicon, the
#: windows, the weights, the floors, the caps — is inside the hash.
NON_ANALYTICAL_SECTIONS = ("database", "api")


def sha256_hex(text: str, *, length: int = 32) -> str:
    """A short, stable content hash. 128 bits at the default length."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def canonical_json(value: Any) -> str:
    """Key-sorted, whitespace-free JSON, so a hash depends on content alone."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def config_fingerprint(config: Mapping[str, Any]) -> str:
    """Hash of the analytical configuration in force.

    Config holds environment variable *names*, never key values (§0), so this is
    safe to compute and safe to expose.
    """
    analytical = {k: v for k, v in config.items()
                  if k not in NON_ANALYTICAL_SECTIONS}
    return sha256_hex(canonical_json(analytical))


def evidence_hash(items: Sequence[Any]) -> str:
    """Hash of exactly what the model was shown.

    Computed from the built payload rather than from the source rows, so it
    covers the truncation window too: if 8.4's head+tail changes what reaches
    the model, the receipt says so.
    """
    return sha256_hex(canonical_json(list(items)))


_UNRESOLVED = object()
_CODE_REVISION: Any = _UNRESOLVED


def code_revision() -> str | None:
    """Short git revision of the working tree, or None outside a checkout.

    Resolved once per process — a subprocess per model call would be absurd. A
    deployment from a tarball legitimately has no revision, and None is the
    honest answer, better than a placeholder later mistaken for a commit.
    """
    global _CODE_REVISION
    if _CODE_REVISION is _UNRESOLVED:
        try:
            out = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=Path(__file__).resolve().parents[2],
                capture_output=True, text=True, timeout=5, check=True,
            )
            _CODE_REVISION = out.stdout.strip() or None
        except Exception:  # noqa: BLE001 — absence is a valid answer
            _CODE_REVISION = None
    return _CODE_REVISION


@dataclass(frozen=True)
class ProviderEcho:
    """What the provider said it served, as opposed to what we asked for.

    `model_served` differing from the requested model is the case this exists
    to catch: an alias silently repointed at a new snapshot.
    """

    model_served: str | None = None
    response_id: str | None = None
    system_fingerprint: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    #: How many times the call was issued before an answer was accepted. >1
    #: means the prompt was rewritten by a retry, and the accepted answer came
    #: from the last variant.
    attempts: int = 1

    @classmethod
    def from_response(cls, response: Any, attempts: int = 1) -> "ProviderEcho":
        """Read whatever this endpoint happens to expose. Absent stays absent."""
        usage = getattr(response, "usage", None)

        def text(name: str) -> str | None:
            value = getattr(response, name, None)
            return str(value) if value else None

        return cls(
            model_served=text("model"),
            response_id=text("id"),
            system_fingerprint=text("system_fingerprint"),
            tokens_in=getattr(usage, "prompt_tokens", None),
            tokens_out=getattr(usage, "completion_tokens", None),
            attempts=attempts,
        )


@dataclass
class Receipt:
    """One model call, fully attributed.

    Assembled by the agent, written once, then referenced by every decision or
    alert the call produced.
    """

    kind: str
    model_requested: str
    prompt_version: str
    #: The SYSTEM prompt's hash.
    prompt_hash: str
    input_hash: str
    config_hash: str
    provider: str | None = None
    schema_version: str | None = None
    rules_version: str | None = None
    normaliser_version: str | None = None
    batch_key: str | None = None
    #: The USER message that was accepted, hashed. None only for a call that
    #: never got an answer. Separate from `prompt_hash` because the retry loop
    #: rewrites the user message and leaves the system prompt alone, so a
    #: receipt carrying only the latter describes a request that may have been
    #: refused rather than the one that produced the judgement.
    prompt_user_hash: str | None = None
    #: Which mission pack supplied the prompt, and the hash of its bytes.
    #:
    #: `code_revision` used to be sufficient provenance for a prompt, because
    #: the prompt WAS code. Now it is a file outside the repository, and a pack
    #: can be edited without any commit at all — so the receipt has to name the
    #: pack itself or the reconstruction guarantee weakens to a convention.
    mission_id: str | None = None
    mission_hash: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    code_revision: str | None = field(default_factory=code_revision)
    package_version: str = __version__
    echo: ProviderEcho = field(default_factory=ProviderEcho)

    def as_row(self) -> dict[str, Any]:
        """Flattened for `SurgeDB.insert_receipt`."""
        row = {k: v for k, v in asdict(self).items() if k != "echo"}
        row.update(asdict(self.echo))
        return row


#: Fields safe to return on `GET /v1/alerts/{id}/evidence`.
#:
#: The prompt HASH is exposed; the prompt TEXT is not. A reader needs to know
#: that two judgements were made under the same criteria and to detect when the
#: criteria moved, which the hash gives them. The wording itself is screening
#: tradecraft and is not part of the evidence surface (8.2).
PUBLIC_RECEIPT_FIELDS = (
    "receipt_id", "kind", "provider", "model_requested", "model_served",
    "response_id", "system_fingerprint", "prompt_version", "prompt_hash",
    "prompt_user_hash",
    "schema_version", "rules_version", "normaliser_version", "config_hash",
    "input_hash", "code_revision", "package_version", "mission_id",
    "mission_hash", "attempts", "created_at",
)


def public_view(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The sanitized subset of a receipt row, or None."""
    if row is None:
        return None
    return {k: row[k] for k in PUBLIC_RECEIPT_FIELDS if k in row.keys()}
