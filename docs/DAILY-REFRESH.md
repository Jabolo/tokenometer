# Daily private refresh and aggregate publication

The optional macOS LaunchAgent checks at 09:15 in the Mac's system timezone, every 15 minutes, and once when the user logs in. Set the Mac system timezone to Europe/Warsaw before installation; the installer checks this and never changes it. The runner returns before collection until 09:15 Warsaw time. A private local-day marker permits at most three collection attempts per Warsaw calendar day, with at least 15 minutes between failed attempts. A manual `--retry-now` invocation can bypass only that cooldown; the lock, daily cap, due time, and same-day success guard still apply. A published or unchanged result ends collection for that day. Legacy same-day state without an attempt count consumes one attempt when upgraded. A stale `running` marker is recovered as interrupted only after the exclusive lock is acquired.

Each collection attempt gets the configured overall runtime limit. After aggregate validation, that attempt allows up to two publication attempts using the same private aggregate, with one cancellable five-second wait after an eligible Git clone, fetch, or push failure. It never repeats collection inside that publication retry. Failed captures are kept under `.local/daily-refresh/attempts/`; after the daily cap, collection waits for the next local day.

The refresh reads the existing local Codex and Antigravity history sources through the standard Tokenometer pipeline. It keeps captures, receipts, the event ledger, logs, and a working Git clone under `.local/`. It writes a temporary aggregate there, checks it with the release guard, and only then updates `web/data.json` in its isolated clone. The Git index is restricted to that one file. The push targets `origin main`; a repository GitHub Actions workflow then deploys the Pages site with Direct Upload. The Mac job has no Cloudflare credentials or deployment command. Its private `published` status confirms the Git push only; website publication requires the matching GitHub Actions check, deploy and live-verification steps to succeed. Hourly CI recovery retries failed publication of the current committed revision, up to three total attempts, independently of this local-day marker. Manual workflow dispatch can also republish committed bytes without collection. See [PUBLISHING.md](PUBLISHING.md#retry-deployment-without-collection). Do not reset daily state or repeat collection just to retry deployment.

The same daily refresh also collects agent-session metadata. Codex counts distinct saved sessions with an explicit subagent-spawn marker, up to the token capture's cutoff. Antigravity counts distinct indexed child conversations with a parent, positive nesting depth and an agent label as of that read; it does not claim verified launches or creation dates. Both figures cover retained local history across projects. They are not added together and do not follow the token chart's date range.

Private `agents.json` evidence is checksummed with each new refresh capture and ingested transactionally into separate agent tables in the existing ledger. Copies, repeated captures and resumed session IDs do not add sessions; later source disappearance does not erase saved observations. Conflicting parents are held out. Failed metadata reads retain the previous usable count and read time with a partial status; a never-observed count remains unknown. Agent metadata does not change token arithmetic. Older token-only captures remain replayable and do not manufacture agent observations. Collection-only `collect` captures remain token-only; use `refresh` for the combined daily flow.

## Optional private Codex reports

Refresh can also load manually supplied and reviewed Codex token reports from `analytics-reports` beside the ledger's parent directory: with the example configuration below, `.local/analytics-reports/`. Refresh and standalone export use the same ledger-based location, including with custom paths. This import is optional. The scheduled job reads these files locally on each refresh; it does not automatically retrieve provider reports. Keep the directory private and never publish its files or provenance.

Before adding a report, independently verify that its source selection covers the intended Codex product, account and user, that the complete UTC interval and capture time are correct, and that its contents are a complete `tokens_used` daily report. The manifest's checksum verifies bytes, not their source or ownership. A hashed label alone does not prove identity or that two scopes are disjoint. Do not mark a report complete unless its provenance and full interval have been verified. Reports do not prove complete account or device history.

Place the original report JSON and a `manifest.json` in the private directory. The manifest has exactly this structure; replace every placeholder with verified values, including a lowercase SHA-256 of the exact report bytes and a stable, private 64-character lowercase SHA-256 scope for the verified account and user:

```json
{
  "schema_version": 1,
  "reports": [
    {
      "file": "report.json",
      "provenance": {
        "sha256": "REPORT_BYTES_SHA256",
        "product": "codex",
        "scope": "VERIFIED_SCOPE_SHA256",
        "start": "2026-10-01T00:00:00Z",
        "end": "2026-10-02T00:00:00Z",
        "timezone": "UTC",
        "captured_at": "2026-10-02T12:00:00Z",
        "complete": true
      }
    }
  ]
}
```

The interval is start-inclusive and end-exclusive, with both boundaries at UTC midnight and capture at or after the end. File names are simple JSON names in the same directory; symlinks and traversal are rejected. The importer checks the checksum, product, nonpartial summaries, contiguous daily rows, model totals when present, and reconciliation to the report summary. A malformed or incomplete existing manifest fails the refresh rather than silently dropping its evidence. An absent report directory is allowed.

The ledger considers reported days only after a successful complete Codex read. It withholds days with any local Codex event, and withholds all reported days when Codex has an undated or ambiguous event or a conflicting identity. Repeated identical scope/day observations are deduplicated; conflicting values or multiple scopes for the same day are withheld because overlapping usage cannot be ruled out. Reconciliation runs again after each ledger ingestion: if a local event arrives later for a reported day, that supplement is removed from the next export. These guards reduce double counting but absence of a local event does not establish complete source coverage.

Only positive eligible daily totals are exported, with date and count alone. When reports are configured, local daily events are regrouped to UTC in the same ledger snapshot, even if a report becomes ineligible; this keeps day boundaries stable. Without reports, the configured timezone remains in use. Report totals have no verified input, cache or output breakdown and do not become individual ledger events; the page adds them to recorded totals without fabricating categories.

## Private configuration

Create `.local/daily-refresh.json` with mode `0600`. Do not put access tokens, SSH private keys, or Git credentials in this file. Git push uses the account's existing Git/SSH authentication. Example:

```json
{
  "schema_version": 1,
  "project_dir": "/path/to/tokenometer",
  "repo_url": "https://github.com/Jabolo/tokenometer.git",
  "branch": "main",
  "private_dir": "/path/to/tokenometer/.local/daily-refresh",
  "runs_dir": "/path/to/tokenometer/.local/runs",
  "ledger_path": "/path/to/tokenometer/.local/ledger/usage.sqlite3",
  "source_config": "/path/to/tokenometer/.local/sources.json",
  "timezone": "Europe/Warsaw",
  "max_runtime_seconds": 3600,
  "python_executable": "/absolute/path/to/python3",
  "git_identity": {
    "name": "Tokenometer Daily Refresh",
    "email": "YOUR_GITHUB_NOREPLY_EMAIL"
  },
  "deployment": {"mode": "github_actions_pages"}
}
```

`source_config` may be `null` to use the standard local root discovery. If set, it must be a private JSON Tokenometer collector config under `.local/`. The Git identity must use a GitHub noreply email. The repository URL must be an HTTPS or SSH URL on `github.com`; embedded URL credentials are rejected. The deployment mode records the expected repository setup; the local job only pushes the aggregate.

Choose an absolute, stable Python executable path, such as a package manager's `opt` symlink, instead of a versioned installation path. The installer validates its current executable target and stores the configured path without resolving the symlink. After changing the path or upgrading from an older installer that resolved it, reinstall the LaunchAgent to replace its saved environment.

## Preview and install

Check the configuration and job plan without scanning history or changing launchd:

```sh
python3 -m tokenometer.automation run --config .local/daily-refresh.json --dry-run
python3 scripts/install-daily-refresh.py dry-run --config .local/daily-refresh.json
```

Run one daily job manually, with the same local-day guard:

```sh
./scripts/daily-refresh.sh
```

To recover a failed or interrupted attempt before its cooldown expires, run the bounded manual override:

```sh
python3 -m tokenometer.automation run --config .local/daily-refresh.json --retry-now
```

The override still observes the 09:15 due time, exclusive lock, three-attempt daily cap, and same-day published or unchanged guard. Scheduled 15-minute checks resume automatically after a cooldown; they become no-ops after a successful publication or unchanged result for that local day.

Install after reviewing the dry-run output:

```sh
python3 scripts/install-daily-refresh.py install --config .local/daily-refresh.json
```

Remove the LaunchAgent and unload it from the current GUI session:

```sh
python3 scripts/install-daily-refresh.py uninstall
```

The overall runtime limit includes collection, release checks, Git fetch and push. On timeout or termination, the job stops its child process group. A lock prevents overlapping runs. The private state records the local date, attempt count, safe failure category, start and completion times, and next retry time in UTC. Logs record safe phase breadcrumbs and bounded result fields; subprocess output, exception messages, tracebacks, configuration values, and source paths are not copied to the log. System exit codes, SIGTERM, keyboard interruption, and unexpected exception classes have distinct safe failure categories.

If the Python executable is missing or not executable, the wrapper emits the fixed `python_unavailable` code through macOS system logging under `tokenometer.daily-refresh`; it cannot update the Python runner's state in that case. For a local check, compare the saved state date with the current Warsaw date, inspect the LaunchAgent with `launchctl print gui/$(id -u)/com.michaljablonski.tokenometer.daily-refresh`, and search recent system logs with `log show --last 2d --style syslog | grep 'tokenometer.daily-refresh'`. LaunchAgent registration and a successful old state do not establish that a recent refresh ran. System logging availability and retention are controlled by macOS; a missing message does not prove a successful start. A damaged executable or a change after the file check may also prevent startup without this message.

The public repository receives no captures, event rows, raw receipts, user roots, private configuration, or logs. The web export retains its partial-coverage and historical-account-completeness caveat. A failed or rejected attempt leaves private evidence available for inspection and does not stage or push website data.

An empty degraded candidate cannot replace a different saved aggregate in the synchronized publisher clone. With no prior aggregate, explicit source errors or partial/unknown provider diagnostics also block an empty publication. An initial empty view limited only by an unobserved provider is allowed. This is a failure guard, not a rule that totals must always increase: valid conflict quarantine can reduce a total. If the private ledger is lost, preserve existing captures and recover it from verified captures before collecting again.

For a publisher-only recovery, retain the validated aggregate and its SHA-256, the source revision, result code, resulting commit SHA and deployment run ID in a private recovery record. Preserve the failed attempt and local-day marker. Reuse the same validated bytes under the existing lock and deadline; do not rerun collection merely to retry publication. Retry-state corruption fails closed and requires inspecting the private state before repairing it.

## Capture storage and recovery after collection

Each capture remains a full nonadditive snapshot. Receipts are gzip-compressed
while being written; `manifest.json` describes their logical uncompressed JSONL
bytes. `verify`, replay, ingestion and comparison support both plain and gzip
captures without temporary transcript extraction. The manifest and ledger run ID
remain unchanged when `pack-runs` converts a historical capture.

Run `python3 -m tokenometer pack-runs .local/runs/20*` only after upgrading all
readers. Use `--ledger` for a nondefault production ledger and `--restore` to
recover exact original plain bytes. The shared ledger lock serializes maintenance
with daily collection and publication. The converter verifies source checksums
and rechecks the converted logical bytes before dropping a redundant encoding;
no historical capture or receipt is pruned. A crash leaves the original or both
encodings available, and conversion is repeatable. Dot-prefixed partial captures
are not eligible for this operation.

A failed attempt can leave a completed capture even though it did not publish.
On retry, the runner selects a compatible capture whose cutoff and completion
fall between the first attempt's start for that local day and the retry's start. It checks source
roots, adapter/model/receipt implementation fingerprints, full unfiltered scope,
timezone and metadata checksums. The replay pipeline then performs normal full
verification before transactional ingestion and aggregate export. This avoids a
second long source scan and another full-history snapshot. If no suitable capture
exists, collection runs normally. Source freshness still refers to that capture's
original cutoff. Corrupt selected event bytes fail closed at replay verification.

The public chart's most recent day can be a morning-only partial day. A capture
made at 09:15 does not include that day's afternoon work; the next day's completed
read supplies those events. A missed publication can leave yesterday's partial
bar online. Compare provider cutoffs and the actual saved capture before
diagnosing an account-switch or parser problem.

The optional private state field `capture_started_at` retains that original
capture window across all three bounded attempts. It does not reset attempt
counts or permit a new daily run after a successful publication.
