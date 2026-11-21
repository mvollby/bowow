"""API tests. A real server on an ephemeral port, no mocking of HTTP."""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bowow_pipeline import api
from bowow_pipeline.api import Handler, _org_from_payload
from bowow_pipeline.models import Opportunity, Org
from bowow_pipeline.store import Store


@pytest.fixture
def server(tmp_path):
    db = tmp_path / "test.db"
    store = Store(db)
    future = (date.today() + timedelta(days=90)).isoformat()
    store.upsert_opportunity(Opportunity(
        source="s", native_id="1", title="Adult literacy tutoring programme",
        funder_name="Funder", summary="supports adult literacy and tutoring",
        applicant_types=["Nonprofit"], regions=["Canada"],
        amount_min=10_000, amount_max=50_000, currency="CAD",
        deadline=future, match_required=False, partner_required=False))
    store.close()

    api._INDEX_CACHE.clear()
    Handler.store_path = str(db)
    Handler.site_root = str(tmp_path)
    (tmp_path / "index.html").write_text("<h1>site</h1>", encoding="utf-8")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def post(base, payload, path="/api/profile"):
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


# --- the fields the form collects -----------------------------------------

def test_posting_the_two_text_fields_creates_a_profile(server):
    status, body = post(server, {
        "website": "https://example.org",
        "program_summary": "We run adult literacy tutoring across the region.",
    })
    assert status == 200
    assert body["action"] == "inserted"
    assert body["stored"]["website"] == "https://example.org"
    assert body["stored"]["program_summary_chars"] > 25


def test_the_summary_becomes_the_mission_used_for_matching(server):
    _, body = post(server, {
        "program_summary": "We run adult literacy tutoring across the region."})
    status, profile = get(server, f"/api/profile?org_id={body['org_id']}")
    assert status == 200
    assert "literacy tutoring" in profile["program_summary"]


def test_posting_again_updates_rather_than_duplicates(server):
    _, first = post(server, {
        "website": "https://example.org",
        "program_summary": "We run adult literacy tutoring across the region."})
    _, second = post(server, {
        "org_id": first["org_id"],
        "program_summary": "We now also run numeracy classes for adults here."})
    assert second["action"] == "updated"
    assert second["org_id"] == first["org_id"]


# --- validation ------------------------------------------------------------

def test_placeholder_summary_is_rejected(server):
    """The same rule the CLI enforces: the API must not be a way around it."""
    status, body = post(server, {"program_summary": "..."})
    assert status == 422
    assert "too short" in body["error"]


def test_malformed_website_is_rejected(server):
    status, body = post(server, {
        "website": "javascript:alert(1)",
        "program_summary": "We run adult literacy tutoring across the region."})
    assert status == 422
    assert "https://example.org" in body["error"]


def test_empty_payload_is_rejected(server):
    status, body = post(server, {})
    assert status == 422
    assert "nothing to save" in body["error"]


def test_non_json_body_is_rejected(server):
    request = urllib.request.Request(
        server + "/api/profile", data=b"not json",
        headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=20)
    assert exc.value.code == 400


def test_oversized_body_is_refused(server):
    status, body = post(server, {"program_summary": "x" * (api.MAX_BODY + 10)})
    assert status == 413


# --- the website is stored, never fetched ---------------------------------

def test_website_is_stored_and_the_response_says_it_is_not_fetched(server):
    """Requesting a user-supplied URL server side is an SSRF vector. The
    response states plainly that it does not happen."""
    _, body = post(server, {
        "website": "https://example.org",
        "program_summary": "We run adult literacy tutoring across the region."})
    assert "never fetched" in body["note"]


# --- matching --------------------------------------------------------------

def test_match_returns_scored_results_with_reasoning(server):
    _, created = post(server, {
        "program_summary": "We run adult literacy tutoring across the region."})
    status, body = get(server, f"/api/match?org_id={created['org_id']}&limit=5")

    assert status == 200
    assert body["screened"] >= 1
    match = body["matches"][0]
    assert match["name"] == "Adult literacy tutoring programme"
    # Every component travels with its explanation, including the unknown ones.
    assert {c["name"] for c in match["components"]} >= {"topic", "award_size"}
    assert all(c["explanation"] for c in match["components"])


def test_match_limit_is_capped(server):
    _, created = post(server, {
        "program_summary": "We run adult literacy tutoring across the region."})
    _, body = get(server, f"/api/match?org_id={created['org_id']}&limit=9999")
    assert len(body["matches"]) <= api.MAX_MATCHES


def test_unknown_profile_is_a_404(server):
    status, _ = get(server, "/api/match?org_id=does-not-exist")
    assert status == 404


def test_unknown_endpoint_is_a_404(server):
    status, _ = get(server, "/api/nope")
    assert status == 404


# --- static site ------------------------------------------------------------

def test_static_files_are_served_from_the_same_origin(server):
    """One origin means no CORS layer to keep in sync."""
    with urllib.request.urlopen(server + "/index.html", timeout=20) as response:
        assert response.status == 200
        assert b"site" in response.read()


def test_static_responses_are_not_cached(server):
    """A cached app.js silently defeats every front-end change and looks
    exactly like a bug in the new code."""
    with urllib.request.urlopen(server + "/index.html", timeout=20) as response:
        assert "no-store" in response.headers.get("Cache-Control", "")


# --- payload building ------------------------------------------------------

def test_existing_profile_keeps_fields_the_post_omits():
    existing = Org(org_id="o", name="Keep Me", country="CA", region="ON",
                   applicant_type="Nonprofit", revenue=400_000,
                   mission="original mission text that is long enough")
    org, error = _org_from_payload({"website": "https://example.org"}, existing)
    assert error == ""
    assert org.revenue == 400_000
    assert org.mission == "original mission text that is long enough"
    assert org.website == "https://example.org"
