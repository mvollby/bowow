"""Real applicant profiles from public charity registries.

Replaces hand-written fixtures with the two registries the plan named:

* **ProPublica Nonprofit Explorer** for US nonprofits, a free JSON API over
  the IRS extract. Live, no key, no caching needed.
* **CRA List of Charities** for Canadian charities, published yearly as CSV on
  open.canada.ca. Downloaded once (about 33 MB) and cached, because a lookup
  cannot stream 12 MB per query.

Two things verified rather than assumed, after guessing recipient-type codes
wrong earlier in this project:

* T3010 line numbers were checked against the live file: **4700** is total
  revenue, **4950** total expenditures, **5000** expenditure on charitable
  programmes. The totals reconcile against their component lines.
* ProPublica exposes `totrevenue` and `totfuncexpns` but **no program-service
  expense line**. `totfuncexpns` is TOTAL functional expenses, so mapping it
  to `program_expense` would silently relabel a different number. US profiles
  therefore carry revenue and leave programme expense unset, which is a real
  asymmetry between the two countries rather than a bug.

CRA resource ids change every year, so the current dataset is discovered
through the CKAN catalogue instead of being hardcoded.
"""
from __future__ import annotations

import csv
import io
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

from .models import Org, slug

USER_AGENT = "bowow-grant-pipeline/0.1 (+research prototype)"

PP_BASE = "https://projects.propublica.org/nonprofits/api/v2"
CKAN = "https://open.canada.ca/data/en/api/3/action"

# Verified against the live financial CSV, not inferred from the form.
T3010_TOTAL_REVENUE = "4700"
T3010_TOTAL_EXPENSE = "4950"
T3010_PROGRAM_EXPENSE = "5000"

# NTEE major group B is Education. B2x are schools, B4x and B5x are higher
# education. Everything else stays a generic nonprofit rather than guessing.
NTEE_APPLICANT_TYPES = (
    (re.compile(r"^B(2\d|3\d)"), "School"),
    (re.compile(r"^B(4\d|5\d)"), "Higher education"),
)


def _get(url: str, timeout: int = 45):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _get_json(url: str, timeout: int = 45) -> dict:
    return json.loads(_get(url, timeout).decode("utf-8"))


def _num(value) -> float | None:
    if value in (None, "", "N"):
        return None
    try:
        n = float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None
    return n


class ProPublicaRegistry:
    """US nonprofits, from the IRS extract via ProPublica."""

    name = "propublica"
    country = "US"

    def search(self, query: str, limit: int = 5) -> list[dict]:
        url = f"{PP_BASE}/search.json?q={urllib.parse.quote(query)}"
        data = _get_json(url)
        return (data.get("organizations") or [])[:limit]

    def fetch(self, ein: str) -> Org | None:
        digits = re.sub(r"\D", "", str(ein))
        if not digits:
            return None
        try:
            data = _get_json(f"{PP_BASE}/organizations/{digits}.json")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

        org = data.get("organization") or {}
        filings = data.get("filings_with_data") or []
        # Most recent filing that actually carries figures.
        latest = max(filings, key=lambda f: f.get("tax_prd_yr") or 0, default=None)

        ntee = (org.get("ntee_code") or "").upper()
        applicant_type = "Nonprofit"
        for pattern, mapped in NTEE_APPLICANT_TYPES:
            if pattern.match(ntee):
                applicant_type = mapped
                break

        subsection = org.get("subsection_code")
        status = f"501(c)({subsection})" if subsection else "Tax-exempt organization"
        if latest:
            status += f", latest filing {latest.get('tax_prd_yr')}"

        return Org(
            org_id=f"us-{digits}",
            name=(org.get("name") or "").strip(),
            country="US",
            region=(org.get("state") or "").strip(),
            applicant_type=applicant_type,
            registry_id=digits,
            status=status,
            revenue=_num(latest.get("totrevenue")) if latest else None,
            # No program-service expense line exists in this API. Total
            # functional expenses is a different number and is not substituted.
            program_expense=None,
            categories=[c for c in (ntee,) if c],
            mission="",          # the IRS extract carries no mission text
            beneficiaries="",
            synthetic=False,
        )


class CraRegistry:
    """Canadian registered charities, from the CRA List of Charities."""

    name = "cra"
    country = "CA"

    def __init__(self, cache_dir: str | Path):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self._ident: dict[str, dict] | None = None
        self._financial: dict[str, dict] | None = None

    # -- discovery ---------------------------------------------------------

    def latest_dataset(self) -> tuple[str, dict[str, str]]:
        """Find the newest yearly release and its two CSV resources.

        Resource ids change with every annual release, so they are discovered
        rather than pinned. Datasets are chosen by the year in the title, not
        by modification date, because old years get re-touched.
        """
        query = urllib.parse.quote('title:"List of charities"')
        data = _get_json(f"{CKAN}/package_search?q={query}&rows=40")
        best_year, best = -1, None
        for package in data["result"]["results"]:
            title = package.get("title")
            title = title.get("en") if isinstance(title, dict) else title
            match = re.search(r"(20\d\d)", str(title) or "")
            if match and int(match.group(1)) > best_year:
                best_year, best = int(match.group(1)), package
        if best is None:
            raise RuntimeError("no CRA List of charities dataset found")

        wanted = {}
        for resource in best.get("resources", []):
            label = resource.get("name")
            label = label.get("en") if isinstance(label, dict) else label
            label = str(label or "").strip().lower()
            # CKAN returns some resource urls site-relative and others
            # absolute, in the same response.
            url = urllib.parse.urljoin("https://open.canada.ca/",
                                       resource.get("url") or "")
            if label == "identification":
                wanted["identification"] = url
            elif label == "financial data":
                wanted["financial"] = url
        missing = {"identification", "financial"} - set(wanted)
        if missing:
            raise RuntimeError(f"CRA {best_year} release is missing {missing}")
        return str(best_year), wanted

    # -- cache -------------------------------------------------------------

    def ensure_cache(self, refresh: bool = False) -> str:
        year, urls = self.latest_dataset()
        for label, url in urls.items():
            path = self.cache / f"cra_{label}_{year}.csv"
            if path.exists() and not refresh:
                continue
            print(f"  downloading CRA {label} for {year} ...")
            path.write_bytes(_get(url, timeout=300))
            print(f"    {path.stat().st_size / 1e6:.1f} MB -> {path.name}")
        self._year = year
        return year

    def _load(self) -> None:
        if self._ident is not None:
            return
        year = getattr(self, "_year", None) or self.ensure_cache()
        ident_path = self.cache / f"cra_identification_{year}.csv"
        fin_path = self.cache / f"cra_financial_{year}.csv"

        def read(path: Path) -> list[dict]:
            # The files are cp1252, like the other CRA publications.
            text = path.read_bytes().decode("cp1252", errors="replace")
            return list(csv.DictReader(io.StringIO(text)))

        self._ident = {}
        for row in read(ident_path):
            bn = (row.get("BN") or "").strip()
            if bn:
                self._ident[bn] = row

        self._financial = {}
        for row in read(fin_path):
            bn = (row.get("BN") or "").strip()
            # Keep the most recent fiscal period end per charity.
            if bn and (bn not in self._financial
                       or (row.get("FPE") or "") > (self._financial[bn].get("FPE") or "")):
                self._financial[bn] = row

    # -- lookup ------------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> list[dict]:
        self._load()
        needle = query.strip().lower()
        hits = [r for r in self._ident.values()
                if needle in (r.get("Legal Name") or "").lower()]
        hits.sort(key=lambda r: len(r.get("Legal Name") or ""))
        return hits[:limit]

    def fetch(self, business_number: str) -> Org | None:
        self._load()
        bn = re.sub(r"\s+", "", str(business_number)).upper()
        row = self._ident.get(bn)
        if row is None:                      # accept a bare 9-digit number
            matches = [k for k in self._ident if k.startswith(bn)]
            if len(matches) != 1:
                return None
            bn, row = matches[0], self._ident[matches[0]]

        fin = (self._financial or {}).get(bn, {})
        name = (row.get("Legal Name") or row.get("Account Name") or "").strip()
        fpe = (fin.get("FPE") or "").strip()

        return Org(
            org_id=f"ca-{slug(name)[:40]}" if name else f"ca-{bn}",
            name=name,
            country="CA",
            region=(row.get("Province") or "").strip(),
            applicant_type="Nonprofit",      # every row here is a charity
            registry_id=bn,
            status=("Registered charity"
                    + (f", return for {fpe}" if fpe else "")),
            revenue=_num(fin.get(T3010_TOTAL_REVENUE)),
            program_expense=_num(fin.get(T3010_PROGRAM_EXPENSE)),
            categories=[c for c in ((row.get("Category") or "").strip(),) if c],
            mission="",          # the register carries no mission text
            beneficiaries="",
            synthetic=False,
        )


def get_registry(country: str, cache_dir: str | Path):
    code = (country or "").upper()
    if code == "US":
        return ProPublicaRegistry()
    if code == "CA":
        return CraRegistry(cache_dir)
    raise ValueError(f"no registry for country {country!r}; have US, CA")
