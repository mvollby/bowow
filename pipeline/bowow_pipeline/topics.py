"""Topical similarity between an organization and a funding programme.

Replaces a six-pattern regex vocabulary that failed in both directions on a
755-record corpus: 70% of Canadian programmes matched no tag at all, and the
tags that did fire could fire on an incidental word. The worst observed case
scored a newcomer literacy charity against an agricultural Emergency
Processing Fund at a confident 1.00, because the charity's mission said it
operated "from a single leased site" and the bare word "operating" matched an
`operating costs` pattern.

Two layers, each fixing one of those failures:

1. **Concepts** are matched on whole phrases, not bare words. "operating
   costs" and "core funding" are the concept; "operating" alone is not. This
   kills the false positives.

2. **TF-IDF cosine** over the whole corpus catches everything the lexicon
   never anticipated, and inverse document frequency automatically discounts
   the boilerplate ("program", "funding", "canada") that appears everywhere.
   This covers the 70% the lexicon misses.

This is lexical similarity, not embeddings. It does not know that "bursary"
and "scholarship" are the same idea unless the lexicon says so, which is
exactly why the lexicon exists. Real embeddings would be better and would need
either a third-party provider or a local model; neither is in this project
today. The interface here is the one an embedding backend would implement, so
swapping it later touches this file and nothing else.
"""
from __future__ import annotations

import math
import re
from collections import Counter

# Phrases, deliberately. A concept fires on a phrase a funder would actually
# write, never on a single ambiguous word that can appear incidentally.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "student aid": (
        "bursary", "bursaries", "scholarship", "tuition", "financial aid",
        "student aid", "student award", "entrance award", "student financial",
    ),
    "learning support": (
        "tutoring", "tutor", "mentoring", "mentorship", "literacy", "numeracy",
        "after school", "homework", "learning support", "educational support",
        "essential skills", "adult learning", "reading program",
    ),
    "rural access": (
        "rural", "remote community", "remote communities", "northern community",
        "northern communities", "underserved community", "isolated community",
    ),
    "indigenous": (
        "indigenous", "first nation", "first nations", "inuit", "metis",
        "aboriginal", "tribal",
    ),
    "newcomers": (
        "newcomer", "immigrant", "refugee", "settlement service",
        "language training", "official language training",
    ),
    "youth": ("youth", "young people", "adolescent", "young person"),
    "employment skills": (
        "employment", "workforce", "skills training", "job readiness",
        "apprenticeship", "labour market", "work placement",
    ),
    # Phrases only. The single word "operating" is what caused the false
    # positive this module was written to fix.
    "operating costs": (
        "operating cost", "operating costs", "core funding", "overhead",
        "operational funding", "administrative cost", "core operating",
        "organizational capacity",
    ),
    "research": ("research", "fieldwork", "scientific study", "clinical trial"),
    "partnerships": (
        "partnership", "consortium", "collaborative project", "coalition",
        "joint initiative",
    ),
    "infrastructure": (
        "infrastructure", "facility", "renovation", "capital project",
        "equipment purchase",
    ),
    "arts culture": (
        "arts", "culture", "heritage", "museum", "festival", "artistic",
    ),
    "health": ("health", "wellness", "mental health", "wellbeing"),
    "food security": ("food security", "nutrition", "food bank", "local food"),
    "environment": (
        "environment", "climate", "conservation", "biodiversity", "emissions",
    ),
}

# Words carrying no topical signal in this corpus. Without these, every
# programme looks similar to every other because they all say "funding".
STOPWORDS = frozenset("""
a an and are as at be been but by for from has have in is it its of on or that
the this to was were will with within who whom which your you our we their they
program programs programme programmes funding fund funds grant grants
contribution contributions project projects support supports supported
application applications applicant applicants eligible eligibility
canada canadian federal government department agency initiative initiatives
provide provides providing deliver delivery organization organizations
activity activities service services new other including include included
per cent year years million thousand up to under over
""".split())


def _stem(word: str) -> str:
    """Crude suffix stripping, enough to join tutor/tutoring and fund/funds."""
    for suffix, replacement in (("ies", "y"), ("ing", ""), ("ed", ""),
                                ("es", ""), ("s", "")):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)] + replacement
    return word


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z]+", (text or "").lower())
    return [_stem(w) for w in words if len(w) > 2 and w not in STOPWORDS]


def concepts_for(*texts: str) -> set[str]:
    """Concepts present in the text, matched on whole phrases."""
    blob = " " + re.sub(r"[^a-z]+", " ", " ".join(t or "" for t in texts).lower()) + " "
    found = set()
    for concept, phrases in CONCEPTS.items():
        for phrase in phrases:
            if f" {phrase} " in blob:
                found.add(concept)
                break
    return found


class TopicIndex:
    """Corpus-weighted similarity. Fit once over every opportunity."""

    def __init__(self, documents: list[str]):
        self.n = max(1, len(documents))
        counts: Counter[str] = Counter()
        for doc in documents:
            counts.update(set(tokenize(doc)))
        self.df = counts
        self._cache: dict[int, dict[str, float]] = {}

    def idf(self, term: str) -> float:
        # Smoothed, so a term appearing in every document contributes ~0.
        return math.log((self.n + 1) / (self.df.get(term, 0) + 1)) + 1.0

    def vector(self, text: str) -> dict[str, float]:
        tokens = tokenize(text)
        if not tokens:
            return {}
        tf = Counter(tokens)
        longest = max(tf.values())
        vec = {t: (0.5 + 0.5 * c / longest) * self.idf(t) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def cosine(self, a: dict[str, float], b: dict[str, float]) -> float:
        if not a or not b:
            return 0.0
        small, large = (a, b) if len(a) < len(b) else (b, a)
        return sum(v * large.get(t, 0.0) for t, v in small.items())

    def shared_terms(self, a: dict[str, float], b: dict[str, float],
                     limit: int = 3) -> list[str]:
        shared = [(t, v * b[t]) for t, v in a.items() if t in b]
        shared.sort(key=lambda kv: -kv[1])
        return [t for t, _ in shared[:limit]]


def similarity(index: TopicIndex, org_text: str, grant_text: str) -> tuple[float, str]:
    """Return (score 0-1, one-line explanation).

    Concepts dominate when they agree, because a shared concept is a stronger
    claim than shared vocabulary. Cosine carries the rest, which matters for
    the majority of programmes the lexicon says nothing about.
    """
    org_concepts = concepts_for(org_text)
    grant_concepts = concepts_for(grant_text)
    shared = org_concepts & grant_concepts

    vec_org = index.vector(org_text)
    vec_grant = index.vector(grant_text)
    cosine = index.cosine(vec_org, vec_grant)

    concept_score = len(shared) / len(org_concepts) if org_concepts else 0.0
    score = min(1.0, 0.65 * concept_score + 0.35 * min(1.0, cosine * 3))

    if shared:
        why = f"shares {', '.join(sorted(shared))} with your programs"
        terms = index.shared_terms(vec_org, vec_grant)
        if terms:
            why += f" (wording in common: {', '.join(terms)})"
    elif cosine > 0.08:
        terms = index.shared_terms(vec_org, vec_grant)
        why = (f"loosely related wording: {', '.join(terms)}" if terms
               else "loosely related wording")
    else:
        why = "nothing in this programme matches your stated program areas"
    return score, why
