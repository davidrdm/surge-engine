"""Shared fixtures. Phase 1 has no LLM and no network dependency."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from surge_iw.config import load_config          # noqa: E402
from surge_iw.db.database import SurgeDB, iso     # noqa: E402
from surge_iw.services.budget import BudgetGuard  # noqa: E402
from surge_iw.services.mission import load as load_mission  # noqa: E402
from surge_iw.base.scoring import TrackModel  # noqa: E402

ANCHOR = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


#: The pack the suite runs against. Deliberately the SYNTHETIC one: if the
#: tests ran on the real mission they would prove the engine works for that
#: mission, which is the thing this refactor was meant to stop assuming.
REFERENCE = Path(__file__).resolve().parents[1] / "missions" / "reference"


#: Loaded once at import so tests that build their own SurgeDB outside a
#: fixture — the hypothesis property tests, mainly — can reach it too.
REFERENCE_MISSION = load_mission(REFERENCE)


#: Harvested words that are ordinary English in a codebase, and the reason.
#:
#: Kept to the smallest set that works and pinned by a test, because this is the
#: one place a real leak could hide. `poll` arrives from a pack's
#: `facility_tokens` — the list a pack writes to say which words are too common
#: to identify anything — and it is also what an HTTP client does to a
#: `poll_url` sixty-eight times in this repository.
ORDINARY_ENGLISH: frozenset[str] = frozenset({"poll"})


def mission_installed() -> bool:
    """Whether any pack other than the engine's own fixture is present.

    The vocabulary scans have nothing to detect without one — no second
    mission, no second mission's words — and that is a legitimate state: it is
    what shipping the engine alone looks like. It is NOT the same as the scan
    passing, so the tests skip with a reason rather than going quietly green.
    """
    packs = Path(__file__).resolve().parents[1] / "missions"
    return any(p.is_dir() and p.name != "reference" for p in packs.iterdir())


def mission_terms() -> tuple[str, ...]:
    """The harvested vocabulary itself, for a scan to prove itself against.

    Same source as `mission_vocabulary`, returned as terms rather than as a
    pattern, so a self-check can assert the pattern sees them without any test
    file having to write one down.
    """
    pattern = mission_vocabulary()
    packs = Path(__file__).resolve().parents[1] / "missions"
    out: set[str] = set()
    for pack in sorted(p for p in packs.iterdir() if p.is_dir()):
        if pack.name == "reference":
            continue
        loaded = load_mission(pack)
        candidates = (list(loaded.tracks) + list(loaded.location_types)
                      + [t for groups in loaded.lexicon.values()
                         for group in groups for t in group]
                      + [t for stream in getattr(loaded, "streams", ())
                         for groups in stream.lexicon.values()
                         for group in groups for t in group]
                      + [s.id for s in getattr(loaded, "streams", ())]
                      + list(loaded.facility_aliases)
                      + list(loaded.facility_aliases.values()))
        out.update(t for t in candidates if pattern.search(t))
    return tuple(sorted(out))


def mission_vocabulary():
    """Words the ENGINE must not contain, harvested from the packs on disk.

    The engine is supposed to know nothing about any particular mission. A scan
    for that property had the obvious problem: to detect a word it has to spell
    it, so the guard became the last place in the engine holding the vocabulary
    it existed to keep out — and it only ever knew the terms whoever wrote it
    thought of.

    So the terms come from the packs themselves. Every installed mission except
    `reference` — which IS the engine's fixture and may be named freely —
    contributes its tracks, its location types, its search lexicon, and the
    facility words and spellings it calls generic. A pack added tomorrow is
    covered the day it lands, and a pack that leaves takes its vocabulary out
    of this scan with it.

    Two things are deliberately NOT harvested. `equivalents` is US geography —
    a county and its seat — and `geo.CITY_AIRPORTS` carries the same places
    because a county's airports are not a mission's opinion. `publishers` are
    real outlets, named by their real domains.

    Identifiers and prose are matched differently, and it matters: a pack's
    `location_types` may legitimately include `OTHER`, and a case-insensitive
    scan for that word finds `other` in half the codebase. Identifiers are
    matched EXACTLY, as the upper-snake tokens they are written as; a pack's
    search terms and facility words are matched case-insensitively, because
    they are English and a docstring will not capitalise them.
    """
    import re as _re
    packs = Path(__file__).resolve().parents[1] / "missions"
    identifiers: set[str] = set()
    prose: set[str] = set()
    shared: set[str] = set()          # anything the reference pack says too
    for pack in sorted(p for p in packs.iterdir() if p.is_dir()):
        loaded = load_mission(pack)
        if pack.name == "reference":
            shared.update(loaded.tracks)
            shared.update(loaded.location_types)
            shared.update(loaded.facility_tokens)
            continue
        prose.add(loaded.identifier)
        identifiers.update(loaded.tracks)
        identifiers.update(loaded.location_types)
        for groups in loaded.lexicon.values():
            for group in groups:
                prose.update(group)
        # v0.2 streams: ids and per-stream lexicons are the pack's vocabulary
        # exactly as the mission-level lexicon is; a promoted family name is
        # an identifier the engine must never spell.
        for stream in getattr(loaded, "streams", ()):
            prose.add(stream.id)
            if stream.family != "SOCIAL":
                identifiers.add(stream.family)
            for groups in stream.lexicon.values():
                for group in groups:
                    prose.update(group)
        prose.update(loaded.facility_tokens)
        prose.update(loaded.facility_aliases)
        prose.update(loaded.facility_aliases.values())
        prose.update(loaded.facility_spellings)
        prose.update(loaded.facility_spellings.values())

    # A term the reference pack uses as well is not distinctive to anyone —
    # `OTHER` is a location type in both packs — and the reference pack is the
    # engine's own fixture, so naming it is explicitly fine.
    identifiers -= shared
    prose -= shared
    prose -= ORDINARY_ENGLISH

    def alternation(terms: set[str]) -> str:
        # Longest first, so a phrase reports as the phrase and not as its
        # first word. Spaces match any run of whitespace, because prose wraps.
        #
        # `(?!)` for an empty set, and this is not a detail. An empty
        # alternation compiles to `\b(?:)\b`, which matches the EMPTY STRING at
        # every word boundary — so a checkout with no second pack made the scan
        # report every line of every file rather than nothing. A guard whose
        # subject is absent must match nothing, and `mission_installed()` below
        # is what turns that into a visible skip instead of a silent pass.
        alternatives = "|".join(
            _re.escape(t).replace(r"\ ", r"\s+")
            for t in sorted(terms, key=len, reverse=True) if len(t) > 2)
        return alternatives or "(?!)"

    return _re.compile(
        rf"\b(?:{alternation(identifiers)})\b"
        rf"|(?i:\b(?:{alternation(prose)})\b)")


@pytest.fixture(scope="session")
def mission():
    return REFERENCE_MISSION


@pytest.fixture
def config(mission) -> dict[str, Any]:
    """Default config, unaffected by any config.yaml on the developer's disk.

    Layered as the application layers it: engine defaults, then the mission's
    thresholds. Without the second layer a test would read illustrative
    placeholder numbers that no deployment ever runs on.
    """
    return load_config(None, mission=mission)


#: Correlation settings for the scoring suites, PINNED here rather than read
#: from whichever mission pack is loaded.
#:
#: Those tests assert exact scores and exact bands: they are testing
#: `correlate()`'s arithmetic, and a mission that picks a different window or a
#: different full-scale is not a bug in it. Tests that care what a real
#: deployment would compute read `config["correlation"]` instead, which carries
#: the mission's own thresholds.
FIXED_CORRELATION = {'alert_min_score': 0.15,
                     'band_high_min_score': 0.75,
                     'band_high_min_types': 3,
                     'band_low_min_reports': 2,
                     'band_low_min_score': 0.15,
                     'band_medium_min_score': 0.45,
                     'band_medium_min_types': 2,
                     'car_drop_full_scale': 50.0,
                     'decay_edge_weight': 0.1,
                     'flight_baseline_min_samples': 3,
                     'flight_baseline_window_days': 30,
                     'flight_excess_full_scale': 100.0,
                     'flight_full_scale': 3.0,
                     'lodging_drop_full_scale': 50.0,
                     'on_airport_weight': 1.5,
                     'price_escalation_full_scale': 40.0,
                     'radius_km': 15.0,
                     'single_source_quality': 0.6,
                     'social_domains_full_scale': 3.0,
                     'window_hours': 168}


@pytest.fixture
def corr_cfg() -> dict[str, Any]:
    return dict(FIXED_CORRELATION)


@pytest.fixture
def db(mission) -> SurgeDB:
    database = SurgeDB(":memory:", mission=mission)
    yield database
    database.close()


#: Fixture track models, shared by every suite that scores something.
#:
#: Weights are pinned HERE rather than read from a pack, because these tests
#: assert exact scores: they are testing `correlate()`'s arithmetic, and a
#: mission that chooses different weights is not a bug in it. The two shapes
#: that matter are "flies military-coded aircraft" and "does not".
AIRLIFT = TrackModel(
    name="AIRSHOW",
    weights={"social": 0.30, "flight_M": 0.35, "flight_J": 0.10,
             "lodging": 0.15, "car": 0.10},
    flight_categories=("M", "J"),
)
CHARTER = TrackModel(
    name="CONCERT_TOUR",
    weights={"social": 0.40, "flight_M": 0.00, "flight_J": 0.25,
             "lodging": 0.20, "car": 0.15},
    flight_categories=("J", "T", "H"),
)
TRACKS = {t.name: t for t in (AIRLIFT, CHARTER)}


def track_model(track):
    """Accept a fixture model or the name of one."""
    return TRACKS[track] if isinstance(track, str) else track


@pytest.fixture
def track(mission):
    """A TrackModel for the reference mission's first track.

    Most tests need *a* track rather than a particular one; those that care
    about a specific track's weights build their own.
    """
    from surge_iw.base.scoring import TrackModel
    return TrackModel.from_mission(mission, mission.tracks[0])


@pytest.fixture
def budget(db: SurgeDB, config: dict[str, Any]) -> BudgetGuard:
    guard = BudgetGuard(db, config)
    guard.seed_budgets()
    return guard


@pytest.fixture
def session(db: SurgeDB) -> int:
    return db.insert_session(label="test", expand_cities=False)


@pytest.fixture
def iteration(db: SurgeDB, session: int) -> int:
    return db.insert_iteration(session, anchor_at=ANCHOR)


# ---------------------------------------------------------------------------
# Signal builders. Return plain dicts so scoring can be tested without a DB.
# ---------------------------------------------------------------------------


def social(
    *,
    domain: str = "example.com",
    salience: float = 1.0,
    hours_ago: float = 1.0,
    track: str = "UNKNOWN",
    signal_id: int | None = None,
    anchor: datetime = ANCHOR,
) -> dict[str, Any]:
    return {
        "signal_id": signal_id,
        "signal_type": "SOCIAL",
        "source_domain": domain,
        "platform": "twitter",
        "salience": salience,
        "track": track,
        "observed_at": iso(anchor - timedelta(hours=hours_ago)),
        "url": f"https://{domain}/p/{signal_id or 0}",
    }


def flight(
    *,
    category: str = "M",
    confidence: str = "CONFIRMED",
    fr24_id: str = "abc123",
    hours_ago: float = 1.0,
    status: str = "airborne_inbound",
    eta: str | None = None,
    track: str = "UNKNOWN",
    signal_id: int | None = None,
    anchor: datetime = ANCHOR,
) -> dict[str, Any]:
    return {
        "signal_id": signal_id,
        "signal_type": "FLIGHT",
        "flight_category": category,
        "category_confidence": confidence,
        "fr24_id": fr24_id,
        "flight_status": status,
        "eta": eta,
        "track": track,
        "observed_at": iso(anchor - timedelta(hours=hours_ago)),
    }


def lodging(
    *,
    near_available: int = 5,
    base_available: int = 30,
    distance_km: float = 3.0,
    hours_ago: float = 1.0,
    provider_ref: str = "L1",
    signal_id: int | None = None,
    anchor: datetime = ANCHOR,
) -> dict[str, Any]:
    return {
        "signal_id": signal_id,
        "signal_type": "LODGING",
        "near_available": near_available,
        "base_available": base_available,
        "distance_km": distance_km,
        "provider_ref": provider_ref,
        "track": "UNKNOWN",
        "observed_at": iso(anchor - timedelta(hours=hours_ago)),
    }


def car(
    *,
    near_available: int = 2,
    base_available: int = 20,
    people_capacity: int = 5,
    is_on_airport: int = 1,
    is_peer_to_peer: int = 0,
    truncated: int = 0,
    distance_km: float = 3.0,
    vehicle_class: str = "ECAR",
    hours_ago: float = 1.0,
    signal_id: int | None = None,
    anchor: datetime = ANCHOR,
) -> dict[str, Any]:
    return {
        "signal_id": signal_id,
        "signal_type": "CAR",
        "near_available": near_available,
        "base_available": base_available,
        "people_capacity": people_capacity,
        "is_on_airport": is_on_airport,
        "is_peer_to_peer": is_peer_to_peer,
        "truncated": truncated,
        "distance_km": distance_km,
        "vehicle_class": vehicle_class,
        "track": "UNKNOWN",
        "observed_at": iso(anchor - timedelta(hours=hours_ago)),
    }


# ---------------------------------------------------------------------------
# Opting in to the live model
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    parser.addoption(
        "--live-model", action="store_true", default=False,
        help="Run the adversarial matrix against the configured LLM. Costs "
             "tokens; contacts no data vendor and spends no collection credit.",
    )


def pytest_configure(config):
    """Let --live-model win over pytest.ini's exclusion.

    `addopts = -m "not live and not live_model"` is evaluated after collection,
    so stripping the marker from the item is not enough — the expression has to
    change.
    """
    if config.getoption("--live-model"):
        config.option.markexpr = "not live"


@pytest.fixture
def live_model_client(pytestconfig):
    """The real OpenAI-compatible client, or a skip."""
    if not pytestconfig.getoption("--live-model"):
        pytest.skip("needs --live-model")
    from surge_iw.config import ConfigError, build_llm_client, load_config

    try:
        return build_llm_client(load_config(None))
    except (ConfigError, ImportError) as exc:
        pytest.skip(f"no model client available: {exc}")
