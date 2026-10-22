#!/bin/sh
# Run from any working directory; all options pass unchanged to the local CLI.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR/.."
exec python3 -m tokenometer collect "$@"
