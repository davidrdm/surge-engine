"""Loading a session's jurisdictions from a file — 8.7(c).

The load-bearing test is `test_an_unresolvable_city_is_refused_not_skipped`.
Everything else here is parsing; that one is the reason the module exists in
this shape. A loader that returned the cities it understood would create a
session quietly missing a jurisdiction the operator believes is covered, and
every later report about that place would be a true absence of evidence about
somewhere nobody looked — which is the failure this whole system is organised
against.
"""
from __future__ import annotations

import pytest

from conftest import REFERENCE_MISSION
from surge_iw.services import inputs
from surge_iw.services.inputs import InputError


def write(tmp_path, text: str, name: str = "set.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestTheShippedFile:
    """The file in the repo has to load, and every entry in it has to resolve —
    including the commented-out ones, which are there to be uncommented."""

    def test_it_loads(self):
        loaded = inputs.load("inputs/example.yaml", mission=REFERENCE_MISSION)
        assert loaded.cities
        assert all(c.canonical for c in loaded.cities)
        assert all(c.resolved_by != "UNRESOLVED" for c in loaded.cities)

    def test_every_commented_out_city_would_also_resolve(self, tmp_path):
        """A file that ships a trap is worse than one that ships less: an
        operator uncommenting a city expects it to work, not to be refused.

        Verified by actually uncommenting the disabled entries and loading the
        result, rather than by scraping labels — the load is the thing that has
        to succeed, so it is the thing to run.
        """
        lines = open("inputs/example.yaml", encoding="utf-8").read().splitlines()
        first_entry = next(i for i, line in enumerate(lines)
                           if line.strip() and not line.startswith("#"))
        body = [line[2:] if line.startswith("# ") else line
                for line in lines[first_entry:]]
        path = tmp_path / "uncommented.yaml"
        path.write_text("\n".join(body) + "\n", encoding="utf-8")

        loaded = inputs.load(path, mission=REFERENCE_MISSION)
        assert len(loaded.cities) > len(
            inputs.load("inputs/example.yaml", mission=REFERENCE_MISSION).cities), (
            "the file should ship some disabled examples")
        assert all(c.resolved_by != "UNRESOLVED" for c in loaded.cities)
        assert loaded.without_locations == []


class TestRefusalRatherThanSilence:
    def test_an_unresolvable_city_is_refused_not_skipped(self, tmp_path):
        path = write(tmp_path, """
Chicago, IL:
  - Broadview Staging Area
Nowheresville, ZZ:
  - Somewhere
""")
        with pytest.raises(InputError) as caught:
            inputs.load(path, mission=REFERENCE_MISSION)
        message = str(caught.value)
        assert "Nowheresville, ZZ" in message, "the refusal must NAME it"
        assert "Chicago" not in message, "and must not blame the ones that worked"

    def test_nothing_is_created_from_a_partially_valid_file(self, tmp_path):
        """All-or-nothing. Returning the good cities is the silent-drop failure
        wearing a different hat."""
        path = write(tmp_path, "Chicago, IL:\n  - X\nNowheresville, ZZ:\n  - Y\n")
        with pytest.raises(InputError):
            inputs.load(path, mission=REFERENCE_MISSION)

    def test_an_empty_file_is_refused(self, tmp_path):
        """Every entry commented out is a file that would create a session
        collecting nothing — which looks like a working deployment."""
        path = write(tmp_path, "# Chicago, IL:\n#   - X\n")
        with pytest.raises(InputError, match="empty"):
            inputs.load(path, mission=REFERENCE_MISSION)

    def test_two_entries_for_one_jurisdiction_are_refused(self, tmp_path):
        """'Chicago' and 'Chicago, IL' resolve to the same key, so one entry's
        key locations would be silently ignored."""
        path = write(tmp_path, "Chicago, IL:\n  - A\nChicago:\n  - B\n")
        with pytest.raises(InputError, match="both resolve"):
            inputs.load(path, mission=REFERENCE_MISSION)

    def test_a_missing_file_names_the_path(self, tmp_path):
        with pytest.raises(InputError, match="No input file"):
            inputs.load(tmp_path / "absent.yaml", mission=REFERENCE_MISSION)

    def test_malformed_yaml_says_so(self, tmp_path):
        path = write(tmp_path, "Chicago, IL:\n  - [unclosed\n")
        with pytest.raises(InputError, match="not valid YAML"):
            inputs.load(path, mission=REFERENCE_MISSION)

    def test_a_list_at_the_top_level_is_refused(self, tmp_path):
        path = write(tmp_path, "- Chicago\n- Atlanta\n")
        with pytest.raises(InputError, match="must be a mapping"):
            inputs.load(path, mission=REFERENCE_MISSION)


class TestLocations:
    def test_a_bare_string_is_a_name(self, tmp_path):
        path = write(tmp_path, "Chicago, IL:\n  - Broadview Staging Area\n")
        city = inputs.load(path, mission=REFERENCE_MISSION).cities[0]
        assert city.key_locations == [{"name": "Broadview Staging Area"}]

    def test_a_mapping_carries_its_type(self, tmp_path):
        path = write(tmp_path, """
Chicago, IL:
  - name: Northside Airfield
    location_type: airfield
""")
        location = inputs.load(path, mission=REFERENCE_MISSION).cities[0].key_locations[0]
        # Upper-cased on the way in: the file is written by a human and the
        # column is compared exactly.
        assert location["location_type"] == "AIRFIELD"

    def test_an_unknown_location_type_is_a_load_error(self, tmp_path):
        """Named at load, with the file and the city, rather than surfacing as
        a 422 the operator has to trace back to a line."""
        path = write(tmp_path,
                     "Chicago, IL:\n  - name: X\n    location_type: BUNKER\n")
        with pytest.raises(InputError, match="Chicago"):
            inputs.load(path, mission=REFERENCE_MISSION)

    def test_an_unknown_field_is_refused_rather_than_dropped(self, tmp_path):
        """A field that vanishes silently leaves the operator believing it took
        effect — the same argument as the receipts writer's."""
        path = write(tmp_path, "Chicago, IL:\n  - name: X\n    radius_km: 5\n")
        with pytest.raises(InputError, match="unknown field"):
            inputs.load(path, mission=REFERENCE_MISSION)

    def test_a_duplicate_location_is_refused(self, tmp_path):
        path = write(tmp_path, "Chicago, IL:\n  - Broadview\n  - broadview\n")
        with pytest.raises(InputError, match="twice"):
            inputs.load(path, mission=REFERENCE_MISSION)

    def test_a_city_with_no_locations_loads_and_is_reported(self, tmp_path):
        """Not an error — flight and car coverage still work — but the lodging
        family will be absent and that has to be said."""
        path = write(tmp_path, "Chicago, IL:\nAtlanta, GA:\n  - Sam Nunn\n")
        loaded = inputs.load(path, mission=REFERENCE_MISSION)
        assert len(loaded.cities) == 2
        assert loaded.without_locations == ["Chicago, IL"]

    def test_a_nameless_location_is_refused(self, tmp_path):
        path = write(tmp_path, "Chicago, IL:\n  - name: '  '\n")
        with pytest.raises(InputError, match="no name"):
            inputs.load(path, mission=REFERENCE_MISSION)


class TestResolutionHappensAtLoad:
    def test_the_canonical_key_and_its_method_are_settled(self, tmp_path):
        """The owner's decision: resolve by the geo ladder at session creation,
        not at query time. A failure to resolve is cheap to correct now and
        arrives hours later as a SKIPPED_NO_MAPPING query otherwise."""
        path = write(tmp_path, "Chicago, IL:\n  - X\n")
        city = inputs.load(path, mission=REFERENCE_MISSION).cities[0]
        assert (city.name, city.state) == ("Chicago", "IL")
        assert city.canonical == "chicago"
        assert city.resolved_by == "TABLE"

    def test_the_payload_is_the_CityIn_shape(self, tmp_path):
        """So a loaded set goes through the same validation and the same
        `_add_city` path a hand-written body does — one code path, not two."""
        from surge_iw.api import schemas

        path = write(tmp_path, "Chicago, IL:\n  - name: X\n    location_type: OTHER\n")
        payload = inputs.load(path, mission=REFERENCE_MISSION).as_payload()
        cities = [schemas.CityIn.model_validate(c) for c in payload]
        assert cities[0].name == "Chicago"
        assert cities[0].key_locations[0].location_type == "OTHER"


class TestTheNameIsNotAPath:
    """The API takes a NAME and resolves it inside the configured directory.

    A path field on an authenticated endpoint is still a file-disclosure
    primitive: authenticated is not the same as trusted with the filesystem,
    and the caller here is a front end, not the operator at a shell.
    """

    @pytest.mark.parametrize("name", [
        "../../etc/passwd", "/etc/passwd", "sub/dir", "..", "",
        "set;rm", "a b",
    ])
    def test_a_path_is_refused(self, name, tmp_path):
        with pytest.raises(InputError, match="not a valid input set name"):
            inputs.input_path(name, {"inputs": {"dir": str(tmp_path)}})

    def test_a_name_resolves_inside_the_directory(self, tmp_path):
        write(tmp_path, "Chicago, IL:\n  - X\n", name="venues.yaml")
        path = inputs.input_path(
            "venues", {"inputs": {"dir": str(tmp_path)}})
        assert path == tmp_path / "venues.yaml"

    def test_the_extension_is_optional_and_yml_works(self, tmp_path):
        write(tmp_path, "Chicago, IL:\n  - X\n", name="other.yml")
        assert inputs.input_path(
            "other", {"inputs": {"dir": str(tmp_path)}}).name == "other.yml"

    def test_an_unknown_name_lists_what_is_available(self, tmp_path):
        write(tmp_path, "Chicago, IL:\n  - X\n", name="venues.yaml")
        with pytest.raises(InputError, match="venues"):
            inputs.input_path("absent", {"inputs": {"dir": str(tmp_path)}})
