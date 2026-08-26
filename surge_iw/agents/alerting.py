"""AlertAgent — writes the human-readable summary. Uses an LLM.

The second and last place a model is used. Its justification is narrow: turning a
scored correlation into one or two sentences an operations reader can act on is a
writing task, and a template would produce prose that reads like a template at
exactly the moment someone needs to understand what is happening.

**The model cannot move the number.** The score and band are copied from the
correlation row; the model never sees a request to produce them and its output is
never parsed for one. A test asserts `alerts.confidence_score` is byte-identical
to `correlations.score`. If the model returns nothing usable, a deterministic
fallback summary is written instead — an alert with a poor sentence is far better
than a scored finding that never reaches anyone.

**The caveat is deterministic too.** Data-gap disclosure comes from
`CorrelationResult.caveat()`, not from the model, so it cannot be softened,
omitted, or paraphrased into something less alarming.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from ..base.agent import LLMAgent
from ..db import enums
from ..services import receipts

def _family_of(source_type: str) -> str:
    """The signal family a source type belongs to (9.11).

    Local so `alerting` does not import the scoring module for one lookup, and
    tolerant of an unknown type so an old row cannot break a caveat.
    """
    from ..base.scoring import SOURCE_TYPE_FAMILY

    return SOURCE_TYPE_FAMILY.get(source_type, source_type)


class AlertAgent(LLMAgent):
    """Turns alertable correlations into alerts with evidence links."""

    stage = "ALERTING"

    @property
    def system_prompt(self) -> str:
        """The mission's alert prompt. No engine fallback, deliberately.

        A default here would write prose for a reader the mission never named,
        in a register nobody chose, and it would look exactly like prose that
        had been written on purpose.
        """
        mission = getattr(self.db, "mission", None)
        if mission is None:
            raise RuntimeError(
                "Alerting cannot run without a mission: the summary prompt "
                "comes from the pack. Configure `mission.name`.")
        return mission.prompts["alert"]

    @property
    def prompt_version(self) -> str:
        return getattr(self.db, "mission").prompt_versions["alert"]

    @property
    def alert_max_tokens(self) -> int:
        """Output ceiling for the summary call — `alerting.max_tokens`.

        Separate from `llm.max_tokens` because the two calls want opposite
        things: triage returns a verdict per post in a batch, an alert returns
        two sentences. It was hardcoded at 400 and every live alert overran it,
        so the prose anyone read was always the deterministic fallback.
        """
        return int((self.config.get("alerting") or {}).get("max_tokens", 4096))

    def _execute(self, iteration_id: int, **kwargs: Any) -> None:
        iteration = self.db.get_iteration(iteration_id)
        if iteration is None:
            raise ValueError(f"No iteration {iteration_id}")
        session_id = int(iteration["session_id"])
        floor = float(self.config.get("correlation", {}).get("alert_min_score", 0.15))

        counts = {"considered": 0, "written": 0, "below_floor": 0, "fallback": 0}

        for correlation in self.db.get_correlations(iteration_id):
            counts["considered"] += 1
            band = correlation["band"]
            correlation_id = int(correlation["correlation_id"])
            score = float(correlation["score"])
            # 8.7(b). Record the decision where it is made. A correlation that
            # becomes no alert has no alert row, and every route into the
            # evidence surface resolves `alerts.correlation_id` first — so
            # without this the reason existed only as the arithmetic gap between
            # `score` and a config value the reader would have to already know
            # about, and only for someone willing to open the database.
            if band not in enums.ALERT_BANDS:
                counts["below_floor"] += 1
                self.db.set_alert_decision(
                    correlation_id, "BAND_NONE",
                    f"band {band} does not qualify for an alert; scored "
                    f"{score:.3f} over {correlation['distinct_types']} distinct "
                    f"signal type(s) — {correlation['rule_trace']}")
                continue
            if score < floor:
                counts["below_floor"] += 1
                self.db.set_alert_decision(
                    correlation_id, "BELOW_FLOOR",
                    f"score {score:.3f} is below correlation.alert_min_score "
                    f"({floor:g}); band would have been {band} over "
                    f"{correlation['distinct_types']} distinct signal type(s)")
                continue
            if self.db.one("SELECT 1 FROM alerts WHERE correlation_id = ?",
                           (correlation_id,)):
                continue          # resumed stage; already written

            evidence = self.db.correlation_signals(correlation["correlation_id"])
            city = self.db.one("SELECT * FROM cities WHERE city_id = ?",
                               (correlation["city_id"],))
            summary, used_fallback, receipt_id = self._summarise(
                correlation, city, evidence, iteration_id
            )
            counts["fallback"] += int(used_fallback)

            self.db.insert_alert(
                correlation_id=int(correlation["correlation_id"]),
                session_id=session_id, iteration_id=iteration_id,
                city_id=int(correlation["city_id"]),
                track=correlation["track"],
                # Copied, never recomputed and never model-influenced.
                confidence_score=float(correlation["score"]),
                confidence_band=band,
                summary=summary,
                caveat=self._caveat(correlation),
                earliest_eta=_earliest_eta(evidence),
                model=self.model if not used_fallback else f"{self.model}/fallback",
                # NULL on the fallback path: no model call produced this
                # sentence, so there is nothing to attribute and inventing a
                # receipt would misrepresent a deterministic string as a
                # judgement.
                receipt_id=None if used_fallback else receipt_id,
            )
            self.db.set_alert_decision(
                correlation_id, "ALERTED",
                f"score {score:.3f} at or above correlation.alert_min_score "
                f"({floor:g}) with band {band} — {correlation['rule_trace']}")
            counts["written"] += 1

        self._log(
            "INFO",
            f"Alerting: {counts['written']} written from {counts['considered']} "
            f"correlation(s); {counts['below_floor']} below the floor, "
            f"{counts['fallback']} used the deterministic fallback",
            iteration_id=iteration_id, **counts,
        )

    # ------------------------------------------------------------------
    # Prose
    # ------------------------------------------------------------------

    def _summarise(
        self, correlation: Mapping[str, Any], city: Mapping[str, Any] | None,
        evidence: Sequence[Mapping[str, Any]], iteration_id: int,
    ) -> tuple[str, bool, int | None]:
        """Returns (summary, used_fallback, receipt_id)."""
        brief = self._brief(correlation, city, evidence)
        try:
            result, echo, accepted = self._call_llm_json(
                json.dumps(brief, indent=1), self.system_prompt,
                iteration_id=iteration_id,
                max_tokens=self.alert_max_tokens,
                ceiling_setting="alerting.max_tokens",
            )
        except Exception as exc:  # noqa: BLE001 — recorded, alert still written
            self._log("WARNING", f"Alert summary failed, using fallback: {exc}",
                      iteration_id=iteration_id,
                      correlation_id=correlation["correlation_id"])
            return _fallback_summary(brief), True, None

        receipt_id = self.db.insert_receipt(iteration_id, receipts.Receipt(
            kind="ALERT",
            provider=self.config.get("llm", {}).get("base_url"),
            model_requested=self.model,
            prompt_version=self.prompt_version,
            prompt_hash=receipts.sha256_hex(self.system_prompt),
            prompt_user_hash=receipts.sha256_hex(accepted),
            mission_id=getattr(self.db.mission, "label", None),
            mission_hash=getattr(self.db.mission, "digest", None),
            config_hash=receipts.config_fingerprint(self.config),
            batch_key=f"correlation:{correlation['correlation_id']}",
            input_hash=receipts.evidence_hash([brief]),
            temperature=self.temperature,
            max_tokens=self.alert_max_tokens,
            echo=echo,
        ).as_row())

        summary = ""
        if isinstance(result, Mapping):
            summary = str(result.get("summary") or "").strip()
        elif isinstance(result, str):
            summary = result.strip()
        if not summary:
            # The call succeeded but said nothing usable. The receipt still
            # stands — it records a real call — but the sentence is ours, so
            # the alert must not claim the model wrote it.
            return _fallback_summary(brief), True, receipt_id
        return summary[:1000], False, receipt_id

    def _brief(
        self, correlation: Mapping[str, Any], city: Mapping[str, Any] | None,
        evidence: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """The evidence the model is shown.

        Deliberately excludes the score and the band. The model has no reason to
        know them, and showing it a number it is told not to characterise is an
        invitation to characterise it anyway.
        """
        name = (city["name"] if city else "") or "unknown"
        state = (city["state"] if city else "") or ""
        brief: dict[str, Any] = {
            "city": f"{name}, {state}" if state else name,
            "track": correlation["track"],
            "social_posts": [], "flights": [], "lodging": [], "rental_cars": [],
        }
        for row in evidence:
            kind = row["signal_type"]
            if kind == "SOCIAL":
                brief["social_posts"].append({
                    "source": row["source_domain"] or row["platform"],
                    # Which mission stream found it — context for the writer,
                    # never an instruction; the prompt's own rules still
                    # forbid characterising anything.
                    "stream": (row["stream"]
                               if "stream" in row.keys() else None) or "",
                    "when": row["observed_at"],
                    "text": (row["snippet"] or "")[:300],
                    "activity": row["activity_type"],
                })
            elif kind == "FLIGHT":
                brief["flights"].append({
                    "callsign": row["callsign"], "type": row["aircraft_type"],
                    "from": row["origin_iata"], "to": row["dest_iata"],
                    "category": row["flight_category"],
                    "category_certainty": row["category_confidence"],
                    "status": row["flight_status"], "eta": row["eta"],
                })
            elif kind == "LODGING":
                brief["lodging"].append({
                    "property": row["item_name"],
                    "availability_drop_pct": row["drop_pct"],
                    "price_near": row["price_near"],
                    "price_baseline": row["price_baseline"],
                    "km_from_facility": row["distance_km"],
                })
            elif kind == "CAR":
                brief["rental_cars"].append({
                    "pickup": row["provider_ref"],
                    "class": row["vehicle_class_name"] or row["vehicle_class"],
                    "seats": row["people_capacity"],
                    "availability_drop_pct": row["drop_pct"],
                })
        return brief

    def _caveat(self, correlation: Mapping[str, Any]) -> str | None:
        """Deterministic data-gap disclosure, rebuilt from the stored row.

        Written here rather than by the model so it cannot be softened or
        dropped. Phrased to say what an absent source does and does not mean.
        """
        import json as _json

        from ..base.scoring import staleness_note

        keys = correlation.keys() if hasattr(correlation, "keys") else correlation
        stale = staleness_note(
            _json.loads(correlation["evidence_freshness_json"] or "null")
            if "evidence_freshness_json" in keys else None)
        failed = [f for f in (correlation["failed_sources"] or "").split(",") if f]
        if not failed:
            return stale or None
        completeness = float(correlation["data_completeness"])
        # 9.11. `failed_sources` is `SOURCE_TYPE:endpoint`; `failed_families`
        # is the subset of families with NOTHING collected. A family whose
        # sibling endpoint succeeded is DEGRADED, not absent — completeness did
        # not count it, so the caveat must not call it unavailable either.
        keys = correlation.keys() if hasattr(correlation, "keys") else correlation
        gaps = [g for g in ((correlation["failed_families"] or "")
                            if "failed_families" in keys else "").split(",") if g]
        partial = sorted(
            entry for entry in failed
            if _family_of(entry.split(":", 1)[0]) not in gaps
        )
        text = ""
        if gaps:
            text = (
                f"Collection incomplete: "
                f"{', '.join(g.lower() for g in sorted(gaps))} unavailable "
                f"this iteration (coverage {completeness:.0%}). Absence of "
                f"those indicators is not evidence of their absence."
            )
        if partial:
            text += (
                (" " if text else "")
                + f"Partly degraded: {', '.join(partial)} failed, but another "
                f"endpoint in that family was collected."
            )
        if correlation["band_capped"]:
            text += " Confidence capped below HIGH as a result."
        if stale:
            text += (" " if text else "") + stale
        return text or None


# ---------------------------------------------------------------------------


def _earliest_eta(evidence: Sequence[Mapping[str, Any]]) -> str | None:
    """Soonest ETA among inbound aircraft — what makes a warning tactical."""
    etas = [
        str(row["eta"]) for row in evidence
        if row["signal_type"] == "FLIGHT"
        and row["flight_status"] == "airborne_inbound" and row["eta"]
    ]
    return min(etas) if etas else None


def _fallback_summary(brief: Mapping[str, Any]) -> str:
    """Deterministic prose for when the model is unavailable.

    Flat and mechanical, which is the point: an alert that reads awkwardly still
    reaches its reader, whereas a scored finding held back because the model
    was down reaches nobody.
    """
    parts: list[str] = []
    counts = {family: len(brief.get(key) or [])
              for family, key in (("social", "social_posts"),
                                  ("flight", "flights"),
                                  ("lodging", "lodging"),
                                  ("car", "rental_cars"))}
    if counts["social"]:
        parts.append(f"{counts['social']} social media report(s)")
    if counts["flight"]:
        confirmed = sum(
            1 for f in brief["flights"]
            if f.get("category_certainty") == "CONFIRMED"
            and f.get("category") == "M"
        )
        parts.append(
            f"{counts['flight']} inbound aircraft"
            + (f" ({confirmed} confirmed military/government)" if confirmed else "")
        )
    if counts["lodging"]:
        drops = [l.get("availability_drop_pct") or 0 for l in brief["lodging"]]
        parts.append(f"lodging availability down up to {max(drops):.0f}%")
    if counts["car"]:
        drops = [c.get("availability_drop_pct") or 0 for c in brief["rental_cars"]]
        parts.append(f"rental car availability down up to {max(drops):.0f}%")

    if not parts:
        return (
            f"Correlated indicators for {brief['city']} "
            f"({brief['track'].lower().replace('_', '-')} track)."
        )
    return (
        f"{brief['city']}: " + "; ".join(parts) +
        f". Track: {brief['track'].lower().replace('_', '-')}."
    )
