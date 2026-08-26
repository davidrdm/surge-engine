# The reference mission — crowd convergence

**This mission is invented.** It exists so the engine can be tested, and its API
contract generated, with no real mission present. Nothing in it is calibrated:
the thresholds are plausible and internally consistent, which is all a fixture
needs to be. **Draw no operational conclusion from a run against this pack.**

---

## The problem it describes

A city needs a day or two of warning that **a large number of people are about
to arrive in it for a scheduled event** — enough time to move traffic
management, transit capacity and stewarding, not enough to wait for the event
to be announced from a podium.

The difficulty is that the arrival is visible long before anyone announces it,
and it is visible in ordinary commercial data rather than in any single feed.
Rooms near the venue stop being available. Rental fleets at the airport draw
down. Charter and display aircraft appear on approach. People talk about it.
Each of those, alone, is noise — conventions book rooms, fleets reposition,
business jets land every day. Together, in one place, in one week, they are a
gathering.

Three kinds of gathering are watched, because they leave different traces:

| Track | What it is | How it shows up |
|---|---|---|
| `CONCERT_TOUR` | A touring act and its production | Chatter first, then a load-in: trucks, a stage build, block-booked rooms |
| `SPORTING_EVENT` | A fixture and the supporters travelling to it | Beds and vehicles, in volume, on a known date |
| `AIRSHOW` | A flying display and its practice days | The aircraft **are** the event, so aviation is the decisive signal |

## What Surge does about it

The engine runs an eight-stage iteration and records every step of it.

1. **Seed.** Each city gets one social query per stream, per lexicon group,
   per track — the search terms in `streams.yaml`, OR-joined and paired with
   the place name, sent to that stream's platforms.
2. **Collect chatter.** Social and news results are stored as raw payloads.
   Nothing is judged yet, and nothing is paid for beyond the search.
3. **Triage.** A language model reads each post once per stream and answers a
   fixed question: is this evidence of a gathering forming at a named place,
   which place, which venue, which track, and how soon. The `local_news`
   stream is judged under its own strict relevance leg — reporting, not talk.
   It is the only place in the system where free text becomes a record, and
   its answer is bound to an opaque per-call id so a reply cannot be
   misattributed.
4. **Tip.** A *relevant* post buys the expensive collection, and only then:
   inbound flights for the city's airports, room availability near the key
   location the model named, and rental-car availability at the airport. A post
   with no usable timestamp buys nothing, because evidence that cannot be
   placed in time cannot be correlated with anything.
5. **Collect the paid families.** Flights are counted against a rolling
   baseline for that airport; lodging is measured as availability collapse
   across two windows over one fixed set of listings; cars are weighted by
   seats, because moving people needs seats.
6. **Correlate.** Deterministic arithmetic in Python — no model. Each family
   contributes its weight from `scoring.yaml` times its quality, aged on a
   decay curve tied to the correlation window, and the result is a score and a
   band per city and track.
7. **Alert.** A model writes one or two sentences of prose. It never sees the
   score and cannot change it. Every alert carries what could **not** be
   collected, and the competing explanations from `hypotheses.yaml`.
8. **Schedule.** A finding worth watching books its own follow-up.

Two properties are worth stating because they are what the pack is shaped
around. **A gathering needs an anchor**: two booking signals alone never
escalate, because rooms and cars move for a dozen innocent reasons. And **a
failure is never silence** — a query that could not run is recorded as a
coverage gap, caps the confidence band, and is named on the alert, so "we did
not look" can never read as "there is nothing there".

---

## The files

`mission.yaml` declares the rest. A file that is present and undeclared, or
declared and missing, is refused at load — the pack is hashed as a whole, and
its digest is stamped on every receipt.

| File | What lives in it |
|---|---|
| `mission.yaml` | The manifest: id, version, description, the three tracks, the venue types a key location may be, the analytic thresholds this mission sets, and the four prompt slots with their version labels. |
| `streams.yaml` | Two watches over the social feed: `chatter` (twitter + reddit, a sub-kind of the SOCIAL family) and `local_news` (news, promoted to its own LOCAL_NEWS banding family, with its own strict relevance leg). Each carries its per-track lexicon — a table rather than a model call, so "why did you search that" has an answer. |
| `scoring.yaml` | What each kind of observation is worth to each track, which FR24 aircraft categories each track asks for, and which of those are measured against a baseline rather than counted. |
| `geography.yaml` | Place names this mission treats as one operational unit, and regional publishers it recognises. Both are empty here: the synthetic mission claims no local knowledge. |
| `facilities.yaml` | How a venue name the model wrote is matched to one the operator registered — spellings that mean the same place, and the words too generic to identify anything on their own. |
| `hypotheses.yaml` | What else would produce this evidence, per signal family, with what in the correlation argues against it. Attached to every alert, so a reader is told the innocent explanation rather than left to think of it. |
| `prompts/triage.md` | The screening prompt: what the tracks are, what to extract, and the exact JSON to return. Carries a `{relevance}` slot filled by one of the two below. |
| `prompts/relevance-strict.md` | The default relevance test: a specific scheduled gathering at a named place, or its logistics. |
| `prompts/relevance-broad.md` | The wider test, selected per session, which admits display aviation at a named place whether or not an event is named. Inherited by both streams. |
| `prompts/local-news-strict.md` | The `local_news` stream's own strict leg: someone REPORTING preparations as fact, not passing on talk of them. |
| `prompts/alert.md` | How to write the warning sentence — plainly, concretely, without characterising confidence, which is attached separately as a number. |

Carried alongside, neither read nor hashed: `README.md`, `docs/`, `inputs/`
and `tests/`. This pack's `tests/` holds what it claims about **itself** — that
it loads, that every stream carries every track and every track a weight and
a flight filter, and
that between them the three tracks exercise every aircraft category the engine
scores. The engine's `pytest.ini` collects them with the rest of the suite.

For the pack format itself — every key, and the refusal each rule triggers —
see the engine's [`docs/missions.md`](../../docs/missions.md).
