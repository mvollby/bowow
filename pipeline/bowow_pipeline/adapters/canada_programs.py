"""Canadian funding programs, derived from award history.

Canada publishes no equivalent of the Grants.gov opportunity API. What it does
publish is every grant and contribution already made, and each of those rows
names the program that funded it. Aggregating by program yields a catalogue of
real Canadian funding programs with real departments, real award sizes and
real evidence of who gets funded.

What this is NOT, and the distinction has to survive all the way to the UI:

* These are not open calls. There is no posted deadline and no guarantee the
  program is accepting applications today. `route` says so explicitly and
  `deadline` stays null, which puts them in the "could not verify" band rather
  than presenting them as something to apply to this week.
* `applicant_types` here records who has ACTUALLY BEEN FUNDED, not a published
  eligibility rule. A program open to nonprofits that only universities have
  applied to will show Academia. That is evidence, not a rule, and the
  provenance records the difference.

Award amounts come from the 10th and 90th percentile of real agreements rather
than the extremes, so one atypical megagrant does not define a program's range.

Recipient type codes are from the publisher's own schema, not inferred:
https://open.canada.ca/data/recombinant-published-schema/grants.json
"""
from __future__ import annotations

import collections
import re
from typing import Iterator

from ..models import Opportunity, slug
from . import SourceAdapter
from .canada_gc import CSV_URL, SEARCH_URL, CanadaGrantsAdapter

# Published schema, verified against the live file. Guessing these would be
# wrong in ways that matter: S is Academia, not School; P is Individual.
RECIPIENT_TYPE_MAP = {
    "A": "Tribal",            # Indigenous recipients
    "F": "For-profit",        # For-profit organizations
    "G": "Government",        # Government
    "I": "Other",             # International (non-government)
    "N": "Nonprofit",         # Not-for-profit organizations and charities
    "O": "Other",             # Other
    "P": "Individual",        # Individual or sole proprietorships
    "S": "Higher education",  # Academia
}

# A program with only a couple of awards tells us too little to publish a
# range or an applicant profile from.
MIN_AWARDS = 3

ROUTE = "Program (no posted call)"


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return ordered[idx]


def award_range(values: list[float]) -> tuple[int | None, int | None, str]:
    """A representative award range, plus a note on how it was derived.

    Percentiles only exclude outliers when there is enough data to have a
    tail. On five awards the 90th percentile IS the largest award, so a single
    atypical megagrant would define the programme's ceiling and every small
    charity would see an implausible number. The method therefore follows the
    sample size, and the note travels with the record so the figure is never
    more authoritative than its basis.
    """
    clean = sorted(v for v in values if v and v > 0)
    if not clean:
        return None, None, "no usable agreement values"
    if len(clean) >= 10:
        return (int(_percentile(clean, 0.10)), int(_percentile(clean, 0.90)),
                f"10th-90th percentile of {len(clean)} agreements")
    if len(clean) >= 5:
        trimmed = clean[1:-1]      # drop the single largest and smallest
        return (int(trimmed[0]), int(trimmed[-1]),
                f"range of {len(clean)} agreements, highest and lowest excluded")
    return (int(clean[0]), int(clean[-1]),
            f"full range of only {len(clean)} agreements; too few to be typical")


class CanadaProgramsAdapter(SourceAdapter):
    name = "canada_programs"
    record_type = "opportunity"

    def __init__(self, url: str = CSV_URL, local_path: str | None = None,
                 max_rows: int = 300_000, program_filter: str | None = None):
        self.url = url
        self.local_path = local_path
        # The published CSV is ordered by department, so a capped scan is a
        # biased sample. Callers get told which departments they actually got.
        self.max_rows = max_rows
        self.program_filter = (program_filter or "").lower()

    def fetch(self, limit: int = 200) -> Iterator[dict]:
        reader = CanadaGrantsAdapter(url=self.url, local_path=self.local_path)
        stream = reader._open_stream()
        programs: dict[tuple[str, str], dict] = {}
        scanned = 0

        try:
            import csv
            for raw_row in csv.DictReader(stream):
                if self.max_rows and scanned >= self.max_rows:
                    break
                scanned += 1
                row = reader._clean_keys(raw_row)

                program = (row.get("prog_name_en") or "").strip()
                dept = (row.get("owner_org_title") or "").split("|")[0].strip()
                if not program or not dept:
                    continue
                if self.program_filter and self.program_filter not in (
                        program + " " + (row.get("prog_purpose_en") or "")).lower():
                    continue

                key = (dept, program)
                entry = programs.setdefault(key, {
                    "department": dept, "program": program, "purpose": "",
                    "values": [], "recipient_types": collections.Counter(),
                    "provinces": collections.Counter(), "awards": 0,
                    "latest_start": "",
                })
                entry["awards"] += 1
                if not entry["purpose"]:
                    entry["purpose"] = (row.get("prog_purpose_en") or "").strip()
                try:
                    value = float((row.get("agreement_value") or "0").replace(",", ""))
                    if value > 0:
                        entry["values"].append(value)
                except ValueError:
                    pass
                code = (row.get("recipient_type") or "").strip().upper()
                if code:
                    entry["recipient_types"][code] += 1
                province = (row.get("recipient_province") or "").strip()
                if province:
                    entry["provinces"][province] += 1
                start = (row.get("agreement_start_date") or "").strip()
                if start > entry["latest_start"]:
                    entry["latest_start"] = start
        finally:
            stream.close()

        ranked = sorted(
            (e for e in programs.values() if e["awards"] >= MIN_AWARDS),
            key=lambda e: e["awards"], reverse=True)
        print(f"  scanned {scanned:,} award rows -> {len(programs):,} programs, "
              f"{len(ranked):,} with >= {MIN_AWARDS} awards")
        if self.max_rows and scanned >= self.max_rows:
            print(f"  NOTE: scan capped at {self.max_rows:,} rows and the file is "
                  f"ordered by department, so this is not a representative "
                  f"sample of all federal programs")

        for entry in ranked[:limit]:
            entry["recipient_types"] = dict(entry["recipient_types"])
            entry["provinces"] = dict(entry["provinces"].most_common(5))
            yield entry

    def parse(self, raw: dict) -> Opportunity | None:
        program = raw.get("program")
        dept = raw.get("department")
        if not program or not dept:
            return None
        # Observed live: one department files a programme literally named "0".
        # A name with no words cannot be shown to an applicant or matched on,
        # so it is dropped rather than published as an opportunity.
        if len(re.findall(r"[A-Za-z]", program)) < 3:
            return None

        low, high, range_note = award_range(raw.get("values") or [])

        counts = raw.get("recipient_types") or {}
        types = sorted({RECIPIENT_TYPE_MAP[c] for c in counts
                        if c in RECIPIENT_TYPE_MAP})

        awards = raw.get("awards", 0)
        funded = ", ".join(f"{RECIPIENT_TYPE_MAP.get(c, c)} ({n})"
                           for c, n in sorted(counts.items(),
                                              key=lambda kv: -kv[1])[:4])

        provenance = {
            "applicant_types": {
                "url": SEARCH_URL,
                # State plainly what this field is derived from. Observed
                # grantees are evidence of eligibility, not a rule.
                "snippet": f"derived from {awards} past awards; reflects who has "
                           f"been funded ({funded}), not a published eligibility "
                           f"rule",
                "method": "canada_programs:aggregate",
            },
            "amount_min": {
                "url": SEARCH_URL,
                "snippet": range_note,
                "method": "canada_programs:aggregate",
            },
        }

        summary = raw.get("purpose") or ""
        if awards:
            summary = (f"{summary}\n\nFunded {awards} agreements in the sampled "
                       f"data, most recently starting {raw.get('latest_start') or 'unknown'}.").strip()

        return Opportunity(
            source=self.name,
            native_id=f"{slug(dept)[:40]}--{slug(program)[:60]}",
            title=program,
            funder_name=dept,
            summary=summary[:2000],
            eligibility_raw="",          # nothing published to extract from
            applicant_types=types,
            regions=["Canada"],
            purposes=[],
            amount_min=low,
            amount_max=high,
            currency="CAD",
            deadline=None,               # not an open call; no posted deadline
            route=ROUTE,
            match_required=None,
            partner_required=None,
            source_url=SEARCH_URL,
            field_provenance=provenance,
            # Observed grantees are strong evidence, but they are not an
            # eligibility statement, and no amount of extraction can turn
            # them into one. Held below a confident reading on purpose.
            extraction_confidence=0.75,
        )
