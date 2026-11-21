"""Turn eligibility prose into structured fields.

Why this exists: the spike found that Grants.gov returns applicant type code 25,
"Others (see text field)", for most education opportunities. The machine
readable field is nearly empty and the real rule is in `applicantEligibilityDesc`
prose. Without this module the matcher cannot answer "can a nonprofit apply".

Three design decisions worth keeping:

1. **Every extracted field must quote its evidence, and the quote is verified
   against the source text.** A quote that is not actually present is treated as
   a hallucination and that field is discarded. This is the cheapest guard
   available against confidently wrong eligibility claims, and eligibility is
   exactly the field where being confidently wrong wastes an applicant's week.

2. **Tri-state, never boolean.** "No partner required" and "the document does
   not say" are different answers. Collapsing them to False invents a fact.

3. **Results are cached on disk by content hash.** Re-ingesting 500 unchanged
   opportunities should cost nothing. The cache key includes the prompt version
   and model, so changing either invalidates correctly.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import APPLICANT_TYPES

# Bump when the prompt or schema changes so cached results are not reused.
PROMPT_VERSION = "v1"

DEFAULT_MODEL = "claude-opus-5-5"

TRI_STATE = ("yes", "no", "unstated")

SYSTEM_PROMPT = """\
You extract eligibility rules from grant program text for a grant matching \
service. Nonprofits rely on this to decide whether to spend days on an \
application, so a confident wrong answer is worse than an honest "unstated".

Rules:
- Answer only from the supplied text. Never use outside knowledge about the \
program or the funder.
- Every field has an evidence field. Put a VERBATIM span copied from the \
supplied text into it. Do not paraphrase, reformat or correct the quote. If \
there is no supporting span, return "unstated" (or an empty list) and leave \
the evidence empty.
- "unstated" means the text does not say. Do not infer "no" from silence.
- applicant_types: list only the categories the text actually permits. If the \
text names a narrow group with no clear category, use "Other" and explain in \
notes.
- match_required means the applicant must contribute or source matching funds, \
also called cost sharing.
- partner_required means a formal partner, consortium or fiscal sponsor is \
mandatory, not merely encouraged."""

# Strict schema. additionalProperties false plus required on every key is what
# makes output_config.format a guarantee rather than a hope.
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "applicant_types": {
            "type": "array",
            "items": {"type": "string", "enum": list(APPLICANT_TYPES)},
        },
        "applicant_types_evidence": {"type": "string"},
        "match_required": {"type": "string", "enum": list(TRI_STATE)},
        "match_required_evidence": {"type": "string"},
        "partner_required": {"type": "string", "enum": list(TRI_STATE)},
        "partner_required_evidence": {"type": "string"},
        "geographic_restriction": {"type": "string"},
        "geographic_evidence": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": [
        "applicant_types", "applicant_types_evidence",
        "match_required", "match_required_evidence",
        "partner_required", "partner_required_evidence",
        "geographic_restriction", "geographic_evidence", "notes",
    ],
    "additionalProperties": False,
}

_EVIDENCE_OF = {
    "applicant_types": "applicant_types_evidence",
    "match_required": "match_required_evidence",
    "partner_required": "partner_required_evidence",
    "geographic_restriction": "geographic_evidence",
}


@dataclass
class Extraction:
    """What came back, plus how much of it survived verification."""
    fields: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    rejected: list = field(default_factory=list)   # fields whose quote was unverifiable
    confidence: float = 0.0
    backend: str = "none"
    model: str = ""
    cached: bool = False
    usage: dict = field(default_factory=dict)


def _normalize(text: str) -> str:
    """Collapse whitespace so quote matching survives reflowed prose."""
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def verify_evidence(quote: str, source: str) -> bool:
    """True when the quote really appears in the source text.

    Deliberately strict about content and lenient about whitespace. A model
    that reformats a line break is fine; a model that invents a sentence is not.
    """
    q, s = _normalize(quote), _normalize(source)
    if len(q) < 12:          # too short to be meaningful evidence
        return False
    return q in s


def _ground(payload: dict, source: str) -> Extraction:
    """Keep only the fields whose evidence is verifiable against the source."""
    out = Extraction(backend="pending")
    checked = 0
    kept = 0

    for name, ev_key in _EVIDENCE_OF.items():
        value = payload.get(name)
        quote = (payload.get(ev_key) or "").strip()

        unstated = value in ("unstated", "", None) or value == []
        if unstated:
            out.fields[name] = None if name != "applicant_types" else []
            continue

        checked += 1
        if verify_evidence(quote, source):
            out.fields[name] = value
            out.evidence[name] = quote
            kept += 1
        else:
            # Claimed a fact it could not point to. Drop the claim, keep the note.
            out.rejected.append(name)
            out.fields[name] = None if name != "applicant_types" else []

    out.fields["notes"] = (payload.get("notes") or "").strip()
    # Confidence is grounding rate, not model self-report. A run where nothing
    # was asserted is not confident, it is empty.
    out.confidence = 0.0 if checked == 0 else round(kept / checked, 2)
    return out


class ExtractionCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(text: str, model: str) -> str:
        blob = f"{PROMPT_VERSION}|{model}|{text}".encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def get(self, key: str) -> dict | None:
        path = self.root / f"{key}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None          # a corrupt cache entry should never be fatal

    def put(self, key: str, payload: dict) -> None:
        (self.root / f"{key}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8")


class RuleExtractor:
    """Deterministic fallback for when no API credential is configured.

    It is intentionally conservative: it only asserts what a literal phrase
    supports, so the pipeline degrades to "less extracted" rather than to
    "confidently wrong". It is also what the tests run against.
    """

    backend = "rules"
    model = "rules"

    PATTERNS = {
        "Nonprofit": r"\bnon-?profits?\b|\b501\(c\)\(3\)\b|\bcharit(?:y|ies|able)\b",
        "Higher education": r"\b(?:universit|college|institutions? of higher)",
        "School": r"\bschool districts?\b|\bk-12\b|\bprimary schools?\b",
        "Individual": r"\bindividuals?\b|\bsole applicants?\b",
        "Government": r"\b(?:state|county|municipal|local) governments?\b",
        "Tribal": r"\btribal\b|\bnative american\b|\bfirst nations?\b",
        "For-profit": r"\bfor-?profit\b|\bsmall business(?:es)?\b",
    }
    # Negation is where keyword matching earns its reputation. "No matching
    # funds are required" and "matching funds are required" differ by one word
    # that sits before the phrase the positive pattern keys on, so the negative
    # patterns are checked first and are written to tolerate the copula.
    _MATCH_SUBJECT = r"(?:cost[- ]sharing|matching funds?|match)"
    _PARTNER_SUBJECT = r"(?:partner(?:ship)?s?|consortium|fiscal sponsor)"

    MATCH_YES = (rf"\b{_MATCH_SUBJECT}\b[^.]{{0,24}}\b(?:is|are)?\s*required\b"
                 r"|\brequires? (?:a )?match\b")
    MATCH_NO = (rf"\bno {_MATCH_SUBJECT}\b[^.]{{0,40}}\brequired\b"
                rf"|\b{_MATCH_SUBJECT}\b[^.]{{0,24}}\bnot required\b")
    PARTNER_YES = (r"\bmust partner\b|\brequires? (?:a )?(?:partner|fiscal sponsor)\b"
                   rf"|\b{_PARTNER_SUBJECT}\b[^.]{{0,24}}\b(?:is|are)?\s*required\b")
    PARTNER_NO = (rf"\bno {_PARTNER_SUBJECT}\b[^.]{{0,40}}\brequired\b"
                  rf"|\b{_PARTNER_SUBJECT}\b[^.]{{0,24}}\bnot required\b")

    def extract(self, text: str) -> Extraction:
        out = Extraction(backend=self.backend, model=self.model)
        low = text.lower()

        types = [name for name, pat in self.PATTERNS.items()
                 if re.search(pat, low)]
        out.fields["applicant_types"] = sorted(types)
        if types:
            out.evidence["applicant_types"] = "matched literal phrase in source"

        def tri(yes_pat, no_pat):
            if re.search(no_pat, low):
                return "no"
            if re.search(yes_pat, low):
                return "yes"
            return None

        out.fields["match_required"] = tri(self.MATCH_YES, self.MATCH_NO)
        out.fields["partner_required"] = tri(self.PARTNER_YES, self.PARTNER_NO)
        out.fields["geographic_restriction"] = None
        out.fields["notes"] = ""

        asserted = sum(1 for v in (types, out.fields["match_required"],
                                   out.fields["partner_required"]) if v)
        # Capped below the review threshold on purpose: keyword matching is a
        # stopgap, and its output should always still be flagged for review.
        out.confidence = min(0.6, 0.2 * asserted)
        return out


class ClaudeExtractor:
    """Structured extraction through the official Anthropic SDK."""

    backend = "claude"

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "low",
                 max_tokens: int = 2000):
        self.model = model
        # Pulling a few labelled fields out of a page of prose is a simple task,
        # and the guidance is to use low effort for those. Raise it if the
        # grounding rate turns out to be poor on real eligibility text.
        self.effort = effort
        # Deliberately short: the schema caps the response at a small JSON
        # object, so the usual 16k default would just be headroom we never use.
        self.max_tokens = max_tokens
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise RuntimeError(
                    "the anthropic SDK is not installed; run "
                    "`pip install anthropic` or use --backend rules") from exc
            self._client = anthropic.Anthropic()
        return self._client

    def build_request(self, text: str) -> dict:
        """The exact request body. Separated out so --dry-run can print it."""
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": SYSTEM_PROMPT,
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
            },
            "messages": [{
                "role": "user",
                "content": f"<eligibility_text>\n{text}\n</eligibility_text>",
            }],
        }

    def extract(self, text: str) -> Extraction:
        client = self._get_client()
        response = client.messages.create(**self.build_request(text))

        # Safety classifiers can decline; stop_reason must be checked before
        # touching content or this raises a confusing StopIteration instead.
        if getattr(response, "stop_reason", None) == "refusal":
            out = Extraction(backend=self.backend, model=self.model)
            out.fields["notes"] = "model declined to process this text"
            return out

        raw = next(b.text for b in response.content if b.type == "text")
        out = _ground(json.loads(raw), text)
        out.backend = self.backend
        out.model = self.model
        usage = getattr(response, "usage", None)
        if usage is not None:
            out.usage = {
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
            }
        return out


def get_extractor(backend: str = "auto", model: str = DEFAULT_MODEL):
    """Pick a backend. 'auto' uses Claude when a credential looks available."""
    if backend == "rules":
        return RuleExtractor()
    if backend == "claude":
        return ClaudeExtractor(model=model)
    if backend != "auto":
        raise ValueError(f"unknown backend {backend!r}")
    has_credential = bool(os.environ.get("ANTHROPIC_API_KEY")
                          or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return ClaudeExtractor(model=model) if has_credential else RuleExtractor()


def extract_cached(text: str, extractor, cache: ExtractionCache | None) -> Extraction:
    """Run extraction, reusing a cached result when the text is unchanged."""
    if cache is None:
        return extractor.extract(text)
    key = cache.key(text, getattr(extractor, "model", "unknown"))
    hit = cache.get(key)
    if hit is not None:
        out = Extraction(**hit)
        out.cached = True
        return out
    result = extractor.extract(text)
    cache.put(key, {
        "fields": result.fields, "evidence": result.evidence,
        "rejected": result.rejected, "confidence": result.confidence,
        "backend": result.backend, "model": result.model, "usage": result.usage,
    })
    return result


def apply_extraction(opp, result: Extraction) -> list[str]:
    """Merge extracted fields into an Opportunity. Returns the fields changed.

    Merge policy: extraction only fills gaps. A value the source API stated
    explicitly is better evidence than anything inferred from prose, so it is
    never overwritten. In practice the gap is almost everything, because the
    structured applicant type field is mostly code 25.
    """
    changed: list[str] = []

    types = result.fields.get("applicant_types") or []
    # ["Other"] is the API's way of saying "look at the prose", so treat it as
    # an empty slot rather than as an answer.
    api_said_nothing = not opp.applicant_types or opp.applicant_types == ["Other"]
    if types and api_said_nothing:
        opp.applicant_types = sorted(set(types))
        changed.append("applicant_types")

    for name in ("match_required", "partner_required"):
        value = result.fields.get(name)
        if value in ("yes", "no") and getattr(opp, name) is None:
            setattr(opp, name, value == "yes")
            changed.append(name)

    for name in changed:
        quote = result.evidence.get(name) or result.evidence.get("applicant_types", "")
        opp.field_provenance[name] = {
            "url": opp.source_url,
            "snippet": quote[:300],
            "method": f"{result.backend}:{result.model}",
        }

    if result.fields.get("notes"):
        opp.field_provenance["eligibility_notes"] = {
            "url": opp.source_url,
            "snippet": result.fields["notes"][:300],
            "method": f"{result.backend}:{result.model}",
        }

    # Confidence reflects how well the eligibility claims are grounded. Only
    # raise it; a strong structured source should not be dragged down by a
    # thin prose extraction.
    opp.extraction_confidence = max(opp.extraction_confidence, result.confidence)
    return changed
