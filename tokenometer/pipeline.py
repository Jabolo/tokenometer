"""One-shot collection orchestration. No scheduling, authentication or network."""
from __future__ import annotations
import importlib
from datetime import datetime, timezone
from pathlib import Path
from .capture import canonicalize, persist_run, verify_run
from .models import CollectionResult
from .config import resolve_roots

PLATFORMS = ('codex', 'antigravity')


def source_roots(config, platforms=PLATFORMS, overrides=None):
    roots = {}; adapters = {}
    overrides = overrides or {}
    for platform in platforms:
        adapter = importlib.import_module('tokenometer.collectors.' + platform)
        explicit = overrides.get(platform) or config[platform + '_roots']
        roots[platform] = resolve_roots(explicit) if explicit else adapter.discover_roots(Path.home())
        adapters[platform] = adapter
    return roots, adapters


def read_sources(roots, adapters, cutoff, since=None, excluded_sessions=(), progress=None):
    results = {}
    for platform, adapter in adapters.items():
        if progress: progress('Reading ' + platform + ' local history…')
        try:
            result = adapter.collect(roots[platform], cutoff, cache=None)
            if not isinstance(result, CollectionResult): raise ValueError('Invalid adapter result')
            results[platform] = result
        except Exception:
            # Source exception text can contain private paths or payloads.
            results[platform] = CollectionResult(stats={'errors':1}, warnings=['collector_failed'])
    events, reconciliation = canonicalize([event for result in results.values() for event in result.events],
                                           cutoff, since, excluded_sessions)
    return events, results, reconciliation


def collect_capture(config, output, platforms=PLATFORMS, overrides=None, cutoff=None, progress=None):
    roots, adapters = source_roots(config, platforms, overrides)
    cutoff = cutoff or datetime.now(timezone.utc)
    started = datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
    events, results, reconciliation = read_sources(roots, adapters, cutoff, progress=progress)
    if progress: progress('Verifying and saving the private counter capture…')
    run = persist_run(Path(output), events, results, cutoff, None, config['timezone'], started, roots, reconciliation)
    return run, verify_run(run)


def refresh(config, runs_base, ledger_path, public_path, platforms=PLATFORMS, overrides=None,
            cutoff=None, from_runs=(), progress=None):
    from .ledger import ingest_runs, ledger_snapshot
    from .public import export_snapshot
    runs = list(from_runs)
    capture = None
    if not runs:
        run, capture = collect_capture(config, runs_base, platforms, overrides, cutoff, progress)
        runs = [run]
    if progress: progress('Reconciling event identities with the durable ledger…')
    ingestion = ingest_runs(Path(ledger_path), [Path(run) for run in runs])
    snapshot = ledger_snapshot(Path(ledger_path), timezone=config['timezone'])
    if progress: progress('Writing aggregate-only website data…')
    exported = export_snapshot(snapshot, Path(public_path))
    return {'capture':capture, 'runs':[str(run) for run in runs], 'ingestion':ingestion,
            'ledger':str(ledger_path), 'public_export':str(public_path),
            'summary':{'total_tokens':exported['totals']['total'], 'events':exported['events'],
                       'coverage_status':exported['quality']['coverage_status']},
            'website_data_updated':True}
