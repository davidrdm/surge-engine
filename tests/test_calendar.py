"""The operator calendar — context the model and the reader see, never input.

Three properties carry the weight here, and each has a test class:

**Annotation only.** A correlation scored with a calendar and one scored
without it produce identical score, band, contributions and completeness. The
calendar's whole output is a stored snapshot, a rule-trace line, and one
reserved alternative — surfaces a reader weighs, never arithmetic.

**Byte-exact reconstruction.** The context block triage appends is a pure
function of append-only rows filtered by `added_at <= iteration.started_at`,
so `prompt_user_hash` verifies forever — including after the calendar has
grown. The loader half is the usual inputs discipline: all-or-nothing,
refusals by name.

**The engine's one reserved word.** `SCHEDULED_EVENT` is the only hypothesis
code the engine writes, so a pack may not claim it — the refusal is tested
here beside the behaviour it protects.
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import reconstruct_prompts as rp                                # noqa: E402
from conftest import ANCHOR, REFERENCE_MISSION
from surge_iw.agents.correlation import CorrelationAgent
from surge_iw.agents.triage import TriageAgent
from surge_iw.db.database import iso
from surge_iw.services import calendar, hypotheses, mission
from surge_iw.services.inputs import InputError
from test_api import AUTH, app_config, client, make_session
from test_phase5 import four_signal_city
from test_triage import FakeLLM, decision, post, store_posts

#: `app_config` and `client` are pytest fixtures — importing them registers
#: them for this module; naming them here is what tells a linter that the
#: import is the use.
__all__ = ["app_config", "client"]

REFERENCE = Path(__file__).resolve().parents[1] / "missions" / "reference"

#: A calendar whose one event brackets ANCHOR, so it overlaps any window
#: anchored there.
OVERLAPPING = """\
Phoenix, AZ:
  - name: Desert Classic Festival
    starts: 2026-07-26
    ends: 2026-07-28
    category: festival
    note: ~120k expected
"""


def write_calendar(tmp_path: Path, text: str = OVERLAPPING,
                   name: str = "events.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def load(tmp_path: Path, text: str) -> calendar.LoadedCalendar:
    return calendar.load(write_calendar(tmp_path, text),
                         mission=REFERENCE_MISSION)


def refusal(tmp_path: Path, text: str) -> str:
    with pytest.raises(InputError) as excinfo:
        load(tmp_path, text)
    return str(excinfo.value)


def backdate(db, session_id: int) -> None:
    """Move every calendar row before any iteration's start.

    Tests insert events after the iteration fixture exists, but the engine's
    filter is `added_at <= started_at` — this puts the rows on the included
    side, the way a real session created with a calendar would have them.
    """
    db.conn.execute(
        "UPDATE calendar_events SET added_at = '2000-01-01T00:00:00+00:00' "
        "WHERE session_id = ?", (session_id,))
    db.conn.commit()


# ===========================================================================
# The loader — inputs discipline, verbatim
# ===========================================================================


class TestTheLoader:
    def test_a_valid_file_loads_with_canonical_instants(self, tmp_path):
        loaded = load(tmp_path, OVERLAPPING)
        assert len(loaded.events) == 1
        event = loaded.events[0]
        assert event["city_canonical"] == "phoenix"
        assert event["city_label"] == "Phoenix, AZ"
        # A bare date means the whole day, both ends.
        assert event["starts_at"] == "2026-07-26T00:00:00+00:00"
        assert event["ends_at"] == "2026-07-28T23:59:59+00:00"
        assert event["category"] == "festival"

    def test_ends_defaults_to_the_end_of_the_start_day(self, tmp_path):
        loaded = load(tmp_path, "Phoenix, AZ:\n  - name: X\n    starts: 2026-07-26\n")
        assert loaded.events[0]["starts_at"] == "2026-07-26T00:00:00+00:00"
        assert loaded.events[0]["ends_at"] == "2026-07-26T23:59:59+00:00"

    def test_a_datetime_passes_through_in_utc(self, tmp_path):
        loaded = load(tmp_path,
                      "Phoenix, AZ:\n  - name: X\n"
                      "    starts: 2026-07-26T09:00:00Z\n"
                      "    ends: 2026-07-26T18:00:00Z\n")
        assert loaded.events[0]["starts_at"] == "2026-07-26T09:00:00+00:00"
        assert loaded.events[0]["ends_at"] == "2026-07-26T18:00:00+00:00"

    def test_a_missing_file_is_refused(self, tmp_path):
        with pytest.raises(InputError, match="No calendar file"):
            calendar.load(tmp_path / "absent.yaml", mission=REFERENCE_MISSION)

    def test_an_empty_file_is_refused(self, tmp_path):
        """Every entry commented out would annotate nothing while looking
        configured."""
        assert "empty" in refusal(tmp_path, "# all commented out\n")

    def test_a_non_mapping_is_refused(self, tmp_path):
        assert "mapping" in refusal(tmp_path, "- just\n- a\n- list\n")

    def test_an_unresolvable_city_refuses_the_whole_file(self, tmp_path):
        """All-or-nothing, by name: an event attached to a city the engine
        cannot place would annotate nothing, silently."""
        message = refusal(tmp_path, OVERLAPPING +
                          "Nowheresville, ZZ:\n  - name: Y\n    starts: 2026-08-01\n")
        assert "Nowheresville" in message
        assert "silently" in message

    def test_every_unresolvable_city_is_named_together(self, tmp_path):
        message = refusal(
            tmp_path,
            "Nowheresville, ZZ:\n  - name: Y\n    starts: 2026-08-01\n"
            "Erewhon, QQ:\n  - name: Z\n    starts: 2026-08-02\n")
        assert "Nowheresville" in message and "Erewhon" in message

    def test_an_unknown_event_key_is_refused_by_name(self, tmp_path):
        message = refusal(tmp_path,
                          "Phoenix, AZ:\n  - name: X\n    starts: 2026-08-01\n"
                          "    severity: high\n")
        assert "severity" in message

    def test_a_nameless_event_is_refused(self, tmp_path):
        assert "name is required" in refusal(
            tmp_path, "Phoenix, AZ:\n  - starts: 2026-08-01\n")

    def test_a_startless_event_is_refused(self, tmp_path):
        assert "starts is required" in refusal(
            tmp_path, "Phoenix, AZ:\n  - name: X\n")

    def test_an_event_ending_before_it_starts_is_refused(self, tmp_path):
        assert "before starts" in refusal(
            tmp_path,
            "Phoenix, AZ:\n  - name: X\n    starts: 2026-08-02\n"
            "    ends: 2026-08-01\n")

    def test_a_duplicate_event_in_one_file_is_refused(self, tmp_path):
        assert "appears twice" in refusal(
            tmp_path,
            "Phoenix, AZ:\n"
            "  - name: X\n    starts: 2026-08-01\n"
            "  - name: X\n    starts: 2026-08-01\n")

    def test_a_city_with_no_events_is_refused(self, tmp_path):
        assert "non-empty list" in refusal(tmp_path, "Phoenix, AZ:\n")

    def test_an_unparseable_date_is_refused(self, tmp_path):
        assert "not a date" in refusal(
            tmp_path, "Phoenix, AZ:\n  - name: X\n    starts: whenever\n")

    def test_a_phone_book_warns_but_loads(self, tmp_path):
        """Every event rides in every triage call; size is a real prompt cost
        the operator should hear about at load time — not a refusal, because
        the calendar is theirs."""
        lines = ["Phoenix, AZ:"]
        for index in range(calendar.ADVISORY_EVENT_LIMIT + 1):
            lines += [f"  - name: Event {index}", f"    starts: 2026-08-{(index % 28) + 1:02d}T{index % 24:02d}:00:00Z"]
        loaded = load(tmp_path, "\n".join(lines) + "\n")
        assert len(loaded.events) == calendar.ADVISORY_EVENT_LIMIT + 1
        assert any("prompt cost" in w for w in loaded.warnings)


# ===========================================================================
# The context block — pure, and byte-stable
# ===========================================================================


class TestTheContextBlock:
    def test_no_events_is_no_block(self):
        assert calendar.context_block([]) == ""

    def test_the_block_is_the_frame_plus_one_line_per_event(self, tmp_path):
        loaded = load(tmp_path, OVERLAPPING)
        block = calendar.context_block(loaded.events)
        assert block.startswith("\nOperator-provided calendar")
        assert "does not change what qualifies as relevant" in block
        assert ("- Phoenix, AZ: 'Desert Classic Festival' (festival) "
                "2026-07-26T00:00:00+00:00 to 2026-07-28T23:59:59+00:00 "
                "— ~120k expected") in block

    def test_category_and_note_are_omitted_when_absent(self, tmp_path):
        loaded = load(tmp_path,
                      "Phoenix, AZ:\n  - name: X\n    starts: 2026-07-26\n")
        line = calendar.context_block(loaded.events).splitlines()[-1]
        assert line == ("- Phoenix, AZ: 'X' 2026-07-26T00:00:00+00:00 to "
                        "2026-07-26T23:59:59+00:00")

    def test_database_rows_render_identically_to_loader_events(
        self, tmp_path, db, session
    ):
        """The pure function is shared between the agent (which passes rows)
        and any caller holding loader dicts; both spellings must serialise
        the same or 'byte-exact' means nothing."""
        loaded = load(tmp_path, OVERLAPPING)
        db.insert_calendar_events(session, loaded.events, source_name="t")
        rows = db.calendar_events(session)
        assert calendar.context_block(rows) == \
            calendar.context_block(loaded.events)


# ===========================================================================
# Triage — the block rides the user message, and only there
# ===========================================================================


def judged_with_calendar(db, config, session, iteration, tmp_path,
                         text=OVERLAPPING):
    """A real triage run over two posts, calendar already on the session."""
    db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
    loaded = calendar.load(write_calendar(tmp_path, text),
                           mission=REFERENCE_MISSION)
    db.insert_calendar_events(session, loaded.events, source_name="events")
    backdate(db, session)
    config["triage"]["max_post_age_hours"] = 24 * 4000
    query = db.enqueue_query(
        session_id=session, iteration_id=iteration, source_type="SOCIAL",
        endpoint="/v1/twitter/posts", params={}, dedup_key="cal")
    posts = [post(f"https://x.com/{i}", "x.com") for i in range(2)]
    store_posts(db, iteration, posts, query)
    llm = FakeLLM(*[[decision(p["url"]) for p in posts]] * 4)
    assert TriageAgent(db, config, llm).run(iteration) is True
    return llm


class TestTriageInjection:
    def test_the_block_is_appended_after_the_items_json(
        self, db, config, session, iteration, tmp_path
    ):
        llm = judged_with_calendar(db, config, session, iteration, tmp_path)
        rows = db.calendar_events(session)
        suffix = "\n" + calendar.context_block(rows)
        assert llm.prompts, "the model was called"
        for prompt in llm.prompts:
            assert prompt.endswith(suffix)
            # The items payload stays first and intact.
            assert prompt.startswith("Screen these 2 items.")

    def test_judgements_are_accepted_and_signals_written_under_a_calendar(
        self, db, config, session, iteration, tmp_path
    ):
        """The block is context, not interference: with it present the model's
        judgements still bind, decisions are ACCEPTED, signals exist. (This
        also pins the test stub's prompt parsing — an items-array parser that
        demands the array be the message's last byte fails exactly here.)"""
        judged_with_calendar(db, config, session, iteration, tmp_path)
        states = [row["state"] for row in
                  db.all("SELECT state FROM triage_decisions")]
        assert states and set(states) == {"ACCEPTED"}
        assert len(db.signals_by_type(iteration, "SOCIAL")) == 2

    def test_no_calendar_means_the_v01_message_byte_for_byte(
        self, db, config, session, iteration
    ):
        """A session without a calendar must produce exactly the pre-calendar
        prompt — no empty frame, no trailing newline."""
        db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        config["triage"]["max_post_age_hours"] = 24 * 4000
        query = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="plain")
        posts = [post("https://x.com/1", "x.com")]
        store_posts(db, iteration, posts, query)
        llm = FakeLLM([decision("https://x.com/1")])
        assert TriageAgent(db, config, llm).run(iteration) is True
        payload = llm.prompts[0]
        assert payload.startswith("Screen these 1 items.\n\n[")
        assert payload.rstrip("\n") == payload
        assert "calendar" not in payload

    def test_events_added_after_the_iteration_started_are_excluded(
        self, db, config, session, iteration, tmp_path
    ):
        """The cut is `added_at <= started_at`. An event landing mid-session
        joins the NEXT iteration's context, so every batch of one iteration
        sees one identical calendar."""
        db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        loaded = load(tmp_path, OVERLAPPING)
        # Inserted after the iteration fixture, so added_at > started_at.
        db.insert_calendar_events(session, loaded.events, source_name="late")
        config["triage"]["max_post_age_hours"] = 24 * 4000
        query = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="late")
        posts = [post("https://x.com/1", "x.com")]
        store_posts(db, iteration, posts, query)
        llm = FakeLLM([decision("https://x.com/1")])
        assert TriageAgent(db, config, llm).run(iteration) is True
        assert "calendar" not in llm.prompts[0]

    def test_the_input_hash_covers_the_items_alone(
        self, db, config, session, iteration, tmp_path
    ):
        """`input_hash` answers "what was judged" over exactly the judged
        items; the calendar is criteria-context, covered by
        `prompt_user_hash`. The rebuild proves both at once: the items-only
        payload hashes to the stored `input_hash` even though the message
        that was actually sent — the one `prompt_user_hash` verifies —
        carries the calendar block."""
        judged_with_calendar(db, config, session, iteration, tmp_path)
        entries = rp.rebuild_triage(db.conn, iteration)
        assert entries
        for entry in entries:
            assert "Operator-provided calendar" in entry["user"]
            assert entry["input_ok"] is True
            assert entry["accepted_ok"] is True


# ===========================================================================
# Reconstruction — byte-exact, forever
# ===========================================================================


class TestReconstruction:
    def test_the_rebuilt_message_verifies_with_its_calendar_block(
        self, db, config, session, iteration, tmp_path
    ):
        judged_with_calendar(db, config, session, iteration, tmp_path)
        entries = rp.rebuild_triage(db.conn, iteration)
        assert entries
        for entry in entries:
            assert entry["problems"] == [], entry["problems"]
            assert entry["accepted_ok"] is True
            assert "Operator-provided calendar" in entry["user"]

    def test_a_later_append_does_not_break_old_receipts(
        self, db, config, session, iteration, tmp_path
    ):
        """The whole point of the added_at filter: the calendar grows, the
        old iteration's block does not."""
        judged_with_calendar(db, config, session, iteration, tmp_path)
        loaded = load(
            tmp_path,
            "Chicago, IL:\n  - name: Lakefront expo\n    starts: 2026-07-27\n")
        db.insert_calendar_events(session, loaded.events, source_name="later")

        entries = rp.rebuild_triage(db.conn, iteration)
        for entry in entries:
            assert entry["problems"] == [], entry["problems"]
            assert entry["accepted_ok"] is True
            assert "Lakefront expo" not in entry["user"]


# ===========================================================================
# Correlation — annotation only, in both directions
# ===========================================================================


def correlation_row(db, city):
    return db.one(
        "SELECT * FROM correlations WHERE city_id = ? AND track = 'AIRSHOW'",
        (city,))


class TestAnnotationOnly:
    def test_the_calendar_moves_no_number(self, db, config, session,
                                          iteration, tmp_path):
        """THE pin. Identical evidence with and without a calendar produces
        identical score, band, contributions, distinct_types, completeness —
        the calendar writes a snapshot, a trace line and an alternative, and
        touches nothing else."""
        city = db.insert_city(session, "Phoenix", canonical="phoenix",
                              state="AZ")
        location = db.insert_key_location(city, "Riverside Fairground")
        four_signal_city(db, iteration, city, location)
        assert CorrelationAgent(db, config).run(iteration) is True
        before = dict(correlation_row(db, city))
        assert before["calendar_matches_json"] is None

        loaded = load(tmp_path, OVERLAPPING)
        db.insert_calendar_events(session, loaded.events, source_name="e")
        backdate(db, session)
        assert CorrelationAgent(db, config).run(iteration) is True
        after = dict(correlation_row(db, city))

        for field in ("score", "band", "contributions_json", "distinct_types",
                      "data_completeness", "failed_sources",
                      "failed_families", "band_capped"):
            assert after[field] == before[field], field
        matches = json.loads(after["calendar_matches_json"])
        assert [m["name"] for m in matches] == ["Desert Classic Festival"]
        assert matches[0]["starts_at"] == "2026-07-26T00:00:00+00:00"
        assert "operator calendar: 1 scheduled event(s) overlap this window " \
               "(annotation only — score unchanged)" in after["rule_trace"]

    def test_a_non_overlapping_event_is_consulted_but_not_matched(
        self, db, config, session, iteration, tmp_path
    ):
        """[] and NULL are different answers: [] says a calendar was there
        and nothing overlapped."""
        city = db.insert_city(session, "Phoenix", canonical="phoenix",
                              state="AZ")
        four_signal_city(db, iteration, city)
        loaded = load(
            tmp_path,
            "Phoenix, AZ:\n  - name: Winter fair\n    starts: 2026-12-01\n")
        db.insert_calendar_events(session, loaded.events, source_name="e")
        backdate(db, session)
        assert CorrelationAgent(db, config).run(iteration) is True
        row = correlation_row(db, city)
        assert row["calendar_matches_json"] == "[]"
        assert "operator calendar" not in row["rule_trace"]
        codes = [a["code"] for a in json.loads(row["alternatives_json"])]
        assert "SCHEDULED_EVENT" not in codes

    def test_another_citys_event_does_not_match(self, db, config, session,
                                                iteration, tmp_path):
        city = db.insert_city(session, "Phoenix", canonical="phoenix",
                              state="AZ")
        four_signal_city(db, iteration, city)
        loaded = load(
            tmp_path,
            "Chicago, IL:\n  - name: Lakefront expo\n"
            "    starts: 2026-07-26\n    ends: 2026-07-28\n")
        db.insert_calendar_events(session, loaded.events, source_name="e")
        backdate(db, session)
        assert CorrelationAgent(db, config).run(iteration) is True
        assert correlation_row(db, city)["calendar_matches_json"] == "[]"

    def test_a_window_straddling_event_matches(self, db, config, session,
                                               iteration, tmp_path):
        """Overlap, not containment: an event that begins before the window
        opens and ends inside it is still context for it."""
        city = db.insert_city(session, "Phoenix", canonical="phoenix",
                              state="AZ")
        four_signal_city(db, iteration, city)
        # Window is [ANCHOR-48h, ANCHOR+48h]; this ends just inside its left
        # edge and started long before.
        starts = iso(ANCHOR - timedelta(days=6))
        ends = iso(ANCHOR - timedelta(hours=47))
        loaded = load(tmp_path,
                      f"Phoenix, AZ:\n  - name: Long festival\n"
                      f"    starts: {starts}\n    ends: {ends}\n")
        db.insert_calendar_events(session, loaded.events, source_name="e")
        backdate(db, session)
        assert CorrelationAgent(db, config).run(iteration) is True
        matches = json.loads(
            correlation_row(db, city)["calendar_matches_json"])
        assert [m["name"] for m in matches] == ["Long festival"]

    def test_the_scheduled_event_alternative_quotes_the_operator(
        self, db, config, session, iteration, tmp_path
    ):
        city = db.insert_city(session, "Phoenix", canonical="phoenix",
                              state="AZ")
        four_signal_city(db, iteration, city)
        loaded = load(tmp_path, OVERLAPPING)
        db.insert_calendar_events(session, loaded.events, source_name="e")
        backdate(db, session)
        assert CorrelationAgent(db, config).run(iteration) is True
        alternatives = json.loads(
            correlation_row(db, city)["alternatives_json"])
        assert alternatives, "the reference pack writes alternatives"
        last = alternatives[-1]
        assert last["code"] == "SCHEDULED_EVENT"
        assert "'Desert Classic Festival' in Phoenix, AZ" in last["statement"]
        assert "2026-07-26T00:00:00+00:00" in last["statement"]
        # Four families contributed, so the engine's alternative carries the
        # same accounting burden as the mission's.
        assert "account for all of them" in last["weakened_by"]


# ===========================================================================
# The reserved code
# ===========================================================================


class TestTheReservedCode:
    def test_a_pack_claiming_scheduled_event_is_refused(self, tmp_path):
        target = tmp_path / "missions" / "trial"
        shutil.copytree(REFERENCE, target)
        data = yaml.safe_load((target / "hypotheses.yaml").read_text())
        data["SOCIAL"] = [{"code": "SCHEDULED_EVENT",
                           "statement": "a mission trying to shadow it"}]
        (target / "hypotheses.yaml").write_text(
            yaml.safe_dump(data, sort_keys=False))
        with pytest.raises(mission.MissionError,
                           match="reserved by the engine"):
            mission.load(target)

    def test_no_alternative_is_offered_for_gap_only_correlations(self):
        """No evidence, no alternatives — the calendar included. The matches
        still reach the row; an 'alternative explanation' for evidence that
        does not exist is not offered."""
        matches = [{"name": "X", "city": "Phoenix, AZ",
                    "starts_at": "s", "ends_at": "e"}]
        assert hypotheses.for_correlation(
            [], mission=REFERENCE_MISSION, calendar_matches=matches) == []

    def test_the_alternative_appears_even_without_a_mission_catalogue_entry(
        self,
    ):
        """SCHEDULED_EVENT is an engine fact about the correlation; a mission
        that wrote no hypotheses for the contributing family cannot suppress
        it."""
        signals = [{"signal_type": "CAR", "stream": None,
                    "category_confidence": None}]
        out = hypotheses.for_correlation(
            signals, mission=REFERENCE_MISSION,
            calendar_matches=[{"name": "X", "city": "Phoenix, AZ",
                               "starts_at": "s", "ends_at": "e"}])
        assert [a["code"] for a in out][-1] == "SCHEDULED_EVENT"


# ===========================================================================
# The API — create, read, append, and the 409
# ===========================================================================


@pytest.fixture
def input_dir(client, tmp_path):
    (tmp_path / "venues.yaml").write_text(
        "Phoenix, AZ:\n  - name: Riverside Fairground\n", encoding="utf-8")
    (tmp_path / "events.yaml").write_text(OVERLAPPING, encoding="utf-8")
    client.app.state.config["inputs"] = {"dir": str(tmp_path)}
    return tmp_path


class TestTheAPI:
    def test_create_with_a_calendar_loads_and_reports_it(self, client, db,
                                                         input_dir):
        session_id = make_session(client, calendar_set="events")
        body = client.get(f"/v1/sessions/{session_id}", headers=AUTH).json()
        assert body["calendar_events"] == 1
        assert len(db.calendar_events(session_id)) == 1

    def test_the_creation_response_says_what_loaded(self, client, input_dir):
        response = client.post("/v1/sessions", headers=AUTH, json={
            "cities": [{"name": "Phoenix", "state": "AZ"}],
            "tracks": ["AIRSHOW"], "calendar_set": "events"})
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["calendar_events"] == 1
        assert any("1 calendar event(s)" in w for w in body["warnings"])

    def test_a_bad_calendar_refuses_the_whole_session(self, client, db,
                                                      input_dir):
        (input_dir / "bad.yaml").write_text(
            "Nowheresville, ZZ:\n  - name: Y\n    starts: 2026-08-01\n",
            encoding="utf-8")
        before = db.scalar("SELECT COUNT(*) FROM sessions")
        response = client.post("/v1/sessions", headers=AUTH, json={
            "cities": [{"name": "Phoenix", "state": "AZ"}],
            "tracks": ["AIRSHOW"], "calendar_set": "bad"})
        assert response.status_code == 422
        assert "Nowheresville" in response.json()["detail"]
        assert db.scalar("SELECT COUNT(*) FROM sessions") == before

    def test_a_path_as_calendar_set_is_refused(self, client, input_dir):
        response = client.post("/v1/sessions", headers=AUTH, json={
            "cities": [{"name": "Phoenix", "state": "AZ"}],
            "tracks": ["AIRSHOW"], "calendar_set": "../../etc/passwd"})
        assert response.status_code == 422
        assert "not a valid input set name" in response.json()["detail"]

    def test_get_lists_the_events_oldest_addition_first(self, client,
                                                        input_dir):
        session_id = make_session(client, calendar_set="events")
        body = client.get(f"/v1/sessions/{session_id}/calendar",
                          headers=AUTH).json()
        assert body["session_id"] == session_id
        assert [e["name"] for e in body["events"]] == \
            ["Desert Classic Festival"]
        event = body["events"][0]
        assert event["city_canonical"] == "phoenix"
        assert event["source_name"] == "events.yaml"
        assert event["added_at"]

    def test_append_grows_the_calendar_between_iterations(self, client, db,
                                                          input_dir):
        session_id = make_session(client, calendar_set="events")
        (input_dir / "more.yaml").write_text(
            OVERLAPPING +
            "Chicago, IL:\n  - name: Lakefront expo\n    starts: 2026-09-08\n",
            encoding="utf-8")
        response = client.post(f"/v1/sessions/{session_id}/calendar",
                               headers=AUTH, json={"calendar_set": "more"})
        assert response.status_code == 201, response.text
        body = response.json()
        assert len(body["events"]) == 2
        # The grown file re-listed the original event; that is the normal way
        # to append, so it warns rather than errors.
        assert any("already on this session's calendar" in w
                   for w in body["warnings"])

    def test_append_during_an_iteration_is_409(self, client, input_dir):
        session_id = make_session(client, calendar_set="events")
        runner = client.app.state.runner
        runner._active[session_id] = 999
        try:
            response = client.post(f"/v1/sessions/{session_id}/calendar",
                                   headers=AUTH,
                                   json={"calendar_set": "events"})
        finally:
            runner._active.pop(session_id, None)
        assert response.status_code == 409
        assert "between iterations" in response.json()["detail"]

    def test_append_to_a_missing_session_is_404(self, client, input_dir):
        response = client.post("/v1/sessions/424242/calendar", headers=AUTH,
                               json={"calendar_set": "events"})
        assert response.status_code == 404

    def test_evidence_surfaces_the_stored_matches(self, client, db,
                                                  input_dir):
        """The snapshots on the row, not a re-match: the API answer must be
        what the correlation was annotated with when it was scored."""
        session_id = make_session(client, calendar_set="events")
        db.conn.execute(
            "UPDATE calendar_events SET added_at = '2000-01-01T00:00:00+00:00'")
        db.conn.commit()
        response = client.post(
            f"/v1/sessions/{session_id}/iterations?wait=true", headers=AUTH)
        assert response.status_code in (200, 202), response.text
        iteration_id = response.json()["iteration_id"]
        correlations = client.get(
            f"/v1/iterations/{iteration_id}/correlations",
            headers=AUTH).json()["correlations"]
        assert correlations
        evidence = client.get(
            f"/v1/correlations/{correlations[0]['correlation_id']}/evidence",
            headers=AUTH).json()
        assert evidence["calendar_matches"] is not None
