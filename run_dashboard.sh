#!/usr/bin/env sh
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "가상환경 없음: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
exec "$PY" "$ROOT/frontend/server.py" --host 127.0.0.1 --port 8765 --open
