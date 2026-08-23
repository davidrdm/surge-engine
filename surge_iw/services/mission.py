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

#: Signal families a hypothesis set may key on. Same reasoning as SCORING_KINDS.
FAMILIES: tuple[str, ...] = ("SOCIAL", "FLIGHT", "LODGING", "CAR")

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
#: geography for this mission, which travels with the pack for convenience but
#: is chosen per session by name and recorded on the session itself. `tests/`
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
    "thresholds", "prompts",
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
            f"  lexicon: {sum(len(g) for g in self.lexicon.values())} query "
            f"group(s) across {len(self.lexicon)} track(s)",
        ]


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


def _lexicon(raw: Any, tracks: tuple[str, ...]
             ) -> dict[str, tuple[tuple[str, ...], ...]]:
    data = _mapping(raw, "lexicon.yaml")
    _known_tracks(data, tracks, "lexicon.yaml")
    out: dict[str, tuple[tuple[str, ...], ...]] = {}
    for track, groups in data.items():
        if not isinstance(groups, (list, tuple)) or not groups:
            raise MissionError(
                f"lexicon.yaml: {track} must be a non-empty list of term groups")
        built: list[tuple[str, ...]] = []
        for index, group in enumerate(groups):
            if not isinstance(group, (list, tuple)) or not group:
                raise MissionError(
                    f"lexicon.yaml: {track}[{index}] must be a non-empty list "
                    f"of search terms")
            terms = []
            for term in group:
                if not isinstance(term, str) or not term.strip():
                    raise MissionError(
                        f"lexicon.yaml: {track}[{index}] holds a term that is "
                        f"not a non-empty string")
                terms.append(term.strip())
            built.append(tuple(terms))
        out[track] = tuple(built)
    return out


def _weights(raw: Any, tracks: tuple[str, ...]) -> dict[str, dict[str, float]]:
    data = _mapping(raw, "scoring.yaml: weights")
    _known_tracks(data, tracks, "scoring.yaml: weights")
    out: dict[str, dict[str, float]] = {}
    for track, kinds in data.items():
        values = _mapping(kinds, f"scoring.yaml: weights.{track}")
        unknown = sorted(set(values) - set(SCORING_KINDS))
        if unknown:
            raise MissionError(
                f"scoring.yaml: weights.{track} names unknown scoring kind(s) "
                f"{', '.join(unknown)}; expected {list(SCORING_KINDS)}")
        missing = [k for k in SCORING_KINDS if k not in values]
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


def _hypotheses(raw: Any) -> dict[str, tuple[dict[str, str], ...]]:
    data = _mapping(raw, "hypotheses.yaml")
    unknown = sorted(set(data) - set(FAMILIES))
    if unknown:
        raise MissionError(
            f"hypotheses.yaml names unknown family/families "
            f"{', '.join(unknown)}; expected {list(FAMILIES)}")
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
        lexicon=_lexicon(_yaml(root, "lexicon.yaml"), tracks),
        weights=_weights(scoring.get("weights"), tracks),
        flight_categories=_flight_categories(
            scoring.get("flight_categories"), tracks),
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
        hypotheses=_hypotheses(_yaml(root, "hypotheses.yaml")),
        prompts=prompts,
        prompt_versions=prompt_versions,
        members=pairs,
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
