"""Derived totals from private counter receipts. No tokenizer estimates."""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from .models import UsageEvent


COUNTERS = ('fresh_input', 'cache_read', 'cache_write', 'output', 'total')


def sum_events(events):
    totals = {key: 0 for key in COUNTERS}
    for e in events:
        for key in COUNTERS:
            totals[key] += getattr(e, key)
    return totals


def summarize(events: list[UsageEvent], timezone: str = 'Europe/Warsaw') -> dict:
    zone = ZoneInfo(timezone)
    days = defaultdict(list)
    platforms = {}
    dated = []; undated = []
    for e in events:
        if e.timestamp:
            day = datetime.fromisoformat(e.timestamp.replace('Z', '+00:00')).astimezone(zone).date().isoformat()
            days[(day, e.platform)].append(e)
            dated.append(e)
        else:
            undated.append(e)
    for platform in ('codex', 'antigravity'):
        es = [e for e in events if e.platform == platform]
        platforms[platform] = {**sum_events(es), 'events': len(es),
            'sessions': len({e.session_key for e in es}),
            'undated_events': sum(e.timestamp is None for e in es)}
    daily = [{'date': day, 'platform': platform, **sum_events(es), 'events': len(es)}
             for (day, platform), es in sorted(days.items())]
    first = min((r['date'] for r in daily), default=None)
    last = max((r['date'] for r in daily), default=None)
    totals = sum_events(events)
    dated_totals = sum_events(dated); undated_totals = sum_events(undated)
    assert all(totals[k] == dated_totals[k] + undated_totals[k] for k in COUNTERS)
    assert sum(r['total'] for r in daily) == dated_totals['total']
    return {'timezone': timezone, 'totals': totals, 'events': len(events), 'platforms': platforms,
            'dated_totals': dated_totals, 'undated_totals': undated_totals,
            'first_recorded_date': first, 'last_recorded_date': last,
            'recorded_active_days': len({r['date'] for r in daily}), 'daily': daily}
