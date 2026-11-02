import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tokenometer.refresh_lock import ledger_lock


class RefreshLockTests(unittest.TestCase):
    def test_competing_process_times_out_then_acquires_after_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / 'usage.sqlite3'
            script = ("import sys\nfrom tokenometer.refresh_lock import ledger_lock\n"
                      "try:\n with ledger_lock(sys.argv[1], timeout=.1): print('acquired')\n"
                      "except TimeoutError: print('busy')\n")
            with ledger_lock(ledger, timeout=1):
                held = subprocess.run([sys.executable, '-c', script, str(ledger)],
                                      capture_output=True, text=True, check=True, timeout=5)
                self.assertEqual(held.stdout.strip(), 'busy')
            free = subprocess.run([sys.executable, '-c', script, str(ledger)],
                                  capture_output=True, text=True, check=True, timeout=5)
            self.assertEqual(free.stdout.strip(), 'acquired')

    def test_inherited_lock_remains_held_after_child_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / 'usage.sqlite3'
            child = ("import sys\nfrom tokenometer.refresh_lock import ledger_lock\n"
                     "with ledger_lock(sys.argv[1], timeout=0, inherited_fd=int(sys.argv[2])):\n"
                     " print('inherited')\n")
            competing = ("import sys\nfrom tokenometer.refresh_lock import ledger_lock\n"
                         "try:\n with ledger_lock(sys.argv[1], timeout=0): print('acquired')\n"
                         "except TimeoutError: print('busy')\n")
            with ledger_lock(ledger) as fd:
                result = subprocess.run([sys.executable, '-c', child, str(ledger), str(fd)],
                                        pass_fds=(fd,), capture_output=True, text=True, check=True, timeout=5)
                self.assertEqual(result.stdout.strip(), 'inherited')
                result = subprocess.run([sys.executable, '-c', competing, str(ledger)],
                                        capture_output=True, text=True, check=True, timeout=5)
                self.assertEqual(result.stdout.strip(), 'busy')

    def test_rejects_unrelated_inherited_descriptor_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / 'usage.sqlite3'
            other = Path(tmp) / 'other'
            other.write_text('')
            with other.open('r+') as stream:
                with self.assertRaises(ValueError):
                    with ledger_lock(ledger, inherited_fd=stream.fileno()):
                        pass
            (Path(tmp) / 'usage.sqlite3.lock').symlink_to(other)
            with self.assertRaises(ValueError):
                with ledger_lock(ledger):
                    pass

    def test_exception_releases_lock_and_keeps_private_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / 'usage.sqlite3'
            with self.assertRaisesRegex(RuntimeError, 'failure'):
                with ledger_lock(ledger):
                    raise RuntimeError('failure')
            lock = Path(tmp) / 'usage.sqlite3.lock'
            self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
            with ledger_lock(ledger, timeout=0):
                pass


if __name__ == '__main__':
    unittest.main()
