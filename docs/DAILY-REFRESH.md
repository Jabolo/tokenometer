# Daily private refresh and aggregate publication

The optional macOS LaunchAgent checks at 09:15 in the Mac's system timezone, every 15 minutes, and once when the user logs in. Set the Mac system timezone to Europe/Warsaw before installation; the installer checks this and never changes it. The runner returns before collection until 09:15 Warsaw time. A private local-day marker permits at most three collection attempts per Warsaw calendar day, with at least 15 minutes between failed attempts. A manual `--retry-now` invocation can bypass only that cooldown; the lock, daily cap, due time, and same-day success guard still apply. A published or unchanged result ends collection for that day. Legacy same-day state without an attempt count consumes one attempt when upgraded. A stale `running` marker is recovered as interrupted only after the exclusive lock is acquired.

Each collection attempt gets the configured overall runtime limit. After aggregate validation, that attempt allows up to two publication attempts using the same private aggregate, with one cancellable five-second wait after an eligible Git clone, fetch, or push failure. It never repeats collection inside that publication retry. Failed captures are kept under `.local/daily-refresh/attempts/`; after the daily cap, collection waits for the next local day.

The refresh reads the existing local Codex and Antigravity history sources through the standard Tokenometer pipeline. It keeps captures, receipts, the event ledger, logs, and a working Git clone under `.local/`. It writes a temporary aggregate there, checks it with the release guard, and only then updates `web/data.json` in its isolated clone. The Git index is restricted to that one file. The push targets `origin main`; a repository GitHub Actions workflow then deploys the Pages site with Direct Upload. The Mac job has no Cloudflare credentials or deployment command.

The same daily refresh also collects agent-session metadata. Codex counts distinct saved sessions with an explicit subagent-spawn marker, up to the token capture's cutoff. Antigravity counts distinct indexed child conversations with a parent, positive nesting depth and an agent label as of that read; it does not claim verified launches or creation dates. Both figures cover retained local history across projects. They are not added together and do not follow the token chart's date range.

Private `agents.json` evidence is checksummed with each new refresh capture and ingested transactionally into separate agent tables in the existing ledger. Copies, repeated captures and resumed session IDs do not add sessions; later source disappearance does not erase saved observations. Conflicting parents are held out. Failed metadata reads retain the previous usable count and read time with a partial status; a never-observed count remains unknown. Agent metadata does not change token arithmetic. Older token-only captures remain replayable and do not manufacture agent observations. Collection-only `collect` captures remain token-only; use `refresh` for the combined daily flow.

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

The public repository receives no captures, event rows, raw receipts, user roots, private configuration, or logs. The web export retains its partial-coverage and historical-account-completeness caveat. A failed or rejected attempt leaves private evidence available for inspection and does not stage or push website data.

An empty degraded candidate cannot replace a different saved aggregate in the synchronized publisher clone. With no prior aggregate, explicit source errors or partial/unknown provider diagnostics also block an empty publication. An initial empty view limited only by an unobserved provider is allowed. This is a failure guard, not a rule that totals must always increase: valid conflict quarantine can reduce a total. If the private ledger is lost, preserve existing captures and recover it from verified captures before collecting again.

For a publisher-only recovery, retain the validated aggregate and its SHA-256, the source revision, result code, resulting commit SHA and deployment run ID in a private recovery record. Preserve the failed attempt and local-day marker. Reuse the same validated bytes under the existing lock and deadline; do not rerun collection merely to retry publication. Retry-state corruption fails closed and requires inspecting the private state before repairing it.
