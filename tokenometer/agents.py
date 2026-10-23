"""Private, read-only collection and persistence of agent-session lineage.

Agent sessions are not usage events: this module keeps their identities and
source evidence separate from token owners and usage aggregation.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from tokenometer.collectors import antigravity, codex
from tokenometer.models import digest, utc_timestamp


ENVELOPE_VERSION = 1
PROVIDERS = ("codex", "antigravity")
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_DIAGNOSTIC_KEYS = (
    "roots_supplied",
    "roots_missing",
    "missing_history_areas",
    "discovery_errors",
    "sources_scanned",
    "read_errors",
    "schema_errors",
    "malformed_records",
    "undated_child_records",
    "future_records",
    "candidate_records",
    "duplicate_records",
    "conflicting_parent_sessions",
    "missing_summary_indexes",
)
_RUN_TABLE = "agent_runs"
_VERSION_TABLE = "agent_versions"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: object, *, name: str) -> str:
    normalized = utc_timestamp(value)
    if normalized is None or not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{name} must be a UTC timestamp ending in Z")
    if normalized != value:
        raise ValueError(f"{name} must use canonical UTC form")
    return normalized


def _datetime_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _cutoff(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cutoff must be a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash_id(provider: str, value: object) -> str | None:
    valid = codex._valid_id(value)
    if valid is None:
        return None
    if provider == "antigravity":
        return digest("antigravity-session-id:" + valid)
    return digest(valid)


def _source_ref(path: Path) -> str:
    return digest(str(codex._safe_resolve(path)))


def _diagnostics() -> dict[str, int]:
    return {key: 0 for key in _DIAGNOSTIC_KEYS}


def _source_reference(source: str, kind: str, index: int) -> dict[str, Any]:
    return {"source": source, "kind": kind, "index": index}


def _codex_records(roots: list[Path], cutoff_dt: datetime, meta: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostics = meta["diagnostics"]
    files, missing, areas, unique_roots, discovery_errors = codex._discover_files(roots)
    diagnostics["roots_supplied"] = unique_roots
    diagnostics["roots_missing"] = sum(1 for root in roots if not Path(root).is_dir())
    diagnostics["missing_history_areas"] = max(0, missing - diagnostics["roots_missing"])
    diagnostics["discovery_errors"] = discovery_errors

    # Key observations by stable child/parent/start identity; source references
    # are unioned so copied/profile/archive records remain auditable but do not
    # create additive sessions.
    observations: dict[tuple[str, str | None, str], set[tuple[str, int]]] = defaultdict(set)
    parents_by_child: dict[str, set[str]] = defaultdict(set)

    for path, _area in files:
        diagnostics["sources_scanned"] += 1
        source_hash = _source_ref(path)
        try:
            with codex._open_text(path) as stream:
                for line_index, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        diagnostics["malformed_records"] += 1
                        continue
                    if not isinstance(obj, dict):
                        diagnostics["malformed_records"] += 1
                        continue
                    if obj.get("type") != "session_meta":
                        continue
                    payload = obj.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    source = payload.get("source")
                    subagent = source.get("subagent") if isinstance(source, dict) else None
                    thread_spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
                    if not isinstance(thread_spawn, dict):
                        continue

                    diagnostics["candidate_records"] += 1
                    raw_thread_id = next((codex._valid_id(value) for value in (
                        payload.get("thread_id"), payload.get("id"), payload.get("session_id"),
                        obj.get("thread_id"), obj.get("session_id"),
                    ) if codex._valid_id(value)), None)
                    child_hash = _hash_id("codex", raw_thread_id)
                    if child_hash is None:
                        diagnostics["schema_errors"] += 1
                        continue

                    time_value = obj.get("timestamp")
                    if time_value is None:
                        time_value = payload.get("timestamp")
                    born = codex._parse_timestamp(time_value)
                    if born is None:
                        diagnostics["undated_child_records"] += 1
                        continue
                    born = born.astimezone(timezone.utc)
                    if born > cutoff_dt:
                        diagnostics["future_records"] += 1
                        continue
                    created_at = born.isoformat().replace("+00:00", "Z")

                    raw_parents = {
                        value for value in (
                            codex._valid_id(thread_spawn.get("parent_thread_id")),
                            codex._valid_id(thread_spawn.get("parent_id")),
                            codex._valid_id(payload.get("parent_thread_id")),
                            codex._valid_id(payload.get("parent_id")),
                            codex._valid_id(obj.get("parent_thread_id")),
                            codex._valid_id(obj.get("parent_id")),
                        ) if value is not None
                    }
                    parent_hashes = {digest(value) for value in raw_parents}
                    if not parent_hashes:
                        # A typed child marker without a known parent is not
                        # sufficient to distinguish this from an unlinked fork.
                        diagnostics["schema_errors"] += 1
                        continue
                    parents: tuple[str | None, ...] = tuple(sorted(parent_hashes))
                    parents_by_child[child_hash].update(parent_hashes)
                    source_ref = (source_hash, line_index)
                    for parent_hash in parents:
                        key = (child_hash, parent_hash, created_at)
                        if observations[key]:
                            diagnostics["duplicate_records"] += 1
                        observations[key].add(source_ref)
        except (OSError, UnicodeError):
            diagnostics["read_errors"] += 1

    for child_hash, parents in parents_by_child.items():
        if len(parents) > 1:
            diagnostics["conflicting_parent_sessions"] += 1

    if not files and diagnostics["read_errors"] == 0 and diagnostics["discovery_errors"] == 0:
        meta["execution_status"] = "partial" if diagnostics["roots_missing"] else "unobserved"

    records: list[dict[str, Any]] = []
    for (child_hash, parent_hash, created_at), source_refs in sorted(observations.items()):
        records.append({
            "session_id_sha256": child_hash,
            "parent_id_sha256": parent_hash,
            "created_at": created_at,
            "source_refs": [
                _source_reference(source, "jsonl", index)
                for source, index in sorted(source_refs)
            ],
        })
    return records


def _antigravity_records(roots: list[Path], meta: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostics = meta["diagnostics"]
    diagnostics["roots_supplied"] = len(roots)
    candidates: dict[Path, None] = {}
    for root_value in roots:
        root = Path(root_value).expanduser()
        try:
            resolved = root.resolve()
            if not resolved.exists():
                diagnostics["roots_missing"] += 1
                continue
            for path in antigravity._candidate_files(resolved):
                candidates[path.resolve()] = None
        except OSError:
            diagnostics["discovery_errors"] += 1

    # The documented summary index is explicitly named and is the only source
    # of parent/depth/agent-name lineage. Trajectory databases are not searched
    # for prompt text or guessed subagent markers.
    indexes = sorted(path for path in candidates if path.name.casefold() == "conversation_summaries.db")
    if not indexes:
        diagnostics["missing_summary_indexes"] = 1
        meta["execution_status"] = "unobserved" if diagnostics["roots_missing"] == 0 and not roots else "partial"
        return []

    # Preserve metadata by identity and parent. Parent collisions remain as
    # distinct versions for ingestion to quarantine; same-parent copies merge.
    observations: dict[tuple[str, str], dict[str, Any]] = {}
    parents_by_child: dict[str, set[str]] = defaultdict(set)
    for path in indexes:
        diagnostics["sources_scanned"] += 1
        source_hash = _source_ref(path)
        snapshot = None
        try:
            snapshot = antigravity._readonly_snapshot(path)
            tables = {str(row[0]) for row in snapshot.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "conversation_summaries" not in tables:
                diagnostics["schema_errors"] += 1
                continue
            columns = antigravity._table_columns(snapshot, "conversation_summaries")
            required = {"conversation_id", "parent_conversation_id", "nesting_depth", "agent_name"}
            if not required.issubset(columns):
                diagnostics["schema_errors"] += 1
                continue
            query = (
                'SELECT "conversation_id", "parent_conversation_id", '
                '"nesting_depth", "agent_name" FROM "conversation_summaries" ORDER BY rowid'
            )
            for row_index, row in enumerate(snapshot.execute(query)):
                conversation_id = codex._valid_id(row["conversation_id"])
                parent_id = codex._valid_id(row["parent_conversation_id"])
                depth = row["nesting_depth"]
                agent_name = row["agent_name"]
                if conversation_id is None or parent_id is None:
                    continue
                if type(depth) is not int or depth <= 0:
                    continue
                if not isinstance(agent_name, str) or not agent_name.strip():
                    continue
                diagnostics["candidate_records"] += 1
                child_hash = _hash_id("antigravity", conversation_id)
                parent_hash = _hash_id("antigravity", parent_id)
                if child_hash is None or parent_hash is None:
                    diagnostics["schema_errors"] += 1
                    continue
                parents_by_child[child_hash].add(parent_hash)
                key = (child_hash, parent_hash)
                record = observations.get(key)
                source_ref = _source_reference(source_hash, "summary_index", row_index)
                if record is None:
                    observations[key] = {
                        "session_id_sha256": child_hash,
                        "parent_id_sha256": parent_hash,
                        "created_at": None,
                        "depth": depth,
                        "agent_name_present": True,
                        "source_refs": [source_ref],
                        "_source_ref_keys": {(source_hash, row_index)},
                        "_depths": {depth},
                    }
                else:
                    source_key = (source_hash, row_index)
                    if source_key in record["_source_ref_keys"]:
                        diagnostics["duplicate_records"] += 1
                    else:
                        record["_source_ref_keys"].add(source_key)
                        record["source_refs"].append(source_ref)
                    record["_depths"].add(depth)
                    record["depth"] = min(record["_depths"])
        except (OSError, sqlite3.Error, TimeoutError):
            diagnostics["read_errors"] += 1
        finally:
            if snapshot is not None:
                snapshot.close()

    diagnostics["conflicting_parent_sessions"] = sum(
        1 for parents in parents_by_child.values() if len(parents) > 1
    )
    diagnostics["duplicate_records"] = max(
        diagnostics["duplicate_records"],
        diagnostics["candidate_records"] - len(observations),
    )
    records = []
    for record in observations.values():
        records.append({key: value for key, value in record.items() if not key.startswith("_")})
    records.sort(key=lambda record: (
        record["session_id_sha256"], record["parent_id_sha256"], record["depth"]
    ))
    return records


def collect(roots: dict[str, list[Path]], cutoff: datetime) -> dict[str, Any]:
    """Read selected local provider roots and return a validated private envelope."""
    if not isinstance(roots, dict) or any(key not in PROVIDERS for key in roots):
        raise ValueError("roots must map selected provider names to root lists")
    cutoff_text = _cutoff(cutoff)
    cutoff_dt = datetime.fromisoformat(cutoff_text.replace("Z", "+00:00"))
    providers: dict[str, Any] = {}
    for provider in PROVIDERS:
        if provider not in roots:
            continue
        provider_roots = roots[provider]
        if not isinstance(provider_roots, list) or any(not isinstance(value, (Path, str)) for value in provider_roots):
            raise ValueError("provider roots must be lists of paths")
        meta: dict[str, Any] = {
            "execution_status": "complete",
            "diagnostics": _diagnostics(),
            "records": [],
        }
        if not provider_roots:
            meta["execution_status"] = "unobserved"
            meta["diagnostics"]["roots_missing"] = 1
        elif provider == "codex":
            meta["records"] = _codex_records([Path(value) for value in provider_roots], cutoff_dt, meta)
        else:
            meta["records"] = _antigravity_records([Path(value) for value in provider_roots], meta)

        incomplete_keys = (
            "roots_missing", "discovery_errors", "read_errors",
            "schema_errors", "malformed_records", "undated_child_records", "missing_summary_indexes",
        )
        if meta["execution_status"] != "unobserved" and any(meta["diagnostics"][key] for key in incomplete_keys):
            meta["execution_status"] = "partial"
        providers[provider] = meta
    return validate({
        "version": ENVELOPE_VERSION,
        "cutoff": cutoff_text,
        "observed_at": _utc_now(),
        "providers": providers,
    })


def _validate_source_refs(value: object, provider: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("source_refs must be a non-empty list")
    expected_kind = "jsonl" if provider == "codex" else "summary_index"
    result: set[tuple[str, str, int]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"source", "kind", "index"}:
            raise ValueError("invalid source reference")
        source, kind, index = item["source"], item["kind"], item["index"]
        if not isinstance(source, str) or not _HASH_RE.fullmatch(source):
            raise ValueError("source reference must be hashed")
        if kind != expected_kind or type(index) is not int or index < 0:
            raise ValueError("invalid source reference")
        result.add((source, kind, index))
    return [
        {"source": source, "kind": kind, "index": index}
        for source, kind, index in sorted(result)
    ]


def validate(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize the private hash-only envelope."""
    if not isinstance(envelope, Mapping) or set(envelope) != {"version", "cutoff", "observed_at", "providers"}:
        raise ValueError("invalid agent envelope")
    if type(envelope["version"]) is not int or envelope["version"] != ENVELOPE_VERSION:
        raise ValueError("unsupported agent envelope version")
    cutoff = _timestamp(envelope["cutoff"], name="cutoff")
    observed_at = _timestamp(envelope["observed_at"], name="observed_at")
    if _datetime_value(cutoff) > _datetime_value(observed_at):
        raise ValueError("cutoff cannot be later than observed_at")
    raw_providers = envelope["providers"]
    if not isinstance(raw_providers, Mapping) or any(key not in PROVIDERS for key in raw_providers):
        raise ValueError("invalid provider collection")

    providers: dict[str, Any] = {}
    for provider, raw in raw_providers.items():
        if not isinstance(raw, Mapping) or set(raw) != {"execution_status", "diagnostics", "records"}:
            raise ValueError("invalid provider envelope")
        status = raw["execution_status"]
        if not isinstance(status, str) or status not in {"complete", "partial", "unobserved"}:
            raise ValueError("invalid execution status")
        diagnostics_raw = raw["diagnostics"]
        if not isinstance(diagnostics_raw, Mapping) or set(diagnostics_raw) - set(_DIAGNOSTIC_KEYS):
            raise ValueError("invalid diagnostics")
        diagnostics = _diagnostics()
        for key, value in diagnostics_raw.items():
            if type(value) is not int or value < 0:
                raise ValueError("diagnostic counters must be non-negative integers")
            diagnostics[key] = value
        records_raw = raw["records"]
        if not isinstance(records_raw, list):
            raise ValueError("records must be a list")
        records: list[dict[str, Any]] = []
        for record in records_raw:
            required = {"session_id_sha256", "parent_id_sha256", "created_at", "source_refs"}
            if provider == "antigravity":
                required |= {"depth", "agent_name_present"}
            if not isinstance(record, Mapping) or set(record) != required:
                raise ValueError("invalid agent-session record")
            child_id = record["session_id_sha256"]
            parent_id = record["parent_id_sha256"]
            if not isinstance(child_id, str) or not _HASH_RE.fullmatch(child_id):
                raise ValueError("child identity must be hashed")
            if parent_id is not None and (not isinstance(parent_id, str) or not _HASH_RE.fullmatch(parent_id)):
                raise ValueError("parent identity must be hashed")
            if provider == "codex":
                if parent_id is None:
                    raise ValueError("Codex child session requires a known parent")
                created_at = _timestamp(record["created_at"], name="created_at")
                if _datetime_value(created_at) > _datetime_value(cutoff):
                    raise ValueError("Codex session birth must not exceed cutoff")
                clean = {
                    "session_id_sha256": child_id,
                    "parent_id_sha256": parent_id,
                    "created_at": created_at,
                    "source_refs": _validate_source_refs(record["source_refs"], provider),
                }
            else:
                if parent_id is None or record["created_at"] is not None:
                    raise ValueError("Antigravity lineage requires parent and null timestamp")
                depth = record["depth"]
                if type(depth) is not int or depth <= 0 or record["agent_name_present"] is not True:
                    raise ValueError("Antigravity lineage requires positive depth and agent name")
                clean = {
                    "session_id_sha256": child_id,
                    "parent_id_sha256": parent_id,
                    "created_at": None,
                    "depth": depth,
                    "agent_name_present": True,
                    "source_refs": _validate_source_refs(record["source_refs"], provider),
                }
            records.append(clean)
        if status == "unobserved" and records:
            raise ValueError("unobserved provider cannot include records")
        if status == "complete" and any(diagnostics[key] for key in (
            "roots_missing", "discovery_errors", "read_errors",
            "schema_errors", "malformed_records", "undated_child_records", "missing_summary_indexes",
        )):
            raise ValueError("incomplete diagnostics require partial status")
        records.sort(key=lambda row: (
            row["session_id_sha256"], row["parent_id_sha256"] or "", row["created_at"] or "",
            row.get("depth", 0),
        ))
        providers[provider] = {
            "execution_status": status,
            "diagnostics": diagnostics,
            "records": records,
        }
    return {
        "version": ENVELOPE_VERSION,
        "cutoff": cutoff,
        "observed_at": observed_at,
        "providers": providers,
    }


def _ensure_schema(connection: sqlite3.Connection) -> None:
    tables = {str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if "ledger_meta" not in tables:
        raise ValueError("agent observations require an initialized tokenometer ledger")
    marker = connection.execute(
        "SELECT value FROM ledger_meta WHERE key='agents_schema_version'").fetchone()
    if marker is not None and marker[0] != "1":
        raise ValueError("unsupported agent ledger schema")
    connection.execute("""CREATE TABLE IF NOT EXISTS agent_runs (
        run_order INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        provider TEXT NOT NULL CHECK(provider IN ('codex','antigravity')),
        observed_at_utc TEXT NOT NULL,
        cutoff_utc TEXT,
        execution_status TEXT NOT NULL CHECK(execution_status IN ('complete','partial','unobserved')),
        diagnostics_json TEXT NOT NULL,
        UNIQUE(run_id, provider)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS agent_versions (
        provider TEXT NOT NULL CHECK(provider IN ('codex','antigravity')),
        session_id_sha256 TEXT NOT NULL,
        parent_id_sha256 TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        depth INTEGER NOT NULL,
        agent_name_present INTEGER NOT NULL,
        source_refs_json TEXT NOT NULL,
        observation_run_ids_json TEXT NOT NULL,
        PRIMARY KEY(provider, session_id_sha256, parent_id_sha256, created_at_utc, depth, agent_name_present)
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS agent_versions_session_idx ON agent_versions(provider, session_id_sha256)")
    if marker is None:
        connection.execute(
            "INSERT INTO ledger_meta(key,value) VALUES('agents_schema_version','1')"
        )
    # Fail closed if an unrelated or partially-created table has a conflicting schema.
    required_run_columns = {"run_order", "run_id", "provider", "observed_at_utc", "cutoff_utc", "execution_status", "diagnostics_json"}
    required_version_columns = {"provider", "session_id_sha256", "parent_id_sha256", "created_at_utc", "depth", "agent_name_present", "source_refs_json", "observation_run_ids_json"}
    run_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(agent_runs)")}
    version_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(agent_versions)")}
    if not required_run_columns.issubset(run_columns) or not required_version_columns.issubset(version_columns):
        raise ValueError("agent ledger schema is incomplete")


def ingest(connection: sqlite3.Connection, run_id: str, envelope: Mapping[str, Any]) -> None:
    """Persist agent observations inside the caller's existing ledger transaction."""
    if not isinstance(run_id, str) or not run_id or len(run_id) > 200 or any(ord(char) < 32 for char in run_id):
        raise ValueError("invalid capture run id")
    clean = validate(envelope)
    _ensure_schema(connection)
    for provider, details in clean["providers"].items():
        cutoff = clean["cutoff"] if provider == "codex" else None
        diagnostics_json = json.dumps(details["diagnostics"], sort_keys=True, separators=(",", ":"))
        existing = connection.execute(
            "SELECT observed_at_utc, cutoff_utc, execution_status, diagnostics_json "
            "FROM agent_runs WHERE run_id=? AND provider=?",
            (run_id, provider),
        ).fetchone()
        expected = (clean["observed_at"], cutoff, details["execution_status"], diagnostics_json)
        if existing is not None and tuple(existing) != expected:
            raise ValueError("capture run already has different agent observations")
        connection.execute(
            "INSERT OR IGNORE INTO agent_runs(run_id,provider,observed_at_utc,cutoff_utc,execution_status,diagnostics_json) "
            "VALUES(?,?,?,?,?,?)",
            (run_id, provider, *expected),
        )
        for record in details["records"]:
            for source_ref in record["source_refs"]:
                parent = record["parent_id_sha256"] or ""
                created = record["created_at"] or ""
                depth = record.get("depth", 0)
                name_present = int(record.get("agent_name_present", False))
                source_refs_json = json.dumps([source_ref], sort_keys=True, separators=(",", ":"))
                old = connection.execute(
                    "SELECT source_refs_json, observation_run_ids_json "
                    "FROM agent_versions WHERE provider=? AND session_id_sha256=? AND parent_id_sha256=? "
                    "AND created_at_utc=? AND depth=? AND agent_name_present=?",
                    (provider, record["session_id_sha256"], parent, created, depth, name_present),
                ).fetchone()
                if old is None:
                    connection.execute(
                        "INSERT INTO agent_versions(provider,session_id_sha256,parent_id_sha256,created_at_utc,depth,agent_name_present,source_refs_json,observation_run_ids_json) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (provider, record["session_id_sha256"], parent, created, depth, name_present,
                         source_refs_json, json.dumps([run_id], separators=(",", ":"))),
                    )
                else:
                    refs = json.loads(old[0])
                    if source_ref not in refs:
                        refs.append(source_ref)
                        refs.sort(key=lambda item: (item["source"], item["kind"], item["index"]))
                    run_ids = sorted(set(json.loads(old[1])) | {run_id})
                    connection.execute(
                        "UPDATE agent_versions SET source_refs_json=?, observation_run_ids_json=? "
                        "WHERE provider=? AND session_id_sha256=? AND parent_id_sha256=? AND created_at_utc=? AND depth=? AND agent_name_present=?",
                        (json.dumps(refs, sort_keys=True, separators=(",", ":")), json.dumps(run_ids, separators=(",", ":")),
                         provider, record["session_id_sha256"], parent, created, depth, name_present),
                    )


def _empty_provider() -> dict[str, Any]:
    return {
        "count": None,
        "observed_at": None,
        "cutoff": None,
        "execution_status": "unobserved",
        "conflicted_sessions": 0,
    }


def _run_semantic_key(provider: str, row: sqlite3.Row | tuple[Any, ...]) -> tuple[datetime, datetime, int]:
    # row layout: run_order, observed_at_utc, cutoff_utc, execution_status, run_id
    observed_at = str(row[1])
    cutoff = row[2] if provider == "codex" and row[2] else observed_at
    return _datetime_value(str(cutoff)), _datetime_value(observed_at), int(row[0])


def snapshot(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Return the public, aggregate-only agent summary for each provider."""
    tables = {str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if not {_RUN_TABLE, _VERSION_TABLE}.intersection(tables):
        return {provider: _empty_provider() for provider in PROVIDERS}
    if not {_RUN_TABLE, _VERSION_TABLE}.issubset(tables):
        raise ValueError("agent ledger schema is incomplete")

    output: dict[str, dict[str, Any]] = {}
    for provider in PROVIDERS:
        runs = connection.execute(
            "SELECT run_order, observed_at_utc, cutoff_utc, execution_status, run_id "
            "FROM agent_runs WHERE provider=?",
            (provider,),
        ).fetchall()
        if not runs:
            output[provider] = _empty_provider()
            continue
        # SQLite text ordering is not chronological for equivalent ISO values
        # with different fractional precision (for example, `...00Z` and
        # `...00.000000Z`). Parse timestamps before selecting freshness.
        latest_attempt = max(
            runs,
            key=lambda row: (_datetime_value(str(row[1])), int(row[0])),
        )
        complete_runs = [row for row in runs if row[3] == "complete"]
        if not complete_runs:
            result = _empty_provider()
            if latest_attempt[3] == "partial":
                result["execution_status"] = "partial"
            output[provider] = result
            continue

        latest_good = max(complete_runs, key=lambda row: _run_semantic_key(provider, row))
        result_status = "complete"
        if latest_attempt[3] != "complete" and _run_semantic_key(provider, latest_attempt) > _run_semantic_key(provider, latest_good):
            result_status = "partial"
        cutoff = latest_good[2] if provider == "codex" else None
        eligible_run_ids = set()
        for run in complete_runs:
            if provider == "codex":
                if _datetime_value(str(run[2])) <= _datetime_value(str(cutoff)):
                    eligible_run_ids.add(str(run[4]))
            elif _datetime_value(str(run[1])) <= _datetime_value(str(latest_good[1])):
                eligible_run_ids.add(str(run[4]))
        rows = connection.execute(
            "SELECT session_id_sha256,parent_id_sha256,observation_run_ids_json "
            "FROM agent_versions WHERE provider=?",
            (provider,),
        ).fetchall()
        parents: dict[str, set[str]] = defaultdict(set)
        for child, parent, observed_runs_json in rows:
            if not eligible_run_ids.intersection(json.loads(observed_runs_json)):
                continue
            if parent:
                parents[str(child)].add(str(parent))
            else:
                parents[str(child)]
        conflicts = {child for child, values in parents.items() if len(values) > 1}
        result = {
            "count": sum(1 for child in parents if child not in conflicts),
            "observed_at": str(latest_good[1]),
            "cutoff": str(cutoff) if cutoff is not None else None,
            "execution_status": result_status,
            "conflicted_sessions": len(conflicts),
        }
        output[provider] = result
    return output
