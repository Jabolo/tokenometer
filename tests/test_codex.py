from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tokenometer.collectors.codex import collect, discover_roots


CUTOFF = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def usage(input_tokens=10, cached=2, cache_write=1, output=4, reasoning=1):
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": input_tokens + output,
    }


def counters(values):
    return dict(zip(FIELDS, values))


def meta(thread_id, *, parent=None, account=None, cwd="/unrelated/project"):
    payload = {"id": thread_id, "cwd": cwd}
    if parent:
        payload["parent_thread_id"] = parent
    if account:
        payload["creator_account_id"] = account
    return {"type": "session_meta", "timestamp": "2026-10-20T00:00:00Z", "payload": payload}


def modern(response_id, *, thread_id=None, timestamp="2026-10-20T00:00:00Z", values=None, direct=False, extra=None):
    payload = {"response_id": response_id, "turn_id": "turn-safe"}
    if thread_id:
        payload["thread_id"] = thread_id
    if values is None:
        values = usage()
    if direct:
        payload.update(values)
    else:
        payload["usage"] = values
    if extra:
        payload.update(extra)
    row = {"type": "token_usage_record", "payload": payload}
    if timestamp is not ...:
        row["timestamp"] = timestamp
    return row


def legacy(timestamp, ordinal, total, last):
    payload = {
        "type": "token_count",
        "ordinal": ordinal,
        "info": {"total_token_usage": counters(total), "last_token_usage": counters(last)},
    }
    row = {"type": "event_msg", "payload": payload}
    if timestamp is not ...:
        row["timestamp"] = timestamp
    return row


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def write_zstd(path: Path, rows):
    content = "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")
    try:
        from compression import zstd
    except ImportError:
        executable = shutil.which("zstd")
        if not executable:
            raise unittest.SkipTest("zstd compression unavailable in this Python and PATH")
        completed = subprocess.run([executable, "-c"], input=content, capture_output=True, check=True)
        compressed = completed.stdout
    else:
        compressed = zstd.compress(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(compressed)


class CodexCollectorTests(unittest.TestCase):
    def test_all_projects_and_account_switches_are_included_and_response_copies_dedup_globally(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "codex"
            event = modern("response-private-1", thread_id="thread-a", extra={"thread_token_usage": usage()})
            first = root / "sessions" / "one.jsonl"
            write_jsonl(first, [meta("thread-a", account="account-one"), event])
            copied = root / "archived_sessions" / "copy.jsonl"
            missing_id_copy = json.loads(json.dumps(event))
            del missing_id_copy["payload"]["response_id"]
            missing_id_copy["payload"]["ordinal"] = 2
            write_jsonl(copied, [meta("thread-b", parent="thread-a", account="account-two"), event, missing_id_copy])
            other_project = root / "profiles" / "switch-2" / "sessions" / "two.jsonl"
            write_jsonl(other_project, [
                meta("thread-c", account="account-two", cwd="/another/workspace"),
                modern("response-private-2", thread_id="thread-c", values=usage(8, 1, 1, 3, 1)),
            ])

            result = collect([root, root], CUTOFF)

            self.assertEqual(len(result.events), 2)
            parent_event = next(event for event in result.events if event.fresh_input == 7)
            self.assertEqual((parent_event.fresh_input, parent_event.cache_read, parent_event.cache_write), (7, 2, 1))
            self.assertEqual((parent_event.output, parent_event.reasoning), (4, 1))
            self.assertEqual(parent_event.source_kind, "recorded")
            self.assertEqual(result.stats["modern_duplicate_records"], 2)
            self.assertEqual(result.stats["threads_with_metadata"], 3)
            self.assertEqual(result.stats["files_with_account_metadata"], 3)
            self.assertEqual(result.stats["distinct_accounts_observed"], 2)
            self.assertNotIn("account-one", repr(result))
            self.assertNotIn("response-private", repr(result))
            self.assertNotIn(str(root), repr(result))

    def test_compressed_and_plain_copies_stream_deduplicate_with_source_receipts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [
                meta("compressed-copy-thread", account="account-private-one"),
                modern("compressed-copy-response", thread_id="compressed-copy-thread"),
            ]
            plain_path = root / "sessions" / "plain.jsonl"
            compressed_path = root / "archived_sessions" / "copy.jsonl.zst"
            write_jsonl(plain_path, rows)
            write_zstd(compressed_path, rows)

            result = collect([root], CUTOFF)

            self.assertEqual(len(result.events), 1)
            self.assertEqual(result.stats["compressed_files_discovered"], 1)
            self.assertEqual(result.stats["compressed_files_read"], 1)
            self.assertEqual(result.stats["compressed_read_errors"], 0)
            self.assertEqual(result.stats["modern_duplicate_records"], 1)
            self.assertEqual(result.stats["files_with_account_metadata"], 2)
            self.assertEqual(result.stats["distinct_accounts_observed"], 1)
            refs = result.events[0].counter_receipt["source_refs"]
            self.assertEqual(len(refs), 2)
            self.assertEqual({ref["kind"] for ref in refs}, {"jsonl"})
            self.assertEqual({ref["index"] for ref in refs}, {1})
            expected_sources = {
                hashlib.sha256(str(path.resolve()).encode()).hexdigest()
                for path in (plain_path, compressed_path)
            }
            self.assertEqual({ref["source"] for ref in refs}, expected_sources)
            self.assertNotIn("account-private-one", repr(result))
            self.assertNotIn(str(root), repr(result))

    def test_corrupt_or_truncated_compressed_history_is_counted_as_source_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            corrupt = root / "sessions" / "corrupt.jsonl.zst"
            truncated = root / "sessions" / "truncated.jsonl.zst"
            corrupt.parent.mkdir(parents=True)
            corrupt.write_bytes(b"not-a-zstandard-frame")
            write_zstd(truncated, [meta("truncated-thread"), modern("truncated-response", thread_id="truncated-thread")])
            truncated.write_bytes(truncated.read_bytes()[:-3])

            result = collect([root], CUTOFF)

            self.assertEqual(result.stats["compressed_files_discovered"], 2)
            self.assertEqual(result.stats["compressed_files_read"], 0)
            self.assertEqual(result.stats["compressed_read_errors"], 2)
            self.assertEqual(result.stats["read_errors"], 2)
            self.assertGreaterEqual(result.stats["errors"], 2)
            self.assertIn("codex_source_read_error", result.warnings)
            self.assertNotIn(str(root), repr(result))

    def test_zstd_cli_fallback_streams_when_stdlib_backend_is_unavailable(self):
        import tokenometer.collectors.codex as codex_collector

        if not shutil.which("zstd"):
            self.skipTest("zstd CLI is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            compressed = root / "sessions" / "cli-fallback.jsonl.zst"
            rows = [meta("cli-fallback-thread"), modern("cli-fallback-response", thread_id="cli-fallback-thread")]
            write_zstd(compressed, rows)
            with patch.object(codex_collector, "_stdlib_zstd", None):
                result = collect([root], CUTOFF)

            self.assertEqual(len(result.events), 1)
            self.assertEqual(result.stats["source_inventory"]["zstd_capability"], "cli")
            self.assertEqual(result.stats["compressed_files_read"], 1)
            self.assertEqual(result.stats["compressed_read_errors"], 0)

    def test_compressed_history_has_explicit_error_when_no_zstd_backend_exists(self):
        import tokenometer.collectors.codex as codex_collector

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            compressed = root / "sessions" / "without-backend.jsonl.zst"
            compressed.parent.mkdir(parents=True)
            compressed.write_bytes(b"opaque")
            with patch.object(codex_collector, "_stdlib_zstd", None), patch.object(
                codex_collector.shutil, "which", return_value=None
            ):
                result = collect([root], CUTOFF)

            self.assertEqual(result.stats["source_inventory"]["zstd_capability"], "unavailable")
            self.assertEqual(result.stats["compressed_files_discovered"], 1)
            self.assertEqual(result.stats["compressed_read_errors"], 1)
            self.assertGreaterEqual(result.stats["errors"], 1)

    def test_modern_records_win_over_cumulative_snapshots_and_new_direct_shape_is_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [
                meta("thread-modern"),
                legacy("2026-10-20T00:00:00Z", 1, (10, 2, 1, 4, 1, 14), (10, 2, 1, 4, 1, 14)),
                legacy("2026-10-20T00:01:00Z", 2, (13, 3, 1, 6, 2, 19), (3, 1, 0, 2, 1, 5)),
                modern("response-modern", thread_id="thread-modern", direct=True, extra={
                    "thread_token_usage": counters((100, 10, 5, 20, 4, 120)),
                    "future_extension_field": {"ignored": True},
                }),
            ]
            write_jsonl(root / "sessions" / "thread.jsonl", rows)

            result = collect([root], CUTOFF)

            self.assertEqual(len(result.events), 1)
            self.assertEqual(result.events[0].source_kind, "recorded")
            self.assertEqual(result.events[0].total, 14)
            self.assertEqual(result.stats["legacy_threads_suppressed_by_modern"], 1)
            self.assertEqual(result.stats["legacy_events"], 2)
            self.assertEqual(result.stats["legacy_events_emitted"], 0)
            self.assertEqual(result.stats["legacy_events_suppressed_modern_overlap"], 2)
            self.assertEqual(result.stats["legacy_suppressed_token_upper_bound"], 19)

    def test_new_envelope_shape_and_partial_known_counters_are_preserved_without_guessing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [
                meta("thread-envelope"),
                {
                    "type": "token_usage_record",
                    "timestamp": "2026-10-20T00:00:00Z",
                    "response_id": "private-envelope-response",
                    "thread_id": "thread-envelope",
                    "usage": {
                        "input_tokens": 8,
                        "cached_input_tokens": 1,
                        "cache_write_input_tokens": 1,
                        "output_tokens": 3,
                        "extension_counter": 999,
                    },
                },
            ]
            write_jsonl(root / "sessions" / "thread.jsonl", rows)

            result = collect([root], CUTOFF)

            self.assertEqual(len(result.events), 1)
            event = result.events[0]
            self.assertEqual((event.fresh_input, event.cache_read, event.cache_write), (6, 1, 1))
            self.assertEqual(event.output, 3)
            self.assertIsNone(event.reasoning)
            self.assertTrue(result.stats["incomplete"])
            self.assertEqual(result.stats["modern_missing_fields"]["reasoning_output_tokens"], 1)
            self.assertIn("codex_missing_usage_fields", result.warnings)
            self.assertNotIn("private-envelope-response", repr(result))
            receipt = event.counter_receipt
            self.assertEqual(set(receipt), {"schema", "source_refs", "counters"})
            self.assertEqual(receipt["schema"], "codex.response.v1")
            self.assertEqual(set(receipt["counters"]), set(FIELDS))
            self.assertEqual(receipt["counters"], {
                "input_tokens": 8,
                "cached_input_tokens": 1,
                "cache_write_input_tokens": 1,
                "output_tokens": 3,
                "reasoning_output_tokens": None,
                "total_tokens": None,
            })
            source_ref = receipt["source_refs"][0]
            self.assertEqual(set(source_ref), {"source", "kind", "index", "retry"})
            self.assertEqual(source_ref, {
                "source": hashlib.sha256(str((root / "sessions" / "thread.jsonl").resolve()).encode()).hexdigest(),
                "kind": "jsonl",
                "index": 1,
                "retry": 0,
            })
            self.assertRegex(source_ref["source"], r"^[a-f0-9]{64}$")
            self.assertRegex(event.key, r"^codex:[a-f0-9]{64}$")
            self.assertRegex(event.session_key, r"^[a-f0-9]{64}$")
            self.assertEqual(event.session_key, hashlib.sha256(b"thread-envelope").hexdigest())
            self.assertNotIn(str(root), repr(receipt))
            self.assertNotIn("thread-envelope", repr(receipt))
            self.assertNotIn("private-envelope-response", repr(receipt))

    def test_conflicting_response_copies_are_excluded_and_warnings_do_not_leak_source_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "sessions" / "thread.jsonl"
            rows = [
                meta("thread-x"),
                modern("conflicting-private-id", thread_id="thread-x", values=usage()),
                modern("conflicting-private-id", thread_id="thread-x", values=usage(input_tokens=11)),
                modern("valid-private-id", thread_id="thread-x", values=usage(6, 1, 1, 2, 1)),
            ]
            write_jsonl(path, rows)

            result = collect([root], CUTOFF)

            self.assertEqual(len(result.events), 1)
            self.assertGreater(result.stats["modern_conflicts"], 0)
            self.assertGreater(result.stats["errors"], 0)
            self.assertIn("codex_response_conflict", result.warnings)
            self.assertNotIn("conflicting-private-id", repr(result))
            self.assertNotIn("valid-private-id", repr(result))
            self.assertNotIn(str(path), repr(result))

    def test_legacy_deltas_match_all_six_fields_and_copied_files_count_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [
                meta("legacy-root"),
                legacy("2026-10-20T00:00:00Z", 1, (10, 2, 1, 4, 1, 14), (10, 2, 1, 4, 1, 14)),
                legacy("2026-10-20T00:01:00Z", 2, (13, 3, 1, 6, 2, 19), (3, 1, 0, 2, 1, 5)),
            ]
            write_jsonl(root / "sessions" / "thread.jsonl", rows)
            write_jsonl(root / "archived_sessions" / "copy.jsonl", rows)

            result = collect([root], CUTOFF)

            self.assertEqual(len(result.events), 2)
            legacy_events = sorted(result.events, key=lambda event: event.timestamp or "")
            self.assertTrue(all(event.source_kind == "legacy_delta" for event in legacy_events))
            self.assertEqual([(e.fresh_input, e.cache_read, e.cache_write, e.output, e.reasoning) for e in legacy_events], [
                (7, 2, 1, 4, 1),
                (2, 1, 0, 2, 1),
            ])
            self.assertEqual(result.stats["legacy_duplicate_snapshots"], 2)
            first_receipt, second_receipt = (event.counter_receipt for event in legacy_events)
            for receipt in (first_receipt, second_receipt):
                self.assertEqual(set(receipt), {"schema", "source_refs", "counters", "baseline", "current", "last"})
                self.assertEqual(receipt["schema"], "codex.legacy_delta.v1")
                self.assertEqual(set(receipt["counters"]), set(FIELDS))
                self.assertEqual(set(receipt["current"]), set(FIELDS))
                self.assertEqual(set(receipt["last"]), set(FIELDS))
                for ref in receipt["source_refs"]:
                    self.assertEqual(set(ref), {"source", "kind", "index", "retry"})
                    self.assertRegex(ref["source"], r"^[a-f0-9]{64}$")
                    self.assertEqual(ref["kind"], "jsonl")
                    self.assertGreaterEqual(ref["index"], 0)
                    self.assertGreaterEqual(ref["retry"], 0)
            self.assertIsNone(first_receipt["baseline"])
            self.assertEqual(first_receipt["current"], counters((10, 2, 1, 4, 1, 14)))
            self.assertEqual(first_receipt["last"], counters((10, 2, 1, 4, 1, 14)))
            self.assertEqual(first_receipt["counters"], counters((10, 2, 1, 4, 1, 14)))
            self.assertEqual(second_receipt["baseline"], counters((10, 2, 1, 4, 1, 14)))
            self.assertEqual(second_receipt["current"], counters((13, 3, 1, 6, 2, 19)))
            self.assertEqual(second_receipt["last"], counters((3, 1, 0, 2, 1, 5)))
            self.assertEqual(second_receipt["counters"], counters((3, 1, 0, 2, 1, 5)))
            self.assertEqual(len(first_receipt["source_refs"]), 2)
            self.assertEqual(len(second_receipt["source_refs"]), 4)
            source_hashes = {
                hashlib.sha256(str(path.resolve()).encode()).hexdigest()
                for path in (root / "sessions" / "thread.jsonl", root / "archived_sessions" / "copy.jsonl")
            }
            self.assertEqual(
                {(ref["source"], ref["index"]) for ref in first_receipt["source_refs"]},
                {(source_hash, 1) for source_hash in source_hashes},
            )
            self.assertEqual(
                {(ref["source"], ref["index"]) for ref in second_receipt["source_refs"]},
                {(source_hash, index) for source_hash in source_hashes for index in (1, 2)},
            )
            self.assertEqual(legacy_events[0].session_key, hashlib.sha256(b"legacy-root").hexdigest())
            self.assertNotIn(str(root), repr(first_receipt))
            self.assertNotIn("legacy-root", repr(first_receipt))
            self.assertNotIn(str(root), repr(second_receipt))
            self.assertNotIn("legacy-root", repr(second_receipt))

    def test_child_inherited_first_snapshot_is_excluded_then_exact_delta_is_counted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inherited = (20, 4, 2, 8, 2, 28)
            first_turn = (3, 1, 0, 2, 1, 5)
            second_total = tuple(a + b for a, b in zip(inherited, first_turn))
            rows = [
                meta("child-thread", parent="parent-thread"),
                legacy("2026-10-20T00:00:00Z", 1, inherited, (2, 1, 0, 1, 1, 3)),
                legacy("2026-10-20T00:01:00Z", 2, second_total, first_turn),
            ]
            write_jsonl(root / "sessions" / "child.jsonl", rows)

            result = collect([root], CUTOFF)

            self.assertEqual(len(result.events), 1)
            self.assertEqual(result.events[0].source_kind, "legacy_delta")
            self.assertEqual(result.events[0].total, 5)
            self.assertEqual(result.stats["legacy_baseline_exclusions"], 1)

    def test_nested_subagent_spawn_parent_and_role_without_parent_exclude_first_legacy_baselines(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inherited = (20, 4, 2, 8, 2, 28)
            turn = (3, 1, 0, 2, 1, 5)
            rows = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "nested-child",
                        "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent-only-in-nested-source"}}},
                    },
                },
                legacy("2026-10-20T00:00:00Z", 1, inherited, (2, 1, 0, 1, 1, 3)),
                legacy("2026-10-20T00:01:00Z", 2, tuple(a + b for a, b in zip(inherited, turn)), turn),
            ]
            role_only = [
                {"type": "session_meta", "payload": {"id": "role-child", "agent_role": "worker"}},
                legacy("2026-10-20T00:00:00Z", 1, turn, turn),
            ]
            write_jsonl(root / "sessions" / "nested.jsonl", rows)
            write_jsonl(root / "sessions" / "role-only.jsonl", role_only)

            result = collect([root], CUTOFF)

            self.assertEqual(result.stats["threads_with_parent_metadata"], 1)
            self.assertEqual(result.stats["legacy_baseline_exclusions"], 2)
            self.assertEqual(len(result.events), 1)
            self.assertEqual(result.events[0].session_key, __import__("hashlib").sha256(b"nested-child").hexdigest())

    def test_missing_response_id_fallbacks_keep_independent_session_owners(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows_a = [meta("independent-a"), modern(None, thread_id="independent-a", values=usage())]
            rows_b = [meta("independent-b"), modern(None, thread_id="independent-b", values=usage())]
            write_jsonl(root / "sessions" / "a.jsonl", rows_a)
            write_jsonl(root / "profiles" / "switch" / "sessions" / "b.jsonl", rows_b)

            result = collect([root], CUTOFF)

            self.assertEqual(len(result.events), 2)
            self.assertEqual(len({event.session_key for event in result.events}), 2)
            self.assertEqual(result.stats["modern_duplicate_records"], 0)

    def test_legacy_mismatch_and_reset_are_diagnosed_without_bridging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [
                meta("legacy-root"),
                legacy("2026-10-20T00:00:00Z", 1, (10, 2, 1, 4, 1, 14), (10, 2, 1, 4, 1, 14)),
                # The stored last-turn vector does not match the cumulative delta.
                legacy("2026-10-20T00:01:00Z", 2, (13, 3, 1, 6, 2, 19), (2, 1, 0, 2, 1, 5)),
                # A decrease is a reset. Its counters are not bridged to the old baseline.
                legacy("2026-10-20T00:02:00Z", 3, (4, 1, 0, 2, 1, 6), (1, 0, 0, 1, 1, 3)),
                legacy("2026-10-20T00:03:00Z", 4, (6, 1, 0, 3, 1, 9), (2, 0, 0, 1, 0, 3)),
            ]
            write_jsonl(root / "sessions" / "thread.jsonl", rows)

            result = collect([root], CUTOFF)

            self.assertEqual(len(result.events), 2)
            self.assertEqual(result.stats["legacy_mismatches"], 1)
            self.assertEqual(result.stats["legacy_resets"], 1)

    def test_legacy_periods_outside_modern_overlap_are_kept_and_gap_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [
                meta("mixed-format-thread"),
                legacy("2026-10-20T00:00:00Z", 1, (10, 2, 1, 4, 1, 14), (10, 2, 1, 4, 1, 14)),
                legacy("2026-10-20T00:01:00Z", 2, (13, 3, 1, 6, 2, 19), (3, 1, 0, 2, 1, 5)),
                legacy("2026-10-20T00:02:00Z", 3, (17, 4, 1, 8, 3, 25), (4, 1, 0, 2, 1, 6)),
                modern("overlap-response", thread_id="mixed-format-thread", timestamp="2026-10-20T00:02:00Z", values=usage(4, 1, 0, 2, 1)),
                legacy("2026-10-20T00:03:00Z", 4, (19, 4, 1, 9, 3, 28), (2, 0, 0, 1, 0, 3)),
                legacy("2026-10-20T00:04:00Z", 5, (20, 4, 1, 11, 4, 31), (1, 0, 0, 2, 1, 3)),
            ]
            write_jsonl(root / "sessions" / "mixed.jsonl", rows)

            result = collect([root], CUTOFF)

            legacy_events = sorted((event for event in result.events if event.source_kind == "legacy_delta"), key=lambda event: event.timestamp or "")
            self.assertEqual(len(legacy_events), 2)
            self.assertEqual([event.timestamp for event in legacy_events], [
                "2026-10-20T00:00:00Z",
                "2026-10-20T00:01:00Z",
            ])
            self.assertEqual(result.stats["legacy_events_suppressed_modern_overlap"], 3)
            self.assertEqual(result.stats["legacy_exact_modern_duplicates"], 1)
            self.assertEqual(result.stats["legacy_events_suppressed_after_first_modern"], 2)
            self.assertEqual(result.stats["legacy_snapshots_suppressed"], 3)
            self.assertEqual(result.stats["legacy_suppressed_token_upper_bound"], 12)
            self.assertEqual(result.stats["legacy_threads_suppressed_by_modern"], 1)
            self.assertTrue(result.stats["incomplete"])
            self.assertIn("codex_legacy_modern_overlap", result.warnings)

    def test_unknown_modern_timestamp_suppresses_legacy_with_token_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [
                meta("unknown-modern-time"),
                legacy("2026-10-20T00:00:00Z", 1, (10, 2, 1, 4, 1, 14), (10, 2, 1, 4, 1, 14)),
                modern("undated-modern", thread_id="unknown-modern-time", timestamp=...),
            ]
            write_jsonl(root / "sessions" / "unknown.jsonl", rows)

            result = collect([root], CUTOFF)

            self.assertEqual([event.source_kind for event in result.events], ["recorded"])
            self.assertEqual(result.stats["legacy_events_suppressed_modern_overlap"], 1)
            self.assertEqual(result.stats["legacy_suppressed_token_upper_bound"], 14)
            self.assertEqual(result.stats["legacy_threads_suppressed_unknown_modern_time"], 1)
            self.assertIn("codex_legacy_modern_overlap", result.warnings)

    def test_future_and_unknown_timestamps_are_separated_and_unknown_modern_time_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [
                meta("thread-time"),
                modern("past-response", thread_id="thread-time", timestamp="2026-10-22T11:00:00Z"),
                modern("future-response", thread_id="thread-time", timestamp="2026-10-22T13:00:00Z"),
                modern("unknown-response", thread_id="thread-time", timestamp=...),
                legacy(..., 4, (10, 2, 1, 4, 1, 14), (10, 2, 1, 4, 1, 14)),
            ]
            write_jsonl(root / "sessions" / "thread.jsonl", rows)

            result = collect([root], CUTOFF)

            self.assertEqual(len(result.events), 2)
            self.assertEqual(sum(event.timestamp is None for event in result.events), 1)
            self.assertEqual(result.stats["future_records"], 1)
            self.assertEqual(result.stats["legacy_undated_snapshots"], 1)
            self.assertGreaterEqual(result.stats["undated_records"], 2)

    def test_cutoff_must_be_aware_and_known_root_discovery_is_bounded_and_deduplicated(self):
        with self.assertRaises(ValueError):
            collect([], datetime(2026, 10, 22))
        missing = collect([], CUTOFF)
        self.assertEqual(missing.stats["missing_sources"], 1)
        self.assertTrue(missing.stats["incomplete"])
        self.assertEqual(missing.stats["errors"], 0)
        self.assertIn("codex_missing_sources", missing.warnings)
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            users_dir = Path(temp) / "Users"
            codex = home / ".codex"
            app = home / "Library" / "Application Support" / "Codex"
            other_user_codex = users_dir / "other" / ".codex"
            custom = Path(temp) / "custom-codex"
            for path in (codex, app, other_user_codex, custom):
                path.mkdir(parents=True)
            with patch.dict(os.environ, {"CODEX_HOME": str(codex)}):
                roots = discover_roots(home, users_dir=users_dir)
            self.assertEqual(set(roots), {codex.resolve(), app.resolve(), other_user_codex.resolve()})

    def test_codex_home_accepts_deduplicated_comma_separated_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            home = base / "home"
            users_dir = base / "Users"
            first = base / "codex-one"
            second = base / "codex-two"
            for path in (first, second):
                path.mkdir()

            with patch.dict(os.environ, {"CODEX_HOME": f"{first}, {second},{first}"}):
                roots = discover_roots(home, users_dir=users_dir)

            self.assertEqual(roots, [first.resolve(), second.resolve()])


if __name__ == "__main__":
    unittest.main()
