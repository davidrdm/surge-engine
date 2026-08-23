# Writing a mission

Surge I&W is two things kept deliberately apart.

The **engine** collects social media and news, flight movements,
short-term-rental availability and rental-car availability; tips paid collection
from what it finds; correlates the result; and records how it reached every
judgement. None of that depends on what you are looking for.

The **mission** says what any of it means: who you are looking for, what words
find them, what each observation is worth, and what makes an item relevant at
all. It lives in a directory of data files read once at startup — never in the
code.

The line between them is worth stating precisely, because it is the line this
guide is about:

> The engine owns anything an engineer would change to fix a bug.
> The mission owns anything an analyst would change to ask a different question.

---

## Contents

- [The shape of a pack](#the-shape-of-a-pack)
- [`mission.yaml`](#missionyaml)
- [`lexicon.yaml`](#lexiconyaml)
- [`scoring.yaml`](#scoringyaml)
- [`geography.yaml`](#geographyyaml)
- [`facilities.yaml`](#facilitiesyaml)
- [`hypotheses.yaml`](#hypothesesyaml)
- [`prompts/`](#prompts)
- [The four rules](#the-four-rules)
- [What the engine keeps](#what-the-engine-keeps)
- [What gets recorded](#what-gets-recorded)
- [Writing your first pack](#writing-your-first-pack)
- [Every refusal](#every-refusal)

---

## The shape of a pack

```
missions/<name>/
  mission.yaml               required — the manifest
  lexicon.yaml               required
  scoring.yaml               required
  geography.yaml             required
  facilities.yaml            required
  hypotheses.yaml            required
  prompts/triage.md          required
  prompts/relevance-strict.md
  prompts/relevance-broad.md
  prompts/alert.md
  README.md                  optional — what this mission is, in prose
  docs/                      optional, never read
  inputs/                    optional, never read by the loader
  tests/                     optional — the pack's own checks, collected by
                             the engine's `pytest.ini`
```

**Write the pack's tests in `tests/`.** What is in a pack is the pack's claim,
not the engine's: that a lexicon covers every track, that a county-to-seat
table was verified by hand, that a prompt still hashes to what the receipts in
the database recorded. The engine cannot assert any of that on your behalf —
it can only assert that it reads whatever you supply, which it does against a
mission built in its own suite. `testpaths` in `pytest.ini` collects
`missions/` alongside `tests/`, so the checks run with the rest of the suite.
Name the file for the pack (`test_<pack>_pack.py`): pytest imports test modules
by basename, and two packs whose test files are both called `test_pack.py`
collide.

Point the engine at it:

```yaml
mission:
  dir: "./missions"
  name: "your-pack"
```

`name` is a bare directory name resolved inside `dir` — **never a path**. The
same rule as `inputs.dir`, and for the same reason: a field that reads a path is
a file-disclosure primitive whatever the intent behind it.

The five `.yaml` files and the four prompts are required because every one of
them supplies something the engine has no default for. There is no partial
mission.

---

## `mission.yaml`

The manifest. Eight keys; five are required.

```yaml
id: your-pack                # required. A bare name: letters, digits, . - _
version: "1"                 # required. Stamped on every receipt.
description: >               # optional, free text
  One paragraph on what this mission warns about.

files:                       # required in practice — see "The four rules"
  - lexicon.yaml
  - scoring.yaml
  - geography.yaml
  - facilities.yaml
  - hypotheses.yaml
  - prompts/triage.md
  - prompts/relevance-strict.md
  - prompts/relevance-broad.md
  - prompts/alert.md

tracks:                      # required. Any number, one or more.
  - TRACK_ONE
  - TRACK_TWO

location_types:              # required. What kind of place a facility can be.
  - VENUE
  - OTHER

thresholds:                  # optional. See below.
  windows: {...}
  triage: {...}
  sensitivity: {...}
  correlation: {...}

prompts:                     # required. All four slots.
  triage:            {file: prompts/triage.md, version: your-triage/1}
  relevance_strict:  {file: prompts/relevance-strict.md, version: your-rel/1-strict}
  relevance_broad:   {file: prompts/relevance-broad.md,  version: your-rel/1-broad}
  alert:             {file: prompts/alert.md,   version: your-alert/1}
```

### `tracks`

A **track** is a named hypothesis about who is acting. Each is scored
independently against the same collected evidence, so adding one costs no extra
API calls — only a second pass over data you already paid for.

Names are upper snake case (`A-Z`, `0-9`, `_`). They are stored in the database
and compared exactly, so lowercase would work right up until two packs disagreed
about the case of one word.

`UNKNOWN` is reserved. It is engine vocabulary meaning *the source did not say
who was acting*, and a signal carrying it is admitted to **every** track — so a
mission track of that name would be scored against itself and against all the
others at once. The loader refuses it.

### `location_types`

What kind of place a registered facility can be. Same naming rule. These are
documentary — facility matching is on the name — but an unknown one is refused
at load time rather than accepted and ignored.

### `thresholds`

Four sections a mission may set: `triage`, `sensitivity`, `windows`,
`correlation`. Every key inside them is documented in
[`config.md`](config.md).

They layer **engine defaults → your thresholds → the operator's `config.yaml`**.
An operator can still override you locally; every key where they do is logged by
name at startup, because a mission's thresholds carry reasoning and replacing
one silently is how that reasoning gets lost.

**The engine's defaults are illustrative placeholders, not calibrated values.**
If you omit a section you inherit numbers that were chosen to be internally
consistent and nothing more. Set them.

Anything outside those four sections is refused: credentials, provider
endpoints, retention ceilings, the database and the API's own settings belong to
whoever runs the deployment, not to whoever wrote the mission.

### `prompts`

Four slots, all required, each naming a file and a version label.

The version is stamped on every receipt. **Change the text, change the version.**
The hash is the guarantee — `receipts.prompt_hash` is taken over the exact bytes
sent — but the label is what a human reading two receipts side by side actually
compares, and a pack that changed a prompt while keeping its label makes two
different judgements look identical.

---

## `lexicon.yaml`

What to search for, per track.

```yaml
TRACK_ONE:
  - ["first term", "second term", "third term"]
  - ["another group"]
```

Each inner list is one **query group**. The engine OR-joins its terms and pairs
the result with a place name, producing one query per group per platform. So a
city costs `(groups across all tracks) x (platforms)` social queries — keep
groups few and broad rather than many and narrow, and check the arithmetic
against `tipping.max_queries_per_city`.

Every track needs an entry. An absent one and an empty one search identically
and mean opposite things, so the loader refuses the absent one.

**This is a table rather than a model call on purpose.** A tactical lexicon is
small, stable, and needs to be auditable: someone asking "why did you search
that" deserves a better answer than "the model chose it". Because it lives in
the pack it is covered by the pack digest on every receipt — which it was not
when it lived in Python.

---

## `scoring.yaml`

What each kind of observation is worth, per track.

```yaml
weights:
  TRACK_ONE:
    social:    0.30
    flight_M:  0.35
    flight_J:  0.10
    lodging:   0.15
    car:       0.10

flight_categories:
  TRACK_ONE: [M, J]

baselined_categories: [J, T, H]
```

### `weights`

The five **scoring kinds** are engine vocabulary and cannot be changed: they name
the four data families this system collects, with FLIGHT split by FR24 category
because a military-coded airframe and a business jet are different evidence. You
choose the numbers, not the rows.

Every kind must be present for every track, and each is `0.0`–`1.0`. **Write
`0.0` out.** An omitted weight and an explicit zero score identically and mean
opposite things — one is a decision that the track does not produce that signal,
the other is nobody having considered it — so the loader refuses the omission.

Weights are not normalised for you. A track whose weights sum to 1.0 can reach a
score of 1.0 on perfect evidence; one summing to 0.6 cannot. That is a choice you
are making either way, so make it deliberately.

### `flight_categories`

Which FR24 category codes each track's flight queries ask for — `M` (military
and government), `J` (business jets), `T` (general aviation), `H` (helicopters).
These are the **vendor's** vocabulary; what is yours is which of them each track
flies in.

A category absent here is never *collected* for that track. Not collected and
scored zero are different facts, and only one of them is a coverage gap.

This also sets what an unverifiable record is worth. FR24's live-positions
endpoint accepts a category filter but does not return the category, so all that
is known is that the aircraft matched the filter — and the engine credits such a
record at the **lowest** weight any category in that filter could have earned.
A filter of `[M, J]` therefore earns the business-jet weight; a filter of
`[J, T, H]` pays no penalty, because those three score identically.

### `baselined_categories`

Codes measured as an excess over a rolling median rather than counted outright.
Ordinary urban traffic needs a baseline: three business jets over a city means
nothing without knowing the city usually sees one. Rare categories can be
counted.

---

## `geography.yaml`

```yaml
equivalents:
  larger-unit-key: smaller-unit-key

publishers:
  "the local herald": "localherald.com"
```

### `equivalents`

Which two place names are **one operational unit**. Keys are the engine's
canonical place keys (lowercase; see `services/geo.py`).

This exists because of a measured failure: a session named one place, a source
reported the same activity under the containing administrative unit's name, and
the article was collected, judged relevant at salience 0.85, and refused because
the two strings did not match. Evidence the system exists to surface was paid
for, judged, and dropped on a name.

**The rule is deliberately narrow, and the narrowness is the safety property.**
An equivalence says two names mean one unit. It is not transitive, and it never
merges two units into one — two neighbouring units in one metro are still two
units, and merging them would let a report about one admit to a session that
named the other, manufacturing evidence rather than finding it.

Two units may not claim the same name; the loader refuses it, because the
reverse lookup would otherwise silently pick one.

Declaring none is normal. The engine then makes no claim that any two place
names mean one place, and an apparent ambiguity is **refused** rather than
resolved — a recorded `UNRESOLVED` beats a confident answer for the wrong place.

### `publishers`

Masthead or domain → canonical domain, for the regional outlets your
jurisdictions depend on. **Added** to the engine's wire services, nationals and
platforms, never replacing them.

Without an entry a regional domain still resolves — as a `HOST` rather than an
`ALIAS`, which is the honest answer: the engine knows the domain and not the
masthead behind it.

---

## `facilities.yaml`

Matching the facility names a model extracts against the ones an operator
registered.

```yaml
aliases:
  "board of trustees": "trustee office"

tokens:
  - festival

spellings:
  fest: festival
```

- **`aliases`** — a deliberate statement that two whole names mean the same
  place, applied after normalisation. Wholly yours; the engine has none.
- **`tokens`** — words too common in *your* domain to identify anything.
  **Added** to the engine's 37 structural words (articles, prepositions,
  administrative units, "center", "building", compass points). A candidate made
  only of generic words is refused as `TOO_GENERIC` rather than matched to
  whichever facility was registered first.
- **`spellings`** — abbreviation → expansion, applied per token so it works at
  any position. **Added** to the engine's 26 generic abbreviations. Applied to
  the registered name as well as the candidate, so a facility registered *as* an
  abbreviation still matches itself.

Adding rather than replacing is the point: every pack restating "the" and
"north" would be ceremony, and a pack that forgot to would silently match on
them.

---

## `hypotheses.yaml`

What else would produce this evidence, per family.

```yaml
LODGING:
  - code: CONVENTION
    statement: >
      A convention booked the same room inventory.
    weakened_by: >
      Baselines are weekday-aligned, so ordinary weekly demand is differenced
      out.
SOCIAL:
  - code: RUMOUR
    when: SOLE_FAMILY
    statement: >
      Amplification of a claim describing no actual movement.
```

Keyed by the four families. `code` is upper snake case and is what a reviewer
matches on to suppress an alternative across alerts, so it must outlive any
rewording of the prose.

`when` says which correlations the explanation applies to. **The engine
evaluates it, so the vocabulary is closed** — a mission may not invent a
condition the engine cannot check:

| `when` | Applies |
|---|---|
| `ALWAYS` (default) | Whenever this family contributed |
| `SOLE_FAMILY` | Only when this family is *all* of the evidence |
| `FLIGHT_CATEGORY_UNCONFIRMED` | Only when no flight record had its category confirmed |

`SOLE_FAMILY` is what keeps a rumour hypothesis off a multi-family finding: a
rumour alongside a movement signal would have to explain the movement too, and it
cannot.

`weakened_by` is what in the correlation itself argues against the alternative.
Leave it out when nothing does — an unanswered alternative is more useful to a
reader than a manufactured rebuttal. Do not write the corroboration note
yourself; the engine appends its own when more than one family contributed.

---

## `prompts/`

Four plain files. Long prose, so files rather than YAML block scalars — and
because `receipts.prompt_hash` is taken over the exact text, whitespace YAML
would normalise is not cosmetic here.

**`triage.md`** — the screening prompt. Must contain the literal placeholder
`{relevance}`, which is where one of the two relevance clauses is inserted.
Without it the clause is silently never applied and the model screens on criteria
nobody chose, so the loader refuses a body that lacks it.

It is `str.format`ed, so `{` and `}` anywhere else in the file will break it or
be silently substituted. `{relevance}` is the only brace pair the file may
contain.

The prompt must ask the model for the engine's output schema, which does not
change per mission: `item_id`, `relevant`, `track`, `cities`, `locations`,
`activity_type`, `imminence_hours`, `salience`, `rationale`. A reply is bound to
its `item_id` with **no positional fallback**, and any field the schema does not
declare is refused — so ask for exactly these and no others. `track` must be one
of yours or `UNKNOWN`.

**`relevance-strict.md`** and **`relevance-broad.md`** — the two criteria the
`triage.require_nexus` switch chooses between. Both are yours to write. The
strict leg is the scoped instrument; the broad one should be a genuinely wider
question rather than a slightly more sensitive version of the same one, because
that is what the switch is for and the engine logs a `WARNING` on every run under
it.

**`alert.md`** — how the summary sentence is written. Not `format`ed, so braces
are safe. The model never sees the score and cannot change it.

---

## The four rules

**1. The pack is the audit unit.** `mission.yaml` declares its members. The
loader refuses a declared file that is missing *and* an undeclared file that is
present, then hashes every member into one digest. A file that is neither loaded
nor hashed would be a change nothing records — exactly what the digest exists to
prevent. `README.md`, `docs/`, `inputs/` and `tests/` are exempt: the loader
does not read them. A pack should be able to introduce itself to whoever opens
the directory, and the alternative to exempting the README is worse both ways —
declared, a typo fix in the prose moves the digest and every receipt appears to
name a different definition; undeclared, the pack will not load at all. `tests/` is exempt for one more reason worth stating — a test that could
move the digest would make every receipt appear to name a different definition
the moment somebody added a case.

**2. Unknown is refused, never ignored.** A misspelled key is an error naming the
key. The failure this prevents is specific — a lexicon whose track name is
misspelled would search nothing for that track, score nothing, and report a quiet
city, which is indistinguishable from a city where nothing is happening.

**3. Some tables add, some replace.** Facility tokens, facility spellings and
publishers are merged on top of an engine core. Everything else — tracks,
lexicon, weights, prompts, facility aliases, equivalences, hypotheses — is wholly
yours and has no engine-side default at all.

**4. There is no fallback.** Without a mission the engine will open its database
and serve its contract, but it will not run an iteration. A default lexicon or a
default prompt would collect and screen against criteria nobody chose while
looking exactly like criteria somebody did.

---

## What the engine keeps

Not yours to change, and worth knowing so you do not try:

- **The four data families** — SOCIAL, FLIGHT, LODGING, CAR. They describe where
  data comes from, not what it means.
- **The five scoring kinds** and the arithmetic over them: `weight x quality`
  summed to a score, then a band from the score and the count of distinct
  families.
- **The per-family quality functions** — how a lodging drop, a price escalation,
  a capacity-weighted car drawdown and a flight cluster each become a number.
- **`UNKNOWN`** as the unattributed marker.
- **Temporal decay**, coverage-gap accounting, the band cap on incomplete
  collection, the independent-reports floor.
- **The structural cores** listed under rule 3.

---

## What gets recorded

Every model call writes a receipt carrying `mission_id`, `mission_hash`,
`prompt_version` and `prompt_hash`. Every session records the mission it ran
under. `GET /v1/capabilities` reports the loaded pack's id, version, digest,
tracks and location types — which is where a client learns the permitted values,
since the contract deliberately no longer declares them as enums.

`code_revision` alone stopped being sufficient provenance the moment the prompt
left the repository: a pack can be edited with no commit at all. The digest is
what closes that.

`scripts/reconstruct_prompts.py` rebuilds the exact messages sent for an
iteration and verifies them against the receipts. It will refuse to print
anything whose hash does not match — if you have edited the pack since the run,
that mismatch is the answer, not an error to work around.

---

## Writing your first pack

```bash
cp -r missions/reference missions/your-pack
```

Edit `id`, then work outward: `tracks` first, because everything else is keyed by
them, then `lexicon.yaml` and `scoring.yaml`, then the prompts.

Point at it and start:

```bash
python run.py --config config.yaml serve
```

Every refusal names the file and the key, so the loader is the fastest way to
learn the format. Startup also prints what it loaded:

```
mission your-pack/1 from missions/your-pack
  digest 4202502c9e1e over 10 file(s)
  tracks: TRACK_ONE, TRACK_TWO
```

Then check what a session would collect before paying for it:

```bash
python run.py --config config.yaml session create --from example --dry-run
```

**Do not calibrate against the shipped `reference` pack.** It is synthetic and
uncalibrated — a benign crowd-convergence problem that exists so the engine can
be tested and its contract generated with no real mission present. It has three
tracks rather than two on purpose, so that any assumption about the shape the
engine grew up with fails loudly rather than passing by luck.

---

## Every refusal

The loader refuses by name. This is the whole list.

| Refusal | Cause |
|---|---|
| `is not a valid mission name` | `mission.name` is a path, not a bare name |
| `No mission <name> in <dir>/` | No such directory, or it holds no manifest |
| `holds no <manifest>` | A path was given to a directory without a manifest |
| `not valid YAML` | A file failed to parse |
| `unknown key(s)` | A key no file of that kind accepts |
| `<key> is required` | `id`, `version`, `tracks`, `location_types` or `prompts` absent |
| `must be upper snake case` | A track, location type or hypothesis code is not `[A-Z][A-Z0-9_]*` |
| `is listed twice` | A duplicated track or location type |
| `<value> cannot be a track` | `UNKNOWN` is reserved: it is the unattributed marker |
| `declares <f>, which does not exist` | A declared member is missing |
| `holds file(s) no manifest declares` | An undeclared file is present outside `README.md`, `docs/`, `inputs/` and `tests/` |
| `is declared twice` | A member listed twice in `files` |
| `escapes the pack directory` | A member path outside the pack |
| `no entry for track(s)` | A lexicon, weight table or flight filter missing a track |
| `is not a track of this mission` | A table naming a track the manifest does not define |
| `has no weight for <kind>` | An omitted scoring kind — write `0.0` |
| `is outside 0.0..1.0` | A weight out of range |
| `names unknown scoring kind(s)` | A row that is not one of the five |
| `expected codes from <codes>` | An unknown FR24 category |
| `names unknown family/families` | A hypothesis family that is not one of the four |
| `.when=<x> is not a condition the engine can evaluate` | A `when` outside the closed set |
| `is claimed by more than one unit` | Two equivalences sharing a name |
| `thresholds names section(s) <x> a mission may not set` | A section outside the four analytic ones |
| `prompts has no entry for <slot>` | One of the four slots missing |
| `prompts.<slot> needs <keys>` | A slot missing `file` or `version` |
| `prompt is empty` | A prompt file with no content |
| `must contain the placeholder` | The triage prompt lacks its `{relevance}` slot |

---

## Reference

- [`config.md`](config.md) — every threshold a mission may set
- `<your pack>/docs/` — a pack may carry its own prose, and the shipped ones
  do: how the engine reaches a judgement, and why each threshold is the number
  it is, are worth writing down where the numbers live
- [`../README.md`](../README.md) §3.1 — the operator's view
- `surge_iw/services/mission.py` — the loader, and the reasoning behind each rule
