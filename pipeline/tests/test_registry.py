"""Registry lookup tests. Network is stubbed, so these are offline."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bowow_pipeline import registry as reg
from bowow_pipeline.match import _org_text
from bowow_pipeline.models import Org

PP_ORG = {
    "organization": {
        "ein": 770568469, "name": "Reading Partners", "state": "CA",
        "ntee_code": "B92", "subsection_code": 3,
    },
    "filings_with_data": [
        {"tax_prd_yr": 2021, "totrevenue": 30_000_000, "totfuncexpns": 29_000_000},
        {"tax_prd_yr": 2023, "totrevenue": 36_214_983, "totfuncexpns": 35_642_772},
    ],
}

CRA_IDENT = {
    "BN": "107782609RR0001", "Legal Name": "LITERACY NORTH HALTON",
    "City": "GEORGETOWN", "Province": "ON", "Category": "0011",
}
CRA_FIN = {
    "BN": "107782609RR0001", "FPE": "2025-03-31",
    reg.T3010_TOTAL_REVENUE: "453153",
    reg.T3010_TOTAL_EXPENSE: "441000",
    reg.T3010_PROGRAM_EXPENSE: "419953",
}


# --- ProPublica ------------------------------------------------------------

@pytest.fixture
def propublica(monkeypatch):
    monkeypatch.setattr(reg, "_get_json", lambda url, timeout=45: PP_ORG)
    return reg.ProPublicaRegistry()


def test_us_profile_maps_registry_fields(propublica):
    org = propublica.fetch("77-0568469")
    assert org.name == "Reading Partners"
    assert org.country == "US" and org.region == "CA"
    assert org.registry_id == "770568469"
    assert org.synthetic is False


def test_us_profile_uses_the_most_recent_filing(propublica):
    """Filings arrive unordered; the newest one is the live figure."""
    assert propublica.fetch("77-0568469").revenue == 36_214_983


def test_us_program_expense_is_never_inferred(propublica):
    """ProPublica exposes total functional expenses and no program-service
    line. Substituting one for the other would relabel a different number."""
    org = propublica.fetch("77-0568469")
    assert org.program_expense is None
    assert org.revenue is not None          # revenue genuinely is available


@pytest.mark.parametrize("ntee,expected", [
    ("B92", "Nonprofit"),       # educational services, not a school
    ("B21", "School"),
    ("B43", "Higher education"),
    ("P20", "Nonprofit"),       # human services, unmapped major group
    ("", "Nonprofit"),
])
def test_ntee_codes_map_to_applicant_types(monkeypatch, ntee, expected):
    payload = json.loads(json.dumps(PP_ORG))
    payload["organization"]["ntee_code"] = ntee
    monkeypatch.setattr(reg, "_get_json", lambda url, timeout=45: payload)
    assert reg.ProPublicaRegistry().fetch("1").applicant_type == expected


# --- CRA -------------------------------------------------------------------

@pytest.fixture
def cra(tmp_path):
    registry = reg.CraRegistry(tmp_path)
    registry._ident = {CRA_IDENT["BN"]: CRA_IDENT}
    registry._financial = {CRA_FIN["BN"]: CRA_FIN}
    return registry


def test_ca_profile_uses_verified_t3010_lines(cra):
    """4700 revenue, 5000 charitable programmes. Both checked against the
    live file rather than read off the form."""
    org = cra.fetch("107782609RR0001")
    assert org.revenue == 453_153
    assert org.program_expense == 419_953
    assert org.country == "CA" and org.region == "ON"


def test_ca_lookup_accepts_a_bare_business_number(cra):
    assert cra.fetch("107782609").name == "LITERACY NORTH HALTON"


def test_ca_lookup_returns_none_when_absent(cra):
    assert cra.fetch("999999999RR0001") is None


def test_ca_search_is_case_insensitive(cra):
    assert cra.search("literacy")[0]["BN"] == "107782609RR0001"


# --- dataset discovery -----------------------------------------------------

def _package(title, resources):
    return {"title": title, "resources": resources}


def test_latest_dataset_picks_by_year_not_modification_date(monkeypatch):
    """Old releases get re-touched, so modified date is the wrong key."""
    payload = {"result": {"results": [
        _package("2019 List of charities", [
            {"name": "Identification", "url": "/a/ident_2019.csv"},
            {"name": "Financial data", "url": "/a/fin_2019.csv"}]),
        _package("2025 List of charities", [
            {"name": "Identification", "url": "/b/ident_2025.csv"},
            {"name": "Financial data", "url": "/b/fin_2025.csv"}]),
    ]}}
    monkeypatch.setattr(reg, "_get_json", lambda url, timeout=45: payload)
    year, urls = reg.CraRegistry("x").latest_dataset()
    assert year == "2025"
    assert "ident_2025" in urls["identification"]


def test_relative_resource_urls_are_made_absolute(monkeypatch):
    """CKAN returns some urls site-relative and others absolute."""
    payload = {"result": {"results": [
        _package("2025 List of charities", [
            {"name": "Identification", "url": "/data/x/ident.csv"},
            {"name": "Financial data",
             "url": "https://open.canada.ca/data/y/fin.csv"}]),
    ]}}
    monkeypatch.setattr(reg, "_get_json", lambda url, timeout=45: payload)
    _, urls = reg.CraRegistry("x").latest_dataset()
    assert urls["identification"].startswith("https://open.canada.ca/")
    assert urls["financial"].startswith("https://open.canada.ca/")


def test_missing_resource_is_an_explicit_error(monkeypatch):
    payload = {"result": {"results": [
        _package("2025 List of charities",
                 [{"name": "Identification", "url": "/x.csv"}])]}}
    monkeypatch.setattr(reg, "_get_json", lambda url, timeout=45: payload)
    with pytest.raises(RuntimeError, match="financial"):
        reg.CraRegistry("x").latest_dataset()


def test_unknown_country_is_rejected():
    with pytest.raises(ValueError, match="US, CA"):
        reg.get_registry("FR", "x")


# --- consequence for matching ----------------------------------------------

def test_org_name_feeds_topical_matching():
    """Neither registry publishes mission text, so for a registry profile the
    legal name is the only topical signal available. Without it, a literacy
    charity matched on budget and funder volume alone."""
    org = Org(org_id="x", name="LITERACY NORTH HALTON", country="CA",
              region="ON", applicant_type="Nonprofit")
    assert "literacy" in _org_text(org).lower()
