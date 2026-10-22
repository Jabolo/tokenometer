from datetime import datetime, timezone
from dataclasses import replace
import unittest
from tokenometer.models import UsageEvent, digest
from tokenometer.compare import compare_events

CUT=datetime(2026,10,22,10,tzinfo=timezone.utc)

def event(key, **kw):
    values=dict(key='codex:'+digest(key),platform='codex',session_key=digest('s'),
                timestamp='2026-10-22T09:00:00Z',fresh_input=10,cache_read=20,cache_write=0,output=5,reasoning=2)
    values.update(kw)
    return UsageEvent(**values)

class CompareTests(unittest.TestCase):
    def test_unchanged_history_is_overlap_not_new_usage(self):
        old=[event('a'),event('b')]
        result=compare_events(old,old,CUT)
        self.assertEqual(result['delta']['total'],0)
        self.assertEqual(result['common_events'],2)
        self.assertEqual(result['naive_sum_duplicate_baseline_tokens'],70)
        self.assertEqual(result['before_events_retained_fraction'],1)

    def test_added_removed_and_updates_reconcile_even_when_net_zero(self):
        old=[event('a'),event('b')]
        new=[event('a',output=40),event('c',fresh_input=0,cache_read=0,output=0,reasoning=0)]
        result=compare_events(old,new,CUT)
        self.assertEqual(result['delta']['total'],0)
        self.assertEqual(result['changed_counter_events'],1)
        self.assertEqual(result['added']['events'],1)
        self.assertEqual(result['removed']['events'],1)
        self.assertEqual(result['common_event_counter_delta']['total'],35)

    def test_time_groups_keep_backfills_and_unknown_separate(self):
        new=[event('past'),event('future',timestamp='2026-10-22T11:00:00Z'),event('unknown',timestamp=None)]
        result=compare_events([],new,CUT)
        self.assertTrue(all(v['events']==1 for v in result['added']['by_time'].values()))
        self.assertIsNone(result['before_events_retained_fraction'])

    def test_metadata_change_does_not_become_token_usage(self):
        result=compare_events([event('a')],[event('a',timestamp=None,model='changed')],CUT)
        self.assertEqual(result['delta']['total'],0)
        self.assertEqual(result['changed_counter_events'],0)
        self.assertEqual(result['common_metadata_changes']['timestamp'],1)

    def test_duplicates_rejected(self):
        with self.assertRaises(ValueError):compare_events([event('a'),event('a')],[],CUT)

class ScopeTests(unittest.TestCase):
    def mocked_run(self, providers, since=None, exclusions=None):
        receipt={'cutoff':'2026-10-22T10:00:00Z','since':since,'project_filter':None,
            'parameters':{'exclude_session_hashes':exclusions or []},
            'implementation':{'source_sha256':{}},
            'providers':{p:{'stats':{},'warnings':[]} for p in providers}}
        return receipt,[],{p:[] for p in providers}

    def test_unselected_provider_is_not_reported_as_removed(self):
        from pathlib import Path
        from unittest.mock import patch
        from tokenometer.compare import compare_runs
        with patch('tokenometer.compare.load', side_effect=[self.mocked_run(['codex','antigravity']),self.mocked_run(['codex'])]):
            result=compare_runs(Path('before'),Path('after'))
        self.assertEqual(set(result['platforms']),{'codex'})
        self.assertEqual(result['platform_scope']['not_compared'],['antigravity'])
        self.assertFalse(result['platforms']['codex']['measurement_code_unchanged'])

    def test_disjoint_provider_scopes_rejected(self):
        from pathlib import Path
        from unittest.mock import patch
        from tokenometer.compare import compare_runs
        with patch('tokenometer.compare.load',side_effect=[self.mocked_run(['antigravity']),self.mocked_run(['codex'])]):
            with self.assertRaises(ValueError):compare_runs(Path('before'),Path('after'))

    def test_filter_change_is_explicitly_incomparable(self):
        from pathlib import Path
        from unittest.mock import patch
        from tokenometer.compare import compare_runs
        with patch('tokenometer.compare.load',side_effect=[self.mocked_run(['codex']),self.mocked_run(['codex'],since='2026-10-22T09:00:00Z')]):
            result=compare_runs(Path('before'),Path('after'))
        self.assertFalse(result['selection_parameters_unchanged'])
