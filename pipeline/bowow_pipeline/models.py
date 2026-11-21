"""Normalized records shared by every source adapter.

Two record kinds flow through the pipeline:

* Opportunity - an open call an organization can apply to.
* Award       - a grant that was already made. This is the precedent signal
                used later for winnability scoring, not something to apply to.

Keeping them in one schema module means an adapter author only has to
satisfy these dataclasses; nothing downstream knows where a record came from.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

# Controlled vocabularies. Adapters map source-specific codes onto these so
# the matcher never has to know that Grants.gov says "12" for a 501(c)(3).
APPLICANT_TYPES = ("Nonprofit", "School", "Higher education", "Individual",
                   "Government", "Tribal", "For-profit", "Other")
ROUTES = ("Open call", "Invitation only", "Rolling")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(text: str) -> str:
    """Lowercase alphanumeric slug, used for cross-source dedupe keys."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def content_hash(payload: object) -> str:
    """Stable hash of any JSON-serializable payload."""
    blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class Opportunity:
    source: str                 # adapter name, e.g. "grants_gov"
    native_id: str              # id in the source system
    title: str
    funder_name: str

    summary: str = ""
    eligibility_raw: str = ""   # verbatim prose; the LLM extraction target
    applicant_types: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    purposes: list[str] = field(default_factory=list)

    amount_min: int | None = None
    amount_max: int | None = None
    currency: str | None = None

    deadline: str | None = None          # ISO date
    route: str | None = None
    match_required: bool | None = None   # None means genuinely unknown
    partner_required: bool | None = None

    source_url: str = ""
    # Per-field {source_url, snippet} so the UI can cite any claim it shows.
    field_provenance: dict = field(default_factory=dict)
    # 0.0-1.0. Low confidence must surface in the UI, never be averaged away.
    extraction_confidence: float = 1.0

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.native_id}"

    @property
    def dedupe_key(self) -> str:
        """Catches the same program listed by two sources or re-posted yearly."""
        cycle = (self.deadline or "")[:4]
        return f"{slug(self.funder_name)}|{slug(self.title)}|{cycle}"

    def fingerprint(self) -> str:
        """Hash of the fields we care about changing. Ignores bookkeeping."""
        d = asdict(self)
        for ignored in ("field_provenance", "extraction_confidence"):
            d.pop(ignored, None)
        return content_hash(d)


@dataclass
class Org:
    """An applicant organization.

    The fields are the ones a public registry actually gives you: the CRA
    T3010 return for Canadian charities, IRS Form 990 for US nonprofits. That
    is deliberate. Everything here should be obtainable from a registry lookup
    on a business number or EIN, with no document parsing, because that is the
    cheapest path to a usable profile.
    """
    org_id: str
    name: str
    country: str                 # ISO-2, e.g. "CA"
    region: str                  # province or state
    applicant_type: str          # one of APPLICANT_TYPES
    registry_id: str = ""        # CRA Business Number or IRS EIN
    status: str = ""             # e.g. "Registered charity"
    revenue: float | None = None
    program_expense: float | None = None
    categories: list[str] = field(default_factory=list)
    mission: str = ""
    beneficiaries: str = ""
    # True for hand-written test fixtures. Never let synthetic rows be
    # mistaken for a real registry record, in the database or in any output.
    synthetic: bool = False

    @property
    def budget_band(self) -> str:
        if self.revenue is None:
            return "unknown"
        if self.revenue < 250_000:
            return "small"
        if self.revenue < 2_000_000:
            return "mid"
        return "large"


@dataclass
class Award:
    source: str
    native_id: str
    funder_name: str
    grantee_name: str
    amount: float | None = None
    currency: str = "CAD"
    year: int | None = None
    purpose: str = ""
    grantee_ref: str = ""     # registry id (CRA Business Number, EIN) when given
    region: str = ""
    source_url: str = ""
    # Amendments restate an agreement and can change its value. Keep every
    # revision and let queries take the highest amendment per reference,
    # otherwise precedent amounts silently reflect superseded figures.
    amendment: int = 0

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.native_id}"

    def fingerprint(self) -> str:
        return content_hash(asdict(self))


def parse_amount(value) -> int | None:
    if value in (None, "", "0"):
        return None
    try:
        n = float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None
    return int(n) if n > 0 else None


def parse_date(value) -> str | None:
    """Accept the several date shapes these sources use. Return ISO or None."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y %I:%M:%S %p %Z",
                "%b %d, %Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    # Trailing timezone names vary; retry on the leading date portion.
    m = re.match(r"([A-Za-z]{3} \d{1,2}, \d{4})", text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%b %d, %Y").date().isoformat()
        except ValueError:
            pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else None


def strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)
    return re.sub(r"[ \t]+", " ", text).strip()


# Topic vocabulary shared by the search form, the exporter and the matcher.
# One definition, so a grant and a program summary are always compared on the
# same terms.
TOPIC_PATTERNS = (
    ("student aid", r"bursar|scholarship|tuition|financial aid|student aid"),
    ("rural access", r"\brural\b|\bremote\b"),
    ("learning support", r"tutor|mentor|literacy|learning"),
    ("partnerships", r"partner|collaborat|consortium"),
    ("research", r"research|fieldwork"),
    ("operating costs", r"operating|capacity|overhead"),
)


def tags_for(*texts: str) -> list[str]:
    blob = " ".join(t or "" for t in texts).lower()
    return [name for name, pattern in TOPIC_PATTERNS if re.search(pattern, blob)]


def days_until(iso_date: str | None, today: date | None = None) -> int | None:
    if not iso_date:
        return None
    try:
        d = date.fromisoformat(iso_date)
    except ValueError:
        return None
    return (d - (today or date.today())).days
