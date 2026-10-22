import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tokenometer.aggregate import summarize
from tokenometer.models import UsageEvent, digest
from tokenometer.public import public_snapshot
from tokenometer.release_guard import (
    PUBLIC_RELEASE_PATHS,
    validate_aggregate_file,
    validate_public_payload,
    validate_public_paths,
    validate_staged_release,
)


def aggregate_fixture():
    event = UsageEvent(
        "codex:" + digest("synthetic-event"), "codex", digest("synthetic-session"),
        "2026-10-21T23:30:00Z", 10, 100, 2, 8, 3,
    )
    summary = summarize([event])
    return public_snapshot({
        "summary": summary,
        "quality": {"coverage_status": "partial", "conflicted_events": 0},
        "providers": {
            "codex": {
                "latest_cutoff": "2026-10-22T01:00:00Z",
                "latest_capture_completed_at": "2026-10-22T01:01:00Z",
                "last_successful_cutoff": "2026-10-22T01:00:00Z",
                "execution_status": "complete",
                "coverage_status": "partial",
                "errors": 0,
                "account": "SYNTHETIC_PRIVATE_CANARY",
                "roots": ["/Users/PRIVATE_CANARY"],
            },
        },
    })


class ReleaseGuardTests(unittest.TestCase):
    def test_valid_synthetic_aggregate_is_returned_in_canonical_shape(self):
        value = aggregate_fixture()
        result = validate_public_payload(value)
        self.assertEqual(result, value)
        self.assertEqual(result["totals"]["total"], 120)
        self.assertEqual(result["quality"]["historical_account_completeness"], "unknown")
        self.assertEqual(result["providers"]["antigravity"]["execution_status"], "unobserved")

    def test_unknown_or_account_specific_fields_are_rejected(self):
        for field, value in (("source_ids", ["SYNTHETIC_ID"]), ("debug", "private detail")):
            payload = aggregate_fixture()
            payload[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Invalid public aggregate fields"):
                validate_public_payload(payload)

    def test_unknown_nested_ids_are_rejected(self):
        payload = aggregate_fixture()
        payload["providers"]["codex"]["raw_event_id"] = "SYNTHETIC_ID"
        with self.assertRaisesRegex(ValueError, "Invalid provider codex fields"):
            validate_public_payload(payload)

    def test_arithmetic_rollup_mismatch_is_rejected(self):
        payload = aggregate_fixture()
        payload["daily"][0]["total"] += 1
        with self.assertRaisesRegex(ValueError, "Inconsistent daily totals"):
            validate_public_payload(payload)

    def test_provider_event_counts_cannot_be_reassigned_between_providers(self):
        payload = aggregate_fixture()
        payload["providers"]["codex"]["events"] = 0
        payload["providers"]["antigravity"]["events"] = 1
        with self.assertRaisesRegex(ValueError, "Inconsistent event counts for codex"):
            validate_public_payload(payload)

    def test_status_and_dates_are_strict(self):
        payload = aggregate_fixture()
        payload["providers"]["codex"]["coverage_status"] = "complete"
        with self.assertRaisesRegex(ValueError, "Invalid provider codex coverage status"):
            validate_public_payload(payload)
        payload = aggregate_fixture()
        payload["daily"][0]["date"] = "2026-13-51"
        with self.assertRaisesRegex(ValueError, "Invalid daily date"):
            validate_public_payload(payload)

    def test_invalid_payload_does_not_echo_sensitive_values(self):
        payload = aggregate_fixture()
        payload["title"] = "private@example.org"
        with self.assertRaises(ValueError) as raised:
            validate_public_payload(payload)
        self.assertNotIn("private@example.org", str(raised.exception))

    def test_aggregate_file_rejects_duplicate_json_keys(self):
        value = aggregate_fixture()
        encoded = json.dumps(value)
        encoded = encoded[:-1] + ',"title":"SYNTHETIC_PRIVATE_CANARY"}'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            path.write_text(encoded, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid aggregate file"):
                validate_aggregate_file(path)

    def test_aggregate_file_accepts_only_a_bounded_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            path.write_text(json.dumps(aggregate_fixture()), encoding="utf-8")
            self.assertEqual(validate_aggregate_file(path)["events"], 1)
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "Invalid aggregate file"):
                validate_aggregate_file(link)

    def test_release_path_allowlist_is_explicit_and_private_docs_are_absent(self):
        self.assertIn("web/data.json", PUBLIC_RELEASE_PATHS)
        self.assertIn("tokenometer/release_guard.py", PUBLIC_RELEASE_PATHS)
        self.assertNotIn("AGENTS.md", PUBLIC_RELEASE_PATHS)
        self.assertNotIn("PRODUCT.md", PUBLIC_RELEASE_PATHS)
        self.assertNotIn("docs/PIPELINE-DELIVERY.md", PUBLIC_RELEASE_PATHS)
        with self.assertRaisesRegex(ValueError, "Unapproved public path"):
            validate_public_paths(".", ["AGENTS.md"])

    def test_path_traversal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid release path"):
            validate_public_paths(".", ["web/../AGENTS.md"])

    def test_synthetic_home_placeholder_is_allowed_in_fixture_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("Synthetic fixture: /Users/PRIVATE_CANARY/local\n", encoding="utf-8")
            result = validate_public_paths(root, ["README.md"])
            self.assertEqual(result["status"], "pass")

    def test_actual_home_path_and_email_are_reported_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            personal_home = b"Path: /Users/" + b"owner-private" + b"/Documents/data\n"
            personal_contact = b"Contact: owner-private@" + b"company.co\n"
            (root / "README.md").write_bytes(personal_home + personal_contact)
            with self.assertRaises(ValueError) as raised:
                validate_public_paths(root, ["README.md"])
            message = str(raised.exception)
            self.assertIn("personal-home-path", message)
            self.assertIn("personal-email", message)
            self.assertNotIn("owner-private", message)
            self.assertNotIn("company.co", message)

    def test_allowlisted_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.txt"
            target.write_text("safe", encoding="utf-8")
            (root / "README.md").symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink in release path"):
                validate_public_paths(root, ["README.md"])

    def test_staged_allowlisted_deletion_is_rejected(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"README.md\0", stderr=b"")
        empty_index = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
        with tempfile.TemporaryDirectory() as directory:
            with patch("tokenometer.release_guard.subprocess.run", side_effect=[completed, empty_index]) as run:
                with self.assertRaisesRegex(ValueError, "staged file is not a regular file"):
                    validate_staged_release(directory)
            command = run.call_args_list[0].args[0]
            self.assertIn("--diff-filter=ACMRTD", command)
            self.assertIn("--no-renames", command)


if __name__ == "__main__":
    unittest.main()
