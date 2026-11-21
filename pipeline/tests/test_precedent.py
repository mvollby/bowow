"""Funder precedent tests.

The identifier handling here is the part that silently fails: award rows carry
a 9-digit business number and the charity register is keyed by the 15-character
BN plus program identifier, so joining the raw strings matches nothing and
reports no error.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bowow_pipeline.precedent import (MIN_GRANTEES, FunderProfile, bn_root,
                                      build_profiles, score_applicant)


def _row(funder="Some Dept", ref="", amount=50_000):
    return {"funder_name": funder, "grantee_ref": ref, "amount": amount}


# --- identifiers -----------------------------------------------------------

def test_bn_root_normalizes_both_identifier_shapes():
    """Observed live: awards carry 9 digits, the register carries 15 chars."""
    assert bn_root("850190752") == "850190752"
    assert bn_root("850190752RR0001") == "850190752"
    assert bn_root("85019 0752") == "850190752"


def test_bn_root_rejects_a_short_or_empty_value():
    assert bn_root("") == ""
    assert bn_root("1234") == ""
    assert bn_root(None) == ""


def test_join_succeeds_across_the_two_shapes():
    """The regression that matters: the raw strings never match."""
    rows = [_row(ref="850190752RR0001"), _row(ref="850190752")]
    revenue = {"850190752": 400_000.0}
    profiles = build_profiles(rows, revenue)
    assert profiles["some dept"].matched == 2


# --- aggregation -----------------------------------------------------------

def test_profile_counts_coverage_separately_from_awards():
    rows = [_row(ref="111111111"), _row(ref=""), _row(ref="999999999")]
    profiles = build_profiles(rows, {"111111111": 300_000.0})
    profile = profiles["some dept"]
    assert profile.awards == 3            # every row
    assert profile.grantees_with_bn == 2  # rows carrying an identifier
    assert profile.matched == 1           # resolved in the register
    assert profile.match_rate == pytest.approx(0.5)


def test_profiles_are_kept_per_funder():
    rows = [_row(funder="A", ref="111111111"), _row(funder="B", ref="111111111")]
    profiles = build_profiles(rows, {"111111111": 1.0})
    assert set(profiles) == {"a", "b"}


# --- scoring ---------------------------------------------------------------

def _profile(revenues):
    return FunderProfile("F", awards=len(revenues), grantees_with_bn=len(revenues),
                         matched=len(revenues), revenues=list(revenues),
                         amounts=[50_000] * len(revenues))


def test_thin_evidence_is_not_a_distribution():
    """Four grantees cannot describe a funder's typical applicant."""
    profile = _profile([100_000] * (MIN_GRANTEES - 1))
    assert profile.usable is False
    score, why = score_applicant(profile, 100_000)
    assert "too few to compare" in why


def test_applicant_inside_the_usual_range_scores_full():
    profile = _profile([100_000, 200_000, 300_000, 400_000, 500_000, 600_000])
    score, why = score_applicant(profile, 350_000)
    assert score == 1.0
    assert "squarely in this funder's usual range" in why


def test_applicant_far_below_the_usual_range_is_penalised():
    """The case the whole component exists for: a $2m grant is technically
    open to a $120k charity and realistically out of reach."""
    profile = _profile([1_000_000, 2_000_000, 3_000_000, 4_000_000,
                        5_000_000, 6_000_000])
    score, why = score_applicant(profile, 120_000)
    assert score < 0.5
    assert "much smaller" in why


def test_applicant_slightly_below_is_treated_gently():
    profile = _profile([400_000, 500_000, 600_000, 700_000, 800_000, 900_000])
    score, why = score_applicant(profile, 300_000)
    assert 0.5 < score < 1.0
    assert "not outside it" in why


def test_unknown_applicant_revenue_is_not_a_zero():
    profile = _profile([100_000, 200_000, 300_000, 400_000, 500_000, 600_000])
    score, why = score_applicant(profile, None)
    assert score == 0.5
    assert "not on file" in why


def test_explanation_quotes_the_actual_distribution():
    """A score nobody can interrogate is worse than no score."""
    profile = _profile([100_000, 200_000, 300_000, 400_000, 500_000, 600_000])
    _, why = score_applicant(profile, 350_000)
    assert "typical grantee reports" in why and "middle half" in why
