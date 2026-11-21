"""Funder precedent: who does this funder actually fund, and at what size.

This is the signal behind "could we conceivably get it" rather than "are we
eligible". Eligibility is a set of boolean gates any directory can apply. A
$2M grant is technically open to a $120K charity and realistically out of
reach, and the only way to know that is to look at who the funder has funded
before.

How it is built:

1. Award rows carry a recipient business number. The CRA charity register
   carries revenue. Joining the two gives, per funder, the revenue
   distribution of its actual grantees.
2. The applicant's revenue is placed in that distribution.

Two identifier facts that are easy to get wrong and were verified against the
data rather than assumed:

* Award rows carry a **9-digit** business number; the charity register is
  keyed by the **15-character** BN plus program identifier ("...RR0001").
  Joining the raw strings matches nothing at all, silently.
* Most federal award rows go to companies, not charities, so they are absent
  from the register by definition. Filtering awards to recipient type "N"
  (not-for-profits and charities) before building this is what makes the join
  worth doing.

Coverage is reported with every profile, because a distribution built from
four grantees is not a distribution.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Below this many matched grantees, the shape of the distribution is noise.
MIN_GRANTEES = 5


def bn_root(business_number: str) -> str:
    """The 9-digit business number root, which is the joinable key."""
    digits = re.sub(r"\D", "", str(business_number or ""))
    return digits[:9] if len(digits) >= 9 else ""


def _percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return ordered[idx]


@dataclass
class FunderProfile:
    funder: str
    awards: int = 0                 # award rows seen for this funder
    grantees_with_bn: int = 0       # rows carrying a business number
    matched: int = 0                # grantees resolved in the charity register
    revenues: list[float] = field(default_factory=list)
    amounts: list[float] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return len(self.revenues) >= MIN_GRANTEES

    @property
    def match_rate(self) -> float:
        return self.matched / self.grantees_with_bn if self.grantees_with_bn else 0.0

    def band(self) -> tuple[float, float, float]:
        """Typical grantee revenue as (p25, median, p75)."""
        return (_percentile(self.revenues, 0.25),
                _percentile(self.revenues, 0.50),
                _percentile(self.revenues, 0.75))

    def typical_award(self) -> float | None:
        return _percentile(self.amounts, 0.50) if self.amounts else None


def build_profiles(award_rows, charity_revenue: dict[str, float]) -> dict[str, FunderProfile]:
    """Aggregate awards into per-funder grantee profiles.

    `award_rows` yields objects with funder_name, grantee_ref and amount.
    `charity_revenue` maps a 9-digit BN root to revenue.
    """
    profiles: dict[str, FunderProfile] = {}
    for row in award_rows:
        funder = (row["funder_name"] or "").strip()
        if not funder:
            continue
        profile = profiles.setdefault(funder.lower(), FunderProfile(funder))
        profile.awards += 1

        amount = row["amount"]
        if amount:
            profile.amounts.append(float(amount))

        root = bn_root(row["grantee_ref"])
        if not root:
            continue
        profile.grantees_with_bn += 1
        revenue = charity_revenue.get(root)
        if revenue:
            profile.matched += 1
            profile.revenues.append(revenue)
    return profiles


def charity_revenue_index(cra_registry) -> dict[str, float]:
    """Map 9-digit BN root to the charity's filed total revenue."""
    cra_registry._load()
    from .registry import T3010_TOTAL_REVENUE

    index: dict[str, float] = {}
    for bn, financial in (cra_registry._financial or {}).items():
        raw = financial.get(T3010_TOTAL_REVENUE)
        try:
            revenue = float(str(raw).replace(",", "")) if raw else 0.0
        except ValueError:
            continue
        root = bn_root(bn)
        if root and revenue > 0:
            index[root] = revenue
    return index


def score_applicant(profile: FunderProfile, revenue: float | None) -> tuple[float, str]:
    """Where does this applicant sit among the funder's actual grantees?"""
    if not profile.usable:
        return 0.0, (f"only {len(profile.revenues)} of this funder's grantees "
                     f"could be resolved in the charity register, too few to "
                     f"compare against")
    p25, median, p75 = profile.band()
    if revenue is None:
        return 0.5, (f"this funder's typical grantee reports "
                     f"${median:,.0f} revenue; your own is not on file")

    if p25 <= revenue <= p75:
        score = 1.0
        verdict = "squarely in this funder's usual range"
    elif revenue < p25:
        ratio = revenue / p25 if p25 else 0
        score = 0.65 if ratio > 0.35 else 0.35
        verdict = ("smaller than this funder's usual grantee, though not "
                   "outside it" if score > 0.5
                   else "much smaller than this funder's usual grantee")
    else:
        ratio = revenue / p75 if p75 else 0
        score = 0.7 if ratio < 3 else 0.45
        verdict = ("larger than this funder's usual grantee" if score > 0.5
                   else "far larger than this funder's usual grantee")

    return score, (f"typical grantee reports ${median:,.0f} revenue "
                   f"(middle half ${p25:,.0f}-${p75:,.0f}); you are at "
                   f"${revenue:,.0f}, {verdict}")
