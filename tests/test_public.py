import json
from pathlib import Path
import tempfile
import unittest
from tokenometer.models import UsageEvent,digest
from tokenometer.aggregate import summarize
from tokenometer.public import public_snapshot,export_snapshot


def fixture():
    event=UsageEvent('codex:'+digest('a'),'codex',digest('s'),'2026-10-21T23:30:00Z',10,100,2,8,3,model='PRIVATE_MODEL')
    summary=summarize([event]);summary['private']='PRIVATE_CANARY'
    return {'summary':summary,'quality':{'coverage_status':'partial','conflicted_events':0,'private':'PRIVATE_CANARY'},
        'providers':{'codex':{'latest_cutoff':'2026-10-22T01:00:00Z','latest_capture_completed_at':'2026-10-22T01:01:00Z',
             'last_successful_cutoff':'2026-10-22T01:00:00Z','execution_status':'complete','coverage_status':'partial','errors':0,
             'account':'PRIVATE_CANARY','roots':['/Users/PRIVATE_CANARY'], 'raw_warning':'PRIVATE_CANARY'}}}

class PublicTests(unittest.TestCase):
    def test_agent_export_projects_only_reviewed_aggregate_fields(self):
        value=fixture()
        unknown={'count':None,'observed_at':None,'cutoff':None,'execution_status':'unobserved','conflicted_sessions':0}
        value['agents']={'codex':{**unknown,'count':4,'observed_at':'2026-10-22T12:00:00.000001Z',
                                 'cutoff':'2026-10-22T12:00:00Z','execution_status':'complete',
                                 'source_refs':['PRIVATE_CANARY'],'session_id_sha256':'PRIVATE_CANARY'},
                         'antigravity':dict(unknown)}
        result=public_snapshot(value)
        self.assertEqual(result['agents']['codex']['count'],4)
        self.assertNotIn('PRIVATE_CANARY',json.dumps(result))
        from tokenometer.release_guard import validate_public_payload
        self.assertEqual(validate_public_payload(result),result)

    def test_allowlist_strips_private_event_source_and_account_data(self):
        result=public_snapshot(fixture());text=json.dumps(result)
        for secret in ('PRIVATE_CANARY','PRIVATE_MODEL','session_key','source_refs','counter_receipt','/Users/'):
            self.assertNotIn(secret,text)
        self.assertEqual(result['totals']['total'],120)
        self.assertEqual(result['recorded_active_days'],1)
        self.assertEqual(result['providers']['antigravity']['execution_status'],'unobserved')

    def test_partial_source_failure_preserves_coverage_and_freshness(self):
        value=fixture();value['quality']['coverage_status']='qualified'
        value['providers']['codex'].update(errors=2,execution_status='partial',last_successful_cutoff=None)
        result=public_snapshot(value)
        self.assertEqual(result['quality']['source_errors'],2)
        self.assertEqual(result['providers']['codex']['execution_status'],'partial')
        self.assertIsNone(result['providers']['codex']['last_successful_cutoff'])
        self.assertEqual(result['quality']['coverage_status'],'partial')

    def test_bad_rollup_cannot_replace_last_public_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'data.json';export_snapshot(fixture(),path);before=path.read_bytes()
            value=fixture();value['summary']['daily'][0]['total']+=1
            with self.assertRaises(ValueError):export_snapshot(value,path)
            self.assertEqual(path.read_bytes(),before)
            self.assertFalse(list(path.parent.glob('.aggregate-*')))

    def test_undated_is_not_assigned_to_daily_bars(self):
        value=fixture();event=UsageEvent('antigravity:'+digest('b'),'antigravity',digest('s'),None,1,0,0,0,0)
        old=UsageEvent('codex:'+digest('a'),'codex',digest('s'),'2026-10-21T23:30:00Z',10,100,2,8,3)
        value['quality']['coverage_status']='qualified'
        value['summary']=summarize([old,event]);result=public_snapshot(value)
        self.assertEqual(result['totals']['total'],121)
        self.assertEqual(sum(r['total'] for r in result['daily']),120)
        self.assertEqual(result['quality']['undated_totals']['total'],1)
        self.assertEqual(result['quality']['coverage_status'],'partial')

    def test_symlink_export_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'private';target.write_text('private')
            link=Path(tmp)/'data.json';link.symlink_to(target)
            with self.assertRaises(ValueError):export_snapshot(fixture(),link)
            self.assertEqual(target.read_text(),'private')

    def test_public_destination_cannot_overwrite_private_or_unrelated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'data.json';path.write_text('{"private":"CANARY"}');before=path.read_bytes()
            with self.assertRaises(ValueError):export_snapshot(fixture(),path)
            self.assertEqual(path.read_bytes(),before)

    def test_observed_unknown_diagnostics_do_not_become_unobserved(self):
        value=fixture();value['quality']['coverage_status']='qualified'
        value['providers']['codex'].update(execution_status='unknown',coverage_status='unknown')
        result=public_snapshot(value)
        self.assertEqual(result['providers']['codex']['execution_status'],'unknown')
        self.assertIsNotNone(result['providers']['codex']['latest_cutoff'])
        self.assertEqual(result['quality']['coverage_status'],'partial')

    def test_conflicts_force_partial_overall_coverage(self):
        value=fixture();value['quality'].update(coverage_status='qualified',conflicted_events=1)
        result=public_snapshot(value)
        self.assertEqual(result['quality']['coverage_status'],'partial')
