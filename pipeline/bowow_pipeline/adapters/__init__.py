"""Source adapter interface.

Every source, whether a JSON API or a 2.4 GB CSV, implements the same two
steps: fetch raw payloads, then parse each into normalized records. Nothing
downstream knows which kind it was. Adding a funder means adding one file here
and nothing else.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Iterator

USER_AGENT = "bowow-grant-pipeline/0.1 (+research prototype; contact via repo)"


class FetchError(RuntimeError):
    pass


def http_json(url: str, payload: dict | None = None, timeout: int = 45,
              retries: int = 3) -> dict:
    """GET or POST JSON with bounded retry and backoff.

    Deliberately polite: we are hitting a few hundred funder sites, not a few
    million pages, so there is no reason to be aggressive.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise FetchError(f"{url} failed after {retries} attempts: {last_error}")


class SourceAdapter:
    """Base class. Subclasses set `name` and `record_type`."""

    name: str = "base"
    record_type: str = "opportunity"   # or "award"

    def fetch(self, limit: int = 50) -> Iterator[dict]:
        """Yield raw payloads exactly as the source returned them."""
        raise NotImplementedError

    def parse(self, raw: dict):
        """Turn one raw payload into an Opportunity or Award, or None to skip."""
        raise NotImplementedError


def get_adapter(name: str) -> SourceAdapter:
    from .canada_gc import CanadaGrantsAdapter
    from .canada_programs import CanadaProgramsAdapter
    from .grants_gov import GrantsGovAdapter

    registry = {
        GrantsGovAdapter.name: GrantsGovAdapter,
        CanadaGrantsAdapter.name: CanadaGrantsAdapter,
        CanadaProgramsAdapter.name: CanadaProgramsAdapter,
    }
    if name not in registry:
        raise KeyError(f"unknown adapter {name!r}; have {sorted(registry)}")
    return registry[name]()


def adapter_names() -> list[str]:
    from .canada_gc import CanadaGrantsAdapter
    from .canada_programs import CanadaProgramsAdapter
    from .grants_gov import GrantsGovAdapter
    return [GrantsGovAdapter.name, CanadaGrantsAdapter.name,
            CanadaProgramsAdapter.name]
