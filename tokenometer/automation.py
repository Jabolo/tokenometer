"""Private macOS daily collection and aggregate-only Git publication.

The job runs the existing local refresh pipeline, validates its staged public
projection with ``release_guard``, then updates only ``web/data.json`` in a
private clone of the configured GitHub repository. Authentication is provided
by the user's existing Git setup; credentials are never accepted in config.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CONFIG_VERSION = 1
MAX_LOG_BYTES = 8 * 1024 * 1024
DEFAULT_BRANCH = "main"
DEFAULT_DEPLOYMENT = "github_actions_pages"
SUPPORTED_DEPLOYMENTS = {"native_pages_git", "github_actions_pages"}
PUBLISH_RETRY_DELAY_SECONDS = 5
MAX_DAILY_ATTEMPTS = 3
RETRY_COOLDOWN_SECONDS = 15 * 60
DAILY_START_TIME = datetime_time(9, 15)
RETRYABLE_PUBLISH_ERRORS = frozenset({"clone_failed", "clone_fetch_failed", "git_push_failed"})
_GITHUB_NOREPLY_DOMAIN = "users." + "noreply.github.com"
_GITHUB_SSH_PREFIX = "git" + "@github.com:"
_ACTIVE_PROCESS: subprocess.Popen[str] | None = None


class AutomationError(RuntimeError):
    """An error whose public message is a safe, bounded status code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _SignalTermination(SystemExit):
    """Marker for a process-level signal so it stays distinct from sys.exit()."""

    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(128 + signum)


def _safe_path(value: Any, name: str, *, reject_symlink: bool = True) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AutomationError("invalid_config")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise AutomationError("invalid_config")
    if reject_symlink and path.is_symlink():
        raise AutomationError("invalid_config")
    return path.resolve(strict=False)


def _under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_repo_url(value: Any) -> str:
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise AutomationError("invalid_config")
    if value.startswith(_GITHUB_SSH_PREFIX):
        path = value.removeprefix(_GITHUB_SSH_PREFIX)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", path):
            raise AutomationError("invalid_config")
        return value
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "github.com"
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", parsed.path)):
        raise AutomationError("invalid_config")
    return value


def load_config(path: Path) -> dict[str, Any]:
    """Read a private config without ever echoing its contents or path."""
    path = Path(path).expanduser()
    try:
        if path.is_symlink():
            raise AutomationError("invalid_config")
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise AutomationError("config_permissions")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except AutomationError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise AutomationError("invalid_config") from None
    keys = {"schema_version", "project_dir", "repo_url", "branch", "private_dir",
            "runs_dir", "ledger_path", "source_config", "timezone",
            "max_runtime_seconds", "python_executable", "git_identity", "deployment"}
    if not isinstance(raw, dict) or set(raw) != keys or raw.get("schema_version") != CONFIG_VERSION:
        raise AutomationError("invalid_config")

    project = _safe_path(raw["project_dir"], "project_dir")
    private = _safe_path(raw["private_dir"], "private_dir")
    runs = _safe_path(raw["runs_dir"], "runs_dir")
    ledger = _safe_path(raw["ledger_path"], "ledger_path")
    expected_private = project / ".local" / "daily-refresh"
    if private != expected_private or not _under(runs, project / ".local") or not _under(ledger, project / ".local"):
        raise AutomationError("invalid_config")
    if not project.is_dir() or project.is_symlink():
        raise AutomationError("invalid_config")

    source_config = raw["source_config"]
    if source_config is not None:
        source_config = _safe_path(source_config, "source_config")
        if not _under(source_config, project / ".local") or source_config.is_symlink():
            raise AutomationError("invalid_config")
        try:
            source_info = source_config.stat()
        except OSError:
            raise AutomationError("invalid_config") from None
        if (not stat.S_ISREG(source_info.st_mode) or source_info.st_uid != os.getuid()
                or stat.S_IMODE(source_info.st_mode) != 0o600):
            raise AutomationError("config_permissions")

    python = _safe_path(raw["python_executable"], "python_executable", reject_symlink=False)
    if not python.is_file() or not os.access(python, os.X_OK):
        raise AutomationError("invalid_config")
    try:
        ZoneInfo(raw["timezone"])
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise AutomationError("invalid_config") from None
    if raw["timezone"] != "Europe/Warsaw":
        raise AutomationError("invalid_config")
    limit = raw["max_runtime_seconds"]
    if type(limit) is not int or not 60 <= limit <= 86_400:
        raise AutomationError("invalid_config")
    identity = raw["git_identity"]
    if (not isinstance(identity, dict) or set(identity) != {"name", "email"}
            or not isinstance(identity["name"], str) or not identity["name"].strip()
            or len(identity["name"]) > 80
            or not isinstance(identity["email"], str)
            or not re.fullmatch(r"[^@\s]+@" + re.escape(_GITHUB_NOREPLY_DOMAIN), identity["email"])):
        raise AutomationError("invalid_config")
    deployment = raw["deployment"]
    if (not isinstance(deployment, dict) or set(deployment) != {"mode"}
            or not isinstance(deployment["mode"], str)
            or deployment["mode"] not in SUPPORTED_DEPLOYMENTS):
        raise AutomationError("invalid_config")
    if raw["branch"] != DEFAULT_BRANCH:
        raise AutomationError("invalid_config")

    return {**raw, "project_dir": project, "private_dir": private, "runs_dir": runs,
            "ledger_path": ledger, "source_config": source_config, "python_executable": python,
            "repo_url": _validate_repo_url(raw["repo_url"])}


def _secure_directory(path: Path, *, create: bool) -> None:
    path = Path(path)
    if path.is_symlink():
        raise AutomationError("private_path_invalid")
    if not path.exists():
        if not create:
            raise AutomationError("private_path_missing")
        path.mkdir(mode=0o700, parents=True)
    info = path.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise AutomationError("private_permissions")


def _secure_file(path: Path, *, create: bool = False) -> int:
    flags = os.O_CREAT | os.O_RDWR if create else os.O_RDWR
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            os.close(fd)
            raise AutomationError("private_permissions")
        return fd
    except AutomationError:
        raise
    except OSError:
        raise AutomationError("private_path_invalid") from None


@contextmanager
def _exclusive_lock(path: Path):
    fd = _secure_file(path, create=True)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    fd = _secure_file(path)
    os.close(fd)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, UnicodeError, ValueError):
        raise AutomationError("state_invalid") from None


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise AutomationError("invalid_clock")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AutomationError("state_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise AutomationError("state_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise AutomationError("state_invalid")
    return parsed.astimezone(timezone.utc)


def _same_day_state(state: dict[str, Any], local_day: str) -> dict[str, Any] | None:
    """Validate and normalize the bounded state for this local day.

    Historical state used only a day and status. Treat that as one consumed
    attempt so an upgrade cannot grant a fresh three-attempt allowance.
    """
    if not state:
        return None
    if "last_attempt_local_date" not in state:
        raise AutomationError("state_invalid")
    recorded_day = state.get("last_attempt_local_date")
    if not isinstance(recorded_day, str):
        raise AutomationError("state_invalid")
    try:
        recorded_date = date.fromisoformat(recorded_day)
    except ValueError:
        raise AutomationError("state_invalid") from None
    if recorded_date.isoformat() != recorded_day:
        raise AutomationError("state_invalid")
    current_date = date.fromisoformat(local_day)
    if recorded_date > current_date:
        raise AutomationError("state_invalid")
    if recorded_date < current_date:
        return None

    allowed_statuses = {"running", "failed", "interrupted", "published", "unchanged"}
    status = state.get("last_status")
    if not isinstance(status, str) or status not in allowed_statuses:
        raise AutomationError("state_invalid")
    legacy_keys = {"last_attempt_local_date", "last_status"}
    if set(state) == legacy_keys:
        normalized = {**state, "attempt_count": 1, "started_at": None,
                      "completed_at": None, "next_retry_at": None,
                      "failure_reason": None}
        return normalized

    required_keys = {"attempt_count", "started_at", "completed_at", "next_retry_at",
                     "failure_reason"}
    if not required_keys.issubset(state):
        raise AutomationError("state_invalid")
    count = state.get("attempt_count")
    if type(count) is not int or not 1 <= count <= MAX_DAILY_ATTEMPTS:
        raise AutomationError("state_invalid")
    normalized = dict(state)
    for key in ("started_at", "completed_at", "next_retry_at"):
        if key in normalized and normalized[key] is not None:
            _parse_utc_timestamp(normalized[key])
    reason = normalized.get("failure_reason")
    if reason is not None and (not isinstance(reason, str)
                               or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason)):
        raise AutomationError("state_invalid")
    started_at = normalized["started_at"]
    completed_at = normalized["completed_at"]
    next_retry_at = normalized["next_retry_at"]
    if status == "running":
        if started_at is None or completed_at is not None or next_retry_at is not None or reason is not None:
            raise AutomationError("state_invalid")
    elif status in {"published", "unchanged"}:
        if started_at is None or completed_at is None or next_retry_at is not None or reason is not None:
            raise AutomationError("state_invalid")
    else:
        recovered_legacy_start = (started_at is None
                                  and reason in {"legacy_outcome_unknown", "stale_running"})
        if (started_at is None and not recovered_legacy_start) or completed_at is None or reason is None:
            raise AutomationError("state_invalid")
        if count < MAX_DAILY_ATTEMPTS and next_retry_at is None:
            raise AutomationError("state_invalid")
        if count == MAX_DAILY_ATTEMPTS and next_retry_at is not None:
            raise AutomationError("state_invalid")
    return normalized


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, AutomationError):
        return exc.code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", exc.code) else "automation_error"
    if isinstance(exc, _SignalTermination):
        return "sigterm" if exc.signum == signal.SIGTERM else "signal_other"
    if isinstance(exc, SystemExit):
        if type(exc.code) is int and 0 <= exc.code <= 255:
            return f"system_exit_code_{exc.code}"
        return "system_exit"
    if isinstance(exc, KeyboardInterrupt):
        return "keyboard_interrupt"
    name = exc.__class__.__name__
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", name):
        return "unexpected_" + name.lower()
    return "unexpected_exception"


def _failure_status(exc: BaseException) -> str:
    if isinstance(exc, (SystemExit, KeyboardInterrupt)):
        return "interrupted"
    return "failed"


def _write_state(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _append_log(path: Path, status: str, **fields: Any) -> None:
    # Only safe enumerated states/counters reach this file; never include raw
    # subprocess output, config values, URLs, paths, or exception strings.
    safe = {"status": status}
    for key, value in fields.items():
        if key in {"exit_code", "events", "total_tokens", "attempt"} and type(value) is int:
            safe[key] = value
        elif key == "coverage_status" and value in {"qualified", "partial"}:
            safe[key] = value
        elif key == "failure_reason" and isinstance(value, str) \
                and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
            safe[key] = value
    line = json.dumps({"at": datetime.now(timezone.utc).isoformat(), **safe}, sort_keys=True) + "\n"
    fd = _secure_file(path, create=True)
    try:
        os.lseek(fd, 0, os.SEEK_END)
        size = os.fstat(fd).st_size
        if size + len(line.encode("utf-8")) > MAX_LOG_BYTES:
            raise AutomationError("log_limit")
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _kill_process_group(process: subprocess.Popen[str], grace_seconds: float = 1.0) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    stop_at = time.monotonic() + grace_seconds
    while time.monotonic() < stop_at:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        process.poll()
        time.sleep(0.05)
    # The direct child can exit before a grandchild that ignores SIGTERM.
    # Check and stop the entire process group after the finite grace window.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise AutomationError("overall_timeout")
    return value


def _run_process(arguments: list[str], *, cwd: Path, deadline: float,
                 env: dict[str, str] | None = None) -> tuple[int, str, str]:
    global _ACTIVE_PROCESS
    try:
        process: subprocess.Popen[str] = subprocess.Popen(
            arguments, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True, close_fds=True)
    except OSError:
        raise AutomationError("process_start_failed") from None
    _ACTIVE_PROCESS = process
    try:
        stdout, stderr = process.communicate(timeout=_remaining(deadline))
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        # Drain pipe buffers after the process group has been stopped.
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        raise AutomationError("overall_timeout") from None
    except BaseException:
        _kill_process_group(process)
        raise
    finally:
        _ACTIVE_PROCESS = None
    return process.returncode, stdout, stderr


def _install_signal_cleanup():
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame):
        process = _ACTIVE_PROCESS
        # A completed leader can still have descendants holding its pipes or
        # continuing work in the process group, so always clean the group.
        if process is not None:
            _kill_process_group(process)
        raise _SignalTermination(signum)

    signal.signal(signal.SIGTERM, terminate)
    return previous


def require_warsaw_system_timezone() -> None:
    """launchd calendar triggers use the Mac's system timezone."""
    try:
        localtime = Path("/etc/localtime").resolve(strict=True)
        parts = localtime.parts
        marker = parts.index("zoneinfo")
        zone = "/".join(parts[marker + 1:])
    except (OSError, ValueError):
        raise AutomationError("system_timezone_unverified") from None
    if zone != "Europe/Warsaw":
        raise AutomationError("system_timezone_not_warsaw")


def _release_guard(config: dict[str, Any], aggregate: Path) -> dict[str, Any]:
    try:
        from .release_guard import validate_aggregate_file
        return validate_aggregate_file(aggregate)
    except ImportError:
        raise AutomationError("release_guard_unavailable") from None
    except (OSError, ValueError, TypeError, KeyError):
        raise AutomationError("aggregate_rejected") from None


def _substantive(value: dict[str, Any]) -> str:
    content = dict(value)
    content.pop("generated_at", None)
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_empty_aggregate(value: dict[str, Any]) -> bool:
    return value["events"] == 0 and all(total == 0 for total in value["totals"].values())


def _has_explicit_incompleteness(value: dict[str, Any]) -> bool:
    quality = value["quality"]
    if quality["source_errors"] > 0 or quality["conflicted_events"] > 0:
        return True
    for provider in value["providers"].values():
        if (provider["source_errors"] > 0
                or provider["execution_status"] in {"partial", "unknown"}
                or provider["coverage_status"] in {"partial", "unknown"}):
            return True
    return False


def _guard_empty_degraded_candidate(candidate: dict[str, Any],
                                    previous: dict[str, Any] | None) -> None:
    """Keep an empty failed snapshot from clearing published history.

    The publisher clone is the only prior-public baseline. With no baseline,
    an empty result carrying explicit failure diagnostics also fails closed.
    A first empty snapshot caused only by an unobserved provider is allowed.
    """
    if not _is_empty_aggregate(candidate):
        return
    degraded = (candidate["quality"]["coverage_status"] == "partial"
                or _has_explicit_incompleteness(candidate))
    if not degraded:
        return

    if previous is not None and _substantive(previous) == _substantive(candidate):
        return
    if previous is None and not _has_explicit_incompleteness(candidate):
        return
    raise AutomationError("empty_partial_aggregate_rejected")


def _git(args: list[str], *, cwd: Path, deadline: float,
         failure_code: str = "git_operation_failed") -> str:
    command = ["git", *args]
    code, stdout, _stderr = _run_process(command, cwd=cwd, deadline=deadline)
    if code != 0:
        raise AutomationError(failure_code)
    return stdout.strip()


def _quarantine_clone(config: dict[str, Any], clone: Path) -> None:
    if clone.is_symlink():
        raise AutomationError("clone_invalid")
    try:
        info = clone.stat()
    except OSError:
        raise AutomationError("clone_invalid") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise AutomationError("clone_invalid")
    # Keep quarantined evidence private even if a prior interrupted clone left
    # its root at Git's usual 0755 mode.
    os.chmod(clone, 0o700)
    attempts = config["private_dir"] / "attempts"
    _secure_directory(attempts, create=True)
    name = "clone-recovery-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = attempts / (name + "-" + uuid.uuid4().hex[:8])
    os.replace(clone, destination)


def _clone_fresh(config: dict[str, Any], clone: Path, deadline: float) -> None:
    try:
        code, _stdout, _stderr = _run_process(
            ["git", "clone", "--branch", DEFAULT_BRANCH, "--single-branch", "--",
             config["repo_url"], os.fspath(clone)],
            cwd=config["project_dir"], deadline=deadline)
    except BaseException:
        if clone.exists() and not clone.is_symlink():
            _quarantine_clone(config, clone)
        raise
    if code != 0:
        if clone.exists() and not clone.is_symlink():
            _quarantine_clone(config, clone)
        raise AutomationError("clone_failed")
    os.chmod(clone, 0o700)


def _validate_clone(config: dict[str, Any], clone: Path, deadline: float) -> None:
    _secure_directory(clone, create=False)
    git_dir = clone / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise AutomationError("clone_invalid")
    root = Path(_git(["rev-parse", "--show-toplevel"], cwd=clone, deadline=deadline)).resolve()
    if root != clone.resolve():
        raise AutomationError("clone_invalid")
    remote = _git(["remote", "get-url", "origin"], cwd=clone, deadline=deadline)
    if remote != config["repo_url"]:
        raise AutomationError("clone_remote_mismatch")
    if _git(["status", "--porcelain", "--untracked-files=all"], cwd=clone, deadline=deadline):
        raise AutomationError("clone_dirty")
    if _git(["branch", "--show-current"], cwd=clone, deadline=deadline) != DEFAULT_BRANCH:
        raise AutomationError("clone_branch_mismatch")
    _git(["fetch", "--prune", "origin", "+refs/heads/main:refs/remotes/origin/main"],
         cwd=clone, deadline=deadline, failure_code="clone_fetch_failed")
    _git(["merge", "--ff-only", "origin/" + DEFAULT_BRANCH], cwd=clone, deadline=deadline,
         failure_code="clone_merge_failed")
    divergence = _git(["rev-list", "--left-right", "--count", "HEAD...origin/" + DEFAULT_BRANCH],
                      cwd=clone, deadline=deadline, failure_code="clone_divergence_failed")
    if divergence != "0\t0":
        raise AutomationError("clone_not_synced")
    _check_committed_tree(config, clone, deadline)


def _ensure_clone(config: dict[str, Any], deadline: float,
                  log_path: Path | None = None) -> Path:
    clone = config["private_dir"] / "public-repo"
    if clone.is_symlink():
        raise AutomationError("clone_invalid")
    if clone.exists():
        try:
            _validate_clone(config, clone, deadline)
            return clone
        except AutomationError as exc:
            # A fetch timeout or failure is a real external blocker; retain the
            # existing clone in place for a retry. Other local clone state is
            # preserved intact under attempts before making a fresh clone.
            if exc.code in {"overall_timeout", "clone_fetch_failed", "public_tree_rejected"}:
                raise
            _quarantine_clone(config, clone)
            if log_path is not None:
                _append_log(log_path, "prior_clone_quarantined")
    _clone_fresh(config, clone, deadline)
    try:
        _validate_clone(config, clone, deadline)
    except AutomationError as exc:
        if exc.code in {"overall_timeout", "clone_fetch_failed", "public_tree_rejected"}:
            raise
        if clone.exists() and not clone.is_symlink():
            _quarantine_clone(config, clone)
        raise
    return clone


def _check_selected_path(config: dict[str, Any], clone: Path, deadline: float) -> None:
    code, _stdout, _stderr = _run_process(
        [os.fspath(config["python_executable"]), "-m", "tokenometer.release_guard",
         "check-paths", "--root", os.fspath(clone), "--paths", "web/data.json"],
        cwd=config["project_dir"], deadline=deadline)
    if code != 0:
        raise AutomationError("public_path_rejected")


def _check_committed_tree(config: dict[str, Any], clone: Path, deadline: float) -> None:
    code, _stdout, _stderr = _run_process(
        [os.fspath(config["python_executable"]), "-m", "tokenometer.release_guard",
         "check-tree", "--root", os.fspath(clone)],
        cwd=config["project_dir"], deadline=deadline)
    if code != 0:
        raise AutomationError("public_tree_rejected")


def _check_staged_release(config: dict[str, Any], clone: Path, deadline: float) -> None:
    code, _stdout, _stderr = _run_process(
        [os.fspath(config["python_executable"]), "-m", "tokenometer.release_guard",
         "check-staged", "--root", os.fspath(clone)],
        cwd=config["project_dir"], deadline=deadline)
    if code != 0:
        raise AutomationError("staged_release_rejected")


def _atomic_public_copy(source: Path, destination: Path) -> None:
    if destination.is_symlink() or destination.parent.is_symlink():
        raise AutomationError("public_path_invalid")
    descriptor, temporary = tempfile.mkstemp(prefix=".data-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _publish(config: dict[str, Any], staged_data: Path, deadline: float,
             validated: dict[str, Any] | None = None,
             log_path: Path | None = None) -> str:
    validated = validated if validated is not None else _release_guard(config, staged_data)
    clone = _ensure_clone(config, deadline, log_path=log_path)
    web_dir = clone / "web"
    if web_dir.is_symlink() or not web_dir.is_dir():
        raise AutomationError("public_path_invalid")
    destination = web_dir / "data.json"
    current: dict[str, Any] | None = None
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file():
            raise AutomationError("public_path_invalid")
        current = _release_guard(config, destination)
    _guard_empty_degraded_candidate(validated, current)
    if current is not None and _substantive(current) == _substantive(validated):
        return "unchanged"

    _atomic_public_copy(staged_data, destination)
    # Release policy is checked against the selected path before Git sees it.
    _check_selected_path(config, clone, deadline)
    _git(["add", "--", "web/data.json"], cwd=clone, deadline=deadline)
    _check_staged_release(config, clone, deadline)
    staged = _git(["diff", "--cached", "--name-only", "--no-renames"], cwd=clone, deadline=deadline)
    if staged.splitlines() != ["web/data.json"]:
        raise AutomationError("staged_path_set_invalid")
    _git(["diff", "--cached", "--check"], cwd=clone, deadline=deadline)
    identity = config["git_identity"]
    _git(["-c", "user.name=" + identity["name"], "-c", "user.email=" + identity["email"],
          "commit", "-m", "Update AI mileage aggregate"], cwd=clone, deadline=deadline)
    committed = _git(["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
                     cwd=clone, deadline=deadline)
    if committed.splitlines() != ["web/data.json"]:
        raise AutomationError("commit_path_set_invalid")
    if _git(["status", "--porcelain", "--untracked-files=all"], cwd=clone, deadline=deadline):
        raise AutomationError("clone_dirty_after_commit")
    _git(["push", "origin", "HEAD:refs/heads/" + DEFAULT_BRANCH], cwd=clone,
         deadline=deadline, failure_code="git_push_failed")
    return "published"


def _publish_with_retry(config: dict[str, Any], staged_data: Path, deadline: float,
                        validated: dict[str, Any], log_path: Path) -> str:
    """Retry one transient Git publication failure without recollecting sources."""
    _append_log(log_path, "publication_attempt_started", attempt=1)
    try:
        result = _publish(config, staged_data, deadline, validated, log_path)
        _append_log(log_path, "publication_attempt_finished", attempt=1)
        return result
    except AutomationError as first_error:
        _append_log(log_path, "publication_attempt_failed", attempt=1,
                    failure_reason=_failure_reason(first_error))
        if first_error.code not in RETRYABLE_PUBLISH_ERRORS:
            raise
        if deadline - time.monotonic() <= PUBLISH_RETRY_DELAY_SECONDS:
            _append_log(log_path, "publish_retry_skipped_deadline")
            raise

    _append_log(log_path, "publish_retry_scheduled")
    # run_automation's SIGTERM handler raises SystemExit, interrupting this
    # wait cleanly while keeping the retry within the same overall deadline.
    time.sleep(PUBLISH_RETRY_DELAY_SECONDS)
    _remaining(deadline)
    _append_log(log_path, "publish_retry_started")
    _append_log(log_path, "publication_attempt_started", attempt=2)
    try:
        result = _publish(config, staged_data, deadline, validated, log_path)
        _append_log(log_path, "publication_attempt_finished", attempt=2)
        return result
    except AutomationError as retry_error:
        _append_log(log_path, "publication_attempt_failed", attempt=2,
                    failure_reason=_failure_reason(retry_error))
        _append_log(log_path, "publish_retry_failed")
        raise


def _perform_attempt(config: dict[str, Any], *, deadline: float, log_path: Path) -> dict[str, Any]:
    attempts = config["private_dir"] / "attempts"
    _secure_directory(attempts, create=True)
    attempt_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    attempt_dir = attempts / attempt_id
    attempt_dir.mkdir(mode=0o700)
    aggregate = attempt_dir / "data.json"
    args = [os.fspath(config["python_executable"]), "-m", "tokenometer", "refresh",
            "--output", os.fspath(config["runs_dir"]),
            "--ledger", os.fspath(config["ledger_path"]),
            "--public-output", os.fspath(aggregate),
            "--timezone", config["timezone"]]
    if config["source_config"] is not None:
        args += ["--config", os.fspath(config["source_config"])]
    _append_log(log_path, "collection_started")
    try:
        code, stdout, _stderr = _run_process(args, cwd=config["project_dir"], deadline=deadline)
    except BaseException as exc:
        _append_log(log_path, "collection_failed", failure_reason=_failure_reason(exc))
        raise
    _append_log(log_path, "refresh_finished", exit_code=code)
    if code not in (0, 2) or not aggregate.is_file() or aggregate.is_symlink():
        reason = "refresh_failed" if code not in (0, 2) else "aggregate_missing"
        _append_log(log_path, "collection_failed", exit_code=code, failure_reason=reason)
        raise AutomationError(reason)
    try:
        result = json.loads(stdout)
        summary = result["summary"]
        if (not isinstance(summary, dict) or type(summary.get("events")) is not int
                or type(summary.get("total_tokens")) is not int
                or summary.get("coverage_status") not in {"qualified", "partial"}):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        _append_log(log_path, "collection_failed", failure_reason="refresh_result_invalid")
        raise AutomationError("refresh_result_invalid") from None
    _append_log(log_path, "aggregate_validation_started")
    try:
        validated = _release_guard(config, aggregate)
    except BaseException as exc:
        _append_log(log_path, "aggregate_validation_failed", failure_reason=_failure_reason(exc))
        raise
    _append_log(log_path, "aggregate_validation_finished")
    result_status = _publish_with_retry(config, aggregate, deadline, validated, log_path)
    _append_log(log_path, result_status, events=summary["events"],
                total_tokens=summary["total_tokens"], coverage_status=summary["coverage_status"])
    return {"status": result_status, "coverage_status": summary["coverage_status"],
            "events": summary["events"], "total_tokens": summary["total_tokens"],
            "attempt_dir": attempt_dir}


def _dry_run(config: dict[str, Any]) -> dict[str, Any]:
    return {"status": "dry_run", "schedule": "09:15 Europe/Warsaw",
            "branch": DEFAULT_BRANCH, "deployment": config["deployment"]["mode"],
            "max_runtime_seconds": config["max_runtime_seconds"]}


def run_automation(config_path: Path, *, dry_run: bool = False,
                   now: datetime | None = None, retry_now: bool = False) -> dict[str, Any]:
    config = load_config(config_path)
    if dry_run:
        return _dry_run(config)
    if type(retry_now) is not bool:
        raise AutomationError("invalid_retry_option")
    clock_override = now
    if now is None:
        require_warsaw_system_timezone()
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise AutomationError("invalid_clock")
    local_now = now.astimezone(ZoneInfo(config["timezone"]))
    local_day = local_now.date().isoformat()
    if local_now.timetz().replace(tzinfo=None) < DAILY_START_TIME:
        return {"status": "before_due", "local_date": local_day}

    local = config["project_dir"] / ".local"
    if local.is_symlink() or not local.is_dir() or local.stat().st_uid != os.getuid():
        raise AutomationError("private_path_invalid")
    _secure_directory(config["private_dir"], create=True)
    _secure_directory(config["private_dir"] / "logs", create=True)
    timestamp_now = _utc_timestamp(now)
    state_path = config["private_dir"] / "state.json"
    lock_path = config["private_dir"] / "run.lock"
    previous_handler = _install_signal_cleanup()
    try:
        with _exclusive_lock(lock_path) as acquired:
            if not acquired:
                return {"status": "locked"}
            log_name = now.astimezone(timezone.utc).strftime("refresh-%Y%m%dT%H%M%SZ.log")
            log_path = config["private_dir"] / "logs" / log_name
            stored_state = _read_state(state_path)
            state = _same_day_state(stored_state, local_day)
            if state is not None and state["last_status"] in {"published", "unchanged"}:
                return {"status": "already_attempted", "local_date": local_day}

            if state is not None and state["last_status"] == "running":
                # With the exclusive lock held there is no active owner of this
                # running marker. A prior process exited without recording its
                # outcome; use the observation time as its conservative finish.
                completed_at = _utc_timestamp(now)
                retry_at = None
                if state["attempt_count"] < MAX_DAILY_ATTEMPTS:
                    retry_at = _utc_timestamp(
                        now.astimezone(timezone.utc) + timedelta(seconds=RETRY_COOLDOWN_SECONDS))
                state.update({"last_status": "interrupted", "failure_reason": "stale_running",
                              "completed_at": completed_at,
                              "next_retry_at": retry_at})
                _write_state(state_path, state)
                _append_log(log_path, "stale_running_recovered", failure_reason="stale_running",
                            attempt=state["attempt_count"])

            elif (state is not None
                  and state["last_status"] in {"failed", "interrupted"}
                  and state.get("started_at") is None
                  and state.get("completed_at") is None
                  and state.get("next_retry_at") is None):
                # Legacy state did not record when an attempt ended. Start a
                # full cooldown at detection time rather than guessing from
                # file metadata or immediately scanning again.
                retry_at = now.astimezone(timezone.utc) + timedelta(seconds=RETRY_COOLDOWN_SECONDS)
                state.update({"failure_reason": state.get("failure_reason") or "legacy_outcome_unknown",
                              "completed_at": _utc_timestamp(now),
                              "next_retry_at": _utc_timestamp(retry_at)})
                _write_state(state_path, state)
                _append_log(log_path, "legacy_state_recovered",
                            failure_reason=state["failure_reason"], attempt=state["attempt_count"])

            if state is not None:
                if state["attempt_count"] >= MAX_DAILY_ATTEMPTS:
                    return {"status": "attempt_limit_reached", "local_date": local_day,
                            "attempt_count": state["attempt_count"]}
                retry_at_value = state.get("next_retry_at")
                if retry_at_value is not None:
                    retry_at = _parse_utc_timestamp(retry_at_value)
                    if now.astimezone(timezone.utc) < retry_at and not retry_now:
                        return {"status": "retry_cooldown", "local_date": local_day,
                                "next_retry_at": retry_at_value}
            else:
                state = {}

            attempt_count = state.get("attempt_count", 0) + 1
            state.update({"last_attempt_local_date": local_day,
                          "attempt_count": attempt_count,
                          "last_status": "running",
                          "failure_reason": None,
                          "started_at": timestamp_now,
                          "completed_at": None,
                          "next_retry_at": None})
            _write_state(state_path, state)
            _append_log(log_path, "started", attempt=attempt_count)
            deadline = time.monotonic() + config["max_runtime_seconds"]
            try:
                result = _perform_attempt(config, deadline=deadline, log_path=log_path)
            except BaseException as exc:
                failure_reason = _failure_reason(exc)
                failure_status = _failure_status(exc)
                finished_at = clock_override if clock_override is not None else datetime.now(timezone.utc)
                completed_at = _utc_timestamp(finished_at)
                next_retry = None
                if attempt_count < MAX_DAILY_ATTEMPTS:
                    next_retry = _utc_timestamp(finished_at.astimezone(timezone.utc)
                                                + timedelta(seconds=RETRY_COOLDOWN_SECONDS))
                state.update({"last_status": failure_status,
                              "failure_reason": failure_reason,
                              "completed_at": completed_at,
                              "next_retry_at": next_retry})
                _write_state(state_path, state)
                _append_log(log_path, failure_status, failure_reason=failure_reason)
                raise
            finished_at = clock_override if clock_override is not None else datetime.now(timezone.utc)
            state.update({"last_status": result["status"], "failure_reason": None,
                          "completed_at": _utc_timestamp(finished_at), "next_retry_at": None})
            _write_state(state_path, state)
            return {key: value for key, value in result.items() if key != "attempt_dir"}
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tokenometer-automation")
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="Run the bounded private daily refresh and publication")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--dry-run", action="store_true", help="Validate and print the plan without scanning")
    run.add_argument("--retry-now", action="store_true",
                     help="Bypass the retry cooldown only; lock, success and daily attempt limits still apply")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_automation(args.config, dry_run=args.dry_run, retry_now=args.retry_now)
        print(json.dumps(result, sort_keys=True))
        return 0
    except AutomationError as exc:
        print(json.dumps({"status": "failed", "reason": exc.code}, sort_keys=True), file=sys.stderr)
        return 1
    except SystemExit as exc:
        reason = _failure_reason(exc)
        print(json.dumps({"status": "interrupted", "reason": reason}, sort_keys=True), file=sys.stderr)
        if type(exc.code) is int and 1 <= exc.code <= 255:
            return exc.code
        return 1
    except BaseException as exc:
        reason = _failure_reason(exc)
        status = _failure_status(exc)
        print(json.dumps({"status": status, "reason": reason}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
