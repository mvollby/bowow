"""Measure match quality against labelled (organization, opportunity) pairs.

Every scoring decision in this project so far was judged by a human reading
output and deciding it looked better. That is enough to catch gross breakage
and not enough to tell whether a change helps on average. This module makes
the difference measurable.

The rule that shapes the design: **labels are evidence, and their provenance
travels with them.** A pair labelled by a grant writer and a pair labelled
because the answer is blindingly obvious are not the same kind of evidence,
and a pair labelled by the system being measured is not evidence at all. Each
label carries a `labeler`, metrics are reported per provenance, and the
harness refuses to present a headline figure drawn only from seed labels.

Labels
    yes        a grant writer would put this on the shortlist
    maybe      plausible, worth a look, not a clear shortlist item
    no         eligible but not worth the application effort
    ineligible the organization cannot apply at all

`yes` is the relevant class for precision and recall. `ineligible` is scored
separately, against the hard gates, because a gate that wrongly rejects is a
different failure from a score that ranks badly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .match import rank
from .models import Org, Opportunity

LABELS = ("yes", "maybe", "no", "ineligible")
RELEVANT = ("yes",)

# Labels the system may rely on for a headline number. "seed" means the pair
# was labelled because the answer is uncontroversial, not by a practitioner;
# useful as a regression tripwire, not as evidence of quality.
EXPERT_LABELERS = ("expert", "grant-writer")


@dataclass
class Pair:
    org_id: str
    opportunity_uid: str
    label: str
    labeler: str
    note: str = ""

    def __post_init__(self):
        if self.label not in LABELS:
            raise ValueError(f"unknown label {self.label!r}; use one of {LABELS}")


@dataclass
class Report:
    org_count: int = 0
    pairs_total: int = 0
    pairs_scored: int = 0
    missing: list[str] = field(default_factory=list)
    precision_at: dict[int, float] = field(default_factory=dict)
    recall_at: dict[int, float] = field(default_factory=dict)
    coverage_at: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0
    false_accepts: list[tuple[str, str]] = field(default_factory=list)
    false_rejects: list[tuple[str, str, str]] = field(default_factory=list)
    by_labeler: dict[str, int] = field(default_factory=dict)

    @property
    def expert_pairs(self) -> int:
        return sum(n for who, n in self.by_labeler.items()
                   if who in EXPERT_LABELERS)

    @property
    def headline_is_trustworthy(self) -> bool:
        """A number drawn only from obvious cases is not a quality measure."""
        return self.expert_pairs >= 50


def load_pairs(path: str | Path) -> list[Pair]:
    pairs = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pairs.append(Pair(**json.loads(line)))
    return pairs


def evaluate(pairs: list[Pair], orgs: dict[str, Org],
             opportunities: list[Opportunity],
             award_index: dict | None = None,
             ks: tuple[int, ...] = (5, 10)) -> Report:
    report = Report(pairs_total=len(pairs))
    known_uids = {o.uid for o in opportunities}

    by_org: dict[str, list[Pair]] = {}
    for pair in pairs:
        report.by_labeler[pair.labeler] = report.by_labeler.get(pair.labeler, 0) + 1
        if pair.org_id not in orgs:
            report.missing.append(f"org {pair.org_id}")
            continue
        if pair.opportunity_uid not in known_uids:
            # The corpus is rebuilt by ingestion, so a labelled opportunity
            # may be absent locally. Skipped and counted, never guessed at.
            report.missing.append(f"opportunity {pair.opportunity_uid}")
            continue
        by_org.setdefault(pair.org_id, []).append(pair)
        report.pairs_scored += 1

    report.org_count = len(by_org)
    hits_at = {k: 0.0 for k in ks}
    shown_at = {k: 0 for k in ks}
    labelled_at = {k: 0 for k in ks}
    recall_num = {k: 0.0 for k in ks}
    recall_den = 0.0
    reciprocal = []

    for org_id, org_pairs in by_org.items():
        org = orgs[org_id]
        labels = {p.opportunity_uid: p for p in org_pairs}
        results = rank(org, opportunities, award_index)

        relevant = {uid for uid, p in labels.items() if p.label in RELEVANT}
        recall_den += len(relevant)

        ordered = [r.opportunity.uid for r in results]
        for k in ks:
            top = ordered[:k]
            labelled = [uid for uid in top if uid in labels]
            hits = [uid for uid in labelled if labels[uid].label in RELEVANT]
            hits_at[k] += len(hits)
            labelled_at[k] += len(labelled)
            shown_at[k] += len(top)
            recall_num[k] += len(set(top) & relevant)

        # Reciprocal rank is only defined where a relevant item exists. An
        # organization whose labels are all "no" or "ineligible" has no first
        # relevant result to find, and counting it as 0.0 would penalise the
        # ranker for a question nobody asked.
        if relevant:
            first = next((i for i, uid in enumerate(ordered, 1)
                          if uid in relevant), None)
            reciprocal.append(1.0 / first if first else 0.0)

        # Gate checks. A gate failure on a pair a practitioner called workable
        # is a false reject; passing every gate on a pair they called
        # ineligible is a false accept.
        for result in results:
            pair = labels.get(result.opportunity.uid)
            if pair is None:
                continue
            if pair.label == "ineligible" and result.eligible:
                report.false_accepts.append((org_id, pair.opportunity_uid))
            if pair.label in ("yes", "maybe") and not result.eligible:
                reason = result.failed[0][0] if result.failed else "?"
                report.false_rejects.append(
                    (org_id, pair.opportunity_uid, reason))

    for k in ks:
        report.precision_at[k] = (hits_at[k] / labelled_at[k]
                                  if labelled_at[k] else 0.0)
        report.coverage_at[k] = (labelled_at[k] / shown_at[k]
                                 if shown_at[k] else 0.0)
        report.recall_at[k] = recall_num[k] / recall_den if recall_den else 0.0
    report.mrr = sum(reciprocal) / len(reciprocal) if reciprocal else 0.0
    return report


def labelling_sheet(org: Org, opportunities: list[Opportunity],
                    award_index: dict | None = None,
                    top: int = 10, sample: int = 5,
                    seed: int = 7) -> list[dict]:
    """Rows for a human to label.

    Deliberately not just the current top matches. Labelling only what the
    system already likes measures agreement with itself and cannot discover a
    good grant ranked 200th, so a random sample from further down is mixed in.
    """
    import random

    results = rank(org, opportunities, award_index)
    eligible = [r for r in results if r.eligible]
    chosen = eligible[:top]
    rest = eligible[top:]
    if rest:
        chosen += random.Random(seed).sample(rest, min(sample, len(rest)))

    rows = []
    for position, result in enumerate(results, 1):
        if result not in chosen:
            continue
        opp = result.opportunity
        rows.append({
            "org_id": org.org_id,
            "opportunity_uid": opp.uid,
            "label": "",                       # for the labeller to fill in
            "labeler": "",
            "note": "",
            "_rank": position,
            "_score": result.rating,
            "_title": opp.title,
            "_funder": opp.funder_name,
            "_amount": opp.amount_max,
            "_url": opp.source_url,
        })
    return rows
