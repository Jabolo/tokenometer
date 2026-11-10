# AI mileage

Michał Jabłoński's AI usage, in numbers. Actual counters, questionable bragging rights.

**[aimilage.michaljablonski.dev](https://aimilage.michaljablonski.dev)**

A small static page showing recorded Codex and Antigravity token volume and recent daily use. No framework, external fonts, analytics, cookies or server-side app. The collector runs locally on macOS; only aggregate data is public.

## How it works

1. Read counters retained in local Codex logs (including compressed archives and CodexBar-managed homes) and Antigravity SQLite/protobuf metadata.
2. Verify a private immutable capture and merge observed events into a private SQLite ledger by identity. Repeated reads, copied histories and account switches do not add a second copy.
3. Optionally reconcile privately supplied, reviewed Codex daily reports with that ledger. Only eligible nonoverlapping days can supplement the displayed total; see the [daily refresh operation](docs/DAILY-REFRESH.md#optional-private-codex-reports).
4. Export a strictly allowlisted aggregate: counts, dates, provider freshness and coverage. Never prompts, conversations, credentials, account labels, event IDs or local paths.
5. The export also reports earlier retained events absent from the latest source read; they stay included in the ledger total and are never added again. A daily Mac job commits only `web/data.json` through an isolated clone. GitHub Actions validates the release and uploads only the static `web/` directory to Cloudflare Pages.

## Read the numbers honestly

The number is **recorded token volume**, including reused context. It is not a bill, a productivity score, unique words, or proof of first-ever AI use. Local history can be incomplete across accounts or devices. Missing optional counters remain unknown in private receipts. Undated events count in the total and stay out of daily charts; contradictory event versions are held out for review.

Input categories are disjoint: fresh input, cache read and cache write. Output already includes reasoning. The page groups fresh input and cache write as “New & stored context.” Quota percentages and transcript-length estimates are not usage sources.

Supplemental report totals do not include a reliable category breakdown, so none is invented. When reports are configured, the chart consistently uses UTC days, including when a report is withheld after a later local observation. Otherwise it uses the configured dataset timezone. Reports and local histories may both be incomplete, and a day with no local record is not proof that all account or device history was captured.

Recent use counts days with a dated event in the 30 complete calendar days before the selected tools' latest saved read, in the dataset timezone. Its exact window stays visible as the saved data ages. Missing daily rows mean no retained record, not proven zero use. Antigravity's absent optional counter categories are not estimated or represented as provider billing totals.

## Run locally

Python 3.9+ and SQLite, with no third-party Python runtime dependencies. Codex `.jsonl.zst` archives need Python 3.14+ (built-in `compression.zstd`) or an existing `zstd` executable. Missing decompression support is an explicit source error.

```sh
./scripts/refresh.sh
./scripts/preview.sh
```

Open `http://127.0.0.1:4317`. The preview serves only explicit web assets. Its reload button reads the saved aggregate; it does not collect history or contact a model.

Refresh exit codes: **0** completed with qualified coverage; **2** completed with explicit partial coverage; **1** failed. Partial coverage is shown on the page. Never add totals from overlapping capture snapshots.

```sh
# Inspect available local stores (private path output).
python3 -m tokenometer discover
# Replay a verified full-history capture without scanning providers.
./scripts/refresh.sh --from-run .local/runs/RUN_DIRECTORY
# Run the fixture and integration suite.
python3 -m unittest discover -s tests -q
# Check browser date windows and aggregate validation (Node.js 18+).
node --test tests/test_web.js
```

Use `tokenometer.example.json` and an explicit `--config` path to select additional known roots. Roots are not accounts. Signing in again is not needed to read history already retained on the Mac; that does not establish complete server-side history.

## Lossless capture storage and retry recovery

New captures stream private receipts directly into `events.jsonl.gz`. The manifest
still names the logical `events.jsonl` byte stream and checks its uncompressed
size and SHA-256. Old plain captures remain readable. Compression does not change
run identity, source receipts, historical daily rows or ledger deduplication.

After upgrading all local readers, compact existing finalized captures:

```sh
python3 -m tokenometer pack-runs .local/runs/20*
# Restore exactly the original JSONL bytes if needed.
python3 -m tokenometer pack-runs --restore .local/runs/RUN_DIRECTORY
```

The command uses the daily refresh ledger lock. For a custom ledger pass
`--ledger PATH` with the same path used by refresh. It validates all logical
checksums, checks the converted stream again, and atomically installs the result
before removing its redundant encoding. It never removes a run, manifest,
receipt, ledger row, agent record or provider history. Hidden partial captures
are refused. Older software that only reads plain JSONL must be upgraded before
compaction or used only after `--restore`.

If a daily attempt fails after saving a completed capture, its next attempt can
replay that capture instead of scanning all source histories again. Recovery
requires the same roots, timezone and compatible counter adapters, an unfiltered
full capture within the failed attempt/retry window, and normal verification
before ingestion. The saved cutoff remains visible; replay does not pretend a
new source read occurred.

## Privacy and operations

`.local/`, credentials, machine-specific notes and raw receipts are excluded from publication. Git ignores new paths by default; the release guard checks an explicit public manifest and strict aggregate schema before publication. Never run a generic HTTP server at the repository root or serve a private capture directory.

- [Publishing and rollback](docs/PUBLISHING.md)
- [Daily refresh operation](docs/DAILY-REFRESH.md)

The Mac must be available and the user session logged in for local collection. The hosted page remains available with its last saved dataset when the collector is offline. No AI agent or subscription is needed for the scheduled refresh.

MIT licensed.

## Agent and operator contract

Start with [AGENTS.md](AGENTS.md) and the [history retro](docs/RETRO-HISTORY-2026-11-10.md). Accepted history stays trusted; source visibility, ledger retention and legacy CSV mapping are separate checks. `python3 -m tokenometer audit-ledger` checks retained state and lineage without scanning providers or restoring data.
