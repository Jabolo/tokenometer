from __future__ import annotations

import json
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tokenometer import automation


def aggregate(generated_at="2026-10-22T09:15:00Z", total=1):
    totals = {"fresh_input": total, "cache_read": 0, "cache_write": 0,
              "output": 0, "total": total}
    zero_totals = {"fresh_input": 0, "cache_read": 0, "cache_write": 0,
                   "output": 0, "total": 0}
    return {
        "schema_version": 1,
        "artifact": "tokenometer.public.aggregate.v1",
        "title": "AI mileage",
        "generated_at": generated_at,
        "timezone": "Europe/Warsaw",
        "totals": totals,
        "events": 1,
        "first_recorded_date": "2026-10-22",
        "last_recorded_date": "2026-10-22",
        "recorded_active_days": 1,
        "providers": {
            "codex": {
                "label": "Codex", "totals": totals, "events": 1,
                "undated_events": 0,
                "latest_cutoff": "2026-10-22T09:15:00Z",
                "latest_capture_completed_at": "2026-10-22T09:16:00Z",
                "last_successful_cutoff": "2026-10-22T09:15:00Z",
                "execution_status": "complete", "coverage_status": "qualified",
                "source_errors": 0,
            },
            "antigravity": {
                "label": "Antigravity", "totals": zero_totals, "events": 0,
                "undated_events": 0, "latest_cutoff": None,
                "latest_capture_completed_at": None, "last_successful_cutoff": None,
                "execution_status": "unobserved", "coverage_status": "unobserved",
                "source_errors": 0,
            },
        },
        "daily": [{"date": "2026-10-22", "platform": "codex", "events": 1, **totals}],
        "quality": {
            "coverage_status": "qualified", "historical_account_completeness": "unknown",
            "conflicted_events": 0, "undated_events": 0,
            "undated_totals": zero_totals, "dated_totals": totals,
            "source_errors": 0,
            "note": "Recorded local history. Some history may be missing. Reused context is included; reasoning is part of output.",
        },
    }


class AutomationTests(unittest.TestCase):
    def _private_config(self, root: Path) -> tuple[Path, dict]:
        project = root / "tokenometer"
        local = project / ".local"
        local.mkdir(parents=True, mode=0o700)
        os.chmod(local, 0o700)
        private = local / "daily-refresh"
        config_path = local / "daily-refresh.json"
        raw = {
            "schema_version": 1,
            "project_dir": str(project),
            "repo_url": "https://github.com/Jabolo/tokenometer.git",
            "branch": "main",
            "private_dir": str(private),
            "runs_dir": str(local / "runs"),
            "ledger_path": str(local / "ledger" / "usage.sqlite3"),
            "source_config": None,
            "timezone": "Europe/Warsaw",
            "max_runtime_seconds": 3600,
            "python_executable": sys.executable,
            "git_identity": {"name": "Daily Refresh",
                             "email": "12345+jabolo@" + "users." + "noreply.github.com"},
            "deployment": {"mode": "github_actions_pages"},
        }
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        os.chmod(config_path, 0o600)
        return config_path, raw

    def test_url_credentials_are_rejected_before_any_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, raw = self._private_config(Path(tmp))
            raw["repo_url"] = "https://token@example.com/Jabolo/tokenometer.git"
            path.write_text(json.dumps(raw), encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaises(automation.AutomationError) as raised:
                automation.load_config(path)
            self.assertEqual(raised.exception.code, "invalid_config")

    def test_daily_state_prevents_launch_at_load_and_calendar_double_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, _raw = self._private_config(Path(tmp))
            now = datetime(2026, 10, 22, 7, 15, tzinfo=timezone.utc)
            with patch.object(automation, "_perform_attempt", return_value={"status": "published"}) as run:
                first = automation.run_automation(config_path, now=now)
                second = automation.run_automation(config_path, now=now)
            self.assertEqual(first["status"], "published")
            self.assertEqual(second, {"status": "already_attempted", "local_date": "2026-10-22"})
            self.assertEqual(run.call_count, 1)
            self.assertEqual((Path(tmp) / "tokenometer/.local/daily-refresh/state.json").stat().st_mode & 0o777, 0o600)

    def test_lock_skips_overlapping_launchd_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, _raw = self._private_config(Path(tmp))
            config = automation.load_config(config_path)
            automation._secure_directory(config["private_dir"], create=True)
            now = datetime(2026, 10, 22, 7, 15, tzinfo=timezone.utc)
            with automation._exclusive_lock(config["private_dir"] / "run.lock") as held:
                self.assertTrue(held)
                result = automation.run_automation(config_path, now=now)
            self.assertEqual(result, {"status": "locked"})

    def test_timeout_stops_the_entire_child_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            child_pid_file = Path(tmp) / "child.pid"
            ready_file = Path(tmp) / "child.ready"
            child_code = (
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"open({str(ready_file)!r},'w').close(); time.sleep(30)"
            )
            parent_code = (
                "import os,subprocess,sys,time\n"
                f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}])\n"
                "deadline=time.monotonic()+5\n"
                f"while not os.path.exists({str(ready_file)!r}) and time.monotonic()<deadline:\n"
                "    time.sleep(.01)\n"
                f"if not os.path.exists({str(ready_file)!r}): raise SystemExit(1)\n"
                f"open({str(child_pid_file)!r},'w').write(str(p.pid))\n"
            )
            with self.assertRaises(automation.AutomationError) as raised:
                automation._run_process([sys.executable, "-c", parent_code], cwd=Path(tmp),
                                        deadline=time.monotonic() + 1.0)
            self.assertEqual(raised.exception.code, "overall_timeout")
            self.assertTrue(ready_file.exists(), "the child did not install its SIGTERM handler")
            child_pid = int(child_pid_file.read_text())
            for _ in range(30):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.fail("the refresh child survived timeout cleanup")

    def test_signal_cleanup_kills_group_after_leader_exits(self):
        previous = automation._install_signal_cleanup()
        try:
            process = type("CompletedProcess", (), {"poll": lambda self: 0})()
            handler = signal.getsignal(signal.SIGTERM)
            with patch.object(automation, "_ACTIVE_PROCESS", process), \
                    patch.object(automation, "_kill_process_group") as cleanup:
                with self.assertRaises(SystemExit):
                    handler(signal.SIGTERM, None)
            cleanup.assert_called_once_with(process)
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_failed_refresh_is_retained_privately_and_never_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, _raw = self._private_config(Path(tmp))
            config = automation.load_config(config_path)
            automation._secure_directory(config["private_dir"], create=True)
            log_dir = config["private_dir"] / "logs"
            automation._secure_directory(log_dir, create=True)
            log_path = log_dir / "test.log"
            deadline = time.monotonic() + 5
            with patch.object(automation, "_run_process", return_value=(1, "", "PRIVATE_TOKEN")), \
                    patch.object(automation, "_publish") as publish:
                with self.assertRaises(automation.AutomationError) as raised:
                    automation._perform_attempt(config, deadline=deadline, log_path=log_path)
            self.assertEqual(raised.exception.code, "refresh_failed")
            publish.assert_not_called()
            attempts = list((config["private_dir"] / "attempts").iterdir())
            self.assertEqual(len(attempts), 1)
            self.assertTrue(attempts[0].is_dir())
            log = log_path.read_text()
            self.assertNotIn("PRIVATE_TOKEN", log)
            self.assertIn('"status": "refresh_finished"', log)

    def test_partial_refresh_result_is_validated_and_published_with_caveat(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, _raw = self._private_config(Path(tmp))
            config = automation.load_config(config_path)
            automation._secure_directory(config["private_dir"], create=True)
            log_dir = config["private_dir"] / "logs"
            automation._secure_directory(log_dir, create=True)
            log_path = log_dir / "partial.log"
            result_payload = {"summary": {"events": 1, "total_tokens": 1, "coverage_status": "partial"}}

            def partial_refresh(arguments, *, cwd, deadline, env=None):
                output = Path(arguments[arguments.index("--public-output") + 1])
                value = aggregate(total=1)
                value["quality"]["coverage_status"] = "partial"
                value["providers"]["codex"]["coverage_status"] = "partial"
                output.write_text(json.dumps(value), encoding="utf-8")
                return 2, json.dumps(result_payload), ""

            with patch.object(automation, "_run_process", side_effect=partial_refresh), \
                    patch.object(automation, "_publish", return_value="published") as publish:
                result = automation._perform_attempt(config, deadline=time.monotonic() + 10,
                                                    log_path=log_path)
            self.assertEqual(result["status"], "published")
            self.assertEqual(result["coverage_status"], "partial")
            publish.assert_called_once()

    def test_transient_publish_failure_retries_same_validated_aggregate_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, _raw = self._private_config(Path(tmp))
            config = automation.load_config(config_path)
            automation._secure_directory(config["private_dir"], create=True)
            log_dir = config["private_dir"] / "logs"
            automation._secure_directory(log_dir, create=True)
            log_path = log_dir / "retry.log"
            source_calls = 0
            publish_attempts = []

            def source_refresh(arguments, *, cwd, deadline, env=None):
                nonlocal source_calls
                source_calls += 1
                output = Path(arguments[arguments.index("--public-output") + 1])
                output.write_text(json.dumps(aggregate(total=4)), encoding="utf-8")
                return 0, json.dumps({"summary": {"events": 1, "total_tokens": 4,
                                                  "coverage_status": "qualified"}}), ""

            def publish(config_arg, staged_data, deadline, validated, log_path_arg):
                publish_attempts.append((staged_data, staged_data.read_bytes(), validated, deadline))
                if len(publish_attempts) == 1:
                    raise automation.AutomationError("clone_failed")
                return "unchanged"

            with patch.object(automation, "_run_process", side_effect=source_refresh), \
                    patch.object(automation, "_publish", side_effect=publish), \
                    patch.object(automation.time, "sleep") as sleep:
                result = automation._perform_attempt(config, deadline=time.monotonic() + 30,
                                                    log_path=log_path)

            self.assertEqual(result["status"], "unchanged")
            self.assertEqual(source_calls, 1)
            self.assertEqual(len(publish_attempts), 2)
            self.assertEqual(publish_attempts[0][0], publish_attempts[1][0])
            self.assertEqual(publish_attempts[0][1], publish_attempts[1][1])
            self.assertIs(publish_attempts[0][2], publish_attempts[1][2])
            self.assertEqual(publish_attempts[0][3], publish_attempts[1][3])
            sleep.assert_called_once_with(automation.PUBLISH_RETRY_DELAY_SECONDS)

    def test_aggregate_guard_failure_never_retries_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, _raw = self._private_config(Path(tmp))
            config = automation.load_config(config_path)
            automation._secure_directory(config["private_dir"], create=True)
            log_dir = config["private_dir"] / "logs"
            automation._secure_directory(log_dir, create=True)
            log_path = log_dir / "guard-failure.log"
            source_calls = 0

            def source_refresh(arguments, *, cwd, deadline, env=None):
                nonlocal source_calls
                source_calls += 1
                output = Path(arguments[arguments.index("--public-output") + 1])
                output.write_text(json.dumps(aggregate(total=4)), encoding="utf-8")
                return 0, json.dumps({"summary": {"events": 1, "total_tokens": 4,
                                                  "coverage_status": "qualified"}}), ""

            with patch.object(automation, "_run_process", side_effect=source_refresh), \
                    patch.object(automation, "_release_guard",
                                 side_effect=automation.AutomationError("aggregate_rejected")), \
                    patch.object(automation, "_publish") as publish, \
                    patch.object(automation.time, "sleep") as sleep:
                with self.assertRaises(automation.AutomationError) as raised:
                    automation._perform_attempt(config, deadline=time.monotonic() + 30,
                                                log_path=log_path)

            self.assertEqual(raised.exception.code, "aggregate_rejected")
            self.assertEqual(source_calls, 1)
            publish.assert_not_called()
            sleep.assert_not_called()

    def test_publish_retry_exhausts_after_two_attempts_with_same_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, _raw = self._private_config(Path(tmp))
            config = automation.load_config(config_path)
            automation._secure_directory(config["private_dir"], create=True)
            log_dir = config["private_dir"] / "logs"
            automation._secure_directory(log_dir, create=True)
            log_path = log_dir / "retry-exhausted.log"
            source_calls = 0
            published = []

            def source_refresh(arguments, *, cwd, deadline, env=None):
                nonlocal source_calls
                source_calls += 1
                output = Path(arguments[arguments.index("--public-output") + 1])
                output.write_text(json.dumps(aggregate(total=5)), encoding="utf-8")
                return 0, json.dumps({"summary": {"events": 1, "total_tokens": 5,
                                                  "coverage_status": "qualified"}}), ""

            def publish(_config, staged_data, _deadline, _validated, _log_path):
                published.append((staged_data, staged_data.read_bytes()))
                raise automation.AutomationError("git_push_failed")

            with patch.object(automation, "_run_process", side_effect=source_refresh), \
                    patch.object(automation, "_publish", side_effect=publish), \
                    patch.object(automation.time, "sleep") as sleep:
                with self.assertRaises(automation.AutomationError) as raised:
                    automation._perform_attempt(config, deadline=time.monotonic() + 30,
                                                log_path=log_path)

            self.assertEqual(raised.exception.code, "git_push_failed")
            self.assertEqual(source_calls, 1)
            self.assertEqual(len(published), 2)
            self.assertEqual(published[0], published[1])
            sleep.assert_called_once_with(automation.PUBLISH_RETRY_DELAY_SECONDS)

    def test_publish_retry_is_skipped_when_deadline_cannot_cover_backoff(self):
        with patch.object(automation, "_publish",
                          side_effect=automation.AutomationError("clone_failed")) as publish, \
                patch.object(automation, "_append_log"), \
                patch.object(automation.time, "sleep") as sleep:
            with self.assertRaises(automation.AutomationError) as raised:
                automation._publish_with_retry({}, Path("unused.json"),
                                               time.monotonic() + 1, {}, Path("unused.log"))

        self.assertEqual(raised.exception.code, "clone_failed")
        publish.assert_called_once()
        sleep.assert_not_called()

    def test_publish_stages_and_pushes_only_web_data_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, raw = self._private_config(root)
            config = automation.load_config(config_path)
            clone = config["private_dir"] / "public-repo"
            (clone / "web").mkdir(parents=True, mode=0o700)
            (clone / ".git").mkdir(mode=0o700)
            clone.chmod(0o700)
            staged = root / "staged.json"
            staged.write_text(json.dumps(aggregate(total=2)), encoding="utf-8")
            calls = []

            def fake_git(args, *, cwd, deadline, **_kwargs):
                calls.append(tuple(args))
                if args == ["rev-parse", "--show-toplevel"]:
                    return str(clone)
                if args == ["remote", "get-url", "origin"]:
                    return raw["repo_url"]
                if args == ["branch", "--show-current"]:
                    return "main"
                if args[0:2] == ["rev-list", "--left-right"]:
                    return "0\t0"
                if args[:3] == ["diff", "--cached", "--name-only"]:
                    return "web/data.json"
                if args[:2] == ["diff-tree", "--no-commit-id"]:
                    return "web/data.json"
                return ""

            with patch.object(automation, "_git", side_effect=fake_git), \
                    patch.object(automation, "_check_committed_tree"), \
                    patch.object(automation, "_check_selected_path") as check_path, \
                    patch.object(automation, "_check_staged_release"), \
                    patch.object(automation, "_release_guard", side_effect=lambda _config, path: json.loads(path.read_text())):
                result = automation._publish(config, staged, time.monotonic() + 30)
            self.assertEqual(result, "published")
            check_path.assert_called_once()
            self.assertEqual((clone / "web/data.json").read_text(), staged.read_text())
            self.assertIn(("add", "--", "web/data.json"), calls)
            self.assertIn(("push", "origin", "HEAD:refs/heads/main"), calls)
            self.assertFalse(any("SECRET" in " ".join(call) for call in calls))

    def test_generated_at_only_change_does_not_commit_or_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, raw = self._private_config(root)
            config = automation.load_config(config_path)
            clone = config["private_dir"] / "public-repo"
            (clone / "web").mkdir(parents=True, mode=0o700)
            (clone / ".git").mkdir(mode=0o700)
            clone.chmod(0o700)
            existing = aggregate("2026-10-21T09:15:00Z", total=2)
            staged_value = aggregate("2026-10-22T09:15:00Z", total=2)
            destination = clone / "web/data.json"
            destination.write_text(json.dumps(existing), encoding="utf-8")
            staged = root / "staged.json"
            staged.write_text(json.dumps(staged_value), encoding="utf-8")
            calls = []

            def fake_git(args, *, cwd, deadline, **_kwargs):
                calls.append(tuple(args))
                if args == ["rev-parse", "--show-toplevel"]:
                    return str(clone)
                if args == ["remote", "get-url", "origin"]:
                    return raw["repo_url"]
                if args == ["branch", "--show-current"]:
                    return "main"
                if args[:2] == ["rev-list", "--left-right"]:
                    return "0\t0"
                return ""

            with patch.object(automation, "_git", side_effect=fake_git), \
                    patch.object(automation, "_check_committed_tree"), \
                    patch.object(automation, "_release_guard", side_effect=lambda _config, path: json.loads(path.read_text())):
                result = automation._publish(config, staged, time.monotonic() + 30)
            self.assertEqual(result, "unchanged")
            self.assertFalse(any(call[0] in {"add", "commit", "push"} for call in calls))
            self.assertEqual(json.loads(destination.read_text())["generated_at"], existing["generated_at"])

    def test_failed_push_clone_is_retained_and_next_attempt_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, raw = self._private_config(root)
            config = automation.load_config(config_path)
            clone = config["private_dir"] / "public-repo"
            (clone / "web").mkdir(parents=True, mode=0o700)
            (clone / ".git").mkdir(mode=0o700)
            clone.chmod(0o700)
            first_stage = root / "first.json"
            first_stage.write_text(json.dumps(aggregate(total=2)), encoding="utf-8")
            second_stage = root / "second.json"
            second_stage.write_text(json.dumps(aggregate("2026-10-23T09:15:00Z", total=3)), encoding="utf-8")
            calls = []
            pushes = 0
            clones = 0
            revision_checks = 0

            def fake_git(args, *, cwd, deadline, **_kwargs):
                nonlocal pushes, revision_checks
                calls.append(tuple(args))
                if args == ["rev-parse", "--show-toplevel"]:
                    return str(clone)
                if args == ["remote", "get-url", "origin"]:
                    return raw["repo_url"]
                if args == ["branch", "--show-current"]:
                    return "main"
                if args[:2] == ["rev-list", "--left-right"]:
                    revision_checks += 1
                    return "1\t0" if revision_checks == 2 else "0\t0"
                if args[:3] == ["diff", "--cached", "--name-only"]:
                    return "web/data.json"
                if args[:2] == ["diff-tree", "--no-commit-id"]:
                    return "web/data.json"
                if args[0] == "push":
                    pushes += 1
                    if pushes == 1:
                        raise automation.AutomationError("git_operation_failed")
                return ""

            def fake_process(arguments, *, cwd, deadline, env=None):
                nonlocal clones
                if arguments[:2] == ["git", "clone"]:
                    clones += 1
                    (clone / "web").mkdir(parents=True, mode=0o700)
                    (clone / ".git").mkdir(mode=0o700)
                    clone.chmod(0o700)
                    return 0, "", ""
                return 0, "", ""

            with patch.object(automation, "_git", side_effect=fake_git), \
                    patch.object(automation, "_run_process", side_effect=fake_process), \
                    patch.object(automation, "_check_committed_tree"), \
                    patch.object(automation, "_check_selected_path"), \
                    patch.object(automation, "_check_staged_release"), \
                    patch.object(automation, "_release_guard", side_effect=lambda _config, path: json.loads(path.read_text())):
                with self.assertRaises(automation.AutomationError) as raised:
                    automation._publish(config, first_stage, time.monotonic() + 30)
                self.assertEqual(raised.exception.code, "git_operation_failed")
                result = automation._publish(config, second_stage, time.monotonic() + 30)

            self.assertEqual(result, "published")
            retained = list((config["private_dir"] / "attempts").glob("clone-recovery-*"))
            self.assertEqual(len(retained), 1)
            self.assertTrue((retained[0] / ".git").is_dir())
            self.assertEqual(pushes, 2)
            self.assertEqual(clones, 1)

    def test_unexpected_staged_path_aborts_before_commit_or_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, raw = self._private_config(root)
            config = automation.load_config(config_path)
            clone = config["private_dir"] / "public-repo"
            (clone / "web").mkdir(parents=True, mode=0o700)
            (clone / ".git").mkdir(mode=0o700)
            clone.chmod(0o700)
            staged = root / "staged.json"
            staged.write_text(json.dumps(aggregate(total=3)), encoding="utf-8")
            calls = []

            def fake_git(args, *, cwd, deadline, **_kwargs):
                calls.append(tuple(args))
                if args == ["rev-parse", "--show-toplevel"]:
                    return str(clone)
                if args == ["remote", "get-url", "origin"]:
                    return raw["repo_url"]
                if args == ["branch", "--show-current"]:
                    return "main"
                if args[:2] == ["rev-list", "--left-right"]:
                    return "0\t0"
                if args[:3] == ["diff", "--cached", "--name-only"]:
                    return "README.md\nweb/data.json"
                return ""

            with patch.object(automation, "_git", side_effect=fake_git), \
                    patch.object(automation, "_check_committed_tree"), \
                    patch.object(automation, "_check_selected_path"), \
                    patch.object(automation, "_check_staged_release"), \
                    patch.object(automation, "_release_guard", side_effect=lambda _config, path: json.loads(path.read_text())):
                with self.assertRaises(automation.AutomationError) as raised:
                    automation._publish(config, staged, time.monotonic() + 30)
            self.assertEqual(raised.exception.code, "staged_path_set_invalid")
            self.assertFalse(any(call[0] in {"commit", "push"} for call in calls))

    def test_rejected_committed_tree_stops_before_staging_or_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, raw = self._private_config(root)
            config = automation.load_config(config_path)
            clone = config["private_dir"] / "public-repo"
            (clone / "web").mkdir(parents=True, mode=0o700)
            (clone / "README.md").write_text("safe placeholder\n", encoding="utf-8")
            (clone / "unexpected-private.txt").write_text("private fixture\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", os.fspath(clone)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", os.fspath(clone), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", os.fspath(clone), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", os.fspath(clone), "add", "README.md", "unexpected-private.txt"], check=True)
            subprocess.run(["git", "-C", os.fspath(clone), "commit", "-qm", "fixture"], check=True)
            clone.chmod(0o700)
            staged = root / "staged.json"
            staged.write_text(json.dumps(aggregate(total=3)), encoding="utf-8")
            calls = []

            def fake_git(args, *, cwd, deadline, **_kwargs):
                calls.append(tuple(args))
                if args == ["rev-parse", "--show-toplevel"]:
                    return str(clone)
                if args == ["remote", "get-url", "origin"]:
                    return raw["repo_url"]
                if args == ["branch", "--show-current"]:
                    return "main"
                if args[:2] == ["rev-list", "--left-right"]:
                    return "0\t0"
                return ""

            with patch.object(automation, "_git", side_effect=fake_git), \
                    patch.object(automation, "_release_guard",
                                 side_effect=lambda _config, path: json.loads(path.read_text())):
                with self.assertRaises(automation.AutomationError) as raised:
                    automation._publish(config, staged, time.monotonic() + 30)

            self.assertEqual(raised.exception.code, "public_tree_rejected")
            self.assertFalse(any(call[0] in {"add", "commit", "push"} for call in calls))
            self.assertTrue(clone.is_dir())


if __name__ == "__main__":
    unittest.main()
