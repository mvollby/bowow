"""Grants.gov adapter: US federal funding opportunities.

Two calls are needed, which is the main thing this adapter exists to hide:

  search2          cheap, paged, but returns only id/title/agency/dates
  fetchOpportunity everything we actually score on (eligibility prose,
                   award floor and ceiling, cost sharing, applicant types)

Both are public and keyless. Verified live against api.grants.gov.
"""
from __future__ import annotations

import time
from typing import Iterator

from ..models import (Opportunity, parse_amount, parse_date, strip_html)
from . import SourceAdapter, http_json

SEARCH_URL = "https://api.grants.gov/v1/api/search2"
FETCH_URL = "https://api.grants.gov/v1/api/fetchOpportunity"
DETAIL_URL = "https://grants.gov/search-results-detail/{id}"

# Grants.gov applicant type codes mapped onto our controlled vocabulary.
# Several codes collapse onto one of ours, which is the point of normalizing:
# the matcher asks "can a nonprofit apply", not "is this code 12 or 13".
APPLICANT_CODE_MAP = {
    "00": "Government", "01": "Government", "02": "Government",
    "04": "Government", "05": "School", "06": "Higher education",
    "07": "Tribal", "08": "Government", "11": "Tribal",
    "12": "Nonprofit", "13": "Nonprofit", "20": "Higher education",
    "21": "Individual", "22": "For-profit", "23": "For-profit",
    "25": "Other", "99": "Other",
}

# Fallback when a code is unknown: keyword match on the description text.
_KEYWORD_MAP = (
    ("nonprofit", "Nonprofit"), ("higher education", "Higher education"),
    ("school district", "School"), ("individual", "Individual"),
    ("tribal", "Tribal"), ("small business", "For-profit"),
    ("for-profit", "For-profit"), ("government", "Government"),
)

# fundingActivityCategories -> our purpose vocabulary.
PURPOSE_MAP = {
    "ED": "Program delivery", "ST": "Research", "HL": "Program delivery",
    "CD": "Program delivery", "ISS": "Program delivery", "O": "Other",
}


def _map_applicant_types(entries: list[dict]) -> tuple[list[str], float]:
    """Return (types, confidence). Unmapped codes lower confidence honestly."""
    mapped: list[str] = []
    unknown = 0
    for entry in entries or []:
        code = str(entry.get("id", "")).strip()
        desc = (entry.get("description") or "").lower()
        if code in APPLICANT_CODE_MAP:
            mapped.append(APPLICANT_CODE_MAP[code])
            continue
        hit = next((v for k, v in _KEYWORD_MAP if k in desc), None)
        if hit:
            mapped.append(hit)
        else:
            unknown += 1
    # "Others (see text field...)" is Grants.gov telling us the real rule is in
    # prose. That is a genuine unknown, so say so rather than guessing.
    confidence = 1.0
    if not mapped:
        confidence = 0.3
    elif unknown or mapped == ["Other"]:
        confidence = 0.6
    return sorted(set(mapped)), confidence


def _agency_name(raw: dict, syn: dict) -> str:
    """Pick the funding agency, avoiding a trap in the API.

    `synopsis.agencyName` is not reliably an agency. On some records it holds
    the program officer's name and title, identical to `agencyContactName`
    (observed: "Joseph  Perez\\nProgram Analyst" on an AFRI opportunity). The
    funder is shown on every result card, so attributing a federal program to
    a named individual is both wrong and embarrassing.

    `agencyDetails.agencyName` carries the operating agency and is preferred.
    The parent department is the fallback, and the raw synopsis field is used
    only when it is clearly not just the contact person.
    """
    contact = (syn.get("agencyContactName") or "").strip()
    candidate = (syn.get("agencyName") or "").strip()
    options = [
        (raw.get("agencyDetails") or {}).get("agencyName"),
        (raw.get("topAgencyDetails") or {}).get("agencyName"),
        candidate if candidate and candidate != contact else "",
        raw.get("owningAgencyCode"),
    ]
    for option in options:
        cleaned = " ".join((option or "").split())
        if cleaned:
            return cleaned
    return "Unknown agency"


class GrantsGovAdapter(SourceAdapter):
    name = "grants_gov"
    record_type = "opportunity"

    def __init__(self, keyword: str = "education", statuses: str = "posted",
                 delay: float = 0.4):
        self.keyword = keyword
        self.statuses = statuses
        self.delay = delay      # polite pacing between detail calls

    def fetch(self, limit: int = 50) -> Iterator[dict]:
        start = 0
        page = min(limit, 25)
        seen = 0
        while seen < limit:
            result = http_json(SEARCH_URL, {
                "keyword": self.keyword,
                "oppStatuses": self.statuses,
                "rows": page,
                "startRecordNum": start,
            })
            if result.get("errorcode") not in (0, None):
                raise RuntimeError(f"search2 error: {result.get('msg')}")
            hits = (result.get("data") or {}).get("oppHits") or []
            if not hits:
                return
            for hit in hits:
                if seen >= limit:
                    return
                detail = http_json(FETCH_URL, {"opportunityId": int(hit["id"])})
                payload = detail.get("data") or {}
                if payload:
                    payload["_searchHit"] = hit
                    yield payload
                    seen += 1
                time.sleep(self.delay)
            start += page

    def parse(self, raw: dict) -> Opportunity | None:
        syn = raw.get("synopsis") or {}
        native_id = str(raw.get("id") or syn.get("opportunityId") or "").strip()
        title = (raw.get("opportunityTitle") or "").strip()
        if not native_id or not title:
            return None

        applicant_types, confidence = _map_applicant_types(syn.get("applicantTypes"))
        eligibility = (syn.get("applicantEligibilityDesc") or "").strip()

        agency = _agency_name(raw, syn)

        purposes = sorted({
            PURPOSE_MAP.get(str(c.get("id")), "Other")
            for c in (syn.get("fundingActivityCategories") or [])
        })

        cost_sharing = syn.get("costSharing")
        source_url = DETAIL_URL.format(id=native_id)

        # Provenance for every field a user could question. Snippets are
        # truncated because this is for citation, not storage of the whole page.
        provenance = {
            "eligibility_raw": {"url": source_url, "snippet": eligibility[:300]},
            "amount_max": {"url": source_url,
                           "snippet": str(syn.get("awardCeilingFormatted") or "")},
            "deadline": {"url": source_url,
                         "snippet": str(syn.get("responseDate") or "")},
        }

        return Opportunity(
            source=self.name,
            native_id=native_id,
            title=title,
            funder_name=agency.strip(),
            summary=strip_html(syn.get("synopsisDesc") or "")[:2000],
            eligibility_raw=eligibility,
            applicant_types=applicant_types,
            regions=["United States"],
            purposes=purposes,
            amount_min=parse_amount(syn.get("awardFloor")),
            amount_max=parse_amount(syn.get("awardCeiling")),
            currency="USD",
            deadline=parse_date(syn.get("responseDate")),
            route="Open call",
            match_required=None if cost_sharing is None else bool(cost_sharing),
            partner_required=None,          # not expressed in the API; needs NLP
            source_url=source_url,
            field_provenance=provenance,
            extraction_confidence=confidence,
        )
