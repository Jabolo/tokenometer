from __future__ import annotations
import argparse
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import sys
import sqlite3
from . import __version__
from .config import load_config, resolve_roots, ConfigError
from .capture import canonicalize, persist_run, verify_run
from .aggregate import summarize
from .models import CollectionResult


PLATFORMS = ('codex', 'antigravity')


def date_argument(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None: raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError('Use an ISO timestamp with timezone, for example 2026-10-22T10:00:00Z') from None


def parser():
    p = argparse.ArgumentParser(prog='tokenometer', description='Collect locally recorded Codex and Antigravity usage. No model calls. No uploads.')
    p.add_argument('--version', action='version', version=__version__)
    commands = p.add_subparsers(dest='command', required=True)
    collect = commands.add_parser('collect', help='Capture immutable local usage receipts')
    discover = commands.add_parser('discover', help='List known local history roots without reading credentials')
    refresh = commands.add_parser('refresh', help='Collect, reconcile the durable ledger and export safe website data')
    refresh.add_argument('--until', type=date_argument)
    refresh.add_argument('--output', type=Path, default=Path('.local/runs'), help='PRIVATE capture base')
    refresh.add_argument('--ledger', type=Path, default=Path('.local/ledger/usage.sqlite3'))
    refresh.add_argument('--public-output', type=Path, default=Path('web/data.json'))
    refresh.add_argument('--timezone')
    refresh.add_argument('--from-run', type=Path, action='append', default=[], help='Use an existing verified full-history capture instead of a live scan; repeatable')
    ingest = commands.add_parser('ingest', help='Idempotently import verified full-history captures into the private ledger')
    ingest.add_argument('runs', type=Path, nargs='+')
    ingest.add_argument('--ledger', type=Path, default=Path('.local/ledger/usage.sqlite3'))
    export = commands.add_parser('export', help='Export allowlisted daily aggregates from the private ledger')
    export.add_argument('--ledger', type=Path, default=Path('.local/ledger/usage.sqlite3'))
    export.add_argument('--output', type=Path, default=Path('web/data.json'))
    export.add_argument('--timezone', default='Europe/Warsaw')
    serve = commands.add_parser('serve', help='Serve only public web assets on loopback')
    serve.add_argument('--port', type=int, default=4317)
    for command in (collect, discover, refresh):
        command.add_argument('--config', type=Path)
        command.add_argument('--platform', choices=('all', *PLATFORMS), default='all')
        command.add_argument('--codex-root', action='append', default=[], metavar='PATH')
        command.add_argument('--antigravity-root', action='append', default=[], metavar='PATH')
    collect.add_argument('--until', type=date_argument, help='Inclusive UTC cutoff; default: collection start')
    collect.add_argument('--since', type=date_argument, help='Inclusive start; undated records are excluded')
    collect.add_argument('--output', type=Path, default=Path('.local/runs'), help='PRIVATE output base; never serve it as a website')
    collect.add_argument('--timezone', help='IANA timezone for daily totals')
    collect.add_argument('--exclude-session-hash', action='append', default=[])
    collect.add_argument('--dry-run', action='store_true', help='Parse and check counters without saving capture files')
    verify = commands.add_parser('verify', help='Verify a capture manifest, identities, cutoff, and rollups')
    verify.add_argument('run', type=Path)
    summary = commands.add_parser('summarize', help='Print a verified capture summary')
    summary.add_argument('run', type=Path)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command in ('verify', 'summarize'):
            check = verify_run(args.run)
            if args.command == 'summarize':
                value = json.loads((args.run/'summary.json').read_text())
                value.pop('daily', None)
            else: value = check
            print(json.dumps(value, indent=2)); return 0
        if args.command == 'serve':
            from .server import serve
            if not 1 <= args.port <= 65535: raise ConfigError('Port must be between 1 and 65535')
            serve(Path(__file__).resolve().parent.parent/'web', args.port); return 0
        if args.command == 'ingest':
            from .ledger import ingest_runs
            print(json.dumps(ingest_runs(args.ledger, args.runs), indent=2)); return 0
        if args.command == 'export':
            from .ledger import ledger_snapshot
            from .public import export_snapshot
            result=export_snapshot(ledger_snapshot(args.ledger, timezone=args.timezone), args.output)
            print(json.dumps({'public_export':str(args.output),'events':result['events'],'total_tokens':result['totals']['total'],'coverage_status':result['quality']['coverage_status']},indent=2)); return 0
        config = load_config(args.config)
        if getattr(args, 'timezone', None):
            config['timezone'] = args.timezone
            from zoneinfo import ZoneInfo
            try: ZoneInfo(config['timezone'])
            except (KeyError, ValueError): raise ConfigError('Timezone must be an installed IANA timezone') from None
        platforms = PLATFORMS if args.platform == 'all' else (args.platform,)
        if args.command == 'refresh':
            from .pipeline import refresh
            progress=lambda message:print(message,file=sys.stderr,flush=True)
            result=refresh(config,args.output,args.ledger,args.public_output,platforms,
                {'codex':args.codex_root,'antigravity':args.antigravity_root},args.until,args.from_run,progress)
            print(json.dumps(result,indent=2))
            return 2 if result['summary']['coverage_status']=='partial' else 0
        roots = {}; adapters = {}
        for platform in platforms:
            module = importlib.import_module('tokenometer.collectors.'+platform)
            adapters[platform] = module
            explicit = getattr(args, platform+'_root') or config[platform+'_roots']
            roots[platform] = resolve_roots(explicit) if explicit else module.discover_roots(Path.home())
        if args.command == 'discover':
            print(json.dumps({'platforms': {p: [str(v) for v in paths] for p,paths in roots.items()},
                 'note': 'Local private paths only. Discovery is not proof of complete account coverage.'}, indent=2)); return 0
        cutoff = args.until or datetime.now(timezone.utc)
        if args.since and args.since > cutoff: raise ConfigError('--since must not be after --until')
        for value in args.exclude_session_hash:
            if len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ConfigError('Excluded sessions require lowercase full SHA-256 hashes')
        started = datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
        results = {}
        for platform in platforms:
            try:
                result = adapters[platform].collect(roots[platform], cutoff, cache=None)
                if not isinstance(result, CollectionResult): raise ValueError('Invalid adapter contract')
                results[platform] = result
            except Exception:
                # Raw exception text can expose source paths and record payloads.
                results[platform] = CollectionResult(stats={'errors':1}, warnings=['collector_failed'])
        events, reconciliation = canonicalize([e for r in results.values() for e in r.events], cutoff, args.since, args.exclude_session_hash)
        if args.dry_run:
            summary = summarize(events, config['timezone']); summary.pop('daily',None)
            print(json.dumps({'dry_run':True,'summary':summary,'reconciliation':reconciliation,
                 'diagnostics':{p:{'stats':r.stats,'warnings':r.warnings} for p,r in results.items()}}, indent=2))
            return 2 if any(r.stats.get('errors',0) for r in results.values()) else 0
        run = persist_run(args.output.expanduser().resolve(), events, results, cutoff, args.since,
                          config['timezone'], started, roots, reconciliation, args.exclude_session_hash)
        checked = verify_run(run)
        print(json.dumps({'run':str(run),**checked}, indent=2))
        return 0 if checked['status']=='complete' else 2
    except ConfigError as exc:
        print(str(exc), file=sys.stderr); return 1
    except (OSError, ValueError, KeyError, TypeError, ImportError, sqlite3.Error) as exc:
        print('Collection command failed. Check arguments, adapter installation, and private capture files; source payloads were not printed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
