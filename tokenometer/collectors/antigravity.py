"""Read locally stored Antigravity usage counters without changing source stores.

Only named usage metadata is decoded. SQLite databases are opened read-only and
backed up into memory so committed WAL rows are visible without checkpointing a
source database. No transcript, prompt, title, preview, workspace, or credential
columns are selected or persisted.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import quote

from tokenometer.models import CollectionResult, UsageEvent, digest, utc_timestamp


_PROTO_LIMIT = 16 * 1024 * 1024
_MAX_COUNTER = (1 << 63) - 1
_BACKUP_TIMEOUT_ENV = "TOKENOMETER_ANTIGRAVITY_SNAPSHOT_TIMEOUT_SECONDS"
_DEFAULT_BACKUP_SECONDS = 5.0
_MAX_BACKUP_SECONDS = 30.0
_QUERY_SECONDS = 8.0
_SQLITE_TIMEOUT = 0.75
_MAX_FAILURE_DIAGNOSTICS = 128
_MAX_TIMEOUT_DIAGNOSE_BYTES = 1024 * 1024
_SQLITE_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".vscdb"}
_TOKEN_FIELDS = {2, 3, 4, 5, 9, 10}
_KNOWN_USAGE_FIELDS = {1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12}
_FIELD_NAMES = {
    2: "fresh_input",
    3: "reported_output",
    4: "cache_write",
    5: "cache_read",
    9: "reasoning",
    10: "visible_output",
}
_COUNTER_NAMES = ("fresh_input", "cache_read", "cache_write", "output")
_STORE_NAMES = ("app", "cli", "ide", "backup", "configured")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]{1,240}$")
_PROFILE_STORE_NAMES = {
    "antigravity",
    "antigravity-cli",
    "antigravity-ide",
    "antigravity-backup",
}


def _snapshot_timeout_seconds() -> float:
    """Read a shell-configurable backup deadline with a strict upper bound."""
    try:
        configured = float(os.environ.get(_BACKUP_TIMEOUT_ENV, _DEFAULT_BACKUP_SECONDS))
    except (TypeError, ValueError, OverflowError):
        configured = _DEFAULT_BACKUP_SECONDS
    if not (0.5 <= configured <= _MAX_BACKUP_SECONDS):
        if configured > _MAX_BACKUP_SECONDS:
            return _MAX_BACKUP_SECONDS
        if configured < 0.5:
            return 0.5
        return _DEFAULT_BACKUP_SECONDS
    return configured

# Explicit model IDs and labels observed in the pinned ccusage v20.0.30 parser.
# Unknown numeric IDs and free-form labels are intentionally omitted.
_MODEL_IDS = {
    246: "gemini-2.5-pro",
    312: "gemini-2.5-flash",
    313: "gemini-2.5-flash-thinking",
    329: "gemini-2.5-flash-thinking",
    330: "gemini-2.5-flash-lite",
    281: "claude-4-sonnet",
    282: "claude-4-sonnet",
    290: "claude-4-opus",
    291: "claude-4-opus",
    333: "claude-4.5-sonnet",
    334: "claude-4.5-sonnet",
    340: "claude-4.5-haiku",
    341: "claude-4.5-haiku",
    342: "gpt-oss-120b-medium",
    1298: "gemini-3.7-flash-high",
    1299: "gemini-3.7-flash-medium",
    1300: "gemini-3.7-flash-low",
    1318: "gemini-3.8-flash-high",
    1319: "gemini-3.8-flash-medium",
    1320: "gemini-3.8-flash-low",
    1071: "gemini-3.6-flash-high",
    1072: "gemini-3.6-flash-medium",
    1073: "gemini-3.6-flash-low",
}
_MODEL_LABELS = {
    "gemini 2.5 pro": "gemini-2.5-pro",
    "gemini 2.5 flash": "gemini-2.5-flash",
    "gemini 2.5 flash thinking": "gemini-2.5-flash-thinking",
    "gemini 2.5 flash lite": "gemini-2.5-flash-lite",
    "claude 4 sonnet": "claude-4-sonnet",
    "claude 4 opus": "claude-4-opus",
    "claude 4.5 sonnet": "claude-4.5-sonnet",
    "claude 4.5 haiku": "claude-4.5-haiku",
    "gemini 3.7 flash (high)": "gemini-3.7-flash-high",
    "gemini 3.7 flash (medium)": "gemini-3.7-flash-medium",
    "gemini 3.7 flash (low)": "gemini-3.7-flash-low",
    "gemini 3.8 flash (high)": "gemini-3.8-flash-high",
    "gemini 3.8 flash (medium)": "gemini-3.8-flash-medium",
    "gemini 3.8 flash (low)": "gemini-3.8-flash-low",
    "gemini 3.6 flash (high)": "gemini-3.6-flash-high",
    "gemini 3.6 flash (medium)": "gemini-3.6-flash-medium",
    "gemini 3.6 flash (low)": "gemini-3.6-flash-low",
    "gemini-2.5-pro": "gemini-2.5-pro",
    "gemini-2.5-flash": "gemini-2.5-flash",
    "gemini-2.5-flash-thinking": "gemini-2.5-flash-thinking",
    "gemini-2.5-flash-lite": "gemini-2.5-flash-lite",
    "claude-4-sonnet": "claude-4-sonnet",
    "claude-4-opus": "claude-4-opus",
    "claude-4.5-sonnet": "claude-4.5-sonnet",
    "claude-4.5-haiku": "claude-4.5-haiku",
    "gpt-oss-120b-medium": "gpt-oss-120b-medium",
    "gemini-3.7-flash-high": "gemini-3.7-flash-high",
    "gemini-3.7-flash-medium": "gemini-3.7-flash-medium",
    "gemini-3.7-flash-low": "gemini-3.7-flash-low",
    "gemini-3.8-flash-high": "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium": "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low": "gemini-3.8-flash-low",
    "gemini-3.6-flash-high": "gemini-3.6-flash-high",
    "gemini-3.6-flash-medium": "gemini-3.6-flash-medium",
    "gemini-3.6-flash-low": "gemini-3.6-flash-low",
    "gpt-oss-120b-medium": "gpt-oss-120b-medium",
}


class _ProtoError(ValueError):
    """Malformed or unsupported protobuf data, without retaining source text."""

    _CODES = {
        "protobuf blob missing": "PROTO_BLOB_MISSING",
        "protobuf blob too large": "PROTO_BLOB_TOO_LARGE",
        "truncated varint": "TRUNCATED_VARINT",
        "varint overflow": "VARINT_OVERFLOW",
        "invalid field number": "INVALID_FIELD_NUMBER",
        "truncated fixed value": "TRUNCATED_FIXED_VALUE",
        "truncated length-delimited value": "TRUNCATED_LENGTH_DELIMITED_VALUE",
        "unsupported wire type": "UNSUPPORTED_WIRE_TYPE",
        "counter out of range": "COUNTER_OUT_OF_RANGE",
        "usage counter has wrong wire type": "USAGE_COUNTER_WRONG_WIRE_TYPE",
        "retry field has wrong wire type": "RETRY_FIELD_WRONG_WIRE_TYPE",
        "retry usage has wrong wire type": "RETRY_USAGE_WRONG_WIRE_TYPE",
        "step usage has wrong wire type": "STEP_USAGE_WRONG_WIRE_TYPE",
        "generation chat field has wrong wire type": "GENERATION_CHAT_WRONG_WIRE_TYPE",
        "generation timestamp field has wrong wire type": "GENERATION_TIMESTAMP_WRONG_WIRE_TYPE",
        "generation usage field has wrong wire type": "GENERATION_USAGE_WRONG_WIRE_TYPE",
    }

    def __init__(self, reason: str):
        super().__init__(reason)
        self.code = self._CODES.get(reason, "PROTO_ERROR")


@dataclass(frozen=True)
class _Field:
    number: int
    wire: int
    value: int | bytes


@dataclass
class _Observation:
    conversation_hash: str
    database_hash: str
    source: str
    source_index: int
    retry_index: int
    aliases: set[str]
    timestamp: datetime | None
    timestamp_source: str
    timestamp_fallback_available: bool
    counters: dict[str, int]
    raw_counter_fields: dict[str, int | None]
    reasoning: int
    presence: dict[str, bool]
    identity_kinds: set[str]
    model: str | None
    model_enum_id: int | None
    provider_enum_id: int | None

    def vector(self) -> tuple[int, int, int, int]:
        return tuple(self.counters[name] for name in _COUNTER_NAMES)  # type: ignore[return-value]


@dataclass
class _Scan:
    observations: list[_Observation] = field(default_factory=list)
    stats: Counter = field(default_factory=Counter)
    warnings: Counter = field(default_factory=Counter)
    inventory: Counter = field(default_factory=Counter)
    missing_sources: list[str] = field(default_factory=list)
    failure_diagnostics: list[dict] = field(default_factory=list)


def discover_roots(home: Path | None = None) -> list[Path]:
    """Return known Antigravity history and IDE storage roots that exist.

    Discovery is deliberately shallow: it checks named Antigravity stores and
    profile locations only. It does not walk credential folders or browser
    profiles. The caller may add private profile roots through configuration.
    """
    home = Path(home) if home is not None else Path.home()
    candidates: list[Path] = []
    gemini = home / ".gemini"
    for name in sorted(_PROFILE_STORE_NAMES):
        candidates.append(gemini / name)

    for container_name in ("profiles", "accounts"):
        container = gemini / container_name
        if container.is_dir():
            try:
                for profile in container.iterdir():
                    if not profile.is_dir() or profile.is_symlink():
                        continue
                    for store_name in sorted(_PROFILE_STORE_NAMES):
                        candidates.append(profile / store_name)
                        candidates.append(profile / ".gemini" / store_name)
            except OSError:
                pass

    app_roots = [
        home / "Library" / "Application Support" / "Antigravity",
        home / "Library" / "Application Support" / "Google" / "Antigravity",
        home / ".config" / "Antigravity",
        home / ".config" / "antigravity",
        home / ".local" / "share" / "Antigravity",
    ]
    for variable, suffix in (("XDG_CONFIG_HOME", "Antigravity"), ("XDG_DATA_HOME", "Antigravity")):
        configured = os.environ.get(variable)
        if configured:
            candidates.append(Path(configured).expanduser() / suffix)
    candidates.extend(app_roots)

    # VS Code style user profiles keep state databases in this fixed subtree.
    for app_root in app_roots:
        candidates.append(app_root / "User" / "globalStorage")
        profiles = app_root / "User" / "profiles"
        if profiles.is_dir():
            try:
                for profile in profiles.iterdir():
                    if profile.is_dir() and not profile.is_symlink():
                        candidates.append(profile / "globalStorage")
            except OSError:
                pass

    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            if not candidate.is_dir():
                continue
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def _read_varint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    for index in range(10):
        if position >= len(data):
            raise _ProtoError("truncated varint")
        byte = data[position]
        position += 1
        payload = byte & 0x7F
        shift = index * 7
        if shift == 63 and payload > 1:
            raise _ProtoError("varint overflow")
        value |= payload << shift
        if byte < 0x80:
            return value, position
        if shift == 63:
            raise _ProtoError("varint overflow")
    raise _ProtoError("varint overflow")


def _decode_fields(data: object) -> list[_Field]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise _ProtoError("protobuf blob missing")
    blob = bytes(data)
    if len(blob) > _PROTO_LIMIT:
        raise _ProtoError("protobuf blob too large")
    result: list[_Field] = []
    position = 0
    while position < len(blob):
        tag, position = _read_varint(blob, position)
        number, wire = tag >> 3, tag & 7
        if number <= 0:
            raise _ProtoError("invalid field number")
        if wire == 0:
            value, position = _read_varint(blob, position)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if position + size > len(blob):
                raise _ProtoError("truncated fixed value")
            value = blob[position : position + size]
            position += size
        elif wire == 2:
            size, position = _read_varint(blob, position)
            if size > _PROTO_LIMIT or position + size > len(blob):
                raise _ProtoError("truncated length-delimited value")
            value = blob[position : position + size]
            position += size
        else:
            # Groups are unsupported by the stored schema and rejected by the
            # pinned parser too; fail this record rather than guessing lengths.
            raise _ProtoError("unsupported wire type")
        result.append(_Field(number, wire, value))
    return result


def _values(fields: Sequence[_Field], number: int, wire: int) -> list[int | bytes]:
    return [item.value for item in fields if item.number == number and item.wire == wire]


def _varint(fields: Sequence[_Field], number: int) -> int | None:
    values = _values(fields, number, 0)
    if not values:
        return None
    value = int(values[-1])
    if value > _MAX_COUNTER:
        raise _ProtoError("counter out of range")
    return value


def _text(fields: Sequence[_Field], number: int) -> str | None:
    values = _values(fields, number, 2)
    if not values:
        return None
    raw = bytes(values[-1])
    if not raw or len(raw) > 4096:
        return None
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not value.strip() or any(ord(char) < 32 for char in value):
        return None
    # Hash source identifiers exactly as stored. Trimming can accidentally
    # collapse two distinct opaque IDs into the same identity.
    return value


def _safe_model(fields: Sequence[_Field], enum_id: int | None = None) -> str | None:
    if enum_id in _MODEL_IDS:
        return _MODEL_IDS[enum_id]
    for number in (19, 21, 12, 8):
        label = _text(fields, number)
        if label:
            normalized = _MODEL_LABELS.get(" ".join(label.casefold().split()))
            if normalized:
                return normalized
    return None


def _timestamp_message(data: bytes) -> datetime | None:
    fields = _decode_fields(data)
    seconds = _varint(fields, 1)
    nanos = _varint(fields, 2) or 0
    # The version-pinned parser rejects pre-epoch/zero timestamps and clamps
    # nanoseconds. This stricter adapter treats invalid nanoseconds as unknown.
    if seconds is None or seconds <= 0 or seconds > 253402300799:
        return None
    if nanos >= 1_000_000_000:
        return None
    try:
        return datetime.fromtimestamp(seconds + nanos / 1_000_000_000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _timestamp(fields: Sequence[_Field], number: int) -> datetime | None:
    for value in _values(fields, number, 2):
        try:
            parsed = _timestamp_message(bytes(value))
        except _ProtoError:
            continue
        if parsed is not None:
            return parsed
    return None


def _identity(kind: str, raw: str) -> str:
    return f"id:{kind}:{digest('antigravity:' + kind + ':' + raw)}"


def _identities(fields: Sequence[_Field]) -> tuple[set[str], set[str]]:
    aliases: set[str] = set()
    kinds: set[str] = set()
    for number, kind in ((7, "message"), (11, "response"), (12, "provider_assigned")):
        value = _text(fields, number)
        if value:
            aliases.add(_identity(kind, value))
            kinds.add(kind)
    return aliases, kinds


def _normalize_output(
    total: int | None, visible: int | None, reasoning: int | None
) -> tuple[int, int]:
    raw_total = total or 0
    raw_visible = visible or 0
    raw_reasoning = reasoning or 0
    output = max(raw_total, raw_visible + raw_reasoning)
    visible_output = max(raw_visible, output - raw_reasoning)
    reasoning_output = max(raw_reasoning, output - visible_output)
    return output, reasoning_output


def _usage_observation(
    blob: bytes,
    *,
    conversation_hash: str,
    database_hash: str,
    source: str,
    source_index: int,
    retry_index: int,
    slot: str,
    timestamp: datetime | None,
    timestamp_source: str,
    timestamp_fallback_available: bool,
    model_fallback: str | None,
    scan: _Scan,
) -> _Observation | None:
    fields = _decode_fields(blob)
    for field_number in _TOKEN_FIELDS:
        if any(item.number == field_number and item.wire != 0 for item in fields):
            raise _ProtoError("usage counter has wrong wire type")
    if any(item.number in (1, 6) and item.wire != 0 for item in fields):
        scan.stats["enum_wire_mismatches"] += 1
        scan.stats["schema_errors"] += 1
        scan.warnings["ENUM_SCHEMA_DRIFT"] += 1
    if any(item.number in (7, 11, 12) and item.wire != 2 for item in fields):
        scan.stats["identity_wire_mismatches"] += 1

    raw = {number: _varint(fields, number) for number in _TOKEN_FIELDS}
    presence = {name: raw[number] is not None for number, name in _FIELD_NAMES.items()}
    if not any(presence.values()):
        return None

    if timestamp is None and timestamp_fallback_available:
        # trajectory_metadata_blob generally timestamps a session, not a call.
        # Keep the event undated and expose only this availability bit.
        scan.stats["timestamp_fallback_available_events"] += 1

    # Model/provider enums are deliberately excluded from token arithmetic.
    model_enum = _varint(fields, 1)
    aliases, identity_kinds = _identities(fields)
    structural = digest(
        "antigravity-row:" + conversation_hash + ":" + source + ":" + slot
    )
    aliases.add("row:" + structural)
    total_output, reasoning = _normalize_output(raw[3], raw[10], raw[9])
    counters = {
        "fresh_input": raw[2] or 0,
        "cache_read": raw[5] or 0,
        "cache_write": raw[4] or 0,
        "output": total_output,
    }
    raw_counter_fields = {
        "fresh_input": raw[2],
        "reported_output": raw[3],
        "cache_write": raw[4],
        "cache_read": raw[5],
        "reasoning": raw[9],
        "visible_output": raw[10],
    }
    if raw[3] is not None and raw[9] is not None and raw[10] is not None:
        if raw[3] != raw[9] + raw[10]:
            scan.stats["output_split_mismatches"] += 1
    if any(number not in _KNOWN_USAGE_FIELDS for number in {item.number for item in fields}):
        scan.stats["unknown_proto_fields"] += len(
            {item.number for item in fields if item.number not in _KNOWN_USAGE_FIELDS}
        )
    return _Observation(
        conversation_hash=conversation_hash,
        database_hash=database_hash,
        source=source,
        source_index=source_index,
        retry_index=retry_index,
        aliases=aliases,
        timestamp=timestamp,
        timestamp_source=timestamp_source,
        timestamp_fallback_available=timestamp_fallback_available,
        counters=counters,
        raw_counter_fields=raw_counter_fields,
        reasoning=reasoning,
        presence=presence,
        identity_kinds=identity_kinds,
        model=_safe_model(fields, model_enum) or model_fallback,
        model_enum_id=model_enum,
        provider_enum_id=_varint(fields, 6),
    )


def _retry_observations(
    fields: Sequence[_Field],
    field_number: int,
    *,
    conversation_hash: str,
    database_hash: str,
    source: str,
    source_index: int,
    slot: str,
    timestamp: datetime | None,
    timestamp_source: str,
    timestamp_fallback_available: bool,
    model_fallback: str | None,
    scan: _Scan,
) -> list[_Observation]:
    result: list[_Observation] = []
    retries = _values(fields, field_number, 2)
    if any(item.number == field_number and item.wire != 2 for item in fields):
        raise _ProtoError("retry field has wrong wire type")
    for retry_index, retry_blob in enumerate(retries):
        retry_fields = _decode_fields(bytes(retry_blob))
        usage_blobs = _values(retry_fields, 2, 2)
        if any(item.number == 2 and item.wire != 2 for item in retry_fields):
            raise _ProtoError("retry usage has wrong wire type")
        for usage_index, usage_blob in enumerate(usage_blobs):
            observation = _usage_observation(
                bytes(usage_blob),
                conversation_hash=conversation_hash,
                database_hash=database_hash,
                source=source,
                source_index=source_index,
                retry_index=retry_index + 1,
                slot=f"{slot}:retry:{retry_index}:{usage_index}",
                timestamp=timestamp,
                timestamp_source=timestamp_source,
                timestamp_fallback_available=timestamp_fallback_available,
                model_fallback=model_fallback,
                scan=scan,
            )
            if observation is not None:
                result.append(observation)
    return result


def _parse_step_metadata(
    blob: bytes,
    *,
    conversation_hash: str,
    database_hash: str,
    row_index: int,
    trajectory_timestamp: datetime | None,
    scan: _Scan,
) -> list[_Observation]:
    fields = _decode_fields(blob)
    timestamp = _timestamp(fields, 8) or _timestamp(fields, 1)
    timestamp_source = "steps" if timestamp is not None else "undated"
    timestamp_fallback_available = timestamp is None and trajectory_timestamp is not None
    model_fallback = None
    model_info_blobs = _values(fields, 24, 2)
    if model_info_blobs:
        model_fields = _decode_fields(bytes(model_info_blobs[-1]))
        model_id = _varint(model_fields, 1)
        model_fallback = _safe_model(model_fields, model_id)
    result: list[_Observation] = []
    usage_blobs = _values(fields, 9, 2)
    if any(item.number == 9 and item.wire != 2 for item in fields):
        raise _ProtoError("step usage has wrong wire type")
    for usage_index, usage_blob in enumerate(usage_blobs):
        observation = _usage_observation(
            bytes(usage_blob),
            conversation_hash=conversation_hash,
            database_hash=database_hash,
            source="steps",
            source_index=row_index,
            retry_index=0,
            slot=f"{row_index}:main:{usage_index}",
            timestamp=timestamp,
            timestamp_source=timestamp_source,
            timestamp_fallback_available=timestamp_fallback_available,
            model_fallback=model_fallback,
            scan=scan,
        )
        if observation is not None:
            result.append(observation)
    result.extend(
        _retry_observations(
            fields,
            28,
            conversation_hash=conversation_hash,
            database_hash=database_hash,
            source="steps",
            source_index=row_index,
            slot=f"{row_index}:step",
            timestamp=timestamp,
            timestamp_source=timestamp_source,
            timestamp_fallback_available=timestamp_fallback_available,
            model_fallback=model_fallback,
            scan=scan,
        )
    )
    return result


def _parse_generation_blob(
    blob: bytes,
    *,
    conversation_hash: str,
    database_hash: str,
    row_index: int,
    trajectory_timestamp: datetime | None,
    scan: _Scan,
) -> list[_Observation]:
    outer = _decode_fields(blob)
    chat_blobs = _values(outer, 1, 2)
    if any(item.number == 1 and item.wire != 2 for item in outer):
        raise _ProtoError("generation chat field has wrong wire type")
    result: list[_Observation] = []
    for chat_index, chat_blob in enumerate(chat_blobs):
        chat = _decode_fields(bytes(chat_blob))
        generation_time = None
        generation_infos = _values(chat, 9, 2)
        if any(item.number == 9 and item.wire != 2 for item in chat):
            raise _ProtoError("generation timestamp field has wrong wire type")
        for info in generation_infos:
            info_fields = _decode_fields(bytes(info))
            generation_time = _timestamp(info_fields, 4) or generation_time
        if generation_infos and generation_time is None:
            scan.stats["timestamp_parse_errors"] += 1
        timestamp_source = "gen_metadata" if generation_time is not None else "undated"
        timestamp_fallback_available = generation_time is None and trajectory_timestamp is not None
        chat_model = _safe_model(chat, _varint(chat, 3))
        usage_blobs = _values(chat, 4, 2)
        if any(item.number == 4 and item.wire != 2 for item in chat):
            raise _ProtoError("generation usage field has wrong wire type")
        for usage_index, usage_blob in enumerate(usage_blobs):
            observation = _usage_observation(
                bytes(usage_blob),
                conversation_hash=conversation_hash,
                database_hash=database_hash,
                source="gen_metadata",
                source_index=row_index,
                retry_index=0,
                slot=f"{row_index}:chat:{chat_index}:main:{usage_index}",
                timestamp=generation_time,
                timestamp_source=timestamp_source,
                timestamp_fallback_available=timestamp_fallback_available,
                model_fallback=chat_model,
                scan=scan,
            )
            if observation is not None:
                result.append(observation)
        result.extend(
            _retry_observations(
                chat,
                17,
                conversation_hash=conversation_hash,
                database_hash=database_hash,
                source="gen_metadata",
                source_index=row_index,
                slot=f"{row_index}:chat:{chat_index}",
                timestamp=generation_time,
                timestamp_source=timestamp_source,
                timestamp_fallback_available=timestamp_fallback_available,
                model_fallback=chat_model,
                scan=scan,
            )
        )
    return result


def _path_store(path: Path) -> str:
    names = {part.casefold() for part in path.parts}
    if "antigravity-cli" in names:
        return "cli"
    if "antigravity-ide" in names or path.name.casefold() == "state.vscdb" or "globalstorage" in names:
        return "ide"
    if "antigravity-backup" in names:
        return "backup"
    if "antigravity" in names:
        return "app"
    return "configured"


def _sqlite_header(path: Path) -> tuple[str, int | None]:
    """Classify a candidate from its SQLite header without exposing its path."""
    try:
        size = path.stat().st_size
        with path.open("rb") as source:
            header = source.read(16)
    except OSError:
        return "HEADER_READ_FAILURE", None
    if size == 0:
        return "EMPTY", 0
    magic = b"SQLite format 3\x00"
    if header == magic:
        return "SQLITE", size
    if len(header) < len(magic) and magic.startswith(header):
        return "TRUNCATED_SQLITE_MAGIC", size
    return "NON_SQLITE", size


def _file_metadata(path: Path) -> tuple[int, int, int, int, int, int] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _validated_copy_is_unrecognized(
    path: Path, *, expected_summary: bool, expected_trajectory: bool
) -> str:
    """Confirm a timed-out small SQLite candidate has no known usage schema.

    This is a diagnostic fallback only. It refuses data-bearing SQLite
    sidecars, copies the bounded database and any empty sidecars into a private
    temporary directory, verifies source metadata and hashes before/after the
    copy, then checks the copy with SQLite's quick_check. It never accepts
    counters from the copy.
    """
    if expected_summary or expected_trajectory:
        return "EXPECTED_LOCATION"
    suffixes = ("-wal", "-shm", "-journal")
    sources = {"database": path}
    sources.update({suffix[1:]: Path(str(path) + suffix) for suffix in suffixes})
    try:
        if any(source.is_symlink() for source in sources.values()):
            return "SIDECAR_SYMLINK"
        before_metadata = {name: _file_metadata(source) for name, source in sources.items()}
        if before_metadata["database"] is None:
            return "DATABASE_MISSING"
        if any(
            item is not None and item[2] > 0
            for name, item in before_metadata.items()
            if name != "database"
        ):
            return "DATA_SIDECAR_PRESENT"
        total_size = sum(item[2] for item in before_metadata.values() if item is not None)
        if total_size > _MAX_TIMEOUT_DIAGNOSE_BYTES:
            return "COPY_SIZE_LIMIT"
        before_hashes = {
            name: _file_sha256(sources[name]) if before_metadata[name] is not None else None
            for name in sources
        }
    except OSError:
        return "SOURCE_METADATA_FAILURE"

    with tempfile.TemporaryDirectory(prefix="tokenometer-antigravity-probe-") as directory:
        base = Path(directory) / "candidate.sqlite"
        copies = {"database": base}
        copies.update({name: Path(str(base) + "-" + name) for name in ("wal", "shm", "journal")})
        try:
            for name, source in sources.items():
                if before_metadata[name] is not None:
                    shutil.copyfile(source, copies[name])
            copy_metadata = {name: _file_metadata(copy) for name, copy in copies.items()}
            copy_hashes = {
                name: _file_sha256(copies[name]) if copy_metadata[name] is not None else None
                for name in copies
            }
            after_metadata = {name: _file_metadata(source) for name, source in sources.items()}
            after_hashes = {
                name: _file_sha256(sources[name]) if after_metadata[name] is not None else None
                for name in sources
            }
            if not (
                before_metadata == after_metadata
                and before_hashes == after_hashes == copy_hashes
            ):
                return "SOURCE_CHANGED_OR_COPY_MISMATCH"

            uri = "file:" + quote(str(base.resolve()), safe="/") + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=_SQLITE_TIMEOUT)
            try:
                connection.execute("PRAGMA query_only=ON")
                check = connection.execute("PRAGMA quick_check").fetchone()
                if check is None or check[0] != "ok":
                    return "QUICK_CHECK_FAILED"
                tables = {
                    str(row[0]).casefold()
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if {"steps", "gen_metadata", "conversation_summaries"} & tables:
                    return "USAGE_SCHEMA_PRESENT"
                return "RECOVERED_NONUSAGE"
            finally:
                connection.close()
        except (OSError, sqlite3.Error, ValueError):
            return "COPY_CHECK_FAILED"


def _source_ref(path: Path) -> str:
    return digest(str(path.resolve()))


def _append_failure_diagnostic(scan: _Scan, diagnostic: dict) -> None:
    if len(scan.failure_diagnostics) < _MAX_FAILURE_DIAGNOSTICS:
        scan.failure_diagnostics.append(diagnostic)
    else:
        scan.stats["failure_diagnostics_omitted"] += 1


def _record_proto_failure(
    scan: _Scan,
    path: Path,
    kind: str,
    index: int,
    byte_size: int,
    error: _ProtoError,
) -> None:
    _append_failure_diagnostic(scan, {
        "source_ref": _source_ref(path),
        "kind": kind,
        "index": index,
        "byte_size": byte_size,
        "error_code": error.code,
    })


def _record_database_failure(
    scan: _Scan,
    path: Path,
    store: str,
    header_class: str,
    byte_size: int | None,
    error_class: str,
    copy_probe_status: str | None = None,
) -> None:
    diagnostic = {
        "source_ref": _source_ref(path),
        "kind": "sqlite_candidate",
        "store": store,
        "header_class": header_class,
        "byte_size": byte_size,
        "error_class": error_class,
    }
    if copy_probe_status is not None:
        diagnostic["copy_probe_status"] = copy_probe_status
    _append_failure_diagnostic(scan, diagnostic)


def _candidate_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root.resolve()]
    if not root.is_dir():
        return []
    candidates: list[Path] = []
    for name in ("conversation_summaries.db",):
        item = root / name
        if item.is_file() and not item.is_symlink():
            candidates.append(item)
    try:
        candidates.extend(
            item for item in sorted(root.iterdir(), key=lambda path: path.name)
            if item.is_file() and not item.is_symlink() and item.suffix.casefold() in _SQLITE_EXTENSIONS
        )
    except OSError:
        pass
    for relative in ("conversations", "brain"):
        directory = root / relative
        if directory.is_dir() and not directory.is_symlink():
            try:
                candidates.extend(
                    item for item in sorted(directory.iterdir(), key=lambda path: path.name)
                    if item.is_file() and not item.is_symlink() and item.suffix.casefold() in _SQLITE_EXTENSIONS
                )
            except OSError:
                pass
    # Antigravity IDE may keep one state database per VS Code profile.
    state_candidates = [root / "User" / "globalStorage" / "state.vscdb"]
    profiles = root / "User" / "profiles"
    if profiles.is_dir() and not profiles.is_symlink():
        try:
            state_candidates.extend(
                profile / "globalStorage" / "state.vscdb"
                for profile in profiles.iterdir()
                if profile.is_dir() and not profile.is_symlink()
            )
        except OSError:
            pass
    for item in state_candidates:
        if item.is_file() and not item.is_symlink():
            candidates.append(item)
    result: list[Path] = []
    seen: set[Path] = set()
    for item in candidates:
        try:
            resolved = item.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def _readonly_snapshot(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    source = sqlite3.connect(uri, uri=True, timeout=_SQLITE_TIMEOUT)
    snapshot = sqlite3.connect(":memory:")
    source.execute("PRAGMA query_only=ON")
    deadline = time.monotonic() + _snapshot_timeout_seconds()

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("snapshot deadline")

    try:
        source.backup(snapshot, pages=256, progress=progress, sleep=0.025)
    except BaseException:
        snapshot.close()
        raise
    finally:
        source.close()
    snapshot.row_factory = sqlite3.Row
    snapshot.execute("PRAGMA query_only=ON")
    query_deadline = time.monotonic() + _QUERY_SECONDS
    snapshot.set_progress_handler(lambda: int(time.monotonic() >= query_deadline), 1000)
    return snapshot


def _table_columns(snapshot: sqlite3.Connection, table: str) -> set[str]:
    # Table names are internal constants, never sourced from database contents.
    return {str(row[1]) for row in snapshot.execute(f'PRAGMA table_info("{table}")')}


def _trajectory_timestamp(snapshot: sqlite3.Connection, scan: _Scan) -> datetime | None:
    """Read the first valid recorded trajectory timestamp, never file mtime."""
    if "trajectory_metadata_blob" not in {
        str(row[0]) for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        return None
    columns = _table_columns(snapshot, "trajectory_metadata_blob")
    if "data" not in columns:
        scan.stats["schema_errors"] += 1
        scan.warnings["TRAJECTORY_METADATA_SCHEMA_DRIFT"] += 1
        return None
    query_deadline = time.monotonic() + _QUERY_SECONDS
    snapshot.set_progress_handler(lambda: int(time.monotonic() >= query_deadline), 1000)
    for row in snapshot.execute('SELECT "data" FROM trajectory_metadata_blob ORDER BY rowid ASC'):
        try:
            fields = _decode_fields(row["data"])
            values = _values(fields, 2, 2)
            for value in values:
                try:
                    parsed = _timestamp_message(bytes(value))
                except _ProtoError:
                    parsed = None
                if parsed is not None:
                    scan.inventory["trajectory_timestamp_indexes"] += 1
                    return parsed
        except _ProtoError:
            scan.stats["timestamp_parse_errors"] += 1
            scan.warnings["TIMESTAMP_METADATA_INVALID"] += 1
    return None


def _row_index(row: sqlite3.Row, fallback: int) -> int:
    try:
        value = row["idx"]
        index = int(value) if value is not None else fallback
        return index if index >= 0 else fallback
    except (KeyError, TypeError, ValueError, OverflowError):
        return fallback


def _scan_database(path: Path, scan: _Scan) -> None:
    scan.inventory["sqlite_candidates"] += 1
    store = _path_store(path)
    scan.inventory[f"databases_{store}"] += 1
    header_class, byte_size = _sqlite_header(path)
    scan.inventory[f"candidate_header:{header_class}"] += 1
    if header_class == "EMPTY":
        scan.inventory["empty_candidates"] += 1
    elif header_class == "SQLITE":
        scan.inventory["sqlite_header_candidates"] += 1
    elif header_class == "NON_SQLITE":
        scan.inventory["non_sqlite_candidates"] += 1
        # A filename extension alone does not make a file a SQLite usage store.
        # Keep this visible as a coverage warning without treating it as a
        # failed read of a recognized database.
        scan.warnings["NON_SQLITE_CANDIDATE"] += 1
        return
    expected_summary = path.name.casefold() == "conversation_summaries.db"
    expected_trajectory = "conversations" in {part.casefold() for part in path.parts}
    snapshot: sqlite3.Connection | None = None
    try:
        snapshot = _readonly_snapshot(path)
        tables = {
            str(row[0])
            for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "conversation_summaries" in tables:
            columns = _table_columns(snapshot, "conversation_summaries")
            if "conversation_id" not in columns:
                scan.stats["schema_errors"] += 1
                scan.warnings["SUMMARY_SCHEMA_DRIFT"] += 1
            else:
                count = snapshot.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0]
                scan.inventory["summary_indexes"] += 1
                scan.inventory["indexed_conversations"] += int(count or 0)
        elif expected_summary:
            scan.stats["schema_errors"] += 1
            scan.warnings["SUMMARY_SCHEMA_DRIFT"] += 1

        has_steps = "steps" in tables
        has_generations = "gen_metadata" in tables
        trajectory_timestamp = _trajectory_timestamp(snapshot, scan)
        if not has_steps and not has_generations:
            scan.inventory["unrecognized_databases"] += 1
            if expected_trajectory:
                scan.stats["schema_errors"] += 1
                scan.warnings["USAGE_SCHEMA_MISSING"] += 1
            scan.inventory["databases_read"] += 1
            return

        scan.inventory["trajectory_databases"] += 1
        conversation_id = path.stem
        conversation_hash = digest("antigravity-session-id:" + conversation_id)
        database_hash = digest(str(path.resolve()))
        if has_steps:
            columns = _table_columns(snapshot, "steps")
            if "metadata" not in columns:
                scan.stats["schema_errors"] += 1
                scan.warnings["STEPS_SCHEMA_DRIFT"] += 1
            else:
                idx_sql = '"idx"' if "idx" in columns else "rowid"
                query_deadline = time.monotonic() + _QUERY_SECONDS
                snapshot.set_progress_handler(lambda: int(time.monotonic() >= query_deadline), 1000)
                cursor = snapshot.execute(
                    f'SELECT {idx_sql} AS idx, "metadata" FROM steps WHERE "metadata" IS NOT NULL ORDER BY {idx_sql}'
                )
                for position, row in enumerate(cursor):
                    scan.stats["records_inspected"] += 1
                    index = _row_index(row, position)
                    try:
                        observations = _parse_step_metadata(
                            bytes(row["metadata"]),
                            conversation_hash=conversation_hash,
                            database_hash=database_hash,
                            row_index=index,
                            trajectory_timestamp=trajectory_timestamp,
                            scan=scan,
                        )
                        scan.observations.extend(observations)
                    except _ProtoError as exc:
                        scan.stats["malformed_records"] += 1
                        scan.stats["schema_errors"] += 1
                        scan.warnings["MALFORMED_PROTO_RECORD"] += 1
                        _record_proto_failure(
                            scan, path, "steps", index, len(row["metadata"]), exc
                        )

        if has_generations:
            columns = _table_columns(snapshot, "gen_metadata")
            if "data" not in columns:
                scan.stats["schema_errors"] += 1
                scan.warnings["GENERATION_SCHEMA_DRIFT"] += 1
            else:
                idx_sql = '"idx"' if "idx" in columns else "rowid"
                size_sql = '"size"' if "size" in columns else "NULL"
                query_deadline = time.monotonic() + _QUERY_SECONDS
                snapshot.set_progress_handler(lambda: int(time.monotonic() >= query_deadline), 1000)
                cursor = snapshot.execute(
                    f'SELECT {idx_sql} AS idx, "data", {size_sql} AS stored_size '
                    f'FROM gen_metadata WHERE "data" IS NOT NULL ORDER BY {idx_sql}'
                )
                for position, row in enumerate(cursor):
                    scan.stats["records_inspected"] += 1
                    index = _row_index(row, position)
                    data = row["data"]
                    if row["stored_size"] is not None:
                        try:
                            if int(row["stored_size"]) != len(data):
                                scan.stats["stored_size_mismatches"] += 1
                        except (TypeError, ValueError, OverflowError):
                            scan.stats["stored_size_mismatches"] += 1
                    try:
                        observations = _parse_generation_blob(
                            bytes(data),
                            conversation_hash=conversation_hash,
                            database_hash=database_hash,
                            row_index=index,
                            trajectory_timestamp=trajectory_timestamp,
                            scan=scan,
                        )
                        scan.observations.extend(observations)
                    except _ProtoError as exc:
                        scan.stats["malformed_records"] += 1
                        scan.stats["schema_errors"] += 1
                        scan.warnings["MALFORMED_PROTO_RECORD"] += 1
                        _record_proto_failure(
                            scan, path, "gen_metadata", index, len(data), exc
                        )
        scan.inventory["databases_read"] += 1
    except Exception as exc:
        # Exception text can contain private paths or source metadata.
        copy_probe_status = None
        if isinstance(exc, TimeoutError):
            copy_probe_status = _validated_copy_is_unrecognized(
                path,
                expected_summary=expected_summary,
                expected_trajectory=expected_trajectory,
            )
        if copy_probe_status == "RECOVERED_NONUSAGE":
            # This small, stable copy has a clean SQLite check and no recognized
            # usage tables. Do not read counters from it. Retain safe provenance
            # for the timed-out source snapshot so the classification is auditable.
            scan.inventory["recovered_nonusage_timeout"] += 1
            scan.inventory["unrecognized_databases"] += 1
            scan.inventory["databases_read"] += 1
            _append_failure_diagnostic(scan, {
                "source_ref": _source_ref(path),
                "kind": "sqlite_candidate",
                "store": store,
                "header_class": header_class,
                "byte_size": byte_size,
                "error_class": "SNAPSHOT_TIMEOUT",
                "classification": "RECOVERED_NONUSAGE",
            })
            return
        scan.stats["read_errors"] += 1
        scan.stats["read_errors_by_store:" + store] += 1
        if isinstance(exc, TimeoutError):
            scan.stats["read_errors_by_kind:SNAPSHOT_TIMEOUT"] += 1
        elif isinstance(exc, sqlite3.Error):
            name = getattr(exc, "sqlite_errorname", None)
            safe_name = name if isinstance(name, str) and re.fullmatch(r"SQLITE_[A-Z0-9_]{1,64}", name) else "SQLITE_OTHER"
            scan.stats["read_errors_by_kind:" + safe_name] += 1
        elif isinstance(exc, OSError):
            error_class = "OS_READ_FAILURE"
            scan.stats["read_errors_by_kind:" + error_class] += 1
        else:
            error_class = "OTHER_READ_FAILURE"
            scan.stats["read_errors_by_kind:" + error_class] += 1
        if isinstance(exc, TimeoutError):
            error_class = "SNAPSHOT_TIMEOUT"
        elif isinstance(exc, sqlite3.Error):
            error_class = safe_name
        _record_database_failure(
            scan,
            path,
            store,
            header_class,
            byte_size,
            error_class,
            copy_probe_status=copy_probe_status,
        )
        scan.warnings["SQLITE_READ_FAILURE"] += 1
    finally:
        if snapshot is not None:
            snapshot.close()


class _UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a == b:
            return
        if self.rank[a] < self.rank[b]:
            a, b = b, a
        self.parent[b] = a
        if self.rank[a] == self.rank[b]:
            self.rank[a] += 1


def _event_key(aliases: Iterable[str]) -> str:
    aliases = sorted(set(aliases))
    for kind in ("id:response:", "id:provider_assigned:", "id:message:", "row:"):
        selected = [alias for alias in aliases if alias.startswith(kind)]
        if selected:
            return "antigravity:" + digest("antigravity-event:" + selected[0])
    return "antigravity:" + digest("antigravity-event:empty")


def _components(observations: Sequence[_Observation]) -> list[list[_Observation]]:
    union = _UnionFind(len(observations))
    owner: dict[str, int] = {}
    for index, observation in enumerate(observations):
        for alias in observation.aliases:
            existing = owner.get(alias)
            if existing is None:
                owner[alias] = index
            else:
                union.union(index, existing)
    groups: dict[int, list[_Observation]] = defaultdict(list)
    for index, observation in enumerate(observations):
        groups[union.find(index)].append(observation)
    return list(groups.values())


def _component_conflicts(group: Sequence[_Observation]) -> bool:
    by_alias: dict[str, list[_Observation]] = defaultdict(list)
    for observation in group:
        for alias in observation.aliases:
            by_alias[alias].append(observation)
    for alias, records in by_alias.items():
        if alias.startswith("row:"):
            by_database: dict[str, set[tuple[int, int, int, int]]] = defaultdict(set)
            for record in records:
                by_database[record.database_hash].add(record.vector())
            # The same conversation row copied into another store must agree.
            if len(by_database) > 1:
                vectors = set().union(*by_database.values())
                if len(vectors) > 1:
                    return True
        elif alias.startswith("id:"):
            by_conversation: dict[str, list[_Observation]] = defaultdict(list)
            for record in records:
                by_conversation[record.conversation_hash].append(record)
            if len(by_conversation) > 1:
                per_conversation = {
                    conversation: tuple(
                        max(record.counters[name] for record in conversation_records)
                        for name in _COUNTER_NAMES
                    )
                    for conversation, conversation_records in by_conversation.items()
                }
                if len(set(per_conversation.values())) > 1:
                    return True
    return False


def _counter_receipt(group: Sequence[_Observation]) -> dict:
    ordered = sorted(
        group,
        key=lambda record: (
            record.database_hash,
            record.source,
            record.source_index,
            record.retry_index,
            tuple(
                -1 if record.raw_counter_fields[name] is None else record.raw_counter_fields[name]
                for name in (
                    "fresh_input",
                    "cache_write",
                    "cache_read",
                    "reported_output",
                    "visible_output",
                    "reasoning",
                )
            ),
        ),
    )
    observations = [
        {
            "input": record.raw_counter_fields["fresh_input"],
            "cache_creation": record.raw_counter_fields["cache_write"],
            "cache_read": record.raw_counter_fields["cache_read"],
            "output_total": record.raw_counter_fields["reported_output"],
            "visible_output": record.raw_counter_fields["visible_output"],
            "reasoning": record.raw_counter_fields["reasoning"],
        }
        for record in ordered
    ]
    source_refs = [
        {
            "source": record.database_hash,
            "kind": record.source,
            "index": record.source_index,
            "retry": record.retry_index,
        }
        for record in ordered
    ]
    field_presence = {
        name: sum(observation[name] is not None for observation in observations)
        for name in (
            "input",
            "cache_creation",
            "cache_read",
            "output_total",
            "visible_output",
            "reasoning",
        )
    }
    timestamps = [record.timestamp for record in ordered]
    known_timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
    if not known_timestamps:
        timestamp_basis = "unknown"
    elif len(ordered) == 1 or (
        len(known_timestamps) == len(ordered) and len(set(known_timestamps)) == 1
    ):
        timestamp_basis = "record"
    else:
        timestamp_basis = "merged_source_metadata"
    return {
        "schema": "antigravity.protobuf.v1",
        "source_refs": source_refs,
        "observations": observations,
        "source_timestamps": [
            utc_timestamp(record.timestamp.isoformat()) if record.timestamp is not None else None
            for record in ordered
        ],
        "timestamp_basis": timestamp_basis,
        "model_enum_ids": sorted(
            {record.model_enum_id for record in ordered if record.model_enum_id is not None}
        ),
        "provider_enum_ids": sorted(
            {record.provider_enum_id for record in ordered if record.provider_enum_id is not None}
        ),
        "field_presence": field_presence,
    }


def _aggregate(scan: _Scan, cutoff: datetime) -> list[UsageEvent]:
    eligible: list[_Observation] = []
    for observation in scan.observations:
        if observation.timestamp is not None and observation.timestamp > cutoff:
            scan.stats["future_events"] += 1
            scan.stats["event_status:future_excluded"] += 1
        else:
            eligible.append(observation)

    events: list[UsageEvent] = []
    for group in _components(eligible):
        aliases = {alias for record in group for alias in record.aliases}
        key = _event_key(aliases)
        conflict = _component_conflicts(group)
        if conflict:
            scan.stats["conflicting_identity_groups"] += 1
            scan.stats["conflicting_events_excluded"] += len(group)
            scan.stats["event_status:conflict_excluded"] += 1
            scan.warnings["IDENTITY_COUNTER_CONFLICT"] += 1
            continue

        counters = {
            name: max(record.counters[name] for record in group)
            for name in _COUNTER_NAMES
        }
        # Reasoning is retained separately as a part of output; merged groups
        # have already been normalized, so take its maximum from source rows.
        reasoning = max(record.reasoning for record in group)
        output = max(counters["output"], reasoning)
        output = max(output, reasoning)
        field_presence = {
            name: any(record.presence.get(name, False) for record in group)
            for name in _FIELD_NAMES.values()
        }
        if not all(field_presence.get(required, False) for required in (
            "fresh_input", "cache_read", "cache_write"
        )) or not any(field_presence.get(name, False) for name in (
            "reported_output", "reasoning", "visible_output"
        )):
            scan.stats["incomplete_events"] += 1
        for name in _FIELD_NAMES.values():
            if not field_presence.get(name, False):
                scan.stats["defaulted_counter_fields"] += 1

        timestamps = {record.timestamp for record in group if record.timestamp is not None}
        timestamp_conflict = len(timestamps) > 1
        timestamp = next(iter(timestamps)) if len(timestamps) == 1 else None
        if timestamp_conflict:
            scan.stats["timestamp_conflict_events"] += 1
            scan.warnings["TIMESTAMP_CONFLICT"] += 1
        session_aliases = tuple(
            sorted({digest("antigravity-session:" + record.conversation_hash) for record in group})
        )
        session_key = session_aliases[0]
        model_values = {record.model for record in group if record.model}
        model = next(iter(model_values)) if len(model_values) == 1 else None
        if len(model_values) > 1:
            scan.stats["model_conflicts"] += 1
        source_kinds = sorted({record.source for record in group})
        source_kind = "antigravity_" + "+".join(source_kinds)
        event = UsageEvent(
            key=key,
            platform="antigravity",
            session_key=session_key,
            session_aliases=session_aliases,
            timestamp=utc_timestamp(timestamp.isoformat()) if timestamp else None,
            fresh_input=counters["fresh_input"],
            cache_read=counters["cache_read"],
            cache_write=counters["cache_write"],
            output=output,
            reasoning=min(reasoning, output),
            model=model,
            source_kind=source_kind,
            counter_receipt=_counter_receipt(group),
        )
        events.append(event)
        scan.stats["duplicate_events"] += max(0, len(group) - 1)
        if timestamp is None:
            scan.stats["undated_events"] += 1
            scan.stats["event_status:included_undated"] += 1
        else:
            scan.stats["event_status:included"] += 1
        for name in _FIELD_NAMES.values():
            if field_presence.get(name, False):
                scan.stats["counter_fields_observed:" + name] += 1
    return events


def collect(
    roots: Sequence[Path], cutoff: datetime, cache: Path | None = None
) -> CollectionResult:
    """Collect all recorded Antigravity usage events below ``roots``.

    ``cache`` is accepted for API compatibility but intentionally unused: a
    complete readonly scan avoids stale identity when the main DB timestamp is
    unchanged but a WAL has new committed pages. No source payload is cached.
    """
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware")
    cutoff_utc = cutoff.astimezone(timezone.utc)
    scan = _Scan()
    normalized_roots: list[Path] = []
    seen_roots: set[Path] = set()
    for index, root_value in enumerate(roots):
        root = Path(root_value).expanduser()
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root.absolute()
        if resolved in seen_roots:
            continue
        seen_roots.add(resolved)
        if not resolved.exists():
            scan.missing_sources.append(f"configured_root_{index + 1}")
            continue
        normalized_roots.append(resolved)
    scan.inventory["roots_supplied"] = len(seen_roots)
    scan.inventory["roots_present"] = len(normalized_roots)
    scan.inventory["roots_missing"] = len(scan.missing_sources)

    candidates: list[Path] = []
    seen_files: set[Path] = set()
    scan.inventory["roots_with_candidates"] = 0
    for root in normalized_roots:
        try:
            found = _candidate_files(root)
        except OSError:
            scan.stats["read_errors"] += 1
            scan.warnings["ROOT_ENUMERATION_FAILURE"] += 1
            continue
        if found:
            scan.inventory["roots_with_candidates"] += 1
        for path in found:
            if path not in seen_files:
                seen_files.add(path)
                candidates.append(path)
    for path in candidates:
        _scan_database(path, scan)

    events = _aggregate(scan, cutoff_utc)
    for key in (
        "future_events",
        "undated_events",
        "duplicate_events",
        "conflicting_identity_groups",
        "conflicting_events_excluded",
        "incomplete_events",
        "malformed_records",
        "unknown_proto_fields",
        "output_split_mismatches",
        "model_conflicts",
        "timestamp_parse_errors",
        "timestamp_fallback_available_events",
        "timestamp_conflict_events",
        "stored_size_mismatches",
        "read_errors",
        "schema_errors",
    ):
        scan.stats.setdefault(key, 0)
    for name in _FIELD_NAMES.values():
        scan.stats.setdefault("counter_fields_observed:" + name, 0)
    scan.stats["errors"] = int(scan.stats["read_errors"] + scan.stats["schema_errors"])
    scan.stats["source_records"] = len(scan.observations)
    scan.stats["cache_used"] = 0
    scan.stats["cache_ignored"] = int(cache is not None)
    scan.stats["snapshot_timeout_seconds"] = _snapshot_timeout_seconds()
    scan.stats["metadata_incomplete"] = 1
    scan.stats["resets"] = 0

    inventory = {key: int(scan.inventory.get(key, 0)) for key in sorted(scan.inventory)}
    inventory["store_counts"] = {
        store: int(scan.inventory.get("databases_" + store, 0))
        for store in _STORE_NAMES
    }
    stats = {
        key: float(value) if isinstance(value, float) else int(value)
        for key, value in sorted(scan.stats.items())
    }
    stats["missing_sources"] = sorted(scan.missing_sources)
    stats["source_inventory"] = inventory
    stats["counter_field_presence"] = {
        name: int(scan.stats.get("counter_fields_observed:" + name, 0))
        for name in _FIELD_NAMES.values()
    }
    stats["event_status_counts"] = {
        status.removeprefix("event_status:"): int(value)
        for status, value in sorted(scan.stats.items())
        if status.startswith("event_status:")
    }
    stats["failure_diagnostics"] = list(scan.failure_diagnostics)
    stats["warning_counts"] = dict(sorted(scan.warnings.items()))
    warnings = [f"{code}:{count}" for code, count in sorted(scan.warnings.items()) if count]
    return CollectionResult(events=events, stats=stats, warnings=warnings[:20])
