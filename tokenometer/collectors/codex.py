"""Read locally retained Codex usage from session JSONL files.

This collector deliberately treats per-response usage records as the additive
source. Cumulative thread and legacy token-count snapshots are never added to
those records; legacy snapshots are admitted only when their six-field delta
can be matched exactly to the stored last-turn counters.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import io
from contextlib import contextmanager
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

try:
    from compression import zstd as _stdlib_zstd
except ImportError:  # Python versions before the stdlib zstd module.
    _stdlib_zstd = None

from tokenometer.models import CollectionResult, UsageEvent, digest, utc_timestamp


_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
_REQUIRED_RESPONSE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
)


@dataclass
class _SessionFile:
    path: Path
    area: str
    thread_id: str | None = None
    parent_id: str | None = None
    child_hint: bool = False
    meta_seen: bool = False
    turn_models: dict[str, str] = field(default_factory=dict)
    modern: list[dict[str, Any]] = field(default_factory=list)
    legacy: list[dict[str, Any]] = field(default_factory=list)
    malformed: int = 0
    read_error: bool = False
    account_fingerprints: set[str] = field(default_factory=set)


def _valid_id(value: object) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        result = str(value).strip()
        return result or None
    return None


def _parse_timestamp(value: object) -> datetime | None:
    normalized = utc_timestamp(value)
    if normalized is None:
        return None
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


def _zstd_backend() -> str:
    if _stdlib_zstd is not None:
        return "stdlib"
    return "cli" if shutil.which("zstd") else "unavailable"


@contextmanager
def _open_text(path: Path):
    """Stream JSONL or JSONL.zst as UTF-8 text without extracting source files."""
    if not str(path).endswith(".jsonl.zst"):
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            yield stream
        return

    if _stdlib_zstd is not None:
        try:
            with _stdlib_zstd.open(path, "rt", encoding="utf-8", errors="replace") as stream:
                yield stream
        except (_stdlib_zstd.ZstdError, EOFError):
            raise OSError("compressed source could not be read") from None
        return

    executable = shutil.which("zstd")
    if not executable:
        raise OSError("zstd decompression is unavailable")
    try:
        process = subprocess.Popen(
            [executable, "-dc", "--", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise OSError("zstd decompressor could not be started") from None
    assert process.stdout is not None
    stream = io.TextIOWrapper(process.stdout, encoding="utf-8", errors="replace")
    try:
        yield stream
    finally:
        stream.close()
        return_code = process.wait()
        if return_code:
            raise OSError("compressed source could not be read")


def _source_ref(source_hash: str, index: int) -> dict[str, Any]:
    return {
        "source": source_hash,
        "kind": "jsonl",
        "index": index,
        "retry": 0,
    }


def _unique_source_refs(refs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {
        (ref["source"], ref["kind"], ref["index"], ref["retry"]): ref
        for ref in refs
    }
    return [unique[key] for key in sorted(unique)]


def discover_roots(home: Path, users_dir: Path | None = None) -> list[Path]:
    """Find known Codex history roots without walking credential directories."""
    home = Path(home).expanduser()
    candidates = [
        home / ".codex",
        home / "Library" / "Application Support" / "Codex",
        home / "Library" / "Application Support" / "com.openai.codex",
    ]
    configured_home = os.environ.get("CODEX_HOME")
    if configured_home:
        candidates.extend(
            Path(value.strip()).expanduser()
            for value in configured_home.split(",")
            if value.strip()
        )

    # Codex histories on this Mac can belong to more than the active user.
    # Restrict discovery to the conventional per-user .codex directory; do not
    # enumerate arbitrary home-directory content or credential subdirectories.
    users_dir = Path("/Users") if users_dir is None else Path(users_dir).expanduser()
    try:
        user_homes = sorted(users_dir.iterdir(), key=lambda path: path.name)
    except OSError:
        user_homes = []
    for user_home in user_homes:
        try:
            if user_home.is_symlink() or not user_home.is_dir():
                continue
            candidate = user_home / ".codex"
            if candidate.is_dir() and not candidate.is_symlink():
                candidates.append(candidate)
        except OSError:
            continue

    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            if not candidate.is_dir():
                continue
            resolved = _safe_resolve(candidate)
            identity = str(resolved)
            if identity in seen:
                continue
            seen.add(identity)
            roots.append(resolved)
        except OSError:
            continue
    return roots


def _session_areas(root: Path) -> list[tuple[Path, str]]:
    """Return only documented history locations, never arbitrary subtrees."""
    candidates: list[tuple[Path, str]] = []
    root_name = root.name
    if root_name in ("sessions", "archived_sessions"):
        candidates.append((root, root_name))
    else:
        for name in ("sessions", "archived_sessions"):
            candidates.append((root / name, name))

    profiles = root / "profiles"
    try:
        if profiles.is_dir() and not profiles.is_symlink():
            for profile in sorted(profiles.iterdir(), key=lambda p: p.name):
                if profile.is_dir() and not profile.is_symlink():
                    for name in ("sessions", "archived_sessions"):
                        candidates.append((profile / name, name))
    except OSError:
        pass
    return candidates


def _discover_files(roots: Sequence[Path]) -> tuple[list[tuple[Path, str]], int, int, int, int]:
    found: list[tuple[Path, str]] = []
    seen_roots: set[str] = set()
    missing_sources = 0
    available_areas = 0
    discovery_errors = 0
    seen_files: set[str] = set()
    for raw_root in roots:
        root = Path(raw_root).expanduser()
        resolved = _safe_resolve(root)
        if str(resolved) in seen_roots:
            continue
        seen_roots.add(str(resolved))
        try:
            if not root.is_dir():
                missing_sources += 1
                continue
        except OSError:
            missing_sources += 1
            continue

        root_had_area = False
        for area, area_name in _session_areas(root):
            try:
                if not area.is_dir():
                    continue
                root_had_area = True
                available_areas += 1
                source_paths = list(area.rglob("*.jsonl")) + list(area.rglob("*.jsonl.zst"))
                for path in source_paths:
                    try:
                        if path.is_symlink() or not path.is_file():
                            continue
                        identity = str(_safe_resolve(path))
                    except OSError:
                        continue
                    if identity in seen_files:
                        continue
                    seen_files.add(identity)
                    found.append((path, area_name))
            except OSError:
                discovery_errors += 1
                continue
        if not root_had_area:
            missing_sources += 1
    return found, missing_sources, available_areas, len(seen_roots), discovery_errors


def _counter_object(value: object) -> dict[str, int | None]:
    source = value if isinstance(value, dict) else {}
    return {
        name: (number if type(number) is int and number >= 0 else None)
        for name in _FIELDS
        for number in (source.get(name),)
    }


def _all_known(values: dict[str, int | None]) -> bool:
    return all(values.get(name) is not None for name in _FIELDS)


def _modern_copy_fingerprint(row: dict[str, Any]) -> tuple[Any, ...]:
    timestamp = row["timestamp"].isoformat() if row["timestamp"] else None
    return (row["owner"], timestamp, row["turn_id"], row["ordinal"], tuple(row["usage"].get(field) for field in _FIELDS))


def _scan_file(path: Path, area: str) -> _SessionFile:
    scanned = _SessionFile(path=path, area=area)
    source_hash = digest(str(_safe_resolve(path)))
    try:
        with _open_text(path) as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    scanned.malformed += 1
                    continue
                if not isinstance(obj, dict):
                    scanned.malformed += 1
                    continue
                kind = obj.get("type")
                payload = obj.get("payload")
                timestamp_value = obj.get("timestamp")
                if not isinstance(payload, dict):
                    payload = {}
                timestamp = _parse_timestamp(timestamp_value)
                if timestamp_value is None:
                    timestamp = _parse_timestamp(payload.get("timestamp"))

                if kind == "session_meta":
                    source = payload.get("source")
                    subagent = source.get("subagent") if isinstance(source, dict) else None
                    thread_spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
                    nested_parent_id = None
                    if isinstance(thread_spawn, dict):
                        nested_parent_id = (
                            _valid_id(thread_spawn.get("parent_thread_id"))
                            or _valid_id(thread_spawn.get("parent_id"))
                        )
                    thread_id = (
                        _valid_id(payload.get("thread_id"))
                        or _valid_id(payload.get("id"))
                        or _valid_id(payload.get("session_id"))
                        or _valid_id(obj.get("thread_id"))
                        or _valid_id(obj.get("session_id"))
                    )
                    parent_id = (
                        _valid_id(payload.get("parent_thread_id"))
                        or _valid_id(payload.get("parent_id"))
                        or _valid_id(obj.get("parent_thread_id"))
                        or nested_parent_id
                    )
                    role = payload.get("agent_role")
                    explicit_subagent = (
                        (isinstance(source, dict) and "subagent" in source)
                        or thread_spawn is not None
                        or (isinstance(role, str) and bool(role.strip()) and role.strip().lower() not in {"root", "main", "primary"})
                    )
                    if thread_id:
                        if scanned.thread_id and scanned.thread_id != thread_id:
                            scanned.meta_seen = True
                            scanned.parent_id = scanned.parent_id or parent_id
                            continue
                        scanned.thread_id = thread_id
                        scanned.parent_id = parent_id or scanned.parent_id
                        scanned.child_hint = scanned.child_hint or bool(parent_id) or explicit_subagent
                        scanned.meta_seen = True
                    account_id = _valid_id(payload.get("creator_account_id"))
                    if account_id:
                        scanned.account_fingerprints.add(digest(account_id))
                    continue

                if kind == "turn_context":
                    turn_id = _valid_id(payload.get("turn_id"))
                    model = payload.get("model")
                    if turn_id and isinstance(model, str) and model:
                        scanned.turn_models[turn_id] = model
                    continue

                if kind == "token_usage_record":
                    usage = payload.get("usage")
                    if not isinstance(usage, dict):
                        usage = obj.get("usage")
                    if not isinstance(usage, dict):
                        if any(name in payload for name in _FIELDS):
                            usage = payload
                        elif any(name in obj for name in _FIELDS):
                            usage = obj
                        else:
                            usage = {}
                    ordinal = payload.get("ordinal", obj.get("ordinal", line_no))
                    if type(ordinal) is not int:
                        ordinal = line_no
                    scanned.modern.append({
                        "timestamp": timestamp,
                        "response_id": _valid_id(payload.get("response_id")) or _valid_id(obj.get("response_id")),
                        "explicit_owner": _valid_id(payload.get("thread_id")) or _valid_id(obj.get("thread_id")),
                        "turn_id": _valid_id(payload.get("turn_id")) or _valid_id(obj.get("turn_id")),
                        "ordinal": ordinal,
                        "source_ref": _source_ref(source_hash, line_no - 1),
                        "usage": _counter_object(usage),
                    })
                    continue

                if kind == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    info = info if isinstance(info, dict) else {}
                    total = _counter_object(info.get("total_token_usage"))
                    last = _counter_object(info.get("last_token_usage"))
                    if any(value is not None for value in total.values()):
                        ordinal = payload.get("ordinal", obj.get("ordinal", line_no))
                        if type(ordinal) is not int:
                            ordinal = line_no
                        scanned.legacy.append({
                            "timestamp": timestamp,
                            "ordinal": ordinal,
                            "source_ref": _source_ref(source_hash, line_no - 1),
                            "total": total,
                            "last": last,
                        })
    except OSError:
        scanned.read_error = True
    return scanned


def _valid_modern(values: dict[str, int | None]) -> tuple[bool, bool]:
    """Return (usable, has_counter_relationship_conflict)."""
    if not all(values.get(name) is not None for name in _REQUIRED_RESPONSE_FIELDS):
        return False, False
    input_tokens = int(values["input_tokens"])
    cached = int(values["cached_input_tokens"])
    cache_write = int(values["cache_write_input_tokens"])
    output = int(values["output_tokens"])
    if cached + cache_write > input_tokens:
        return False, True
    reasoning = values.get("reasoning_output_tokens")
    if reasoning is not None and reasoning > output:
        return False, True
    total = values.get("total_tokens")
    if total is not None and total != input_tokens + output:
        return False, True
    return True, False


def _event_for_usage(
    *,
    owner: str,
    identity: str,
    timestamp: datetime | None,
    values: dict[str, int | None],
    model: str | None,
    source_kind: str,
    counter_receipt: dict[str, Any],
) -> UsageEvent:
    return UsageEvent(
        key="codex:" + digest(identity),
        platform="codex",
        session_key=digest(owner),
        timestamp=(timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if timestamp else None),
        fresh_input=int(values["input_tokens"]) - int(values["cached_input_tokens"]) - int(values["cache_write_input_tokens"]),
        cache_read=int(values["cached_input_tokens"]),
        cache_write=int(values["cache_write_input_tokens"]),
        output=int(values["output_tokens"]),
        reasoning=(values.get("reasoning_output_tokens") if type(values.get("reasoning_output_tokens")) is int else None),
        model=model,
        source_kind=source_kind,
        counter_receipt=counter_receipt,
    )


def collect(roots: Sequence[Path], cutoff: datetime, cache: Path | None = None) -> CollectionResult:
    """Collect all eligible Codex usage in the supplied local history roots."""
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be a timezone-aware datetime")
    cutoff = cutoff.astimezone(timezone.utc)

    files, missing_sources, available_areas, unique_root_count, discovery_errors = _discover_files(roots)
    scanned_files = [_scan_file(path, area) for path, area in files]
    counters: Counter[str] = Counter()
    counters["files_discovered"] = len(files)
    counters["files_read"] = sum(not item.read_error for item in scanned_files)
    counters["read_errors"] = sum(item.read_error for item in scanned_files)
    compressed_files = [item for item in scanned_files if str(item.path).endswith(".jsonl.zst")]
    counters["compressed_files_discovered"] = len(compressed_files)
    counters["compressed_files_read"] = sum(not item.read_error for item in compressed_files)
    counters["compressed_read_errors"] = sum(item.read_error for item in compressed_files)
    account_fingerprints = {
        fingerprint
        for item in scanned_files
        for fingerprint in item.account_fingerprints
    }
    counters["files_with_account_metadata"] = sum(bool(item.account_fingerprints) for item in scanned_files)
    counters["threads_with_account_metadata"] = len({
        item.thread_id for item in scanned_files
        if item.thread_id and item.account_fingerprints
    })
    counters["distinct_accounts_observed"] = len(account_fingerprints)
    counters["zstd_capability"] = _zstd_backend()
    counters["discovery_errors"] = discovery_errors
    counters["malformed_jsonl_lines"] = sum(item.malformed for item in scanned_files)
    counters["files_missing_session_metadata"] = sum(not item.meta_seen for item in scanned_files)
    area_inventory = Counter(area for _path, area in files)

    thread_parents: dict[str, set[str]] = defaultdict(set)
    thread_child: dict[str, bool] = defaultdict(bool)
    metadata_threads: set[str] = set()
    metadata_conflicts = 0
    for item in scanned_files:
        if not item.thread_id:
            continue
        metadata_threads.add(item.thread_id)
        thread_child[item.thread_id] = thread_child[item.thread_id] or item.child_hint
        if item.parent_id:
            thread_parents[item.thread_id].add(item.parent_id)
            thread_child[item.thread_id] = True
    metadata_conflicts = sum(max(0, len(parents) - 1) for parents in thread_parents.values())
    counters["metadata_parent_conflicts"] = metadata_conflicts
    counters["threads_with_parent_metadata"] = sum(bool(parents) for parents in thread_parents.values())
    models_by_thread_turn: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in scanned_files:
        if item.thread_id:
            for turn_id, model_name in item.turn_models.items():
                models_by_thread_turn[(item.thread_id, turn_id)].add(model_name)

    modern_candidates: list[dict[str, Any]] = []
    legacy_by_thread: dict[str, list[dict[str, Any]]] = defaultdict(list)
    modern_timestamps_by_owner: dict[str, list[datetime]] = defaultdict(list)
    modern_undated_owners: set[str] = set()
    modern_missing_fields: Counter[str] = Counter()
    for item in scanned_files:
        for row in item.modern:
            counters["modern_records_seen"] += 1
            owner = row["explicit_owner"] or item.thread_id
            row["owner"] = owner
            row["enclosing_thread"] = item.thread_id
            if row["timestamp"] and row["timestamp"] > cutoff:
                counters["future_records"] += 1
                continue
            if row["timestamp"] is None:
                counters["undated_records"] += 1
            for field_name in _FIELDS:
                if row["usage"].get(field_name) is None:
                    modern_missing_fields[field_name] += 1
            if owner:
                if row["timestamp"] is None:
                    modern_undated_owners.add(owner)
                else:
                    modern_timestamps_by_owner[owner].append(row["timestamp"])
            if not row["response_id"]:
                counters["modern_records_missing_response_id"] += 1
            if not owner:
                counters["modern_missing_owner"] += 1
                continue
            modern_candidates.append(row)
        for row in item.legacy:
            counters["legacy_snapshots_seen"] += 1
            if row["timestamp"] and row["timestamp"] > cutoff:
                counters["legacy_future_snapshots"] += 1
                continue
            if row["timestamp"] is None:
                counters["undated_records"] += 1
                counters["legacy_undated_snapshots"] += 1
                continue
            if not item.thread_id:
                counters["legacy_missing_owner"] += 1
                continue
            legacy_by_thread[item.thread_id].append(row)

    # Collapse exact response copies globally, then reject a response ID when
    # any copy disagrees on owner, stored counters, or a known timestamp.
    grouped_modern: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in modern_candidates:
        rid = row["response_id"]
        if rid:
            identity = "response:" + rid
        else:
            identity = "fallback:" + digest(json.dumps(_modern_copy_fingerprint(row), sort_keys=True, separators=(",", ":")))
        row["identity"] = identity
        grouped_modern[identity].append(row)

    # A copied record can lose its response_id in one source copy. Join a
    # fallback only when its stored timestamp, turn, ordinal and all counters
    # identify exactly one response group; multiple matches remain ambiguous.
    response_ids_by_fingerprint: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    for identity, copies in grouped_modern.items():
        if identity.startswith("response:"):
            for row in copies:
                response_ids_by_fingerprint[_modern_copy_fingerprint(row)].add(identity)
    for identity in list(grouped_modern):
        if not identity.startswith("fallback:"):
            continue
        copies = grouped_modern[identity]
        matches = response_ids_by_fingerprint.get(_modern_copy_fingerprint(copies[0]), set())
        if len(matches) == 1:
            target = next(iter(matches))
            grouped_modern[target].extend(copies)
            del grouped_modern[identity]
        elif len(matches) > 1:
            counters["modern_conflicts"] += 1
            counters["modern_cross_id_conflicts"] += 1
            del grouped_modern[identity]

    events: list[UsageEvent] = []
    valid_modern_owners: set[str] = set()
    for identity, copies in grouped_modern.items():
        counters["modern_duplicate_records"] += max(0, len(copies) - 1)
        owners = {row["owner"] for row in copies}
        usage_values = {tuple(row["usage"].get(name) for name in _FIELDS) for row in copies}
        known_timestamps = {row["timestamp"] for row in copies if row["timestamp"] is not None}
        conflict = len(owners) != 1 or len(usage_values) != 1 or len(known_timestamps) > 1
        if conflict:
            counters["modern_conflicts"] += 1
            if len(owners) != 1:
                counters["modern_owner_conflicts"] += 1
            continue
        selected = max(copies, key=lambda row: (row["timestamp"] is not None, row["ordinal"]))
        values = selected["usage"]
        usable, relationship_conflict = _valid_modern(values)
        if not usable:
            counters["modern_invalid_records"] += 1
            if relationship_conflict:
                counters["modern_relationship_conflicts"] += 1
            continue
        owner = selected["owner"]
        if not selected["response_id"]:
            counters["modern_records_without_response_id"] += 1
        turn_id = selected["turn_id"]
        candidate_models = set(models_by_thread_turn.get((owner, turn_id), set())) if turn_id else set()
        enclosing = selected.get("enclosing_thread")
        if turn_id and enclosing and enclosing != owner:
            candidate_models.update(models_by_thread_turn.get((enclosing, turn_id), set()))
        model = next(iter(candidate_models)) if len(candidate_models) == 1 else None
        counter_receipt = {
            "schema": "codex.response.v1",
            "source_refs": _unique_source_refs([row["source_ref"] for row in copies]),
            "counters": {field: values.get(field) for field in _FIELDS},
        }
        events.append(_event_for_usage(
            owner=owner,
            identity=identity,
            timestamp=selected["timestamp"],
            values=values,
            model=model,
            source_kind="recorded",
            counter_receipt=counter_receipt,
        ))
        valid_modern_owners.add(owner)
        counters["modern_events"] += 1

    # Legacy token_count records are cumulative snapshots. Use a first total
    # only for roots when it exactly equals last-turn usage; child baselines are
    # always excluded. Later rows count only if every one of six deltas exactly
    # equals the row's complete last-turn vector.
    legacy_event_candidates: list[dict[str, Any]] = []
    for thread_id, raw_rows in legacy_by_thread.items():
        unique_rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in raw_rows:
            key = (row["timestamp"], row["ordinal"])
            signature = (tuple(row["total"][f] for f in _FIELDS), tuple(row["last"][f] for f in _FIELDS))
            existing = unique_rows.get(key)
            if existing is None:
                row["signature"] = signature
                row["copy_count"] = 1
                row["source_refs"] = [row["source_ref"]]
                unique_rows[key] = row
            elif existing["signature"] == signature:
                existing["copy_count"] += 1
                existing["source_refs"].append(row["source_ref"])
                counters["legacy_duplicate_snapshots"] += 1
            else:
                existing["conflict"] = True
                existing.setdefault("conflicting_signatures", set()).add(signature)
                counters["legacy_conflicts"] += 1
        ordered = sorted(unique_rows.values(), key=lambda row: (row["timestamp"], row["ordinal"]))
        previous: dict[str, int] | None = None
        previous_row: dict[str, Any] | None = None
        is_child = thread_child.get(thread_id, False)
        first_seen = True
        for row in ordered:
            if row.get("conflict"):
                counters["legacy_conflicting_snapshots_excluded"] += 1
                previous = None
                previous_row = None
                first_seen = False
                continue
            total, last = row["total"], row["last"]
            if not _all_known(total) or not _all_known(last):
                counters["legacy_missing_counter_snapshots"] += 1
                previous = None
                previous_row = None
                first_seen = False
                continue
            current = {field: int(total[field]) for field in _FIELDS}
            turn = {field: int(last[field]) for field in _FIELDS}
            if previous is None:
                if first_seen and not is_child and current == turn:
                    legacy_event_candidates.append({
                        "thread_id": thread_id,
                        "timestamp": row["timestamp"],
                        "ordinal": row["ordinal"],
                        "values": turn,
                        "signature": (row["timestamp"], row["ordinal"], tuple(turn[f] for f in _FIELDS)),
                        "copy_count": row["copy_count"],
                        "baseline_timestamp": None,
                        "counter_receipt": {
                            "schema": "codex.legacy_delta.v1",
                            "source_refs": _unique_source_refs(row["source_refs"]),
                            "counters": dict(turn),
                            "baseline": None,
                            "current": dict(current),
                            "last": dict(turn),
                        },
                    })
                    counters["legacy_events"] += 1
                else:
                    counters["legacy_baseline_exclusions"] += 1
                previous = current
                previous_row = row
                first_seen = False
                continue

            delta = {field: current[field] - previous[field] for field in _FIELDS}
            if any(value < 0 for value in delta.values()):
                counters["legacy_resets"] += 1
                previous = current
                previous_row = row
                first_seen = False
                continue
            if delta != turn:
                counters["legacy_mismatches"] += 1
                previous = current
                previous_row = row
                first_seen = False
                continue
            legacy_event_candidates.append({
                "thread_id": thread_id,
                "timestamp": row["timestamp"],
                "ordinal": row["ordinal"],
                "values": turn,
                "signature": (row["timestamp"], row["ordinal"], tuple(turn[f] for f in _FIELDS)),
                "copy_count": row["copy_count"],
                "baseline_timestamp": previous_row["timestamp"] if previous_row is not None else None,
                "counter_receipt": {
                    "schema": "codex.legacy_delta.v1",
                    "source_refs": _unique_source_refs(
                        [*previous_row["source_refs"], *row["source_refs"]]
                    ) if previous_row is not None else _unique_source_refs(row["source_refs"]),
                    "counters": dict(delta),
                    "baseline": dict(previous),
                    "current": dict(current),
                    "last": dict(turn),
                },
            })
            counters["legacy_events"] += 1
            previous = current
            previous_row = row
            first_seen = False

    verified_legacy_candidates: list[dict[str, Any]] = []
    for row in legacy_event_candidates:
        valid, relationship_conflict = _valid_modern(row["values"])
        if not valid:
            counters["legacy_invalid_events"] += 1
            if relationship_conflict:
                counters["legacy_conflicts"] += 1
            continue
        verified_legacy_candidates.append(row)

    # Legacy files do not carry response IDs. Resolve ambiguous ownership
    # before time filtering so a conflicting copy cannot make another copy
    # look safe to emit.
    legacy_owners: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    for row in verified_legacy_candidates:
        legacy_owners[row["signature"]].add(row["thread_id"])
    ambiguous_legacy = {signature for signature, owners in legacy_owners.items() if len(owners) > 1}
    counters["legacy_cross_thread_conflicts"] = len(ambiguous_legacy)
    counters["legacy_conflicting_events_excluded"] += sum(
        row["signature"] in ambiguous_legacy for row in verified_legacy_candidates
    )
    verified_legacy_candidates = [
        row for row in verified_legacy_candidates if row["signature"] not in ambiguous_legacy
    ]

    # Modern response records and legacy deltas can coexist in one thread.
    # Retain legacy events only before the first dated modern record. Later
    # snapshots may lag modern responses, so their timestamps alone cannot
    # establish disjoint usage. Exact same-owner/time/six-counter matches are
    # diagnosed as duplicates; other candidates receive a token upper bound.
    modern_fingerprints_by_owner: dict[str, set[tuple[datetime, tuple[int | None, ...]]]] = defaultdict(set)
    for row in modern_candidates:
        if row["timestamp"] is not None and row["owner"]:
            vector = tuple(row["usage"].get(field) for field in _FIELDS)
            modern_fingerprints_by_owner[row["owner"]].add((row["timestamp"], vector))
    first_modern_by_owner = {
        owner: min(timestamps)
        for owner, timestamps in modern_timestamps_by_owner.items()
        if timestamps
    }
    retained_legacy_candidates: list[dict[str, Any]] = []
    overlap_threads: set[str] = set()
    unknown_time_threads: set[str] = set()
    for row in verified_legacy_candidates:
        owner = row["thread_id"]
        overlaps = False
        if owner in modern_undated_owners:
            overlaps = True
            unknown_time_threads.add(owner)
        elif owner in first_modern_by_owner and row["timestamp"] >= first_modern_by_owner[owner]:
            vector = tuple(row["values"].get(field) for field in _FIELDS)
            is_exact_duplicate = (
                row["timestamp"], vector
            ) in modern_fingerprints_by_owner.get(owner, set())
            overlaps = True
            if overlaps:
                overlap_threads.add(owner)
                if is_exact_duplicate:
                    counters["legacy_exact_modern_duplicates"] += 1
                else:
                    counters["legacy_events_suppressed_after_first_modern"] += 1
        if overlaps:
            counters["legacy_events_suppressed_modern_overlap"] += 1
            counters["legacy_snapshots_suppressed"] += row["copy_count"]
            counters["legacy_suppressed_token_upper_bound"] += row["values"]["total_tokens"]
            continue
        retained_legacy_candidates.append(row)
    counters["legacy_threads_suppressed_by_modern"] = len(overlap_threads | unknown_time_threads)
    counters["legacy_threads_suppressed_unknown_modern_time"] = len(unknown_time_threads)

    for row in retained_legacy_candidates:
        values = row["values"]
        identity = "legacy:" + digest(json.dumps((row["thread_id"], row["signature"]), default=str, separators=(",", ":")))
        events.append(_event_for_usage(
            owner=row["thread_id"], identity=identity, timestamp=row["timestamp"],
            values=values, model=None, source_kind="legacy_delta",
            counter_receipt=row["counter_receipt"],
        ))
        counters["legacy_events_emitted"] += 1

    counters["events"] = len(events)
    counters["threads_with_metadata"] = len(metadata_threads)
    counters["threads_with_modern_usage"] = len(valid_modern_owners)
    counters["roots_supplied"] = unique_root_count
    counters["history_areas_found"] = available_areas
    counters["missing_sources"] = missing_sources
    counters["cache_used"] = 0
    for name in (
        "modern_records_seen", "future_records", "undated_records", "modern_missing_owner",
        "modern_records_missing_response_id", "modern_cross_id_conflicts",
        "legacy_snapshots_seen", "legacy_future_snapshots", "legacy_undated_snapshots",
        "legacy_missing_owner", "legacy_threads_suppressed_by_modern", "legacy_snapshots_suppressed",
        "legacy_events_suppressed_modern_overlap", "legacy_suppressed_token_upper_bound",
        "legacy_threads_suppressed_unknown_modern_time", "legacy_exact_modern_duplicates",
        "legacy_events_suppressed_after_first_modern",
        "modern_duplicate_records", "modern_conflicts", "modern_owner_conflicts",
        "modern_invalid_records", "modern_relationship_conflicts", "modern_records_without_response_id",
        "modern_events", "legacy_duplicate_snapshots", "legacy_conflicts",
        "legacy_conflicting_snapshots_excluded", "legacy_missing_counter_snapshots",
        "legacy_baseline_exclusions", "legacy_resets", "legacy_mismatches", "legacy_events",
        "legacy_cross_thread_conflicts", "legacy_conflicting_events_excluded", "legacy_invalid_events",
        "legacy_events_emitted", "threads_with_metadata", "threads_with_modern_usage",
        "roots_supplied", "history_areas_found", "missing_sources", "cache_used", "discovery_errors",
        "compressed_files_discovered", "compressed_files_read", "compressed_read_errors",
        "files_with_account_metadata", "threads_with_account_metadata", "distinct_accounts_observed",
    ):
        counters[name] += 0
    if not roots:
        counters["missing_sources"] += 1
    counters["modern_missing_fields"] = dict(modern_missing_fields)
    # cache is intentionally accepted for a stable public interface. A private
    # incremental cache is deferred until correctness has been established.
    _ = cache

    error_count = (
        counters["read_errors"] + counters["discovery_errors"] + counters["malformed_jsonl_lines"]
        + counters["modern_invalid_records"] + counters["modern_conflicts"]
        + counters["legacy_conflicts"] + counters["legacy_cross_thread_conflicts"]
        + counters["legacy_missing_counter_snapshots"] + counters["modern_missing_owner"]
        + counters["legacy_missing_owner"] + counters["metadata_parent_conflicts"]
    )
    incomplete = bool(
        counters["missing_sources"]
        or error_count
        or counters["undated_records"]
        or counters["files_missing_session_metadata"]
        or any(modern_missing_fields.values())
        or counters["legacy_cross_thread_conflicts"]
        or counters["legacy_resets"]
        or counters["legacy_mismatches"]
        or counters["legacy_baseline_exclusions"]
        or counters["legacy_events_suppressed_modern_overlap"]
        or counters["legacy_missing_counter_snapshots"]
        or counters["modern_missing_owner"]
        or counters["legacy_missing_owner"]
    )
    warning_codes: list[str] = []
    for key, code in (
        ("missing_sources", "codex_missing_sources"),
        ("read_errors", "codex_source_read_error"),
        ("discovery_errors", "codex_source_discovery_error"),
        ("malformed_jsonl_lines", "codex_malformed_jsonl"),
        ("files_missing_session_metadata", "codex_missing_session_metadata"),
        ("modern_invalid_records", "codex_invalid_usage"),
        ("modern_conflicts", "codex_response_conflict"),
        ("modern_missing_owner", "codex_missing_usage_owner"),
        ("legacy_missing_owner", "codex_missing_legacy_owner"),
        ("legacy_missing_counter_snapshots", "codex_legacy_missing_counters"),
        ("legacy_cross_thread_conflicts", "codex_legacy_owner_conflict"),
        ("metadata_parent_conflicts", "codex_metadata_conflict"),
        ("legacy_resets", "codex_legacy_reset"),
        ("legacy_mismatches", "codex_legacy_mismatch"),
        ("undated_records", "codex_undated_records"),
        ("future_records", "codex_future_records"),
        ("legacy_baseline_exclusions", "codex_legacy_baseline_excluded"),
        ("legacy_events_suppressed_modern_overlap", "codex_legacy_modern_overlap"),
    ):
        if counters[key]:
            warning_codes.append(code)
    if modern_missing_fields:
        warning_codes.append("codex_missing_usage_fields")
    stats: dict[str, Any] = dict(counters)
    stats.update({
        "platform": "codex",
        "status": "partial" if incomplete else "complete",
        "incomplete": incomplete,
        "errors": error_count,
        "cutoff_utc": cutoff.isoformat().replace("+00:00", "Z"),
        "source_inventory": {
            "roots_supplied": unique_root_count,
            "history_areas_found": available_areas,
            "jsonl_files_discovered": len(files),
            "jsonl_files_read": counters["files_read"],
            "jsonl_zst_files_discovered": counters["compressed_files_discovered"],
            "jsonl_zst_files_read": counters["compressed_files_read"],
            "jsonl_zst_read_errors": counters["compressed_read_errors"],
            "jsonl_files_by_area": dict(area_inventory),
            "missing_sources": counters["missing_sources"],
            "zstd_capability": counters["zstd_capability"],
        },
        "duplicates": counters["modern_duplicate_records"] + counters["legacy_duplicate_snapshots"],
        "conflicts": (
            counters["modern_conflicts"] + counters["legacy_conflicts"]
            + counters["legacy_cross_thread_conflicts"] + counters["metadata_parent_conflicts"]
        ),
        "warnings": warning_codes,
    })
    return CollectionResult(events=events, stats=stats, warnings=warning_codes)
