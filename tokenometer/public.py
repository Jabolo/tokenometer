"""Allowlisted aggregate projection. Private event rows never enter website data."""
from __future__ import annotations
import json
import os
import tempfile
from datetime import datetime, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo
from .models import utc_timestamp

COUNTERS = ('fresh_input','cache_read','cache_write','output','total')
MAX_SAFE = 2**53 - 1


def count(value):
    if type(value) is not int or not 0 <= value <= MAX_SAFE:
        raise ValueError('Public counts require nonnegative safe integers')
    return value


def totals(value):
    result = {name:count(value[name]) for name in COUNTERS}
    if result['total'] != sum(result[name] for name in COUNTERS[:-1]):
        raise ValueError('Public counter categories do not reconcile')
    return result


def day(value):
    if value is None: return None
    if not isinstance(value,str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('Invalid public date')
    return value


def timestamp(value):
    if value is None: return None
    result=utc_timestamp(value)
    if result is None: raise ValueError('Invalid public timestamp')
    return result


def public_snapshot(snapshot):
    summary=snapshot['summary']; zone=summary['timezone']; ZoneInfo(zone)
    overall=totals(summary['totals']); dated=totals(summary['dated_totals']); undated=totals(summary['undated_totals'])
    if any(overall[k] != dated[k]+undated[k] for k in COUNTERS):
        raise ValueError('Dated and undated totals do not reconcile')
    providers={}
    for name,label in (('codex','Codex'),('antigravity','Antigravity')):
        metrics=summary['platforms'][name]; state=snapshot['providers'].get(name,{})
        execution=state.get('execution_status')
        coverage=state.get('coverage_status')
        providers[name]={'label':label, 'totals':totals(metrics), 'events':count(metrics['events']),
            'undated_events':count(metrics['undated_events']),
            'latest_cutoff':timestamp(state.get('latest_cutoff')),
            'latest_capture_completed_at':timestamp(state.get('latest_capture_completed_at')),
            'last_successful_cutoff':timestamp(state.get('last_successful_cutoff')),
            'execution_status':execution if execution in ('complete','partial','unknown') else 'unobserved',
            'coverage_status':coverage if coverage in ('qualified','partial','unknown') else 'unobserved',
            'source_errors':count(state.get('errors') or 0)}
    if any(sum(p['totals'][k] for p in providers.values()) != overall[k] for k in COUNTERS):
        raise ValueError('Provider totals differ from ledger')
    rows=[]; seen=set()
    for row in summary['daily']:
        platform=row['platform']
        if platform not in providers: raise ValueError('Unknown daily provider')
        key=(day(row['date']),platform)
        if key in seen: raise ValueError('Duplicate daily aggregate')
        seen.add(key)
        rows.append({'date':key[0],'platform':platform,'events':count(row['events']),**totals(row)})
    if any(sum(row[k] for row in rows) != dated[k] for k in COUNTERS):
        raise ValueError('Daily categories do not reconcile')
    if sum(row['events'] for row in rows)+sum(p['undated_events'] for p in providers.values()) != summary['events']:
        raise ValueError('Dated and undated event counts differ')
    dates={row['date'] for row in rows}
    if (min(dates,default=None),max(dates,default=None),len(dates)) != (summary['first_recorded_date'],summary['last_recorded_date'],summary['recorded_active_days']):
        raise ValueError('Recorded activity dates differ from daily rows')
    if count(summary['events']) != sum(p['events'] for p in providers.values()):
        raise ValueError('Provider event counts do not reconcile')
    quality=snapshot['quality']
    coverage='qualified' if quality.get('coverage_status')=='qualified' else 'partial'
    if any(provider['execution_status']!='complete' or provider['coverage_status']!='qualified'
           or provider['source_errors']>0 for provider in providers.values()):
        coverage='partial'
    if (count(quality['conflicted_events'])>0 or
            any(provider['undated_events']>0 for provider in providers.values()) or
            any(value>0 for value in undated.values())):
        coverage='partial'
    result = {'schema_version':1, 'artifact':'tokenometer.public.aggregate.v1', 'title':'AI mileage',
        'generated_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
        'timezone':zone, 'totals':overall,'events':count(summary['events']),
        'first_recorded_date':day(summary['first_recorded_date']),
        'last_recorded_date':day(summary['last_recorded_date']),
        'recorded_active_days':count(summary['recorded_active_days']),
        'providers':providers,'daily':sorted(rows,key=lambda r:(r['date'],r['platform'])),
        'quality':{'coverage_status':coverage,
            'historical_account_completeness':'unknown',
            'conflicted_events':count(quality['conflicted_events']),
            'undated_events':sum(p['undated_events'] for p in providers.values()),
            'undated_totals':undated,'dated_totals':dated,
            'source_errors':sum(p['source_errors'] for p in providers.values()),
            'note':'Recorded local history. Some history may be missing. Reused context is included; reasoning is part of output.'}}
    if 'agents' in snapshot:
        from .release_guard import validate_agent_summary
        fields = ('count', 'observed_at', 'cutoff', 'execution_status', 'conflicted_sessions')
        projected = {name: {key: snapshot['agents'][name][key] for key in fields}
                     for name in ('codex', 'antigravity')}
        result['agents'] = validate_agent_summary(projected, result['generated_at'])
    return result


def export_snapshot(snapshot, path:Path):
    value=public_snapshot(snapshot)
    path=Path(path)
    if path.is_symlink() or path.parent.is_symlink(): raise ValueError('Public output may not be a symlink')
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        if path.stat().st_size > 16*1024*1024:
            raise ValueError('Refusing to replace a file that is not a public aggregate')
        try: previous=json.loads(path.read_text())
        except (ValueError,UnicodeError):
            raise ValueError('Refusing to replace a file that is not a public aggregate') from None
        legacy_keys=set(value)-{'artifact'}
        if not isinstance(previous,dict) or not (previous.get('artifact')=='tokenometer.public.aggregate.v1' or (set(previous)==legacy_keys and previous.get('title')=='AI mileage' and previous.get('schema_version')==1)):
            raise ValueError('Refusing to replace a file that is not a public aggregate')
    fd,temporary=tempfile.mkstemp(prefix='.aggregate-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,ensure_ascii=False,sort_keys=True,indent=2)
            stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.chmod(temporary,0o644)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    return value
