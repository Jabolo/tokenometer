"""Persistent, private, event-keyed ledger over verified capture runs.

Runs are immutable snapshots, so their totals are never added together. The
ledger stores each validated event version and its originating capture, then
counts at most one trusted version per event key. Conflicting versions remain
available for audit but are excluded from trusted totals.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone as dt_timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Sequence

from .aggregate import summarize
from .capture import verify_run
from .models import UsageEvent, utc_timestamp
from .receipts import validate_receipt


SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5_000
PROVIDERS = ("codex", "antigravity")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _private_parent(path: Path, create: bool) -> Path:
    parent = path.parent
    if parent.is_symlink():
        raise ValueError("Ledger parent must not be a symlink")
    if create:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent.exists():
        raise FileNotFoundError("Ledger parent does not exist")
    info = parent.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("Ledger parent must be an owner-only 0700 directory")
    return parent


def _private_database(path: Path, create: bool) -> None:
    _private_parent(path, create)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            raise
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            info = path.lstat()
        else:
            os.close(fd)
            info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Ledger must be an owner-owned regular file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("Existing ledger file must have mode 0600")
    if info.st_size == 0:
        if not create:
            raise FileNotFoundError("Ledger has not been initialized")
        # A 0600 empty file is the only safe recovery point after a process is
        # interrupted between exclusive creation and SQLite initialization.
        return
    _assert_existing_ledger_identity(path)


def _assert_existing_ledger_identity(path: Path) -> None:
    """Read-only identity check; rejects arbitrary databases before any write."""
    required = {"ledger_meta", "capture_runs", "run_locations", "event_versions",
                "run_events", "event_state"}
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if not required.issubset(tables):
                raise ValueError("Refusing to use a database without tokenometer ledger tables")
            meta = connection.execute(
                "SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()
            if meta is None or meta[0] not in {"1", str(SCHEMA_VERSION)}:
                raise ValueError("Refusing to use a database without a supported ledger marker")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(event_versions)")}
            if not {"event_key", "payload_json", "counters_signature", "owner_session_key",
                    "timestamp_utc", "platform"}.issubset(columns):
                raise ValueError("Existing tokenometer ledger schema is incomplete")
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise ValueError("Existing database is not a readable tokenometer ledger") from exc


def _connect(path: Path, create: bool) -> sqlite3.Connection:
    _private_database(path, create)
    connection = sqlite3.connect(os.fspath(path), timeout=BUSY_TIMEOUT_MS / 1000,
                                 isolation_level=None)
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _create_schema(connection: sqlite3.Connection) -> None:
    existing_tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if existing_tables and "ledger_meta" not in existing_tables:
        raise ValueError("File is not a tokenometer ledger")
    if existing_tables and not {"capture_runs", "run_locations", "event_versions",
                                "run_events", "event_state"}.issubset(existing_tables):
        raise ValueError("Ledger schema is incomplete")
    connection.execute("""CREATE TABLE IF NOT EXISTS ledger_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS capture_runs (
        run_id TEXT PRIMARY KEY,
        manifest_sha256 TEXT NOT NULL,
        collection_json TEXT NOT NULL,
        source_inventory_json TEXT NOT NULL,
        first_ingested_at TEXT NOT NULL,
        last_ingested_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS run_locations (
        run_id TEXT NOT NULL REFERENCES capture_runs(run_id),
        path TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        PRIMARY KEY (run_id, path)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS event_versions (
        version_id INTEGER PRIMARY KEY,
        event_key TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        counters_signature TEXT NOT NULL,
        owner_session_key TEXT NOT NULL,
        owner_aliases_json TEXT NOT NULL DEFAULT '[]',
        timestamp_utc TEXT,
        timestamp_evidence_json TEXT NOT NULL DEFAULT '[]',
        timestamp_evidence_ambiguous INTEGER NOT NULL DEFAULT 0,
        reasoning_value INTEGER,
        platform TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        UNIQUE (event_key, payload_sha256)
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS event_versions_key_idx ON event_versions(event_key)")
    connection.execute("""CREATE TABLE IF NOT EXISTS run_events (
        run_id TEXT NOT NULL REFERENCES capture_runs(run_id),
        event_key TEXT NOT NULL,
        version_id INTEGER NOT NULL REFERENCES event_versions(version_id),
        PRIMARY KEY (run_id, event_key, version_id)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS event_state (
        event_key TEXT PRIMARY KEY,
        conflicted INTEGER NOT NULL,
        counter_conflict INTEGER NOT NULL,
        owner_conflict INTEGER NOT NULL,
        timestamp_conflict INTEGER NOT NULL,
        timestamp_ambiguous INTEGER NOT NULL DEFAULT 0,
        reasoning_conflict INTEGER NOT NULL DEFAULT 0,
        chosen_version_id INTEGER REFERENCES event_versions(version_id),
        version_count INTEGER NOT NULL,
        timestamp_enriched INTEGER NOT NULL,
        reasoning_enriched INTEGER NOT NULL DEFAULT 0
    )""")
    row = connection.execute("SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()
    if row is None:
        if existing_tables:
            raise ValueError("Ledger metadata is missing")
        connection.execute("INSERT INTO ledger_meta(key,value) VALUES('schema_version',?)",
                           (str(SCHEMA_VERSION),))
    elif row[0] == "1":
        _migrate_v1_to_v2(connection)
    elif row[0] != str(SCHEMA_VERSION):
        raise ValueError("Unsupported ledger schema version")


def _event_timestamp_evidence(event: UsageEvent) -> tuple[str, bool]:
    values = []
    if event.platform == "antigravity":
        values = event.counter_receipt.get("source_timestamps", [])
    elif event.timestamp is not None:
        values = [event.timestamp]
    known = sorted({utc_timestamp(value) for value in values if value is not None})
    return _json(known), len(known) > 1


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Add receipt-derived evidence columns and recompute trusted state atomically."""
    additions = {
        "event_versions": {
            "owner_aliases_json": "TEXT NOT NULL DEFAULT '[]'",
            "timestamp_evidence_json": "TEXT NOT NULL DEFAULT '[]'",
            "timestamp_evidence_ambiguous": "INTEGER NOT NULL DEFAULT 0",
            "reasoning_value": "INTEGER",
        },
        "event_state": {
            "timestamp_ambiguous": "INTEGER NOT NULL DEFAULT 0",
            "reasoning_conflict": "INTEGER NOT NULL DEFAULT 0",
            "reasoning_enriched": "INTEGER NOT NULL DEFAULT 0",
        },
    }
    for table, columns in additions.items():
        present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in present:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    last_version_id = 0
    while True:
        rows = connection.execute("""SELECT version_id,payload_json FROM event_versions
            WHERE version_id>? ORDER BY version_id LIMIT 500""", (last_version_id,)).fetchall()
        if not rows:
            break
        for version_id, payload_json in rows:
            event = UsageEvent(**json.loads(payload_json))
            validate_receipt(event)
            timestamp = utc_timestamp(event.timestamp) if event.timestamp is not None else None
            evidence_json, ambiguous = _event_timestamp_evidence(event)
            counters_signature = _json([
                event.fresh_input, event.cache_read, event.cache_write, event.output,
            ])
            connection.execute("""UPDATE event_versions SET counters_signature=?,
                owner_aliases_json=?,timestamp_evidence_json=?,timestamp_evidence_ambiguous=?,
                reasoning_value=?,timestamp_utc=? WHERE version_id=?""",
                (counters_signature, _json(event.session_aliases), evidence_json,
                 int(ambiguous), event.reasoning, timestamp, version_id))
        last_version_id = rows[-1][0]
    connection.execute("UPDATE ledger_meta SET value=? WHERE key='schema_version'",
                       (str(SCHEMA_VERSION),))
    _refresh_event_state(connection, None)


def _validate_scope(collection: dict) -> None:
    parameters = collection.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("Capture has no valid scope parameters")
    if collection.get("since") is not None:
        raise ValueError("Global ledger accepts all-history captures only")
    if collection.get("project_filter") is not None:
        raise ValueError("Global ledger rejects project-filtered captures")
    if parameters.get("exclude_session_hashes") != []:
        raise ValueError("Global ledger rejects captures with excluded sessions")
    providers = collection.get("providers")
    if not isinstance(providers, dict) or not set(providers).issubset(PROVIDERS):
        raise ValueError("Capture has an invalid provider scope")
    if not providers:
        raise ValueError("Capture has no provider scope")
    if parameters.get("platforms") != sorted(providers):
        raise ValueError("Capture provider scope differs from its parameters")
    if parameters.get("snapshot_mode") != "full" or parameters.get("network") is not False:
        raise ValueError("Capture is not a full local snapshot")


def _capture_time(value, name: str) -> str:
    normalized = utc_timestamp(value)
    if normalized is None:
        raise ValueError(f"Capture {name} requires an explicit timezone")
    return normalized


def _prepare_run(run_path: Path) -> dict:
    """Verify bytes before reading or accepting any capture metadata."""
    path = Path(run_path)
    verify_run(path)
    collection = json.loads((path / "collection.json").read_text(encoding="utf-8"))
    _validate_scope(collection)
    completed_at = _capture_time(collection.get("completed_at"), "completion time")
    cutoff = _capture_time(collection.get("cutoff"), "cutoff")
    manifest_bytes = (path / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    source_inventory = json.loads((path / "sources.private.json").read_text(encoding="utf-8"))
    if not isinstance(source_inventory, dict):
        raise ValueError("Capture source inventory must be an object")
    # Hash the validated manifest structure so harmless JSON formatting changes
    # in a copied capture do not create a second lineage identity.
    run_id = hashlib.sha256(_json(manifest).encode("utf-8")).hexdigest()
    return {
        "path": path,
        "location": os.fspath(path.resolve()),
        "run_id": run_id,
        "manifest_sha256": run_id,
        "collection": collection,
        "collection_json": _json(collection),
        "source_inventory_json": _json(source_inventory),
        "completed_at": completed_at,
        "cutoff": cutoff,
    }


def _record_event(connection: sqlite3.Connection, run_id: str, event: UsageEvent,
                  observed_at: str, touched: set[str]) -> tuple[int, int]:
    # verify_run validates every receipt before this transaction; validate again
    # at the point where its timestamp can enrich an older undated observation.
    validate_receipt(event)
    payload_json = _json(event.to_dict())
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    counters_signature = _json([
        event.fresh_input, event.cache_read, event.cache_write, event.output,
    ])
    timestamp = utc_timestamp(event.timestamp) if event.timestamp is not None else None
    timestamp_evidence_json, timestamp_ambiguous = _event_timestamp_evidence(event)
    cursor = connection.execute(
        """INSERT OR IGNORE INTO event_versions
        (event_key,payload_sha256,payload_json,counters_signature,owner_session_key,
         owner_aliases_json,timestamp_utc,timestamp_evidence_json,
         timestamp_evidence_ambiguous,reasoning_value,platform,first_seen_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event.key, payload_hash, payload_json, counters_signature,
         event.session_key, _json(event.session_aliases), timestamp,
         timestamp_evidence_json, int(timestamp_ambiguous), event.reasoning,
         event.platform, observed_at),
    )
    inserted_version = int(cursor.rowcount == 1)
    row = connection.execute(
        "SELECT version_id FROM event_versions WHERE event_key=? AND payload_sha256=?",
        (event.key, payload_hash),
    ).fetchone()
    if row is None:
        raise sqlite3.DatabaseError("Event version insert could not be read back")
    version_id = int(row[0])
    cursor = connection.execute(
        "INSERT OR IGNORE INTO run_events(run_id,event_key,version_id) VALUES(?,?,?)",
        (run_id, event.key, version_id),
    )
    inserted_link = int(cursor.rowcount == 1)
    touched.add(event.key)
    return inserted_version, inserted_link


def _refresh_event_state(connection: sqlite3.Connection, touched: set[str] | None) -> None:
    if touched is not None and not touched:
        return
    connection.execute("CREATE TEMP TABLE IF NOT EXISTS touched_event_keys (event_key TEXT PRIMARY KEY)")
    if touched is None:
        connection.execute("INSERT OR IGNORE INTO touched_event_keys(event_key) SELECT DISTINCT event_key FROM event_versions")
    else:
        connection.executemany("INSERT OR IGNORE INTO touched_event_keys(event_key) VALUES(?)",
                               ((key,) for key in touched))
    connection.execute("CREATE TEMP TABLE IF NOT EXISTS owner_conflict_keys (event_key TEXT PRIMARY KEY)")
    connection.execute("DELETE FROM owner_conflict_keys")
    differing_owner_keys = connection.execute("""SELECT v.event_key
        FROM event_versions v JOIN touched_event_keys t USING(event_key)
        GROUP BY v.event_key HAVING COUNT(DISTINCT v.owner_aliases_json)>1""").fetchall()
    for (key,) in differing_owner_keys:
        owner_sets = [set(json.loads(row[0])) for row in connection.execute(
            "SELECT DISTINCT owner_aliases_json FROM event_versions WHERE event_key=?", (key,))]
        if any(not (left <= right or right <= left)
               for index, left in enumerate(owner_sets) for right in owner_sets[index + 1:]):
            connection.execute("INSERT INTO owner_conflict_keys(event_key) VALUES(?)", (key,))
    connection.execute("""WITH grouped AS (
        SELECT v.event_key,
               COUNT(DISTINCT v.counters_signature) > 1 AS counter_conflict,
               EXISTS(SELECT 1 FROM owner_conflict_keys o WHERE o.event_key=v.event_key) AS owner_conflict,
               COUNT(DISTINCT v.timestamp_utc) > 1 AS timestamp_conflict,
               MAX(v.timestamp_evidence_ambiguous) AS timestamp_ambiguous,
               COUNT(DISTINCT v.reasoning_value) > 1 AS reasoning_conflict,
               COUNT(*) AS version_count,
               COUNT(v.timestamp_utc) AS dated_version_count,
               COUNT(DISTINCT v.timestamp_utc) AS distinct_timestamp_count,
               COUNT(v.reasoning_value) AS known_reasoning_count,
               COUNT(*) AS total_version_count
          FROM event_versions v JOIN touched_event_keys t USING(event_key)
         GROUP BY v.event_key
    ), resolved AS (
        SELECT g.*,
               (SELECT v.version_id FROM event_versions v
                 WHERE v.event_key=g.event_key
                 ORDER BY (v.timestamp_utc IS NOT NULL) DESC, v.payload_sha256 ASC
                 LIMIT 1) AS chosen_version_id
          FROM grouped g
    )
    INSERT INTO event_state(event_key,conflicted,counter_conflict,owner_conflict,
                            timestamp_conflict,timestamp_ambiguous,reasoning_conflict,
                            chosen_version_id,version_count,timestamp_enriched,
                            reasoning_enriched)
    SELECT event_key,
           (counter_conflict OR owner_conflict OR timestamp_conflict OR reasoning_conflict),
           (counter_conflict OR reasoning_conflict), owner_conflict, timestamp_conflict,
           timestamp_ambiguous, reasoning_conflict, chosen_version_id, version_count,
           (dated_version_count > 0 AND dated_version_count < version_count
             AND distinct_timestamp_count = 1
             AND NOT timestamp_ambiguous AND NOT counter_conflict AND NOT owner_conflict
             AND NOT timestamp_conflict AND NOT reasoning_conflict),
           (known_reasoning_count > 0 AND known_reasoning_count < total_version_count
             AND NOT counter_conflict AND NOT owner_conflict AND NOT timestamp_conflict
             AND NOT reasoning_conflict)
      FROM resolved WHERE 1
    ON CONFLICT(event_key) DO UPDATE SET
        conflicted=excluded.conflicted,
        counter_conflict=excluded.counter_conflict,
        owner_conflict=excluded.owner_conflict,
        timestamp_conflict=excluded.timestamp_conflict,
        timestamp_ambiguous=excluded.timestamp_ambiguous,
        reasoning_conflict=excluded.reasoning_conflict,
        chosen_version_id=excluded.chosen_version_id,
        version_count=excluded.version_count,
        timestamp_enriched=excluded.timestamp_enriched,
        reasoning_enriched=excluded.reasoning_enriched""")
    connection.execute("DELETE FROM touched_event_keys")
    connection.execute("DELETE FROM owner_conflict_keys")


def ingest_runs(ledger_path: Path, run_paths: Sequence[Path]) -> dict:
    """Ingest verified all-history snapshots atomically and idempotently.

    No source capture is modified. The ledger records private run locations,
    complete receipt versions, and run-to-event lineage. Repeated snapshots or
    copied run directories add no token totals; removed source files never
    delete already ingested events.
    """
    if isinstance(run_paths, (str, bytes, Path)):
        raise TypeError("run_paths must be a sequence of capture paths")
    run_paths = list(run_paths)
    prepared_by_id: dict[str, dict] = {}
    locations_by_id: dict[str, set[str]] = {}
    for value in run_paths:
        prepared = _prepare_run(Path(value))
        run_id = prepared["run_id"]
        existing = prepared_by_id.get(run_id)
        if existing is not None and (existing["collection_json"] != prepared["collection_json"]
                                     or existing["source_inventory_json"] != prepared["source_inventory_json"]):
            raise ValueError("Capture manifest identity collision")
        prepared_by_id.setdefault(run_id, prepared)
        locations_by_id.setdefault(run_id, set()).add(prepared["location"])

    ledger = Path(ledger_path)
    connection = _connect(ledger, create=True)
    observed_at = datetime.now(dt_timezone.utc).isoformat().replace("+00:00", "Z")
    inserted_runs = 0
    inserted_locations = 0
    inserted_versions = 0
    linked_observations = 0
    read_events = 0
    touched: set[str] = set()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _create_schema(connection)
        for run_id, prepared in prepared_by_id.items():
            cursor = connection.execute(
                """INSERT OR IGNORE INTO capture_runs
                (run_id,manifest_sha256,collection_json,source_inventory_json,
                 first_ingested_at,last_ingested_at)
                VALUES(?,?,?,?,?,?)""",
                (run_id, prepared["manifest_sha256"], prepared["collection_json"],
                 prepared["source_inventory_json"], observed_at, observed_at),
            )
            inserted_runs += int(cursor.rowcount == 1)
            if cursor.rowcount == 0:
                stored = connection.execute(
                    "SELECT collection_json,source_inventory_json FROM capture_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if stored != (prepared["collection_json"], prepared["source_inventory_json"]):
                    raise ValueError("Capture metadata changed for an existing manifest")
                connection.execute("UPDATE capture_runs SET last_ingested_at=? WHERE run_id=?",
                                   (observed_at, run_id))
            for location in sorted(locations_by_id[run_id]):
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO run_locations(run_id,path,first_seen_at) VALUES(?,?,?)",
                    (run_id, location, observed_at),
                )
                inserted_locations += int(cursor.rowcount == 1)

        # Stream each unique verified run; any failure rolls the complete batch back.
        for run_id, prepared in prepared_by_id.items():
            with (prepared["path"] / "events.jsonl").open(encoding="utf-8") as source:
                for line in source:
                    if not line.strip():
                        continue
                    event = UsageEvent(**json.loads(line))
                    read_events += 1
                    added, linked = _record_event(connection, run_id, event, observed_at, touched)
                    inserted_versions += added
                    linked_observations += linked
        _refresh_event_state(connection, touched)
        conflicted_keys = int(connection.execute(
            "SELECT COUNT(*) FROM event_state WHERE conflicted=1"
        ).fetchone()[0])
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()

    return {
        "verified_runs": len(run_paths),
        "new_runs": inserted_runs,
        "reused_runs": max(0, len(run_paths) - inserted_runs),
        "new_run_locations": inserted_locations,
        "new_event_versions": inserted_versions,
        "reused_event_versions": read_events - inserted_versions,
        "new_run_event_links": linked_observations,
        "reused_run_event_links": read_events - linked_observations,
        "conflicted_event_keys": conflicted_keys,
    }


def _safe_count(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _provider_file_counts(provider: str, stats: dict) -> tuple[int | None, int | None]:
    if provider == "codex":
        # files_read already includes compressed sessions; adding compressed_* counts
        # would count them twice.
        return _safe_count(stats.get("files_read")), _safe_count(stats.get("files_discovered"))
    inventory = stats.get("source_inventory") if isinstance(stats.get("source_inventory"), dict) else {}
    return _safe_count(inventory.get("databases_read")), _safe_count(inventory.get("sqlite_candidates"))


def _provider_coverage_status(stats: dict, warnings) -> str:
    errors = _safe_count(stats.get("errors"))
    warning_count = len(warnings) if isinstance(warnings, list) else None
    incomplete_keys = (
        "incomplete", "incomplete_events", "malformed_records", "undated_events",
        "timestamp_conflict_events", "conflicting_events_excluded", "resets",
        "metadata_incomplete", "legacy_unverified", "discovery_errors", "read_errors",
        "schema_errors",
    )
    incomplete = any(
        (value is True or (type(value) is int and value > 0)) if key == "incomplete" else
        (type(value) is int and value > 0)
        for key in incomplete_keys if (value := stats.get(key)) is not None
    )
    missing_sources = stats.get("missing_sources")
    if isinstance(missing_sources, list):
        incomplete = incomplete or bool(missing_sources)
    elif type(missing_sources) is int:
        incomplete = incomplete or missing_sources > 0
    if errors is None:
        return "partial" if incomplete or warning_count else "unknown"
    if errors > 0 or incomplete or (warning_count is not None and warning_count > 0):
        return "partial"
    return "qualified"


def _provider_scope(run: dict, provider: str, provider_data: dict) -> dict:
    collection = run["collection"]
    stats = provider_data.get("stats") if isinstance(provider_data.get("stats"), dict) else {}
    warnings = provider_data.get("warnings")
    warning_count = len(warnings) if isinstance(warnings, list) else None
    errors = _safe_count(stats.get("errors"))
    execution_status = "complete" if errors == 0 else "partial" if errors is not None else "unknown"
    files_read, files_discovered = _provider_file_counts(provider, stats)
    return {
        "platform_scope": [provider],
        "since": collection.get("since"),
        "project_filter": collection.get("project_filter"),
        "excluded_session_count": len(collection.get("parameters", {}).get("exclude_session_hashes", [])),
        "snapshot_mode": collection.get("parameters", {}).get("snapshot_mode"),
        "network": collection.get("parameters", {}).get("network"),
        "latest_cutoff": run["cutoff"],
        "latest_capture_completed_at": run["completed_at"],
        "execution_status": execution_status,
        "coverage_status": _provider_coverage_status(stats, warnings),
        "errors": errors,
        "warning_count": warning_count,
        "source_files_read": files_read,
        "source_files_discovered": files_discovered,
    }


def _load_runs(connection: sqlite3.Connection) -> list[dict]:
    runs = []
    for run_id, collection_json, inventory_json in connection.execute(
            "SELECT run_id,collection_json,source_inventory_json FROM capture_runs"):
        collection = json.loads(collection_json)
        runs.append({
            "run_id": run_id,
            "collection": collection,
            "source_inventory": json.loads(inventory_json),
            "completed_at": _capture_time(collection.get("completed_at"), "completion time"),
            "cutoff": _capture_time(collection.get("cutoff"), "cutoff"),
        })
    return runs


def _trusted_events(connection: sqlite3.Connection) -> list[UsageEvent]:
    events: list[UsageEvent] = []
    current_key = None
    chosen_payload = None
    aliases: set[str] = set()
    known_reasoning: set[int] = set()
    timestamp_ambiguous = False

    def finish_group():
        if chosen_payload is None:
            return
        event = UsageEvent(**json.loads(chosen_payload))
        # Alias additions are monotone source lineage; they do not alter counters
        # or create a conflict. Keep all aliases while retaining each raw version.
        merged_aliases = tuple(sorted(aliases | set(event.session_aliases)))
        updates = {
            "session_key": merged_aliases[0],
            "session_aliases": merged_aliases,
        }
        if timestamp_ambiguous:
            updates["timestamp"] = None
        if event.reasoning is None and known_reasoning:
            updates["reasoning"] = next(iter(known_reasoning))
        events.append(replace(event, **updates))

    cursor = connection.execute("""SELECT v.event_key,v.payload_json,
        (v.version_id=s.chosen_version_id) AS chosen,s.timestamp_ambiguous
        FROM event_state s JOIN event_versions v ON v.event_key=s.event_key
        WHERE s.conflicted=0 ORDER BY v.event_key,v.version_id""")
    for key, payload, chosen, ambiguous in cursor:
        if key != current_key:
            finish_group()
            current_key = key
            chosen_payload = None
            aliases = set()
            known_reasoning = set()
            timestamp_ambiguous = False
        event_version = UsageEvent(**json.loads(payload))
        aliases.update(event_version.session_aliases)
        if event_version.reasoning is not None:
            known_reasoning.add(event_version.reasoning)
        timestamp_ambiguous = timestamp_ambiguous or bool(ambiguous)
        if chosen:
            chosen_payload = payload
    finish_group()
    return events


def ledger_snapshot(ledger_path: Path, timezone: str = "Europe/Warsaw") -> dict:
    """Return aggregate totals with explicit quality, provider scope and freshness.

    The result intentionally omits capture paths, source references, event keys,
    session hashes, account fingerprints, and raw warning text.
    """
    ledger = Path(ledger_path)
    connection = _connect(ledger, create=False)
    try:
        connection.execute("BEGIN")
        try:
            _create_schema(connection)
            version = connection.execute("SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()
        except sqlite3.OperationalError as exc:
            raise ValueError("File is not a tokenometer ledger") from exc
        if version is None or version[0] != str(SCHEMA_VERSION):
            raise ValueError("Unsupported ledger schema version")

        events = _trusted_events(connection)
        summary = summarize(events, timezone)
        run_rows = _load_runs(connection)
        quality_rows = connection.execute("""SELECT
            SUM(CASE WHEN conflicted=0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN conflicted=1 THEN 1 ELSE 0 END),
            SUM(version_count), SUM(timestamp_enriched),
            SUM(counter_conflict), SUM(owner_conflict), SUM(timestamp_conflict),
            SUM(timestamp_ambiguous), SUM(reasoning_conflict), SUM(reasoning_enriched)
            FROM event_state""").fetchone()
        (trusted_count, conflict_count, version_count, enriched_count,
         counter_conflicts, owner_conflicts, time_conflicts, ambiguous_count,
         reasoning_conflicts, reasoning_enrichments) = (
            int(value or 0) for value in quality_rows
        )

        captures: dict[str, list[tuple[dict, dict]]] = {provider: [] for provider in PROVIDERS}
        execution_counts = {"complete": 0, "partial": 0}
        coverage_counts = {"qualified": 0, "partial": 0}
        for run in run_rows:
            collection = run["collection"]
            execution = collection.get("execution_status")
            coverage = collection.get("coverage_status")
            if execution in execution_counts:
                execution_counts[execution] += 1
            if coverage in coverage_counts:
                coverage_counts[coverage] += 1
            for provider, provider_data in collection.get("providers", {}).items():
                if provider in captures and isinstance(provider_data, dict):
                    captures[provider].append((run, provider_data))

        now = datetime.now(dt_timezone.utc)
        provider_snapshot = {}
        freshness = {}
        any_unobserved = False
        any_partial_scope = False
        latest_provider_errors = {}
        for provider in PROVIDERS:
            rows = sorted(captures[provider], key=lambda item: (
                item[0]["completed_at"], item[0]["cutoff"], item[0]["run_id"]
            ))
            if not rows:
                any_unobserved = True
                any_partial_scope = True
                provider_snapshot[provider] = {
                    "capture_count": 0,
                    "latest_cutoff": None,
                    "latest_capture_completed_at": None,
                    "last_successful_cutoff": None,
                    "last_successful_capture_completed_at": None,
                    "execution_status": "unobserved",
                    "coverage_status": "unobserved",
                    "errors": None,
                    "warning_count": None,
                    "platform_scope": [],
                    "latest_since": None,
                    "latest_excluded_session_count": None,
                    "source_files_read": None,
                    "source_files_discovered": None,
                }
                freshness[provider] = {
                    "status": "never_captured",
                    "latest_capture_completed_at": None,
                    "age_seconds": None,
                }
                latest_provider_errors[provider] = None
                continue

            latest_run, latest_data = rows[-1]
            latest = _provider_scope(latest_run, provider, latest_data)
            latest_success = None
            for run, provider_data in rows:
                stats = provider_data.get("stats") if isinstance(provider_data.get("stats"), dict) else {}
                if _safe_count(stats.get("errors")) == 0:
                    latest_success = (run, provider_data)
            if latest_success:
                successful_run, _ = latest_success
                last_successful_cutoff = successful_run["cutoff"]
                last_successful_completed = successful_run["completed_at"]
            else:
                last_successful_cutoff = None
                last_successful_completed = None
            latest_errors = latest["errors"]
            latest_provider_errors[provider] = latest_errors
            provider_snapshot[provider] = {
                "capture_count": len(rows),
                **latest,
                "last_successful_cutoff": last_successful_cutoff,
                "last_successful_capture_completed_at": last_successful_completed,
                "latest_since": latest["since"],
                "latest_excluded_session_count": latest["excluded_session_count"],
            }
            if latest["execution_status"] != "complete" or latest["coverage_status"] != "qualified" or latest_errors != 0:
                any_partial_scope = True
            completed = datetime.fromisoformat(latest["latest_capture_completed_at"].replace("Z", "+00:00"))
            age_seconds = max(0, int((now - completed).total_seconds()))
            freshness[provider] = {
                "status": "captured",
                "latest_capture_completed_at": latest["latest_capture_completed_at"],
                "age_seconds": age_seconds,
                "future_completion_time": completed > now,
                "last_successful_capture_completed_at": last_successful_completed,
            }

        coverage_status = "partial" if any_unobserved or any_partial_scope else "qualified"
        quality_status = "partial" if conflict_count or coverage_status == "partial" else "qualified"
        total_locations = connection.execute("SELECT COUNT(*) FROM run_locations").fetchone()[0]
        result = {
            "summary": summary,
            "quality": {
                "status": quality_status,
                "coverage_status": coverage_status,
                "historical_account_completeness": "unknown",
                "trusted_events": trusted_count,
                "conflicted_events": conflict_count,
                "event_versions": version_count,
                "timestamp_enrichments": enriched_count,
                "ambiguous_timestamp_events": ambiguous_count,
                "reasoning_enrichments": reasoning_enrichments,
                "ingested_runs": len(run_rows),
                "known_run_locations": int(total_locations),
            },
            "providers": provider_snapshot,
            "source_freshness": freshness,
            "diagnostics": {
                "capture_execution_status_counts": execution_counts,
                "capture_coverage_status_counts": coverage_counts,
                "conflict_reasons": {
                    "counters": int(counter_conflicts or 0),
                    "owner": int(owner_conflicts or 0),
                    "timestamp": int(time_conflicts or 0),
                },
                "reasoning_conflicts": int(reasoning_conflicts or 0),
                "providers_never_captured": [p for p in PROVIDERS if not captures[p]],
                "latest_provider_errors": latest_provider_errors,
            },
        }
        connection.execute("COMMIT")
        return result
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
