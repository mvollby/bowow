"""Topical similarity tests.

Two of these are regressions for failures observed on the live 755-record
corpus, and they are the reason this module replaced a six-pattern regex.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bowow_pipeline.topics import (CONCEPTS, STOPWORDS, TopicIndex,
                                   concepts_for, similarity, tokenize)

CORPUS = [
    "Adult Learning Literacy and Essential Skills Program supports tutoring and adult literacy",
    "Local Food Infrastructure Fund supports community food security projects",
    "Dairy Farm Investment Program for modernising dairy processing equipment",
    "Youth Employment and Skills Program work placements for young people",
    "Agricultural Clean Technology research into emissions reduction on farms",
    "Emergency Processing Fund for food processors to maintain operations",
]

LITERACY_ORG = ("learning support literacy after-school tutoring for children "
                "reading below grade level")


@pytest.fixture
def index():
    return TopicIndex(CORPUS)


# --- the two live failures this module was written to fix ------------------

def test_incidental_word_no_longer_fires_a_concept():
    """Regression. The observed failure: a newcomer literacy charity matched
    an agricultural Emergency Processing Fund at a confident 1.00, because its
    mission said it operated "from a single leased site" and the bare word
    "operating" matched an `operating costs` pattern."""
    mission = ("Language and workforce readiness classes for newly arrived "
               "families, operating from a single leased site.")
    assert "operating costs" not in concepts_for(mission)


def test_real_operating_cost_language_still_matches():
    """The fix must not simply delete the concept."""
    assert "operating costs" in concepts_for(
        "core funding toward operating costs and overhead")
    assert "operating costs" in concepts_for("support for organizational capacity")


def test_unrelated_programme_scores_near_zero(index):
    """Regression. An unrelated fund must not read as a match for a literacy
    charity, however well it fits on size or funder volume."""
    score, why = similarity(index, LITERACY_ORG, CORPUS[2])   # dairy farms
    assert score < 0.10
    assert "nothing" in why


def test_relevant_programme_scores_high(index):
    score, why = similarity(index, LITERACY_ORG, CORPUS[0])   # adult literacy
    assert score > 0.5
    assert "learning support" in why


def test_relevant_beats_unrelated_by_a_wide_margin(index):
    good, _ = similarity(index, LITERACY_ORG, CORPUS[0])
    bad, _ = similarity(index, LITERACY_ORG, CORPUS[2])
    assert good - bad > 0.4


# --- mechanics -------------------------------------------------------------

def test_concepts_match_phrases_not_substrings():
    """A concept phrase must be a whole phrase, not a fragment of a word."""
    assert concepts_for("the arts council") == {"arts culture"}
    assert "arts" not in " ".join(concepts_for("smarts and parts"))


def test_tokenizer_drops_corpus_boilerplate():
    tokens = tokenize("This funding program in Canada supports applicants")
    assert "fund" not in tokens and "canada" not in tokens
    assert "program" not in tokens


def test_stemmer_joins_related_forms():
    assert tokenize("tutoring")[0] == tokenize("tutor")[0]
    assert tokenize("libraries")[0] == tokenize("library")[0]


def test_idf_discounts_ubiquitous_terms():
    index = TopicIndex(["literacy tutoring", "literacy mentoring",
                        "literacy reading", "dairy farming"])
    # Derive the stems rather than hardcoding them; the stemmer is crude by
    # design and its exact output is not the thing under test here.
    common = tokenize("literacy")[0]
    rare = tokenize("dairy")[0]
    assert index.idf(common) < index.idf(rare)


def test_empty_text_is_handled(index):
    score, why = similarity(index, "", CORPUS[0])
    assert score == 0.0
    assert why


def test_cosine_is_symmetric(index):
    a, b = index.vector(CORPUS[0]), index.vector(CORPUS[3])
    assert index.cosine(a, b) == pytest.approx(index.cosine(b, a))


def test_explanation_names_the_shared_concept(index):
    _, why = similarity(index, LITERACY_ORG, CORPUS[0])
    assert "shares" in why and "wording in common" in why


def test_every_concept_uses_multiword_or_unambiguous_phrases():
    """Single-word concept triggers are what caused the false positive.
    Any one-word trigger must be a term that is unambiguous in this domain."""
    allowed_single = {
        "bursary", "bursaries", "scholarship", "tuition", "tutoring", "tutor",
        "mentoring", "mentorship", "literacy", "numeracy", "homework", "rural",
        "indigenous", "inuit", "metis", "aboriginal", "tribal", "newcomer",
        "immigrant", "refugee", "youth", "adolescent", "employment",
        "workforce", "apprenticeship", "overhead", "research", "fieldwork",
        "partnership", "consortium", "coalition", "infrastructure", "facility",
        "renovation", "arts", "culture", "heritage", "museum", "festival",
        "artistic", "health", "wellness", "wellbeing", "nutrition",
        "environment", "climate", "conservation", "biodiversity", "emissions",
    }
    for concept, phrases in CONCEPTS.items():
        for phrase in phrases:
            if " " not in phrase:
                assert phrase in allowed_single, (
                    f"{concept!r} fires on the bare word {phrase!r}; either "
                    f"make it a phrase or add it to the reviewed list")
