"""A small HTTP layer so the web form can reach the pipeline.

The four profile cards on the front page collected data that went nowhere:
the website field was never read, and the program summary was lowercased for
a keyword parse and then discarded. This serves the static site and a tiny API
from one process, so a profile typed in the browser becomes a real Org and is
scored by the real matcher.

Deliberate choices:

* **One origin.** The static files and the API are served by the same server,
  so there is no CORS layer and no second port to keep in sync.
* **Localhost only.** It binds to 127.0.0.1. This is a development server
  built on `http.server`; it is not hardened and should not face a network.
* **The website is stored, never fetched.** Requesting a user-supplied URL
  from the server is a server-side request forgery vector. Crawling an
  applicant's site for mission text is a separate piece of work with its own
  risks, and this does not quietly do it.
* **Request bodies are capped** and input is validated with the same rules
  the CLI uses, so the API cannot write a profile the CLI would refuse.
"""
from __future__ import annotations

import json
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .match import rank
from .models import Org, mission_problem, slug
from .store import Store

MAX_BODY = 64 * 1024          # a profile is text; anything larger is a mistake
MAX_MATCHES = 50

# Accept only http(s) and a plausible host. This is validation for storage,
# not permission to request the URL.
WEBSITE_RE = re.compile(r"^https?://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/\S*)?$")


def _org_from_payload(payload: dict, existing: Org | None) -> tuple[Org | None, str]:
    """Build or update an Org from posted fields. Returns (org, error)."""
    summary = (payload.get("program_summary") or "").strip()
    website = (payload.get("website") or "").strip()
    name = (payload.get("name") or "").strip()

    if website and not WEBSITE_RE.match(website):
        return None, "website should look like https://example.org"

    if summary:
        problem = mission_problem(summary)
        if problem:
            return None, f"program summary {problem}"

    if existing is None:
        if not (summary or website or name):
            return None, "nothing to save: add a program summary or a website"
        label = name or (urlparse(website).hostname if website else "") or "unnamed profile"
        existing = Org(
            org_id=f"web-{slug(label)[:40]}" or "web-profile",
            name=name or label,
            country=(payload.get("country") or "CA").upper(),
            region=(payload.get("region") or "").strip(),
            applicant_type=(payload.get("applicant_type") or "Nonprofit"),
        )

    if summary:
        existing.mission = summary
    if website:
        existing.website = website
    if name:
        existing.name = name
    return existing, ""


# Built once per process. Assembling it means parsing ~33 MB of CRA CSVs and
# aggregating every award row, which took long enough per request that the
# results page simply sat there with no error and no content: the request had
# not come back yet. A development server can hold this in memory; restart it
# after re-ingesting.
_INDEX_CACHE: dict[str, object] = {}


def award_index_cached(store: Store, store_path: str):
    if store_path not in _INDEX_CACHE:
        from .cli import _award_index
        print("  building funder precedent index (once per process) ...")
        _INDEX_CACHE[store_path] = _award_index(store) or None
    return _INDEX_CACHE[store_path]


def _match_payload(store: Store, org: Org, limit: int, store_path: str = "") -> dict:
    opportunities = store.all_opportunities()
    results = rank(org, opportunities, award_index_cached(store, store_path))
    eligible = [r for r in results if r.eligible]
    shown = (eligible or results)[:limit]

    return {
        "org": {
            "org_id": org.org_id, "name": org.name,
            "country": org.country, "region": org.region,
            "revenue": org.revenue,
            "has_mission": bool(org.mission),
        },
        "screened": len(results),
        "eligible": len(eligible),
        "on_topic": sum(1 for r in eligible if not r.off_topic),
        "matches": [{
            "uid": r.opportunity.uid,
            "name": r.opportunity.title,
            "funder": r.opportunity.funder_name,
            "rating": r.rating,
            "off_topic": r.off_topic,
            "coverage": round(r.coverage, 2),
            "low": r.opportunity.amount_min,
            "high": r.opportunity.amount_max,
            "currency": r.opportunity.currency,
            "deadline": r.opportunity.deadline,
            "route": r.opportunity.route,
            "url": r.opportunity.source_url,
            "blocked": [{"gate": g, "why": w} for g, w in r.failed],
            "unverified": [{"gate": g, "why": w} for g, w in r.unknown],
            "components": [{
                "name": c.name,
                "score": c.score,
                "explanation": c.explanation,
            } for c in r.components],
        } for r in shown],
    }


class Handler(SimpleHTTPRequestHandler):
    store_path: str = ""
    site_root: str = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=self.site_root, **kwargs)

    # Keep the console readable; one line per request is enough.
    def log_message(self, fmt, *args):
        print(f"  {self.command} {self.path.split('?')[0]} -> {args[1]}")

    def end_headers(self):
        # No caching, for everything including static files. A browser
        # serving a cached app.js silently defeats every front-end change
        # and looks exactly like a bug in the new code: a submit handler
        # that had been edited still ran the previous version from cache.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def _json(self, status: int, body: dict) -> None:
        blob = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):          # noqa: N802 - http.server naming
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return super().do_GET()

        query = parse_qs(parsed.query)
        store = Store(self.store_path)
        try:
            if parsed.path == "/api/profile":
                org = store.find_org((query.get("org_id") or [""])[0])
                if org is None:
                    return self._json(404, {"error": "no such profile"})
                return self._json(200, {
                    "org_id": org.org_id, "name": org.name,
                    "website": org.website, "program_summary": org.mission,
                    "revenue": org.revenue, "region": org.region,
                })

            if parsed.path == "/api/match":
                org = store.find_org((query.get("org_id") or [""])[0])
                if org is None:
                    return self._json(404, {"error": "no such profile"})
                try:
                    limit = int((query.get("limit") or ["10"])[0])
                except ValueError:
                    limit = 10
                return self._json(200, _match_payload(
                    store, org, max(1, min(MAX_MATCHES, limit)),
                    self.store_path))

            return self._json(404, {"error": "unknown endpoint"})
        finally:
            store.close()

    def do_POST(self):         # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/profile":
            return self._json(404, {"error": "unknown endpoint"})

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad Content-Length"})
        if length <= 0 or length > MAX_BODY:
            return self._json(413, {"error": f"body must be 1..{MAX_BODY} bytes"})

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(400, {"error": "body must be JSON"})
        if not isinstance(payload, dict):
            return self._json(400, {"error": "body must be a JSON object"})

        store = Store(self.store_path)
        try:
            existing = None
            if payload.get("org_id"):
                existing = store.find_org(payload["org_id"])
            org, error = _org_from_payload(payload, existing)
            if error:
                return self._json(422, {"error": error})
            action = store.upsert_org(org)
            return self._json(200, {
                "org_id": org.org_id, "action": action,
                "stored": {
                    "name": org.name,
                    "website": org.website,
                    "program_summary_chars": len(org.mission),
                },
                # Say plainly what was not done with the website.
                "note": ("the website is stored but never fetched by the "
                         "server" if org.website else ""),
            })
        finally:
            store.close()


def serve(store_path: str, site_root: str, port: int = 8000) -> None:
    Handler.store_path = str(store_path)
    Handler.site_root = str(site_root)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"serving {site_root} and /api on http://127.0.0.1:{port}")
    print("  development server, bound to localhost only")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
