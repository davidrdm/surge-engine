"""TriageAgent — judges social posts and extracts entities. Uses an LLM.

This is one of only two places a model is used, and the justification is narrow:
deciding whether a piece of free text is evidence of what the loaded mission
is looking for, and pulling out which city, which facility, which track and how
imminent, is irreducibly a language task. Everything downstream —
what to search next, how signals correlate, what an alert scores — is
deterministic Python.

What the model is NOT trusted with:

  * **Admitting a city.** It proposes candidates; `admit_city()` decides, and
    requires corroboration from two independent source domains. One viral post
    must not be able to steer collection into a city nobody is deploying to.
  * **Scoring.** It returns a `salience` used as one input to a weighted model
    it never sees, and it never touches a confidence band.
  * **Deciding what gets recorded.** Every post produces a `triage_decisions`
    row whether accepted or rejected, including posts the model failed to rule
    on. An unexplained omission is indistinguishable from a considered rejection,
    and for an audit trail that difference is the whole point.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Sequence

from datetime import datetime, timedelta

from ..base.agent import LLMAgent, TruncatedResponse
from ..db import enums
from ..db.database import SurgeDB, iso, parse_iso, utcnow
from ..services import calendar as calendar_service
from ..services import (facility, governance, provenance, receipts,
                        sensitivity)
from .queueing import admit_city
from .triage_schema import (
    SCHEMA_VERSION, BatchOutcome, ItemFault, TriageItem, build_request,
    parse_batch,
)

def build_system_prompt(mission: Any, config: Mapping[str, Any],
                        stream: Any = None) -> tuple[str, str]:
    """The screening criteria in force, and the version label for them.

    Both come from the mission pack. The engine has no opinion about what makes
    an item relevant — that is the whole of what a mission decides — and it
    supplies no fallback text, because a default prompt would screen against
    criteria nobody chose while looking exactly like criteria somebody did.

    Which of the two relevance legs to run is an ANALYST'S decision, exposed as
    `triage.require_nexus` and deliberately not chosen for them. The strict leg
    is the scoped instrument. The broad leg is materially different and far
    noisier; it is off unless someone turns it on.

    Returns (prompt, version). The prompt is what gets hashed onto the receipt,
    so an analyst can always establish which criteria produced a judgement, and
    the pack digest on the same receipt says which pack the text came from.
    """
    if mission is None:
        raise RuntimeError(
            "Triage cannot run without a mission: the screening prompt and "
            "the relevance criteria both come from the pack. Configure "
            "`mission.name`.")
    require_nexus = bool((config.get("triage") or {}).get("require_nexus", True))
    slot = "relevance_strict" if require_nexus else "relevance_broad"
    text, version = mission.prompts[slot], mission.prompt_versions[slot]
    if stream is not None and slot in stream.relevance:
        # The stream's own leg, inheriting the mission-level one per leg
        # independently when it declares none. The version label follows the
        # text: a receipt naming the mission's label for a stream's words
        # would make two different criteria indistinguishable afterwards.
        text, version = stream.relevance[slot]
    prompt = mission.prompts["triage"].format(relevance=text)
    return prompt, version


class TriageAgent(LLMAgent):
    """Screens collected social payloads into signals."""

    stage = "TRIAGING"

    def __init__(self, db: SurgeDB, config: Mapping[str, Any], client: Any) -> None:
        super().__init__(db, config, client)
        triage_cfg = config.get("triage", {})
        self.batch_size = int(triage_cfg.get("batch_size", 10))
        self.min_salience = float(triage_cfg.get("min_salience", 0.0))
        self.max_post_age_hours = float(
            triage_cfg.get("max_post_age_hours", 168.0))
        # Resolved once, so every batch in this run is judged under the same
        # criteria and the receipt hash is stable across them. One prompt per
        # STREAM: each stream may carry its own relevance leg, and a batch is
        # judged under exactly one system prompt, so batches are per stream
        # and each receipt's prompt_hash is that stream's.
        mission = getattr(db, "mission", None)
        # None — the mission-level legs — is always present: it is the
        # implicit stream's prompt, and it is also what judges a post whose
        # stream cannot be established (a pre-stream row in a mixed-era
        # database). A post the engine cannot place under any stream's
        # criteria is still judged under the MISSION'S, never dropped.
        self.prompts_by_stream: dict[str | None, tuple[str, str]] = {
            None: build_system_prompt(mission, config)}
        if mission is not None:
            for stream in getattr(mission, "streams", ()):
                self.prompts_by_stream[stream.id] = build_system_prompt(
                    mission, config, stream)
        # The implicit stream's pair, kept under the old names for the
        # single-stream call sites (alert-path helpers, tests, logs).
        self.system_prompt, self.prompt_version = next(
            iter(self.prompts_by_stream.values()))
        self.require_nexus = bool(triage_cfg.get("require_nexus", True))
        #: 9.4. raw_id -> the acquisition record for signals from that payload.
        #: Memoised because one response yields many posts and the answer is a
        #: property of the response, not of the post.
        self._acquisition_cache: dict[int, dict[str, str]] = {}
        #: iteration_id -> the calendar context block, resolved once per run.
        self._calendar_cache: dict[int, str] = {}

    # ------------------------------------------------------------------

    def _execute(
        self, iteration_id: int, *, retry_of: int | None = None,
        batch_size: int | None = None, **kwargs: Any,
    ) -> None:
        """Judge this iteration's social posts.

        `retry_of` switches the input from "what this iteration collected" to
        "what THAT iteration asked about and never got an answer for" (8.8).
        Everything after the gather is identical — the same strict binding, the
        same sensitivity gates, the same receipts — because the judgements a
        retry produces have to be the same kind of record as any other.
        """
        session_id = self._session_for(iteration_id)
        expand = bool(self.db.get_session(session_id)["expand_cities"])

        if not self.require_nexus:
            self._log(
                "WARNING",
                "Triage is running on the mission's BROAD relevance criteria "
                "rather than its strict ones. This is a wider and noisier "
                "instrument than the default and was switched on deliberately "
                "(triage.require_nexus: false). The pack's "
                "prompts/relevance-broad.md is what is in force; the receipt "
                "hash and the pack digest both record it.",
                iteration_id=iteration_id,
                prompt_version=self.prompt_version,
            )

        if retry_of is not None:
            posts = self._gather_for_retry(iteration_id, int(retry_of))
        else:
            posts = self._gather(iteration_id)
        if not posts:
            self._log("INFO", "No untriaged social payloads",
                      iteration_id=iteration_id)
            return

        counts = {"requested": 0, "accepted": 0, "rejected": 0, "undecided": 0,
                  "invalid": 0, "model_error": 0, "signals": 0}
        # Cities are admitted from the whole iteration's evidence, not per batch,
        # so corroboration across batches still counts toward the admission gate.
        by_city: dict[str, list[dict[str, Any]]] = {}

        batch_size = int(batch_size or self.batch_size)
        # One system prompt per model call, so batches are PER STREAM: a
        # stream may carry its own relevance criteria, and mixing two
        # streams' posts in one call would judge half of them under the
        # wrong ones. Iteration order follows the posts, which follow the
        # mission's stream declaration order — deterministic, so the receipt
        # sequence is reproducible.
        by_stream: dict[str | None, list[dict[str, Any]]] = {}
        for post in posts:
            by_stream.setdefault(post.get("stream"), []).append(post)
        uncovered_by_stream: dict[str | None, int] = {}

        for stream, stream_posts in by_stream.items():
          for batch in _batched(stream_posts, batch_size):
            payload, index = build_request(batch, iteration_id)
            outcome, receipt_id = self._judge(
                payload, list(index), iteration_id, stream)
            # Which CALL judged each item. One entry per requested id, because
            # a split batch is several calls and a decision must reference the
            # receipt whose input actually contained it.
            receipt_of: dict[str, int] = {i: receipt_id for i in index}

            # 8.8. A batch that overran `llm.max_tokens` will overrun it again
            # if re-sent unchanged, so halve and re-send rather than record a
            # coverage gap the system already knows how to avoid. Down to one
            # item: below that there is nothing left to shrink and the failure
            # is genuinely the model's. Every attempt writes its own receipt,
            # so the attempt history is on the record rather than inferred.
            attempt_size = len(batch)
            while outcome.truncated and attempt_size > 1:
                attempt_size = max(1, attempt_size // 2)
                self._log(
                    "WARNING",
                    f"Batch of {len(batch)} hit the token ceiling; retrying at "
                    f"{attempt_size} item(s) rather than recording "
                    f"{len(batch)} coverage gap(s)",
                    iteration_id=iteration_id, batch_size=attempt_size)
                merged = BatchOutcome()
                receipt_of = {}
                for sub in _batched(batch, attempt_size):
                    sub_payload, sub_index = build_request(sub, iteration_id)
                    sub_outcome, receipt_id = self._judge(
                        sub_payload, list(sub_index), iteration_id, stream)
                    merged.valid.update(sub_outcome.valid)
                    merged.faults.extend(sub_outcome.faults)
                    merged.missing.extend(sub_outcome.missing)
                    merged.truncated = merged.truncated or sub_outcome.truncated
                    merged.batch_error = (merged.batch_error
                                          or sub_outcome.batch_error)
                    index.update(sub_index)
                    # Per SUB-CALL, not per split. A single `receipt_id`
                    # carried out of this loop was the LAST sub-call's, and
                    # every decision from every earlier one was then stamped
                    # with a receipt whose `input_hash` and `batch_key` cover
                    # a request that did not contain it — which is the one
                    # thing a receipt exists to make impossible.
                    receipt_of.update({i: receipt_id for i in sub_index})
                outcome = merged
            counts["requested"] += len(batch)

            for fault in outcome.faults:
                # A fault with no id cannot be attributed to a post; it is
                # recorded on the log and its post shows up in `missing`.
                if fault.item_id and fault.item_id in index:
                    counts["invalid"] += 1
                    self._record_fault(iteration_id, index[fault.item_id],
                                       "INVALID_OUTPUT", fault,
                                       receipt_of.get(fault.item_id))

            faulted = {f.item_id for f in outcome.faults}
            for fault in outcome.faults:
                if fault.item_id and fault.item_id in index:
                    uncovered_by_stream[stream] = (
                        uncovered_by_stream.get(stream, 0) + 1)
            for identifier in outcome.missing:
                if identifier in faulted:
                    continue          # already recorded as INVALID_OUTPUT
                state = "MODEL_ERROR" if outcome.batch_error else "UNDECIDED"
                counts["model_error" if outcome.batch_error else "undecided"] += 1
                uncovered_by_stream[stream] = (
                    uncovered_by_stream.get(stream, 0) + 1)
                self._record_fault(
                    iteration_id, index[identifier], state,
                    ItemFault(identifier, state,
                              outcome.batch_error
                              or "no judgement was returned for this item"),
                    receipt_of.get(identifier))

            for identifier, item in outcome.valid.items():
                post = index[identifier]
                if not item.relevant:
                    counts["rejected"] += 1
                    self._record(iteration_id, post, item, "REJECTED",
                                 signal_id=None,
                                 receipt_id=receipt_of.get(identifier))
                    continue
                counts["accepted"] += 1
                for name in item.cities or ["__UNLOCATED__"]:
                    by_city.setdefault(name, []).append(
                        {"post": post, "item": item,
                         "receipt_id": receipt_of.get(identifier)}
                    )

        counts["signals"] = self._materialise(
            iteration_id, session_id, expand, by_city
        )
        uncovered = counts["undecided"] + counts["invalid"] + counts["model_error"]
        if uncovered:
            # Not a rejection and not a quiet city: these posts were never
            # judged, so SOCIAL coverage for this iteration is incomplete and
            # correlation must say so rather than score as if it were whole.
            # Per stream, because streams may occupy different banding
            # families and the gap must reach the right one.
            where = ", ".join(
                f"{n} in stream {stream}" if stream else f"{n} implicit"
                for stream, n in sorted(
                    uncovered_by_stream.items(), key=lambda kv: kv[1],
                    reverse=True))
            self._add_degradation(
                iteration_id,
                f"TriageAgent: {uncovered} of {counts['requested']} post(s) "
                f"were not judged ({counts['undecided']} undecided, "
                f"{counts['invalid']} invalid, {counts['model_error']} model "
                f"error; {where}); SOCIAL coverage is incomplete")

        self._log(
            "INFO" if not uncovered else "WARNING",
            f"Triaged {counts['requested']} posts: {counts['accepted']} "
            f"accepted, {counts['rejected']} rejected, {uncovered} not judged; "
            f"{counts['signals']} signals",
            iteration_id=iteration_id, schema_version=SCHEMA_VERSION, **counts,
        )

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    def _session_for(self, iteration_id: int) -> int:
        row = self.db.get_iteration(iteration_id)
        if row is None:
            raise ValueError(f"No iteration {iteration_id}")
        return int(row["session_id"])

    def _gather(self, iteration_id: int) -> list[dict[str, Any]]:
        """Flatten collected payloads into the posts that will be judged.

        Two properties this has to get right, both found by review after
        Phase 6 and both able to turn evidence into apparent absence:

        **Resume is per POST.** One response carries many posts, so a crash
        after the first batch leaves the rest of that response undecided.
        Filtering by payload — which is what a LEFT JOIN on `raw_id` does —
        dropped every one of them forever while the stage still described
        itself as re-entrant. Everything collected is rescanned; what has
        already been decided or already written off is dropped per URL.

        **Deduplication picks the best copy, not the first.** The same article
        surfaces from several queries, and the copies do not carry the same
        metadata: one may be undated or older. Marking a URL seen on first
        sight let payload ORDER decide whether the eligible copy was the one
        that reached the model. Every occurrence is collected first, the
        representative is chosen deterministically, and only then do the
        pre-model gates run — once, on the copy that was chosen.
        """
        cutoff = utcnow() - timedelta(hours=self.max_post_age_hours)
        decided = self.db.triaged_urls(iteration_id)          # (stream, url)
        recorded_pairs, recorded_stale = self.db.recorded_skips(iteration_id)

        def skip(reason: str, *, raw_id: int | None = None,
                 url: str | None = None, stream: str | None = None,
                 **fields: Any) -> None:
            """8.9. Every way a collected post fails to reach the model.

            Five drops used to leave four with no trace at all and the fifth
            with a bare count. A vendor payload that arrives malformed takes
            every post in it out of the run, and an absence of evidence
            produced by a parse failure is indistinguishable from an absence
            of the thing being watched for — which is the failure this system
            exists to prevent.

            Suppressed when the same refusal is already on the record, so a
            resumed stage does not report the same loss twice.
            """
            if reason == "STALE":
                if (stream, url) in recorded_stale:
                    return
                if url:
                    recorded_stale.add((stream, url))
            elif (raw_id, reason) in recorded_pairs:
                return
            self.db.insert_triage_skip(
                iteration_id=iteration_id, reason=reason, raw_id=raw_id,
                url=url, stream=stream, **fields)

        # (stream, url) -> every copy of it that was collected, in scan
        # order. Per STREAM, because streams judge under different criteria:
        # the same article surfacing in two streams is two judgements, while
        # within one stream it is still exactly one.
        occurrences: dict[tuple[str | None, str], list[dict[str, Any]]] = {}

        for raw in self.db.collected_raw_results(iteration_id, "SOCIAL"):
            raw_id = int(raw["raw_id"])
            stream = raw["stream"]
            try:
                payload = json.loads(raw["payload_json"])
            except (TypeError, ValueError) as exc:
                skip("PAYLOAD_UNPARSEABLE", raw_id=raw_id,
                     detail=f"{type(exc).__name__}: {exc}")
                continue
            if not isinstance(payload, list):
                skip("PAYLOAD_NOT_A_LIST", raw_id=raw_id,
                     detail=f"payload is {type(payload).__name__}, expected a "
                            f"list of posts")
                continue
            structural = 0
            for item in payload:
                if not isinstance(item, dict):
                    structural += 1
                    skip("ITEM_NOT_AN_OBJECT", raw_id=raw_id,
                         detail=f"element is {type(item).__name__}")
                    continue
                url = str(item.get("url") or "").strip()
                if not url:
                    structural += 1
                    skip("ITEM_NO_URL", raw_id=raw_id,
                         detail="no url, so a judgement has nothing to bind to")
                    continue
                if (stream, url) in decided:
                    # NOT a skip, and not a rejudgement. The same article
                    # legitimately surfaces from several queries, and on a
                    # resume it may already have been ruled on; either way
                    # that stream's judgement is the record.
                    continue
                occurrences.setdefault((stream, url), []).append(
                    {**item, "raw_id": raw_id, "url": url, "stream": stream})
            if structural:
                # Every structural refusal in this payload is now on the
                # record, so a rescan must not write them again.
                recorded_pairs |= {(raw_id, "ITEM_NOT_AN_OBJECT"),
                                   (raw_id, "ITEM_NO_URL")}

        posts: list[dict[str, Any]] = []
        for stream, url in occurrences:
            item = _representative(occurrences[(stream, url)])
            # Measured live: the social endpoints return a long tail of
            # ancient content. In the first real iteration the MEDIAN post was
            # 206 days old and the oldest 2,165; only 1% fell inside the
            # 48-hour correlation window. Judging that tail cost a model call
            # per ten posts and exhausted the quota before the recent items
            # were reached — and the one signal it did produce was a tweet
            # from 2020.
            #
            # Dropped BEFORE the model sees them, not after: a post that
            # cannot correlate is not a judgement the system needs, and paying
            # to judge it is what starved the posts that could.
            observed = _observed_at(item)
            if observed is not None and observed < cutoff:
                # The cutoff and the window are stored, not just the fact: the
                # cutoff is `utcnow() - max_post_age_hours` at THIS moment, so
                # a reader recomputing it later gets a different answer than
                # the run did. Same moved-cutoff hazard 8.8 had to design
                # around.
                skip("STALE", raw_id=int(item["raw_id"]), url=url,
                     stream=stream,
                     observed_at=iso(observed), cutoff_at=iso(cutoff),
                     max_post_age_hours=self.max_post_age_hours)
                continue
            posts.append(item)

        counts = self.db.triage_skip_counts(iteration_id)
        if counts:
            self._log(
                "INFO",
                f"{sum(counts.values())} collected post(s) did not reach the "
                f"model: "
                + ", ".join(f"{n} {reason}" for reason, n in counts.items()),
                iteration_id=iteration_id, kept=len(posts), **counts)

        # A malformed payload is a DEFECT, not a decision, and it costs every
        # post in that response. `data_completeness` already models exactly this
        # — but only for stages and queries, and this happens inside one. A run
        # that silently lost a vendor response must not close COMPLETE.
        lost = {r: n for r, n in counts.items() if r in enums.PAYLOAD_LEVEL_SKIPS}
        if lost:
            self._add_degradation(
                iteration_id,
                f"TriageAgent: {sum(lost.values())} collected payload(s) were "
                f"unusable and every post in them was lost ("
                + ", ".join(f"{n} {r}" for r, n in lost.items())
                + "); SOCIAL coverage is incomplete")
        return posts

    def _gather_for_retry(
        self, iteration_id: int, parent_id: int
    ) -> list[dict[str, Any]]:
        """The parent's unjudged posts, from its decision rows (8.8).

        Not from `raw_results`. `_gather` applies a staleness cutoff computed
        from `utcnow()`, so re-gathering an hour later would silently drop posts
        that were inside the window when the failed call was made — the retry
        would cover less than it was asked to and nothing would say so. Reading
        the decision rows takes exactly the set that failed, and takes it with
        no second freshness filter to drift from the first.

        It also means a post the age cut dropped cannot appear here: it never
        got a decision row. The requirement that legitimately skipped posts are
        not re-judged holds by construction rather than by remembering to
        re-apply a rule.
        """
        rows = self.db.uncovered_triage_decisions(parent_id)
        if not rows:
            return []

        posts: list[dict[str, Any]] = []
        purged = 0
        missing = 0
        for row in rows:
            if row["payload_json"] is None:
                # Retention purged the payload (raw_id ON DELETE SET NULL). The
                # judgement cannot be redone and saying so is the honest
                # outcome — sending an empty body to a model would manufacture
                # a judgement about nothing.
                purged += 1
                continue
            item = _post_in_payload(row["payload_json"], row["url"])
            if item is None:
                missing += 1
                continue
            # The parent's decision row says which stream asked for the
            # judgement, so the retry re-asks under the SAME criteria — a
            # retry that switched streams would answer a different question
            # and file it under the old one.
            posts.append({**item, "raw_id": int(row["raw_id"]),
                          "url": row["url"], "stream": row["stream"]})

        self._log(
            "INFO",
            f"Re-triage of iteration {parent_id}: {len(rows)} unjudged post(s) "
            f"found, {len(posts)} recoverable"
            + (f", {purged} whose payload has been purged" if purged else "")
            + (f", {missing} no longer present in their payload" if missing
               else ""),
            iteration_id=iteration_id, retry_of=parent_id,
            candidates=len(rows), recoverable=len(posts),
            purged=purged or None, missing_from_payload=missing or None,
        )
        if purged or missing:
            self._add_degradation(
                iteration_id,
                f"TriageAgent: {purged + missing} of {len(rows)} post(s) from "
                f"iteration {parent_id} could not be re-judged (payload purged "
                f"or no longer present); they remain a coverage gap")
        return posts

    # ------------------------------------------------------------------
    # The model call
    # ------------------------------------------------------------------

    def _judge(
        self, payload: Sequence[Mapping[str, Any]], expected: Sequence[str],
        iteration_id: int, stream: str | None = None,
    ) -> tuple[BatchOutcome, int]:
        """One model call, bound to the ids that were requested.

        Returns the outcome and the id of its receipt (8.1). A receipt is
        written for a FAILED call too: the provider echo is then absent, but
        which prompt version and which configuration failed is exactly what a
        reader of a MODEL_ERROR row needs, and a coverage gap with no receipt
        would be the only judgement in the system with no account of itself.

        A model failure must not fail the stage: every post in the batch is
        then recorded MODEL_ERROR and collection continues. Losing a batch of
        judgements is a coverage gap, and after Phase 7 the correlation layer
        represents it as one rather than as an absence of relevant news.
        """
        prompt = (
            f"Screen these {len(payload)} items.\n\n"
            f"{json.dumps(list(payload), indent=1)}"
            f"{self._calendar_block(iteration_id)}"
        )
        system_prompt, prompt_version = self.prompts_by_stream.get(
            stream) or self.prompts_by_stream[None]
        echo = receipts.ProviderEcho()
        accepted = None
        try:
            result, echo, accepted = self._call_llm_json(
                prompt, system_prompt, iteration_id=iteration_id,
                ceiling_setting="llm.max_tokens (or lower triage.batch_size)",
            )
        except Exception as exc:  # noqa: BLE001 — recorded, stage continues
            # 8.8. Truncation is flagged structurally, not left in the message.
            # "the reply was too big for max_tokens" and "the provider was
            # down" both land here and both become MODEL_ERROR, but only one is
            # fixed by sending fewer items — and a retry that re-sent the same
            # oversized batch would fail identically while looking like a
            # decision.
            truncated = isinstance(exc, TruncatedResponse)
            self._log("ERROR", f"Triage batch failed: {exc}",
                      iteration_id=iteration_id, batch_size=len(payload),
                      truncated=truncated or None)
            return (BatchOutcome(batch_error=f"{type(exc).__name__}: {exc}",
                                 missing=sorted(expected),
                                 truncated=truncated),
                    self._receipt(iteration_id, payload, echo, None,
                                  system_prompt, prompt_version))

        outcome = parse_batch(result, expected,
                              getattr(self.db, "mission", None))
        if outcome.batch_error:
            self._log("ERROR", f"Triage response unusable: {outcome.batch_error}",
                      iteration_id=iteration_id)
        for fault in outcome.faults:
            self._log("WARNING",
                      f"Triage item rejected ({fault.reason}): {fault.detail}",
                      iteration_id=iteration_id, item_id=fault.item_id)
        return outcome, self._receipt(iteration_id, payload, echo, accepted,
                                      system_prompt, prompt_version)

    def _calendar_block(self, iteration_id: int) -> str:
        """The operator-calendar context for this iteration, or "".

        Exactly the events with `added_at <= iteration.started_at`, in
        insertion order — a pure function of durable rows, which is what
        keeps `prompt_user_hash` verifiable after later appends. Memoised per
        iteration so every batch of one run sees one identical block. It
        rides OUTSIDE `payload`, so `input_hash` still answers "what was
        judged" over exactly the judged items; the block is criteria-context
        and `prompt_user_hash` covers it.
        """
        cached = self._calendar_cache.get(iteration_id)
        if cached is not None:
            return cached
        iteration = self.db.get_iteration(iteration_id)
        events = self.db.calendar_events(
            int(iteration["session_id"]),
            added_before=iteration["started_at"]) if iteration else []
        block = calendar_service.context_block(events)
        text = f"\n{block}" if block else ""
        self._calendar_cache[iteration_id] = text
        return text

    def _receipt(
        self, iteration_id: int, payload: Sequence[Mapping[str, Any]],
        echo: receipts.ProviderEcho, accepted_prompt: str | None,
        system_prompt: str | None = None, prompt_version: str | None = None,
    ) -> int:
        """One receipt for one call, shared by every decision it produced.

        `input_hash` is taken from the BUILT payload rather than the source
        rows, so it covers the 8.4 truncation window as well: change what
        reaches the model and the receipt says the input changed.
        """
        return self.db.insert_receipt(iteration_id, receipts.Receipt(
            kind="TRIAGE",
            provider=self.config.get("llm", {}).get("base_url"),
            model_requested=self.model,
            prompt_version=prompt_version or self.prompt_version,
            prompt_hash=receipts.sha256_hex(
                system_prompt if system_prompt is not None
                else self.system_prompt),
            # What the accepted call was SENT. Equal to the rebuilt request
            # only when no retry rewrote it, which is exactly the question a
            # reconstruction has to be able to answer.
            prompt_user_hash=(receipts.sha256_hex(accepted_prompt)
                              if accepted_prompt is not None else None),
            mission_id=getattr(self.db.mission, "label", None),
            mission_hash=getattr(self.db.mission, "digest", None),
            schema_version=SCHEMA_VERSION,
            rules_version=sensitivity.RULES_VERSION,
            normaliser_version=provenance.RULES_VERSION,
            config_hash=receipts.config_fingerprint(self.config),
            batch_key=receipts.sha256_hex(
                ",".join(str(p["item_id"]) for p in payload), length=16),
            input_hash=receipts.evidence_hash(payload),
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            echo=echo,
        ).as_row())

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------

    def _record(
        self, iteration_id: int, post: Mapping[str, Any], item: TriageItem,
        state: str, signal_id: int | None, receipt_id: int | None = None,
    ) -> int:
        """Persist a validated judgement. No coercion is left to do here."""
        return self.db.insert_triage_decision(
            iteration_id=iteration_id, raw_id=post["raw_id"], state=state,
            url=post["url"], stream=post.get("stream"), track=item.track,
            cities=list(item.cities), locations=list(item.locations),
            salience=item.salience, imminence_hours=item.imminence_hours,
            rationale=item.rationale, signal_id=signal_id, model=self.model,
            schema_version=SCHEMA_VERSION, receipt_id=receipt_id,
        )

    def _record_fault(
        self, iteration_id: int, post: Mapping[str, Any], state: str,
        fault: ItemFault, receipt_id: int | None = None,
    ) -> int:
        """Persist a post the model did not usefully judge.

        Distinct rows for distinct facts. Before Phase 7 an omission, a schema
        violation and a dead model all wrote `relevant=0` with one rationale
        string, so an outage was indistinguishable from an iteration in which
        nothing was relevant.
        """
        return self.db.insert_triage_decision(
            iteration_id=iteration_id, raw_id=post["raw_id"], state=state,
            url=post["url"], stream=post.get("stream"), model=self.model,
            schema_version=SCHEMA_VERSION,
            receipt_id=receipt_id, fault_detail=fault.detail[:2000],
            rationale=f"not judged ({fault.reason}): {fault.detail}"[:2000],
        )

    def _materialise(
        self, iteration_id: int, session_id: int, expand: bool,
        by_city: Mapping[str, list[dict[str, Any]]],
    ) -> int:
        """Admit cities, then write one signal per accepted post.

        City admission runs over the accumulated evidence for each name, which is
        what makes the two-independent-domains gate meaningful — it is a property
        of the set of posts naming a city, not of any single post.
        """
        written = 0
        # One decision row per POST, not per post-city pair. A post naming two
        # cities lands in two buckets, and recording inside the loop wrote it
        # twice — inflating every per-post count, including the coverage figure
        # that now caps the band. The city fan-out belongs to the signals.
        # Keyed (stream, url): the same article under two streams is two
        # judgements under two criteria, and one key collapsed them to
        # whichever stream recorded last.
        recorded: dict[tuple[str | None, str], int | None] = {}

        for name, entries in by_city.items():
            if name == "__UNLOCATED__":
                # Relevant but names no city. Recorded so the judgement is on
                # the record, but it cannot correlate and gets no signal.
                for entry in entries:
                    recorded.setdefault(
                        (entry["post"].get("stream"), entry["post"]["url"]),
                        None)
                continue

            evidence = []
            for entry in entries:
                publisher = provenance.publisher_of(
                    entry["post"], getattr(self.db, "mission", None))
                evidence.append({
                    "publisher_key": publisher.key,
                    # Carried, not implied: `independent_publishers` refuses to
                    # count an unresolved source as a publisher at all, and
                    # omitting the method here would make every one of them
                    # unresolved.
                    "publisher_method": publisher.method,
                    "claim_key": provenance.claim_of(entry["post"]),
                    "salience": entry["item"].salience,
                })
            city_id = admit_city(
                self.db, self.config.get("tipping", {}),
                iteration_id=iteration_id, session_id=session_id,
                name=name, signals=evidence, expand_cities=expand,
                stage=self.stage,
            )
            for entry in entries:
                post, item = entry["post"], entry["item"]
                signal_id = None
                if city_id is not None:
                    signal_id = self._write_signal(
                        iteration_id, city_id, post, item
                    )
                    if signal_id is not None:
                        written += 1
                # The first signal this post produced, if any. A post named in
                # two cities has two signals and one decision; the decision
                # points at the first, and the signals carry the rest.
                key = (post.get("stream"), post["url"])
                if recorded.get(key) is None:
                    recorded[key] = signal_id

        by_key = {(entry["post"].get("stream"), entry["post"]["url"]): entry
                  for entries in by_city.values() for entry in entries}
        for key, signal_id in recorded.items():
            entry = by_key[key]
            self._record(iteration_id, entry["post"], entry["item"],
                         "ACCEPTED", signal_id=signal_id,
                         receipt_id=entry["receipt_id"])
        return written

    def _acquisition(self, raw_id: Any) -> dict[str, str]:
        """9.4. How the payload behind this post reached us.

        Read from the stored payload's own provenance rather than assumed:
        a social post arrives through API Direct today, and a second social
        provider would otherwise be silently mislabelled as the first.
        """
        try:
            key = int(raw_id)
        except (TypeError, ValueError):
            return {"collection_class": "UNRECORDED",
                    "collection_basis": "no payload row to attribute this to"}
        if key not in self._acquisition_cache:
            row = self.db.one(
                "SELECT r.provider AS provider, q.endpoint AS endpoint "
                "FROM raw_results r "
                "LEFT JOIN query_queue q ON q.query_id = r.query_id "
                "WHERE r.raw_id = ?", (key,))
            collection_class, basis = governance.collection_class(
                row["provider"] if row else "",
                row["endpoint"] if row else None)
            self._acquisition_cache[key] = {
                "collection_class": collection_class,
                "collection_basis": basis}
        return self._acquisition_cache[key]

    def _write_signal(
        self, iteration_id: int, city_id: int, post: Mapping[str, Any],
        item: TriageItem,
    ) -> int | None:
        """Write a social signal, as CANDIDATE or CONFIRMED.

        Below `triage.min_salience` nothing is written at all — the judgement
        stays on the `triage_decisions` record but there is no evidence row.
        At or above it, `signal_state` decides whether the row scores and can
        tip: see `services/sensitivity.py`.
        """
        if item.salience < self.min_salience:
            return None
        location = facility.resolve(
            item.locations, self.db.get_key_locations(city_id),
            getattr(self.db, "mission", None),
        )
        observed = post.get("observed_at") or post.get("date") or ""
        publisher = provenance.publisher_of(
            post, getattr(self.db, "mission", None))
        state, reason = sensitivity.classify(item, observed, self.config)
        try:
            return self.db.insert_signal(
                iteration_id=iteration_id, raw_id=post["raw_id"],
                stream=post.get("stream"),
                signal_type="SOCIAL", city_id=city_id,
                location_id=location.location_id,
                track=item.track,
                observed_at=observed or None,
                quality=item.salience,
                signal_state=state, state_reason=reason,
                url=post["url"],
                author=(post.get("author") or "")[:200] or None,
                platform=post.get("platform") or None,
                source_domain=post.get("source_domain") or None,
                publisher_key=publisher.key,
                publisher_method=publisher.method,
                claim_key=provenance.claim_of(post),
                location_method=location.method,
                snippet=(post.get("snippet") or "")[:1000] or None,
                salience=item.salience,
                activity_type=item.activity_type,
                imminence_hours=item.imminence_hours,
                **self._acquisition(post.get("raw_id")),
            )
        except sqlite3.IntegrityError:
            # A signal the dedup index already holds. Legitimate on a re-run
            # of this stage, where writing the same observation twice is
            # exactly what the index exists to prevent — so it is not an
            # error. But it is a judgement that produced no evidence row, and
            # this used to return None in silence: the live run that found the
            # missing-city bug had no record of the loss anywhere, which is
            # the one thing this system is not allowed to do.
            self._log(
                "WARNING",
                f"Signal for {post['url']} in city {city_id} duplicates one "
                f"already recorded this iteration; the judgement stands on "
                f"its triage_decisions row and no second evidence row is "
                f"written",
                iteration_id=iteration_id, url=post["url"], city_id=city_id,
                stream=post.get("stream"),
            )
            return None


def _observed_at(item: Mapping[str, Any]) -> datetime | None:
    """When the post says it was observed, or None if it does not say."""
    return parse_iso(item.get("observed_at") or item.get("date"))


def _representative(copies: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Which copy of one URL is the one to judge.

    The same article arrives from several queries and the copies are not
    identical: one may carry a timestamp and another not, or one may be the
    provider's stale cached row. Whichever is chosen is the copy the freshness
    gate is applied to and the copy the model sees, so choosing by ARRIVAL
    ORDER let the provider decide whether eligible evidence became a signal.

    Freshest first, dated ahead of undated, then first seen. Deterministic:
    the same set of copies always yields the same representative, whatever
    order the payloads were scanned in.
    """
    def rank(pair: tuple[int, Mapping[str, Any]]) -> tuple[int, float, int]:
        index, item = pair
        observed = _observed_at(item)
        # Undated sorts last: it cannot be shown to be inside the window, and
        # a dated copy of the same article is strictly more useful.
        return (0 if observed is not None else 1,
                -observed.timestamp() if observed is not None else 0.0,
                index)

    return dict(min(enumerate(copies), key=rank)[1])


def _batched(items: Sequence[Any], size: int):
    size = max(1, size)
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _post_in_payload(payload_json: str, url: str) -> dict[str, Any] | None:
    """Find one post inside a stored social payload, by URL (8.8).

    Matching on URL rather than on position: a re-triage happens after the fact
    and the payload is the same bytes it always was, but binding by index is
    exactly the mistake the Phase 6 review found in the model boundary — one
    post's fields silently becoming another's. There is no reason to reintroduce
    it on the recovery path.

    Returns None when the post is not in the payload, which is a real
    possibility for a decision row whose `raw_id` was reassigned by a purge.
    """
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, list):
        return None
    for item in payload:
        if isinstance(item, dict) and str(item.get("url") or "").strip() == url:
            return item
    return None
