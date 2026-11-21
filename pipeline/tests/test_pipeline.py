"""Offline tests. No network, so these stay fast and deterministic.

The fixtures are trimmed copies of real payloads captured from the live APIs
during the spike, including the quirks that actually broke things.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bowow_pipeline.adapters.canada_gc import CanadaGrantsAdapter
from bowow_pipeline.adapters.grants_gov import GrantsGovAdapter
from bowow_pipeline.models import Opportunity, parse_amount, parse_date, strip_html
from bowow_pipeline.rawstore import RawStore
from bowow_pipeline.store import Store

GG_PAYLOAD = {
    "id": "334326",
    "opportunityTitle": "Tribal Colleges and Universities Program (TCUP)",
    "synopsis": {
        "opportunityId": "334326",
        "agencyName": "U.S. National Science Foundation",
        "synopsisDesc": "<p>Provides awards to <b>Tribal</b> colleges.</p>",
        "applicantEligibilityDesc": "Proposals may only be submitted by federally recognized Tribal Colleges.",
        "applicantTypes": [{"id": "25", "description": "Others (see text field)"}],
        "fundingActivityCategories": [{"id": "ST", "description": "Science and Technology"}],
        "awardCeiling": 3500000,
        "awardFloor": 100000,
        "costSharing": False,
        "responseDate": "Jan 11, 2027 12:00:00 AM EST",
    },
}

# Header key reproduces the real defect: a UTF-8 BOM read through cp1252
# arrives as three separate characters, not U+FEFF.
CA_ROW = {
    "ï»¿ref_number": "199-2021-2022-Q2-002",
    "amendment_number": "1",
    "recipient_legal_name": "Carleton University",
    "recipient_business_number": "108161829RR0001",
    "recipient_province": "ON",
    "recipient_country": "CA",
    "agreement_value": "438311.0",
    "agreement_start_date": "2022-04-01",
    "prog_name_en": "Advancing Accessibility Standards Research",
    "agreement_title_en": "Accessible learning project",
    "owner_org_title": "Employment and Social Development Canada",
}


# --- parsing helpers -------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Jan 11, 2027 12:00:00 AM EST", "2027-01-11"),
    ("06/24/2021", "2021-06-24"),
    ("2026-12-03", "2026-12-03"),
    ("", None),
    ("not a date", None),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("3,500,000", 3500000), ("$250", 250), (0, None), ("", None), ("abc", None),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


def test_strip_html():
    assert strip_html("<p>Hi<br>there &amp; you</p>") == "Hi\nthere & you"


# --- grants.gov adapter ----------------------------------------------------

def test_grants_gov_parse_maps_fields():
    opp = GrantsGovAdapter().parse(GG_PAYLOAD)
    assert opp.native_id == "334326"
    assert opp.funder_name == "U.S. National Science Foundation"
    assert opp.amount_min == 100000 and opp.amount_max == 3500000
    assert opp.deadline == "2027-01-11"
    assert opp.currency == "USD" and opp.regions == ["United States"]
    assert opp.match_required is False          # costSharing False, not unknown
    assert opp.partner_required is None         # genuinely not in the API
    assert "<p>" not in opp.summary


def test_unmapped_applicant_type_lowers_confidence():
    """Code 25 is Grants.gov saying the rule is in prose. Say so, do not guess."""
    opp = GrantsGovAdapter().parse(GG_PAYLOAD)
    assert opp.applicant_types == ["Other"]
    assert opp.extraction_confidence < 0.7


def test_provenance_is_recorded():
    opp = GrantsGovAdapter().parse(GG_PAYLOAD)
    assert "eligibility_raw" in opp.field_provenance
    assert opp.field_provenance["eligibility_raw"]["url"].startswith("https://")


def test_agency_name_is_not_the_contact_person():
    """Observed live: synopsis.agencyName sometimes holds the program officer."""
    payload = {
        "id": "1", "opportunityTitle": "AFRI",
        "agencyDetails": {"agencyName": "National Institute of Food and Agriculture"},
        "topAgencyDetails": {"agencyName": "Department of Agriculture"},
        "synopsis": {
            "agencyName": "Joseph  Perez\nProgram Analyst",
            "agencyContactName": "Joseph  Perez\nProgram Analyst",
        },
    }
    opp = GrantsGovAdapter().parse(payload)
    assert opp.funder_name == "National Institute of Food and Agriculture"


def test_agency_name_falls_back_to_department():
    payload = {
        "id": "1", "opportunityTitle": "X",
        "topAgencyDetails": {"agencyName": "Department of Agriculture"},
        "synopsis": {},
    }
    assert GrantsGovAdapter().parse(payload).funder_name == "Department of Agriculture"


def test_parse_returns_none_on_junk():
    assert GrantsGovAdapter().parse({"id": "", "synopsis": {}}) is None


# --- canada adapter --------------------------------------------------------

def test_canada_header_bom_is_stripped():
    row = CanadaGrantsAdapter._clean_keys(CA_ROW)
    assert "ref_number" in row


def test_canada_parse_keeps_amendment_in_identity():
    award = CanadaGrantsAdapter().parse(CanadaGrantsAdapter._clean_keys(CA_ROW))
    assert award.native_id == "199-2021-2022-Q2-002#1"
    assert award.amendment == 1
    assert award.amount == 438311.0
    assert award.year == 2022
    assert award.grantee_ref == "108161829RR0001"   # the CRA join key


# --- store behaviour -------------------------------------------------------

@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_upsert_is_idempotent(store):
    opp = GrantsGovAdapter().parse(GG_PAYLOAD)
    assert store.upsert_opportunity(opp) == "inserted"
    assert store.upsert_opportunity(opp) == "unchanged"
    assert store.counts()["opportunities"] == 1


def test_change_detection_records_each_field(store):
    opp = GrantsGovAdapter().parse(GG_PAYLOAD)
    store.upsert_opportunity(opp)
    opp.deadline = "2027-03-15"
    opp.amount_max = 4000000
    assert store.upsert_opportunity(opp) == "updated"

    changed = {r["field"]: (r["old_value"], r["new_value"])
               for r in store.recent_changes()}
    assert changed["deadline"] == ("2027-01-11", "2027-03-15")
    assert changed["amount_max"] == ("3500000", "4000000")


def test_applicant_type_reordering_is_not_a_change(store):
    """Upstream returns applicantTypes in unstable order. That is noise.

    Observed live: two calls for the same opportunity returned the same
    applicant types in different order. Sorting in the adapter means the
    fingerprint holds steady and no false change alert fires.
    """
    a = GrantsGovAdapter().parse(GG_PAYLOAD)
    reordered = dict(GG_PAYLOAD)
    reordered["synopsis"] = dict(GG_PAYLOAD["synopsis"])
    reordered["synopsis"]["applicantTypes"] = [
        {"id": "13", "description": "Nonprofits without 501(c)(3)"},
        {"id": "12", "description": "Nonprofits with 501(c)(3)"},
    ]
    b = GrantsGovAdapter().parse(reordered)
    c = GrantsGovAdapter().parse({**reordered, "synopsis": {
        **reordered["synopsis"],
        "applicantTypes": list(reversed(reordered["synopsis"]["applicantTypes"]))}})

    store.upsert_opportunity(a)
    assert store.upsert_opportunity(b) == "updated"      # real change, 25 -> 12/13
    assert store.upsert_opportunity(c) == "unchanged"    # pure reordering


def test_reingest_does_not_erase_extracted_values(store):
    """A re-parse has null for extracted fields. Null must not win over known.

    Without this, every scheduled re-ingest silently discards the eligibility
    extraction the project paid an API bill to produce.
    """
    enriched = GrantsGovAdapter().parse(GG_PAYLOAD)
    enriched.applicant_types = ["Nonprofit"]
    enriched.partner_required = False
    enriched.extraction_confidence = 0.95
    enriched.field_provenance["partner_required"] = {"url": "u", "snippet": "s"}
    store.upsert_opportunity(enriched)

    fresh = GrantsGovAdapter().parse(GG_PAYLOAD)   # ["Other"], partner None
    assert fresh.partner_required is None and fresh.applicant_types == ["Other"]
    store.upsert_opportunity(fresh)

    row = store.conn.execute("SELECT * FROM opportunity").fetchone()
    stored = store.decode(row)
    assert stored.partner_required is False
    assert stored.applicant_types == ["Nonprofit"]
    assert stored.extraction_confidence == 0.95
    assert "partner_required" in stored.field_provenance


def test_source_can_still_correct_an_extracted_value(store):
    """Carrying forward must not freeze a value the funder later states."""
    first = GrantsGovAdapter().parse(GG_PAYLOAD)
    first.partner_required = False
    store.upsert_opportunity(first)

    corrected = GrantsGovAdapter().parse(GG_PAYLOAD)
    corrected.partner_required = True          # source now says otherwise
    store.upsert_opportunity(corrected)

    stored = store.decode(store.conn.execute("SELECT * FROM opportunity").fetchone())
    assert stored.partner_required is True


def test_dedupe_key_groups_reposted_programs(store):
    base = GrantsGovAdapter().parse(GG_PAYLOAD)
    twin = GrantsGovAdapter().parse({**GG_PAYLOAD, "id": "999999"})
    twin.source = "other_source"
    store.upsert_opportunity(base)
    store.upsert_opportunity(twin)
    groups = store.duplicate_groups()
    assert len(groups) == 1 and groups[0]["n"] == 2


def test_award_amendments_are_both_kept(store):
    from bowow_pipeline.models import Award
    first = Award(source="canada_gc", native_id="REF#0", funder_name="ESDC",
                  grantee_name="Carleton University", amount=661126.0, amendment=0)
    second = Award(source="canada_gc", native_id="REF#1", funder_name="ESDC",
                   grantee_name="Carleton University", amount=438311.0, amendment=1)
    assert store.upsert_award(first) == "inserted"
    assert store.upsert_award(second) == "inserted"
    assert store.counts()["awards"] == 2


# --- raw store -------------------------------------------------------------

def test_rawstore_is_content_addressed(tmp_path):
    raw = RawStore(tmp_path)
    d1, new1 = raw.put("s", {"a": 1})
    d2, new2 = raw.put("s", {"a": 1})
    d3, new3 = raw.put("s", {"a": 2})
    assert d1 == d2 and new1 is True and new2 is False
    assert d3 != d1 and new3 is True
    assert raw.count("s") == 2
    assert raw.get("s", d1)["payload"] == {"a": 1}


def test_rawstore_roundtrips_unicode(tmp_path):
    raw = RawStore(tmp_path)
    payload = {"fr": "accessibilité des apprenants"}
    digest, _ = raw.put("canada_gc", payload)
    assert raw.get("canada_gc", digest)["payload"] == payload
