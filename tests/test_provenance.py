"""Phase 7.3–7.4: who told us, what they told us, and where they said it was.

Three resolvers, one discipline, taken from `services/geo.py`: an explicit alias
table, a minimum length before any fuzzy step, a uniqueness requirement, and a
closed vocabulary for how the answer was reached. Every one of these replaced a
rule that returned a confident answer for the wrong thing.
"""
from __future__ import annotations


import pytest

from surge_iw.services import facility, provenance


# ===========================================================================
# Publisher identity
# ===========================================================================


class TestPublisherKey:
    @pytest.mark.parametrize("value", [
        "apnews.com", "www.apnews.com", "WWW.APNews.COM", "m.apnews.com",
        "amp.apnews.com", " apnews.com ", "https://www.apnews.com/article/1",
        "Associated Press", "associated press", "The Associated Press", "AP",
    ])
    def test_every_alias_of_one_publisher_resolves_to_one_key(self, value):
        """`www.apnews.com` and `apnews.com` counted as two independent
        publishers, and for news the vendor puts a DISPLAY NAME in the domain
        field — so "associated press" was a third."""
        assert provenance.resolve_publisher(value).key == "apnews.com"

    def test_a_subdomain_resolves_to_its_organisation(self):
        assert provenance.resolve_publisher("news.bbc.co.uk").key == "bbc.co.uk"
        assert provenance.resolve_publisher("edition.cnn.com").key == "cnn.com"

    def test_the_resolution_method_is_recorded(self):
        assert provenance.resolve_publisher("Reuters").method == "ALIAS"
        assert provenance.resolve_publisher("example.org").method == "HOST"
        assert provenance.resolve_publisher("").method == "UNKNOWN"

    def test_two_unknowns_are_not_two_publishers(self):
        """The conservative direction. Under-counting corroboration delays an
        alert; over-counting manufactures one."""
        rows = [{"publisher_key": "", "publisher_method": "UNKNOWN"},
                {"publisher_key": "", "publisher_method": "UNKNOWN"}]
        assert provenance.independent_publishers(rows) == 0

    def test_a_row_written_before_phase_7_still_resolves(self):
        """publisher_key is a nullable migrated column, so every historical
        signal has NULL there. Counting those as unresolved would collapse the
        social quality of every past correlation."""
        rows = [{"source_domain": "www.apnews.com"},
                {"source_domain": "apnews.com"},
                {"source_domain": "reuters.com"}]
        assert provenance.independent_publishers(rows) == 2

    def test_a_stored_key_is_preferred_over_re_resolution(self):
        row = {"publisher_key": "pinned.example", "publisher_method": "ALIAS",
               "source_domain": "somewhere-else.com"}
        assert provenance.publisher_for_row(row).key == "pinned.example"


class TestClaimIdentity:
    def test_tracking_parameters_do_not_make_a_second_claim(self):
        one = provenance.claim_of({"url": "https://apnews.com/a/1"})
        two = provenance.claim_of(
            {"url": "https://www.apnews.com/a/1/?utm_source=x&fbclid=y"})
        assert one == two

    def test_two_hosts_carrying_one_wire_story_are_one_claim(self):
        """Triage deduplicates by exact URL, so syndicated copies at different
        addresses counted as independent corroboration of each other."""
        text = ("A second demonstration team has been added to the "
                "Riverside Fairground display ahead of tomorrow's practice "
                "day, the promoter said")
        a = provenance.claim_of({"title": "Second team added", "snippet": text})
        b = provenance.claim_of({"title": "Second team added", "snippet": text})
        assert a == b

    def test_a_wire_story_at_two_REAL_urls_is_still_one_claim(self):
        """Review #8, HIGH. The production shape, which the gate missed.

        `claim_of` preferred the canonical URL whenever there was one, and
        triage refuses a post without one — so the syndication clause was
        unreachable for exactly the traffic it was written for. Two publishers
        running the same paragraph then satisfied the independence gate, and
        republication breadth read as independent reporting: with
        `expand_cities`, enough to admit a city on one story.
        """
        text = ("A second demonstration team has been added to the Riverside "
                "Fairground display ahead of tomorrow's practice day, "
                "organisers said")
        rows = [
            {"source_domain": "apnews.com", "title": "Second team added",
             "snippet": text, "url": "https://apnews.com/article/phoenix-1"},
            {"source_domain": "abc15.com", "title": "Second team added",
             "snippet": text, "url": "https://abc15.com/story/phoenix-2"},
        ]
        publishers, claims = provenance.corroboration(rows)
        assert publishers == 2
        assert claims == 1
        assert min(publishers, claims) == 1

    def test_a_short_post_still_falls_back_to_its_url(self):
        """The other direction, and why there is a word floor. Two short posts
        share their opening words with many others; merging on that would
        collapse separate reports and UNDER-count corroboration, which nothing
        downstream could see."""
        one = provenance.claim_of(
            {"snippet": "crews arriving", "url": "https://x.com/a/1"})
        two = provenance.claim_of(
            {"snippet": "crews arriving", "url": "https://x.com/b/2"})
        assert one != two
        assert one.startswith("u:") and two.startswith("u:")

    def test_two_links_to_one_article_are_still_one_claim(self):
        """URL identity survives the reordering: it is what answers "is this
        the same document", and it is the route taken whenever the text is too
        thin to be specific."""
        assert provenance.claim_of({"url": "https://apnews.com/a/1"}) == \
            provenance.claim_of({"url": "https://www.apnews.com/a/1/?utm_source=x"})

    def test_two_genuinely_different_reports_stay_distinct(self):
        a = provenance.claim_of({"url": "https://apnews.com/a/1"})
        b = provenance.claim_of({"url": "https://reuters.com/b/2"})
        assert a != b

    def test_an_unidentifiable_post_is_its_own_claim(self):
        """Never assert that two things are the same report without evidence."""
        a = provenance.claim_of({"title": "x", "snippet": "short"})
        b = provenance.claim_of({"title": "y", "snippet": "also short"})
        assert a != b


class TestCorroboration:
    def test_syndication_does_not_satisfy_an_independence_gate(self):
        """Two publishers, one claim. Corroboration takes the LOWER."""
        text = ("A second demonstration team has been added to the "
                "Riverside Fairground display, three organisers said")
        rows = [
            {"source_domain": "apnews.com", "title": "Wire", "snippet": text},
            {"source_domain": "abc15.com", "title": "Wire", "snippet": text},
        ]
        publishers, claims = provenance.corroboration(rows)
        assert publishers == 2
        assert claims == 1
        assert min(publishers, claims) == 1

    def test_two_independent_reports_do_satisfy_it(self):
        rows = [
            {"source_domain": "apnews.com", "url": "https://apnews.com/a/1"},
            {"source_domain": "reuters.com", "url": "https://reuters.com/b/2"},
        ]
        assert min(*provenance.corroboration(rows)) == 2

    def test_one_outlet_running_two_investigations_is_one_publisher(self):
        rows = [
            {"source_domain": "apnews.com", "url": "https://apnews.com/a/1"},
            {"source_domain": "apnews.com", "url": "https://apnews.com/a/2"},
        ]
        publishers, claims = provenance.corroboration(rows)
        assert (publishers, claims) == (1, 2)
        assert min(publishers, claims) == 1


# ===========================================================================
# Facility matching
# ===========================================================================


def locations(*names):
    return [{"location_id": index, "name": name}
            for index, name in enumerate(names, start=1)]


class TestFacilityMatching:
    def test_an_exact_name_matches(self):
        rows = locations("Riverside Fairground")
        match = facility.resolve(["Riverside Fairground"], rows)
        assert match.location_id == 1
        assert match.method == "EXACT"

    def test_punctuation_and_spacing_do_not_matter(self):
        rows = locations("Riverside Fairground")
        assert facility.resolve(
            ["  riverside  fairground. "], rows).matched

    def test_a_registered_alias_matches(self):
        rows = locations("Riverside Exhibition Centre")
        assert facility.resolve(
            ["Riverside Exhibition Center"], rows).method == "EXACT"

    def test_a_more_specific_candidate_matches_uniquely(self):
        rows = locations("Riverside Fairground Pavilion",
                         "Lakeside Arena Annex")
        match = facility.resolve(["the Riverside pavilion"], rows)
        assert match.location_id == 1
        assert match.method == "CONTAINED"

    def test_a_generic_candidate_matching_two_facilities_returns_nothing(self):
        """The whole bug: a generic name attached to whichever of these the
        operator happened to register FIRST.

        `center`, `north` and `south` are the ENGINE's structural core, so this
        refuses with no mission loaded at all."""
        rows = locations("North Exhibition Center", "South Exhibition Center")
        match = facility.resolve(["North Center"], rows)
        assert match.location_id is None
        assert match.method == "TOO_GENERIC"

    def test_a_short_substring_does_not_match(self):
        rows = locations("Riverside Fairground")
        assert facility.resolve(["RF"], rows).location_id is None

    def test_a_single_character_matches_nothing(self):
        """A `locations` value that was a bare string used to iterate as
        characters, and a one-character candidate is contained in virtually
        every facility name — anchoring the signal to row #1 regardless."""
        rows = locations("Riverside Fairground")
        for char in "riverside":
            assert facility.resolve([char], rows).location_id is None

    def test_a_registered_name_containing_the_candidate_is_not_enough(self):
        """The old rule matched in BOTH directions, so a registered `City Hall`
        matched a model's `city hall annex parking structure downtown`."""
        rows = locations("City Hall")
        assert facility.resolve(
            ["city hall annex parking structure downtown"], rows
        ).location_id is None

    def test_the_result_does_not_depend_on_registration_order(self):
        forward = locations("North Exhibition Center", "South Exhibition Center")
        backward = locations("South Exhibition Center", "North Exhibition Center")
        assert (facility.resolve(["Exhibition Center"], forward).location_id
                == facility.resolve(["Exhibition Center"], backward).location_id
                is None)

        specific_f = facility.resolve(["North Exhibition Center"], forward)
        specific_b = facility.resolve(["North Exhibition Center"], backward)
        assert forward[specific_f.location_id - 1]["name"] == \
            backward[specific_b.location_id - 1]["name"] == \
            "North Exhibition Center"

    def test_no_candidates_is_distinguished_from_no_match(self):
        rows = locations("Riverside Fairground")
        assert facility.resolve(None, rows).method == "NONE_GIVEN"
        assert facility.resolve(["Somewhere Else Entirely"], rows).method == \
            "NO_MATCH"

    def test_two_facilities_registered_under_one_name_are_refused(self):
        """An operator data problem worth seeing rather than guessing past."""
        rows = locations("Ticket Office", "Ticket Office")
        assert facility.resolve(["Ticket Office"], rows).method == "AMBIGUOUS"

    def test_a_later_candidate_can_resolve_when_an_earlier_one_cannot(self):
        rows = locations("Riverside Fairground")
        match = facility.resolve(
            ["the exhibition center", "Riverside Fairground"], rows)
        assert match.location_id == 1


class TestAbbreviations:
    """9.7 / issue #6.

    `N. Exhibition Center` returned NO_MATCH against a registered `North
    Exhibition Center`. The conservative direction was right — no location beats
    a wrong one — but an operator writing a name the ordinary way got silence.

    The failure was one rung down from where it looked: `north` is a generic
    token and `n` is not, so the containment rung compared `{"n"}` against an
    empty significant set and refused. Expanding the abbreviation makes it an
    EXACT match, above the rung that had the problem, so none of the
    uniqueness or specificity rules had to move — and these tests are mostly
    about proving they did not.
    """

    def test_a_directional_abbreviation_resolves_exactly(self):
        rows = locations("North Exhibition Center", "South Exhibition Center")
        match = facility.resolve(["N. Exhibition Center"], rows)
        assert match.method == "EXACT"
        assert rows[match.location_id - 1]["name"] == "North Exhibition Center"

    def test_the_abbreviated_form_still_picks_the_right_one_of_two(self):
        rows = locations("North Exhibition Center", "South Exhibition Center")
        assert facility.resolve(["S Exhibition Center"], rows).location_id == \
            rows[1]["location_id"]

    def test_institutional_abbreviations_resolve(self):
        rows = locations("Municipal Depot", "County Administration Center")
        assert facility.resolve(["Muni. Depot"], rows).method == "EXACT"
        assert facility.resolve(["County Admin Ctr"], rows).method == "EXACT"

    def test_an_abbreviation_in_the_registered_name_matches_the_long_form(self):
        """Expansion runs on both sides, so it does not matter which way round
        the operator and the model wrote it."""
        rows = locations("N. Exhibition Ctr.")
        assert facility.resolve(["North Exhibition Center"], rows).method == \
            "EXACT"

    def test_a_bare_directional_letter_still_matches_nothing(self):
        """The hole this could have opened. `n` expands to `north`, which is a
        generic token and shorter than the minimum candidate — so it is refused
        for two independent reasons rather than anchored to whatever facility
        happens to have `North` in its name."""
        rows = locations("North Exhibition Center")
        for char in "nsew":
            assert facility.resolve([char], rows).location_id is None

    def test_two_facilities_differing_only_by_abbreviation_are_refused(self):
        """Expansion can collapse two registered names into one key. That is an
        operator data problem, and refusing shows it rather than picking the
        row that was registered first."""
        rows = locations("N. Exhibition Center", "North Exhibition Center")
        assert facility.resolve(["North Exhibition Center"], rows).method == \
            "AMBIGUOUS"

    def test_a_near_miss_abbreviation_does_not_widen_to_its_neighbour(self):
        """`N.` is north and `NE.` is northeast. Specificity is the property
        that stops an abbreviation table becoming a fuzzy matcher."""
        rows = locations("North Exhibition Center", "Northeast Exhibition Center")
        north = facility.resolve(["N. Exhibition Center"], rows)
        assert rows[north.location_id - 1]["name"] == "North Exhibition Center"
        northeast = facility.resolve(["NE Exhibition Center"], rows)
        assert rows[northeast.location_id - 1]["name"] == \
            "Northeast Exhibition Center"

    def test_an_ambiguous_abbreviation_is_deliberately_absent(self):
        """`comm` is commission, committee or community; `reg` is registrar or
        regional. An expansion that guesses wrong is worse than the NO_MATCH it
        replaces, so those are left out on purpose."""
        for risky in ("comm", "reg", "sec", "st", "ct"):
            assert risky not in facility._SPELLINGS, risky


class TestTableIntegrity:
    """The same checks tests/test_geo.py applies to the city alias table."""

    def test_every_publisher_alias_points_at_a_host(self):
        for alias, target in provenance.PUBLISHER_ALIASES.items():
            assert "." in target, f"{alias} -> {target} is not a host"
            assert target == target.lower()

    def test_no_publisher_alias_is_its_own_target(self):
        for alias, target in provenance.PUBLISHER_ALIASES.items():
            assert alias != target

    def test_publisher_aliases_are_normalised_keys(self):
        for alias in provenance.PUBLISHER_ALIASES:
            assert provenance.normalise_name(alias) == alias, (
                f"{alias!r} would never be looked up")

    def test_facility_aliases_are_normalised_keys(self):
        """An alias whose key is not already normalised could never be looked
        up, because `normalise` runs before the lookup.

        Checked here for the pack the engine runs on. Every pack repeats it
        for its own table, in its own tests — the table is the mission's, so
        the claim about it is too."""
        from conftest import REFERENCE_MISSION as loaded
        assert loaded.facility_aliases
        for alias in loaded.facility_aliases:
            collapsed = facility._WS_RE.sub(
                " ", facility._PUNCT_RE.sub(" ", alias.lower())).strip()
            assert collapsed == alias, f"{alias!r} would never be looked up"

    def test_spelling_keys_are_single_normalised_tokens(self):
        """`_SPELLINGS` is applied per token after punctuation is stripped, so
        a key with a space or a dot in it would never be looked up."""
        for short, long in facility._SPELLINGS.items():
            assert short == short.lower().strip()
            assert " " not in short and "." not in short, short
            assert short != long

    def test_no_expansion_needs_a_second_pass(self):
        """A value that is itself a key would expand only halfway, and which
        half you got would depend on dictionary order."""
        for long in facility._SPELLINGS.values():
            assert long not in facility._SPELLINGS, long

    def test_rules_versions_are_stamped(self):
        assert provenance.RULES_VERSION
        assert facility.RULES_VERSION
