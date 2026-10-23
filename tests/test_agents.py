from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tokenometer import agents
from tokenometer.models import digest


def stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def codex_spawn(child: str, parent: str | None, created_at: str | None):
    payload = {
        "id": child,
        "source": {"subagent": {"thread_spawn": {}}},
    }
    if parent is not None:
        payload["source"]["subagent"]["thread_spawn"]["parent_thread_id"] = parent
    row = {"type": "session_meta", "payload": payload}
    if created_at is not None:
        row["timestamp"] = created_at
    return row


def codex_parent_only(child: str, parent: str):
    return {"type": "session_meta", "payload": {"id": child, "parent_thread_id": parent}}


def write_rows(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def hashed(value: str, provider: str = "codex") -> str:
    return digest("antigravity-session-id:" + value) if provider == "antigravity" else digest(value)


def record(child: str, parent: str, *, provider: str = "codex", created_at="2026-10-20T00:00:00Z", source="a" * 64):
    result = {
        "session_id_sha256": hashed(child, provider),
        "parent_id_sha256": hashed(parent, provider),
        "created_at": created_at,
        "source_refs": [{"source": source, "kind": "jsonl" if provider == "codex" else "summary_index", "index": 1}],
    }
    if provider == "antigravity":
        result.update({"created_at": None, "depth": 1, "agent_name_present": True})
    return result


def envelope(status="complete", records=None, *, provider="codex", cutoff="2026-10-22T00:00:00Z", observed="2026-10-23T00:00:00Z", diagnostics=None):
    return {
        "version": 1,
        "cutoff": cutoff,
        "observed_at": observed,
        "providers": {
            provider: {
                "execution_status": status,
                "diagnostics": diagnostics or {},
                "records": records or [],
            }
        },
    }


def ledger_connection(testcase: unittest.TestCase):
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO ledger_meta(key,value) VALUES('schema_version','2')")
    connection.commit()
    testcase.addCleanup(connection.close)
    return connection


class AgentCollectionTests(unittest.TestCase):
    def test_codex_uses_only_typed_nested_spawn_metadata_and_deduplicates_copies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "codex"
            first = root / "sessions" / "one.jsonl"
            write_rows(first, [
                codex_spawn("child-a", "parent-a", "2026-10-20T00:00:00Z"),
                codex_parent_only("parent-only-child", "parent-only"),
                {"type": "session_meta", "payload": {
                    "id": "role-only-child", "source": {"subagent": {"role": "worker"}},
                }},
            ])
            copy = root / "archived_sessions" / "copy.jsonl"
            write_rows(copy, [codex_spawn("child-a", "parent-a", "2026-10-20T00:00:00Z")])

            result = agents.collect({"codex": [root]}, stamp("2026-10-22T00:00:00Z"))

        provider = result["providers"]["codex"]
        self.assertEqual(provider["execution_status"], "complete")
        self.assertEqual(len(provider["records"]), 1)
        self.assertEqual(provider["diagnostics"]["candidate_records"], 2)
        self.assertEqual(provider["diagnostics"]["duplicate_records"], 1)
        self.assertEqual(len(provider["records"][0]["source_refs"]), 2)
        self.assertNotIn("child-a", repr(result))
        self.assertNotIn(str(root), repr(result))

    def test_existing_roots_without_session_areas_are_scope_diagnostics_not_scan_failures(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            history = base / "codex-main"
            empty_without_area = base / "codex-app-support"
            empty_with_area = base / "codex-profile"
            empty_without_area.mkdir()
            (empty_with_area / "sessions").mkdir(parents=True)
            write_rows(history / "sessions" / "one.jsonl", [
                codex_spawn("one-child", "one-parent", "2026-10-20T00:00:00Z"),
            ])

            result = agents.collect(
                {"codex": [history, empty_without_area, empty_with_area]},
                stamp("2026-10-22T00:00:00Z"),
            )

        provider = result["providers"]["codex"]
        self.assertEqual(provider["execution_status"], "complete")
        self.assertEqual(len(provider["records"]), 1)
        self.assertEqual(provider["diagnostics"]["roots_missing"], 0)
        self.assertEqual(provider["diagnostics"]["missing_history_areas"], 1)

    def test_codex_cutoff_uses_datetime_precision_and_excludes_future_or_undated_children(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "codex"
            path = root / "sessions" / "events.jsonl"
            write_rows(path, [
                codex_spawn("before-edge", "parent", "2026-10-20T01:00:00.499999Z"),
                codex_spawn("at-edge", "parent", "2026-10-20T01:00:00.500000Z"),
                codex_spawn("after-edge", "parent", "2026-10-20T01:00:00.500001Z"),
                codex_spawn("undated", "parent", None),
            ])
            cutoff = stamp("2026-10-20T01:00:00.500000Z")

            result = agents.collect({"codex": [root]}, cutoff)

        provider = result["providers"]["codex"]
        self.assertEqual(provider["execution_status"], "partial")
        self.assertEqual(len(provider["records"]), 2)
        self.assertEqual(provider["diagnostics"]["future_records"], 1)
        self.assertEqual(provider["diagnostics"]["undated_child_records"], 1)
        self.assertEqual(result["cutoff"], "2026-10-20T01:00:00.500000Z")

    def test_codex_missing_parent_is_excluded_and_not_counted_as_a_child(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "codex"
            write_rows(root / "sessions" / "one.jsonl", [
                codex_spawn("orphan", None, "2026-10-20T00:00:00Z"),
            ])
            result = agents.collect({"codex": [root]}, stamp("2026-10-22T00:00:00Z"))
        self.assertEqual(result["providers"]["codex"]["records"], [])
        self.assertEqual(result["providers"]["codex"]["execution_status"], "partial")
        self.assertEqual(result["providers"]["codex"]["diagnostics"]["schema_errors"], 1)

    def test_antigravity_reads_only_summary_lineage_fields_and_deduplicates_conflicts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "agy"
            root.mkdir()
            db_path = root / "conversation_summaries.db"
            connection = sqlite3.connect(db_path)
            self.addCleanup(connection.close)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE conversation_summaries (
                conversation_id TEXT, parent_conversation_id TEXT, nesting_depth INTEGER,
                agent_name TEXT, title TEXT, preview TEXT
            )""")
            connection.executemany(
                "INSERT INTO conversation_summaries VALUES(?,?,?,?,?,?)",
                [
                    ("child-a", "parent-a", 1, "Planner", "private title", "private preview"),
                    ("child-a", "parent-a", 1, "Planner", "duplicate title", "duplicate preview"),
                    ("conflict", "parent-a", 1, "Worker", "secret title", "secret preview"),
                    ("conflict", "parent-b", 2, "Worker", "secret title", "secret preview"),
                    ("no-parent", None, 1, "Worker", "secret title", "secret preview"),
                    ("bad-depth", "parent-a", 0, "Worker", "secret title", "secret preview"),
                    ("no-name", "parent-a", 1, " ", "secret title", "secret preview"),
                ],
            )
            connection.commit()  # The source remains open in WAL mode during snapshot.
            result = agents.collect({"antigravity": [root]}, stamp("2026-10-22T00:00:00Z"))

        provider = result["providers"]["antigravity"]
        self.assertEqual(provider["execution_status"], "complete")
        self.assertEqual(len(provider["records"]), 3)  # two conflicting parent versions remain quarantinable
        self.assertEqual(provider["diagnostics"]["conflicting_parent_sessions"], 1)
        self.assertIsNone(provider["records"][0]["created_at"])
        serialized = repr(result)
        for private in ("private title", "duplicate title", "secret preview", "Planner", "Worker", str(root)):
            self.assertNotIn(private, serialized)


class AgentValidationAndLedgerTests(unittest.TestCase):
    def test_validator_rejects_raw_ids_bad_hashes_and_unhashable_status_without_leaking(self):
        valid = envelope(records=[record("child", "parent")])
        self.assertEqual(agents.validate(valid)["providers"]["codex"]["records"], valid["providers"]["codex"]["records"])
        for mutate in (
            lambda value: value["providers"]["codex"].update(execution_status={"complete": True}),
            lambda value: value["providers"]["codex"]["records"][0].update(session_id_sha256="raw-child"),
            lambda value: value.update(cutoff="2026-10-23T00:00:00.000000Z"),
        ):
            candidate = json.loads(json.dumps(valid))
            mutate(candidate)
            with self.assertRaises(ValueError):
                agents.validate(candidate)
        partial_without_dated_record = envelope(status="partial", records=[])
        self.assertEqual(agents.validate(partial_without_dated_record)["providers"]["codex"]["execution_status"], "partial")

    def test_empty_old_ledger_is_unobserved_but_complete_empty_scan_is_zero(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.assertIsNone(agents.snapshot(connection)["codex"]["count"])
        connection.execute("INSERT INTO ledger_meta(key,value) VALUES('schema_version','2')")
        agents.ingest(connection, "empty-run", envelope(records=[]))
        self.assertEqual(agents.snapshot(connection)["codex"]["count"], 0)
        self.assertEqual(agents.snapshot(connection)["codex"]["execution_status"], "complete")

    def test_partial_rows_become_countable_only_when_seen_by_a_complete_run(self):
        connection = ledger_connection(self)
        first_record = record("seen-in-good-run", "root")
        partial_only = record("partial-only", "root")
        agents.ingest(connection, "partial", envelope(
            "partial", [first_record, partial_only], observed="2026-10-23T01:00:00Z",
        ))
        self.assertIsNone(agents.snapshot(connection)["codex"]["count"])
        agents.ingest(connection, "complete", envelope(
            "complete", [first_record], observed="2026-10-23T02:00:00Z",
        ))
        result = agents.snapshot(connection)["codex"]
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["execution_status"], "complete")

    def test_later_partial_and_unobserved_attempts_preserve_last_good_count_and_freshness(self):
        connection = ledger_connection(self)
        good = record("good", "root")
        agents.ingest(connection, "good", envelope(records=[good], observed="2026-10-23T02:00:00Z"))
        partial_record = record("partial-only", "root")
        agents.ingest(connection, "partial", envelope(
            "partial", [partial_record], observed="2026-10-23T02:00:00.000001Z",
        ))
        partial = agents.snapshot(connection)["codex"]
        self.assertEqual(partial["count"], 1)
        self.assertEqual(partial["execution_status"], "partial")
        self.assertEqual(partial["observed_at"], "2026-10-23T02:00:00Z")
        agents.ingest(connection, "unobserved", envelope(
            "unobserved", [], observed="2026-10-23T04:00:00Z",
        ))
        unobserved = agents.snapshot(connection)["codex"]
        self.assertEqual(unobserved["count"], 1)
        self.assertEqual(unobserved["execution_status"], "partial")
        self.assertEqual(unobserved["observed_at"], "2026-10-23T02:00:00Z")

    def test_replay_is_idempotent_and_older_complete_snapshot_cannot_regress_freshness(self):
        connection = ledger_connection(self)
        older = envelope(records=[record("a", "root")], cutoff="2026-10-20T00:00:00Z", observed="2026-10-20T00:00:00Z")
        newer = envelope(records=[record("b", "root")], cutoff="2026-10-22T00:00:00Z", observed="2026-10-22T00:00:00Z")
        agents.ingest(connection, "newer", newer)
        agents.ingest(connection, "older", older)
        agents.ingest(connection, "older", older)
        result = agents.snapshot(connection)["codex"]
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["cutoff"], "2026-10-22T00:00:00Z")
        self.assertEqual(result["observed_at"], "2026-10-22T00:00:00Z")

    def test_conflicting_parents_are_excluded_and_reported(self):
        connection = ledger_connection(self)
        first = record("same-child", "parent-a", source="a" * 64)
        second = record("same-child", "parent-b", source="b" * 64)
        agents.ingest(connection, "conflict", envelope(records=[first, second]))
        result = agents.snapshot(connection)["codex"]
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["conflicted_sessions"], 1)

    def test_unselected_provider_is_not_created_and_ingest_keeps_callers_transaction(self):
        connection = ledger_connection(self)
        value = envelope(records=[record("child", "parent")])
        connection.execute("BEGIN")
        agents.ingest(connection, "transactional", value)
        self.assertTrue(connection.in_transaction)
        result = agents.snapshot(connection)
        self.assertEqual(result["codex"]["count"], 1)
        self.assertIsNone(result["antigravity"]["count"])
        self.assertEqual(result["antigravity"]["execution_status"], "unobserved")
        connection.rollback()


if __name__ == "__main__":
    unittest.main()
