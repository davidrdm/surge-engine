"""City and county resolution.

The class that matters most is TestNoSilentWrongCity. The original
surge/utils/geo.py resolved with:

    if key.startswith(k) or k.startswith(key):
        return v

which returns the first dict entry sharing a prefix in either direction. It never
returns "I don't know" — it returns a confident wrong answer, and the caller then
queries flights into the wrong airport with no indication anything went astray.
For a warning system, resolving to nothing is strictly better than resolving to
somewhere else.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from surge_iw.services import geo


class TestNormalise:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Phoenix", "phoenix"),
            ("  PHOENIX  ", "phoenix"),
            ("Phoenix, AZ", "phoenix"),
            ("Maricopa County", "maricopa county"),
            ("Miami-Dade County", "miami-dade county"),
            ("St. Louis", "st. louis"),
            ("New   York", "new york"),
        ],
    )
    def test_normalisation(self, raw, expected):
        assert geo.normalise(raw) == expected

    def test_county_suffix_is_kept(self):
        """Counties are first-class keys, not cities with a suffix to strip.

        The old code stripped the suffix, turning "orange county" into "orange",
        which then prefix-matched to something else entirely.
        """
        assert geo.normalise("Orange County") == "orange county"
        assert geo.resolve_city("Orange County")[0] == "orange county"

    def test_state_is_split_off_and_returned(self):
        assert geo.split_state("Phoenix, AZ") == ("Phoenix", "AZ")
        assert geo.split_state("Phoenix") == ("Phoenix", None)


class TestExactAndAlias:
    def test_exact_match_wins(self):
        assert geo.resolve_city("Phoenix") == ("phoenix", "TABLE")

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("DC", "washington"),
            ("Washington DC", "washington"),
            ("Washington D.C.", "washington"),
            ("NYC", "new york"),
            ("Brooklyn", "new york"),
            ("St. Louis", "saint louis"),
            ("Ft. Worth", "fort worth"),
            ("Vegas", "las vegas"),
            ("Dallas-Fort Worth", "dallas"),
            ("Miami Dade County", "miami-dade county"),
        ],
    )
    def test_aliases_resolve_explicitly(self, raw, expected):
        key, method = geo.resolve_city(raw)
        assert key == expected
        assert method == "ALIAS"

    def test_dc_resolves_to_three_airports(self):
        assert geo.city_to_airports("DC") == ["DCA", "IAD", "BWI"]


class TestNoSilentWrongCity:
    """Ambiguity must resolve to nothing, never to a guess."""

    @pytest.mark.parametrize("ambiguous", ["san", "new", "s", "sa", "co", "x"])
    def test_short_or_ambiguous_input_resolves_to_nothing(self, ambiguous):
        key, method = geo.resolve_city(ambiguous)
        assert key is None
        assert method == "UNRESOLVED"
        assert geo.city_to_airports(ambiguous) == []

    def test_san_does_not_silently_become_a_specific_san_city(self):
        """The headline case: "San" must not become San Antonio or San Diego."""
        assert geo.city_to_airports("San") == []

    @pytest.mark.parametrize(
        "name,expected_first",
        [
            ("San Antonio", "SAT"),
            ("San Diego", "SAN"),
            ("San Francisco", "SFO"),
            ("San Jose", "SJC"),
        ],
    )
    def test_full_san_names_still_resolve_correctly(self, name, expected_first):
        assert geo.city_to_airports(name)[0] == expected_first

    def test_similar_pairs_do_not_collide(self):
        """columbus/columbia and charleston/charlotte share long prefixes."""
        assert geo.city_to_airports("Columbus") == ["CMH"]
        assert geo.city_to_airports("Columbia") == ["CAE"]
        assert geo.city_to_airports("Charleston") == ["CHS"]
        assert geo.city_to_airports("Charlotte") == ["CLT"]

    def test_prefix_match_requires_a_unique_candidate(self):
        key, method = geo.resolve_city("Orange")
        assert key == "orange county"
        assert method == "PREFIX"

    def test_unknown_city_resolves_to_nothing(self):
        assert geo.resolve_city("Nowheresville")[0] is None
        assert geo.city_to_airports("Nowheresville") == []
        assert geo.city_to_pickup_location("Nowheresville") is None

    def test_empty_input_is_safe(self):
        assert geo.resolve_city("")[0] is None
        assert geo.city_to_airports("") == []


class TestTableIntegrity:
    def test_no_duplicate_keys_in_the_airport_table(self):
        """Python silently keeps the last duplicate key in a dict literal.

        The original geo.py had "richmond" at two different lines; nothing warned
        and nothing failed. The only way to catch it is to parse the source.
        """
        source = Path(geo.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        duplicates: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = [
                t.id for t in node.targets if isinstance(t, ast.Name)
            ]
            if not targets or not isinstance(node.value, ast.Dict):
                continue
            keys = [
                k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            ]
            seen: set[str] = set()
            dupes = [k for k in keys if k in seen or seen.add(k)]
            if dupes:
                duplicates[targets[0]] = dupes
        assert duplicates == {}, f"duplicate dict keys: {duplicates}"

    def test_every_alias_points_at_a_real_entry(self):
        dangling = {
            alias: target for alias, target in geo.CITY_ALIASES.items()
            if target not in geo.CITY_AIRPORTS
        }
        assert dangling == {}

    def test_no_alias_shadows_a_real_city(self):
        """An alias for a name that is already a table key would never be used."""
        shadowed = set(geo.CITY_ALIASES) & set(geo.CITY_AIRPORTS)
        assert shadowed == set()

    def test_every_airport_code_is_three_letters(self):
        bad = {
            city: codes for city, codes in geo.CITY_AIRPORTS.items()
            if not all(len(c) == 3 and c.isupper() for c in codes)
        }
        assert bad == {}

    def test_no_city_has_duplicate_airports(self):
        bad = {
            city: codes for city, codes in geo.CITY_AIRPORTS.items()
            if len(codes) != len(set(codes))
        }
        assert bad == {}


class TestPickupLocation:
    def test_pickup_is_the_primary_airport_code(self):
        """Priceline echoes the code back as pickupLocation.airportCode, so an
        IATA code is an exact round-trip and needs no autocomplete call."""
        assert geo.city_to_pickup_location("Phoenix") == "PHX"
        assert geo.city_to_pickup_location("New York") == "JFK"

    def test_limit_caps_airport_fan_out(self):
        assert len(geo.city_to_airports("Los Angeles", limit=2)) == 2
        assert len(geo.city_to_airports("Los Angeles")) == 5


class TestLodgingLocationString:
    def test_key_location_is_placed_first(self):
        """Anchoring on the facility is the point: a convergence shows up as
        scarcity near the venue, not city-wide."""
        assert geo.lodging_location_string(
            "Phoenix", "AZ", "Riverside Fairground"
        ) == "Riverside Fairground, Phoenix, AZ"

    def test_city_only_is_valid(self):
        assert geo.lodging_location_string("Phoenix", "AZ") == "Phoenix, AZ"
        assert geo.lodging_location_string("Phoenix") == "Phoenix"
