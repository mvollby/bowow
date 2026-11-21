"""Matching tests: hard gates, component scoring and the honesty rules."""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bowow_pipeline.match import rank, score_match
from bowow_pipeline.models import Opportunity, Org

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "orgs_ca.json"


def _org(**kw) -> Org:
    base = dict(org_id="o1", name="Test Society", country="CA",
                region="Ontario", applicant_type="Nonprofit",
                revenue=200_000, categories=["learning support"],
                mission="tutoring and literacy programs", synthetic=True)
    base.update(kw)
    return Org(**base)


def _opp(**kw) -> Opportunity:
    future = (date.today() + timedelta(days=120)).isoformat()
    base = dict(source="grants_gov", native_id="1", title="Literacy tutoring grant",
                funder_name="Some Agency", summary="supports tutoring and learning",
                applicant_types=["Nonprofit"], regions=["Canada"],
                purposes=["Program delivery"], amount_min=20_000,
                amount_max=40_000, currency="CAD", deadline=future,
                match_required=False, partner_required=False)
    base.update(kw)
    return Opportunity(**base)


# --- hard gates ------------------------------------------------------------

def test_geography_gate_blocks_wrong_country():
    result = score_match(_org(country="CA"), _opp(regions=["United States"]))
    assert not result.eligible
    assert result.failed[0][0] == "Geography"


def test_international_region_is_open_to_anyone():
    result = score_match(_org(country="CA"), _opp(regions=["International"]))
    assert "Geography" in result.passed


def test_applicant_type_gate_blocks_wrong_type():
    result = score_match(_org(applicant_type="School"),
                         _opp(applicant_types=["Nonprofit"]))
    assert ("Applicant type", result.failed[0][1]) == result.failed[0]
    assert not result.eligible


def test_unresolved_applicant_type_is_unknown_not_a_pass():
    """["Other"] is Grants.gov saying the rule is in prose. Not a green light."""
    result = score_match(_org(), _opp(applicant_types=["Other"]))
    assert result.eligible                       # not blocked
    assert any(name == "Applicant type" for name, _ in result.unknown)
    assert "Applicant type" not in result.passed


def test_expired_deadline_is_blocked():
    past = (date.today() - timedelta(days=5)).isoformat()
    result = score_match(_org(), _opp(deadline=past))
    assert not result.eligible
    assert any(name == "Deadline" for name, _ in result.failed)


# --- components ------------------------------------------------------------

def test_missing_component_is_not_scored_as_zero():
    """The core honesty rule: unknown is dropped and weights renormalize."""
    full = score_match(_org(), _opp())
    partial = score_match(_org(revenue=None), _opp())   # award_size unknown

    size = next(c for c in partial.components if c.name == "award_size")
    assert size.score is None
    assert partial.coverage < full.coverage
    # Losing a component must not look like failing it.
    assert partial.rating > 1.0


def test_coverage_caps_the_top_of_the_band():
    """A record we know little about cannot read as a near-perfect match."""
    rich = score_match(_org(), _opp())
    thin = score_match(_org(revenue=None),
                       _opp(amount_min=None, amount_max=None,
                            match_required=None, partner_required=None))
    assert thin.eligible and rich.eligible
    assert thin.coverage < rich.coverage
    assert thin.rating < rich.rating


def test_award_size_flags_an_implausible_ask():
    small = _org(revenue=50_000)
    result = score_match(small, _opp(amount_min=1_000_000, amount_max=3_000_000))
    size = next(c for c in result.components if c.name == "award_size")
    assert size.score < 0.3
    assert "capacity" in size.explanation


def test_award_size_rewards_a_credible_ask():
    result = score_match(_org(revenue=200_000), _opp(amount_min=20_000, amount_max=40_000))
    size = next(c for c in result.components if c.name == "award_size")
    assert size.score == 1.0
    assert "credible" in size.explanation


def test_matching_funds_weigh_harder_on_a_small_org():
    big = score_match(_org(revenue=5_000_000), _opp(match_required=True))
    small = score_match(_org(revenue=90_000), _opp(match_required=True))
    op_big = next(c for c in big.components if c.name == "operational")
    op_small = next(c for c in small.components if c.name == "operational")
    assert op_small.score < op_big.score
    assert "your budget" in op_small.explanation


def test_every_component_carries_an_explanation():
    """A score nobody can interrogate is worse than no score."""
    result = score_match(_org(), _opp())
    assert all(c.explanation.strip() for c in result.components)


# --- ranking ---------------------------------------------------------------

def test_ineligible_never_outranks_eligible():
    good = _opp(native_id="good")
    blocked = _opp(native_id="blocked", regions=["United States"])
    results = rank(_org(), [blocked, good])
    assert results[0].opportunity.native_id == "good"
    assert results[0].rating > results[1].rating


# --- fixtures --------------------------------------------------------------

def test_fixture_orgs_are_all_marked_synthetic():
    """Nothing in the test set may pass itself off as a registry record."""
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    assert data["orgs"], "fixture file is empty"
    for record in data["orgs"]:
        assert record["synthetic"] is True, record["org_id"]


def test_fixture_registry_ids_cannot_collide_with_real_ones():
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    for record in data["orgs"]:
        rid = record["registry_id"]
        digits = rid.replace("-", "")[:9]
        assert digits.startswith("00000000"), f"{record['org_id']} uses {rid}"


def test_fixtures_cover_a_range_of_budget_bands():
    """A scorer that only works on tidy mid-size charities is not finished."""
    data = json.loads(FIXTURES.read_text(encoding="utf-8"))
    bands = {Org(**r).budget_band for r in data["orgs"]}
    assert {"small", "mid", "large", "unknown"} <= bands


# --- mission validation ----------------------------------------------------

def test_placeholder_text_is_refused_as_a_mission():
    """Regression: the documentation placeholder "..." was pasted verbatim and
    accepted, replacing a real 330-character description with three characters
    of punctuation. No registry publishes mission text, so it was gone."""
    from bowow_pipeline.models import mission_problem
    assert "too short" in mission_problem("...")
    assert mission_problem("") 
    assert "placeholder" in mission_problem("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")


def test_a_short_but_real_mission_is_accepted():
    """The guard must not reject genuine brevity."""
    from bowow_pipeline.models import mission_problem
    assert mission_problem("We help adults learn to read.") == ""
    assert mission_problem(
        "Bursaries for rural students entering trades training.") == ""
