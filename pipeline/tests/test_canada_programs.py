"""Tests for the derived Canadian programme catalogue.

The thing most worth protecting here is the distinction between evidence and
rule: these records say who HAS BEEN funded, not who is ELIGIBLE, and they are
not open calls. If that distinction erodes, the product starts telling
nonprofits they can apply to something that may not be accepting applications.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bowow_pipeline.adapters.canada_programs import (
    MIN_AWARDS, RECIPIENT_TYPE_MAP, ROUTE, CanadaProgramsAdapter, _percentile,
    award_range)

AGG = {
    "department": "Employment and Social Development Canada",
    "program": "Adult Learning, Literacy and Essential Skills Program",
    "purpose": "Supports projects that improve adult literacy.",
    "values": [10_000, 25_000, 50_000, 75_000, 900_000],
    "recipient_types": {"N": 12, "S": 3, "P": 1},
    "provinces": {"ON": 9, "BC": 4},
    "awards": 16,
    "latest_start": "2025-04-01",
}


def test_recipient_codes_match_the_published_schema():
    """Verified against the publisher's schema. Guessing these gets it wrong:
    S is Academia (not School) and P is Individual (not Private)."""
    assert RECIPIENT_TYPE_MAP["S"] == "Higher education"
    assert RECIPIENT_TYPE_MAP["P"] == "Individual"
    assert RECIPIENT_TYPE_MAP["N"] == "Nonprofit"
    assert RECIPIENT_TYPE_MAP["A"] == "Tribal"
    assert RECIPIENT_TYPE_MAP["F"] == "For-profit"


def test_parse_builds_an_opportunity_from_aggregate():
    opp = CanadaProgramsAdapter().parse(AGG)
    assert opp.title == AGG["program"]
    assert opp.funder_name == AGG["department"]
    assert opp.regions == ["Canada"]
    assert opp.currency == "CAD"
    assert opp.applicant_types == ["Higher education", "Individual", "Nonprofit"]


def test_programs_are_never_presented_as_open_calls():
    """No deadline and an explicit route, so the UI cannot imply a live call."""
    opp = CanadaProgramsAdapter().parse(AGG)
    assert opp.deadline is None
    assert opp.route == ROUTE
    assert "no posted call" in opp.route


def test_applicant_types_are_labelled_as_observed_not_as_a_rule():
    opp = CanadaProgramsAdapter().parse(AGG)
    snippet = opp.field_provenance["applicant_types"]["snippet"]
    assert "not a published eligibility rule" in snippet
    assert "16 past awards" in snippet


def test_eligibility_rules_are_never_invented():
    """Nothing is published about match or partner rules, so both stay unknown."""
    opp = CanadaProgramsAdapter().parse(AGG)
    assert opp.match_required is None
    assert opp.partner_required is None
    assert opp.eligibility_raw == ""


def test_confidence_stays_below_a_confident_reading():
    """Observed grantees are strong evidence but not an eligibility statement,
    and no extraction pass can promote them into one."""
    assert CanadaProgramsAdapter().parse(AGG).extraction_confidence < 0.8


def test_amount_range_ignores_the_outlier():
    """A single atypical megagrant must not define a programme's range."""
    opp = CanadaProgramsAdapter().parse(AGG)
    assert opp.amount_max < 900_000     # the outlier is excluded
    assert opp.amount_min <= opp.amount_max


def test_award_range_method_follows_the_sample_size():
    """Percentiles only exclude outliers when there is a tail to exclude."""
    many = [1000 * i for i in range(1, 21)] + [5_000_000]
    low, high, note = award_range(many)
    assert high < 5_000_000 and "percentile" in note

    few = [10_000, 25_000, 50_000, 75_000, 900_000]
    low, high, note = award_range(few)
    assert (low, high) == (25_000, 75_000)
    assert "highest and lowest excluded" in note

    barely = [10_000, 900_000]
    low, high, note = award_range(barely)
    assert (low, high) == (10_000, 900_000)
    # The figure is published but must announce that it is not typical.
    assert "too few to be typical" in note

    assert award_range([]) == (None, None, "no usable agreement values")


def test_percentile_is_stable_on_small_inputs():
    assert _percentile([], 0.5) == 0.0
    assert _percentile([5], 0.9) == 5
    assert _percentile([1, 2, 3, 4, 5], 0.0) == 1
    assert _percentile([1, 2, 3, 4, 5], 1.0) == 5


def test_parse_skips_rows_without_a_programme_or_department():
    adapter = CanadaProgramsAdapter()
    assert adapter.parse({"program": "", "department": "X"}) is None
    assert adapter.parse({"program": "X", "department": ""}) is None


def test_native_id_is_stable_and_scoped_to_the_department():
    a = CanadaProgramsAdapter().parse(AGG)
    b = CanadaProgramsAdapter().parse({**AGG, "department": "Another Department"})
    assert a.native_id == CanadaProgramsAdapter().parse(AGG).native_id
    assert a.native_id != b.native_id


def test_thin_programmes_are_excluded_by_the_award_floor():
    """A programme with one or two awards cannot support a published range."""
    assert MIN_AWARDS >= 3
