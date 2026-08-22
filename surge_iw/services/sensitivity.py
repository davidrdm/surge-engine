"""Candidate observation versus confirmed operational signal.

The two-domain corroboration gate applies **only when admitting a new city**.
`admit_city` returns early for a city that already exists — seeded by the user
or admitted by an earlier tip — before every gate. The consequence, verified
against the code rather than assumed:

  * one accepted post produces a signal for a seeded city, with no
    corroboration required, ever;
  * with `min_salience: 0.0`, a post whose salience is *missing* produced a
    signal, because the coercion returned None, `or 0.0` made it zero, and
    `0.0 < 0.0` is false;
  * `run_tip` had no salience gate and no timestamp gate, so that signal booked
    the full paid follow-on set — flights, lodging, cars — for the city;
  * and an undated signal is then excluded from correlation by the window
    check. Money spent on evidence that cannot score.

None of that is a bug in any one function. It is the absence of a distinction:
between *something worth recording* and *something worth acting on*.

    CANDIDATE   recorded, visible, reviewable. Does not score and cannot tip.
    CONFIRMED   scores, and may spend money.

**The thresholds here are interim and deliberately conservative.** The owner's
decision was to build the distinction and the measurement first and set the
floors from the adversarial matrix afterwards, rather than from intuition. Until
that measurement exists, no document may describe the corroboration gate as
protecting operational signals — it protects city admission and nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from ..db.database import parse_iso, utcnow

RULES_VERSION = "sensitivity/1"

#: Interim defaults, to be replaced by measured values from the adversarial
#: matrix. Chosen to be conservative in the direction that costs a delayed
#: alert rather than a manufactured one.
DEFAULTS: dict[str, Any] = {
    # A signal must clear this to be CONFIRMED and score. Below it the
    # observation is still recorded as a CANDIDATE.
    "confirm_min_salience": 0.35,
    # Paid follow-on collection needs more than scoring does: a query is a
    # purchase, and a wrong one is unrecoverable.
    "tip_min_salience": 0.5,
    # A signal with no usable timestamp cannot be placed in the correlation
    # window, so acting on it spends money on evidence that cannot score.
    "tip_require_timestamp": True,
    # How old an observation may be and still justify buying collection.
    "tip_max_age_hours": 48.0,
    # And how far into the future a claimed observation time may sit before it
    # is treated as unusable rather than prescient.
    "max_future_skew_hours": 6.0,
}


@dataclass(frozen=True)
class TipDecision:
    """Whether a signal may spend money, and why not if not."""

    allowed: bool
    reason: str


def settings(config: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update(config.get("sensitivity") or {})
    return merged


def observed_at(value: Any) -> datetime | None:
    """Parse an observation time, or report that there is not one.

    A string that does not parse is not a timestamp. Treating it as one is how
    an undated post reached the tipping rules.
    """
    if isinstance(value, datetime):
        return value
    return parse_iso(value) if value else None


def classify(
    item: Any, observed: Any, config: Mapping[str, Any],
    *, now: datetime | None = None,
) -> tuple[str, str]:
    """CONFIRMED or CANDIDATE, with the reason recorded either way.

    `item` is a validated `TriageItem`. The reason is persisted on the signal so
    an operator can see why a post they can read in the evidence list did not
    contribute to a score.
    """
    cfg = settings(config)
    moment = observed_at(observed)
    now = now or utcnow()

    if item.salience < float(cfg["confirm_min_salience"]):
        return "CANDIDATE", (
            f"salience {item.salience:.2f} below the confirmation floor "
            f"{cfg['confirm_min_salience']}")
    if moment is None:
        return "CANDIDATE", (
            "no usable observation time, so it cannot be placed in the "
            "correlation window")
    skew = float(cfg["max_future_skew_hours"])
    if moment > now + timedelta(hours=skew):
        return "CANDIDATE", (
            f"observation time is more than {skew:g}h in the future")
    return "CONFIRMED", "meets the confirmation floor with a usable timestamp"


def may_tip(
    signal: Mapping[str, Any], config: Mapping[str, Any],
    *, now: datetime | None = None,
) -> TipDecision:
    """Whether a social signal may book paid follow-on collection.

    A strictly higher bar than scoring, because the failure modes differ. A
    weak signal that scores produces a LOW alert a reader can dismiss; a weak
    signal that tips spends FR24 credits that bill per record returned and
    cannot be refunded.
    """
    cfg = settings(config)
    now = now or utcnow()

    if (signal.get("signal_state") or "CONFIRMED") != "CONFIRMED":
        return TipDecision(False, "signal is a candidate, not confirmed")

    salience = float(signal.get("salience") or 0.0)
    floor = float(cfg["tip_min_salience"])
    if salience < floor:
        return TipDecision(
            False, f"salience {salience:.2f} below the tipping floor {floor}")

    moment = observed_at(signal.get("observed_at"))
    if moment is None:
        if cfg["tip_require_timestamp"]:
            return TipDecision(
                False,
                "no usable observation time; collection bought on an undated "
                "signal cannot correlate with it")
        return TipDecision(True, "undated, but timestamps are not required")

    age = (now - moment).total_seconds() / 3600.0
    max_age = float(cfg["tip_max_age_hours"])
    if age > max_age:
        return TipDecision(
            False,
            f"observed {age:.1f}h ago, beyond the {max_age:g}h tipping window")
    return TipDecision(True, "confirmed, recent and above the tipping floor")
