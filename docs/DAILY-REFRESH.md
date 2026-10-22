# Daily private refresh and aggregate publication

The optional macOS LaunchAgent runs at 09:15 in the Mac's system timezone and once when the user logs in. Set the Mac system timezone to Europe/Warsaw before installation; the installer checks this and never changes it. A private local-day marker ensures those triggers result in at most one attempt per Warsaw calendar day. Failed attempts are kept under `.local/daily-refresh/attempts/`; the job does not loop or retry again that day. A normal scheduled run resumes the next day.

The refresh reads the existing local Codex and Antigravity history sources through the standard Tokenometer pipeline. It keeps captures, receipts, the event ledger, logs, and a working Git clone under `.local/`. It writes a temporary aggregate there, checks it with the release guard, and only then updates `web/data.json` in its isolated clone. The Git index is restricted to that one file. The push targets `origin main`; a repository GitHub Actions workflow then deploys the Pages site with Direct Upload. The Mac job has no Cloudflare credentials or deployment command.

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

Install after reviewing the dry-run output:

```sh
python3 scripts/install-daily-refresh.py install --config .local/daily-refresh.json
```

Remove the LaunchAgent and unload it from the current GUI session:

```sh
python3 scripts/install-daily-refresh.py uninstall
```

The overall runtime limit includes collection, release checks, Git fetch and push. On timeout or termination, the job stops its child process group. A lock prevents overlapping runs. The job records only bounded status fields in its private log; subprocess output, configuration values, and source paths are not copied to the log.

The public repository receives no captures, event rows, raw receipts, user roots, private configuration, or logs. The web export retains its partial-coverage and historical-account-completeness caveat. A failed or rejected attempt leaves private evidence available for inspection and does not stage or push website data.
