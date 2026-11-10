from datetime import datetime,timezone
from pathlib import Path
import contextlib
import io
import json
import tempfile
import sqlite3
import unittest
from unittest.mock import patch
from tokenometer.models import UsageEvent,CollectionResult,digest
from tokenometer.capture import persist_run
from tokenometer.pipeline import refresh
from tokenometer.ledger import ledger_snapshot
from tokenometer.cli import main

CUT=datetime(2026,10,22,12,tzinfo=timezone.utc)
CONFIG={'timezone':'Europe/Warsaw','codex_roots':[],'antigravity_roots':[]}

def event(name='c',platform='codex'):
    if platform=='codex':
        receipt={'schema':'codex.response.v1','source_refs':[{'source':digest('source'),'kind':'jsonl','index':0,'retry':0}],
            'counters':{'input_tokens':112,'cached_input_tokens':100,'cache_write_input_tokens':2,'output_tokens':8,'reasoning_output_tokens':3,'total_tokens':120}}
        values=(10,100,2,8,3)
    else:
        receipt={'schema':'antigravity.protobuf.v1','source_refs':[{'source':digest('source'),'kind':'steps','index':0,'retry':0}],
            'observations':[{'input':1,'cache_creation':0,'cache_read':0,'output_total':0,'visible_output':0,'reasoning':0}],
            'source_timestamps':['2026-10-21T10:00:00Z']}
        values=(1,0,0,0,0)
    return UsageEvent(platform+':'+digest(name),platform,digest('s'),'2026-10-21T10:00:00Z',*values,counter_receipt=receipt)

def capture(base,events,platforms=('codex','antigravity'),error=False):
    results={p:CollectionResult(stats={'errors':int(error and p=='antigravity')},warnings=['SOURCE_FAILURE'] if error and p=='antigravity' else []) for p in platforms}
    return persist_run(base,events,results,CUT,None,'Europe/Warsaw','2026-10-22T12:00:00Z',{p:[] for p in platforms},{})

class PipelineTests(unittest.TestCase):

    def test_interrupted_empty_initialization_remains_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); ledger=root/'ledger/usage.sqlite3'; public=root/'web/data.json'; base=root/'runs'
            ledger.parent.mkdir(mode=0o700);ledger.touch(mode=0o600)
            run=capture(base,[event()])
            result=refresh(CONFIG,base,ledger,public,from_runs=[run])
            self.assertEqual(result['summary']['total_tokens'],120)
            self.assertEqual(json.loads(public.read_text())['events'],1)


    def test_missing_stored_state_blocks_provider_scan_and_preserves_public_file(self):
        from tokenometer.ledger import LedgerIntegrityError
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); ledger=root/'ledger/usage.sqlite3'; public=root/'web/data.json'; base=root/'runs'
            first=capture(base,[event()]); refresh(CONFIG,base,ledger,public,from_runs=[first])
            old_export=public.read_bytes()
            with sqlite3.connect(ledger) as connection:
                connection.execute('DELETE FROM event_state')
            with patch('tokenometer.pipeline.collect_capture') as collect:
                with self.assertRaises(LedgerIntegrityError): refresh(CONFIG,base,ledger,public)
            collect.assert_not_called(); self.assertEqual(public.read_bytes(),old_export)
            output=io.StringIO(); errors=io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code=main(['refresh','--ledger',str(ledger),'--output',str(base),'--public-output',str(public)])
            self.assertEqual(code,3); self.assertIn('audit-ledger',errors.getvalue())
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(main(['audit-ledger','--ledger',str(ledger)]),1)
            self.assertNotIn(str(root), output.getvalue()+errors.getvalue())

    def test_reported_day_is_retracted_if_late_local_event_arrives(self):
        from dataclasses import replace
        from tokenometer.reported_usage import Report
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);base=root/'runs';ledger=root/'ledger/usage.sqlite3';public=root/'web/data.json'
            report=Report('a'*64, (('2026-10-20', 12345),))
            first=capture(base,[event()],platforms=('codex',))
            with patch('tokenometer.reported_usage.load_reports', return_value=[report]):
                refresh(CONFIG,base,ledger,public,from_runs=[first])
                before=json.loads(public.read_text())
                self.assertEqual(before['timezone'],'UTC')
                self.assertEqual(before['supplemental_daily'],[{'date':'2026-10-20','total':12345}])
                self.assertEqual(before['totals']['total'],120)  # categories remain local only
                self.assertNotIn('a'*64,public.read_text())
                late=replace(event('late'),timestamp='2026-10-20T23:59:59Z')
                second=capture(base,[late],platforms=('codex',))
                refresh(CONFIG,base,ledger,public,from_runs=[second])
            after=json.loads(public.read_text())
            self.assertNotIn('supplemental_daily',after)
            self.assertEqual(after['totals']['total'],240)

    def test_daily_live_refresh_collects_agents_and_replays_without_counting_again(self):
        envelope = {'version': 1, 'cutoff': CUT.isoformat().replace('+00:00','Z'),
                    'observed_at': '2026-10-22T12:01:00Z', 'providers': {'codex': {
            'execution_status': 'complete', 'diagnostics': {}, 'records': [{
                'session_id_sha256': digest('child-without-token-usage'),
                'parent_id_sha256': digest('parent'), 'created_at': '2026-10-21T12:00:00Z',
                'source_refs': [{'source': digest('metadata-file'), 'kind': 'jsonl', 'index': 1}]}]}}}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);base=root/'runs';ledger=root/'ledger/usage.sqlite3';public=root/'web/data.json'
            class Good:
                def collect(self,*args,**kwargs):return CollectionResult(events=[event()],stats={'errors':0})
            with patch('tokenometer.pipeline.source_roots', return_value=({'codex': []}, {'codex':Good()})), \
                    patch('tokenometer.agents.collect', return_value=envelope) as collected:
                result=refresh(CONFIG,base,ledger,public,platforms=('codex',),cutoff=CUT)
            collected.assert_called_once_with({'codex': []}, CUT)
            value=json.loads(public.read_text())
            self.assertEqual(value['agents']['codex']['count'], 1)
            self.assertIsNone(value['agents']['antigravity']['count'])
            self.assertEqual(value['totals']['total'], 120)
            run=Path(result['runs'][0]);self.assertTrue((run/'agents.json').is_file())
            again=refresh(CONFIG,base,ledger,public,from_runs=[run])
            self.assertEqual(json.loads(public.read_text())['agents']['codex']['count'],1)
            self.assertEqual(again['ingestion']['new_event_versions'],0)
            for private in ['session_id_sha256','source_refs','metadata-file',str(root)]:
                self.assertNotIn(private,public.read_text())

    def test_agent_capture_corruption_rolls_back_and_preserves_public_export(self):
        envelope = {'version': 1, 'cutoff': CUT.isoformat().replace('+00:00','Z'),
                    'observed_at': '2026-10-22T12:01:00Z', 'providers': {'codex': {
                        'execution_status': 'complete', 'diagnostics': {}, 'records': []}}}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);base=root/'runs';ledger=root/'ledger/usage.sqlite3';public=root/'web/data.json'
            first=capture(base,[event()],platforms=('codex',));refresh(CONFIG,base,ledger,public,from_runs=[first])
            before=public.read_bytes()
            results={'codex':CollectionResult(stats={'errors':0})}
            run=persist_run(base,[event('other')],results,CUT,None,'Europe/Warsaw',
                            '2026-10-22T12:00:00Z',{'codex':[]},{},agent_sessions=envelope)
            (run/'agents.json').write_text('{}')
            with self.assertRaises(ValueError):refresh(CONFIG,base,ledger,public,from_runs=[run])
            self.assertEqual(public.read_bytes(),before)
            self.assertEqual(ledger_snapshot(ledger)['summary']['events'],1)

    def test_replay_restart_retention_per_provider_quality_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);base=root/'runs';ledger=root/'ledger/usage.sqlite3';public=root/'web/data.json'
            first=capture(base,[event(),event('a','antigravity')],error=True)
            result=refresh(CONFIG,base,ledger,public,from_runs=[first])
            self.assertEqual(result['summary']['total_tokens'],121)
            original=json.loads(public.read_text())
            self.assertEqual(original['providers']['codex']['execution_status'],'complete')
            self.assertEqual(original['providers']['antigravity']['execution_status'],'partial')
            self.assertIsNone(original['providers']['antigravity']['last_successful_cutoff'])
            second=capture(base,[event('new')],platforms=('codex',))
            result=refresh(CONFIG,base,ledger,public,from_runs=[second])
            self.assertEqual(result['summary']['total_tokens'],241)
            self.assertEqual(result['summary']['events'],3)
            again=refresh(CONFIG,base,ledger,public,from_runs=[second])
            self.assertEqual(again['ingestion']['new_event_versions'],0)
            self.assertEqual(again['summary'],result['summary'])
            final=json.loads(public.read_text())
            self.assertEqual(final['providers']['antigravity']['latest_cutoff'],original['providers']['antigravity']['latest_cutoff'])
            self.assertEqual(final['providers']['antigravity']['totals']['total'],1)
            self.assertEqual(final['totals']['total'],sum(row['total'] for row in final['daily']))
            self.assertNotIn('session_key',public.read_text());self.assertNotIn(str(root),public.read_text())

    def test_invalid_capture_does_not_replace_public_data_or_ingest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);base=root/'runs';ledger=root/'ledger/usage.sqlite3';public=root/'web/data.json'
            first=capture(base,[event()]);refresh(CONFIG,base,ledger,public,from_runs=[first])
            before=public.read_bytes();count=ledger_snapshot(ledger)['summary']['events']
            bad=capture(base,[event('bad')]);(bad/'events.jsonl').write_text('corrupt')
            with self.assertRaises(ValueError):refresh(CONFIG,base,ledger,public,from_runs=[bad])
            self.assertEqual(public.read_bytes(),before);self.assertEqual(ledger_snapshot(ledger)['summary']['events'],count)

    def test_live_adapter_failure_preserves_history_and_reports_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);base=root/'runs';ledger=root/'ledger/usage.sqlite3';public=root/'web/data.json'
            first=capture(base,[event()]);refresh(CONFIG,base,ledger,public,from_runs=[first])
            class Failed:
                def collect(self,*args,**kwargs):raise RuntimeError('PRIVATE_SOURCE_PAYLOAD')
            with patch('tokenometer.pipeline.source_roots',return_value=({'codex':[]},{'codex':Failed()})):
                result=refresh(CONFIG,base,ledger,public,platforms=('codex',),cutoff=CUT)
            self.assertEqual(result['capture']['execution_status'],'partial')
            self.assertEqual(result['summary']['total_tokens'],120)
            self.assertNotIn('PRIVATE_SOURCE_PAYLOAD',public.read_text())
            self.assertEqual(json.loads(public.read_text())['providers']['codex']['execution_status'],'partial')

    def test_cli_refresh_replay_emits_reviewable_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=capture(root/'runs',[event()]);output=io.StringIO()
            with contextlib.redirect_stdout(output),contextlib.redirect_stderr(io.StringIO()):
                status=main(['refresh','--from-run',str(run),'--ledger',str(root/'ledger/usage.sqlite3'),'--public-output',str(root/'web/data.json')])
            result=json.loads(output.getvalue())
            self.assertIn(status,(0,2));self.assertTrue(result['website_data_updated'])
            self.assertEqual(result['summary']['total_tokens'],120)
