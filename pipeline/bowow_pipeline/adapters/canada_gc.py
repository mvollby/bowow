"""Canada proactive-disclosure adapter: grants and contributions already awarded.

This is the second source shape the pipeline has to survive: not a JSON API but
a single 2.46 GB CSV, published under the Open Government Licence. Two
consequences drive the design here.

* Never load it into memory. We stream and stop, so a 200-row sample costs a
  couple of hundred KB rather than a 2.46 GB download.
* These are awards, not open calls. Nobody can apply to them. They exist to
  answer "who does this funder actually fund, and at what size", which is the
  precedent signal behind a believable match score.

The payoff field is recipient_business_number: a CRA Business Number that joins
straight to the charities register. US 990-PF grant rows carry no grantee EIN,
so Canadian precedent data is materially cleaner than the US equivalent.
"""
from __future__ import annotations

import csv
import io
import re
import urllib.request
from typing import Iterator

from ..models import Award
from . import USER_AGENT, SourceAdapter

DATASET = "432527ab-7aac-45b5-81d6-7597107a7013"
CSV_URL = (f"https://open.canada.ca/data/dataset/{DATASET}/resource/"
           "1d15a62f-5656-49ad-8c88-f40ce689d831/download/grants.csv")
SEARCH_URL = "https://search.open.canada.ca/grants/"

# The file is not UTF-8. Try the strict reading first, fall back to the Windows
# codepage that French accents actually arrive in.
ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")

# Leading junk on a header name: BOM residue, quotes, whitespace.
_HEADER_JUNK = re.compile(r"^[^A-Za-z0-9]+")


class CanadaGrantsAdapter(SourceAdapter):
    name = "canada_gc"
    record_type = "award"

    def __init__(self, url: str = CSV_URL, local_path: str | None = None,
                 program_filter: str | None = None):
        self.url = url
        self.local_path = local_path      # lets tests and reruns skip the network
        self.program_filter = (program_filter or "").lower()

    def _open_stream(self) -> io.TextIOWrapper:
        if self.local_path:
            binary = open(self.local_path, "rb")
        else:
            req = urllib.request.Request(
                self.url, headers={"User-Agent": USER_AGENT})
            binary = urllib.request.urlopen(req, timeout=120)
        # errors="replace" keeps one bad byte from killing a whole ingest run.
        return io.TextIOWrapper(binary, encoding=ENCODINGS[1],
                                errors="replace", newline="")

    @staticmethod
    def _clean_keys(row: dict) -> dict:
        """Strip BOM residue and stray whitespace from header names.

        The file carries a UTF-8 byte order mark on a body that is otherwise
        cp1252. Read as cp1252, those three BOM bytes do not collapse into
        U+FEFF; they arrive as the separate characters 0xEF 0xBB 0xBF, so the
        first column is '\xef\xbb\xbfref_number' and every lookup on it fails
        silently. Dropping leading non-alphanumerics is encoding-agnostic and
        safe, because every real header in this schema starts with a letter.
        """
        return {_HEADER_JUNK.sub("", (k or "").strip()): v for k, v in row.items()}

    def fetch(self, limit: int = 200) -> Iterator[dict]:
        stream = self._open_stream()
        try:
            reader = csv.DictReader(stream)
            kept = 0
            for raw_row in reader:
                row = self._clean_keys(raw_row)
                if kept >= limit:
                    break
                if self.program_filter:
                    haystack = " ".join([
                        row.get("prog_name_en") or "",
                        row.get("prog_purpose_en") or "",
                        row.get("agreement_title_en") or "",
                    ]).lower()
                    if self.program_filter not in haystack:
                        continue
                yield row
                kept += 1
        finally:
            stream.close()   # closing early aborts the download mid-stream

    def parse(self, raw: dict) -> Award | None:
        ref = (raw.get("ref_number") or "").strip()
        grantee = (raw.get("recipient_legal_name")
                   or raw.get("recipient_operating_name") or "").strip()
        if not ref or not grantee:
            return None

        try:
            amount = float((raw.get("agreement_value") or "0").replace(",", ""))
        except ValueError:
            amount = None

        start = (raw.get("agreement_start_date") or "").strip()
        year = None
        if len(start) >= 4 and start[:4].isdigit():
            year = int(start[:4])

        purpose = " ".join(filter(None, [
            (raw.get("prog_name_en") or "").strip(),
            (raw.get("agreement_title_en") or "").strip(),
        ])).strip()

        region = " ".join(filter(None, [
            (raw.get("recipient_province") or "").strip(),
            (raw.get("recipient_country") or "").strip(),
        ])).strip()

        try:
            amendment = int(float(raw.get("amendment_number") or 0))
        except ValueError:
            amendment = 0

        return Award(
            source=self.name,
            # Amendment number belongs in the identity. Keying on ref_number
            # alone drops revisions, and a revision can change the dollar value.
            native_id=f"{ref}#{amendment}",
            amendment=amendment,
            # owner_org_title is bilingual, "English | French". Keep the
            # English half only, so award rows and the program catalogue
            # derived from them key on the same funder name and the
            # precedent lookup actually joins.
            funder_name=((raw.get("owner_org_title") or "Government of Canada")
                         .split("|")[0].strip()),
            grantee_name=grantee,
            grantee_ref=(raw.get("recipient_business_number") or "").strip(),
            amount=amount,
            currency="CAD",
            year=year,
            purpose=purpose[:500],
            region=region,
            source_url=SEARCH_URL,
        )
