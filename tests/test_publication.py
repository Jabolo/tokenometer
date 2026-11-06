from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tokenometer import publication


class PublicationTests(unittest.TestCase):
    def run_record(self, ident=1, **fields):
        return {"id": ident, "head_sha": "a" * 40, "event": "push",
                "status": "completed", "conclusion": "failure", "run_attempt": 1, **fields}

    def test_recovery_stops_after_three_attempts_and_after_success(self):
        args = {"sha": "a" * 40, "current_run": 4}
        self.assertTrue(publication.recovery_needed([self.run_record()], **args))
        self.assertFalse(publication.recovery_needed([self.run_record(run_attempt=3)], **args))
        self.assertFalse(publication.recovery_needed([self.run_record(conclusion="success")], **args))
        self.assertFalse(publication.recovery_needed([self.run_record(status="in_progress")], **args))
        self.assertTrue(publication.recovery_needed([
            self.run_record(4), self.run_record(head_sha="b" * 40, conclusion="success"),
            self.run_record(event="pull_request", conclusion="success")], **args))

    def test_scheduled_noop_success_is_not_a_successful_deployment(self):
        scheduled = self.run_record(2, event="schedule", conclusion="success")
        with patch.dict(publication.os.environ, {
            "GITHUB_REPOSITORY": "owner/repo", "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "3",
        }), patch.object(publication, "_json_api", side_effect=[
            {"object": {"sha": "a" * 40}},
            {"total_count": 3, "workflow_runs": [self.run_record(), scheduled, self.run_record(3)]},
            {"jobs": [{"name": "check", "conclusion": "skipped"}]},
        ]):
            self.assertTrue(publication.scheduled_recovery())

    def test_scheduled_recovery_counts_previous_real_recovery(self):
        with patch.dict(publication.os.environ, {
            "GITHUB_REPOSITORY": "owner/repo", "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "3",
        }), patch.object(publication, "_json_api", side_effect=[
            {"object": {"sha": "a" * 40}},
            {"total_count": 2, "workflow_runs": [self.run_record(2, event="schedule", run_attempt=3)]},
            {"jobs": [{"name": "check", "conclusion": "failure"}]},
        ]):
            self.assertFalse(publication.scheduled_recovery())

    def test_stale_revisions_cannot_trigger_recovery_or_deploy(self):
        with patch.dict(publication.os.environ, {
            "GITHUB_REPOSITORY": "owner/repo", "GITHUB_SHA": "a" * 40,
        }), patch.object(publication, "_json_api", return_value={"object": {"sha": "b" * 40}}):
            self.assertFalse(publication.current_main())
            self.assertFalse(publication.scheduled_recovery())

    def test_production_must_match_all_assets_headers_and_private_boundary(self):
        headers = {"Content-Security-Policy": "default-src 'self'", "Cache-Control": "no-store"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "web").mkdir()
            for name in ("data.json", "index.html", "app.js", "styles.css", "favicon.svg", "404.html"):
                (root / "web" / name).write_bytes(b"expected")
            def response(path):
                return (404, headers, b"missing") if path.startswith(("/.", "/tokenometer/")) else (200, headers, b"expected")
            with patch("tokenometer.release_guard.validate_aggregate_file"), patch.object(
                    publication, "_public_get", side_effect=response):
                publication.verify_production(root)
            for path, result in (("/data.json", (200, headers, b"old")),
                                 ("/app.js", (200, headers, b"old")),
                                 ("/data.json", (200, {}, b"expected")),
                                 ("/.git/config", (200, headers, b"expected"))):
                with self.subTest(path=path), patch("tokenometer.release_guard.validate_aggregate_file"), patch.object(
                        publication, "_public_get", side_effect=lambda p: result if p == path else response(p)):
                    with self.assertRaises(ValueError):
                        publication.verify_production(root)
