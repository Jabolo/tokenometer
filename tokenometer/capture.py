from __future__ import annotations
import csv
import hashlib
import json
import os
import stat
import sys
import tempfile
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from .aggregate import summarize
from .models import UsageEvent, CollectionResult, utc_timestamp
from .receipts import validate_receipt

SCHEMA_VERSION = 1
FILES = {'collection.json', 'summary.json', 'sources.private.json', 'events.jsonl', 'daily.csv'}


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    path.chmod(0o600)


def counter_signature(e):
    return e.fresh_input, e.cache_read, e.cache_write, e.output


def canonicalize(events, cutoff: datetime, since: datetime | None = None, excluded_sessions=()):
    groups = {}; counters = {'duplicates_removed': 0, 'conflicting_keys': 0,
        'receipt_conflicts': 0, 'future_excluded': 0, 'before_since_excluded': 0,
        'sessions_excluded': 0}
    for e in events:
        groups.setdefault(e.key, []).append(e)
    accepted = []
    for key, group in sorted(groups.items()):
        counters['duplicates_removed'] += len(group) - 1
        times = {utc_timestamp(e.timestamp) for e in group if e.timestamp is not None}
        counter_signatures = {(e.platform, counter_signature(e)) for e in group}
        known_reasoning = {e.reasoning for e in group if e.reasoning is not None}
        alias_sets = [set(e.session_aliases) for e in group]
        # A size-ordered subset chain is equivalent to pairwise comparability,
        # without quadratic work for a heavily copied event identity.
        ordered_alias_sets = sorted(alias_sets, key=len)
        owners_comparable = all(left.issubset(right)
            for left, right in zip(ordered_alias_sets, ordered_alias_sets[1:]))
        if (len(counter_signatures) != 1 or len(known_reasoning) > 1 or
                len(times) > 1 or not owners_comparable):
            counters['conflicting_keys'] += 1
            continue

        aliases = tuple(sorted(set().union(*alias_sets)))
        if set(aliases).intersection(excluded_sessions):
            counters['sessions_excluded'] += 1
            continue

        # Validate source versions before enriching metadata. A later copy may
        # supply reasoning, a timestamp, or a monotone alias superset, but its
        # receipt must remain the evidence for the selected event.
        for candidate in group:
            validate_receipt(candidate)

        timestamp_key = next(iter(times), None)
        timestamp_ambiguous = any(
            candidate.platform == 'antigravity' and
            len({utc_timestamp(value) for value in candidate.counter_receipt.get('source_timestamps', [])
                 if value is not None}) > 1
            for candidate in group
        )
        if timestamp_ambiguous:
            timestamp_key = None
        reasoning = next(iter(known_reasoning), None)
        models = {e.model for e in group if e.model}
        model = next(iter(models)) if len(models) == 1 else None

        # Prefer a source version that already carries the enriched fields.
        # Any combined event is then checked against that exact receipt; if no
        # source receipt supports the combination, exclude it with a specific
        # reconciliation diagnostic instead of fabricating evidence.
        candidates = sorted(group, key=lambda e: (
            timestamp_key is not None and utc_timestamp(e.timestamp) != timestamp_key,
            reasoning is not None and e.reasoning != reasoning,
            -len(e.session_aliases),
            json.dumps(e.to_dict(), sort_keys=True),
        ))
        event = None
        for candidate in candidates:
            candidate_time = utc_timestamp(candidate.timestamp) if candidate.timestamp else None
            merged = replace(candidate,
                session_key=aliases[0], session_aliases=aliases,
                timestamp=(candidate.timestamp if candidate_time == timestamp_key else timestamp_key),
                reasoning=reasoning, model=model)
            # Receipt validation depends on counters, reasoning, and time.
            # Those are unchanged in the common case, and the source version
            # was already validated above; alias/model normalization is safe.
            if merged.timestamp == candidate.timestamp and merged.reasoning == candidate.reasoning:
                event = merged
                break
            try:
                validate_receipt(merged)
            except ValueError:
                continue
            event = merged
            break
        if event is None:
            counters['conflicting_keys'] += 1
            counters['receipt_conflicts'] += 1
            continue

        if event.timestamp:
            parsed = datetime.fromisoformat(event.timestamp.replace('Z', '+00:00'))
            if parsed > cutoff:
                counters['future_excluded'] += 1; continue
            if since is not None and parsed < since:
                counters['before_since_excluded'] += 1; continue
        elif since is not None:
            counters['before_since_excluded'] += 1; continue
        accepted.append(event)
    return sorted(accepted, key=lambda e: (e.timestamp or '', e.platform, e.key)), counters


def statuses(providers, reconciliation, undated_events):
    errors = sum(int(value['stats'].get('errors', 0)) for value in providers.values())
    execution = 'partial' if errors else 'complete'
    issues = errors or undated_events or reconciliation.get('conflicting_keys', 0)
    for value in providers.values():
        stats = value['stats']
        issues = issues or bool(value['warnings']) or bool(stats.get('missing_sources'))
        for name in ('incomplete_events', 'conflicting_events_excluded', 'undated_events',
                     'malformed_records', 'resets', 'legacy_unverified', 'metadata_incomplete'):
            issues = issues or bool(stats.get(name))
    return execution, 'partial' if issues else 'qualified'


def private_base(base: Path):
    if base.is_symlink(): raise ValueError('Private output base must not be a symlink')
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = base.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Private output base must be owner-only (0700); choose a dedicated directory')


def promote_run(run: Path):
    """Verify before changing the pointer. Never promote a failed or incomplete read."""
    checked = verify_run(run)
    if checked['execution_status'] != 'complete': return
    base = run.parent
    # Serialize pointer selection and avoid an older fixed-cutoff run replacing a newer one.
    import fcntl
    fd = os.open(base / '.promotion.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        pointer = base / 'latest.json'
        cutoff = json.loads((run / 'collection.json').read_text())['cutoff']
        if pointer.exists():
            previous = json.loads(pointer.read_text())['run']
            if not isinstance(previous, str) or Path(previous).name != previous:
                raise ValueError('Invalid latest capture pointer')
            try:
                previous_check = verify_run(base / previous)
            except (OSError, ValueError, KeyError, TypeError):
                previous_check = None
            if previous_check and previous_check['execution_status'] == 'complete':
                old_cutoff = json.loads((base / previous / 'collection.json').read_text())['cutoff']
                if datetime.fromisoformat(old_cutoff.replace('Z', '+00:00')) > datetime.fromisoformat(cutoff.replace('Z', '+00:00')):
                    return
        handle, tmp = tempfile.mkstemp(prefix='.latest-', dir=base)
        try:
            with os.fdopen(handle, 'w') as output:
                json.dump({'run': run.name, 'schema_version': SCHEMA_VERSION}, output)
                output.write('\n'); output.flush(); os.fsync(output.fileno())
            os.replace(tmp, pointer)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    finally:
        os.close(fd)


def persist_run(base: Path, events: list[UsageEvent], results: dict[str, CollectionResult],
                cutoff: datetime, since: datetime | None, tz: str, started: str,
                roots: dict[str, list[Path]], reconciliation: dict, excluded_sessions=()) -> Path:
    private_base(base)
    name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '-' + uuid.uuid4().hex[:8]
    staging = base / ('.' + name + '.partial'); final = base / name
    staging.mkdir(mode=0o700)
    totals = summarize(events, tz)
    providers = {p: {'stats': r.stats, 'warnings': sorted(set(r.warnings))} for p, r in results.items()}
    execution, coverage = statuses(providers, reconciliation, sum(e.timestamp is None for e in events))
    receipt = {'schema_version': SCHEMA_VERSION, 'status': execution, 'execution_status': execution,
        'coverage_status': coverage, 'historical_account_completeness': 'unknown',
        'coverage': 'Recorded local history; completeness of all accounts cannot be established.',
        'started_at': started, 'completed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'cutoff': cutoff.isoformat().replace('+00:00', 'Z'),
        'since': since.isoformat().replace('+00:00', 'Z') if since else None,
        'timezone': tz, 'project_filter': None,
        'parameters': {'platforms': sorted(results), 'exclude_session_hashes': sorted(set(excluded_sessions)),
                       'snapshot_mode': 'full', 'network': False},
        'implementation': {'python': sys.version.split()[0],
            'source_sha256': {str(p.relative_to(Path(__file__).parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(Path(__file__).parent.rglob('*.py'))}},
        'counter_semantics': {'total': 'fresh_input + cache_read + cache_write + output',
             'output': 'Includes reasoning; do not add reasoning again.',
             'recorded_history': 'Not subscription spending, an invoice, words written, or a productivity score.'},
        'providers': providers, 'reconciliation': reconciliation,
        'summary': {k:v for k,v in totals.items() if k != 'daily'}}
    write_json(staging / 'sources.private.json', {p: [str(v) for v in paths] for p, paths in roots.items()})
    write_json(staging / 'collection.json', receipt)
    write_json(staging / 'summary.json', totals)
    with (staging / 'events.jsonl').open('w') as f:
        for e in events: f.write(json.dumps(e.to_dict(), ensure_ascii=False, sort_keys=True) + '\n')
    (staging / 'events.jsonl').chmod(0o600)
    with (staging / 'daily.csv').open('w', newline='') as f:
        columns = ['date', 'platform', 'events', *totals['totals'].keys()]
        writer = csv.DictWriter(f, fieldnames=columns); writer.writeheader(); writer.writerows(totals['daily'])
    (staging / 'daily.csv').chmod(0o600)
    files = []
    for file in sorted(staging.iterdir()):
        data = file.read_bytes(); files.append({'file': file.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    write_json(staging / 'manifest.json', {'schema_version': SCHEMA_VERSION, 'files': files})
    # Verify staged bytes before finalizing or advancing the pointer.
    verify_run(staging)
    os.replace(staging, final)
    promote_run(final)
    return final


def verify_run(run: Path) -> dict:
    if run.is_symlink(): raise ValueError('Capture directory may not be a symlink')
    manifest_path = run / 'manifest.json'
    if manifest_path.is_symlink(): raise ValueError('Manifest may not be a symlink')
    manifest = json.loads(manifest_path.read_text())
    rows = manifest['files']
    if manifest.get('schema_version') != SCHEMA_VERSION or len(rows) != len(FILES) or {r['file'] for r in rows} != FILES:
        raise ValueError('Unexpected capture file manifest')
    for row in rows:
        path = run / row['file']
        if path.is_symlink(): raise ValueError('Capture files may not be symlinks')
        data = path.read_bytes()
        if len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError('Capture checksum mismatch')
    events = []
    with (run / 'events.jsonl').open() as f:
        for line in f:
            if line.strip():
                event = UsageEvent(**json.loads(line)); validate_receipt(event); events.append(event)
    if len({e.key for e in events}) != len(events): raise ValueError('Duplicate event identity')
    receipt = json.loads((run / 'collection.json').read_text())
    if receipt.get('schema_version') != SCHEMA_VERSION or receipt.get('project_filter') is not None:
        raise ValueError('Unexpected capture scope')
    parameters = receipt['parameters']
    if parameters['platforms'] != sorted(receipt['providers']) or parameters['snapshot_mode'] != 'full' or parameters['network'] is not False:
        raise ValueError('Invalid capture parameters')
    if any(set(e.session_aliases).intersection(parameters['exclude_session_hashes']) for e in events):
        raise ValueError('Excluded session present in capture')
    if any(e.platform not in receipt['providers'] for e in events):
        raise ValueError('Event provider lies outside capture scope')
    cutoff_text = utc_timestamp(receipt['cutoff'])
    if cutoff_text is None: raise ValueError('Cutoff requires timezone')
    cutoff = datetime.fromisoformat(cutoff_text.replace('Z', '+00:00'))
    since_text = utc_timestamp(receipt['since']) if receipt['since'] else None
    if receipt['since'] and since_text is None: raise ValueError('Start requires timezone')
    since = datetime.fromisoformat(since_text.replace('Z', '+00:00')) if since_text else None
    if since and since > cutoff: raise ValueError('Start after cutoff')
    for event in events:
        timestamp = datetime.fromisoformat(event.timestamp.replace('Z', '+00:00')) if event.timestamp else None
        if timestamp and timestamp > cutoff: raise ValueError('Event after cutoff')
        if since and (timestamp is None or timestamp < since): raise ValueError('Event outside start boundary')
    actual = summarize(events, receipt['timezone'])
    if actual != json.loads((run / 'summary.json').read_text()): raise ValueError('Summary differs from event ledger')
    if receipt['summary'] != {k:v for k,v in actual.items() if k != 'daily'}:
        raise ValueError('Receipt summary differs from event ledger')
    execution, coverage = statuses(receipt['providers'], receipt['reconciliation'], sum(e.timestamp is None for e in events))
    if (receipt.get('status'), receipt.get('execution_status'), receipt.get('coverage_status'), receipt.get('historical_account_completeness')) != (execution, execution, coverage, 'unknown'):
        raise ValueError('Receipt status differs from coverage diagnostics')
    with (run / 'daily.csv').open() as f:
        reader = csv.DictReader(f); daily = list(reader)
        columns = ['date', 'platform', 'events', *actual['totals'].keys()]
        if reader.fieldnames != columns: raise ValueError('Unexpected daily columns')
    expected = [{key: str(row[key]) for key in columns} for row in actual['daily']]
    if daily != expected: raise ValueError('Daily rows differ from dated events')
    return {'verified': True, 'events': len(events), 'total_tokens': actual['totals']['total'],
            'status': execution, 'execution_status': execution, 'coverage_status': coverage,
            'historical_account_completeness': 'unknown'}
