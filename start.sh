#!/usr/bin/env sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$SCRIPT_DIR/data"
exec python3 "$SCRIPT_DIR/server.py" \
  --db "$SCRIPT_DIR/data/wheelchair.db" \
  --host "${HOST:-127.0.0.1}" \
  --port "${PORT:-8080}" \
  "$@"
