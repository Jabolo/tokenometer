# History retention and freshness retro

The incident combined stale source reads, incomplete root discovery and an
incorrect inference during diagnosis: failing to join an old CSV was treated as
confirmed history loss. Previously accepted counters were not invalidated by
that failed join. Recovery was proposed before a missing accepted event set had
been demonstrated; no backup database was downloaded or restored.

## Causes, fixes and evidence boundaries

| Trigger | Failure or mistaken inference | Control | Verification |
| --- | --- | --- | --- |
| Work continues after a morning capture | Yesterday's partial bar appears to describe the full day | Show the exact oldest successful source cutoff independently of export time | Browser tests cover mixed provider ages, failed reads and calendar boundaries |
| A daily attempt fails after saving its capture | Retrying repeats a costly full scan | Reuse compatible verified captures; retain cutoff; fall back only after a bad replay | Recovery, retry-window and corrupt-capture regressions |
| CodexBar uses another history home | Default discovery omits retained counters | Discover named managed homes; exclude temporary folders and symlinks; deduplicate copies | Plain/compressed and cross-root identity fixtures |
| Current stores stop returning earlier records | A smaller snapshot is mistaken for ledger loss | Preserve accepted versions/lineage; expose dated source-visibility gaps separately | Missing-source and replay retention tests |
| Legacy CSV IDs cannot be joined | Unmatched rows are called lost or added again | Keep accepted history trusted; classify mapping uncertainty before recovery | Read-only stored-lineage audit plus explicit recovery gate in AGENTS |
| Derived ledger state is missing or inconsistent | An export could silently omit retained counters | Projection preflight before collection, checks during ingestion/export; fail without replacing the public file | Missing-state, wrong-choice and hidden-conflict regressions |
| Backup files are cloud-offloaded | Verification unintentionally hydrates a large tree | Inspect flags/manifests first; compare existing fingerprints or hash at the storage host | Private audit evidence; not a claim that every possible account was captured |

## Decision rules

Source visibility, stored retention and historical authenticity are independent
questions. A positive public `history_audit` reports dated retained events absent
from the latest capture; those tokens are already in the main total. A zero count
does not establish complete account history. Undated usage remains separate.

An unmatched legacy CSV does not supply a canonical event identity or prove that
its total is disjoint. Keep it as comparison evidence, without adding it again or
requiring source recovery to validate an earlier accepted observation.

Before proposing a restore, record a concrete missing event set/interval or
corrupt retained artifact, why existing captures cannot satisfy it, the source,
bounded operation, private destination and deduplication check. An owner request
can authorize a different scope. Never silently weaken arithmetic/conflict checks
or overwrite live stores. A full device restore is a separate operation.

## Operator sequence

1. Inspect the daily job outcome and provider read cutoff; do not infer zero use
   from a blank day or describe a morning capture as a completed day.
2. Inspect root inventory and read diagnostics. Use capture comparison to separate
   historical backfill from post-cutoff activity; snapshots are never additive.
3. If stored retention is in question, run:

   ```sh
   python3 -m tokenometer audit-ledger --max-seconds 300
   ```

   This uses a read-only transaction and reports bounded aggregate checks of
   derived state and capture lineage. It does not scan providers, repair/migrate
   the database, restore backups, or re-prove source authenticity. A legitimate
   evidence conflict is reported separately and can coexist with structural
   integrity. Exit 0 means these stored checks passed; exit 1 means a failed or
   timed-out audit, not proven account completeness or zero usage.
4. A refresh projection failure exits 3 with `ledger_integrity_failed` in the
   daily runner. Preserve captures, the ledger and the last public export. A
   failed preflight stops before provider collection. Later checks also stop
   aggregate export. Do not delete state, reset totals or rerun long collectors
   as a repair; investigate the named structural inconsistency first.
5. After a reviewed change, run tests, release checks and independent review;
   publish only the approved aggregate/source and verify the actual deployment.

The automatic projection guard is intentionally narrower than the full manual
lineage audit: it checks missing/orphan derived states and invalid selected
versions, and rejects counter conflicts hidden behind a trusted state. Full
capture-link counts are checked by `audit-ledger`, outside the routine source
scan. These are relational consistency checks, not signed attestations or a
recomputation of every source receipt.

## Durable delivery

`AGENTS.md` is now tracked, sanitized and explicitly release-allowlisted. Private
case evidence and the previous local instruction bytes remain outside Git.
Future agents should start with that contract rather than reconstructing owner
decisions from old audit artifacts. The existing single daily scheduler and
publication route remain unchanged. No NAS restore, new backup job, account
switch or additional provider scan is part of this change.
