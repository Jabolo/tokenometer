#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON=${TOKENOMETER_PYTHON:-python3}
CONFIG=${TOKENOMETER_DAILY_CONFIG:-$PROJECT_DIR/.local/daily-refresh.json}

cd "$PROJECT_DIR"

# Python cannot write its private status if it cannot start. Emit only fixed
# codes, never the executable/config path or the shell's error text.
startup_failure() {
    logger -t tokenometer.daily-refresh "$1" >/dev/null 2>&1 || :
}

PYTHON_PATH=$(command -v "$PYTHON" 2>/dev/null || :)
if [ -z "$PYTHON_PATH" ] || [ ! -f "$PYTHON_PATH" ] || [ ! -x "$PYTHON_PATH" ]; then
    startup_failure python_unavailable
    exit 1
fi

# Replace the wrapper so launchd signals reach the runner directly.
exec "$PYTHON" -m tokenometer.automation run --config "$CONFIG"
