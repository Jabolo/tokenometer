import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.test_capture import CUT, event
from tokenometer.capture import persist_run, verify_run
from tokenometer.models import CollectionResult
from tokenometer.storage import open_events, repack_run
from tokenometer.ledger import ingest_runs, ledger_snapshot
from tokenometer.compare import compare_runs


def capture(root):
    return persist_run(root, [event()], {'codex':CollectionResult(stats={'errors':0})},
                       CUT, None, 'Europe/Warsaw', '2026-10-22T12:00:00Z', {'codex':[]}, {})


class StorageTests(unittest.TestCase):
    def test_new_capture_is_compressed_and_keeps_logical_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = capture(Path(tmp)/'runs')
            self.assertFalse((run/'events.jsonl').exists())
            self.assertTrue((run/'events.jsonl.gz').exists())
            self.assertEqual(verify_run(run)['total_tokens'], 120)
            manifest = json.loads((run/'manifest.json').read_text())
            entry = next(r for r in manifest['files'] if r['file']=='events.jsonl')
            raw = gzip.decompress((run/'events.jsonl.gz').read_bytes())
            self.assertEqual(entry['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(entry['bytes'], len(raw))
            self.assertEqual((run/'events.jsonl.gz').stat().st_mode & 0o777, 0o600)

    def test_round_trip_preserves_all_bytes_run_identity_replay_and_daily_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp); run=capture(base/'runs'); ledger=base/'ledger'/'usage.sqlite3'
            original_manifest=(run/'manifest.json').read_bytes()
            original_raw=gzip.decompress((run/'events.jsonl.gz').read_bytes())
            first=ingest_runs(ledger,[run]); before=ledger_snapshot(ledger)
            repack_run(run,restore=True)
            self.assertEqual((run/'events.jsonl').read_bytes(),original_raw)
            self.assertFalse((run/'events.jsonl.gz').exists())
            self.assertEqual(verify_run(run)['total_tokens'],120)
            packed=repack_run(run)
            self.assertTrue(packed['changed'])
            self.assertEqual(gzip.decompress((run/'events.jsonl.gz').read_bytes()),original_raw)
            self.assertEqual((run/'manifest.json').read_bytes(),original_manifest)
            second=ingest_runs(ledger,[run]); after=ledger_snapshot(ledger)
            self.assertEqual(second['new_runs'],0)
            self.assertEqual(second['new_event_versions'],0)
            self.assertEqual(before['summary'],after['summary'])
            self.assertFalse(repack_run(run)['changed'])

    def test_corruption_is_detected_and_plain_source_is_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=capture(Path(tmp)/'runs'); repack_run(run,restore=True)
            (run/'events.jsonl').write_text('bad')
            with self.assertRaises(ValueError):repack_run(run)
            self.assertEqual((run/'events.jsonl').read_text(),'bad')
            self.assertFalse((run/'events.jsonl.gz').exists())

    def test_interrupted_conversion_preserves_original_and_can_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=capture(Path(tmp)/'runs');repack_run(run,restore=True)
            original=(run/'events.jsonl').read_bytes()
            with patch('tokenometer.storage.os.replace', side_effect=OSError('fixture')):
                with self.assertRaises(OSError):repack_run(run)
            self.assertEqual((run/'events.jsonl').read_bytes(),original)
            self.assertEqual(verify_run(run)['total_tokens'],120)
            repack_run(run)
            self.assertEqual(verify_run(run)['total_tokens'],120)

    def test_corrupt_and_symlinked_compressed_streams_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=capture(root/'runs');p=run/'events.jsonl.gz'
            raw=p.read_bytes();p.write_bytes(raw[:-8])
            with self.assertRaises((OSError,ValueError,EOFError)):verify_run(run)
            p.unlink();outside=root/'outside';outside.write_bytes(raw);p.symlink_to(outside)
            with self.assertRaises(ValueError):verify_run(run)

    def test_compare_supports_plain_and_gzip_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=capture(root/'runs');new=capture(root/'runs')
            repack_run(old,restore=True)
            result=compare_runs(old,new)
            self.assertTrue(result['platforms']['codex']['reconciliation_passed'])

    def test_invalid_deflate_is_a_bounded_cli_error(self):
        from tokenometer.cli import main
        from contextlib import redirect_stderr
        import io
        with tempfile.TemporaryDirectory() as tmp:
            run=capture(Path(tmp)/'runs')
            (run/'events.jsonl.gz').write_bytes(bytes.fromhex('1f8b0800000000000003') + b'\x07' + b'\x00'*8)
            with self.assertRaisesRegex(ValueError,'Compressed capture is corrupt'):
                verify_run(run)
            error=io.StringIO()
            with redirect_stderr(error):
                self.assertEqual(main(['verify',str(run)]),1)
            self.assertNotIn('Traceback',error.getvalue())

class RecoveryTests(unittest.TestCase):
    def test_retry_selects_completed_compatible_capture_without_reading_histories(self):
        from tokenometer import automation
        from tokenometer.pipeline import source_roots
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);runs=root/'runs'
            roots={'codex':[Path('/fixture/codex')],'antigravity':[Path('/fixture/agy')]}
            run=persist_run(runs,[event()],{p:CollectionResult(stats={'errors':0}) for p in roots},
                            CUT,None,'Europe/Warsaw','2026-10-22T11:59:00Z',roots,{})
            # Preserve logical manifest integrity while using deterministic completion time.
            meta=json.loads((run/'collection.json').read_text());meta['completed_at']='2026-10-22T12:05:00Z'
            (run/'collection.json').write_text(json.dumps(meta))
            manifest=json.loads((run/'manifest.json').read_text())
            for row in manifest['files']:
                if row['file']=='collection.json':
                    b=(run/'collection.json').read_bytes();row.update(bytes=len(b),sha256=hashlib.sha256(b).hexdigest())
            (run/'manifest.json').write_text(json.dumps(manifest))
            config={'runs_dir':runs,'timezone':'Europe/Warsaw','source_config':None,
                    '_recovery_after':'2026-10-22T11:59:00Z','_recovery_before':'2026-10-22T12:10:00Z'}
            with patch('tokenometer.pipeline.source_roots',return_value=(roots,{})):
                self.assertEqual(automation._recovery_capture(config),run)
                config['_recovery_after']='2026-10-22T12:06:00Z'
                self.assertIsNone(automation._recovery_capture(config))
                config['_recovery_after']='2026-10-22T11:59:00Z'
                with patch('tokenometer.pipeline.source_roots',return_value=({'codex':[],'antigravity':[]},{})):
                    self.assertIsNone(automation._recovery_capture(config))

    def test_retries_keep_the_original_capture_window(self):
        from tokenometer import automation
        from tests.test_automation import AutomationTests
        with tempfile.TemporaryDirectory() as tmp:
            path,_=AutomationTests()._private_config(Path(tmp))
            seen=[]
            def fail(config,**kwargs):
                seen.append(config['_recovery_after'])
                raise automation.AutomationError('git_push_failed')
            with patch.object(automation,'_perform_attempt',side_effect=fail):
                for minute in (20,40,60):
                    now=CUT.replace(day=23,hour=7+minute//60,minute=minute%60)
                    with self.assertRaises(automation.AutomationError):
                        automation.run_automation(path,now=now)
            self.assertEqual(seen,[None,'2026-10-23T07:20:00Z','2026-10-23T07:20:00Z'])

    def test_failed_replay_clears_recovery_window_but_keeps_attempt_limit(self):
        from tokenometer import automation
        from tests.test_automation import AutomationTests
        with tempfile.TemporaryDirectory() as tmp:
            path,_=AutomationTests()._private_config(Path(tmp));seen=[]
            def fail(config,**kwargs):
                seen.append(config['_recovery_after'])
                raise automation.AutomationError('capture_replay_failed' if len(seen)==2 else 'git_push_failed')
            with patch.object(automation,'_perform_attempt',side_effect=fail):
                for minute in (20,40,60):
                    with self.assertRaises(automation.AutomationError):
                        automation.run_automation(path,now=CUT.replace(day=23,hour=7+minute//60,minute=minute%60))
            self.assertEqual(seen,[None,'2026-10-23T07:20:00Z',None])
            self.assertEqual(json.loads((path.parent/'daily-refresh/state.json').read_text())['attempt_count'],3)

    def test_replay_verification_failure_has_a_specific_recovery_code(self):
        import time
        from tokenometer import automation
        from tests.test_automation import AutomationTests
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path,_=AutomationTests()._private_config(root);config=automation.load_config(path)
            config['private_dir'].mkdir(mode=0o700)
            with patch.object(automation,'_recovery_capture',return_value=root/'saved-capture'), \
                    patch.object(automation,'_run_process',return_value=(1,'','')) as child:
                with self.assertRaises(automation.AutomationError) as raised:
                    automation._perform_attempt(config,deadline=time.monotonic()+10,log_path=root/'replay.log')
            self.assertEqual(raised.exception.code,'capture_replay_failed')
            self.assertIn('--from-run',child.call_args.args[0])
