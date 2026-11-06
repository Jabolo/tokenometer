"""Bounded deployment recovery and public, aggregate-only production checks."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PRODUCTION = "https://aimilage.michaljablonski.dev"
MAX_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class PublicationError(ValueError):
    """An operator-visible fixed failure code; never request or response data."""


def recovery_needed(runs: list[dict], *, sha: str, current_run: int) -> bool:
    """Count attempts of this workflow/revision, excluding this scheduled gate.

    No-op scheduled gates also appear in run history, so only runs whose check
    job actually started count as attempts (the workflow supplies that list).
    """
    previous = [run for run in runs if run["id"] != current_run
                and run["head_sha"] == sha and run["event"] != "pull_request"]
    if any(run["status"] != "completed" for run in previous):
        return False
    if any(run["conclusion"] == "success" for run in previous):
        return False
    return sum(run.get("run_attempt", 1) for run in previous) < MAX_ATTEMPTS


def _json_api(path: str) -> dict:
    request = Request("https://api.github.com/" + path, headers={
        "Authorization": "Bearer " + os.environ["GH_TOKEN"],
        "Accept": "application/vnd.github+json", "User-Agent": "tokenometer-publication",
    })
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def workflow_context() -> tuple[str, str]:
    repo, sha = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("invalid_workflow_context")
    return repo, sha


def current_main() -> bool:
    repo, sha = workflow_context()
    return _json_api(f"repos/{repo}/git/ref/heads/main")["object"]["sha"] == sha


def scheduled_recovery() -> bool:
    repo, sha = workflow_context()
    if not current_main():
        return False
    current = int(os.environ["GITHUB_RUN_ID"])
    # Ignore schedule-only no-ops by selecting only runs with an actual check
    # job. Page size is bounded; stop rather than guess with truncated history.
    result = _json_api(f"repos/{repo}/actions/workflows/check.yml/runs?head_sha={sha}&per_page=100")
    if result["total_count"] > 100:
        return False
    attempts = []
    for run in result["workflow_runs"]:
        if run["id"] == current:
            continue
        if run["event"] == "schedule":
            jobs = _json_api(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")
            if not any(job["name"] == "check" and job["conclusion"] != "skipped"
                       for job in jobs["jobs"]):
                continue
        attempts.append(run)
    return recovery_needed(attempts, sha=sha, current_run=current)


def _public_get(path: str) -> tuple[int, object, bytes]:
    request = Request(PRODUCTION + path, headers={"Cache-Control": "no-cache",
                                                "User-Agent": "tokenometer-publication"})
    try:
        response = urlopen(request, timeout=20)
    except HTTPError as error:
        response = error
    with response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise PublicationError("response_too_large")
        return response.code, response.headers, body


def verify_production(root: Path) -> None:
    """Compare served data and assets with the committed public tree."""
    from .release_guard import validate_aggregate_file
    validate_aggregate_file(root / "web/data.json")
    for name in ("data.json", "index.html", "app.js", "styles.css", "favicon.svg", "404.html"):
        status, headers, body = _public_get("/" + name)
        if status != 200 or body != (root / "web" / name).read_bytes():
            raise PublicationError("public_content_mismatch_" + name.replace(".", "_"))
        if "default-src 'self'" not in headers.get("Content-Security-Policy", ""):
            raise PublicationError("public_csp_missing_" + name.replace(".", "_"))
        if name == "data.json" and "no-store" not in headers.get("Cache-Control", ""):
            raise PublicationError("public_data_cache_policy_invalid")
    for path in ("/.local/", "/.git/config", "/tokenometer/automation.py"):
        if _public_get(path)[0] != 404:
            raise PublicationError("private_path_not_404")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("recovery-needed", "current-main", "verify"))
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    try:
        if args.command in {"recovery-needed", "current-main"}:
            needed = scheduled_recovery() if args.command == "recovery-needed" else current_main()
            key = "needed" if args.command == "recovery-needed" else "current"
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(key + "=" + str(needed).lower() + "\n")
            print("Publication gate: " + ("proceed" if needed else "skipped"))
        else:
            # Allow bounded propagation time. No data collection or Git writes.
            for attempt in range(6):
                try:
                    verify_production(args.root)
                    print("Production content, headers and private-path checks: PASS")
                    return 0
                except (ValueError, URLError, TimeoutError) as error:
                    if attempt == 5:
                        if isinstance(error, PublicationError):
                            raise
                        raise PublicationError("production_verification_failed") from None
                    time.sleep(10)
        return 0
    except PublicationError as error:
        print("Publication check failed: " + str(error))
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        # Do not print API bodies, request headers or exception text.
        print("Publication check failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
