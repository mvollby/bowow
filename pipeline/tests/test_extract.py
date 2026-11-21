"""Extraction tests. No network and no spend: the Claude path uses a stub
client that returns canned JSON in the real response shape.

The behaviour these pin down is the part that matters for trust: a claim the
model cannot quote from the source never reaches the database.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bowow_pipeline.adapters.grants_gov import GrantsGovAdapter
from bowow_pipeline.extract import (ClaudeExtractor, Extraction,
                                    ExtractionCache, RuleExtractor,
                                    apply_extraction, extract_cached,
                                    get_extractor, verify_evidence)
from bowow_pipeline.models import Opportunity

SOURCE = (
    "Who May Submit Proposals: Proposals may only be submitted by nonprofit "
    "organizations with 501(c)(3) status and accredited institutions of higher "
    "education. Cost sharing is required for all awards. Applicants are "
    "encouraged, but not required, to collaborate with community partners."
)


def _stub_client(payload: dict, stop_reason: str = "end_turn",
                 usage=(100, 40)):
    """Mimic the SDK response shape: .content blocks, .stop_reason, .usage."""
    class _Messages:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text",
                                         text=json.dumps(payload))],
                stop_reason=stop_reason,
                usage=SimpleNamespace(input_tokens=usage[0],
                                      output_tokens=usage[1]),
            )
    return SimpleNamespace(messages=_Messages())


GOOD_PAYLOAD = {
    "applicant_types": ["Nonprofit", "Higher education"],
    "applicant_types_evidence": [
        "submitted by nonprofit organizations with 501(c)(3) status",
        "accredited institutions of higher education",
    ],
    "match_required": "yes",
    "match_required_evidence": "Cost sharing is required for all awards.",
    "partner_required": "no",
    "partner_required_evidence":
        "Applicants are encouraged, but not required, to collaborate with "
        "community partners.",
    "geographic_restriction": "",
    "geographic_evidence": "",
    "notes": "",
}


# --- evidence verification -------------------------------------------------

def test_verify_evidence_accepts_exact_quote():
    assert verify_evidence("Cost sharing is required for all awards.", SOURCE)


def test_verify_evidence_tolerates_whitespace_reflow():
    assert verify_evidence("Cost sharing is\n  required for all awards.", SOURCE)


def test_verify_evidence_rejects_invented_quote():
    assert not verify_evidence("Applicants must be based in Ontario.", SOURCE)


def test_verify_evidence_rejects_trivially_short_quote():
    """A two-word 'quote' matches almost anything and proves nothing."""
    assert not verify_evidence("required", SOURCE)


# --- grounding -------------------------------------------------------------

def test_grounded_fields_survive():
    ex = ClaudeExtractor()
    ex._client = _stub_client(GOOD_PAYLOAD)
    out = ex.extract(SOURCE)
    assert out.fields["applicant_types"] == ["Nonprofit", "Higher education"]
    assert out.fields["match_required"] == "yes"
    assert out.fields["partner_required"] == "no"
    assert out.rejected == []
    assert out.confidence == 1.0


def test_hallucinated_field_is_dropped_not_stored():
    """The guard that matters: an unquotable claim must not reach the record."""
    bad = dict(GOOD_PAYLOAD)
    bad["match_required"] = "yes"
    bad["match_required_evidence"] = "A 25 percent match of total project cost is mandatory."
    ex = ClaudeExtractor()
    ex._client = _stub_client(bad)
    out = ex.extract(SOURCE)

    assert "match_required" in out.rejected
    assert out.fields["match_required"] is None
    assert out.fields["applicant_types"] == ["Nonprofit", "Higher education"]
    assert out.confidence < 1.0        # grounding rate, not self-report


def test_unstated_is_not_an_assertion():
    payload = {k: v for k, v in GOOD_PAYLOAD.items()}
    payload["partner_required"] = "unstated"
    payload["partner_required_evidence"] = ""
    ex = ClaudeExtractor()
    ex._client = _stub_client(payload)
    out = ex.extract(SOURCE)
    assert out.fields["partner_required"] is None
    assert "partner_required" not in out.rejected   # silence is not a failure


def test_refusal_is_handled_without_raising():
    ex = ClaudeExtractor()
    ex._client = _stub_client(GOOD_PAYLOAD, stop_reason="refusal")
    out = ex.extract(SOURCE)
    assert out.confidence == 0.0
    assert "declined" in out.fields["notes"]


def test_list_field_may_cite_several_spans():
    """The fix for the live run: 12 of 15 dropped claims were applicant_types,
    because eligible applicants are enumerated across bullets and no single
    contiguous span covers them."""
    source = ("Eligible applicants:\n"
              "- nonprofit organizations with 501(c)(3) status\n"
              "- accredited institutions of higher education\n"
              "Cost sharing is required for all awards.")
    payload = dict(GOOD_PAYLOAD)
    payload["applicant_types_evidence"] = [
        "nonprofit organizations with 501(c)(3) status",
        "accredited institutions of higher education",
    ]
    payload["match_required_evidence"] = "Cost sharing is required for all awards."
    payload["partner_required"] = "unstated"
    payload["partner_required_evidence"] = ""

    ex = ClaudeExtractor()
    ex._client = _stub_client(payload)
    out = ex.extract(source)

    assert out.fields["applicant_types"] == ["Nonprofit", "Higher education"]
    assert out.rejected == []


def test_one_invented_span_still_discards_the_whole_field():
    """Allowing several spans must not weaken the no-fabrication guarantee."""
    payload = dict(GOOD_PAYLOAD)
    payload["applicant_types_evidence"] = [
        "submitted by nonprofit organizations with 501(c)(3) status",
        "and by municipal governments of any size",        # not in SOURCE
    ]
    ex = ClaudeExtractor()
    ex._client = _stub_client(payload)
    out = ex.extract(SOURCE)

    assert "applicant_types" in out.rejected
    assert out.fields["applicant_types"] == []


def test_rejected_quotes_are_kept_for_review():
    """A dropped claim has to be inspectable, or the threshold cannot be judged."""
    bad = dict(GOOD_PAYLOAD)
    bad["match_required_evidence"] = "A 25 percent match of total project cost is mandatory."
    ex = ClaudeExtractor()
    ex._client = _stub_client(bad)
    out = ex.extract(SOURCE)

    assert out.rejected_quotes["match_required"] == [
        "A 25 percent match of total project cost is mandatory."]


def test_missing_evidence_is_rejected_not_accepted():
    payload = dict(GOOD_PAYLOAD)
    payload["match_required_evidence"] = ""
    ex = ClaudeExtractor()
    ex._client = _stub_client(payload)
    out = ex.extract(SOURCE)
    assert "match_required" in out.rejected
    assert out.rejected_quotes["match_required"] == ["(no evidence supplied)"]


def test_schema_declares_evidence_list_for_applicant_types():
    schema = ClaudeExtractor().build_request(SOURCE)["output_config"]["format"]["schema"]
    assert schema["properties"]["applicant_types_evidence"]["type"] == "array"
    assert schema["properties"]["match_required_evidence"]["type"] == "string"


def test_requeue_finds_only_previously_rejected_texts(tmp_path):
    cache = ExtractionCache(tmp_path)
    ex = ClaudeExtractor()
    ex._client = _stub_client({**GOOD_PAYLOAD,
                               "match_required_evidence": "an invented sentence entirely"})
    extract_cached(SOURCE, ex, cache)

    assert cache.was_rejected(SOURCE, ex.model) is True
    assert cache.was_rejected("some other eligibility text", ex.model) is False


def test_request_uses_strict_schema_and_system_prompt():
    ex = ClaudeExtractor()
    body = ex.build_request(SOURCE)
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert set(fmt["schema"]["required"]) == set(fmt["schema"]["properties"])
    assert "unstated" in body["system"]
    assert body["model"].startswith("claude-")


# --- merge policy ----------------------------------------------------------

def _opportunity(**kw) -> Opportunity:
    base = dict(source="grants_gov", native_id="1", title="Test grant",
                funder_name="Agency", eligibility_raw=SOURCE,
                source_url="https://example.gov/x", extraction_confidence=0.6)
    base.update(kw)
    return Opportunity(**base)


def test_extraction_fills_the_empty_other_slot():
    opp = _opportunity(applicant_types=["Other"])
    ex = ClaudeExtractor()
    ex._client = _stub_client(GOOD_PAYLOAD)
    changed = apply_extraction(opp, ex.extract(SOURCE))

    assert "applicant_types" in changed
    assert opp.applicant_types == ["Higher education", "Nonprofit"]
    assert opp.match_required is True
    assert opp.partner_required is False


def test_extraction_never_overwrites_a_confident_api_value():
    """The API stating Nonprofit explicitly beats anything read out of prose."""
    opp = _opportunity(applicant_types=["Nonprofit"], match_required=False)
    ex = ClaudeExtractor()
    ex._client = _stub_client(GOOD_PAYLOAD)
    changed = apply_extraction(opp, ex.extract(SOURCE))

    assert "applicant_types" not in changed
    assert opp.applicant_types == ["Nonprofit"]
    assert opp.match_required is False      # not flipped to True
    assert "partner_required" in changed    # the genuinely empty slot is filled


def test_provenance_records_quote_and_method():
    opp = _opportunity(applicant_types=["Other"])
    ex = ClaudeExtractor()
    ex._client = _stub_client(GOOD_PAYLOAD)
    apply_extraction(opp, ex.extract(SOURCE))
    prov = opp.field_provenance["match_required"]
    assert prov["snippet"].startswith("Cost sharing is required")
    assert prov["method"].startswith("claude:")
    assert prov["url"] == "https://example.gov/x"


def test_confidence_only_rises():
    opp = _opportunity(applicant_types=["Other"], extraction_confidence=0.9)
    weak = Extraction(fields={"applicant_types": []}, confidence=0.1)
    apply_extraction(opp, weak)
    assert opp.extraction_confidence == 0.9


# --- rule fallback ---------------------------------------------------------

def test_rule_extractor_finds_literal_phrases():
    out = RuleExtractor().extract(SOURCE)
    assert "Nonprofit" in out.fields["applicant_types"]
    assert "Higher education" in out.fields["applicant_types"]
    assert out.fields["match_required"] == "yes"


def test_rule_extractor_stays_below_review_threshold():
    """Keyword matching should always leave a record flagged for review."""
    out = RuleExtractor().extract(SOURCE)
    assert out.confidence <= 0.6


def test_rule_extractor_reads_negation_correctly():
    text = "No matching funds are required and partners are not required."
    out = RuleExtractor().extract(text)
    assert out.fields["match_required"] == "no"
    assert out.fields["partner_required"] == "no"


def test_backend_selection_without_credential(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert get_extractor("auto").backend == "rules"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert get_extractor("auto").backend == "claude"


# --- caching ---------------------------------------------------------------

def test_cache_prevents_a_second_call(tmp_path):
    cache = ExtractionCache(tmp_path)
    ex = ClaudeExtractor()
    ex._client = _stub_client(GOOD_PAYLOAD)

    first = extract_cached(SOURCE, ex, cache)
    second = extract_cached(SOURCE, ex, cache)

    assert first.cached is False and second.cached is True
    assert len(ex._client.messages.calls) == 1          # billed exactly once
    assert second.fields == first.fields


def test_cache_key_changes_with_model(tmp_path):
    cache = ExtractionCache(tmp_path)
    assert cache.key(SOURCE, "claude-opus-5-5") != cache.key(SOURCE, "other")


def test_corrupt_cache_entry_is_ignored(tmp_path):
    cache = ExtractionCache(tmp_path)
    key = cache.key(SOURCE, "m")
    (tmp_path / f"{key}.json").write_text("{not json", encoding="utf-8")
    assert cache.get(key) is None


# --- end to end over a real adapter payload --------------------------------

def test_extraction_rescues_a_code_25_record():
    """The case that motivated this module: structured field says nothing."""
    payload = {
        "id": "334326",
        "opportunityTitle": "Example Program",
        "synopsis": {
            "agencyName": "Agency",
            "applicantEligibilityDesc": SOURCE,
            "applicantTypes": [{"id": "25", "description": "Others (see text field)"}],
        },
    }
    opp = GrantsGovAdapter().parse(payload)
    assert opp.applicant_types == ["Other"]        # useless before extraction

    ex = ClaudeExtractor()
    ex._client = _stub_client(GOOD_PAYLOAD)
    apply_extraction(opp, ex.extract(opp.eligibility_raw))

    assert opp.applicant_types == ["Higher education", "Nonprofit"]
    assert opp.extraction_confidence >= 0.7        # no longer flagged
