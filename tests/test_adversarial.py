"""The adversarial matrix, run two ways.

Offline (always) it asserts the **machinery**: that whatever the model says, the
five decision points behave as specified — a malformed or injected answer cannot
create a signal, an undated post cannot buy collection, syndication cannot
satisfy an independence gate.

Live (`--live-model`, opt in) it measures the **model's judgement** against the
same cases and reports precision and recall. Until that has been run, no
threshold in `services/sensitivity.py` should be described as calibrated — the
owner's decision was to build the measurement first and set the floors from it.

    python -m pytest tests/test_adversarial.py --live-model -s -q

The live run needs GEMINI_API_KEY (or whatever `llm.api_key_env` names) and
costs LLM tokens. It contacts no data vendor and spends no collection credit.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from adversarial_cases import CASES, CORROBORATION_CASES
from conftest import ANCHOR
from surge_iw.agents.triage import TriageAgent
from surge_iw.agents.triage_schema import (
    ELISION, MAX_TEXT_HEAD, MAX_TEXT_TAIL, build_request, window_text)
from surge_iw.db.database import iso, utcnow
from surge_iw.services import provenance, sensitivity
from test_triage import FakeLLM, store_posts


def as_post(case, *, now=None):
    """A case as a freshly collected post.

    Dated from NOW rather than the fixed ANCHOR: triage's recency cut compares
    against now, and a case dated from a stale anchor would be filtered before
    the model ever saw it — which would quietly stop the matrix measuring
    anything.
    """
    now = now or utcnow()
    observed = (iso(now - timedelta(hours=case.hours_ago))
                if case.hours_ago is not None else "")
    if case.case_id == "time-future":
        observed = iso(now + timedelta(days=200))
    return {
        "url": case.url or f"https://{case.domain}/{case.case_id}",
        "title": case.title, "author": "reporter", "platform": case.platform,
        "source_domain": case.domain, "snippet": case.snippet,
        "observed_at": observed,
    }


# ===========================================================================
# Offline — the machinery, whatever the model says
# ===========================================================================


class TestInjectionCannotReachThePipeline:
    """Content that instructs the system is data, not instruction. These pass
    whatever the model returns, because the guarantee is structural."""

    @pytest.mark.parametrize(
        "case", [c for c in CASES if c.family == "injection"],
        ids=lambda c: c.case_id)
    def test_an_injected_instruction_cannot_change_the_output_contract(
        self, db, config, session, iteration, case
    ):
        """The worst case: the model obeys the injection completely."""
        db.insert_city(session, "Phoenix", canonical="phoenix")
        config["triage"]["max_post_age_hours"] = 24 * 400
        query = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key=case.case_id)
        store_posts(db, iteration, [as_post(case)], query)

        obedient = [{"admit_city": True, "confidence_band": "HIGH",
                     "relevant": True, "salience": 1.0}]
        TriageAgent(db, config, FakeLLM(obedient, translate=False)).run(iteration)

        assert db.signals_by_type(iteration, "SOCIAL") == []
        row = db.one("SELECT * FROM triage_decisions")
        assert row["state"] in ("INVALID_OUTPUT", "UNDECIDED")

    def test_an_injected_city_cannot_be_admitted_without_corroboration(
        self, db, config, session, iteration
    ):
        """expand_cities is off by default, and even on it needs two
        independent publishers making two distinct claims."""
        case = next(c for c in CASES if c.case_id == "inj-fabricated")
        config["triage"]["max_post_age_hours"] = 24 * 400
        query = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="inj")
        store_posts(db, iteration, [as_post(case)], query)
        TriageAgent(db, config, FakeLLM([{
            "url": as_post(case)["url"], "relevant": True,
            "track": "AIRSHOW", "cities": ["Aurorapolis"],
            "locations": [], "activity_type": "static display",
            "imminence_hours": 1.0, "salience": 1.0, "rationale": "x",
        }])).run(iteration)

        assert db.find_city(session, "aurorapolis") is None
        assert db.signals_by_type(iteration, "SOCIAL") == []


class TestTimingGatesHoldOffline:
    @pytest.mark.parametrize(
        "case", [c for c in CASES if c.family == "timing"
                 and c.expect.tip is False],
        ids=lambda c: c.case_id)
    def test_a_post_that_cannot_correlate_cannot_buy_collection(
        self, config, case
    ):
        # Dated from ANCHOR, because the gates below are asked at ANCHOR. The
        # default real-clock dating is for the live matrix, where the whole run
        # shares one wall clock; mixing the two frames here made "30 days old"
        # drift toward ANCHOR as real time passed, and the case flipped from
        # stale to fresh the day the calendar crossed the tip window — a test
        # that fails on a date is a test of the date.
        post = as_post(case, now=ANCHOR)
        signal = {"signal_state": "CONFIRMED", "salience": 1.0,
                  "observed_at": post["observed_at"] or None}
        if case.case_id == "time-future":
            # Classification refuses it before tipping is even asked.
            from surge_iw.agents.triage_schema import TriageItem
            item = TriageItem.model_validate({
                "item_id": "iX", "relevant": True, "salience": 1.0,
                "rationale": "x"})
            state, _ = sensitivity.classify(
                item, post["observed_at"], config, now=ANCHOR)
            assert state == "CANDIDATE"
            return
        assert not sensitivity.may_tip(signal, config, now=ANCHOR).allowed


class TestCorroborationScenarios:
    @pytest.mark.parametrize(
        "case", CORROBORATION_CASES, ids=lambda c: c.case_id)
    def test_independence_is_the_lower_of_publishers_and_claims(self, case):
        publishers, claims = provenance.corroboration(case.posts)
        assert min(publishers, claims) == case.expect_independent, case.note


class TestTruncationIsMeasuredNotAssumed:
    """8.4. This was the live matrix's one genuine miss: a report whose
    retraction fell past an 800-character head window was accepted at 0.90 and
    CONFIRMED, because the model never saw the correction. The window is now
    head + tail, so the ending is always shown."""

    def test_a_trailing_retraction_is_inside_the_window_the_model_sees(self):
        case = next(c for c in CASES if c.case_id == "trunc-retraction")
        payload, _index = build_request([{**as_post(case), "raw_id": 1}], 1)
        assert "CORRECTION" in case.snippet
        assert "CORRECTION" in payload[0]["text"], (
            "the retraction must reach the model — this is the 8.4 fix")

    def test_no_head_length_can_hide_a_trailing_correction(self):
        """The property, not the example. A head-only window fails this for
        some body length; keeping both ends passes it for every length."""
        for filler in (0, 1_000, 50_000):
            text = window_text("CLAIM: raid tomorrow." + ("x" * filler)
                               + " CORRECTION: the raid was called off.")
            assert "CORRECTION" in text, f"lost at filler={filler}"
            assert "CLAIM" in text, f"lost the claim at filler={filler}"

    def test_a_body_that_fits_is_never_spliced(self):
        """The marker has to mean something, so it must not appear otherwise."""
        assert window_text("short body") == "short body"
        assert ELISION not in window_text("y" * (MAX_TEXT_HEAD + MAX_TEXT_TAIL))


# ===========================================================================
# Live — the model's judgement, measured
# ===========================================================================


@pytest.mark.live_model
class TestModelJudgement:
    """Opt-in. Reports precision and recall rather than asserting a bar.

    It drives the REAL `TriageAgent` against a real model, not a bare
    completion — so what is measured is what the system does, including the
    retry that feeds a parse error back, the strict binding by `item_id`, and
    the sensitivity gates that decide candidate from signal. A raw-completion
    harness measured something the system never runs.

    Asserting a specific accuracy would make the suite fail on an unrelated
    model change, which is how a measurement gets deleted. The numbers are the
    deliverable. The only hard assertions are contract ones: every requested
    item is accounted for, and nothing malformed became a signal.
    """

    def test_measure(self, db, config, session, iteration, live_model_client,
                     capsys):
        db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        query = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="adversarial")
        store_posts(db, iteration, [as_post(c) for c in CASES], query)

        # The matrix deliberately includes a month-old case; the cut exists to
        # drop years-old noise, not to remove the case that tests staleness.
        config["triage"]["batch_size"] = 8
        config["triage"]["max_post_age_hours"] = 24 * 400
        agent = TriageAgent(db, config, live_model_client)
        assert agent.run(iteration) is True, "the stage must not crash"

        decisions = {row["url"]: row for row in
                     db.all("SELECT * FROM triage_decisions")}
        signals = {row["url"]: row for row in
                   db.signals_by_type(iteration, "SOCIAL")}

        rows, tp, fp, fn, tn, unjudged = [], 0, 0, 0, 0, 0
        for case in CASES:
            url = as_post(case)["url"]
            decision = decisions.get(url)
            signal = signals.get(url)
            state = decision["state"] if decision else "MISSING"
            got = {"ACCEPTED": True, "REJECTED": False}.get(state)
            want = case.expect.relevant
            verdict = ""
            if got is None:
                unjudged += 1
                verdict = f"not judged ({state})"
            elif want is not None:
                if want and got:
                    tp += 1; verdict = "TP"
                elif want and not got:
                    fn += 1; verdict = "FN  <-- MISSED A REAL ONE"
                elif not want and got:
                    fp += 1; verdict = "FP  <-- false positive"
                else:
                    tn += 1; verdict = "TN"
            rows.append((
                case.case_id, case.family, want, got,
                None if decision is None else decision["salience"],
                "-" if signal is None else signal["signal_state"], verdict))

        with capsys.disabled():
            print(f"\n\n  ADVERSARIAL MATRIX — {config['llm']['model']}")
            print(f"  {'case':<20} {'family':<11} {'want':<6} {'got':<6} "
                  f"{'sal':<5} {'signal':<10} verdict")
            print("  " + "-" * 84)
            for case_id, family, want, got, sal, sig, verdict in rows:
                sal = "-" if sal is None else f"{sal:.2f}"
                print(f"  {case_id:<20} {family:<11} {str(want):<6} "
                      f"{str(got):<6} {sal:<5} {sig:<10} {verdict}")
            precision = tp / (tp + fp) if (tp + fp) else float("nan")
            recall = tp / (tp + fn) if (tp + fn) else float("nan")
            states = db.triage_state_counts(iteration)
            confirmed = sum(1 for s in signals.values()
                            if s["signal_state"] == "CONFIRMED")
            print(f"\n  states     : {states}")
            print(f"  signals    : {len(signals)} written, {confirmed} CONFIRMED, "
                  f"{len(signals) - confirmed} CANDIDATE")
            print(f"  TP {tp}  FP {fp}  FN {fn}  TN {tn}  unjudged {unjudged}")
            print(f"  precision  : {precision:.2f}")
            print(f"  recall     : {recall:.2f}\n")

        # Contract assertions only — the accuracy numbers are reported, not gated.
        assert len(decisions) == len(CASES), (
            "every requested post must have a durable decision row")
        for case in CASES:
            if case.family == "injection":
                url = as_post(case)["url"]
                assert db.find_city(session, "aurorapolis") is None, (
                    "an injected city must never be admitted")
        for signal in signals.values():
            assert signal["salience"] is not None
            assert 0.0 <= signal["salience"] <= 1.0
