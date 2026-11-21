"""Immutable, content-addressed store for everything we fetch.

The rule behind this module: save the bytes before you parse them. Parsers are
wrong more often than networks are, and refetching 2.4 GB to fix a regex is a
bad trade. Every snapshot is written once under its own SHA-256, so a repeated
fetch of unchanged upstream data costs no extra disk.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from .models import utcnow


class RawStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, source: str, digest: str) -> Path:
        # Shard by the first two hex chars so directories stay a sane size.
        return self.root / source / digest[:2] / f"{digest}.json.gz"

    def put(self, source: str, payload: object, meta: dict | None = None) -> tuple[str, bool]:
        """Store a snapshot. Returns (digest, was_new)."""
        blob = json.dumps(payload, sort_keys=True, default=str,
                          ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(blob).hexdigest()
        path = self._path(source, digest)
        if path.exists():
            return digest, False
        path.parent.mkdir(parents=True, exist_ok=True)
        envelope = {
            "source": source,
            "fetched_at": utcnow(),
            "sha256": digest,
            "meta": meta or {},
            "payload": payload,
        }
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(envelope, fh, ensure_ascii=False, default=str)
        tmp.replace(path)          # atomic, so a crash never leaves a half file
        return digest, True

    def get(self, source: str, digest: str) -> dict:
        with gzip.open(self._path(source, digest), "rt", encoding="utf-8") as fh:
            return json.load(fh)

    def count(self, source: str | None = None) -> int:
        base = self.root / source if source else self.root
        return sum(1 for _ in base.rglob("*.json.gz")) if base.exists() else 0
