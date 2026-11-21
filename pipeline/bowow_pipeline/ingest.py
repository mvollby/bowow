"""The ingest loop: fetch, snapshot, parse, extract, upsert, report.

Order matters. The raw snapshot is written before parsing, so a parser bug is
always recoverable without touching the network again.
"""
from __future__ import annotations

from .adapters import get_adapter
from .extract import ExtractionCache, apply_extraction, extract_cached
from .rawstore import RawStore
from .store import Store

# Only opportunities below this confidence are sent for extraction. Records
# whose source already stated eligibility cleanly are left alone, which keeps
# the bill proportional to the records that actually need help.
EXTRACT_BELOW = 0.7

# Below this many characters there is nothing worth extracting from.
MIN_ELIGIBILITY_CHARS = 40


def run_ingest(source: str, store: Store, raw: RawStore, limit: int = 25,
               extractor=None, cache: ExtractionCache | None = None,
               **adapter_kwargs) -> dict:
    adapter = get_adapter(source)
    for key, value in adapter_kwargs.items():
        if value is not None and hasattr(adapter, key):
            setattr(adapter, key, value)

    run_id = store.start_run(source)
    counts = {"fetched": 0, "inserted": 0, "updated": 0,
              "unchanged": 0, "errors": 0, "extracted": 0, "extract_cached": 0}

    for payload in adapter.fetch(limit=limit):
        counts["fetched"] += 1
        digest, _ = raw.put(source, payload)       # snapshot first, always
        try:
            record = adapter.parse(payload)
        except Exception as exc:                   # one bad row must not stop a run
            counts["errors"] += 1
            print(f"  parse error ({source}): {type(exc).__name__}: {exc}")
            continue
        if record is None:
            counts["errors"] += 1
            continue

        if adapter.record_type == "award":
            counts[store.upsert_award(record)] += 1
            continue

        if extractor is not None and _needs_extraction(record):
            try:
                result = extract_cached(record.eligibility_raw, extractor, cache)
                changed = apply_extraction(record, result)
                counts["extract_cached" if result.cached else "extracted"] += 1
                if result.rejected:
                    print(f"  unverifiable evidence dropped for "
                          f"{record.native_id}: {', '.join(result.rejected)}")
                if changed:
                    print(f"  extracted {', '.join(changed)} "
                          f"for {record.title[:44]}")
            except Exception as exc:
                # Extraction is an enrichment. Losing it must never lose the
                # record itself, which is already safely snapshotted.
                counts["errors"] += 1
                print(f"  extraction failed ({record.native_id}): "
                      f"{type(exc).__name__}: {exc}")

        counts[store.upsert_opportunity(record, raw_sha=digest)] += 1

    store.finish_run(run_id, counts)
    return counts


def _needs_extraction(record) -> bool:
    return (record.extraction_confidence < EXTRACT_BELOW
            and len(record.eligibility_raw or "") >= MIN_ELIGIBILITY_CHARS)
