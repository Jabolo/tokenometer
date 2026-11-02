"""Private, conservative reconciliation of OpenAI Analytics daily totals.

These reports have neither response identities nor counter categories. They are
not UsageEvents and must not be used to invent per-response or category data.
The caller must independently supply an exhaustive view of the Codex ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from typing import Iterable, Mapping

MAX_SAFE = 2**53 - 1
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_REPORTS = 1024


@dataclass(frozen=True)
class Report:
    # scope is an opaque private identity, never an export field.
    scope: str
    days: tuple[tuple[str, int], ...]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate report field')
        result[key] = value
    return result


def _instant(value, *, require_utc=False):
    if not isinstance(value, str):
        raise ValueError('Expected UTC timestamp')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('Expected UTC timestamp') from None
    if result.tzinfo is None or (require_utc and result.utcoffset() != timedelta(0)):
        raise ValueError('Expected UTC timestamp')
    return result.astimezone(timezone.utc)


def _midnight(value):
    result = _instant(value, require_utc=True)
    if result.time() != datetime.min.time():
        raise ValueError('Expected UTC midnight')
    return result


def _count(value):
    if type(value) is not int or not 0 <= value <= MAX_SAFE:
        raise ValueError('Invalid token count')
    return value


def _read_private_file(directory_fd, filename, limit):
    """Read a bounded regular file without following links or blocking on FIFOs."""
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except OSError:
        raise ValueError('Cannot open private report file') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('Invalid private report file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('Private report file too large')
        return data
    finally:
        os.close(fd)


def load_reports(directory) -> list[Report]:
    """Load an optional private report directory, rejecting links and traversal.

    The strict manifest contains schema_version 1 and reports entries with
    exactly file and provenance keys. No file names or provenance are exported.
    An existing but incomplete or malformed directory raises ValueError so a
    refresh cannot silently discard observations or unresolved contradictions.
    """
    path = Path(directory)
    if '..' in path.parts:
        raise ValueError('Invalid private report directory')
    # Open each component relative to its parent. This rejects symlinks in the
    # entire path and keeps later reads bound to the opened directory inode.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/' if path.is_absolute() else '.', flags)
    try:
        parts = path.parts[1:] if path.is_absolute() else path.parts
        for part in parts:
            if part == '.':
                continue
            try:
                child_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                return []
            except OSError:
                raise ValueError('Cannot open private report directory') from None
            os.close(fd)
            fd = child_fd
        raw = _read_private_file(fd, 'manifest.json', MAX_MANIFEST_BYTES)
        try:
            manifest = json.loads(raw, object_pairs_hook=_unique_object,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number')))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ValueError('Invalid private report manifest') from None
        if (not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'reports'}
                or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1
                or not isinstance(manifest['reports'], list) or len(manifest['reports']) > MAX_REPORTS):
            raise ValueError('Invalid private report manifest')
        loaded = []
        filenames = set()
        for entry in manifest['reports']:
            if not isinstance(entry, dict) or set(entry) != {'file', 'provenance'}:
                raise ValueError('Invalid private report manifest')
            name = entry['file']
            if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*\.json', name)
                    or name == 'manifest.json' or name in filenames or len(name) > 255):
                raise ValueError('Invalid private report filename')
            filenames.add(name)
            loaded.append(parse_report(_read_private_file(fd, name, MAX_REPORT_BYTES), entry['provenance']))
        return loaded
    finally:
        os.close(fd)


def parse_report(raw: bytes, provenance: Mapping) -> Report:
    """Validate original report bytes and a separate private provenance record.

    Required provenance keys: sha256, product ('codex'), scope (stable opaque
    SHA-256 for the selected account and user), start, end, timezone ('UTC'),
    captured_at, and complete (True). The caller must establish the meaning
    and stability of scope from the source selection; hashing an arbitrary
    label is not proof of identity or independent coverage. Intervals are
    half-open. Only an explicit complete, elapsed UTC interval is accepted.
    """
    required = {'sha256', 'product', 'scope', 'start', 'end', 'timezone', 'captured_at', 'complete'}
    if not isinstance(provenance, Mapping) or set(provenance) != required:
        raise ValueError('Invalid report provenance')
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_REPORT_BYTES:
        raise ValueError('Invalid report bytes')
    if provenance['sha256'] != sha256(raw).hexdigest():
        raise ValueError('Report checksum mismatch')
    if (provenance['product'] != 'codex' or provenance['timezone'] != 'UTC'
            or provenance['complete'] is not True
            or not isinstance(provenance['scope'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', provenance['scope'])):
        raise ValueError('Invalid report scope')
    start, end = _midnight(provenance['start']), _midnight(provenance['end'])
    captured = _instant(provenance['captured_at'])
    if end <= start or captured < end:
        raise ValueError('Report interval is not complete')
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number')))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ValueError('Invalid report JSON') from None
    if not isinstance(value, dict) or value.get('chart_key') != 'tokens_used':
        raise ValueError('Not a Codex token report')
    summary = value.get('summary')
    series = value.get('series')
    if (not isinstance(summary, dict) or summary.get('is_partial') is not False
            or not isinstance(series, list) or not series):
        raise ValueError('Partial or unexpected report')
    series_totals = {}
    for item in series:
        if (not isinstance(item, dict) or item.get('source') != 'model'
                or not isinstance(item.get('column'), str) or not item['column']
                or item['column'] in series_totals):
            raise ValueError('Partial or unexpected report')
        series_summary = item.get('summary')
        if not isinstance(series_summary, dict) or series_summary.get('is_partial') is not False:
            raise ValueError('Partial or unexpected report')
        series_totals[item['column']] = _count(series_summary.get('value'))
    total = _count(summary.get('value'))
    if series_totals.get('All') != total:
        raise ValueError('Report summary mismatch')
    rows = value.get('rows')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Missing daily rows')
    days = []
    observed_models = {}
    expected = start
    for row in rows:
        if not isinstance(row, dict) or 'Start Time' not in row or 'End Time' not in row or 'All' not in row:
            raise ValueError('Invalid daily row')
        row_start, row_end = _midnight(row['Start Time']), _midnight(row['End Time'])
        if row_start != expected or row_end != row_start + timedelta(days=1) or row_end > end:
            raise ValueError('Incomplete daily interval')
        amount = _count(row['All'])
        models = {k: _count(v) for k, v in row.items() if k not in ('Start Time', 'End Time', 'All')}
        if models and sum(models.values()) != amount:
            raise ValueError('Report model totals disagree')
        for model, tokens in models.items():
            observed_models[model] = observed_models.get(model, 0) + tokens
        days.append((row_start.date().isoformat(), amount))
        expected = row_end
    if expected != end or sum(v for _, v in days) != total:
        raise ValueError('Report rows do not cover or reconcile to summary')
    for model, tokens in series_totals.items():
        if model != 'All' and observed_models.get(model) != tokens:
            raise ValueError('Report model summary mismatch')
    return Report(provenance['scope'], tuple(days))


def eligible_daily(reports: Iterable[Report], *, cutoff: str, codex_timestamps: Iterable[str | None],
                   codex_has_conflicts: bool, codex_has_ambiguous_timestamps: bool) -> list[dict]:
    """Return only safely eligible UTC day totals, with no private provenance.

    timestamps must include *all* trusted Codex ledger events. Conflicts and
    ambiguous timestamps must include excluded/conflicted versions too. A
    caller unable to establish any of those facts must not call this function
    with a false flag. Recompute on every export, after ingestion, and use the
    same ledger snapshot/transaction to avoid a late-ingestion race.

    Multiple scopes for one day are withheld: distinct account labels do not
    prove disjoint usage. Repeated identical snapshots deduplicate; differing
    values for one scope/day are contradictory and withheld. This produces a
    conservative lower bound and does not establish historical completeness.
    """
    if type(codex_has_conflicts) is not bool or type(codex_has_ambiguous_timestamps) is not bool:
        raise ValueError('Missing ledger quality evidence')
    cutoff_time = _instant(cutoff)
    occupied = set()
    unknown_time = False
    for timestamp in codex_timestamps:
        if timestamp is None:
            unknown_time = True
        else:
            occupied.add(_instant(timestamp).date().isoformat())
    observations = {}
    for report in reports:
        if not isinstance(report, Report):
            raise ValueError('Invalid report')
        for day, value in report.days:
            if not isinstance(day, str) or _midnight(day + 'T00:00:00Z').date().isoformat() != day:
                raise ValueError('Invalid report day')
            _count(value)
            observations.setdefault(day, {}).setdefault(report.scope, set()).add(value)
    if unknown_time or codex_has_conflicts or codex_has_ambiguous_timestamps:
        return []
    result = []
    for day, scopes in sorted(observations.items()):
        if day in occupied or len(scopes) != 1:
            continue
        values = next(iter(scopes.values()))
        if len(values) != 1 or _midnight(day + 'T00:00:00Z') + timedelta(days=1) > cutoff_time:
            continue
        amount = next(iter(values))
        if amount:
            result.append({'date': day, 'total': amount})
    return result
