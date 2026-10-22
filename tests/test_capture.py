import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from tokenometer.models import UsageEvent, CollectionResult, digest
from tokenometer.capture import canonicalize, persist_run, verify_run
from tokenometer.aggregate import summarize
from tokenometer.config import load_config, ConfigError

CUT = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def event(key='a', timestamp='2026-10-21T23:30:00Z', **changes):
    values = dict(key='codex:'+digest(key), platform='codex', session_key=digest('session'), timestamp=timestamp,
                  fresh_input=10, cache_read=100, cache_write=2, output=8, reasoning=3, counter_receipt={
        'schema':'codex.response.v1', 'source_refs':[{'source':digest('/fixture'), 'kind':'jsonl', 'index':1, 'retry':0}],
        'counters':{'input_tokens':112,'cached_input_tokens':100,'cache_write_input_tokens':2,'output_tokens':8,'reasoning_output_tokens':3,'total_tokens':120}})
    values.update(changes)
    if 'counter_receipt' not in changes:
        values['counter_receipt']['counters'] = {
            'input_tokens': values['fresh_input'] + values['cache_read'] + values['cache_write'],
            'cached_input_tokens': values['cache_read'],
            'cache_write_input_tokens': values['cache_write'],
            'output_tokens': values['output'],
            'reasoning_output_tokens': values['reasoning'],
            'total_tokens': values['fresh_input'] + values['cache_read'] + values['cache_write'] + values['output'],
        }
    return UsageEvent(**values)


class CaptureTests(unittest.TestCase):
    def test_input_categories_and_reasoning_once(self):
        e = event(); self.assertEqual(e.total, 120)
        self.assertEqual(summarize([e])['totals']['total'],120)
        with self.assertRaises(ValueError): event(reasoning=9)

    def test_global_duplicate_and_conflict_exclusion(self):
        result, stats = canonicalize([event(),event(),event(key='b'),event(key='b',output=9)], CUT)
        self.assertEqual([e.key for e in result], ['codex:'+digest('a')])
        self.assertEqual(stats['duplicates_removed'],2)
        self.assertEqual(stats['conflicting_keys'],1)

    def test_optional_reasoning_enrichment_keeps_a_valid_receipt(self):
        from tokenometer.receipts import validate_receipt

        unknown, known_zero = event(reasoning=None), event(reasoning=0)
        for versions in ([unknown, known_zero], [known_zero, unknown]):
            accepted, stats = canonicalize(versions, CUT)
            self.assertEqual(len(accepted), 1)
            self.assertEqual(accepted[0].reasoning, 0)
            validate_receipt(accepted[0])
            self.assertEqual(stats['conflicting_keys'], 0)

    def test_reasoning_conflict_still_excludes_the_key(self):
        accepted, stats = canonicalize([event(reasoning=1), event(reasoning=2)], CUT)
        self.assertEqual(accepted, [])
        self.assertEqual(stats['conflicting_keys'], 1)

    def test_comparable_alias_superset_enrichment_is_accepted(self):
        old, new = digest('old-owner'), digest('new-owner')
        original = event(session_key=old, session_aliases=(old,))
        enriched = event(session_key=new, session_aliases=(old, new))
        for versions in ([original, enriched], [enriched, original]):
            accepted, stats = canonicalize(versions, CUT)
            self.assertEqual(len(accepted), 1)
            self.assertEqual(accepted[0].session_key, min(old, new))
            self.assertEqual(accepted[0].session_aliases, tuple(sorted((old, new))))
            self.assertEqual(stats['conflicting_keys'], 0)

    def test_noncomparable_alias_owners_still_conflict(self):
        shared, left, right = digest('shared-owner'), digest('left-owner'), digest('right-owner')
        first = event(session_key=shared, session_aliases=(shared, left))
        second = event(session_key=shared, session_aliases=(shared, right))
        accepted, stats = canonicalize([first, second], CUT)
        self.assertEqual(accepted, [])
        self.assertEqual(stats['conflicting_keys'], 1)

    def test_ambiguous_antigravity_receipt_stays_undated_during_deduplication(self):
        from tokenometer.receipts import validate_receipt

        first_time, second_time = '2026-10-20T10:00:00Z', '2026-10-21T10:00:00Z'
        observation = {
            'input': 10, 'cache_creation': 2, 'cache_read': 100,
            'output_total': 8, 'visible_output': 5, 'reasoning': 3,
        }
        observations = [observation, observation]

        def agy(timestamp, source_times):
            refs = [
                {'source': digest(f'/agy/{index}'), 'kind': 'steps', 'index': index, 'retry': 0}
                for index, _ in enumerate(source_times)
            ]
            receipt = {
                'schema': 'antigravity.protobuf.v1', 'source_refs': refs,
                'observations': observations[:len(source_times)],
                'source_timestamps': source_times,
                'timestamp_basis': 'merged_source_metadata' if len(source_times) > 1 else 'record',
            }
            return UsageEvent(
                'antigravity:' + digest('ambiguous-date'), 'antigravity', digest('agy-session'),
                timestamp, 10, 100, 2, 8, 3, counter_receipt=receipt,
            )

        ambiguous = agy(None, [first_time, second_time])
        dated = agy(first_time, [first_time])
        accepted, _ = canonicalize([dated, ambiguous], CUT)
        self.assertEqual(len(accepted), 1)
        self.assertIsNone(accepted[0].timestamp)
        validate_receipt(accepted[0])

    def test_cutoff_and_since_do_not_assign_unknown_dates(self):
        ev = [event(),event(key='future',timestamp='2026-10-23T00:00:00Z'),event(key='unknown',timestamp=None)]
        result, stat = canonicalize(ev,CUT,since=datetime(2026,10,20,tzinfo=timezone.utc))
        self.assertEqual(len(result),1)
        self.assertEqual(stat['future_excluded'],1)
        self.assertEqual(stat['before_since_excluded'],1)
        self.assertEqual(summarize(ev)['undated_totals']['total'],120)

    def test_timezone_and_daily_not_utc_string_prefix(self):
        result = summarize([event()], 'Europe/Warsaw')
        self.assertEqual(result['first_recorded_date'],'2026-10-22')
        self.assertEqual(result['daily'][0]['total'],120)

    def test_private_capture_and_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = persist_run(root,[event()],{'codex':CollectionResult([event()],{'errors':0})},
                              CUT,None,'Europe/Warsaw','2026-10-22T12:00:00Z',{'codex':[Path('/private/history')]},{})
            self.assertEqual(verify_run(run)['total_tokens'],120)
            self.assertEqual(run.stat().st_mode & 0o777,0o700)
            for f in run.iterdir(): self.assertEqual(f.stat().st_mode & 0o777,0o600)
            pointer = json.loads((root/'latest.json').read_text())['run']
            partial = persist_run(root,[],{'codex':CollectionResult(stats={'errors':1})},CUT,None,
                                  'Europe/Warsaw','2026-10-22T12:00:00Z',{'codex':[]},{})
            self.assertEqual(verify_run(partial)['status'],'partial')
            self.assertEqual(json.loads((root/'latest.json').read_text())['run'],pointer)
            self.assertNotEqual(run,partial)
            (run/'events.jsonl').write_text('')
            with self.assertRaises(ValueError):verify_run(run)

    def test_two_runs_are_snapshots_not_additive_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for _ in range(2):
                run=persist_run(root,[event()],{'codex':CollectionResult(stats={'errors':0})},CUT,None,
                                'Europe/Warsaw','2026-10-22T12:00:00Z',{'codex':[]},{})
            self.assertEqual(verify_run(run)['total_tokens'],120)
            self.assertEqual(len(list(root.glob('*/events.jsonl'))),2)

    def test_config_rejects_unsupported_or_invalid_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'config.json'
            p.write_text('{"upload_secret":"x"}')
            with self.assertRaises(ConfigError):load_config(p)
            p.write_text('{"codex_roots":"/wrong"}')
            with self.assertRaises(ConfigError):load_config(p)
            p.write_text('{"timezone":"Not/A/Timezone"}')
            with self.assertRaises(ConfigError):load_config(p)

    def test_explicitly_excluded_session(self):
        result,stats=canonicalize([event()],CUT,excluded_sessions=[digest('session')])
        self.assertEqual(result,[]);self.assertEqual(stats['sessions_excluded'],1)


if __name__=='__main__':unittest.main()

class ReviewRegressionTests(unittest.TestCase):
    def make_run(self, root, events=None, result=None):
        return persist_run(root, [event()] if events is None else events,
                           {'codex': result or CollectionResult(stats={'errors':0})}, CUT, None,
                           'Europe/Warsaw', '2026-10-22T12:00:00Z', {'codex':[]}, {})

    def rehash(self, run, filename):
        import hashlib
        path=run/'manifest.json'; manifest=json.loads(path.read_text())
        data=(run/filename).read_bytes()
        for item in manifest['files']:
            if item['file']==filename:
                item.update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        path.write_text(json.dumps(manifest))

    def test_invalid_evidence_cannot_advance_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); good=self.make_run(root)
            before=(root/'latest.json').read_bytes()
            with self.assertRaises(ValueError):self.make_run(root,[event(counter_receipt={})])
            self.assertEqual((root/'latest.json').read_bytes(), before)
            self.assertEqual(json.loads(before)['run'],good.name)

    def test_recomputed_manifest_cannot_hide_daily_row_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=self.make_run(Path(tmp))
            path=run/'daily.csv';path.write_text(path.read_text().replace('2026-10-22,codex','2026-10-20,antigravity'))
            self.rehash(run,'daily.csv')
            with self.assertRaises(ValueError):verify_run(run)

    def test_source_counter_mismatch_rejected_even_with_rehashed_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=self.make_run(Path(tmp));path=run/'events.jsonl';row=json.loads(path.read_text())
            row['counter_receipt']['counters']['cached_input_tokens']=99
            path.write_text(json.dumps(row)+'\n');self.rehash(run,'events.jsonl')
            with self.assertRaises(ValueError):verify_run(run)

    def test_warnings_and_errors_have_separate_status_and_unknown_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=self.make_run(Path(tmp),result=CollectionResult(stats={'errors':0},warnings=['INCOMPLETE_HISTORY']))
            checked=verify_run(run)
            self.assertEqual(checked['execution_status'],'complete')
            self.assertEqual(checked['coverage_status'],'partial')
            self.assertEqual(checked['historical_account_completeness'],'unknown')
            receipt=json.loads((run/'collection.json').read_text());receipt['coverage_status']='qualified'
            (run/'collection.json').write_text(json.dumps(receipt));self.rehash(run,'collection.json')
            with self.assertRaises(ValueError):verify_run(run)

    def test_raw_identity_rejected(self):
        with self.assertRaises(ValueError):event(key='irrelevant',session_key='raw-session')
        with self.assertRaises(ValueError):UsageEvent('codex:raw-id','codex',digest('s'),None,0,0,0,0)

    def test_duplicate_session_and_time_conflicts_are_order_independent(self):
        for other in (event(session_key=digest('other')),event(timestamp='2026-10-20T10:00:00Z')):
            for records in ([event(),other],[other,event()]):
                events,stats=canonicalize(records,CUT)
                self.assertEqual(events,[]);self.assertEqual(stats['conflicting_keys'],1)
        a,_=canonicalize([event(timestamp=None),event()],CUT)
        b,_=canonicalize([event(),event(timestamp=None)],CUT)
        self.assertEqual(a,b)

    def test_unsafe_existing_output_directory_refused_without_chmod(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'unsafe';root.mkdir(mode=0o755)
            with self.assertRaises(ValueError):self.make_run(root)
            self.assertEqual(root.stat().st_mode & 0o777,0o755)

    def test_receipt_summary_tamper_and_duplicate_manifest_entry_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=self.make_run(Path(tmp))
            receipt=json.loads((run/'collection.json').read_text());receipt['summary']['events']=2
            (run/'collection.json').write_text(json.dumps(receipt));self.rehash(run,'collection.json')
            with self.assertRaises(ValueError):verify_run(run)
        with tempfile.TemporaryDirectory() as tmp:
            run=self.make_run(Path(tmp));manifest=json.loads((run/'manifest.json').read_text())
            manifest['files'].append(manifest['files'][0]);(run/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):verify_run(run)
