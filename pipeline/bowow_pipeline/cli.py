"""Command line entry point.

    python -m bowow_pipeline ingest grants_gov --limit 25 --keyword education
    python -m bowow_pipeline ingest canada_gc --limit 200 --program-filter literacy
    python -m bowow_pipeline stats
    python -m bowow_pipeline changes
    python -m bowow_pipeline precedent "Carleton University"
"""
from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from .adapters import adapter_names
from .extract import (DEFAULT_MODEL, ExtractionCache, apply_extraction,
                      extract_cached, get_extractor)
from .ingest import run_ingest
from .match import rank
from .models import Org, days_until, utcnow
from .topics import concepts_for
from .rawstore import RawStore
from .store import Store

DATA = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA / "bowow.db"
RAW_PATH = DATA / "raw"
CACHE_PATH = DATA / "extraction_cache"
FIXTURE_ORGS = Path(__file__).resolve().parent.parent / "fixtures" / "orgs_ca.json"
# The static site lives one level up from the pipeline package.
SITE_ROOT = Path(__file__).resolve().parent.parent.parent
WEB_DATA = SITE_ROOT / "data" / "opportunities.json"


def _open() -> tuple[Store, RawStore]:
    return Store(DB_PATH), RawStore(RAW_PATH)


def cmd_ingest(args) -> None:
    store, raw = _open()
    kwargs = {}
    if args.keyword:
        kwargs["keyword"] = args.keyword
    if args.program_filter:
        kwargs["program_filter"] = args.program_filter
    if args.local_path:
        kwargs["local_path"] = args.local_path
    if args.max_rows is not None:
        kwargs["max_rows"] = args.max_rows

    extractor = cache = None
    if args.extract:
        extractor = get_extractor(args.backend, args.model)
        cache = ExtractionCache(CACHE_PATH)
        print(f"eligibility extraction on, backend={extractor.backend} "
              f"model={extractor.model}")

    print(f"ingesting {args.source} (limit {args.limit}) ...")
    counts = run_ingest(args.source, store, raw, limit=args.limit,
                        extractor=extractor, cache=cache, **kwargs)
    print("  " + "  ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"  raw snapshots on disk: {raw.count(args.source)}")
    store.close()


def cmd_stats(args) -> None:
    store, raw = _open()
    c = store.counts()
    print(f"opportunities {c['opportunities']}   awards {c['awards']}   "
          f"changes {c['changes']}   sources {c['sources']}")
    print(f"raw snapshots {raw.count()}")

    rows = store.conn.execute(
        "SELECT source, COUNT(*) n, AVG(extraction_confidence) conf "
        "FROM opportunity GROUP BY source").fetchall()
    for r in rows:
        print(f"  {r['source']:12} {r['n']:4} opportunities  "
              f"avg confidence {r['conf']:.2f}")

    low = store.conn.execute(
        "SELECT COUNT(*) FROM opportunity WHERE extraction_confidence < 0.7"
    ).fetchone()[0]
    if low:
        print(f"  {low} need review: eligibility is prose we have not parsed")

    print("\nnext deadlines")
    for r in store.conn.execute(
            "SELECT title, funder_name, deadline, amount_max FROM opportunity "
            "WHERE deadline IS NOT NULL ORDER BY deadline LIMIT 5"):
        left = days_until(r["deadline"])
        cap = f"up to ${r['amount_max']:,}" if r["amount_max"] else "amount unstated"
        print(f"  {r['deadline']}  ({left:>4}d)  {r['title'][:52]:52} {cap}")

    dupes = store.duplicate_groups()
    print(f"\nduplicate dedupe_key groups: {len(dupes)}")
    for d in dupes[:5]:
        print(f"  {d['n']}x  {d['dedupe_key'][:70]}")
    store.close()


def cmd_changes(args) -> None:
    store, _ = _open()
    rows = store.recent_changes(args.limit)
    if not rows:
        print("no changes recorded yet (run ingest twice to see detection work)")
    for r in rows:
        print(f"[{r['changed_at']}] {(r['title'] or r['uid'])[:46]}")
        print(f"   {r['field']}: {str(r['old_value'])[:60]} -> {str(r['new_value'])[:60]}")
    store.close()


def cmd_precedent(args) -> None:
    """Toy version of the Phase 5 winnability signal, over whatever is loaded."""
    store, _ = _open()
    # Only the highest amendment of each agreement counts. Averaging every
    # revision would double count agreements and use superseded values.
    rows = store.conn.execute(
        """WITH latest AS (
             SELECT *, ROW_NUMBER() OVER (
               PARTITION BY substr(native_id, 1, instr(native_id, '#') - 1)
               ORDER BY amendment DESC) AS rn
             FROM award WHERE grantee_name LIKE ?
           )
           SELECT funder_name, COUNT(*) n, AVG(amount) avg_amt,
                  MIN(amount) min_amt, MAX(amount) max_amt
           FROM latest WHERE rn = 1
           GROUP BY funder_name ORDER BY n DESC""",
        (f"%{args.grantee}%",)).fetchall()
    if not rows:
        print(f"no awards on record for a grantee matching {args.grantee!r}")
    for r in rows:
        print(f"{r['funder_name'][:55]:55} {r['n']:3} awards  "
              f"avg ${r['avg_amt']:,.0f}  range ${r['min_amt']:,.0f}-${r['max_amt']:,.0f}")
    store.close()


def cmd_extract(args) -> None:
    """Backfill eligibility extraction over opportunities already stored."""
    store, _ = _open()
    extractor = get_extractor(args.backend, args.model)
    cache_for_scan = ExtractionCache(CACHE_PATH)

    if args.requeue_rejected:
        # Only the records a grounding change could affect. Everything else
        # already grounded cleanly and would just be paid for twice.
        pending = [o for o in store.needing_extraction(threshold=1.01, limit=10000)
                   if cache_for_scan.was_rejected(o.eligibility_raw, extractor.model)]
        scope = "records with a previously dropped claim"
    else:
        pending = store.needing_extraction(limit=args.limit)
        scope = "records below the confidence threshold"

    if not pending:
        print(f"no {scope}; nothing to do")
        store.close()
        return
    pending = pending[:args.limit]

    print(f"backend={extractor.backend} model={extractor.model}  "
          f"candidates={len(pending)} ({scope})")

    if args.dry_run:
        # Show exactly what would be sent, so the prompt and schema can be
        # reviewed before anyone is billed for a single call.
        opp = pending[0]
        print(f"\ndry run, first candidate: {opp.title[:60]}")
        if hasattr(extractor, "build_request"):
            body = extractor.build_request(opp.eligibility_raw)
            body["messages"][0]["content"] = \
                body["messages"][0]["content"][:600] + "\n... truncated ..."
            print(json.dumps(body, indent=2)[:2600])
        else:
            print("(rule backend takes no API request)")
        store.close()
        return

    cache = ExtractionCache(CACHE_PATH)
    stats = {"changed": 0, "cached": 0, "rejected": 0, "none": 0}
    tokens = {"input_tokens": 0, "output_tokens": 0}

    for opp in pending:
        result = extract_cached(opp.eligibility_raw, extractor, cache)
        changed = apply_extraction(opp, result)
        stats["cached"] += int(result.cached)
        stats["rejected"] += len(result.rejected)
        for k in tokens:
            tokens[k] += result.usage.get(k, 0)
        if changed:
            stats["changed"] += 1
            store.upsert_opportunity(opp)
            print(f"  {opp.title[:50]:50} -> {', '.join(changed)}")
            for name in changed:
                quote = result.evidence.get(name, "")
                if quote:
                    print(f"      {name}: \"{quote[:88]}\"")
        else:
            stats["none"] += 1
        if result.rejected:
            print(f"  {opp.title[:50]:50} !! unverifiable: "
                  f"{', '.join(result.rejected)}")
            # Print the offending span so the threshold can be judged from
            # evidence rather than from a count.
            for fname, quotes in result.rejected_quotes.items():
                print(f"      {fname} claimed: \"{str(quotes[0])[:96]}\"")

    print(f"\nupdated {stats['changed']}  no-change {stats['none']}  "
          f"cache hits {stats['cached']}  dropped claims {stats['rejected']}")
    if any(tokens.values()):
        print(f"tokens in={tokens['input_tokens']:,} "
              f"out={tokens['output_tokens']:,}")
    store.close()


def cmd_check(args) -> None:
    """Verify extraction setup. Never prints or logs the credential itself."""
    import os

    print("environment")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        value = os.environ.get(name)
        # Report presence and length only. A key must never reach a log,
        # a terminal transcript or a bug report.
        print(f"  {name:22} {'set, length ' + str(len(value)) if value else 'not set'}")
    print(f"  {'ANTHROPIC_BASE_URL':22} "
          f"{os.environ.get('ANTHROPIC_BASE_URL') or 'default'}")

    try:
        import anthropic
        print(f"  {'anthropic SDK':22} {anthropic.__version__}")
    except ImportError:
        print(f"  {'anthropic SDK':22} not installed (pip install anthropic)")

    chosen = get_extractor("auto")
    print(f"\nauto backend resolves to: {chosen.backend}")
    if chosen.backend == "rules":
        print("  no credential found, so extraction would use keyword matching")
        print("  set ANTHROPIC_API_KEY in your shell, then rerun this command")
        return
    if not args.live:
        print("  rerun with --live to make one real call and confirm it works")
        return

    sample = ("Eligibility: applications are accepted only from registered "
              "nonprofit organizations. Matching funds are not required for "
              "this program.")
    print("\nmaking one live call on a short sample ...")
    extractor = get_extractor("claude", args.model)
    result = extractor.extract(sample)

    print(f"  model              {result.model}")
    print(f"  applicant_types    {result.fields.get('applicant_types')}")
    print(f"  match_required     {result.fields.get('match_required')}")
    print(f"  partner_required   {result.fields.get('partner_required')}")
    print(f"  grounded confidence {result.confidence}")
    if result.rejected:
        print(f"  dropped unverifiable claims: {', '.join(result.rejected)}")
    if result.usage:
        print(f"  tokens in={result.usage.get('input_tokens')} "
              f"out={result.usage.get('output_tokens')}")

    expected = {"applicant_types": ["Nonprofit"], "match_required": "no"}
    ok = all(result.fields.get(k) == v for k, v in expected.items())
    print(f"\n{'PASS' if ok else 'CHECK'}: extraction "
          f"{'matches' if ok else 'did not match'} the expected answer "
          f"for this sample")


def cmd_profiles(args) -> None:
    store, _ = _open()
    if args.action == "load":
        path = Path(args.file) if args.file else FIXTURE_ORGS
        data = json.loads(path.read_text(encoding="utf-8"))
        counts = {"inserted": 0, "updated": 0}
        for record in data["orgs"]:
            record.pop("_comment", None)
            org = Org(**record)
            # Fixture rows are synthetic whatever the file claims. Nothing
            # loaded from a fixture may present itself as a registry record.
            org.synthetic = True
            counts[store.upsert_org(org)] += 1
        print(f"loaded {path.name}: {counts['inserted']} new, "
              f"{counts['updated']} updated (all marked synthetic)")

    orgs = store.orgs()
    print(f"\n{len(orgs)} profiles on file")
    for org in orgs:
        rev = f"${org.revenue:,.0f}" if org.revenue is not None else "no filing"
        tag = " [synthetic]" if org.synthetic else ""
        print(f"  {org.org_id:24} {org.country} {org.region[:12]:12} "
              f"{org.applicant_type[:16]:16} {rev:>12}  {org.name[:32]}{tag}")
    store.close()


def cmd_match(args) -> None:
    store, _ = _open()
    org = store.find_org(args.org)
    if org is None:
        print(f"no profile matching {args.org!r}; try: profiles list")
        store.close()
        return

    opportunities = store.all_opportunities()
    if not opportunities:
        print("the corpus is empty; run ingest first")
        store.close()
        return

    # Funder award history, keyed by funder, for the precedent component.
    award_index = {}
    for row in store.conn.execute(
            "SELECT funder_name, COUNT(*) n FROM award GROUP BY funder_name"):
        award_index[(row["funder_name"] or "").lower()] = row["n"]

    results = rank(org, opportunities, award_index or None)
    eligible = [r for r in results if r.eligible]

    rev = f"${org.revenue:,.0f}" if org.revenue is not None else "not on file"
    print(f"{org.name}{' [synthetic]' if org.synthetic else ''}")
    print(f"  {org.applicant_type}, {org.region}, {org.country} | revenue {rev}")
    print(f"\nscreened {len(results)} opportunities: "
          f"{len(eligible)} pass every hard gate, "
          f"{len(results) - len(eligible)} fail at least one")

    reasons = collections.Counter(
        name for r in results for name, _ in r.failed)
    if reasons:
        print("gate failures by cause:")
        for name, n in reasons.most_common():
            print(f"  {name:16} {n}")

    shown = (eligible or results)[:args.limit]
    header = "top matches" if eligible else "nothing passes; closest below"
    print(f"\n{header}")
    for r in shown:
        o = r.opportunity
        cap = f"up to ${o.amount_max:,}" if o.amount_max else "amount unstated"
        print(f"\n  {r.rating}/10  {o.title[:58]}")
        print(f"      {o.funder_name[:50]} | {cap} | {o.deadline or 'no deadline'}")
        for name, why in r.failed:
            print(f"      BLOCKED {name}: {why}")
        for name, why in r.unknown:
            print(f"      unknown {name}: {why}")
        for c in r.components:
            state = f"{c.score:.2f}" if c.known else " -- "
            print(f"      {state} {c.name:12} {c.explanation}")
        print(f"      scored on {r.coverage:.0%} of the intended weighting")
    store.close()


def cmd_export(args) -> None:
    """Write the corpus as JSON for the front end to fetch.

    Everything the UI needs to be honest travels with each record: the
    tri-state eligibility flags as true/false/null, the extraction confidence,
    and when the record was last verified. The page can then distinguish
    "no match required" from "nobody has established whether a match is
    required", which is the distinction the whole pipeline exists to preserve.
    """
    store, _ = _open()
    out = []
    for r in store.conn.execute(
            "SELECT * FROM opportunity ORDER BY deadline IS NULL, deadline"):
        purposes = json.loads(r["purposes"] or "[]")
        host = ""
        if r["source_url"]:
            try:
                host = urlparse(r["source_url"]).hostname or ""
            except ValueError:
                host = ""
        funder = r["funder_name"] or ""
        out.append({
            "id": r["uid"],
            "name": r["title"],
            "funder": funder,
            "host": host,
            # Initials give the card a placeholder before the favicon loads,
            # and a permanent one for funders whose favicon 404s.
            "mark": "".join(w[0] for w in funder.split()[:2]).upper() or "?",
            "summary": (r["summary"] or "")[:320],
            "eligibility": (r["eligibility_raw"] or "")[:400],
            "applicant": json.loads(r["applicant_types"] or "[]"),
            "regions": json.loads(r["regions"] or "[]"),
            "purposes": purposes,
            "tags": sorted(concepts_for(r["title"], r["summary"],
                                        " ".join(purposes))),
            "low": r["amount_min"], "high": r["amount_max"],
            "currency": r["currency"], "deadline": r["deadline"],
            "route": r["route"],
            # null means unknown and must stay null all the way to the browser.
            "match_required": None if r["match_required"] is None
            else bool(r["match_required"]),
            "partner_required": None if r["partner_required"] is None
            else bool(r["partner_required"]),
            "url": r["source_url"],
            "confidence": round(r["extraction_confidence"] or 0.0, 2),
            "last_verified": r["last_verified"],
            "provenance": json.loads(r["field_provenance"] or "{}"),
        })

    payload = {
        "generated_at": utcnow(),
        "count": len(out),
        "sources": sorted({r["source"] for r in store.conn.execute(
            "SELECT DISTINCT source FROM opportunity")}),
        "opportunities": out,
    }
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    unknown = sum(1 for o in out if o["match_required"] is None
                  or o["partner_required"] is None)
    print(f"wrote {len(out)} opportunities to {target}")
    print(f"  {unknown} still have at least one unknown eligibility flag")
    store.close()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="bowow_pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("ingest", help="fetch and normalize from one source")
    i.add_argument("source", choices=adapter_names())
    i.add_argument("--limit", type=int, default=25)
    i.add_argument("--keyword", help="grants_gov search keyword")
    i.add_argument("--program-filter", help="canada_gc substring filter")
    i.add_argument("--local-path", help="canada_gc: read a local CSV instead")
    i.add_argument("--max-rows", type=int,
                   help="canada_programs: award rows to scan (0 = whole file)")
    i.add_argument("--extract", action="store_true",
                   help="run eligibility extraction on low-confidence records")
    i.add_argument("--backend", choices=("auto", "claude", "rules"),
                   default="auto")
    i.add_argument("--model", default=DEFAULT_MODEL)
    i.set_defaults(func=cmd_ingest)

    x = sub.add_parser("extract", help="backfill eligibility extraction")
    x.add_argument("--limit", type=int, default=25)
    x.add_argument("--backend", choices=("auto", "claude", "rules"),
                   default="auto")
    x.add_argument("--model", default=DEFAULT_MODEL)
    x.add_argument("--dry-run", action="store_true",
                   help="print the request that would be sent, call nothing")
    x.add_argument("--requeue-rejected", action="store_true",
                   help="re-run only records whose claims were dropped before")
    x.set_defaults(func=cmd_extract)

    s = sub.add_parser("stats", help="corpus summary")
    s.set_defaults(func=cmd_stats)

    c = sub.add_parser("changes", help="recent field-level changes")
    c.add_argument("--limit", type=int, default=20)
    c.set_defaults(func=cmd_changes)

    pr = sub.add_parser("precedent", help="funder history for a grantee")
    pr.add_argument("grantee")
    pr.set_defaults(func=cmd_precedent)

    pf = sub.add_parser("profiles", help="load or list applicant profiles")
    pf.add_argument("action", choices=("load", "list"), nargs="?", default="list")
    pf.add_argument("--file", help="fixture JSON to load (default: Canadian set)")
    pf.set_defaults(func=cmd_profiles)

    m = sub.add_parser("match", help="score one organization against the corpus")
    m.add_argument("org", help="org_id or part of the name")
    m.add_argument("--limit", type=int, default=3)
    m.set_defaults(func=cmd_match)

    ck = sub.add_parser("check", help="verify extraction credentials and setup")
    ck.add_argument("--live", action="store_true",
                    help="make one real API call on a short sample")
    ck.add_argument("--model", default=DEFAULT_MODEL)
    ck.set_defaults(func=cmd_check)

    e = sub.add_parser("export", help="dump normalized JSON for the front end")
    # Defaults into the static site's own folder so the running server can
    # serve it without any extra wiring.
    e.add_argument("--out", default=str(WEB_DATA))
    e.set_defaults(func=cmd_export)

    args = p.parse_args(argv)
    args.func(args)
