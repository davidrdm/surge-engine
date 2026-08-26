"""Loading a mission definition from a pack of files.

The engine collects social, flight, lodging and car-rental data and correlates
it. What it is *looking for* — who the actors are, what words find them, what an
observation is worth, and what makes an item relevant at all — is not the
engine's business, and until now it was indistinguishable from it: two system
prompts as Python constants, a search lexicon as a literal dict, a weight matrix
keyed by actor track, and three vocabularies written into SQLite CHECK clauses.

A mission pack is a directory of data files read at initialization. It carries
everything an analyst would have to change to point this instrument at a
different question, and nothing an engineer would have to change to fix a bug.

**The pack is the audit unit.** `mission.yaml` declares its members, and the
loader refuses a declared file that is missing *and* an undeclared file that is
present. A stray file in the directory that might or might not be read is not
acceptable in something whose whole purpose is making a judgement
reconstructible. `digest` hashes every declared member, so the receipt for an
iteration names the exact bytes that produced it.

**Unknown is refused, never ignored** — the rule `services/tunables.py` already
establishes. A misspelled key is an error naming the key, not a silent default.
The failure this prevents is specific: a lexicon whose track name is misspelled
would search nothing for that track, and the run would report a quiet city.

**There is no partial mission.** Every accessor raises on a pack that failed to
load rather than returning an empty default, because an empty lexicon and an
absent one produce the same collection and mean opposite things.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from ..db.enums import UNATTRIBUTED
from . import geo, hypotheses

#: Where the engine looks for a named mission pack.
DEFAULT_MISSION_DIR = "./missions"

#: The manifest, always at the root of a pack.
MANIFEST = "mission.yaml"

#: A name the loader will accept. Deliberately not a path, for the same reason
#: `services/inputs.py` refuses one: the mission name reaches this from
#: configuration an operator writes, but a field that reads a path is a
#: file-disclosure primitive whatever the intent behind it, and there is no
#: reason a mission should ever live outside its directory.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: A vocabulary member — a track name or a location type. Upper snake case,
#: because these are stored in the database and compared exactly. Lowercase
#: would work until the day two packs disagreed about the case of one word.
_VOCAB_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: Scoring kinds a weight table may name. These are engine vocabulary, not
#: mission vocabulary: they name the four data families and the FR24 category
#: split within FLIGHT, which is what this engine collects regardless of what
#: it is pointed at. A mission chooses the *numbers*, not the rows.
SCORING_KINDS: tuple[str, ...] = (
    "social", "flight_M", "flight_J", "lodging", "car",
)

#: The non-social kinds, which no mission can rename or remove. With streams
#: declared, a track's weight rows are its stream ids plus exactly these.
NON_SOCIAL_KINDS: tuple[str, ...] = tuple(
    k for k in SCORING_KINDS if k != "social")

#: Platform names the social feed can collect. Engine vocabulary: they name
#: the three APIDirect endpoints, and `queueing.SOCIAL_ENDPOINTS` must carry
#: exactly these keys (a test holds the two together). A stream chooses which
#: of them it searches, not what they are.
SOCIAL_PLATFORMS: tuple[str, ...] = ("twitter", "reddit", "news")

#: Kind names a stream id may not take, case-insensitively. `social` is NOT
#: here: it names the implicit stream, and allowing a pack to declare it
#: explicitly is what makes "one stream called social over every platform
#: behaves exactly like no streams at all" a statement a test can make.
RESERVED_STREAM_IDS: frozenset[str] = frozenset(
    k.lower() for k in SCORING_KINDS if k != "social") | {"flight_ambiguous"}

#: Stream ids are lower snake — they ARE scoring kinds, and the weight table
#: spells kinds that way.
_STREAM_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Keys a stream entry in streams.yaml may carry.
STREAM_KEYS: frozenset[str] = frozenset({
    "platforms", "family", "lexicon", "relevance_strict", "relevance_broad",
})

#: The relevance legs a stream may override, per leg independently.
STREAM_PROMPT_SLOTS: tuple[str, ...] = ("relevance_strict", "relevance_broad")

#: Signal families a hypothesis set may key on. Same reasoning as SCORING_KINDS.
FAMILIES: tuple[str, ...] = ("SOCIAL", "FLIGHT", "LODGING", "CAR")

#: Which family each non-social scoring kind belongs to. Mirrors
#: `base.scoring.KIND_FAMILY` for the kinds a weight table names; kept here so
#: the loader does not import the scorer.
_KIND_FAMILY: dict[str, str] = {
    "flight_M": "FLIGHT", "flight_J": "FLIGHT",
    "lodging": "LODGING", "car": "CAR",
}

#: FR24 category codes. The vendor's vocabulary, not the mission's — a mission
#: says which codes each of its tracks flies in, not what the codes mean.
FLIGHT_CODES: frozenset[str] = frozenset({"M", "J", "T", "H"})

#: Prompt slots the engine asks for by name. A pack that omits one cannot run
#: the stage that needs it, so all four are required rather than optional.
PROMPTS: tuple[str, ...] = (
    "triage", "relevance_strict", "relevance_broad", "alert",
)

#: Directories a pack may carry that the loader does not read, and therefore
#: does not hash or require to be declared.
#:
#: `docs/` is the mission's own prose — requiring every note to be declared
#: would make writing one a schema change. `inputs/` is the operator's
#: geography for this mission, which travels with the pack for convenience and
#: is chosen per session by name; the resolved cities and key locations are
#: what the session records, not the file's name. `tests/`
#: holds the pack's own checks: a claim like "these 29 county-to-seat pairs are
#: hand-verified" is the PACK's to make, so it is the pack that must carry the
#: test, and the engine's suite collects it wherever the pack is mounted. The
#: digest covers what DEFINES the mission, not everything filed next to it —
#: and a test that could move the digest would make every receipt appear to
#: name a different definition the moment somebody added a case.
CARRIED_ALONGSIDE: frozenset[str] = frozenset({"docs", "inputs", "tests"})

#: Files, rather than directories, carried on the same terms. A pack should be
#: able to introduce itself to whoever opens the directory, and the alternative
#: to exempting one is worse both ways: declared, a typo fix in the prose moves
#: the digest and every receipt appears to name a different definition;
#: undeclared, the pack will not load at all.
CARRIED_FILES: frozenset[str] = frozenset({"README.md"})

#: Every key each pack file may carry. Named rather than inline because they
#: are the pack FORMAT, and `docs/missions.md` is pinned against them: a key
#: added to the loader and not to the guide is a format change an author would
#: only find by having their pack refused.
MANIFEST_KEYS: frozenset[str] = frozenset({
    "id", "version", "description", "files", "tracks", "location_types",
    "thresholds", "prompts", "collects",
})
#: The subset without which a pack cannot run. `description` and `thresholds`
#: are optional; `files` is checked separately, because an empty pack is legal
#: and a pack that declares nothing while holding files is not.
REQUIRED_MANIFEST_KEYS: tuple[str, ...] = (
    "id", "version", "tracks", "location_types", "prompts",
)
SCORING_KEYS: frozenset[str] = frozenset({
    "weights", "flight_categories", "baselined_categories",
})

#: Which of the engine's four families a mission collects at all. SOCIAL is
#: not optional: every paid family is TIPPED by a social judgement, so a pack
#: that dropped it would enqueue nothing and report every city as quiet.
COLLECTABLE_FAMILIES: frozenset[str] = frozenset(FAMILIES)
GEOGRAPHY_KEYS: frozenset[str] = frozenset({"equivalents", "publishers"})
FACILITY_KEYS: frozenset[str] = frozenset({"aliases", "tokens", "spellings"})

#: Config sections a mission may set. These are the analytic ones — what counts
#: as relevant, what clears a floor, how wide the window is, how the bands are
#: drawn. Everything else (credentials, endpoints, the database, the API's own
#: deployment settings) is the operator's and cannot be set by a pack.
THRESHOLD_SECTIONS: frozenset[str] = frozenset({
    "triage", "sensitivity", "windows", "correlation",
})


class MissionError(ValueError):
    """A pack that cannot be trusted. The message always names the file."""


@dataclass(frozen=True)
class Stream:
    """One named watch over the social feed.

    A stream is a lens: its own platform subset, its own per-track lexicon,
    its own scoring weight (a row in scoring.yaml keyed by this id), and
    optionally its own relevance criteria. Where it counts in banding is the
    `family` — SOCIAL means a sub-kind of the SOCIAL family, exactly as
    flight_M and flight_J are sub-kinds of FLIGHT; any other name promotes it
    to a family of its own, counting toward `distinct_types` and the
    completeness denominator.
    """

    id: str
    platforms: tuple[str, ...]
    family: str
    lexicon: dict[str, tuple[tuple[str, ...], ...]]
    #: slot -> (prompt text, version label), for the legs this stream
    #: overrides. A missing slot inherits the mission-level leg.
    relevance: dict[str, tuple[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Mission:
    """One loaded mission pack.

    Frozen because it is read by every stage of an iteration and by the receipt
    that records what produced the judgement. A mission that could be mutated
    mid-run would make `digest` a claim about a state that no longer exists.
    """

    identifier: str
    version: str
    description: str
    #: Where it was loaded from, for error messages and operator readouts.
    path: Path
    #: sha256 over every declared member. Recorded on each receipt.
    digest: str

    #: What this mission is looking for. Replaces the two tracks that used to
    #: be built into the engine; any number of tracks is legal.
    tracks: tuple[str, ...]
    #: What kind of place a key location can be.
    location_types: tuple[str, ...]

    #: Analytic config, layered under config.yaml. See `THRESHOLD_SECTIONS`.
    thresholds: dict[str, Any]

    #: track -> groups of search terms. A group becomes one OR-joined query.
    lexicon: dict[str, tuple[tuple[str, ...], ...]]

    #: track -> scoring kind -> weight.
    weights: dict[str, dict[str, float]]
    #: track -> the FR24 category codes it asks for.
    flight_categories: dict[str, tuple[str, ...]]
    #: Codes measured against a rolling baseline rather than counted outright.
    baselined_categories: frozenset[str]

    #: Places that are one operational unit under two names, unit -> other.
    equivalents: dict[str, str]
    #: The same table with its reverse index built, ready for `geo`. Built at
    #: load rather than per call: a hand-written reverse would be a consistency
    #: requirement with nothing enforcing it, and building it per lookup would
    #: rebuild it inside `resolve_city`'s ambiguity branch.
    jurisdictions: "geo.Equivalents"
    #: Publisher domain -> canonical name, for outlets this mission cares about.
    publishers: dict[str, str]

    #: Facility name spellings this mission should collapse.
    facility_aliases: dict[str, str]
    #: Words too generic to identify a facility on their own. ADDED to the
    #: engine's structural core, never replacing it.
    facility_tokens: frozenset[str]
    #: Abbreviation -> expansion, for abbreviations specific to this domain.
    #: Also added to the engine's generic table rather than replacing it.
    facility_spellings: dict[str, str]

    #: family -> competing explanations for a signal in that family.
    hypotheses: dict[str, tuple[dict[str, str], ...]]

    #: slot -> prompt text, keyed by `PROMPTS`.
    prompts: dict[str, str]
    #: slot -> the version label stamped on receipts. Owned by the pack,
    #: because a pack that changed a prompt and kept the label would make two
    #: different judgements indistinguishable in the audit trail.
    prompt_versions: dict[str, str]

    #: Every declared member, relative to `path`, with its own hash.
    members: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    #: The mission's streams over the social feed, in declaration order.
    #: Empty means the pack declared none and runs one IMPLICIT stream: every
    #: configured platform, the mission-level lexicon, the mission-level
    #: relevance legs, the weight row `social` — byte-identical to a pack
    #: written before streams existed.
    streams: tuple[Stream, ...] = ()
    #: The scoring kinds the social feed produces: the stream ids, or
    #: ("social",) for the implicit stream. Derived at load, stored so every
    #: consumer reads one answer.
    social_kinds: tuple[str, ...] = ("social",)
    #: stream id -> the family it counts as in banding and completeness.
    stream_families: dict[str, str] = field(
        default_factory=lambda: {"social": "SOCIAL"})
    #: Every family this mission's evidence can occupy: the engine's four,
    #: plus promoted stream families in declaration order after SOCIAL. The
    #: length of this tuple is the data_completeness denominator.
    families: tuple[str, ...] = FAMILIES
    #: Every kind a weight table names: social kinds + the non-social kinds
    #: of the families this mission collects. What `scoring.TrackModel`
    #: validates against.
    scoring_kinds: tuple[str, ...] = SCORING_KINDS
    #: The engine families this mission collects at all, in engine order.
    #: A family absent here is never queried, never scored, never counted in
    #: the completeness denominator and — this is the point — never a coverage
    #: gap: nothing was attempted, so nothing failed. Defaults to all four, so
    #: a pack that says nothing behaves exactly as before.
    collects: tuple[str, ...] = FAMILIES

    @property
    def label(self) -> str:
        return f"{self.identifier}/{self.version}"

    def track(self, value: str, field_name: str = "track") -> str:
        """Return `value` if this mission defines it, else raise by name."""
        if value not in self.tracks:
            raise MissionError(
                f"{field_name}={value!r} is not a track of mission "
                f"{self.label}; expected one of {list(self.tracks)}")
        return value

    def attribution(self, value: str, field_name: str = "track") -> str:
        """As `track`, but the unattributed marker also passes.

        Used where a signal or a model judgement is being stored: "nobody said
        who" is a legitimate answer there, and the alternative is a guess.
        """
        if value == UNATTRIBUTED:
            return value
        return self.track(value, field_name)

    def stream_id(self, value: str, field: str = "stream") -> str:
        """Return `value` if it names one of this mission's streams.

        Every stream-carrying write funnels through here, exactly as `track`
        and `location_type` do — the stream vocabulary is the pack's, so the
        pack is what a value is checked against. NULL never reaches here: it
        is the implicit stream and always legal.
        """
        if self.streams and value in self.stream_families:
            return value
        if not self.streams:
            raise MissionError(
                f"{field}={value!r} is not a stream of mission {self.label}: "
                f"this mission defines no streams, so only the implicit "
                f"stream (stored as NULL) exists.")
        raise MissionError(
            f"{field}={value!r} is not a stream of mission {self.label}; "
            f"expected one of {[s.id for s in self.streams]}")

    def stream_lexicons(self) -> list[tuple[str | None, dict]]:
        """(stream id, lexicon) per stream — or one (None, lexicon) pair for
        the implicit stream, so a caller writes one loop for both shapes."""
        if self.streams:
            return [(s.id, s.lexicon) for s in self.streams]
        return [(None, self.lexicon)]

    def location_type(self, value: str, field_name: str = "location_type") -> str:
        if value not in self.location_types:
            raise MissionError(
                f"{field_name}={value!r} is not a location type of mission "
                f"{self.label}; expected one of {list(self.location_types)}")
        return value

    def describe(self) -> list[str]:
        """One line per fact an operator should see at startup."""
        return [
            f"mission {self.label} from {self.path}",
            f"  digest {self.digest[:12]} over {len(self.members)} file(s)",
            f"  tracks: {', '.join(self.tracks)}",
            f"  location types: {', '.join(self.location_types)}",
            *self._describe_collects(),
            *self._describe_social(),
        ]

    def _describe_collects(self) -> list[str]:
        """Only when a family is switched OFF. A pack collecting all four is
        the ordinary case and does not need a line saying so; a pack that has
        stopped buying three vendors' data must say it out loud at startup,
        because the absence is otherwise indistinguishable from an outage."""
        absent = [f for f in FAMILIES if f not in self.collects]
        if not absent:
            return []
        return [f"  collects: {', '.join(self.collects)} "
                f"(NOT collecting {', '.join(absent)} — never queried, never "
                f"a coverage gap)"]

    def _describe_social(self) -> list[str]:
        if not self.streams:
            return [
                f"  lexicon: {sum(len(g) for g in self.lexicon.values())} "
                f"query group(s) across {len(self.lexicon)} track(s)",
            ]
        lines = [f"  streams: {len(self.streams)}; families: "
                 f"{', '.join(self.families)}"]
        for stream in self.streams:
            groups = sum(len(g) for g in stream.lexicon.values())
            overridden = ",".join(sorted(stream.relevance)) or "inherited"
            lines.append(
                f"    {stream.id}: {','.join(stream.platforms)} -> "
                f"{stream.family}; {groups} group(s); relevance {overridden}")
        return lines


# ---------------------------------------------------------------------------
# Locating a pack
# ---------------------------------------------------------------------------


def mission_dir(config: Mapping[str, Any] | None = None) -> Path:
    return Path(((config or {}).get("mission") or {}).get(
        "dir", DEFAULT_MISSION_DIR))


def mission_name(config: Mapping[str, Any] | None = None) -> str | None:
    name = ((config or {}).get("mission") or {}).get("name")
    return str(name) if name else None


def mission_path(name: str, config: Mapping[str, Any] | None = None) -> Path:
    """Resolve a NAME to a pack directory, or raise naming what was available."""
    if not _SAFE_NAME_RE.match(name or ""):
        raise MissionError(
            f"{name!r} is not a valid mission name. Give the NAME of a "
            f"directory inside the mission directory, not a path.")
    directory = mission_dir(config)
    candidate = directory / name
    if (candidate / MANIFEST).is_file():
        return candidate
    available = sorted(
        p.name for p in directory.iterdir()
        if p.is_dir() and (p / MANIFEST).is_file()
    ) if directory.is_dir() else []
    raise MissionError(
        f"No mission {name!r} in {directory}/. "
        + (f"Available: {', '.join(available)}." if available
           else f"No directory there holds a {MANIFEST}."))


# ---------------------------------------------------------------------------
# Reading and hashing members
# ---------------------------------------------------------------------------


def _member_path(root: Path, declared: str) -> Path:
    """Resolve one declared member, refusing anything outside the pack."""
    if not declared or declared.startswith("/") or "\\" in declared:
        raise MissionError(
            f"{MANIFEST}: {declared!r} must be a relative path inside the pack")
    resolved = (root / declared).resolve()
    if root.resolve() not in resolved.parents and resolved != root.resolve():
        raise MissionError(
            f"{MANIFEST}: {declared!r} escapes the pack directory")
    if not resolved.is_file():
        raise MissionError(
            f"{MANIFEST} declares {declared!r}, which does not exist in {root}")
    return resolved


def _declared_members(root: Path, declared: Iterable[str]) -> list[str]:
    """The manifest plus its declared members, checked both ways.

    Both directions matter. A declared file that is missing means a stage will
    run without the data it was promised. An undeclared file that is present
    means someone edited the pack and the digest would not notice — which is
    exactly the guarantee the digest exists to provide.
    """
    members = [MANIFEST]
    for entry in declared:
        if not isinstance(entry, str):
            raise MissionError(f"{MANIFEST}: files must be a list of paths")
        _member_path(root, entry)
        if entry in members:
            raise MissionError(f"{MANIFEST}: {entry!r} is declared twice")
        members.append(entry)

    present = {
        str(p.relative_to(root)).replace("\\", "/")
        for p in root.rglob("*")
        if p.is_file()
        and not set(p.relative_to(root).parts) & CARRIED_ALONGSIDE
        and p.name not in CARRIED_FILES
    }
    stray = sorted(present - set(members))
    if stray:
        raise MissionError(
            f"{root} holds file(s) no manifest declares: {', '.join(stray)}. "
            f"Add them to `files:` or remove them — the pack is hashed as a "
            f"whole, and a file that is neither loaded nor hashed is a change "
            f"nothing would record. Exempt: "
            f"{'/, '.join(sorted(CARRIED_ALONGSIDE))}/ and "
            f"{', '.join(sorted(CARRIED_FILES))}.")
    return members


def _digest(root: Path, members: Iterable[str]) -> tuple[str, tuple[tuple[str, str], ...]]:
    """sha256 over the sorted (path, content hash) list of every member."""
    pairs = []
    for name in sorted(members):
        data = (root / name).read_bytes()
        pairs.append((name, hashlib.sha256(data).hexdigest()))
    joined = "\n".join(f"{name}:{digest}" for name, digest in pairs)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest(), tuple(pairs)


def _yaml(root: Path, name: str) -> Any:
    try:
        with open(root / name, encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except FileNotFoundError as exc:
        # A named refusal, not a raw traceback. `_declared_members` already
        # refuses a DECLARED file that is missing; this covers the loader
        # asking for a file the manifest legitimately omitted — reachable the
        # day a caller's branching is wrong, and the message should say whose
        # fault that is.
        raise MissionError(
            f"{name} does not exist in this pack, but the loader asked for "
            f"it. Declare it in files:, or report this as an engine bug if "
            f"the manifest is correct.") from exc
    except yaml.YAMLError as exc:
        raise MissionError(f"{name}: not valid YAML — {exc}") from exc


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MissionError(f"{where}: expected a mapping, "
                           f"got {type(value).__name__}")
    return dict(value)


# ---------------------------------------------------------------------------
# Field validators, each refusing by name
# ---------------------------------------------------------------------------


def _vocabulary(values: Any, where: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or not values:
        raise MissionError(f"{where}: expected a non-empty list of names")
    out: list[str] = []
    for value in values:
        if not isinstance(value, str) or not _VOCAB_RE.match(value):
            raise MissionError(
                f"{where}: {value!r} must be upper snake case (A-Z, 0-9, _)")
        if value in out:
            raise MissionError(f"{where}: {value!r} is listed twice")
        out.append(value)
    return tuple(out)


def _known_tracks(mapping: Mapping[str, Any], tracks: tuple[str, ...],
                  where: str, *, complete: bool = True) -> None:
    """Refuse a track this mission does not define, and (by default) a missing
    one. A lexicon or weight table that silently skipped a track would collect
    or score nothing for it, and report that as a quiet result."""
    for key in mapping:
        if key not in tracks:
            raise MissionError(
                f"{where}: {key!r} is not a track of this mission; "
                f"expected one of {list(tracks)}")
    if complete:
        missing = [t for t in tracks if t not in mapping]
        if missing:
            raise MissionError(
                f"{where}: no entry for track(s) {', '.join(missing)}. Every "
                f"track needs one — an absent entry and an empty one produce "
                f"the same behaviour and mean opposite things.")


def _lexicon(raw: Any, tracks: tuple[str, ...], where: str = "lexicon.yaml"
             ) -> dict[str, tuple[tuple[str, ...], ...]]:
    data = _mapping(raw, where)
    _known_tracks(data, tracks, where)
    out: dict[str, tuple[tuple[str, ...], ...]] = {}
    for track, groups in data.items():
        if not isinstance(groups, (list, tuple)) or not groups:
            raise MissionError(
                f"{where}: {track} must be a non-empty list of term groups")
        built: list[tuple[str, ...]] = []
        for index, group in enumerate(groups):
            if not isinstance(group, (list, tuple)) or not group:
                raise MissionError(
                    f"{where}: {track}[{index}] must be a non-empty list "
                    f"of search terms")
            terms = []
            for term in group:
                if not isinstance(term, str) or not term.strip():
                    raise MissionError(
                        f"{where}: {track}[{index}] holds a term that is "
                        f"not a non-empty string")
                terms.append(term.strip())
            built.append(tuple(terms))
        out[track] = tuple(built)
    return out


def _weights(raw: Any, tracks: tuple[str, ...],
             expected_kinds: tuple[str, ...] = SCORING_KINDS
             ) -> dict[str, dict[str, float]]:
    data = _mapping(raw, "scoring.yaml: weights")
    _known_tracks(data, tracks, "scoring.yaml: weights")
    out: dict[str, dict[str, float]] = {}
    for track, kinds in data.items():
        values = _mapping(kinds, f"scoring.yaml: weights.{track}")
        unknown = sorted(set(values) - set(expected_kinds))
        if unknown:
            raise MissionError(
                f"scoring.yaml: weights.{track} names unknown scoring kind(s) "
                f"{', '.join(unknown)}; expected {list(expected_kinds)}")
        missing = [k for k in expected_kinds if k not in values]
        if missing:
            raise MissionError(
                f"scoring.yaml: weights.{track} has no weight for "
                f"{', '.join(missing)}. Give 0.0 to mean 'this track does not "
                f"produce that signal' — an omitted weight would score the "
                f"same and say nothing.")
        built: dict[str, float] = {}
        for kind, weight in values.items():
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise MissionError(
                    f"scoring.yaml: weights.{track}.{kind} must be a number")
            if not 0.0 <= float(weight) <= 1.0:
                raise MissionError(
                    f"scoring.yaml: weights.{track}.{kind}={weight} is outside "
                    f"0.0..1.0")
            built[kind] = float(weight)
        out[track] = built
    return out


def _flight_categories(raw: Any, tracks: tuple[str, ...]
                       ) -> dict[str, tuple[str, ...]]:
    data = _mapping(raw, "scoring.yaml: flight_categories")
    _known_tracks(data, tracks, "scoring.yaml: flight_categories")
    out: dict[str, tuple[str, ...]] = {}
    for track, codes in data.items():
        if not isinstance(codes, (list, tuple)):
            raise MissionError(
                f"scoring.yaml: flight_categories.{track} must be a list of "
                f"FR24 category codes")
        seen: list[str] = []
        for code in codes:
            if code not in FLIGHT_CODES:
                raise MissionError(
                    f"scoring.yaml: flight_categories.{track} names {code!r}; "
                    f"expected codes from {sorted(FLIGHT_CODES)}")
            if code in seen:
                raise MissionError(
                    f"scoring.yaml: flight_categories.{track} lists {code!r} "
                    f"twice")
            seen.append(code)
        out[track] = tuple(seen)
    return out


def _string_map(raw: Any, where: str) -> dict[str, str]:
    data = _mapping(raw, where)
    out: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise MissionError(f"{where}: {key!r} must map a string to a string")
        out[key] = value
    return out


def _hypotheses(raw: Any, allowed_families: tuple[str, ...] = FAMILIES
                ) -> dict[str, tuple[dict[str, str], ...]]:
    data = _mapping(raw, "hypotheses.yaml")
    unknown = sorted(set(data) - set(allowed_families))
    if unknown:
        raise MissionError(
            f"hypotheses.yaml names unknown family/families "
            f"{', '.join(unknown)}; expected {list(allowed_families)}")
    out: dict[str, tuple[dict[str, str], ...]] = {}
    for family, entries in data.items():
        if not isinstance(entries, (list, tuple)):
            raise MissionError(
                f"hypotheses.yaml: {family} must be a list of explanations")
        built = []
        for index, entry in enumerate(entries):
            item = _mapping(entry, f"hypotheses.yaml: {family}[{index}]")
            missing = sorted({"code", "statement"} - set(item))
            if missing:
                raise MissionError(
                    f"hypotheses.yaml: {family}[{index}] needs "
                    f"{', '.join(missing)}")
            extra = sorted(set(item) - {"code", "statement", "weakened_by",
                                        "when"})
            if extra:
                raise MissionError(
                    f"hypotheses.yaml: {family}[{index}] has unknown key(s) "
                    f"{', '.join(extra)}")
            if not _VOCAB_RE.match(str(item["code"])):
                raise MissionError(
                    f"hypotheses.yaml: {family}[{index}].code must be upper "
                    f"snake case")
            if str(item["code"]) == hypotheses.SCHEDULED_EVENT_CODE:
                raise MissionError(
                    f"hypotheses.yaml: {family}[{index}].code "
                    f"{hypotheses.SCHEDULED_EVENT_CODE} is reserved by the "
                    f"engine for operator-calendar matches; choose another "
                    f"code")
            when = str(item.get("when") or "ALWAYS").upper()
            if when not in hypotheses.CONDITIONS:
                raise MissionError(
                    f"hypotheses.yaml: {family}[{index}].when={when!r} is not "
                    f"a condition the engine can evaluate; expected one of "
                    f"{sorted(hypotheses.CONDITIONS)}")
            built.append({
                "code": str(item["code"]),
                # Stripped: a YAML folded scalar keeps a trailing newline, and
                # this text is concatenated into prose a reader sees.
                "statement": str(item["statement"]).strip(),
                "when": when,
                # Empty is the honest answer more often than not, and a reader
                # is better served by an unanswered alternative than by a
                # manufactured rebuttal.
                "weakened_by": str(item.get("weakened_by") or "").strip(),
            })
        out[family] = tuple(built)
    return out


def _collects(raw: Any) -> tuple[str, ...]:
    """Which engine families this pack collects. Refuses anything else.

    A mission that scores only chatter should not be made to buy flight,
    lodging and rental-car data to ignore it — three vendors, three sets of
    credentials and a per-iteration spend, for evidence no weight will ever
    read. Declaring the families is how a pack says so once, instead of
    saying it four times in zeroed weight rows that nothing enforces.
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        raise MissionError(
            f"{MANIFEST}: collects must be a non-empty list of engine "
            f"families from {sorted(COLLECTABLE_FAMILIES)}. Omit the key "
            f"entirely to collect all four.")
    out: list[str] = []
    for family in raw:
        name = str(family)
        if name not in COLLECTABLE_FAMILIES:
            raise MissionError(
                f"{MANIFEST}: collects names {name!r}, which is not an engine "
                f"family; expected any of {sorted(COLLECTABLE_FAMILIES)}. A "
                f"promoted stream family is declared in streams.yaml and is "
                f"collected through the social feed, so it does not belong "
                f"here.")
        if name in out:
            raise MissionError(f"{MANIFEST}: collects lists {name!r} twice")
        out.append(name)
    if "SOCIAL" not in out:
        raise MissionError(
            f"{MANIFEST}: collects must include SOCIAL. Every other family is "
            f"TIPPED by a social judgement — a pack collecting none would "
            f"enqueue nothing at all and report every city as quiet.")
    # Engine order, not declaration order: this tuple decides the order
    # families are reported in, and that belongs to the engine.
    return tuple(f for f in FAMILIES if f in out)


def _thresholds(raw: Any) -> dict[str, Any]:
    data = _mapping(raw, f"{MANIFEST}: thresholds")
    unknown = sorted(set(data) - THRESHOLD_SECTIONS)
    if unknown:
        raise MissionError(
            f"{MANIFEST}: thresholds names section(s) {', '.join(unknown)} a "
            f"mission may not set; expected any of "
            f"{sorted(THRESHOLD_SECTIONS)}. Credentials, endpoints, the "
            f"database and the API's own settings belong to the operator.")
    # Imported here rather than at module level: `config` reaches back into
    # this module to layer a pack's thresholds, and a lazy import keeps that
    # relationship one-directional at import time.
    from ..config import DEFAULT_CONFIG

    for section, values in data.items():
        values = _mapping(values, f"{MANIFEST}: thresholds.{section}")
        known = set(DEFAULT_CONFIG.get(section) or {})
        unknown_keys = sorted(set(values) - known)
        if unknown_keys:
            raise MissionError(
                f"{MANIFEST}: thresholds.{section} sets "
                f"{', '.join(unknown_keys)}, which the engine does not read. "
                f"A setting that is accepted and ignored is worse than one "
                f"that is refused: it would be hashed into every receipt as "
                f"though it had shaped the judgement. Settable in {section}: "
                f"{', '.join(sorted(known))}.")
    return {k: dict(v or {}) for k, v in data.items()}


def _prompts(root: Path, declared: Mapping[str, Any]
             ) -> tuple[dict[str, str], dict[str, str]]:
    """Read the prompt files and their version labels.

    Prompts are files rather than YAML strings because they are long prose and
    a block scalar diffs badly — and because `receipts.prompt_hash` is taken
    over the exact text, so whitespace that YAML would normalise is not
    cosmetic here.
    """
    unknown = sorted(set(declared) - set(PROMPTS))
    if unknown:
        raise MissionError(
            f"{MANIFEST}: prompts names unknown slot(s) {', '.join(unknown)}; "
            f"expected {list(PROMPTS)}")
    missing = [slot for slot in PROMPTS if slot not in declared]
    if missing:
        raise MissionError(
            f"{MANIFEST}: prompts has no entry for {', '.join(missing)}")

    texts: dict[str, str] = {}
    versions: dict[str, str] = {}
    for slot in PROMPTS:
        entry = _mapping(declared[slot], f"{MANIFEST}: prompts.{slot}")
        missing_keys = sorted({"file", "version"} - set(entry))
        if missing_keys:
            raise MissionError(
                f"{MANIFEST}: prompts.{slot} needs {', '.join(missing_keys)}")
        extra = sorted(set(entry) - {"file", "version"})
        if extra:
            raise MissionError(
                f"{MANIFEST}: prompts.{slot} has unknown key(s) "
                f"{', '.join(extra)}")
        path = _member_path(root, str(entry["file"]))
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise MissionError(f"{entry['file']}: prompt is empty")
        texts[slot] = text
        versions[slot] = str(entry["version"])

    if "{relevance}" not in texts["triage"]:
        raise MissionError(
            "the triage prompt must contain the placeholder {relevance}, "
            "which is where the strict or broad relevance clause is inserted")
    return texts, versions


def _streams(root: Path, raw: Any, tracks: tuple[str, ...]
             ) -> tuple[Stream, ...]:
    """Validate streams.yaml. Every refusal names the stream and the field."""
    data = _mapping(raw, "streams.yaml")
    if not data:
        raise MissionError(
            "streams.yaml declares no streams. A pack that wants exactly one "
            "watch over the whole feed should declare no streams.yaml at all "
            "and use lexicon.yaml — one implicit stream is that shape's name.")

    out: list[Stream] = []
    for stream_id, entry in data.items():
        where = f"streams.yaml: {stream_id}"
        if not isinstance(stream_id, str) or not _STREAM_ID_RE.match(stream_id):
            raise MissionError(
                f"streams.yaml: stream id {stream_id!r} must be lower snake "
                f"case ([a-z][a-z0-9_]*) — a stream id IS a scoring kind, and "
                f"the weight table spells kinds that way")
        if stream_id.lower() in RESERVED_STREAM_IDS:
            raise MissionError(
                f"streams.yaml: stream id {stream_id!r} collides with an "
                f"engine scoring kind. The non-social kinds "
                f"{list(NON_SOCIAL_KINDS)} are the engine's; a stream under "
                f"one of their names would make a weight row unreadable.")
        entry = _mapping(entry, where)
        unknown = sorted(set(entry) - STREAM_KEYS)
        if unknown:
            raise MissionError(
                f"{where} has unknown key(s) {', '.join(unknown)}; expected "
                f"{sorted(STREAM_KEYS)}")

        platforms_raw = entry.get("platforms")
        if not isinstance(platforms_raw, (list, tuple)) or not platforms_raw:
            raise MissionError(
                f"{where}: platforms is required — a non-empty subset of "
                f"{list(SOCIAL_PLATFORMS)}. A stream with no platform would "
                f"collect nothing and report it as quiet.")
        platforms: list[str] = []
        for platform in platforms_raw:
            if platform not in SOCIAL_PLATFORMS:
                raise MissionError(
                    f"{where}: platforms names {platform!r}; the engine "
                    f"collects {list(SOCIAL_PLATFORMS)}")
            if platform in platforms:
                raise MissionError(
                    f"{where}: platforms lists {platform!r} twice")
            platforms.append(platform)

        family = str(entry.get("family") or "SOCIAL")
        if not _VOCAB_RE.match(family):
            raise MissionError(
                f"{where}: family {family!r} must be upper snake case")
        if family in ("FLIGHT", "LODGING", "CAR"):
            raise MissionError(
                f"{where}: family {family!r} is refused — those families are "
                f"collected by their own connectors, and a social stream "
                f"claiming one would count in their banding slot without "
                f"their evidence.")
        if family == UNATTRIBUTED:
            raise MissionError(
                f"{where}: family {UNATTRIBUTED!r} is refused for the same "
                f"reason it cannot be a track: it is engine vocabulary "
                f"meaning 'not attributed'.")

        if "lexicon" not in entry:
            raise MissionError(
                f"{where}: lexicon is required — a stream with no search "
                f"terms would seed nothing and report a quiet city.")
        lexicon = _lexicon(entry["lexicon"], tracks, f"{where}.lexicon")

        relevance: dict[str, tuple[str, str]] = {}
        for slot in STREAM_PROMPT_SLOTS:
            if slot not in entry:
                continue
            leg = _mapping(entry[slot], f"{where}.{slot}")
            missing_keys = sorted({"file", "version"} - set(leg))
            if missing_keys:
                raise MissionError(
                    f"{where}.{slot} needs {', '.join(missing_keys)}")
            extra = sorted(set(leg) - {"file", "version"})
            if extra:
                raise MissionError(
                    f"{where}.{slot} has unknown key(s) {', '.join(extra)}")
            path = _member_path(root, str(leg["file"]))
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                raise MissionError(f"{leg['file']}: prompt is empty")
            relevance[slot] = (text, str(leg["version"]))

        out.append(Stream(id=stream_id, platforms=tuple(platforms),
                          family=family, lexicon=lexicon,
                          relevance=relevance))
    return tuple(out)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load(name_or_path: str | Path,
         *, config: Mapping[str, Any] | None = None) -> Mission:
    """Load and validate a mission pack, or raise `MissionError`.

    Accepts a bare NAME resolved inside the configured mission directory, or a
    path to a pack directory — the same split `services/inputs.py` makes, and
    for the same reason: an operator running the CLI already has the filesystem,
    while a name that arrived over the wire must not be able to address one.
    """
    if isinstance(name_or_path, Path) or "/" in str(name_or_path):
        root = Path(name_or_path)
        if not (root / MANIFEST).is_file():
            raise MissionError(f"{root} holds no {MANIFEST}")
    else:
        root = mission_path(str(name_or_path), config)

    manifest = _mapping(_yaml(root, MANIFEST), MANIFEST)
    unknown = sorted(set(manifest) - MANIFEST_KEYS)
    if unknown:
        raise MissionError(
            f"{MANIFEST}: unknown key(s) {', '.join(unknown)}; expected "
            f"{sorted(MANIFEST_KEYS)}")
    for required in REQUIRED_MANIFEST_KEYS:
        if required not in manifest:
            raise MissionError(f"{MANIFEST}: {required} is required")

    identifier = str(manifest["id"])
    if not _SAFE_NAME_RE.match(identifier):
        raise MissionError(
            f"{MANIFEST}: id {identifier!r} must be a bare name "
            f"(letters, digits, dot, dash, underscore)")

    files = manifest.get("files") or []
    if not isinstance(files, (list, tuple)):
        raise MissionError(f"{MANIFEST}: files must be a list of paths")
    members = _declared_members(root, files)
    digest, pairs = _digest(root, members)

    tracks = _vocabulary(manifest["tracks"], f"{MANIFEST}: tracks")
    if UNATTRIBUTED in tracks:
        raise MissionError(
            f"{MANIFEST}: {UNATTRIBUTED!r} cannot be a track. It is engine "
            f"vocabulary meaning 'the source did not say who was acting', and "
            f"a signal carrying it is admitted to EVERY track. A mission track "
            f"of the same name would be scored against itself and against all "
            f"the others at once.")
    location_types = _vocabulary(
        manifest["location_types"], f"{MANIFEST}: location_types")

    prompts, prompt_versions = _prompts(
        root, _mapping(manifest["prompts"], f"{MANIFEST}: prompts"))

    # The social feed's shape: either one implicit stream described by
    # lexicon.yaml, or named streams described by streams.yaml — never both
    # (two answers to one question) and never neither (the engine would seed
    # no social queries and report every city as quiet).
    has_streams = "streams.yaml" in members
    has_lexicon = "lexicon.yaml" in members
    if has_streams and has_lexicon:
        raise MissionError(
            "streams.yaml and lexicon.yaml are both declared. A pack defines "
            "its social collection once: either one lexicon for one implicit "
            "stream, or named streams each carrying its own. Two answers to "
            "one question.")
    if not has_streams and not has_lexicon:
        raise MissionError(
            "neither lexicon.yaml nor streams.yaml is declared. One of them "
            "must be: without a lexicon the engine seeds no social queries "
            "and reports every city as quiet.")

    if has_streams:
        streams = _streams(root, _yaml(root, "streams.yaml"), tracks)
        lexicon: dict[str, tuple[tuple[str, ...], ...]] = {}
        social_kinds = tuple(stream.id for stream in streams)
        stream_families = {stream.id: stream.family for stream in streams}
        promoted = []
        for stream in streams:
            if stream.family not in FAMILIES and stream.family not in promoted:
                promoted.append(stream.family)
    else:
        streams = ()
        lexicon = _lexicon(_yaml(root, "lexicon.yaml"), tracks)
        social_kinds = ("social",)
        stream_families = {"social": "SOCIAL"}
        promoted = []

    # Which of the engine's families this pack collects at all. A family it
    # does not collect leaves the completeness denominator and the weight
    # table together: it is never queried, so it can never be a gap, and a
    # zeroed weight row for it would be a second statement of the same fact
    # that nothing holds to the first.
    collects = (_collects(manifest["collects"]) if "collects" in manifest
                else FAMILIES)
    families = tuple(f for f in FAMILIES if f in collects) + tuple(promoted)
    scoring_kinds = social_kinds + tuple(
        k for k in NON_SOCIAL_KINDS
        if _KIND_FAMILY[k] in collects)

    scoring = _mapping(_yaml(root, "scoring.yaml"), "scoring.yaml")
    unknown = sorted(set(scoring) - SCORING_KEYS)
    if unknown:
        raise MissionError(
            f"scoring.yaml: unknown key(s) {', '.join(unknown)}")

    baselined = scoring.get("baselined_categories") or []
    if not isinstance(baselined, (list, tuple)):
        raise MissionError("scoring.yaml: baselined_categories must be a list")
    for code in baselined:
        if code not in FLIGHT_CODES:
            raise MissionError(
                f"scoring.yaml: baselined_categories names {code!r}; expected "
                f"codes from {sorted(FLIGHT_CODES)}")

    geography = _mapping(_yaml(root, "geography.yaml"), "geography.yaml")
    unknown = sorted(set(geography) - GEOGRAPHY_KEYS)
    if unknown:
        raise MissionError(f"geography.yaml: unknown key(s) {', '.join(unknown)}")
    equivalents = _string_map(
        geography.get("equivalents"), "geography.yaml: equivalents")
    try:
        jurisdictions = geo.Equivalents.of(equivalents)
    except ValueError as exc:
        raise MissionError(f"geography.yaml: {exc}") from exc

    facilities = _mapping(_yaml(root, "facilities.yaml"), "facilities.yaml")
    unknown = sorted(set(facilities) - FACILITY_KEYS)
    if unknown:
        raise MissionError(f"facilities.yaml: unknown key(s) {', '.join(unknown)}")
    tokens = facilities.get("tokens") or []
    if not isinstance(tokens, (list, tuple)):
        raise MissionError("facilities.yaml: tokens must be a list of words")

    return Mission(
        identifier=identifier,
        version=str(manifest["version"]),
        description=str(manifest.get("description") or "").strip(),
        path=root,
        digest=digest,
        tracks=tracks,
        location_types=location_types,
        thresholds=_thresholds(manifest.get("thresholds")),
        lexicon=lexicon,
        weights=_weights(scoring.get("weights"), tracks, scoring_kinds),
        # Required only of a pack that collects FLIGHT. Demanding an empty
        # list per track from one that does not would be a second statement
        # of what `collects` already says, and two statements of one fact can
        # disagree.
        flight_categories=(
            _flight_categories(scoring.get("flight_categories"), tracks)
            if "FLIGHT" in collects
            else {track: () for track in tracks}),
        baselined_categories=frozenset(str(c) for c in baselined),
        equivalents=equivalents,
        jurisdictions=jurisdictions,
        publishers=_string_map(
            geography.get("publishers"), "geography.yaml: publishers"),
        facility_aliases=_string_map(
            facilities.get("aliases"), "facilities.yaml: aliases"),
        facility_tokens=frozenset(str(t) for t in tokens),
        facility_spellings=_string_map(
            facilities.get("spellings"), "facilities.yaml: spellings"),
        hypotheses=_hypotheses(_yaml(root, "hypotheses.yaml"), families),
        prompts=prompts,
        prompt_versions=prompt_versions,
        members=pairs,
        streams=streams,
        social_kinds=social_kinds,
        stream_families=stream_families,
        families=families,
        scoring_kinds=scoring_kinds,
        collects=collects,
    )


def load_configured(config: Mapping[str, Any] | None = None) -> Mission | None:
    """Load the mission named in configuration, or None if none is named.

    None is a legitimate state: `init-db` and contract generation need a
    database and a schema, not a mission. What is *not* legitimate is running
    an iteration without one, which is enforced where the orchestrator is
    built rather than here — this function's job is to answer whether a pack
    was configured, not to decide what may run without it.
    """
    name = mission_name(config)
    if not name:
        return None
    return load(name, config=config)
