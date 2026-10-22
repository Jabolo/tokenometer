"""Compare two verified, nonadditive snapshots without exposing event/account IDs.

Usage: python3 -m tokenometer.compare BEFORE AFTER [--output PRIVATE_JSON]
"""
from __future__ import annotations
import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from .aggregate import sum_events
from .capture import verify_run, write_json, private_base
from .models import UsageEvent

COUNTERS = ('fresh_input', 'cache_read', 'cache_write', 'output', 'total')
SIGNATURE = ('fresh_input', 'cache_read', 'cache_write', 'output', 'reasoning')


def compare_events(before, after, baseline_cutoff):
    """Reconcile identity sets, counters and time changes; never infer account billing."""
    before = list(before); after = list(after)
    old = {e.key:e for e in before}; new = {e.key:e for e in after}
    if len(old) != len(before) or len(new) != len(after):
        raise ValueError('Comparison requires unique event identities')
    common = old.keys() & new.keys()
    added = [new[k] for k in sorted(new.keys() - old.keys())]
    removed = [old[k] for k in sorted(old.keys() - new.keys())]
    old_totals = sum_events(before); new_totals = sum_events(after)
    changes = Counter()
    changed = 0
    for key in common:
        left, right = old[key], new[key]
        if any(getattr(left, field) != getattr(right, field) for field in SIGNATURE):
            changed += 1
        for field in ('timestamp', 'session_key', 'session_aliases', 'model'):
            changes[field] += getattr(left, field) != getattr(right, field)
    time_groups = {'at_or_before_baseline_cutoff':[], 'after_baseline_cutoff':[], 'undated':[]}
    for event in added:
        if event.timestamp is None: label = 'undated'
        elif datetime.fromisoformat(event.timestamp.replace('Z', '+00:00')) <= baseline_cutoff:
            label = 'at_or_before_baseline_cutoff'
        else: label = 'after_baseline_cutoff'
        time_groups[label].append(event)
    common_delta = {field:sum(getattr(new[k],field) - getattr(old[k],field) for k in common) for field in COUNTERS}
    added_totals = sum_events(added); removed_totals = sum_events(removed)
    delta = {field:new_totals[field] - old_totals[field] for field in COUNTERS}
    assert all(delta[f] == added_totals[f] - removed_totals[f] + common_delta[f] for f in COUNTERS)
    common_old_tokens = sum(old[k].total for k in common)
    return {
        'before': {'events':len(old), 'totals':old_totals},
        'after': {'events':len(new), 'totals':new_totals},
        'delta':delta, 'common_events':len(common), 'unchanged_counter_events':len(common)-changed,
        'changed_counter_events':changed, 'common_event_counter_delta':common_delta,
        'common_metadata_changes':dict(changes),
        'before_events_retained_fraction':len(common)/len(old) if old else None,
        'before_tokens_present_under_same_id':common_old_tokens,
        'added':{'events':len(added), 'totals':added_totals,
            'by_time':{label:{'events':len(events),'totals':sum_events(events)} for label,events in time_groups.items()}},
        'removed':{'events':len(removed),'totals':removed_totals},
        'union_unique_events':len(old.keys() | new.keys()),
        'naive_sum_duplicate_baseline_tokens':common_old_tokens,
        'reconciliation_passed':True,
    }


def load(run):
    verify_run(run)
    receipt = json.loads((run/'collection.json').read_text())
    events = [UsageEvent(**json.loads(line)) for line in (run/'events.jsonl').open() if line.strip()]
    roots = json.loads((run/'sources.private.json').read_text())
    return receipt, events, roots


def compare_runs(before_path, after_path):
    before, old, old_roots = load(before_path)
    after, new, new_roots = load(after_path)
    cutoff = datetime.fromisoformat(before['cutoff'].replace('Z','+00:00'))
    if datetime.fromisoformat(after['cutoff'].replace('Z','+00:00')) < cutoff:
        raise ValueError('After cutoff must not precede before cutoff')
    output = {'schema_version':1,'before_run':before_path.name,'after_run':after_path.name,
        'before_cutoff':before['cutoff'],'after_cutoff':after['cutoff'],
        'account_switch': 'User-reported; authentication credentials were not inspected.',
        'grain':'Deduplicated local usage event; not account billing.', 'platforms':{},
        'caveats':[
            'Snapshots are not additive. Common event IDs represent overlapping recorded history.',
            'A post-cutoff event is not proof of usage by the newly selected account.',
            'Newly observed pre-cutoff events can be backfills or changes in source visibility.',
            'Partial source reads and parser coverage limits can affect the comparison.',
            'Active account labels cannot retroactively assign ownership to local history.'
        ]}
    old_platforms=set(before['providers']); new_platforms=set(after['providers'])
    output['platform_scope']={'before':sorted(old_platforms),'after':sorted(new_platforms),
        'compared':sorted(old_platforms & new_platforms),
        'not_compared':sorted(old_platforms ^ new_platforms)}
    if not old_platforms & new_platforms:
        raise ValueError('Captures have no commonly selected providers')
    output['selection_parameters_unchanged']=(before['since']==after['since'] and
        before['parameters']['exclude_session_hashes']==after['parameters']['exclude_session_hashes'] and
        before['project_filter']==after['project_filter'])
    if not output['selection_parameters_unchanged']:
        output['caveats'].append('Time-start, session exclusions or project selection differ; deltas are not comparable activity.')
    for platform in sorted(old_platforms & new_platforms):
        metrics = compare_events([e for e in old if e.platform==platform], [e for e in new if e.platform==platform],cutoff)
        before_refs = {r['source'] for e in old if e.platform==platform for r in e.counter_receipt['source_refs']}
        after_refs = {r['source'] for e in new if e.platform==platform for r in e.counter_receipt['source_refs']}
        source_hashes = ('collectors/'+platform+'.py','models.py','aggregate.py','capture.py','receipts.py')
        old_hashes=before['implementation']['source_sha256'];new_hashes=after['implementation']['source_sha256']
        metrics.update({
            'roots_unchanged':sorted(old_roots.get(platform,[]))==sorted(new_roots.get(platform,[])),
            'counter_sources':{'before':len(before_refs),'after':len(after_refs),
                'common':len(before_refs & after_refs),'added':len(after_refs-before_refs),'removed':len(before_refs-after_refs)},
            'measurement_code_unchanged':all(k in old_hashes and k in new_hashes and old_hashes[k]==new_hashes[k] for k in source_hashes),
            'before_diagnostics':before['providers'].get(platform),
            'after_diagnostics':after['providers'].get(platform),
        })
        output['platforms'][platform]=metrics
    return output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before',type=Path);parser.add_argument('after',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    result=compare_runs(args.before,args.after)
    if args.output:
        private_base(args.output.parent)
        if args.output.exists(): raise ValueError('Choose a new comparison output path')
        write_json(args.output,result)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
