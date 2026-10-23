#!/usr/bin/env python3
"""Install or remove the user LaunchAgent for Tokenometer's daily refresh."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.fspath(PROJECT_ROOT))

from tokenometer.automation import AutomationError, load_config


LABEL = "com.michaljablonski.tokenometer.daily-refresh"
PLIST_NAME = LABEL + ".plist"
MARKER = "Tokenometer daily refresh managed LaunchAgent"


def _launch_agents(home: Path | None = None) -> Path:
    home = home or Path.home()
    return home / "Library" / "LaunchAgents"


def render_plist(config: dict, config_path: Path) -> bytes:
    script = config["project_dir"] / "scripts" / "daily-refresh.sh"
    if script.is_symlink() or not script.is_file():
        raise AutomationError("refresh_script_missing")
    values = {
        "Label": LABEL,
        "ProgramArguments": ["/bin/sh", os.fspath(script)],
        "WorkingDirectory": os.fspath(config["project_dir"]),
        "EnvironmentVariables": {
            "TOKENOMETER_PYTHON": os.fspath(config["python_executable"]),
            "TOKENOMETER_DAILY_CONFIG": os.fspath(config_path.resolve()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "StartCalendarInterval": {"Hour": 9, "Minute": 15},
        "StartInterval": 900,
        "RunAtLoad": True,
        "KeepAlive": False,
        "ThrottleInterval": 900,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "Comment": MARKER,
    }
    return plistlib.dumps(values, fmt=plistlib.FMT_XML, sort_keys=True)


def _launchctl(*arguments: str) -> bool:
    executable = shutil.which("launchctl")
    if executable is None:
        raise AutomationError("launchctl_unavailable")
    try:
        result = subprocess.run([executable, *arguments], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        raise AutomationError("launchctl_failed") from None
    return result.returncode == 0


def _managed_plist(path: Path) -> bool:
    if path.is_symlink() or not path.exists():
        return False
    try:
        if not path.is_file() or path.stat().st_uid != os.getuid():
            return False
        with path.open("rb") as stream:
            value = plistlib.load(stream)
        return value.get("Label") == LABEL and value.get("Comment") == MARKER
    except (OSError, ValueError, plistlib.InvalidFileException, AttributeError):
        return False


def _write_plist(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.parent.stat().st_uid != os.getuid():
        raise AutomationError("launch_agents_path_invalid")
    if path.exists() and not _managed_plist(path):
        raise AutomationError("plist_collision")
    fd, temporary = tempfile.mkstemp(prefix=".tokenometer-launchagent-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _service_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def install(config_path: Path, *, dry_run: bool = False,
            launch_agents: Path | None = None) -> dict:
    config = load_config(config_path)
    from tokenometer.automation import require_warsaw_system_timezone
    require_warsaw_system_timezone()
    content = render_plist(config, config_path)
    target_dir = launch_agents or _launch_agents()
    path = target_dir / PLIST_NAME
    if path.exists() and not _managed_plist(path):
        raise AutomationError("plist_collision")
    if dry_run:
        return {"status": "dry_run", "action": "install",
                "schedule": "09:15 Europe/Warsaw plus 15-minute retry checks",
                "run_at_load": True, "registration": "launchctl bootstrap"}

    _write_plist(path, content)
    target = _service_target()
    if _launchctl("print", target):
        if not _launchctl("bootout", target):
            raise AutomationError("launchctl_failed")
    if not _launchctl("bootstrap", f"gui/{os.getuid()}", os.fspath(path)):
        raise AutomationError("launchctl_failed")
    return {"status": "installed",
            "schedule": "09:15 Europe/Warsaw plus 15-minute retry checks", "run_at_load": True}


def uninstall(*, dry_run: bool = False, launch_agents: Path | None = None) -> dict:
    path = (launch_agents or _launch_agents()) / PLIST_NAME
    if not path.exists():
        return {"status": "not_installed" if not dry_run else "dry_run", "action": "uninstall"}
    if not _managed_plist(path):
        raise AutomationError("plist_not_managed")
    if dry_run:
        return {"status": "dry_run", "action": "uninstall", "registration": "launchctl bootout"}
    target = _service_target()
    if _launchctl("print", target) and not _launchctl("bootout", target):
        raise AutomationError("launchctl_failed")
    path.unlink()
    return {"status": "uninstalled"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install-daily-refresh")
    commands = parser.add_subparsers(dest="action", required=True)
    install_parser = commands.add_parser("install", help="Write and register the user LaunchAgent")
    install_parser.add_argument("--config", type=Path, required=True)
    install_parser.add_argument("--dry-run", action="store_true")
    uninstall_parser = commands.add_parser("uninstall", help="Unload and remove the user LaunchAgent")
    uninstall_parser.add_argument("--dry-run", action="store_true")
    dry = commands.add_parser("dry-run", help="Show the install plan without writes or launchctl calls")
    dry.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            result = install(args.config, dry_run=args.dry_run)
        elif args.action == "uninstall":
            result = uninstall(dry_run=args.dry_run)
        else:
            result = install(args.config, dry_run=True)
        print(json.dumps(result, sort_keys=True))
        return 0
    except AutomationError as exc:
        print(json.dumps({"status": "failed", "reason": exc.code}, sort_keys=True), file=sys.stderr)
        return 1
    except BaseException:
        print(json.dumps({"status": "failed", "reason": "unexpected_error"}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
