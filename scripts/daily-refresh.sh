#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON=${TOKENOMETER_PYTHON:-python3}
CONFIG=${TOKENOMETER_DAILY_CONFIG:-$PROJECT_DIR/.local/daily-refresh.json}

cd "$PROJECT_DIR"
exec "$PYTHON" -m tokenometer.automation run --config "$CONFIG"
