"""Score an organization against the opportunity corpus.

Two stages, kept strictly separate, as in the project plan.

Stage A, hard gates. Binary and auditable: geography, applicant type, whether
the deadline has passed. A failed gate means the grant does not appear as a
match, however attractive it looks otherwise. A gate the data cannot answer is
recorded as unknown, never silently passed.

Stage B, weighted components, each carrying its own plain-English explanation.
The score exists to justify an organization spending days on an application,
so a number nobody can interrogate is worse than no number.

The important rule here: a component we cannot compute is dropped and the
remaining weights are renormalized. It is never scored as zero. Treating
"we have no funder precedent data" as "this funder has never funded anyone
like you" would be a fabrication dressed up as arithmetic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import Opportunity, Org, days_until
from .topics import TopicIndex, similarity

# Which corpus region strings a country can apply to. "International" is open
# to anyone, so it satisfies any country.
COUNTRY_REGIONS = {
    "CA": {"Canada", "International"},
    "US": {"United States", "International"},
}

WEIGHTS = {
    "topic": 0.35,
    "award_size": 0.20,
    "precedent": 0.25,
    "operational": 0.15,
    "timing": 0.05,
}


@dataclass
class Component:
    name: str
    weight: float
    score: float | None          # None means not computable from current data
    explanation: str

    @property
    def known(self) -> bool:
        return self.score is not None


# Below this topical score, a grant has nothing to do with what the applicant
# does. Weighting alone did not express that: a dairy farm investment fund
# with a tidy award size and a high-volume funder outranked a literacy
# programme for a literacy charity, because four generic components scoring
# 1.00 outvoted one relevant component scoring 0.00.
RELEVANCE_FLOOR = 0.10


@dataclass
class MatchResult:
    opportunity: Opportunity
    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    unknown: list[tuple[str, str]] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    rating: float = 0.0
    # Eligible, but unrelated to the applicant's work. Kept and shown rather
    # than hidden, because "you could apply but it is not your field" is
    # useful, and ranked beneath everything relevant.
    off_topic: bool = False

    @property
    def eligible(self) -> bool:
        return not self.failed

    @property
    def coverage(self) -> float:
        """Share of total weight we could actually compute."""
        total = sum(c.weight for c in self.components)
        known = sum(c.weight for c in self.components if c.known)
        return known / total if total else 0.0


def _gates(org: Org, opp: Opportunity) -> tuple[list, list, list]:
    passed, failed, unknown = [], [], []

    allowed = COUNTRY_REGIONS.get(org.country, set())
    if not opp.regions:
        unknown.append(("Geography", "the funder does not state a region"))
    elif allowed & set(opp.regions):
        passed.append("Geography")
    else:
        failed.append(("Geography", f"open to {', '.join(opp.regions)}, "
                                    f"not {org.country}"))

    if not opp.applicant_types or opp.applicant_types == ["Other"]:
        unknown.append(("Applicant type", "eligibility not resolved to a category"))
    elif org.applicant_type in opp.applicant_types:
        passed.append("Applicant type")
    else:
        failed.append(("Applicant type",
                       f"open to {', '.join(opp.applicant_types).lower()}, "
                       f"not {org.applicant_type.lower()}"))

    if not opp.deadline:
        unknown.append(("Deadline", "no deadline published"))
    elif (days_until(opp.deadline) or 0) >= 0:
        passed.append("Deadline")
    else:
        failed.append(("Deadline", f"closed on {opp.deadline}"))

    return passed, failed, unknown


def _org_text(org: Org) -> str:
    # The name is included deliberately. Neither public registry publishes
    # mission text, so for a profile pulled from one the legal name is the
    # only topical signal there is, and charity names are usually descriptive
    # ("Literacy North Halton"). Without it a real registry profile matched
    # on budget and funder volume alone.
    return " ".join([org.name, " ".join(org.categories), org.mission,
                     org.beneficiaries])


def _grant_text(opp: Opportunity) -> str:
    return " ".join([opp.title, opp.summary, " ".join(opp.purposes)])


def _topic(org: Org, opp: Opportunity, index: TopicIndex) -> Component:
    w = WEIGHTS["topic"]
    org_text = _org_text(org).strip()
    grant_text = _grant_text(opp).strip()

    if not org_text:
        return Component("topic", w, None,
                         "your profile describes no program areas to compare")
    # Having text and matching nothing is a real answer, not missing data.
    # Returning unknown here let an unrelated fund rank as highly as a
    # tutoring program, because unknown components are dropped, not penalized.
    if not grant_text:
        return Component("topic", w, None,
                         "the funder publishes no description to compare")

    score, why = similarity(index, org_text, grant_text)
    return Component("topic", w, score, why)


def _award_size(org: Org, opp: Opportunity) -> Component:
    w = WEIGHTS["award_size"]
    if org.revenue is None:
        return Component("award_size", w, None,
                         "your annual revenue is not on file")
    if opp.amount_max is None and opp.amount_min is None:
        return Component("award_size", w, None,
                         "the funder does not state an award range")

    ask = opp.amount_max if opp.amount_min is None else (
        (opp.amount_min + opp.amount_max) / 2 if opp.amount_max else opp.amount_min)
    ratio = ask / org.revenue

    # A request far above a quarter of annual revenue rarely reads as credible
    # to a funder; one far below is not worth the application effort.
    if 0.05 <= ratio <= 0.25:
        return Component("award_size", w, 1.0,
                         f"a typical award is about {ratio:.0%} of your annual "
                         f"revenue, a credible ask")
    if ratio < 0.05:
        return Component("award_size", w, 0.35,
                         f"a typical award is only {ratio:.0%} of your revenue; "
                         f"may not repay the application effort")
    if ratio <= 1.0:
        return Component("award_size", w, 0.5,
                         f"a typical award is {ratio:.0%} of your annual revenue, "
                         f"a stretch for an organization your size")
    return Component("award_size", w, 0.15,
                     f"a typical award is {ratio:.0f}x your annual revenue; "
                     f"funders rarely grant beyond an applicant's capacity")


def _precedent(org: Org, opp: Opportunity, award_index: dict | None) -> Component:
    w = WEIGHTS["precedent"]
    if not award_index:
        return Component("precedent", w, None,
                         "no award history on file for this funder")
    count = award_index.get(opp.funder_name.lower())
    if not count:
        return Component("precedent", w, None,
                         "no award history on file for this funder")
    # Volume only. The real precedent signal compares the applicant with this
    # funder's past grantees, which needs grantee records resolved against a
    # registry (Phase 5). Saying so beats implying a similarity we have not
    # computed.
    score = 0.4 if count < 10 else 0.7 if count < 100 else 1.0
    return Component("precedent", w, score,
                     f"{count} awards on record from this funder; grant volume "
                     f"only, not a comparison with past grantees")


def _operational(org: Org, opp: Opportunity) -> Component:
    w = WEIGHTS["operational"]
    notes, score = [], 1.0
    known = False

    if opp.match_required is not None:
        known = True
        if opp.match_required:
            if org.budget_band == "small":
                score -= 0.5
                notes.append("matching funds required, demanding for your budget")
            else:
                score -= 0.2
                notes.append("matching funds required")
        else:
            notes.append("no matching funds required")

    if opp.partner_required is not None:
        known = True
        if opp.partner_required:
            score -= 0.25
            notes.append("a formal partner is required")
        else:
            notes.append("no partner required")

    if not known:
        return Component("operational", w, None,
                         "the funder states neither match nor partner rules")
    return Component("operational", w, max(0.0, score), "; ".join(notes))


def _timing(opp: Opportunity) -> Component:
    w = WEIGHTS["timing"]
    left = days_until(opp.deadline) if opp.deadline else None
    if left is None:
        return Component("timing", w, None, "no deadline published")
    if left >= 60:
        return Component("timing", w, 1.0, f"{left} days to prepare")
    if left >= 30:
        return Component("timing", w, 0.7, f"{left} days left, tight but workable")
    return Component("timing", w, 0.3,
                     f"only {left} days left, very tight for a full application")


def score_match(org: Org, opp: Opportunity,
                award_index: dict | None = None,
                index: TopicIndex | None = None) -> MatchResult:
    # Fitting an index on one document is meaningless, but it keeps
    # score_match usable on its own in tests and ad-hoc checks.
    index = index or TopicIndex([_grant_text(opp), _org_text(org)])
    passed, failed, unknown = _gates(org, opp)
    components = [
        _topic(org, opp, index),
        _award_size(org, opp),
        _precedent(org, opp, award_index),
        _operational(org, opp),
        _timing(opp),
    ]

    # Renormalize over what we could actually compute. An unknown component
    # must not drag the score down as though it were a zero.
    usable = [c for c in components if c.known]
    total_weight = sum(c.weight for c in usable)
    raw = (sum(c.score * c.weight for c in usable) / total_weight
           if total_weight else 0.0)

    result = MatchResult(opp, passed, failed, unknown, components)

    # Banded the same way the results page bands, so the number never
    # contradicts the ordering: ineligible can never outrank eligible.
    #
    # Headroom inside a band scales with coverage. A grant scoring well on
    # half the intended weighting cannot reach the top of its band, because
    # we have not established the other half. Without this a record with no
    # award range and no funder history reads 9.9/10, which claims a
    # certainty the data does not support.
    topic = next((c for c in components if c.name == "topic"), None)
    result.off_topic = bool(topic and topic.known and topic.score < RELEVANCE_FLOOR)

    coverage = result.coverage
    if failed:
        base, span = 1.0, 2.9
    elif unknown:
        base, span = 4.0, 2.9
    else:
        base, span = 7.0, 2.9

    # An off-topic grant is held near the floor of its band. Relevance is not
    # one consideration among five; a grant unrelated to the applicant's work
    # is not a match however well it fits on every other axis.
    modifier = 0.2 if result.off_topic else 1.0
    result.rating = round(base + raw * span * coverage * modifier, 1)
    return result


def rank(org: Org, opportunities: list[Opportunity],
         award_index: dict | None = None,
         index: TopicIndex | None = None) -> list[MatchResult]:
    # Inverse document frequency is a property of the corpus, so the index is
    # fitted once over every opportunity rather than per comparison.
    index = index or TopicIndex([_grant_text(o) for o in opportunities])
    results = [score_match(org, o, award_index, index) for o in opportunities]
    # Relevance outranks everything except a failed hard gate.
    results.sort(key=lambda r: (len(r.failed), r.off_topic,
                                len(r.unknown), -r.rating))
    return results
