"""SQLite persistence with upsert, dedupe and field-level change detection.

SQLite keeps the spike at zero setup. The schema is deliberately the one from
the plan, so moving to Postgres with pgvector later is a driver swap plus an
embedding column rather than a redesign.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .models import Award, Opportunity, Org, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunity (
  uid TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  native_id TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  title TEXT, funder_name TEXT, summary TEXT, eligibility_raw TEXT,
  applicant_types TEXT, regions TEXT, purposes TEXT,
  amount_min INTEGER, amount_max INTEGER, currency TEXT,
  deadline TEXT, route TEXT, match_required INTEGER, partner_required INTEGER,
  source_url TEXT, field_provenance TEXT, extraction_confidence REAL,
  fingerprint TEXT NOT NULL,
  raw_sha256 TEXT,
  first_seen TEXT NOT NULL,
  last_verified TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opp_dedupe ON opportunity(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_opp_deadline ON opportunity(deadline);

CREATE TABLE IF NOT EXISTS opportunity_change (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT NOT NULL,
  changed_at TEXT NOT NULL,
  field TEXT NOT NULL,
  old_value TEXT,
  new_value TEXT
);
CREATE INDEX IF NOT EXISTS idx_change_uid ON opportunity_change(uid);

CREATE TABLE IF NOT EXISTS award (
  uid TEXT PRIMARY KEY,
  source TEXT NOT NULL, native_id TEXT NOT NULL,
  funder_name TEXT, grantee_name TEXT, grantee_ref TEXT,
  amount REAL, currency TEXT, year INTEGER, purpose TEXT, region TEXT,
  amendment INTEGER DEFAULT 0,
  source_url TEXT, fingerprint TEXT NOT NULL, first_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_award_funder ON award(funder_name);
CREATE INDEX IF NOT EXISTS idx_award_ref ON award(grantee_ref);

CREATE TABLE IF NOT EXISTS org (
  org_id TEXT PRIMARY KEY,
  name TEXT NOT NULL, country TEXT, region TEXT,
  applicant_type TEXT, registry_id TEXT, status TEXT,
  revenue REAL, program_expense REAL,
  categories TEXT, mission TEXT, beneficiaries TEXT, website TEXT,
  synthetic INTEGER DEFAULT 0,
  first_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL, started_at TEXT, finished_at TEXT,
  fetched INTEGER DEFAULT 0, inserted INTEGER DEFAULT 0,
  updated INTEGER DEFAULT 0, unchanged INTEGER DEFAULT 0,
  errors INTEGER DEFAULT 0, note TEXT
);
"""

# Fields watched for change detection. Deadline and amount moves are the ones
# that actually burn an applicant, so they are the point of the whole table.
TRACKED = ("title", "deadline", "amount_min", "amount_max", "route",
           "match_required", "partner_required", "eligibility_raw", "summary")

_LIST_FIELDS = ("applicant_types", "regions", "purposes")

# Explicit, ordered column list. Never derive this from a set: column order and
# value order have to agree, and set iteration order is not a contract.
_OPP_COLUMNS = (
    "title", "funder_name", "summary", "eligibility_raw",
    "applicant_types", "regions", "purposes",
    "amount_min", "amount_max", "currency",
    "deadline", "route", "match_required", "partner_required",
    "source_url", "field_provenance", "extraction_confidence",
)


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    # Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS
    # leaves an existing table alone, so a new column has to be added
    # explicitly or every insert against an older database fails.
    MIGRATIONS = (
        ("org", "website", "TEXT"),
        ("award", "amendment", "INTEGER DEFAULT 0"),
    )

    def _migrate(self) -> None:
        for table, column, decl in self.MIGRATIONS:
            existing = {r["name"] for r in
                        self.conn.execute(f"PRAGMA table_info({table})")}
            if existing and column not in existing:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _encode(opp: Opportunity) -> dict:
        row = asdict(opp)
        for key in _LIST_FIELDS:
            row[key] = json.dumps(row[key])
        row["field_provenance"] = json.dumps(row["field_provenance"])
        for key in ("match_required", "partner_required"):
            row[key] = None if row[key] is None else int(row[key])
        return row

    def upsert_opportunity(self, opp: Opportunity, raw_sha: str = "") -> str:
        """Insert, update or no-op. Returns 'inserted', 'updated' or 'unchanged'."""
        row = self._encode(opp)
        now = utcnow()
        fp = opp.fingerprint()
        existing = self.conn.execute(
            "SELECT * FROM opportunity WHERE uid = ?", (opp.uid,)).fetchone()

        if existing is None:
            cols = ("uid", "source", "native_id", "dedupe_key", *_OPP_COLUMNS,
                    "fingerprint", "raw_sha256", "first_seen", "last_verified")
            values = (opp.uid, opp.source, opp.native_id, opp.dedupe_key,
                      *[row[c] for c in _OPP_COLUMNS], fp, raw_sha, now, now)
            self.conn.execute(
                f"INSERT INTO opportunity ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})", values)
            self.conn.commit()
            return "inserted"

        # Carry forward enrichment the fresh parse does not have. A re-parse
        # starts with null for everything extraction fills, so without this a
        # routine re-ingest overwrites extracted eligibility with "unknown"
        # and quietly throws away work that was already paid for. Known never
        # loses to unknown; a source that later states a value still wins,
        # because then the new value is not null.
        if self._carry_forward(opp, existing):
            row = self._encode(opp)
            fp = opp.fingerprint()

        if existing["fingerprint"] == fp:
            self.conn.execute(
                "UPDATE opportunity SET last_verified = ? WHERE uid = ?",
                (now, opp.uid))
            self.conn.commit()
            return "unchanged"

        for fname in TRACKED:
            old, new = existing[fname], row.get(fname)
            if old is None and new is None:
                continue
            if str(old) != str(new):
                self.conn.execute(
                    "INSERT INTO opportunity_change "
                    "(uid, changed_at, field, old_value, new_value) VALUES (?,?,?,?,?)",
                    (opp.uid, now, fname,
                     None if old is None else str(old),
                     None if new is None else str(new)))

        sets = ", ".join(f"{c} = ?" for c in _OPP_COLUMNS)
        self.conn.execute(
            f"UPDATE opportunity SET {sets}, dedupe_key = ?, fingerprint = ?, "
            f"raw_sha256 = ?, last_verified = ? WHERE uid = ?",
            (*[row[c] for c in _OPP_COLUMNS], opp.dedupe_key, fp, raw_sha,
             now, opp.uid))
        self.conn.commit()
        return "updated"

    @staticmethod
    def _carry_forward(opp: Opportunity, existing: sqlite3.Row) -> bool:
        """Restore enriched values the new parse lost. Returns True if changed."""
        touched = False

        for name in ("match_required", "partner_required"):
            if getattr(opp, name) is None and existing[name] is not None:
                setattr(opp, name, bool(existing[name]))
                touched = True

        # ["Other"] is Grants.gov code 25, meaning "the rule is in the prose".
        # It must not displace a list extraction already resolved.
        stored_types = json.loads(existing["applicant_types"] or "[]")
        if (not opp.applicant_types or opp.applicant_types == ["Other"]) \
                and stored_types and stored_types != ["Other"]:
            opp.applicant_types = stored_types
            touched = True

        stored_prov = json.loads(existing["field_provenance"] or "{}")
        for key, value in stored_prov.items():
            if key not in opp.field_provenance:
                opp.field_provenance[key] = value
                touched = True

        stored_conf = existing["extraction_confidence"] or 0.0
        if stored_conf > opp.extraction_confidence:
            opp.extraction_confidence = stored_conf
            touched = True

        return touched

    def upsert_award(self, award: Award) -> str:
        exists = self.conn.execute(
            "SELECT 1 FROM award WHERE uid = ?", (award.uid,)).fetchone()
        if exists:
            return "unchanged"
        self.conn.execute(
            "INSERT INTO award (uid, source, native_id, funder_name, grantee_name, "
            "grantee_ref, amount, currency, year, purpose, region, amendment, "
            "source_url, fingerprint, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (award.uid, award.source, award.native_id, award.funder_name,
             award.grantee_name, award.grantee_ref, award.amount, award.currency,
             award.year, award.purpose, award.region, award.amendment,
             award.source_url, award.fingerprint(), utcnow()))
        self.conn.commit()
        return "inserted"

    def upsert_org(self, org: Org) -> str:
        existing = self.conn.execute(
            "SELECT 1 FROM org WHERE org_id = ?", (org.org_id,)).fetchone()
        values = (org.org_id, org.name, org.country, org.region,
                  org.applicant_type, org.registry_id, org.status,
                  org.revenue, org.program_expense,
                  json.dumps(org.categories), org.mission, org.beneficiaries,
                  org.website, int(org.synthetic))
        if existing:
            self.conn.execute(
                "UPDATE org SET name=?, country=?, region=?, applicant_type=?, "
                "registry_id=?, status=?, revenue=?, program_expense=?, "
                "categories=?, mission=?, beneficiaries=?, website=?, synthetic=? "
                "WHERE org_id=?", (*values[1:], org.org_id))
            self.conn.commit()
            return "updated"
        self.conn.execute(
            "INSERT INTO org (org_id, name, country, region, applicant_type, "
            "registry_id, status, revenue, program_expense, categories, mission, "
            "beneficiaries, website, synthetic, first_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*values, utcnow()))
        self.conn.commit()
        return "inserted"

    @staticmethod
    def decode_org(row: sqlite3.Row) -> Org:
        return Org(
            org_id=row["org_id"], name=row["name"], country=row["country"],
            region=row["region"], applicant_type=row["applicant_type"],
            registry_id=row["registry_id"] or "", status=row["status"] or "",
            revenue=row["revenue"], program_expense=row["program_expense"],
            categories=json.loads(row["categories"] or "[]"),
            mission=row["mission"] or "", beneficiaries=row["beneficiaries"] or "",
            website=(row["website"] if "website" in row.keys() else "") or "",
            synthetic=bool(row["synthetic"]))

    def orgs(self) -> list[Org]:
        return [self.decode_org(r) for r in
                self.conn.execute("SELECT * FROM org ORDER BY name")]

    def find_org(self, needle: str) -> Org | None:
        row = self.conn.execute(
            "SELECT * FROM org WHERE org_id = ? OR name LIKE ? ORDER BY name LIMIT 1",
            (needle, f"%{needle}%")).fetchone()
        return self.decode_org(row) if row else None

    def all_opportunities(self) -> list[Opportunity]:
        return [self.decode(r) for r in
                self.conn.execute("SELECT * FROM opportunity")]

    def start_run(self, source: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO ingest_run (source, started_at) VALUES (?,?)",
            (source, utcnow()))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, counts: dict, note: str = "") -> None:
        self.conn.execute(
            "UPDATE ingest_run SET finished_at = ?, fetched = ?, inserted = ?, "
            "updated = ?, unchanged = ?, errors = ?, note = ? WHERE id = ?",
            (utcnow(), counts.get("fetched", 0), counts.get("inserted", 0),
             counts.get("updated", 0), counts.get("unchanged", 0),
             counts.get("errors", 0), note, run_id))
        self.conn.commit()

    def duplicate_groups(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT dedupe_key, COUNT(*) n, GROUP_CONCAT(uid, ' | ') uids "
            "FROM opportunity GROUP BY dedupe_key HAVING n > 1 ORDER BY n DESC"
        ).fetchall()

    def recent_changes(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT c.*, o.title FROM opportunity_change c "
            "LEFT JOIN opportunity o ON o.uid = c.uid "
            "ORDER BY c.id DESC LIMIT ?", (limit,)).fetchall()

    @staticmethod
    def decode(row: sqlite3.Row) -> Opportunity:
        """Rebuild an Opportunity from a stored row, for backfill passes."""
        return Opportunity(
            source=row["source"], native_id=row["native_id"],
            title=row["title"], funder_name=row["funder_name"],
            summary=row["summary"] or "",
            eligibility_raw=row["eligibility_raw"] or "",
            applicant_types=json.loads(row["applicant_types"] or "[]"),
            regions=json.loads(row["regions"] or "[]"),
            purposes=json.loads(row["purposes"] or "[]"),
            amount_min=row["amount_min"], amount_max=row["amount_max"],
            currency=row["currency"], deadline=row["deadline"],
            route=row["route"],
            match_required=None if row["match_required"] is None
            else bool(row["match_required"]),
            partner_required=None if row["partner_required"] is None
            else bool(row["partner_required"]),
            source_url=row["source_url"] or "",
            field_provenance=json.loads(row["field_provenance"] or "{}"),
            extraction_confidence=row["extraction_confidence"] or 0.0,
        )

    def needing_extraction(self, threshold: float = 0.7,
                           limit: int = 50) -> list[Opportunity]:
        rows = self.conn.execute(
            "SELECT * FROM opportunity WHERE extraction_confidence < ? "
            "AND eligibility_raw IS NOT NULL AND length(eligibility_raw) > 40 "
            "ORDER BY deadline LIMIT ?", (threshold, limit)).fetchall()
        return [self.decode(r) for r in rows]

    def counts(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]
        return {
            "opportunities": q("SELECT COUNT(*) FROM opportunity"),
            "awards": q("SELECT COUNT(*) FROM award"),
            "changes": q("SELECT COUNT(*) FROM opportunity_change"),
            "sources": q("SELECT COUNT(DISTINCT source) FROM opportunity"),
        }
