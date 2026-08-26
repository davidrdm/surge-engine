"""CorrelationAgent — spatial and temporal correlation. No LLM, ever.

Thin by design. All the analysis lives in `base/scoring.py`, which is pure and
was tested against a hand-computed expectation table in Phase 1; this agent's
only job is to feed it rows from the bus and write the verdict back. Keeping the
arithmetic in a pure module is what lets the confidence model be argued about,
diffed and re-run against fixtures without a database or a network.

One correlation per (city, actor track). Both tracks are scored from the same
collection pass, so adding a track costs no extra API calls — only a
second pass over rows already in hand.

The property that matters most is negative: a source that FAILED or was SKIPPED
is passed to the scorer as an unreliable source type, which lowers
`data_completeness`, caps the band below HIGH and produces a caveat. Absence of
evidence and absence of collection must never look the same.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

from ..base.agent import BaseAgent
from ..base.scoring import TrackModel, correlate, source_types_for_skipped
from ..db.database import parse_iso, utcnow
from ..services import hypotheses, receipts


class CorrelationAgent(BaseAgent):
    """Scores every city on every active track and records the working."""

    stage = "CORRELATING"

    def _mission(self):
        """The loaded mission, or a refusal naming what is missing.

        Scoring without one is not possible and must not be approximated: the
        per-track weights and the flight category filter both come from the
        pack, and an engine-side default would produce a number that looked
        exactly like a judgement.
        """
        mission = getattr(self.db, "mission", None)
        if mission is None:
            raise RuntimeError(
                "Correlation cannot run without a mission: the per-track "
                "weights and the flight category filter both come from the "
                "pack. Configure `mission.name`.")
        return mission

    def _execute(self, iteration_id: int, **kwargs: Any) -> None:
        iteration = self.db.get_iteration(iteration_id)
        if iteration is None:
            raise ValueError(f"No iteration {iteration_id}")
        session_id = int(iteration["session_id"])
        anchor = parse_iso(iteration["anchor_at"]) or utcnow()
        tracks = self.db.session_tracks(session_id)
        cfg = self.config.get("correlation", {})

        # The operator calendar as this ITERATION saw it — the same
        # `added_at <= started_at` cut triage's context block uses, so the
        # events a correlation is annotated with are exactly the events the
        # judgements upstream of it were framed by. Windowing and city
        # filtering are safe HERE, unlike in the prompt block, because what
        # this produces is STORED verbatim on the row: a later config change
        # cannot silently reframe an old correlation.
        calendar = [dict(row) for row in self.db.calendar_events(
            session_id, added_before=iteration["started_at"])]
        window_start = anchor - timedelta(
            hours=float(cfg.get("window_hours", 48)))
        window_end = anchor + timedelta(hours=float(
            self.config.get("windows", {}).get("near_term_hours", 48)))

        counts = {"correlated": 0, "alertable": 0, "capped": 0,
                  "candidates_excluded": 0}

        # A post the model never judged is not a post about nothing. Triage
        # non-coverage makes SOCIAL a gap for EVERY city in the iteration: an
        # unjudged post has no city — the model is what would have told us — so
        # the gap cannot be attributed to one, and the honest reading is that
        # all of them are affected.
        #
        # Before this, a model outage left completeness at 1.0 AND produced no
        # correlation row at all for a city whose only evidence was social,
        # making a dead judgement layer indistinguishable from a quiet city.
        uncovered_posts = self.db.triage_uncovered(iteration_id)
        # Per STREAM, because a stream occupies a banding family and the gap
        # must reach the right one. The implicit stream (None) means "the
        # social feed as a whole", which `correlate` maps to every
        # social-derived family — conservative where lineage is absent.
        uncovered_streams = sorted(
            self.db.triage_uncovered_by_stream(iteration_id),
            key=lambda v: (v is None, v or ""))
        triage_gap = ["SOCIAL"] if uncovered_posts else []

        # A stage that never ran is a gap even when every query it DID issue
        # succeeded, and nothing else can see it: the queries are COMPLETE, the
        # skipped stage wrote no decisions to count, and it enqueued nothing to
        # refuse. Found live by cancelling a run mid-collection — it reported a
        # quiet city on evidence it had paid for and then discarded.
        skipped = self.db.skipped_stages(iteration_id)
        skipped_gap = source_types_for_skipped(skipped)
        if skipped_gap:
            self._log(
                "WARNING",
                f"Stages {', '.join(skipped)} did not run; "
                f"{', '.join(skipped_gap)} count as coverage gaps",
                iteration_id=iteration_id, skipped_stages=skipped,
            )
        if triage_gap:
            self._log(
                "WARNING",
                f"{uncovered_posts} post(s) were never judged; SOCIAL counts as "
                "a coverage gap for every city this iteration",
                iteration_id=iteration_id, uncovered_posts=uncovered_posts,
            )

        for city in self.db.get_cities(session_id):
            city_id = int(city["city_id"])
            # Overlap, not containment: an event straddling either edge of
            # [anchor - window_hours, anchor + near_term_hours] is context
            # for it. `None` when the session has no calendar at all, so the
            # stored row can distinguish "nothing to consult" from "consulted,
            # nothing matched". Snapshots carry the fields the API's calendar
            # listing shows, so the two surfaces read alike.
            matches = [
                {"event_id": int(event["event_id"]), "name": event["name"],
                 "city": event["city_label"],
                 "city_canonical": event["city_canonical"],
                 "starts_at": event["starts_at"], "ends_at": event["ends_at"],
                 "category": event["category"], "note": event["note"],
                 "source_name": event["source_name"],
                 "added_at": event["added_at"]}
                for event in calendar
                if event["city_canonical"] == city["canonical"]
                and parse_iso(event["starts_at"]) <= window_end
                and parse_iso(event["ends_at"]) >= window_start
            ] if calendar else None
            rows = [dict(row) for row in self._signals(session_id, city_id, anchor, cfg)]
            # CANDIDATE rows stay visible in the evidence trail but do not
            # score. See services/sensitivity.py for why the distinction exists.
            signals = [r for r in rows
                       if (r.get("signal_state") or "CONFIRMED") == "CONFIRMED"]
            candidates = len(rows) - len(signals)
            # 9.11. The endpoint each failure came from, so the correlation can
            # report `LODGING:/search` rather than a bare `LODGING` that reads
            # as "no lodging data" when the price leg succeeded.
            # 9.10. What this city's non-military flight traffic normally
            # looks like, from iterations already collected and paid for.
            baselines = self._flight_baselines(iteration_id, city_id, cfg)
            endpoints = dict(self.db.unreliable_sources(iteration_id, city_id))
            collected = self.db.collected_source_types(iteration_id, city_id)
            # Under a streams mission, every social loss travels through
            # `social_stream_gaps` — per stream where the lineage exists,
            # `None` (= every social-derived family) where it does not — and
            # the bare "SOCIAL" source-type entry is removed so one lost
            # query cannot gap both a stream's family and, via the
            # conservative streamless rule, all of them at once. A no-streams
            # mission takes the v0.1 path untouched.
            if getattr(self._mission(), "streams", ()):
                stream_gaps: list[str | None] = sorted(
                    set(self.db.unreliable_social_streams(
                        iteration_id, city_id))
                    | set(uncovered_streams)
                    | ({None} if "SOCIAL" in skipped_gap else set()),
                    key=lambda v: (v is None, v or ""))
            else:
                stream_gaps = []
            unreliable = sorted(
                set(self.db.unreliable_source_types(iteration_id, city_id))
                # A query a guard refused to enqueue leaves no queue row, so it
                # is invisible to the status-based check. Measured live: the
                # per-city cap silently dropped an entire actor track.
                | set(self.db.refused_source_types(iteration_id, city_id))
                | set(triage_gap)
                | set(skipped_gap)
            )
            if stream_gaps:
                unreliable = sorted(set(unreliable) - {"SOCIAL"})
            if not signals and not unreliable:
                # Nothing observed and nothing missing. Recording a zero-score
                # correlation for every quiet city on every track would bury the
                # real ones in noise.
                continue

            for track in tracks:
                # The mission supplies this track's weights and its flight
                # category filter; the engine supplies the arithmetic.
                model = TrackModel.from_mission(self._mission(), track)
                result = correlate(
                    signals, track=model, anchor_at=anchor, cfg=cfg,
                    iteration_id=iteration_id,
                    unreliable_source_types=unreliable,
                    failed_endpoints=endpoints,
                    collected_source_types=collected,
                    flight_baselines=baselines,
                    social_stream_gaps=stream_gaps,
                )
                # 9.6. What else would produce THIS evidence, derived from
                # the families that actually contributed rather than from
                # everything in the window: a signal scored at zero did not
                # produce the correlation and should not shape the list of
                # things that might have.
                contributing = [
                    row for row in signals
                    if row.get("signal_id") in result.signal_contributions
                ]
                trace = result.rule_trace
                if matches:
                    trace += (
                        f"; operator calendar: {len(matches)} scheduled "
                        f"event(s) overlap this window (annotation only — "
                        f"score unchanged)")
                correlation_id = self.db.upsert_correlation(
                    iteration_id=iteration_id, city_id=city_id,
                    track=track, score=result.score, band=result.band,
                    distinct_types=result.distinct_types,
                    contributions=result.contributions,
                    data_completeness=result.data_completeness,
                    # The DETAILED list, so a reader sees which endpoint
                    # failed. `data_completeness` still reflects families.
                    failed_sources=result.failed_sources,
                    failed_families=result.failed_families,
                    band_capped=result.band_capped,
                    rule_trace=trace,
                    alternatives=hypotheses.for_correlation(
                        contributing, result.contributions,
                        self._mission(), calendar_matches=matches or ()),
                    calendar_matches=matches,
                    # The tunables this score was computed under. Correlation
                    # writes no receipt — no model is involved — so without
                    # this the arithmetic's settings live only in a config
                    # file that anyone may edit afterwards.
                    config_hash=receipts.config_fingerprint(self.config),
                    flight_baseline=(
                        {**result.flight_baseline,
                         "_contamination_filter":
                             "RELAXED" if getattr(self, "_baseline_relaxed",
                                                  False) else "APPLIED"}
                        if result.flight_baseline else result.flight_baseline),
                    evidence_freshness=result.evidence_freshness,
                )
                for signal_id, contribution in result.signal_contributions.items():
                    self.db.link_correlation_signal(
                        correlation_id, signal_id, contribution
                    )
                counts["correlated"] += 1
                counts["alertable"] += int(result.is_alertable)
                counts["capped"] += int(result.band_capped)
            counts["candidates_excluded"] += candidates

        self._log(
            "INFO",
            f"Correlated {counts['correlated']} city/track pair(s): "
            f"{counts['alertable']} alertable, {counts['capped']} band-capped",
            iteration_id=iteration_id, **counts,
        )

    def _flight_baselines(
        self, iteration_id: int, city_id: int, cfg: Mapping[str, Any],
    ) -> dict[str, float]:
        """Normal flight volume for this city, per scoring kind (9.10).

        The median of prior per-iteration counts. Median rather than mean on
        the owner's decision — a mean lets one exceptional day drag the notion
        of normal with it, which is exactly the contamination a baseline built
        from your own history is prone to.

        Returns only kinds with enough samples. A kind below the floor is
        absent from the mapping, `correlate` scores it on the absolute count as
        before, and the correlation records UNBASELINED — a cold start must be
        visible, not silently indistinguishable from a city whose normal is
        genuinely what was seen today.

        Deliberately NOT bucketed by weekday or hour, per the owner's decision.
        Bucketing multiplies the samples each bucket needs and so lengthens the
        cold start; the cost is that a strongly weekday-patterned airport will
        show excess on its busy days until the median absorbs both.
        """
        from datetime import timedelta

        from ..base.scoring import (BASELINED_FLIGHT_KINDS, median,
                                    scoring_kind)

        minimum = int(cfg.get("flight_baseline_min_samples", 3))
        window = int(cfg.get("flight_baseline_window_days", 30))
        since = utcnow() - timedelta(days=window)
        rows = self.db.flight_baseline_samples(
            city_id, before_iteration=iteration_id, since=since)

        # The contamination filter can starve the baseline it protects, and on
        # real data it does. Measured: Atlanta's only two flight iterations
        # BOTH produced MEDIUM correlations, so the filter excluded 100% of its
        # samples — and Atlanta is the city whose 13/13/14 business-jet counts
        # motivated baselining in the first place. The rule is circular there:
        # general-aviation density inflates the score to MEDIUM, and MEDIUM
        # then disqualifies the sample that would have corrected it.
        #
        # So the filter is applied WHENEVER IT CAN BE AFFORDED and relaxed when
        # it cannot. A median already resists a sustained surge — more than
        # half the samples must be surged to move it — so the filter is the
        # second line of defence, not the only one, and an unfiltered baseline
        # is far better than no baseline at all. `relaxed` is recorded on the
        # correlation, so a reader is never left assuming the filter held.
        self._baseline_relaxed = False
        if len(rows) < minimum:
            unfiltered = self.db.flight_baseline_samples(
                city_id, before_iteration=iteration_id, since=since,
                exclude_bands=())
            if len(unfiltered) > len(rows):
                rows = unfiltered
                self._baseline_relaxed = True
        if not rows:
            return {}

        per_kind: dict[str, dict[int, int]] = {}
        for iteration, category, confidence, count in rows:
            kind = scoring_kind({
                "signal_type": "FLIGHT", "flight_category": category,
                "category_confidence": confidence})
            if kind not in BASELINED_FLIGHT_KINDS:
                continue
            # One iteration can hold two categories that map to one kind — J
            # and T both score as flight_J — so the sample is their sum, which
            # is what the observed count will also be.
            bucket = per_kind.setdefault(kind, {})
            bucket[iteration] = bucket.get(iteration, 0) + count

        baselines: dict[str, float] = {}
        for kind, by_iteration in per_kind.items():
            samples = list(by_iteration.values())
            if len(samples) >= minimum:
                value = median(samples)
                if value > 0:
                    baselines[kind] = value
        if baselines:
            self._log(
                "INFO",
                f"Flight baselines for city {city_id}: "
                + ", ".join(f"{k}={v:g}" for k, v in sorted(baselines.items()))
                + (" (contamination filter relaxed — too few clean samples)"
                   if self._baseline_relaxed else ""),
                iteration_id=iteration_id, city_id=city_id,
                samples={k: len(v) for k, v in per_kind.items()},
                contamination_filter="RELAXED" if self._baseline_relaxed
                else "APPLIED",
            )
        return baselines

    def _signals(
        self, session_id: int, city_id: int, anchor, cfg: Mapping[str, Any],
    ):
        """Signals for a city inside the correlation window.

        Read across iterations rather than only this one. A flight observed
        thirty minutes before this iteration started is still live evidence, and
        scoping to the current iteration would discard it purely because of when
        the operator happened to press the button.
        """
        from datetime import timedelta

        window = timedelta(hours=float(cfg.get("window_hours", 48)))
        return self.db.recent_signals_for_city(
            session_id, city_id, since=anchor - window
        )
