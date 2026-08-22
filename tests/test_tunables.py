"""Per-session configuration overrides (9.2, issue #11).

The defect was not that `tunables` was documented wrongly. It was accepted,
stored in `sessions.config_json`, and never read — so a client could ask for a
narrower window or a tighter cap, receive a successful session, and have paid
collection run under settings it did not choose, with every receipt stamped
`config_hash` from a configuration it never asked for.

Two things therefore have to be true, and the second is the one that would have
caught the original defect: an unsupported field is refused **by name**, and a
supported one **reaches the pipeline**. A test that only checked the merge
function would have passed while nothing read its output.
"""
from __future__ import annotations

import json

import pytest

from surge_iw.services import receipts, tunables
from surge_iw.services.tunables import TunableError


@pytest.fixture
def server(config):
    """The server's own configuration, as the operator set it."""
    return config


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


class TestItRefusesWhatItCannotApply:
    def test_a_server_owned_section_is_refused_with_a_reason(self, server):
        with pytest.raises(TunableError) as exc:
            tunables.validate({"staying": {"retention_days": 3650}}, server)
        assert "staying" in str(exc.value)
        assert "retention" in str(exc.value)

    def test_dry_run_is_refused(self, server):
        """A session that could set it would receive fixture data
        indistinguishable from collection it had paid for."""
        with pytest.raises(TunableError, match="dry_run"):
            tunables.validate({"dry_run": True}, server)

    def test_an_unknown_section_is_refused(self, server):
        with pytest.raises(TunableError) as exc:
            tunables.validate({"scoring": {"weight": 1}}, server)
        assert "settable sections" in str(exc.value).lower()

    def test_a_misspelled_setting_is_refused_not_ignored(self, server):
        """The whole failure mode in one test. `max_post_age` silently doing
        nothing is exactly what this replaced."""
        with pytest.raises(TunableError) as exc:
            tunables.validate({"triage": {"max_post_age": 24}}, server)
        assert "triage.max_post_age" in str(exc.value)
        assert "max_post_age_hours" in str(exc.value), (
            "the refusal should name what the caller probably meant")

    def test_true_is_not_accepted_as_the_number_one(self, server):
        """In Python `True` is an `int`, so a naive check would take
        `batch_size: true` as a batch of one."""
        with pytest.raises(TunableError, match="batch_size"):
            tunables.validate({"triage": {"batch_size": True}}, server)

    def test_a_string_is_not_a_number(self, server):
        with pytest.raises(TunableError, match="window_hours"):
            tunables.validate({"correlation": {"window_hours": "48"}}, server)

    def test_a_value_outside_its_range_is_refused(self, server):
        with pytest.raises(TunableError, match="above the maximum"):
            tunables.validate({"correlation": {"band_high_min_score": 5.0}},
                              server)

    def test_an_unknown_provider_in_a_budget_map_is_refused(self, server):
        with pytest.raises(TunableError, match="ACME"):
            tunables.validate(
                {"budget": {"monthly_limit": {"ACME": 10.0}}}, server)


class TestCeilingsOnlyComeDown:
    def test_a_lower_cap_is_accepted(self, server):
        clean = tunables.validate(
            {"budget": {"per_iteration_cap": {"FR24": 100.0}}}, server)
        assert clean["budget"]["per_iteration_cap"] == {"FR24": 100.0}

    def test_a_higher_cap_is_refused(self, server):
        with pytest.raises(TunableError) as exc:
            tunables.validate(
                {"budget": {"per_iteration_cap": {"FR24": 1e9}}}, server)
        assert "never raise" in str(exc.value)

    def test_a_wider_fan_out_is_refused(self, server):
        with pytest.raises(TunableError, match="max_queries_per_iteration"):
            tunables.validate(
                {"tipping": {"max_queries_per_iteration": 4999}}, server)

    def test_a_narrower_fan_out_is_accepted(self, server):
        clean = tunables.validate(
            {"tipping": {"max_queries_per_iteration": 20}}, server)
        assert clean["tipping"]["max_queries_per_iteration"] == 20

    def test_the_clamp_still_holds_if_the_server_lowers_its_cap_later(
        self, server
    ):
        """Validation is a courtesy to the client; the clamp is the guarantee.

        A cap accepted in March must not outlive an operator lowering the
        server's own cap in April — the stored number would then be a session
        spending above the envelope the operator now believes is in force.
        """
        stored = tunables.validate(
            {"budget": {"per_iteration_cap": {"FR24": 3000.0}}}, server)
        server["budget"]["per_iteration_cap"]["FR24"] = 500.0
        merged = tunables.effective(server, stored)
        assert merged["budget"]["per_iteration_cap"]["FR24"] == 500.0

    def test_the_same_clamp_applies_to_a_scalar_ceiling(self, server):
        stored = tunables.validate(
            {"tipping": {"max_queries_per_city": 30}}, server)
        server["tipping"]["max_queries_per_city"] = 5
        assert tunables.effective(
            server, stored)["tipping"]["max_queries_per_city"] == 5


# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------


class TestTheEffectiveConfiguration:
    def test_an_override_replaces_only_what_it_names(self, server):
        merged = tunables.effective(
            server, {"correlation": {"window_hours": 48}})
        assert merged["correlation"]["window_hours"] == 48
        assert merged["correlation"]["radius_km"] == \
            server["correlation"]["radius_km"]
        assert merged["triage"] == server["triage"]

    def test_the_base_configuration_is_not_mutated(self, server):
        before = server["correlation"]["window_hours"]
        tunables.effective(server, {"correlation": {"window_hours": 48}})
        assert server["correlation"]["window_hours"] == before

    def test_a_stored_setting_outside_the_allowlist_is_dropped(self, server):
        """A session created before the allowlist existed has arbitrary
        objects in `config_json`. Honouring one now would apply a setting
        nobody validated."""
        merged = tunables.effective(
            server, {"llm": {"api_key_env": "SOMETHING_ELSE"},
                     "correlation": {"window_hours": 48}})
        assert merged["llm"] == server["llm"]
        assert merged["correlation"]["window_hours"] == 48

    def test_what_was_dropped_can_be_reported(self, server):
        assert sorted(tunables.unsupported(
            {"llm": {"model": "x"}, "triage": {"nope": 1, "batch_size": 4}}
        )) == ["llm", "triage.nope"]

    def test_no_tunables_is_the_server_configuration(self, server):
        assert tunables.effective(server, {}) == server
        assert tunables.effective(server, None) == server


# ---------------------------------------------------------------------------
# It reaches the pipeline. This is the part the original defect failed.
# ---------------------------------------------------------------------------


class TestItGovernsTheRun:
    def _session(self, db, overrides, server):
        session = db.insert_session(
            label="tuned", tracks=["AIRSHOW"],
            config=tunables.validate(overrides, server))
        db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        return session

    def test_the_orchestrator_binds_the_session_configuration(self, db, config):
        from test_orchestrator import build

        # Read the server's value rather than hardcoding one: it comes from
        # the loaded mission's thresholds, and this test's claim is that the
        # session override does not reach back and change it.
        server_window = config["correlation"]["window_hours"]
        session = self._session(
            db, {"correlation": {"window_hours": 48}}, config)
        orch = build(db, config, {})
        orch.start(session)
        assert orch.config["correlation"]["window_hours"] == 48
        assert config["correlation"]["window_hours"] == server_window, (
            "the server's own configuration must be left alone")

    def test_binding_a_second_session_does_not_inherit_the_first(self, db,
                                                                 config):
        """One orchestrator can drive two sessions in a stepped run. Merging
        onto the previous result would carry an override across."""
        from test_orchestrator import build

        server_window = config["correlation"]["window_hours"]
        tuned = self._session(db, {"correlation": {"window_hours": 48}}, config)
        plain = self._session(db, {}, config)
        orch = build(db, config, {})
        orch.start(tuned)
        db.finish_iteration(db.open_iterations(tuned)[0]["iteration_id"],
                            outcome="PARTIAL")
        orch.start(plain)
        assert orch.config["correlation"]["window_hours"] == server_window

    def test_the_spend_envelope_honours_a_lowered_cap(self, db, config):
        from test_orchestrator import build

        session = self._session(
            db, {"budget": {"per_iteration_cap": {"FR24": 50.0}}}, config)
        orch = build(db, config, {})
        iteration = orch.start(session)
        plan = json.loads(
            db.get_iteration(iteration)["budget_plan_json"] or "{}")
        assert plan["FR24"] <= 50.0, plan

    def test_a_narrowed_triage_window_drops_the_posts_it_excludes(
        self, db, config
    ):
        """An analytical override reaching an agent, observed through what the
        agent did rather than through what it was handed.

        8.9 records every pre-model drop, so a post excluded by the session's
        own `max_post_age_hours` leaves a `triage_skips` row naming the reason
        — which is how this is visible at all.
        """
        from surge_iw.agents.triage import TriageAgent
        from test_triage import FakeLLM, decision, post, store_posts

        session = self._session(
            db, {"triage": {"max_post_age_hours": 6.0}}, config)
        iteration = db.insert_iteration(session)
        query = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="r")
        # Both are well inside the server's 168-hour default, so only the
        # session's own 6-hour cut can separate them.
        fresh = post("https://x.com/fresh", "x.com", hours_ago=2)
        stale = post("https://x.com/stale", "x.com", hours_ago=48)
        store_posts(db, iteration, [fresh, stale], query)

        effective = tunables.effective(
            config, json.loads(db.get_session(session)["config_json"]))
        TriageAgent(db, effective,
                    FakeLLM([decision(fresh["url"])])).run(iteration)

        judged = {row["url"] for row in db.all(
            "SELECT url FROM triage_decisions WHERE iteration_id = ?",
            (iteration,))}
        assert fresh["url"] in judged
        assert stale["url"] not in judged
        skips = {row["url"]: row["reason"] for row in db.triage_skips(iteration)}
        assert stale["url"] in skips

    def test_a_receipt_records_the_configuration_that_produced_it(self, db,
                                                                  config):
        """The serious half of the issue. `config_hash` is what makes a
        judgement reconstructible, and while tunables were stored-but-unread it
        named a configuration the run had not used."""
        from surge_iw.agents.triage import TriageAgent
        from test_triage import FakeLLM, decision, post, store_posts

        session = self._session(
            db, {"correlation": {"window_hours": 48}}, config)
        iteration = db.insert_iteration(session)
        query = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="r")
        p = post("https://x.com/1", "x.com")
        store_posts(db, iteration, [p], query)

        stored = json.loads(db.get_session(session)["config_json"])
        effective = tunables.effective(config, stored)
        TriageAgent(db, effective, FakeLLM([decision(p["url"])])).run(iteration)

        stamped = db.one("SELECT config_hash FROM receipts")["config_hash"]
        assert stamped == receipts.config_fingerprint(effective)
        assert stamped != receipts.config_fingerprint(config), (
            "a receipt stamped from the process configuration would be the "
            "wrong answer to the only question a receipt exists to answer")


class TestNothingSettableIsInert:
    """A tunable that is accepted and never read is issue #11 all over again.

    That issue was about `tunables` being stored and never applied: a client
    received a 201 and had `receipts.config_hash` stamped from settings nothing
    used. The allowlist fixed the mechanism; this asserts the CONTENT — every
    key a client may set is actually read somewhere.

    Found live: `correlation.lodging_drop_min` was settable and inert — scored
    at 0.0, 20.0, 100.0 and 999.0 the result was identical. It has since been
    removed from both the allowlist and the configuration.
    """

    #: Known-inert entries, with the reason. Empty, and meant to stay that
    #: way: an entry here is a debt, not a dispensation. The one that was here
    #: — `correlation.lodging_drop_min` — was removed from `ALLOWED` and from
    #: `DEFAULT_CONFIG` rather than excused.
    KNOWN_INERT: dict[str, str] = {}

    def _read_somewhere(self, key: str) -> bool:
        import pathlib, re
        root = pathlib.Path(__file__).resolve().parents[1]
        paths = [p for p in (root / "surge_iw").rglob("*.py")
                 if "__pycache__" not in p.parts
                 and p.name not in ("config.py", "tunables.py")]
        paths += list((root / "scripts").rglob("*.py")) + [root / "run.py"]
        blob = "\n".join(p.read_text(encoding="utf-8") for p in paths)
        # Whole-file, not per line: real reads wrap across lines.
        pattern = (rf'(\[\s*["\']{re.escape(key)}["\']\s*\]'
                   rf'|\.get\(\s*["\']{re.escape(key)}["\'])')
        return re.search(pattern, blob, re.S) is not None

    def test_the_probe_finds_keys_that_are_definitely_read(self):
        """Guards the test itself: a probe that found nothing would pass the
        assertion below for the wrong reason."""
        for key in ("window_hours", "batch_size", "max_tip_depth",
                    "confirm_min_salience"):
            assert self._read_somewhere(key), key

    def test_every_settable_key_is_read_somewhere(self):
        inert = []
        for section, keys in tunables.ALLOWED.items():
            for key in keys:
                if key in ("per_iteration_cap", "monthly_limit"):
                    continue          # read by provider, not by key name
                if not self._read_somewhere(key):
                    inert.append(f"{section}.{key}")
        unexpected = sorted(set(inert) - set(self.KNOWN_INERT))
        assert unexpected == [], (
            f"settable but read nowhere: {unexpected}. A tunable that is "
            f"accepted and ignored is the defect issue #11 was about.")

    def test_the_known_inert_list_does_not_go_stale(self):
        """If one is wired or removed, it must leave this list — otherwise the
        list stops describing anything and starts excusing everything."""
        still_inert = [k for k in self.KNOWN_INERT
                       if not self._read_somewhere(k.split(".", 1)[1])]
        assert sorted(still_inert) == sorted(self.KNOWN_INERT), (
            "a known-inert tunable is now read or gone; drop it from "
            "KNOWN_INERT")
