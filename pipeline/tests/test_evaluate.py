"""Eval harness tests.

The behaviour most worth protecting is the harness refusing to dress seed
labels up as a quality measurement.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bowow_pipeline.evaluate import (EXPERT_LABELERS, Pair, evaluate,
                                     labelling_sheet, load_pairs)
from bowow_pipeline.models import Opportunity, Org

PAIRS_FILE = Path(__file__).resolve().parent.parent / "eval" / "pairs.jsonl"


def _org(org_id="o1", **kw) -> Org:
    base = dict(org_id=org_id, name="Literacy Trust", country="CA",
                region="ON", applicant_type="Nonprofit", revenue=400_000,
                categories=["learning support"],
                mission="adult literacy and tutoring", synthetic=True)
    base.update(kw)
    return Org(**base)


def _opp(native_id, title, **kw) -> Opportunity:
    future = (date.today() + timedelta(days=90)).isoformat()
    base = dict(source="s", native_id=native_id, title=title,
                funder_name="Funder", summary=title,
                applicant_types=["Nonprofit"], regions=["Canada"],
                amount_min=20_000, amount_max=60_000, currency="CAD",
                deadline=future, match_required=False, partner_required=False)
    base.update(kw)
    return Opportunity(**base)


GOOD = _opp("1", "Adult literacy and tutoring programme")
BAD = _opp("2", "Dairy farm investment programme")
US_ONLY = _opp("3", "US literacy programme", regions=["United States"])
CORPUS = [GOOD, BAD, US_ONLY]
ORGS = {"o1": _org()}


# --- metrics ---------------------------------------------------------------

def test_precision_counts_only_labelled_items():
    pairs = [Pair("o1", GOOD.uid, "yes", "seed"),
             Pair("o1", BAD.uid, "no", "seed")]
    report = evaluate(pairs, ORGS, CORPUS, ks=(2,))
    assert report.precision_at[2] == pytest.approx(0.5)
    assert report.pairs_scored == 2


def test_coverage_reports_how_much_of_the_top_k_was_labelled():
    """Precision over two labelled items out of ten is a thin number and the
    report has to say so."""
    pairs = [Pair("o1", GOOD.uid, "yes", "seed")]
    report = evaluate(pairs, ORGS, CORPUS, ks=(3,))
    assert report.coverage_at[3] == pytest.approx(1 / 3)


def test_mrr_ignores_orgs_with_no_relevant_label():
    """An organization whose labels are all 'no' has no first relevant result
    to find; scoring it 0.0 would penalise the ranker for a question nobody
    asked."""
    orgs = {"o1": _org("o1"), "o2": _org("o2")}
    pairs = [Pair("o1", GOOD.uid, "yes", "seed"),
             Pair("o2", BAD.uid, "no", "seed")]
    assert evaluate(pairs, orgs, CORPUS).mrr == pytest.approx(1.0)


def test_missing_records_are_skipped_and_counted():
    """The corpus is rebuilt by ingestion, so labelled records can vanish."""
    pairs = [Pair("o1", "s:does-not-exist", "yes", "seed"),
             Pair("ghost-org", GOOD.uid, "yes", "seed")]
    report = evaluate(pairs, ORGS, CORPUS)
    assert report.pairs_scored == 0
    assert len(report.missing) == 2


# --- gate accuracy ---------------------------------------------------------

def test_gate_false_reject_is_reported_with_its_cause():
    pairs = [Pair("o1", US_ONLY.uid, "yes", "seed")]   # a gate will block it
    report = evaluate(pairs, ORGS, CORPUS)
    assert report.false_rejects and report.false_rejects[0][2] == "Geography"
    assert not report.false_accepts


def test_gate_false_accept_is_reported():
    pairs = [Pair("o1", GOOD.uid, "ineligible", "seed")]
    report = evaluate(pairs, ORGS, CORPUS)
    assert report.false_accepts == [("o1", GOOD.uid)]


# --- label provenance ------------------------------------------------------

def test_seed_labels_do_not_make_a_headline_trustworthy():
    pairs = [Pair("o1", GOOD.uid, "yes", "seed") for _ in range(200)]
    report = evaluate(pairs, ORGS, CORPUS)
    assert report.expert_pairs == 0
    assert report.headline_is_trustworthy is False


def test_enough_practitioner_labels_makes_it_trustworthy():
    pairs = ([Pair("o1", GOOD.uid, "yes", EXPERT_LABELERS[0])] * 50)
    report = evaluate(pairs, ORGS, CORPUS)
    assert report.expert_pairs == 50
    assert report.headline_is_trustworthy is True


def test_unknown_label_is_rejected_at_load():
    with pytest.raises(ValueError, match="unknown label"):
        Pair("o1", GOOD.uid, "probably", "seed")


# --- sheet -----------------------------------------------------------------

def test_sheet_samples_beyond_the_top_matches():
    """Labelling only what the system already likes measures agreement with
    itself, so the sheet mixes in candidates from further down."""
    corpus = [_opp(str(i), f"Programme {i}") for i in range(40)]
    rows = labelling_sheet(_org(), corpus, top=5, sample=4)
    ranks = [r["_rank"] for r in rows]
    assert len(rows) == 9
    assert max(ranks) > 5


def test_sheet_leaves_the_label_blank():
    rows = labelling_sheet(_org(), CORPUS, top=2, sample=0)
    assert all(r["label"] == "" and r["labeler"] == "" for r in rows)


# --- the shipped seed file -------------------------------------------------

def test_shipped_pairs_file_parses_and_is_honestly_labelled():
    pairs = load_pairs(PAIRS_FILE)
    assert pairs, "seed file is empty"
    assert all(p.labeler == "seed" for p in pairs), (
        "a pair claims a practitioner label; seed data must not be presented "
        "as expert judgement")
    assert all(p.note for p in pairs), "every seed label needs its reasoning"
