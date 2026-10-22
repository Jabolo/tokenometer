from datetime import datetime,timezone
from pathlib import Path
import contextlib
import io
import json
import tempfile
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
