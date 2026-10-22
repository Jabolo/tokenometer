"""Public release and aggregate-data privacy guard.

The release path list is intentionally explicit. Private captures, ledgers,
profiles, receipts, research notes and local evidence are not publication
inputs. Run ``check-paths`` with the exact paths intended for release before
staging, and validate every aggregate immediately before upload.
"""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import utc_timestamp

MAX_SAFE = 2**53 - 1
MAX_FILE_BYTES = 16 * 1024 * 1024
PUBLIC_ARTIFACT = "tokenometer.public.aggregate.v1"
PUBLIC_TITLE = "AI mileage"
PUBLIC_NOTE = (
    "Recorded local history. Some history may be missing. Reused context is included; "
    "reasoning is part of output."
)
COUNTERS = ("fresh_input", "cache_read", "cache_write", "output", "total")
PROVIDER_LABELS = {"codex": "Codex", "antigravity": "Antigravity"}
EXECUTION_STATES = {"complete", "partial", "unknown", "unobserved"}
COVERAGE_STATES = {"qualified", "partial", "unknown", "unobserved"}

# This is the only set of paths a publication workflow may stage or package.
# Keep historical docs and operator state out; add a new doc only after review.
PUBLIC_RELEASE_PATHS = frozenset({
    ".gitignore",
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "tokenometer.example.json",
    "tokenometer/__init__.py",
    "tokenometer/__main__.py",
    "tokenometer/aggregate.py",
    "tokenometer/automation.py",
    "tokenometer/capture.py",
    "tokenometer/cli.py",
    "tokenometer/collectors/__init__.py",
    "tokenometer/collectors/antigravity.py",
    "tokenometer/collectors/codex.py",
    "tokenometer/compare.py",
    "tokenometer/config.py",
    "tokenometer/ledger.py",
    "tokenometer/models.py",
    "tokenometer/pipeline.py",
    "tokenometer/public.py",
    "tokenometer/receipts.py",
    "tokenometer/server.py",
    "tokenometer/release_guard.py",
    "scripts/collect.sh",
    "scripts/daily-refresh.sh",
    "scripts/install-daily-refresh.py",
    "scripts/preview.sh",
    "scripts/refresh.sh",
    "tests/test_antigravity.py",
    "tests/test_capture.py",
    "tests/test_codex.py",
    "tests/test_compare.py",
    "tests/test_ledger.py",
    "tests/test_pipeline.py",
    "tests/test_public.py",
    "tests/test_receipts.py",
    "tests/test_server.py",
    "tests/test_automation.py",
    "tests/test_release_guard.py",
    "web/app.js",
    "web/data.json",
    "web/favicon.svg",
    "web/index.html",
    "web/styles.css",
    "web/_headers",
    "web/404.html",
    "docs/PUBLISHING.md",
    "docs/DAILY-REFRESH.md",
    ".github/workflows/check.yml",
})

_TOP_KEYS = {
    "schema_version", "artifact", "title", "generated_at", "timezone",
    "totals", "events", "first_recorded_date", "last_recorded_date",
    "recorded_active_days", "providers", "daily", "quality",
}
_TOTAL_KEYS = set(COUNTERS)
_PROVIDER_KEYS = {
    "label", "totals", "events", "undated_events", "latest_cutoff",
    "latest_capture_completed_at", "last_successful_cutoff", "execution_status",
    "coverage_status", "source_errors",
}
_DAILY_KEYS = {"date", "platform", "events", *COUNTERS}
_QUALITY_KEYS = {
    "coverage_status", "historical_account_completeness", "conflicted_events",
    "undated_events", "undated_totals", "dated_totals", "source_errors", "note",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    _require(type(value) is dict and set(value) == keys, f"Invalid {label} fields")
    return value


def _count(value: Any, label: str) -> int:
    _require(type(value) is int and 0 <= value <= MAX_SAFE, f"Invalid {label}")
    return value


def _date(value: Any, label: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    _require(type(value) is str, f"Invalid {label}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Invalid {label}") from None
    _require(parsed.isoformat() == value, f"Invalid {label}")
    return value


def _timestamp(value: Any, label: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    _require(type(value) is str, f"Invalid {label}")
    normalized = utc_timestamp(value)
    _require(normalized is not None and normalized == value, f"Invalid {label}")
    return value


def _totals(value: Any, label: str) -> dict[str, int]:
    source = _object(value, _TOTAL_KEYS, label)
    result = {name: _count(source[name], f"{label}.{name}") for name in COUNTERS}
    _require(result["total"] == sum(result[name] for name in COUNTERS[:-1]),
             f"Inconsistent {label}")
    return result


def validate_public_payload(value: Any) -> dict[str, Any]:
    """Validate and return a canonical aggregate payload, rejecting extra data.

    Every string is either a fixed public label/note, a valid IANA time zone,
    or a strictly formatted date/timestamp. No event, account, path, receipt or
    arbitrary free-text field can pass this schema.
    """
    source = _object(value, _TOP_KEYS, "public aggregate")
    _require(type(source["schema_version"]) is int and source["schema_version"] == 1,
             "Invalid aggregate schema version")
    _require(source["artifact"] == PUBLIC_ARTIFACT and source["title"] == PUBLIC_TITLE,
             "Invalid aggregate identity")
    generated_at = _timestamp(source["generated_at"], "generated_at")
    zone = source["timezone"]
    _require(type(zone) is str and len(zone) <= 128, "Invalid aggregate timezone")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Invalid aggregate timezone") from None

    overall = _totals(source["totals"], "totals")
    events = _count(source["events"], "events")
    first = _date(source["first_recorded_date"], "first_recorded_date", nullable=True)
    last = _date(source["last_recorded_date"], "last_recorded_date", nullable=True)
    active_days = _count(source["recorded_active_days"], "recorded_active_days")

    source_providers = _object(source["providers"], set(PROVIDER_LABELS), "providers")
    providers: dict[str, dict[str, Any]] = {}
    for name, label in PROVIDER_LABELS.items():
        item = _object(source_providers[name], _PROVIDER_KEYS, f"provider {name}")
        _require(item["label"] == label, f"Invalid provider {name} label")
        execution = item["execution_status"]
        coverage = item["coverage_status"]
        _require(type(execution) is str and execution in EXECUTION_STATES,
                 f"Invalid provider {name} execution status")
        _require(type(coverage) is str and coverage in COVERAGE_STATES,
                 f"Invalid provider {name} coverage status")
        providers[name] = {
            "label": label,
            "totals": _totals(item["totals"], f"provider {name} totals"),
            "events": _count(item["events"], f"provider {name} events"),
            "undated_events": _count(item["undated_events"], f"provider {name} undated events"),
            "latest_cutoff": _timestamp(item["latest_cutoff"], f"provider {name} latest_cutoff", True),
            "latest_capture_completed_at": _timestamp(
                item["latest_capture_completed_at"], f"provider {name} latest_capture_completed_at", True
            ),
            "last_successful_cutoff": _timestamp(
                item["last_successful_cutoff"], f"provider {name} last_successful_cutoff", True
            ),
            "execution_status": execution,
            "coverage_status": coverage,
            "source_errors": _count(item["source_errors"], f"provider {name} source errors"),
        }
        _require(providers[name]["undated_events"] <= providers[name]["events"],
                 f"Inconsistent provider {name} event counts")

    quality_source = _object(source["quality"], _QUALITY_KEYS, "quality")
    quality_coverage = quality_source["coverage_status"]
    _require(type(quality_coverage) is str and quality_coverage in {"qualified", "partial"},
             "Invalid quality coverage status")
    _require(quality_source["historical_account_completeness"] == "unknown",
             "Invalid historical completeness")
    note = quality_source["note"]
    _require(note == PUBLIC_NOTE, "Invalid public quality note")
    quality = {
        "coverage_status": quality_coverage,
        "historical_account_completeness": "unknown",
        "conflicted_events": _count(quality_source["conflicted_events"], "conflicted events"),
        "undated_events": _count(quality_source["undated_events"], "undated events"),
        "undated_totals": _totals(quality_source["undated_totals"], "undated totals"),
        "dated_totals": _totals(quality_source["dated_totals"], "dated totals"),
        "source_errors": _count(quality_source["source_errors"], "source errors"),
        "note": PUBLIC_NOTE,
    }
    _require(all(overall[key] == quality["dated_totals"][key] + quality["undated_totals"][key]
                 for key in COUNTERS), "Inconsistent dated and undated totals")
    _require(quality["undated_events"] == sum(item["undated_events"] for item in providers.values()),
             "Inconsistent undated event count")
    _require(quality["source_errors"] == sum(item["source_errors"] for item in providers.values()),
             "Inconsistent source error count")
    _require(events == sum(item["events"] for item in providers.values()),
             "Inconsistent provider event count")
    _require(all(overall[key] == sum(item["totals"][key] for item in providers.values())
                 for key in COUNTERS), "Inconsistent provider totals")

    rows_source = source["daily"]
    _require(type(rows_source) is list, "Invalid daily aggregates")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    daily_by_provider = {name: {key: 0 for key in COUNTERS} for name in PROVIDER_LABELS}
    daily_events_by_provider = {name: 0 for name in PROVIDER_LABELS}
    daily_events = 0
    for raw in rows_source:
        row = _object(raw, _DAILY_KEYS, "daily aggregate")
        row_date = _date(row["date"], "daily date")
        platform = row["platform"]
        _require(type(platform) is str and platform in PROVIDER_LABELS, "Invalid daily platform")
        key = (row_date, platform)
        _require(key not in seen, "Duplicate daily aggregate")
        seen.add(key)
        normalized = {
            "date": row_date,
            "platform": platform,
            "events": _count(row["events"], "daily events"),
            **_totals({name: row[name] for name in COUNTERS}, "daily totals"),
        }
        _require(normalized["events"] > 0, "Daily aggregate must contain an event")
        rows.append(normalized)
        daily_events_by_provider[platform] += normalized["events"]
        for name in COUNTERS:
            daily_by_provider[platform][name] += normalized[name]
        daily_events += normalized["events"]

    expected_rows = sorted(rows, key=lambda row: (row["date"], row["platform"]))
    _require(rows == expected_rows, "Daily aggregates are not canonically ordered")
    dates = {row["date"] for row in rows}
    _require((min(dates, default=None), max(dates, default=None), len(dates)) ==
             (first, last, active_days), "Inconsistent recorded activity dates")
    _require(all(sum(row[key] for row in rows) == quality["dated_totals"][key]
                 for key in COUNTERS), "Inconsistent daily totals")
    _require(daily_events + quality["undated_events"] == events,
             "Inconsistent dated and undated event counts")
    for name, provider in providers.items():
        _require(provider["events"] == daily_events_by_provider[name] + provider["undated_events"],
                 f"Inconsistent event counts for {name}")
        _require(all(provider["totals"][key] >= daily_by_provider[name][key] for key in COUNTERS),
                 f"Inconsistent daily totals for {name}")
    undated_from_providers = {
        key: sum(providers[name]["totals"][key] - daily_by_provider[name][key]
                 for name in providers)
        for key in COUNTERS
    }
    _require(undated_from_providers == quality["undated_totals"],
             "Inconsistent provider and undated totals")

    return {
        "schema_version": 1,
        "artifact": PUBLIC_ARTIFACT,
        "title": PUBLIC_TITLE,
        "generated_at": generated_at,
        "timezone": zone,
        "totals": overall,
        "events": events,
        "first_recorded_date": first,
        "last_recorded_date": last,
        "recorded_active_days": active_days,
        "providers": providers,
        "daily": rows,
        "quality": quality,
    }


_SECRET_PATTERNS = (
    ("credential-token", re.compile(rb"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{30,}|AIza[0-9A-Za-z_-]{35}|sk-[A-Za-z0-9_-]{24,})\b")),
    ("private-key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("jwt-like-token", re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)
_HOME_PATH = re.compile(rb"/(?:Users|home)/([A-Za-z0-9._-]+)(?:/|\b)")
_EMAIL = re.compile(rb"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_SYNTHETIC_HOME_NAMES = {
    b"user", b"example", b"test", b"tester", b"alice", b"bob", b"private",
    b"canary", b"private_canary", b"synthetic", b"fixture",
}
_RESERVED_EMAIL_DOMAINS = {
    b"example.com", b"example.org", b"example.net", b"example.invalid", b"test.invalid",
}


def _privacy_findings(path: str, content: bytes) -> list[str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{path}:1: non-utf8-content"]
    findings: list[str] = []
    for line_number, line in enumerate(content.splitlines(), 1):
        for category, pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(f"{path}:{line_number}: {category}")
        for match in _HOME_PATH.finditer(line):
            if match.group(1).lower() not in _SYNTHETIC_HOME_NAMES:
                findings.append(f"{path}:{line_number}: personal-home-path")
        for match in _EMAIL.finditer(line):
            domain = match.group().lower().split(b"@", 1)[1]
            if (domain not in _RESERVED_EMAIL_DOMAINS and
                    not domain.endswith((b".example", b".invalid", b".test"))):
                findings.append(f"{path}:{line_number}: personal-email")
        if b"\x00" in line:
            findings.append(f"{path}:{line_number}: binary-content")
    return sorted(set(findings))


def _safe_relative_path(value: str) -> str:
    _require(type(value) is str and value != "", "Invalid release path")
    _require("\\" not in value and not value.startswith("/"), "Invalid release path")
    parsed = PurePosixPath(value)
    _require(all(part not in {"", ".", ".."} for part in parsed.parts), "Invalid release path")
    normalized = parsed.as_posix()
    _require(normalized == value, "Invalid release path")
    _require(normalized in PUBLIC_RELEASE_PATHS, "Unapproved public path")
    return normalized


def _validate_file_bytes(path: str, content: bytes) -> None:
    _require(len(content) <= MAX_FILE_BYTES, f"{path}: file exceeds release limit")
    if path == "web/data.json":
        try:
            value = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise ValueError("web/data.json: invalid aggregate JSON") from None
        validate_public_payload(value)
        return
    findings = _privacy_findings(path, content)
    if findings:
        raise ValueError("\n".join(findings))


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def validate_aggregate_file(path: str | Path) -> dict[str, Any]:
    """Load and validate one aggregate JSON file without printing its contents."""
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Aggregate path must be a regular bounded file")
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Invalid "):
            raise
        raise ValueError("Invalid aggregate file") from None
    return validate_public_payload(value)


def validate_public_paths(root: str | Path, paths: list[str]) -> dict[str, Any]:
    """Validate exact publication candidates before staging or packaging."""
    root = Path(root).resolve()
    _require(bool(paths), "No public paths supplied")
    _require(len(set(paths)) == len(paths), "Duplicate public path")
    checked = 0
    findings: list[str] = []
    for raw_path in paths:
        relative = _safe_relative_path(raw_path)
        target = root / relative
        current = root
        for part in PurePosixPath(relative).parts:
            current = current / part
            _require(not current.is_symlink(), f"{relative}: symlink in release path")
        try:
            resolved = target.resolve(strict=True)
            resolved.relative_to(root)
            _require(resolved.is_file(), f"{relative}: not a regular file")
            _require(resolved.stat().st_size <= MAX_FILE_BYTES,
                     f"{relative}: file exceeds release limit")
            content = resolved.read_bytes()
        except (OSError, ValueError):
            raise ValueError(f"{relative}: missing or unsafe release file") from None
        try:
            _validate_file_bytes(relative, content)
        except ValueError as exc:
            findings.extend(str(exc).splitlines())
        checked += 1
    if findings:
        raise ValueError("\n".join(findings))
    return {"checked": checked, "paths": sorted(paths), "status": "pass"}


def validate_public_release(root: str | Path) -> dict[str, Any]:
    """Validate every path in the embedded public release manifest."""
    return validate_public_paths(root, sorted(PUBLIC_RELEASE_PATHS))


def _git_index_modes(root: Path) -> dict[str, tuple[str, int, str]]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--stage", "-z"], cwd=root, check=True, capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        raise ValueError("Cannot read Git index modes") from None
    modes: dict[str, tuple[str, int, str]] = {}
    try:
        for record in result.stdout.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_id, stage = metadata.split(b" ", 2)
            path = raw_path.decode("utf-8", errors="strict")
            modes[path] = (mode.decode("ascii"), int(stage), object_id.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Git index contains an invalid entry") from None
    return modes


def _read_git_blob(root: Path, object_id: str) -> bytes:
    try:
        size_result = subprocess.run(
            ["git", "cat-file", "-s", object_id], cwd=root, check=True, capture_output=True,
        )
        size = int(size_result.stdout.strip())
        _require(size <= MAX_FILE_BYTES, "Git object exceeds release limit")
        return subprocess.run(
            ["git", "cat-file", "blob", object_id], cwd=root, check=True, capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError, ValueError):
        raise ValueError("Cannot read bounded Git blob") from None


def validate_staged_release(root: str | Path) -> dict[str, Any]:
    """Read the Git index and reject staged paths outside the public manifest."""
    root = Path(root).resolve()
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRTD", "--no-renames", "-z"],
            cwd=root, check=True, capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        raise ValueError("Cannot read Git index") from None
    try:
        names = [name.decode("utf-8", errors="strict") for name in result.stdout.split(b"\0") if name]
    except UnicodeDecodeError:
        raise ValueError("Git index contains a non-UTF-8 path") from None
    _require(bool(names), "No staged release files")
    _require(len(set(names)) == len(names), "Duplicate staged path")
    index_modes = _git_index_modes(root)
    findings: list[str] = []
    for name in names:
        relative = _safe_relative_path(name)
        entry = index_modes.get(relative)
        _require(entry is not None and entry[0] in {"100644", "100755"} and entry[1] == 0,
                 f"{relative}: staged file is not a regular file")
        try:
            blob = _read_git_blob(root, entry[2])
            _validate_file_bytes(relative, blob)
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            findings.extend(str(exc).splitlines() or [f"{relative}: invalid staged file"])
    if findings:
        raise ValueError("\n".join(findings))
    return {"checked": len(names), "paths": sorted(names), "status": "pass"}


def validate_committed_tree(root: str | Path) -> dict[str, Any]:
    """Validate the committed release tree for CI after the first public commit."""
    root = Path(root).resolve()
    try:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "-z", "HEAD"],
            cwd=root, check=True, capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        raise ValueError("Cannot read committed Git tree") from None
    tree: dict[str, tuple[str, str, str]] = {}
    try:
        for record in result.stdout.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.split(b" ", 2)
            name = raw_path.decode("utf-8", errors="strict")
            tree[name] = (mode.decode("ascii"), object_type.decode("ascii"), object_id.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Committed tree contains an invalid entry") from None
    names = list(tree)
    _require(set(names) == PUBLIC_RELEASE_PATHS, "Committed tree differs from public release manifest")
    findings: list[str] = []
    for name in names:
        relative = _safe_relative_path(name)
        mode, object_type, object_id = tree[relative]
        _require(mode in {"100644", "100755"} and object_type == "blob",
                 f"{relative}: committed entry is not a regular file")
        try:
            blob = _read_git_blob(root, object_id)
            _validate_file_bytes(relative, blob)
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            findings.extend(str(exc).splitlines() or [f"{relative}: invalid committed file"])
    if findings:
        raise ValueError("\n".join(findings))
    return {"checked": len(names), "paths": sorted(names), "status": "pass"}


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Tokenometer public release boundaries")
    subparsers = parser.add_subparsers(dest="command", required=True)
    paths_parser = subparsers.add_parser("check-paths", help="check explicit paths before staging")
    paths_parser.add_argument("--root", default=".")
    paths_parser.add_argument("--paths", nargs="+", required=True)
    release_parser = subparsers.add_parser("check-release", help="check the complete public manifest")
    release_parser.add_argument("--root", default=".")
    tree_parser = subparsers.add_parser("check-tree", help="check the committed release tree in CI")
    tree_parser.add_argument("--root", default=".")
    aggregate_parser = subparsers.add_parser("check-aggregate", help="validate aggregate JSON before upload")
    aggregate_parser.add_argument("path")
    staged_parser = subparsers.add_parser("check-staged", help="check staged paths and staged bytes")
    staged_parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    try:
        if args.command == "check-paths":
            result = validate_public_paths(args.root, args.paths)
        elif args.command == "check-release":
            result = validate_public_release(args.root)
        elif args.command == "check-tree":
            result = validate_committed_tree(args.root)
        elif args.command == "check-aggregate":
            payload = validate_aggregate_file(args.path)
            result = {"events": payload["events"], "status": "pass"}
        else:
            result = validate_staged_release(args.root)
    except ValueError as exc:
        print(f"release guard: FAIL\n{exc}", file=sys.stderr)
        return 1
    print(f"release guard: PASS ({result['checked'] if 'checked' in result else 'aggregate'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
