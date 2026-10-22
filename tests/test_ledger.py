import hashlib
import json
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from tokenometer.capture import persist_run, verify_run
from tokenometer.ledger import ingest_runs, ledger_snapshot
from tokenometer.models import CollectionResult, UsageEvent, digest


CUTOFF = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


def event(name="event", *, timestamp="2026-10-21T23:30:00Z", session=None,
          fresh_input=10, cache_read=100, cache_write=2, output=8,
          reasoning=3, model="model-a", aliases=None, platform="codex"):
    key = "codex:" + digest(name)
    session_key = session or digest("session-" + name)
    input_tokens = fresh_input + cache_read + cache_write
    total_tokens = input_tokens + output
    receipt = {
        "schema": "codex.response.v1",
        "source_refs": [{"source": digest("source-" + name), "kind": "jsonl", "index": 1, "retry": 0}],
        "counters": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cache_read,
            "cache_write_input_tokens": cache_write,
            "output_tokens": output,
            "reasoning_output_tokens": reasoning,
            "total_tokens": total_tokens,
        },
    }
    return UsageEvent(
        key=key, platform=platform, session_key=session_key, timestamp=timestamp,
        fresh_input=fresh_input, cache_read=cache_read, cache_write=cache_write,
        output=output, reasoning=reasoning, model=model, counter_receipt=receipt,
        session_aliases=tuple(aliases) if aliases is not None else (),
    )


def make_run(root: Path, events, *, providers=None, cutoff=CUTOFF, warnings=None,
             errors=None, provider_stats=None, source_path="/private/history"):
    providers = providers or ("codex",)
    errors = errors or {}
    results = {}
    roots = {}
    for provider in providers:
        stats = {"errors": errors.get(provider, 0)}
        stats.update((provider_stats or {}).get(provider, {}))
        results[provider] = CollectionResult(
            stats=stats,
            warnings=list((warnings or {}).get(provider, [])),
        )
        roots[provider] = [Path(source_path)]
    return persist_run(
        root, list(events), results, cutoff, None, "Europe/Warsaw",
        "2026-10-22T12:00:00Z", roots, {},
    )


def antigravity_event(name, timestamps):
    key = "antigravity:" + digest(name)
    session_key = digest("session-" + name)
    observations = [
        {"input": 10, "cache_creation": 2, "cache_read": 100,
         "output_total": 8, "visible_output": 5, "reasoning": 3}
        for _ in timestamps
    ]
    known = set(value for value in timestamps if value is not None)
    timestamp = next(iter(known)) if len(known) == 1 else None
    receipt = {
        "schema": "antigravity.protobuf.v1",
        "source_refs": [
            {"source": digest(f"source-{name}-{index}"), "kind": "steps", "index": index, "retry": 0}
            for index in range(len(timestamps))
        ],
        "observations": observations,
        "source_timestamps": timestamps,
        "timestamp_basis": "record",
    }
    return UsageEvent(
        key=key, platform="antigravity", session_key=session_key,
        timestamp=timestamp, fresh_input=10, cache_read=100, cache_write=2,
        output=8, reasoning=3, model="model-a", counter_receipt=receipt,
    )


def rewrite_collection_file(run: Path, edit):
    collection_path = run / "collection.json"
    collection = json.loads(collection_path.read_text())
    edit(collection)
    collection_path.write_text(json.dumps(collection, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    collection_path.chmod(0o600)
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    data = collection_path.read_bytes()
    for item in manifest["files"]:
        if item["file"] == "collection.json":
            item["bytes"] = len(data)
            item["sha256"] = hashlib.sha256(data).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    manifest_path.chmod(0o600)


class LedgerTests(unittest.TestCase):
    def test_private_ledger_is_idempotent_for_repeated_and_copied_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = make_run(root / "runs", [event()])
            copied = root / "copied-run"
            shutil.copytree(run, copied)
            self.assertTrue(verify_run(copied)["verified"])

            ledger = root / "private" / "usage.sqlite3"
            first = ingest_runs(ledger, [run, copied])
            second = ingest_runs(ledger, [run])
            snapshot = ledger_snapshot(ledger)

            self.assertEqual(first["new_runs"], 1)
            self.assertEqual(first["new_event_versions"], 1)
            self.assertEqual(first["new_run_event_links"], 1)
            self.assertEqual(first["new_run_locations"], 2)
            self.assertEqual(second["new_runs"], 0)
            self.assertEqual(second["new_event_versions"], 0)
            self.assertEqual(second["new_run_event_links"], 0)
            self.assertEqual(snapshot["summary"]["totals"]["total"], 120)
            self.assertEqual(snapshot["quality"]["trusted_events"], 1)
            self.assertEqual(snapshot["quality"]["event_versions"], 1)
            self.assertEqual(ledger.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)

            rendered = json.dumps(snapshot)
            self.assertNotIn("/private/history", rendered)
            self.assertNotIn(event().key, rendered)
            self.assertNotIn(event().session_key, rendered)
            with closing(sqlite3.connect(ledger)) as connection:
                payload = json.loads(connection.execute("SELECT payload_json FROM event_versions").fetchone()[0])
                self.assertEqual(payload["counter_receipt"]["schema"], "codex.response.v1")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM run_locations").fetchone()[0], 2)

    def test_invalid_run_in_batch_leaves_database_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = make_run(root / "runs", [event()])
            corrupt = root / "corrupt-copy"
            shutil.copytree(good, corrupt)
            with (corrupt / "events.jsonl").open("a") as output:
                output.write("{}\n")
            ledger = root / "private" / "usage.sqlite3"
            ingest_runs(ledger, [])
            before = ledger_snapshot(ledger)
            with self.assertRaises(ValueError):
                ingest_runs(ledger, [good, corrupt])
            after = ledger_snapshot(ledger)
            self.assertEqual(after["summary"], before["summary"])
            self.assertEqual(after["quality"], before["quality"])

    def test_changed_counters_owner_or_known_timestamp_are_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = [
                (event("counter"), event("counter", output=9)),
                (event("owner"), event("owner", session=digest("other-owner"))),
                (event("time"), event("time", timestamp="2026-10-22T01:00:00Z")),
            ]
            runs = []
            for index, pair in enumerate(cases):
                case_root = root / f"case-{index}"
                runs.append(make_run(case_root / "runs", [pair[0]]))
                runs.append(make_run(case_root / "runs", [pair[1]], cutoff=CUTOFF.replace(day=25)))
            ledger = root / "private" / "usage.sqlite3"
            ingest_runs(ledger, runs)
            snapshot = ledger_snapshot(ledger)

            self.assertEqual(snapshot["quality"]["trusted_events"], 0)
            self.assertEqual(snapshot["quality"]["conflicted_events"], 3)
            self.assertEqual(snapshot["summary"]["events"], 0)
            self.assertEqual(snapshot["diagnostics"]["conflict_reasons"], {
                "counters": 1, "owner": 1, "timestamp": 1,
            })
            with closing(sqlite3.connect(ledger)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM event_versions").fetchone()[0], 6)

    def test_null_to_known_timestamp_enrichment_is_trusted_and_auditable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            undated = event("enrich", timestamp=None)
            dated = event("enrich")
            first = make_run(root / "runs", [undated])
            second = make_run(root / "runs", [dated], cutoff=CUTOFF.replace(day=25))
            ledger = root / "private" / "usage.sqlite3"

            ingest_runs(ledger, [first, second])
            snapshot = ledger_snapshot(ledger)

            self.assertEqual(snapshot["summary"]["events"], 1)
            self.assertEqual(snapshot["summary"]["dated_totals"]["total"], 120)
            self.assertEqual(snapshot["quality"]["timestamp_enrichments"], 1)
            self.assertEqual(snapshot["quality"]["event_versions"], 2)

    def test_metadata_drift_and_monotone_session_aliases_do_not_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = event("metadata")
            alias = digest("same-session-alias")
            expanded = replace(original, model="model-b",
                               session_aliases=(original.session_key, alias))
            run1 = make_run(root / "runs", [original])
            run2 = make_run(root / "runs", [expanded], cutoff=CUTOFF.replace(day=25))
            ledger = root / "private" / "usage.sqlite3"

            ingest_runs(ledger, [run1, run2])
            snapshot = ledger_snapshot(ledger)

            self.assertEqual(snapshot["quality"]["conflicted_events"], 0)
            self.assertEqual(snapshot["quality"]["trusted_events"], 1)
            self.assertEqual(snapshot["quality"]["event_versions"], 2)
            self.assertEqual(snapshot["summary"]["totals"]["total"], 120)
            with closing(sqlite3.connect(ledger)) as connection:
                models = {json.loads(row[0])["model"] for row in connection.execute(
                    "SELECT payload_json FROM event_versions")}
                self.assertEqual(models, {"model-a", "model-b"})

    def test_alias_superset_may_change_primary_session_and_merges_stably(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = event("alias-primary")
            new_primary = "0" * 64
            expanded = replace(original, session_key=new_primary,
                               session_aliases=(original.session_key, new_primary))
            runs = [
                make_run(root / "runs", [original]),
                make_run(root / "runs", [expanded], cutoff=CUTOFF.replace(day=25)),
            ]
            ledger = root / "private" / "usage.sqlite3"
            ingest_runs(ledger, runs)

            snapshot = ledger_snapshot(ledger)
            self.assertEqual(snapshot["quality"]["conflicted_events"], 0)
            self.assertEqual(snapshot["summary"]["platforms"]["codex"]["sessions"], 1)

    def test_incomparable_session_alias_sets_are_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = "1" * 64
            shared = "2" * 64
            right = "3" * 64
            first = replace(event("alias-conflict", session=left), session_aliases=(left, shared))
            second = replace(first, session_key=shared, session_aliases=(shared, right))
            runs = [
                make_run(root / "runs", [first]),
                make_run(root / "runs", [second], cutoff=CUTOFF.replace(day=25)),
            ]
            ledger = root / "private" / "usage.sqlite3"
            ingest_runs(ledger, runs)

            snapshot = ledger_snapshot(ledger)
            self.assertEqual(snapshot["quality"]["conflicted_events"], 1)
            self.assertEqual(snapshot["diagnostics"]["conflict_reasons"]["owner"], 1)
            self.assertEqual(snapshot["summary"]["events"], 0)

    def test_ambiguous_antigravity_receipt_stays_undated_after_known_timestamp_arrives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ambiguous = antigravity_event("ambiguous-time", [
                "2026-10-21T10:00:00Z", "2026-10-21T11:00:00Z",
            ])
            later_known = antigravity_event("ambiguous-time", ["2026-10-21T10:00:00Z"])
            runs = [
                make_run(root / "runs", [ambiguous], providers=("antigravity",)),
                make_run(root / "runs", [later_known], providers=("antigravity",),
                         cutoff=CUTOFF.replace(day=25)),
            ]
            ledger = root / "private" / "usage.sqlite3"
            ingest_runs(ledger, runs)

            snapshot = ledger_snapshot(ledger)
            self.assertEqual(snapshot["quality"]["trusted_events"], 1)
            self.assertEqual(snapshot["quality"]["ambiguous_timestamp_events"], 1)
            self.assertEqual(snapshot["quality"]["timestamp_enrichments"], 0)
            self.assertEqual(snapshot["summary"]["undated_totals"]["total"], 120)

    def test_optional_reasoning_null_enriches_but_disagreement_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            undetermined = event("reason-enrich", reasoning=None)
            known_zero = event("reason-enrich", reasoning=0)
            ledger = root / "private" / "usage.sqlite3"
            ingest_runs(ledger, [
                make_run(root / "runs", [undetermined]),
                make_run(root / "runs", [known_zero], cutoff=CUTOFF.replace(day=25)),
            ])
            enriched = ledger_snapshot(ledger)
            self.assertEqual(enriched["quality"]["conflicted_events"], 0)
            self.assertEqual(enriched["quality"]["reasoning_enrichments"], 1)

            conflicting = event("reason-enrich", reasoning=3)
            ingest_runs(ledger, [make_run(root / "runs", [conflicting], cutoff=CUTOFF.replace(day=26))])
            quarantined = ledger_snapshot(ledger)
            self.assertEqual(quarantined["quality"]["conflicted_events"], 1)
            self.assertEqual(quarantined["diagnostics"]["reasoning_conflicts"], 1)

    def test_disappearing_event_remains_in_durable_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_run(root / "runs", [event("keep"), event("disappears")])
            second = make_run(root / "runs", [event("keep")], cutoff=CUTOFF.replace(day=25))
            ledger = root / "private" / "usage.sqlite3"
            ingest_runs(ledger, [first, second])
            shutil.rmtree(first)

            snapshot = ledger_snapshot(ledger)
            self.assertEqual(snapshot["summary"]["events"], 2)
            self.assertEqual(snapshot["summary"]["totals"]["total"], 240)
            self.assertEqual(snapshot["quality"]["ingested_runs"], 2)

    def test_filtered_all_history_scope_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "private" / "usage.sqlite3"
            since_run = make_run(root / "runs", [event()])
            rewrite_collection_file(since_run, lambda collection: collection.update(
                since="2026-10-20T00:00:00Z"))
            self.assertTrue(verify_run(since_run)["verified"])
            with self.assertRaisesRegex(ValueError, "all-history"):
                ingest_runs(ledger, [since_run])

            excluded_run = make_run(root / "runs", [event()])
            rewrite_collection_file(excluded_run, lambda collection: collection["parameters"].update(
                exclude_session_hashes=[digest("absent-session")]))
            self.assertTrue(verify_run(excluded_run)["verified"])
            with self.assertRaisesRegex(ValueError, "excluded sessions"):
                ingest_runs(ledger, [excluded_run])

    def test_provider_freshness_and_last_success_are_scoped_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_run = make_run(root / "runs", [event()], providers=("codex",))
            antigravity_success = make_run(
                root / "runs", [], providers=("antigravity",),
                cutoff=CUTOFF.replace(day=23),
            )
            antigravity_failure = make_run(
                root / "runs", [], providers=("antigravity",),
                cutoff=CUTOFF.replace(day=25), errors={"antigravity": 1},
                warnings={"antigravity": ["PRIVATE_WARNING_TEXT"]},
            )
            ledger = root / "private" / "usage.sqlite3"

            ingest_runs(ledger, [codex_run, antigravity_success, antigravity_failure])
            snapshot = ledger_snapshot(ledger)

            codex = snapshot["providers"]["codex"]
            antigravity = snapshot["providers"]["antigravity"]
            self.assertEqual(codex["platform_scope"], ["codex"])
            self.assertEqual(codex["errors"], 0)
            self.assertIsNotNone(codex["last_successful_cutoff"])
            self.assertEqual(antigravity["platform_scope"], ["antigravity"])
            self.assertEqual(antigravity["capture_count"], 2)
            self.assertEqual(antigravity["errors"], 1)
            self.assertEqual(antigravity["execution_status"], "partial")
            self.assertEqual(antigravity["last_successful_cutoff"], "2026-10-23T12:00:00Z")
            self.assertIsNotNone(snapshot["source_freshness"]["codex"]["age_seconds"])
            self.assertEqual(snapshot["quality"]["coverage_status"], "partial")
            rendered = json.dumps(snapshot)
            self.assertNotIn("PRIVATE_WARNING_TEXT", rendered)
            self.assertNotIn("/private/history", rendered)

    def test_provider_scope_status_and_file_counts_use_provider_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = make_run(
                root / "runs", [], providers=("antigravity", "codex"),
                errors={"antigravity": 2, "codex": 0},
                warnings={"antigravity": ["private"], "codex": []},
                provider_stats={
                    "codex": {"files_read": 8, "compressed_files_read": 3,
                              "files_discovered": 10, "compressed_files_discovered": 4,
                              "incomplete": False},
                    "antigravity": {"source_inventory": {
                        "databases_read": 7, "sqlite_candidates": 9,
                    }},
                },
            )
            ledger = root / "private" / "usage.sqlite3"
            ingest_runs(ledger, [run])
            snapshot = ledger_snapshot(ledger)

            codex = snapshot["providers"]["codex"]
            antigravity = snapshot["providers"]["antigravity"]
            self.assertEqual(codex["execution_status"], "complete")
            self.assertEqual(codex["coverage_status"], "qualified")
            self.assertEqual(codex["source_files_read"], 8)
            self.assertEqual(codex["source_files_discovered"], 10)
            self.assertEqual(antigravity["execution_status"], "partial")
            self.assertEqual(antigravity["source_files_read"], 7)
            self.assertEqual(antigravity["source_files_discovered"], 9)

    def test_snapshot_reports_unobserved_provider_instead_of_implicit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = make_run(root / "runs", [event()], providers=("codex",))
            ledger = root / "private" / "usage.sqlite3"
            ingest_runs(ledger, [run])

            snapshot = ledger_snapshot(ledger)
            self.assertEqual(snapshot["summary"]["platforms"]["antigravity"]["total"], 0)
            self.assertEqual(snapshot["providers"]["antigravity"]["coverage_status"], "unobserved")
            self.assertIn("antigravity", snapshot["diagnostics"]["providers_never_captured"])
            self.assertEqual(snapshot["source_freshness"]["antigravity"]["status"], "never_captured")

    def test_observed_provider_with_missing_status_stays_unknown_not_unobserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = make_run(
                root / "runs", [event()], providers=("codex",),
            )
            rewrite_collection_file(run, lambda collection: collection["providers"]["codex"].update(stats={}))
            ledger = root / "private" / "usage.sqlite3"
            ingest_runs(ledger, [run])

            snapshot = ledger_snapshot(ledger)
            codex = snapshot["providers"]["codex"]
            antigravity = snapshot["providers"]["antigravity"]
            self.assertEqual(codex["capture_count"], 1)
            self.assertEqual(codex["execution_status"], "unknown")
            self.assertEqual(codex["coverage_status"], "unknown")
            self.assertIsNone(codex["last_successful_cutoff"])
            self.assertEqual(antigravity["execution_status"], "unobserved")

    def test_snapshot_requires_an_existing_private_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                ledger_snapshot(Path(tmp) / "private" / "missing.sqlite3")

    def test_unmarked_database_is_refused_without_changing_bytes_or_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "private"
            parent.mkdir(mode=0o700)
            database = parent / "provider.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE provider_data (value TEXT)")
                connection.execute("INSERT INTO provider_data VALUES ('keep')")
            database.chmod(0o600)
            before = database.read_bytes()
            with self.assertRaises(ValueError):
                ingest_runs(database, [])
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)

            database.chmod(0o644)
            before = database.read_bytes()

            with self.assertRaises(ValueError):
                ingest_runs(database, [])

            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(database.stat().st_mode & 0o777, 0o644)

    def test_zero_byte_file_is_not_a_ledger_for_snapshot_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "private"
            parent.mkdir(mode=0o700)
            database = parent / "interrupted.sqlite3"
            database.touch(mode=0o600)

            with self.assertRaises(FileNotFoundError):
                ledger_snapshot(database)
            self.assertEqual(database.stat().st_size, 0)
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)

    def test_version_one_ledger_is_migrated_transactionally_on_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = make_run(root / "runs", [event("migration")])
            source_ledger = root / "private" / "source.sqlite3"
            ingest_runs(source_ledger, [run])
            with closing(sqlite3.connect(source_ledger)) as source:
                capture_runs = source.execute("SELECT * FROM capture_runs").fetchall()
                run_locations = source.execute("SELECT * FROM run_locations").fetchall()
                event_versions = source.execute("""SELECT version_id,event_key,payload_sha256,
                    payload_json,owner_session_key,timestamp_utc,platform,first_seen_at
                    FROM event_versions""").fetchall()
                run_events = source.execute("SELECT * FROM run_events").fetchall()
                states = source.execute("""SELECT event_key,conflicted,counter_conflict,
                    owner_conflict,timestamp_conflict,chosen_version_id,version_count,
                    timestamp_enriched FROM event_state""").fetchall()

            ledger = root / "private" / "legacy.sqlite3"
            with closing(sqlite3.connect(ledger)) as legacy:
                legacy.executescript("""CREATE TABLE ledger_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE capture_runs(run_id TEXT PRIMARY KEY,manifest_sha256 TEXT NOT NULL,
                        collection_json TEXT NOT NULL,source_inventory_json TEXT NOT NULL,
                        first_ingested_at TEXT NOT NULL,last_ingested_at TEXT NOT NULL);
                    CREATE TABLE run_locations(run_id TEXT NOT NULL REFERENCES capture_runs(run_id),
                        path TEXT NOT NULL,first_seen_at TEXT NOT NULL,PRIMARY KEY(run_id,path));
                    CREATE TABLE event_versions(version_id INTEGER PRIMARY KEY,event_key TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,payload_json TEXT NOT NULL,counters_signature TEXT NOT NULL,
                        owner_session_key TEXT NOT NULL,timestamp_utc TEXT,platform TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,UNIQUE(event_key,payload_sha256));
                    CREATE TABLE run_events(run_id TEXT NOT NULL REFERENCES capture_runs(run_id),
                        event_key TEXT NOT NULL,version_id INTEGER NOT NULL REFERENCES event_versions(version_id),
                        PRIMARY KEY(run_id,event_key,version_id));
                    CREATE TABLE event_state(event_key TEXT PRIMARY KEY,conflicted INTEGER NOT NULL,
                        counter_conflict INTEGER NOT NULL,owner_conflict INTEGER NOT NULL,
                        timestamp_conflict INTEGER NOT NULL,chosen_version_id INTEGER REFERENCES event_versions(version_id),
                        version_count INTEGER NOT NULL,timestamp_enriched INTEGER NOT NULL);""")
                legacy.execute("INSERT INTO ledger_meta VALUES('schema_version','1')")
                legacy.executemany("INSERT INTO capture_runs VALUES(?,?,?,?,?,?)", capture_runs)
                legacy.executemany("INSERT INTO run_locations VALUES(?,?,?)", run_locations)
                legacy.executemany("""INSERT INTO event_versions(version_id,event_key,payload_sha256,
                    payload_json,counters_signature,owner_session_key,timestamp_utc,platform,first_seen_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", [
                        (version_id, key, sha, payload, "legacy-counters", owner, timestamp,
                         platform, first_seen)
                        for version_id, key, sha, payload, owner, timestamp, platform, first_seen in event_versions
                    ])
                legacy.executemany("INSERT INTO run_events VALUES(?,?,?)", run_events)
                legacy.executemany("INSERT INTO event_state VALUES(?,?,?,?,?,?,?,?)", states)
                legacy.commit()
            ledger.chmod(0o600)

            snapshot = ledger_snapshot(ledger)

            self.assertEqual(snapshot["summary"]["events"], 1)
            with closing(sqlite3.connect(ledger)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()[0], "2")

    def test_concurrent_writer_wait_is_bounded_and_batch_is_not_applied(self):
        from unittest.mock import patch
        import time

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = make_run(root / "runs", [event()])
            ledger = root / "private" / "usage.sqlite3"
            ingest_runs(ledger, [])
            blocker = sqlite3.connect(ledger, isolation_level=None)
            try:
                blocker.execute("BEGIN IMMEDIATE")
                started = time.monotonic()
                with patch("tokenometer.ledger.BUSY_TIMEOUT_MS", 150):
                    with self.assertRaises(sqlite3.OperationalError):
                        ingest_runs(ledger, [run])
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 2.0)
                self.assertEqual(ledger_snapshot(ledger)["summary"]["events"], 0)
                blocker.execute("ROLLBACK")
            finally:
                blocker.close()
            self.assertEqual(ingest_runs(ledger, [run])["new_event_versions"], 1)


if __name__ == "__main__":
    unittest.main()
