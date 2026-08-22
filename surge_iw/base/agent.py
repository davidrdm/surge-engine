"""Agent base classes.

Split into two deliberately. `BaseAgent` has no LLM dependency at all, so the
three deterministic agents cannot accidentally acquire one — a Python-level
statement of the rule that reasoning happens only where language reasoning is
genuinely required. `LLMAgent` adds the model client for the two agents that do.

Derived from iw/iw_agents/base/agent.py, with one behaviour reversed.

That version's `run()` set the WHOLE assessment to FAILED on any exception and
re-raised. Here failure is isolated: an exception marks this agent's `agent_runs`
row FAILED, records a degradation, and returns False. The driver decides whether
the iteration is PARTIAL or FAILED. A social-connector outage must not discard a
real military-flight cluster that another agent already collected.
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Any, Mapping

from ..db.database import SurgeDB
from ..services.receipts import ProviderEcho
from ..services.redact import redact_exception


class AgentError(RuntimeError):
    """Raised for a condition that should stop this agent but not the run."""


class NonFiniteNumber(ValueError):
    """Raised when model output contains a bare NaN or Infinity token."""


class TruncatedResponse(AgentError):
    """The model hit its output ceiling mid-answer.

    Distinct from malformed output, and the distinction is actionable: a
    truncated batch is a TOKEN BUDGET problem an operator fixes by lowering
    `triage.batch_size` or raising `llm.max_tokens`, while genuinely malformed
    output is a model problem they cannot. Conflating them cost real evidence —
    measured while broadening the triage criteria, whose longer rationales
    overflowed 4096 tokens at batch_size 10 and lost whole batches to a message
    that only said "invalid JSON after 3 attempts".

    Retrying is pointless: the same prompt at the same ceiling truncates again,
    and each retry appends MORE context, making it worse.
    """


def _reject_constant(token: str) -> Any:
    raise NonFiniteNumber(f"the non-finite JSON token {token!r}")


def loads_strict(text: str) -> Any:
    """`json.loads` with NaN and Infinity refused at the door.

    Python's decoder accepts bare `NaN`, `Infinity` and `-Infinity` even though
    no JSON standard permits them, and every downstream numeric guard in this
    system is a comparison — which NaN silently wins. That is how `salience:
    NaN` reached a clamp that turned it into MAXIMUM salience. Refusing here
    covers both LLM agents at the one point the text becomes data.
    """
    return json.loads(text, parse_constant=_reject_constant)


class BaseAgent(ABC):
    """Deterministic agent. Database, config, logging, and a run wrapper.

    No LLM client is available here. Subclasses that need one inherit from
    LLMAgent instead.
    """

    #: Stage this agent runs in, for the agent_runs record.
    stage: str = ""

    def __init__(self, db: SurgeDB, config: Mapping[str, Any]) -> None:
        self.db = db
        self.config = config
        self.agent_name = type(self).__name__
        #: The stage this agent is currently in, set by run(). Not the class
        #: attribute: CollectionAgent runs in two and overrides it per call.
        self._current_stage: str = self.stage

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self, iteration_id: int, *, stage: str | None = None,
            **kwargs: Any) -> bool:
        """Execute the agent. Returns True on success, False on failure.

        Deliberately does NOT re-raise. The caller is the orchestrator, and an
        agent that failed has already recorded why — both in its `agent_runs`
        row and in the iteration's degradation list. Propagating the exception
        would let one connector outage abort stages that have nothing to do
        with it.

        `stage` overrides the class attribute for an agent that runs in more
        than one stage. CollectionAgent is the only such agent, and without the
        override its social and tipped runs share one `agent_runs` key —
        `start_agent_run` replaces on conflict, so the social pass's record
        would be erased by the tipped one and two of three collection runs
        would leave no audit trail at all.
        """
        self._current_stage = stage or self.stage
        run_id = self.db.start_agent_run(
            iteration_id, self.agent_name, self._current_stage
        )
        self._log("INFO", f"{self.agent_name} starting", iteration_id=iteration_id)
        started = time.monotonic()
        try:
            self._execute(iteration_id, **kwargs)
        except Exception as exc:  # noqa: BLE001 — recorded, never swallowed
            detail = redact_exception(exc)
            self.db.finish_agent_run(run_id, "FAILED", detail)
            self._log("ERROR", f"{self.agent_name} failed: {detail}",
                      iteration_id=iteration_id, exc_type=type(exc).__name__)
            self._add_degradation(iteration_id, f"{self.agent_name}: {detail}")
            return False
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self.db.finish_agent_run(run_id, "COMPLETE")
        self._log("INFO", f"{self.agent_name} complete",
                  iteration_id=iteration_id, elapsed_ms=elapsed_ms)
        return True

    @abstractmethod
    def _execute(self, iteration_id: int, **kwargs: Any) -> None:
        """Agent-specific work. Raise to mark this agent failed."""

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _log(self, level: str, message: str, **extra: Any) -> None:
        """Structured log to agent_log. Scrubbed on the way in."""
        iteration_id = extra.pop("iteration_id", None)
        self.db.log(self.agent_name, level, message,
                    iteration_id=iteration_id, **extra)

    def _add_degradation(self, iteration_id: int, note: str) -> None:
        """Append to the iteration's degradation list.

        Surfaced by GET /v1/iterations/{id} so an operator can see what the run
        did not manage to do, rather than inferring it from a lower confidence
        band.

        Attributed to the stage actually running — `_current_stage`, not the
        class attribute, because CollectionAgent runs in two — so discarding
        that stage retracts what it said about itself.
        """
        self.db.append_degradation(
            iteration_id, note, source=self._current_stage or self.stage)


class LLMAgent(BaseAgent):
    """Adds an OpenAI-compatible model client.

    The OpenAI SDK is used for every provider so production can point at a
    self-hosted open-weight model by changing `base_url` alone.
    """

    def __init__(self, db: SurgeDB, config: Mapping[str, Any], client: Any) -> None:
        super().__init__(db, config)
        self.client = client
        llm = config.get("llm", {})
        self.model = llm.get("model", "")
        self.max_tokens = int(llm.get("max_tokens", 4096))
        self.temperature = float(llm.get("temperature", 0.2))

    # ------------------------------------------------------------------
    # Model calls
    # ------------------------------------------------------------------

    def _call_llm(
        self,
        prompt: str,
        system: str,
        *,
        iteration_id: int | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        attempts: int = 3,
        ceiling_setting: str | None = None,
    ) -> tuple[str, ProviderEcho]:
        """Single completion, with exponential backoff on transient errors.

        Returns the text **and** what the provider echoed about what it served
        (8.1). The echo used to be dropped on the floor, which is why a vendor
        repointing a model alias left no trace: `model_served` is the only place
        that shows up. Token counts are logged per call as well, because an LLM
        stage that silently triples in cost is otherwise invisible until the
        bill arrives.
        """
        import openai  # lazy: the deterministic agents must not need it

        last: Exception = RuntimeError("LLM call never attempted")
        for attempt in range(attempts):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=max_tokens or self.max_tokens,
                    temperature=(
                        self.temperature if temperature is None else temperature
                    ),
                )
                echo = ProviderEcho.from_response(response, attempt + 1)
                self._log(
                    "INFO", "LLM call complete", iteration_id=iteration_id,
                    model=self.model, model_served=echo.model_served,
                    tokens_in=echo.tokens_in, tokens_out=echo.tokens_out,
                )
                choice = response.choices[0]
                if getattr(choice, "finish_reason", None) == "length":
                    # Say what actually happened. Left to the JSON parser this
                    # surfaces as "invalid JSON", which sends an operator
                    # looking for a model fault instead of a token ceiling.
                    # Name the setting that ACTUALLY set this ceiling. The
                    # message used to say "lower triage.batch_size or raise
                    # llm.max_tokens" on every call, including the alert one,
                    # where neither knob is in play — an error that sends an
                    # operator to the wrong dial is worse than a vague one.
                    raise TruncatedResponse(
                        f"the model hit its {max_tokens or self.max_tokens}-token "
                        f"output ceiling mid-response, so the answer is "
                        f"incomplete rather than wrong. Raise "
                        f"{ceiling_setting or 'llm.max_tokens'}; retrying at "
                        f"the same ceiling will truncate again. On a model that "
                        f"reasons before answering, the ceiling has to cover "
                        f"the reasoning as well as the answer."
                    )
                return choice.message.content or "", echo
            except TruncatedResponse:
                raise
            except openai.RateLimitError as exc:
                last = exc
                wait = 2 ** (attempt + 1)
                self._log("WARNING", f"LLM rate limited; retrying in {wait}s",
                          iteration_id=iteration_id, attempt=attempt + 1)
                time.sleep(wait)
            except openai.APIError as exc:
                last = exc
                if attempt == attempts - 1:
                    break
                wait = 2 ** attempt
                self._log("WARNING", f"LLM API error; retrying in {wait}s",
                          iteration_id=iteration_id, attempt=attempt + 1)
                time.sleep(wait)
        raise AgentError(
            f"LLM call failed after {attempts} attempts: {redact_exception(last)}"
        ) from last

    def _call_llm_json(
        self,
        prompt: str,
        system: str,
        *,
        iteration_id: int | None = None,
        max_tokens: int | None = None,
        attempts: int = 3,
        ceiling_setting: str | None = None,
    ) -> tuple[Any, ProviderEcho, str]:
        """Completion parsed as JSON, retrying with the parse error sent back.

        Returns the value, the echo from the call that produced it, **and the
        user message that call was actually sent**. All three because this
        method rewrites the prompt between attempts, so "which variant the
        accepted answer came from" is a real question: the echo carries the
        accepted attempt's number, and the third value is the text itself, so
        a caller's receipt can hash what was accepted rather than what was
        first tried. Without it a retried classification reconstructed as
        byte-exact against a request that had been refused.

        No regex is ever used to extract JSON from model output. The previous
        implementation used a non-greedy `\\[.*?\\]`, which truncated at the
        first nested `]` and silently discarded every finding whenever the model
        wrapped its answer in prose.
        """
        json_system = (
            system.rstrip()
            + "\n\nIMPORTANT: respond with a single valid JSON value and nothing "
              "else. No markdown fences, no commentary, no text outside the JSON."
        )
        current = prompt
        raw = ""
        for attempt in range(attempts):
            raw, echo = self._call_llm(
                current, json_system, iteration_id=iteration_id,
                max_tokens=max_tokens, ceiling_setting=ceiling_setting,
            )
            # The inner call counts its own transport retries; this loop's
            # rewrites are a separate thing and both belong on the receipt.
            echo = replace(echo, attempts=attempt + 1)
            try:
                # Not plain json.loads: Python's decoder accepts bare NaN,
                # Infinity and -Infinity, which no JSON standard permits. That
                # is how `salience: NaN` reached a clamp that turned it into
                # MAXIMUM salience. Refused at the door, for every agent.
                return loads_strict(_strip_fences(raw)), echo, current
            except NonFiniteNumber as exc:
                if attempt == attempts - 1:
                    raise AgentError(str(exc)) from exc
                self._log("WARNING", f"LLM returned a non-finite number: {exc}",
                          iteration_id=iteration_id, attempt=attempt + 1)
                current = (
                    f"Your previous response contained {exc}. JSON has no NaN "
                    f"or Infinity. Return finite numbers only.\n\n{prompt}"
                )
                continue
            except json.JSONDecodeError as exc:
                if attempt == attempts - 1:
                    break
                self._log("WARNING", f"LLM JSON parse failed: {exc}",
                          iteration_id=iteration_id, attempt=attempt + 1)
                current = (
                    f"Your previous response could not be parsed as JSON.\n"
                    f"Parse error: {exc}\n"
                    f"First 500 characters of what you sent:\n{raw[:500]}\n\n"
                    f"Original request:\n{prompt}\n\n"
                    "Return only valid JSON."
                )
        raise AgentError(
            f"LLM returned invalid JSON after {attempts} attempts; "
            f"response began: {raw[:200]!r}"
        )


def _strip_fences(text: str) -> str:
    """Remove a markdown code fence if the model added one anyway."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    start = 1
    end = len(lines) - 1 if lines[-1].strip().startswith("```") else len(lines)
    return "\n".join(lines[start:end]).strip()
