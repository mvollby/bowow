# BOWOW ingestion pipeline (Phase 1 spike)

Proves the corpus architecture from the project plan against two real sources
with deliberately different shapes: a JSON API and a 2.46 GB CSV. Python 3.11+,
SQLite for storage. The core runs on the standard library alone; the `anthropic`
SDK is optional and only needed for Claude-backed eligibility extraction.

```bash
cd pipeline
python -m bowow_pipeline ingest grants_gov --limit 20 --keyword education
python -m bowow_pipeline ingest canada_gc  --limit 150
python -m bowow_pipeline stats
python -m bowow_pipeline changes
python -m bowow_pipeline precedent "Carleton"
python -m bowow_pipeline export
python -m pytest tests/ -q
```

Data lands in `pipeline/data/` and is gitignored.

## Eligibility extraction

The structured applicant-type field is mostly empty (finding 1 below), so the
real rule has to be read out of prose. That is what `extract.py` does.

```bash
pip install anthropic            # optional; without it the rule backend is used

python -m bowow_pipeline check                    # verify setup, no API call
python -m bowow_pipeline check --live             # one real call on a sample
python -m bowow_pipeline extract --dry-run        # print the request, call nothing
python -m bowow_pipeline extract --limit 25       # backfill stored records
python -m bowow_pipeline ingest grants_gov --limit 20 --extract
python -m bowow_pipeline extract --backend rules  # force the offline fallback
```

Backends: `auto` (default) uses Claude when a credential is present and the
rule extractor otherwise, so the pipeline never hard-fails on a missing key.
`--backend claude` and `--backend rules` force the choice.

The credential comes from the environment (`ANTHROPIC_API_KEY`, or an
`ant auth login` profile); it is never read from a file in this repo and never
passed on a command line. `check` reports only whether a credential is present
and how long it is, never its value, so its output is safe to paste into an
issue. Create a key at <https://console.anthropic.com/settings/keys>, then on
Windows set it without putting it in shell history:

```powershell
[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", (Read-Host "Paste key"), "User")
```

Open a new terminal afterwards, then run `check` to confirm.

**Evidence is verified, not trusted.** Every extracted field must come with a
verbatim quote from the source text, and the quote is checked against that text
before the field is stored. A claim whose quote cannot be found is discarded
and reported as a dropped claim. Confidence is the share of asserted fields
that survived this check, not the model's own estimate of itself. Eligibility
is precisely the field where a confident wrong answer costs a nonprofit a week
of work, so the guard is worth its cost.

**Three-state, never boolean.** "No match required" and "the document does not
say" are different answers, and collapsing them to `False` invents a fact. The
schema uses `yes` / `no` / `unstated`, and `unstated` maps to `None`.

**Extraction fills gaps only.** A value the source API stated explicitly is
better evidence than anything inferred from prose and is never overwritten.
`["Other"]` is treated as an empty slot, because that is what Grants.gov code
25 actually means.

**Cost control.** Only records below 0.7 confidence with at least 40 characters
of eligibility text are sent. Results are cached on disk by a hash of the text,
model and prompt version, so re-ingesting unchanged records costs nothing, and
changing the prompt invalidates correctly. The model is `claude-opus-5-5` at
`low` effort with a small `max_tokens`, since the response is a small JSON
object; `--model` overrides it (`claude-haiku-5-5` is the obvious choice if a
full-corpus run gets expensive). Token usage is reported per run.

Not yet done: the system prompt is shorter than the minimum cacheable prefix,
so prompt caching would not currently fire and has deliberately been left out
rather than added as decoration. Worth revisiting if the prompt grows.

## What it proves

| Claim from the plan | How it is demonstrated |
|---|---|
| One adapter interface fits any source shape | `adapters/grants_gov.py` (paged JSON API, two calls per record) and `adapters/canada_gc.py` (streamed CSV) differ only inside `fetch` and `parse` |
| Snapshot before parsing | `rawstore.py` writes content-addressed gzip envelopes; a parser fix never needs a refetch |
| Ingest is idempotent | Second run of the same query reports `unchanged=N`, `inserted=0` |
| Changes are caught at field level | `opportunity_change` rows name the field, old value and new value |
| Never load the big file | A 150-row sample from a 2.46 GB CSV completes in about 2 seconds by closing the stream early |

## Architecture

```
adapters/     fetch() -> raw payloads, parse() -> Opportunity | Award
rawstore.py   immutable content-addressed snapshots (sha256, gzip)
models.py     normalized schema, controlled vocabularies, field parsers
store.py      SQLite upsert, dedupe keys, field-level change log
ingest.py     fetch -> snapshot -> parse -> upsert -> run record
cli.py        ingest / stats / changes / precedent / export
```

Two record kinds move through the same pipeline. An **Opportunity** is an open
call someone can apply to. An **Award** is a grant already made, which nobody
can apply to; it exists to answer "who does this funder actually fund, and at
what size", the precedent signal behind a believable match score.

## Front end

`export` writes `../data/opportunities.json`, which the static site fetches.
With the server running, regenerate and reload:

```bash
python -m bowow_pipeline export
```

`results.js` no longer carries a hardcoded grant list. The change that matters
is that a filter now resolves three ways rather than two: match, miss, or the
source never said. The prototype only had match and miss, so an unknown
silently counted as a match and the card asserted something nobody had
checked. Unknowns are now counted separately in the header, listed on the card
under "Could not verify", and ranked below records that could be verified.

Scores are banded so the number cannot contradict the ordering: 7.0 to 9.2 for
fully verified, 4.0 to 6.9 when something could not be checked, 1.0 to 3.9 when
a filter is missed. The first version used one blended scale and produced a
verified 5.5 sorted above an unverified 5.7, which looks broken even though
the order was right. The score measures filter match only. It is not a
judgement of the grant, and extraction confidence sits beside it as a badge
rather than being blended in.

## Findings that should change the plan

**1. The structured eligibility field is mostly empty.** Grants.gov exposes
`applicantTypes` as codes, which looks like it removes the need for text
parsing. In a 20-record education sample, 16 came back as code 25, "Others (see
text field entitled Additional Information on Eligibility)". The real rule
lives in `applicantEligibilityDesc` prose. LLM extraction of eligibility text
is therefore core Phase 1 work, not a Phase 4 nicety for foundation websites.

Records carry `extraction_confidence`, and anything below 0.7 is reported by
`stats` as needing review rather than being presented as a confident fact.
This is what the extraction layer above exists to fix.

A note on the rule fallback: in a 12-record live run it recovered applicant
types for 10 records, but 6 of those still resolved to `Other` because no
literal phrase matched, and its confidence is capped at 0.6 so those records
stay flagged. Keyword matching also fails exactly where it matters most, on
negation. "No matching funds are required" and "matching funds are required"
differ by one word sitting before the phrase a positive pattern keys on, and
the first version of the patterns got it backwards until a test caught it. The
fallback is a stopgap for running without a key, not a substitute for the
model.

**2. Upstream ordering is unstable, so hash the normalized record, not the
response.** Two identical calls for the same opportunity returned
`applicantTypes` in different order, producing a different raw payload hash.
Diffing raw responses would fire a false "this grant changed" alert on every
run and train users to ignore alerts. The adapter sorts into the controlled
vocabulary and the fingerprint covers only normalized fields, so the reordering
is correctly seen as noise. Covered by
`test_applicant_type_reordering_is_not_a_change`.

**3. Amendments change dollar values, so they belong in the record identity.**
Canadian agreements are restated by amendment, sharing one `ref_number`. In the
first 150 rows, 28 reference numbers appeared more than once, and at least one
dropped from $661,126 to $438,311 on amendment. Keying on `ref_number` alone
silently kept the superseded figure. Identity is now `ref_number#amendment`,
and `precedent` takes only the highest amendment per agreement.

**4. Business Number coverage is partial, and the file is ordered by
department.** The plan claimed CRA Business Numbers give clean grantee
resolution. Across 5,000 sampled rows, 51.5% carry one. Broken out by
recipient type, it is 1,511/1,519 for type F and 568/657 for type N, while
rows with a blank recipient type have almost none. The first 150 rows showed
0%, purely because they were all one department. Two consequences: fuzzy name
matching is still required for roughly half of rows, and **any sample taken
from the head of this file is not representative**. Sample randomly before
quoting a statistic from it.

**5. Encoding is genuinely mixed.** The CSV carries a UTF-8 byte order mark on
a body that is otherwise cp1252. Read as cp1252, the BOM does not collapse to
U+FEFF; it arrives as three separate characters, so the first column is
`\xef\xbb\xbfref_number` and every lookup against it fails silently with no
exception. Header names are normalized per row.

## Known limits

- `partner_required` is never populated. Grants.gov does not express it, so it
  stays `None`, meaning unknown rather than false. The matcher must treat the
  two differently.
- Currencies are stored as listed and never converted.
- Both adapters cover government sources. The foundation long tail, which is
  the hard part, is untouched.
- SQLite, single process, no scheduler. Postgres with pgvector plus a scheduler
  is the Phase 2 move.
- `export` writes the JSON shape `results.js` expects, but the front end has
  not been pointed at it.

## Sources

Grants.gov `search2` and `fetchOpportunity` are public and keyless. The
Canadian data is Proactive Disclosure of Grants and Contributions, dataset
`432527ab-7aac-45b5-81d6-7597107a7013`, published under the Open Government
Licence, which requires attribution. Both are polled politely with a
descriptive User-Agent and backoff.
