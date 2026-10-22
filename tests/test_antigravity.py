from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tokenometer.aggregate import summarize
from tokenometer.capture import canonicalize
from tokenometer.collectors.antigravity import collect, discover_roots
from tokenometer.receipts import validate_receipt


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _field_varint(number: int, value: int) -> bytes:
    return _varint((number << 3) | 0) + _varint(value)


def _field_bytes(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _timestamp(seconds: int, nanos: int = 0) -> bytes:
    return _field_varint(1, seconds) + _field_varint(2, nanos)


def _usage(
    *,
    fresh: int = 10,
    output: int = 20,
    cache_write: int | None = 2,
    cache_read: int | None = 3,
    reasoning: int = 5,
    visible: int = 15,
    model_enum: int = 246,
    message_id: str = "message-1",
    response_id: str = "response-1",
    provider_id: str | None = None,
    unknown_field: bool = False,
    model_label: str | None = None,
) -> bytes:
    result = bytearray()
    # #1 and #6 are enums, never token counters.
    result += _field_varint(1, model_enum)
    result += _field_varint(2, fresh)
    result += _field_varint(3, output)
    if cache_write is not None:
        result += _field_varint(4, cache_write)
    if cache_read is not None:
        result += _field_varint(5, cache_read)
    result += _field_varint(6, 4)
    result += _field_bytes(7, message_id.encode())
    result += _field_varint(9, reasoning)
    result += _field_varint(10, visible)
    result += _field_bytes(11, response_id.encode())
    if provider_id:
        result += _field_bytes(12, provider_id.encode())
    if unknown_field:
        result += _field_varint(99, 37)
    if model_label:
        result += _field_bytes(19, model_label.encode())
    return bytes(result)


def _retry(usage: bytes, field_number: int = 2) -> bytes:
    retry_info = _field_bytes(field_number, usage)
    return retry_info


def _step_metadata(usage: bytes, *, seconds: int | None = 1_750_000_000, retries=()) -> bytes:
    result = bytearray()
    if seconds is not None:
        stamp = _timestamp(seconds)
        result += _field_bytes(1, stamp)
        result += _field_bytes(8, stamp)
    result += _field_bytes(9, usage)
    for retry in retries:
        result += _field_bytes(28, retry)
    return bytes(result)


def _generation_metadata(usage: bytes, *, seconds: int | None = 1_750_000_000, retries=()) -> bytes:
    chat = bytearray()
    chat += _field_bytes(4, usage)
    if seconds is not None:
        generation_info = _field_bytes(4, _timestamp(seconds))
        chat += _field_bytes(9, generation_info)
    for retry in retries:
        chat += _field_bytes(17, retry)
    return _field_bytes(1, bytes(chat))


def _make_summary(root: Path, conversation_ids=()) -> None:
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / "conversation_summaries.db")
    connection.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT)")
    connection.executemany(
        "INSERT INTO conversation_summaries(conversation_id, title) VALUES(?, ?)",
        [(conversation_id, "private title must never be selected") for conversation_id in conversation_ids],
    )
    connection.commit()
    connection.close()


def _make_trajectory(
    root: Path,
    conversation_id: str,
    *,
    steps=(),
    generations=(),
    wal: bool = False,
    trajectory_timestamp: int | None = None,
) -> Path:
    directory = root / "conversations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{conversation_id}.db"
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE steps(idx INTEGER, metadata BLOB)")
    connection.execute("CREATE TABLE gen_metadata(idx INTEGER, data BLOB, size INTEGER)")
    connection.executemany("INSERT INTO steps(idx, metadata) VALUES(?, ?)", steps)
    connection.executemany(
        "INSERT INTO gen_metadata(idx, data, size) VALUES(?, ?, ?)",
        [(index, data, len(data)) for index, data in generations],
    )
    if trajectory_timestamp is not None:
        connection.execute("CREATE TABLE trajectory_metadata_blob(id INTEGER, data BLOB)")
        connection.execute(
            "INSERT INTO trajectory_metadata_blob(id, data) VALUES(?, ?)",
            (1, _field_bytes(2, _timestamp(trajectory_timestamp))),
        )
    connection.commit()
    return connection


class AntigravityCollectorTests(unittest.TestCase):
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_multi_store_copies_and_identity_bridge_merge_maxima(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            main_step = _usage(
                fresh=10,
                output=20,
                cache_write=4,
                cache_read=5,
                reasoning=5,
                visible=15,
                message_id="main-step-message",
                response_id="main-step-response",
                provider_id="bridge-main",
            )
            main_generation = _usage(
                fresh=12,
                output=23,
                cache_write=6,
                cache_read=7,
                reasoning=6,
                visible=17,
                message_id="main-generation-message",
                response_id="main-generation-response",
                provider_id="bridge-main",
            )
            retry_step = _usage(
                fresh=2,
                output=4,
                cache_write=0,
                cache_read=1,
                reasoning=1,
                visible=3,
                message_id="retry-step-message",
                response_id="bridge-retry",
            )
            retry_generation = _usage(
                fresh=3,
                output=6,
                cache_write=2,
                cache_read=2,
                reasoning=2,
                visible=4,
                message_id="retry-generation-message",
                response_id="bridge-retry",
            )

            roots = []
            for store in ("antigravity", "antigravity-cli"):
                root = base / store
                _make_summary(root, ("shared-conversation",))
                _make_trajectory(
                    root,
                    "shared-conversation",
                    steps=[(1, _step_metadata(main_step, retries=(_retry(retry_step),)))],
                    generations=[
                        (
                            1,
                            _generation_metadata(
                                main_generation,
                                retries=(_retry(retry_generation),),
                            ),
                        )
                    ],
                ).close()
                roots.append(root)

            result = collect(roots, self.cutoff)
            self.assertEqual(len(result.events), 2)
            main = next(event for event in result.events if event.fresh_input == 12)
            retry = next(event for event in result.events if event.fresh_input == 3)
            self.assertEqual(
                (main.fresh_input, main.cache_read, main.cache_write, main.output, main.reasoning),
                (12, 7, 6, 23, 6),
            )
            self.assertEqual(
                (retry.fresh_input, retry.cache_read, retry.cache_write, retry.output, retry.reasoning),
                (3, 2, 2, 6, 2),
            )
            self.assertEqual(result.stats["duplicate_events"], 6)
            self.assertEqual(result.stats["conflicting_identity_groups"], 0)
            self.assertTrue(all(event.key.startswith("antigravity:") for event in result.events))
            self.assertEqual(len({event.session_key for event in result.events}), 1)
            expected_sources = {
                hashlib.sha256(str((root / "conversations" / "shared-conversation.db").resolve()).encode()).hexdigest()
                for root in roots
            }
            for event in (main, retry):
                receipt = event.counter_receipt
                validate_receipt(event)
                self.assertEqual(receipt["schema"], "antigravity.protobuf.v1")
                self.assertEqual(
                    set(receipt),
                    {"schema", "source_refs", "observations", "source_timestamps", "timestamp_basis", "model_enum_ids", "provider_enum_ids", "field_presence"},
                )
                self.assertEqual(len(receipt["observations"]), 4)
                self.assertEqual(len(receipt["source_refs"]), 4)
                self.assertTrue(all(len(reference["source"]) == 64 for reference in receipt["source_refs"]))
                self.assertEqual({reference["source"] for reference in receipt["source_refs"]}, expected_sources)
                self.assertTrue(all(set(reference) == {"source", "kind", "index", "retry"} for reference in receipt["source_refs"]))
                self.assertTrue(
                    all(
                        set(observation)
                        == {"input", "cache_creation", "cache_read", "output_total", "visible_output", "reasoning"}
                        for observation in receipt["observations"]
                    )
                )
                self.assertTrue(all(reference["index"] >= 0 for reference in receipt["source_refs"]))
                self.assertTrue(all(reference["retry"] == (1 if event is retry else 0) for reference in receipt["source_refs"]))
                self.assertEqual(receipt["model_enum_ids"], [246])
                self.assertEqual(receipt["provider_enum_ids"], [4])

    def test_global_identity_deduplicates_equal_copies_but_excludes_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            roots = []
            for store, conversation_id, response_id, fresh in (
                ("antigravity", "copy-a", "same-response", 10),
                ("antigravity-cli", "copy-b", "same-response", 10),
                ("antigravity-ide", "conflict-a", "conflict-response", 20),
                ("antigravity-backup", "conflict-b", "conflict-response", 99),
            ):
                root = base / store
                _make_trajectory(
                    root,
                    conversation_id,
                    steps=[
                        (
                            1,
                            _step_metadata(
                                _usage(
                                    fresh=fresh,
                                    response_id=response_id,
                                    message_id=f"message-{conversation_id}",
                                ),
                            ),
                        )
                    ],
                ).close()
                roots.append(root)
            result = collect(roots, self.cutoff)
            self.assertEqual(len(result.events), 1)
            self.assertEqual(result.events[0].fresh_input, 10)
            event = result.events[0]
            validate_receipt(event)
            expected_aliases = {
                hashlib.sha256(
                    ("antigravity-session:" + hashlib.sha256(
                        ("antigravity-session-id:" + conversation_id).encode()
                    ).hexdigest()).encode()
                ).hexdigest()
                for conversation_id in ("copy-a", "copy-b")
            }
            self.assertEqual(set(event.session_aliases), expected_aliases)
            self.assertEqual(event.session_key, min(expected_aliases))
            for alias in expected_aliases:
                accepted, counters = canonicalize([event], self.cutoff, excluded_sessions=[alias])
                self.assertEqual(accepted, [])
                self.assertEqual(counters["sessions_excluded"], 1)
            self.assertEqual(result.stats["conflicting_identity_groups"], 1)
            self.assertEqual(result.stats["conflicting_events_excluded"], 2)
            self.assertEqual(result.stats["duplicate_events"], 1)
            self.assertNotIn("conflict-response", json.dumps(result.stats))
            self.assertNotIn("conflict-response", " ".join(result.warnings))

    def test_distinct_source_timestamps_keep_identity_undated(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first_root, second_root = base / "first", base / "second"
            first_time = int(datetime(2025, 12, 20, 10, tzinfo=timezone.utc).timestamp())
            second_time = int(datetime(2025, 12, 21, 10, tzinfo=timezone.utc).timestamp())
            _make_trajectory(
                first_root,
                "copy-one",
                steps=[(
                    1,
                    _step_metadata(
                        _usage(fresh=14, response_id="timestamp-shared", message_id="same-message"),
                        seconds=first_time,
                    ),
                )],
            ).close()
            _make_trajectory(
                second_root,
                "copy-two",
                generations=[(
                    1,
                    _generation_metadata(
                        _usage(fresh=14, response_id="timestamp-shared", message_id="same-message"),
                        seconds=second_time,
                    ),
                )],
            ).close()

            result = collect([first_root, second_root], self.cutoff)

            self.assertEqual(len(result.events), 1)
            event = result.events[0]
            self.assertIsNone(event.timestamp)
            self.assertEqual(result.stats["timestamp_conflict_events"], 1)
            self.assertIn("TIMESTAMP_CONFLICT:1", result.warnings)
            expected_times = {
                datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                for value in (first_time, second_time)
            }
            self.assertEqual(set(event.counter_receipt["source_timestamps"]), expected_times)
            validate_receipt(event)
            totals = summarize(result.events, "UTC")
            self.assertEqual(totals["daily"], [])
            self.assertEqual(totals["dated_totals"]["total"], 0)
            self.assertEqual(totals["undated_totals"]["total"], event.total)

    def test_empty_database_and_missing_root_are_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            empty_root = base / "antigravity"
            _make_summary(empty_root)
            missing_root = base / "missing-private-root"
            result = collect([empty_root, missing_root], self.cutoff)
            self.assertEqual(result.events, [])
            self.assertEqual(result.stats["errors"], 0)
            self.assertEqual(result.stats["source_inventory"]["indexed_conversations"], 0)
            self.assertEqual(result.stats["missing_sources"], ["configured_root_2"])
            self.assertNotIn(str(missing_root), json.dumps(result.stats))

    def test_non_sqlite_database_candidate_is_visible_without_a_read_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "antigravity"
            conversations = root / "conversations"
            conversations.mkdir(parents=True)
            (conversations / "unreadable-private-session.db").write_bytes(b"not a sqlite database")

            result = collect([root], self.cutoff)

            self.assertEqual(result.stats["errors"], 0)
            self.assertEqual(result.stats["source_inventory"]["non_sqlite_candidates"], 1)
            self.assertEqual(result.stats["warning_counts"]["NON_SQLITE_CANDIDATE"], 1)
            self.assertEqual(result.stats["failure_diagnostics"], [])
            serialized = json.dumps({"stats": result.stats, "warnings": result.warnings})
            self.assertNotIn("unreadable-private-session", serialized)
            self.assertNotIn(str(root), serialized)

    def test_candidate_inventory_separates_non_sqlite_and_empty_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "antigravity"
            root.mkdir()
            (root / "application-cache.db").write_bytes(b"not a sqlite store")
            (root / "new-empty.db").touch()

            result = collect([root], self.cutoff)

            inventory = result.stats["source_inventory"]
            self.assertEqual(inventory["sqlite_candidates"], 2)
            self.assertEqual(inventory["non_sqlite_candidates"], 1)
            self.assertEqual(inventory["empty_candidates"], 1)
            self.assertEqual(inventory["candidate_header:NON_SQLITE"], 1)
            self.assertEqual(inventory["candidate_header:EMPTY"], 1)
            self.assertEqual(result.stats["errors"], 0)
            self.assertEqual(result.stats["warning_counts"]["NON_SQLITE_CANDIDATE"], 1)
            self.assertEqual(result.stats["failure_diagnostics"], [])

    def test_recognized_sqlite_snapshot_failure_has_hashed_header_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "antigravity"
            root.mkdir()
            recognized = sqlite3.connect(root / "recognized-usage.db")
            recognized.execute("CREATE TABLE steps (idx INTEGER, metadata BLOB)")
            recognized.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
            recognized.commit()
            recognized.close()

            with patch(
                "tokenometer.collectors.antigravity._readonly_snapshot",
                side_effect=TimeoutError("private path must not escape"),
            ):
                result = collect([root], self.cutoff)

            self.assertEqual(result.stats["errors"], 1)
            inventory = result.stats["source_inventory"]
            self.assertEqual(inventory["sqlite_header_candidates"], 1)
            self.assertEqual(inventory.get("recovered_nonusage_timeout", 0), 0)
            diagnostic, = result.stats["failure_diagnostics"]
            self.assertEqual(diagnostic["kind"], "sqlite_candidate")
            self.assertEqual(diagnostic["header_class"], "SQLITE")
            self.assertEqual(diagnostic["error_class"], "SNAPSHOT_TIMEOUT")
            self.assertEqual(diagnostic["copy_probe_status"], "USAGE_SCHEMA_PRESENT")
            self.assertGreater(diagnostic["byte_size"], 0)
            self.assertRegex(diagnostic["source_ref"], r"^[0-9a-f]{64}$")
            serialized = json.dumps({"stats": result.stats, "warnings": result.warnings})
            self.assertNotIn("recognized.db", serialized)
            self.assertNotIn(str(root), serialized)

    def test_timeout_copy_can_classify_small_unrecognized_sqlite_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "antigravity"
            root.mkdir()
            connection = sqlite3.connect(root / "arbitrary-app-store.db")
            connection.execute("CREATE TABLE cache_items (key TEXT, value BLOB)")
            connection.execute("CREATE TABLE settings (name TEXT, value TEXT)")
            connection.commit()
            connection.close()

            with patch(
                "tokenometer.collectors.antigravity._readonly_snapshot",
                side_effect=TimeoutError("private path must not escape"),
            ):
                result = collect([root], self.cutoff)

            inventory = result.stats["source_inventory"]
            self.assertEqual(result.stats["errors"], 0)
            self.assertEqual(inventory["recovered_nonusage_timeout"], 1)
            self.assertEqual(inventory["unrecognized_databases"], 1)
            self.assertEqual(inventory["databases_read"], 1)
            diagnostic, = result.stats["failure_diagnostics"]
            self.assertEqual(diagnostic["error_class"], "SNAPSHOT_TIMEOUT")
            self.assertEqual(diagnostic["classification"], "RECOVERED_NONUSAGE")
            self.assertEqual(diagnostic["source_ref"], hashlib.sha256(
                str((root / "arbitrary-app-store.db").resolve()).encode()
            ).hexdigest())
            serialized = json.dumps({"stats": result.stats, "warnings": result.warnings})
            self.assertNotIn("arbitrary-app-store", serialized)
            self.assertNotIn(str(root), serialized)

    def test_timeout_copy_rejects_data_bearing_sqlite_sidecar(self):
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(sidecar=suffix), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "antigravity"
                root.mkdir()
                database = root / "app-cache.db"
                connection = sqlite3.connect(database)
                connection.execute("CREATE TABLE cache_items (key TEXT, value BLOB)")
                connection.commit()
                connection.close()
                Path(str(database) + suffix).write_bytes(b"sidecar-data")

                with patch(
                    "tokenometer.collectors.antigravity._readonly_snapshot",
                    side_effect=TimeoutError("private path must not escape"),
                ):
                    result = collect([root], self.cutoff)

                self.assertEqual(result.stats["errors"], 1)
                diagnostic, = result.stats["failure_diagnostics"]
                self.assertEqual(diagnostic["error_class"], "SNAPSHOT_TIMEOUT")
                self.assertEqual(diagnostic["copy_probe_status"], "DATA_SIDECAR_PRESENT")
                self.assertEqual(result.stats["source_inventory"].get("recovered_nonusage_timeout", 0), 0)

    def test_timeout_copy_rejects_source_changed_during_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "antigravity"
            root.mkdir()
            database = root / "app-cache.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE cache_items (key TEXT, value BLOB)")
            connection.commit()
            connection.close()

            import tokenometer.collectors.antigravity as antigravity

            real_copyfile = antigravity.shutil.copyfile

            def copy_then_change(source, destination, *args, **kwargs):
                result = real_copyfile(source, destination, *args, **kwargs)
                if Path(source).resolve() == database.resolve():
                    with database.open("ab") as changed:
                        changed.write(b"x")
                return result

            with (
                patch(
                    "tokenometer.collectors.antigravity._readonly_snapshot",
                    side_effect=TimeoutError("private path must not escape"),
                ),
                patch("tokenometer.collectors.antigravity.shutil.copyfile", side_effect=copy_then_change),
            ):
                result = collect([root], self.cutoff)

            self.assertEqual(result.stats["errors"], 1)
            diagnostic, = result.stats["failure_diagnostics"]
            self.assertEqual(diagnostic["error_class"], "SNAPSHOT_TIMEOUT")
            self.assertEqual(diagnostic["copy_probe_status"], "SOURCE_CHANGED_OR_COPY_MISMATCH")
            self.assertEqual(result.stats["source_inventory"].get("recovered_nonusage_timeout", 0), 0)

    def test_snapshot_deadline_is_configurable_and_capped(self):
        with patch.dict(os.environ, {"TOKENOMETER_ANTIGRAVITY_SNAPSHOT_TIMEOUT_SECONDS": "12.5"}):
            configured = collect([], self.cutoff)
        with patch.dict(os.environ, {"TOKENOMETER_ANTIGRAVITY_SNAPSHOT_TIMEOUT_SECONDS": "999"}):
            capped_high = collect([], self.cutoff)
        with patch.dict(os.environ, {"TOKENOMETER_ANTIGRAVITY_SNAPSHOT_TIMEOUT_SECONDS": "0"}):
            capped_low = collect([], self.cutoff)

        self.assertEqual(configured.stats["snapshot_timeout_seconds"], 12.5)
        self.assertEqual(capped_high.stats["snapshot_timeout_seconds"], 30.0)
        self.assertEqual(capped_low.stats["snapshot_timeout_seconds"], 0.5)

    def test_cutoff_excludes_future_before_identity_dedup_and_keeps_undated(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            past_root, future_root, undated_root = (base / name for name in ("past", "future", "undated"))
            _make_trajectory(
                past_root,
                "past-session",
                steps=[
                    (
                        1,
                        _step_metadata(
                            _usage(fresh=10, response_id="cutoff-shared", message_id="past-message"),
                            seconds=1_700_000_000,
                        ),
                    )
                ],
            ).close()
            _make_trajectory(
                future_root,
                "future-session",
                steps=[
                    (
                        1,
                        _step_metadata(
                            _usage(fresh=99, response_id="cutoff-shared", message_id="future-message"),
                            seconds=1_800_000_000,
                        ),
                    )
                ],
            ).close()
            _make_trajectory(
                undated_root,
                "undated-session",
                steps=[
                    (
                        1,
                        _step_metadata(
                            _usage(response_id="undated-response", message_id="undated-message"),
                            seconds=None,
                        ),
                    )
                ],
            ).close()

            result = collect([past_root, future_root, undated_root], self.cutoff)
            self.assertEqual(len(result.events), 2)
            self.assertEqual(sorted(event.fresh_input for event in result.events), [10, 10])
            self.assertEqual(sum(event.timestamp is None for event in result.events), 1)
            self.assertEqual(result.stats["future_events"], 1)
            self.assertEqual(result.stats["undated_events"], 1)
            self.assertEqual(result.stats["conflicting_identity_groups"], 0)
            self.assertNotIn("event_diagnostics", result.stats)

    def test_trajectory_session_timestamp_is_only_a_fallback_availability_signal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "antigravity"
            _make_trajectory(
                root,
                "session-only-time",
                steps=[
                    (
                        1,
                        _step_metadata(
                            _usage(response_id="session-time-response"),
                            seconds=None,
                        ),
                    )
                ],
                trajectory_timestamp=1_750_000_000,
            ).close()

            result = collect([root], self.cutoff)

            self.assertEqual(len(result.events), 1)
            self.assertIsNone(result.events[0].timestamp)
            self.assertEqual(result.stats["undated_events"], 1)
            self.assertEqual(result.stats["timestamp_fallback_available_events"], 1)
            receipt = result.events[0].counter_receipt
            self.assertEqual(receipt["timestamp_basis"], "unknown")
            self.assertEqual(receipt["schema"], "antigravity.protobuf.v1")
            self.assertEqual(receipt["observations"][0]["input"], 10)

    def test_wal_snapshot_is_readonly_and_cache_argument_never_stales(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "antigravity"
            writer = _make_trajectory(
                root,
                "wal-session",
                steps=[(1, _step_metadata(_usage(fresh=10, response_id="wal-response")))],
                wal=True,
            )
            db_path = root / "conversations" / "wal-session.db"
            wal_path = Path(str(db_path) + "-wal")
            self.assertTrue(wal_path.exists())
            before_main = (db_path.stat().st_mtime_ns, hashlib.sha256(db_path.read_bytes()).hexdigest())
            before_wal_size = wal_path.stat().st_size
            cache = base / "private-cache.sqlite"

            first = collect([root], self.cutoff, cache=cache)
            self.assertEqual(first.events[0].fresh_input, 10)
            self.assertFalse(cache.exists())
            writer.execute(
                "UPDATE steps SET metadata=? WHERE idx=1",
                (_step_metadata(_usage(fresh=15, response_id="wal-response")),),
            )
            writer.commit()
            second = collect([root], self.cutoff, cache=cache)

            self.assertEqual(second.events[0].fresh_input, 15)
            self.assertEqual(
                (db_path.stat().st_mtime_ns, hashlib.sha256(db_path.read_bytes()).hexdigest()),
                before_main,
            )
            self.assertTrue(wal_path.exists())
            self.assertGreaterEqual(wal_path.stat().st_size, before_wal_size)
            self.assertEqual(second.stats["cache_used"], 0)
            self.assertEqual(second.stats["cache_ignored"], 1)
            writer.close()

    def test_enum_defaults_unknown_fields_and_model_allowlist_are_reported_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "antigravity"
            usage = _usage(
                fresh=7,
                output=20,
                cache_write=None,
                cache_read=0,
                reasoning=5,
                visible=15,
                model_enum=99999,
                response_id="private-response-id",
                unknown_field=True,
                model_label="private-unreviewed-model-name",
            )
            _make_trajectory(
                root,
                "private-conversation-id",
                steps=[(1, _step_metadata(usage))],
            ).close()

            result = collect([root], self.cutoff)
            event = result.events[0]
            self.assertEqual((event.fresh_input, event.cache_read, event.cache_write), (7, 0, 0))
            self.assertEqual(event.output, 20)
            self.assertEqual(event.reasoning, 5)
            self.assertIsNone(event.model)
            self.assertGreaterEqual(result.stats["defaulted_counter_fields"], 1)
            self.assertGreaterEqual(result.stats["unknown_proto_fields"], 1)
            receipt = event.counter_receipt
            self.assertEqual(receipt["schema"], "antigravity.protobuf.v1")
            self.assertEqual(receipt["observations"][0]["output_total"], 20)
            self.assertEqual(receipt["observations"][0]["reasoning"], 5)
            self.assertIsNone(receipt["observations"][0]["cache_creation"])
            self.assertEqual(receipt["field_presence"]["cache_creation"], 0)
            self.assertEqual(receipt["model_enum_ids"], [99999])
            self.assertEqual(receipt["provider_enum_ids"], [4])
            serialized = json.dumps({"events": [event.to_dict()], "stats": result.stats, "warnings": result.warnings})
            for secretish in ("private-response-id", "private-conversation-id", "private-unreviewed-model-name"):
                self.assertNotIn(secretish, serialized)
            self.assertTrue(all(warning.isascii() for warning in result.warnings))

    def test_malformed_protobuf_and_schema_drift_are_counted_without_raw_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "antigravity"
            connection = _make_trajectory(
                root,
                "malformed-session",
                steps=[
                    (1, b"\x00"),
                    # #2 is a token field, so a length-delimited value is schema drift.
                    (
                        2,
                        _step_metadata(
                            _field_bytes(2, b"not-a-varint")
                            + _field_varint(3, 20)
                            + _field_bytes(11, b"wrong-wire-response")
                        ),
                    ),
                    (3, _step_metadata(_usage(response_id="valid-response"))),
                ],
            )
            connection.execute("CREATE TABLE transcript(secret TEXT)")
            connection.execute("INSERT INTO transcript(secret) VALUES('never read')")
            connection.commit()
            connection.close()

            result = collect([root], self.cutoff)
            self.assertEqual(len(result.events), 1)
            self.assertEqual(result.stats["malformed_records"], 2)
            self.assertEqual(result.stats["errors"], 2)
            self.assertEqual(result.stats["warning_counts"]["MALFORMED_PROTO_RECORD"], 2)
            serialized = json.dumps({"stats": result.stats, "warnings": result.warnings})
            self.assertNotIn("never read", serialized)
            self.assertNotIn("transcript", serialized)
            diagnostics = result.stats["failure_diagnostics"]
            self.assertEqual(
                {item["error_code"] for item in diagnostics},
                {"INVALID_FIELD_NUMBER", "USAGE_COUNTER_WRONG_WIRE_TYPE"},
            )
            self.assertEqual({item["kind"] for item in diagnostics}, {"steps"})
            self.assertTrue(all(item["byte_size"] > 0 for item in diagnostics))
            self.assertTrue(all(len(item["source_ref"]) == 64 for item in diagnostics))

    def test_large_valid_generation_record_above_previous_limit_is_collected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "antigravity"
            usage = _usage(
                fresh=23,
                output=31,
                cache_write=7,
                cache_read=5,
                response_id="large-record-response",
            )
            chat = (
                _field_bytes(4, usage)
                + _field_bytes(9, _field_bytes(4, _timestamp(1_750_000_000)))
                + _field_bytes(99, b"x" * (4 * 1024 * 1024 + 1))
            )
            generation = _field_bytes(1, chat)
            self.assertGreater(len(generation), 4 * 1024 * 1024)
            _make_trajectory(root, "large-record-session", generations=[(1, generation)]).close()

            result = collect([root], self.cutoff)

            self.assertEqual(len(result.events), 1)
            event = result.events[0]
            self.assertEqual(event.fresh_input, 23)
            self.assertEqual(event.cache_read, 5)
            self.assertEqual(event.cache_write, 7)
            self.assertEqual(event.output, 31)
            self.assertEqual(result.stats["malformed_records"], 0)
            validate_receipt(event)

    def test_discovery_deduplicates_profiles_and_excludes_browser_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / ".gemini" / "antigravity").mkdir(parents=True)
            (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
            (home / ".gemini" / "antigravity-browser-profile").mkdir(parents=True)
            (home / ".gemini" / "profiles" / "profile-a" / "antigravity").mkdir(parents=True)
            (home / ".gemini" / "accounts" / "account-b" / ".gemini" / "antigravity-cli").mkdir(parents=True)
            app_root = home / "Library" / "Application Support" / "Antigravity"
            (app_root / "User" / "globalStorage").mkdir(parents=True)
            (app_root / "User" / "profiles" / "profile-b" / "globalStorage").mkdir(parents=True)

            roots = discover_roots(home)
            resolved = [path.resolve() for path in roots]
            self.assertEqual(len(resolved), len(set(resolved)))
            self.assertIn((home / ".gemini" / "antigravity").resolve(), resolved)
            self.assertIn((home / ".gemini" / "antigravity-cli").resolve(), resolved)
            self.assertIn((home / ".gemini" / "profiles" / "profile-a" / "antigravity").resolve(), resolved)
            self.assertIn((home / ".gemini" / "accounts" / "account-b" / ".gemini" / "antigravity-cli").resolve(), resolved)
            self.assertNotIn((home / ".gemini" / "antigravity-browser-profile").resolve(), resolved)
            self.assertIn((app_root / "User" / "profiles" / "profile-b" / "globalStorage").resolve(), resolved)


if __name__ == "__main__":
    unittest.main()
