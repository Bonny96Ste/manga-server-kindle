#!/bin/sh
set -eu

STACK_DIR="${STACK_DIR:-/stack}"
VENV_DIR="${VENV_DIR:-$STACK_DIR/venv}"
APP_DIR="$STACK_DIR/webapp"
REQ_FILE="$APP_DIR/requirements.txt"
PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"

mkdir -p "$STACK_DIR/data/library" "$STACK_DIR/data/downloads" "$STACK_DIR/data/state"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Creating Python virtual environment at $VENV_DIR"
  rm -rf "$VENV_DIR"
  python -m venv "$VENV_DIR"
fi

. "$VENV_DIR/bin/activate"

echo "Installing and verifying Python dependencies..."
python -m pip install --disable-pip-version-check --upgrade pip setuptools wheel
python -m pip install --disable-pip-version-check --upgrade-strategy only-if-needed -r "$REQ_FILE"

python - <<'PYDEPS'
import flask
import fitz
import gunicorn
import requests
from PIL import Image

print("Dependency check passed.")
PYDEPS

export PATH="$VENV_DIR/bin:$PATH"
export PYTHONPATH="$APP_DIR:${PYTHONPATH:-}"
cd "$APP_DIR"

echo "Starting MangaBridge v2 on $HOST:$PORT"
exec gunicorn \
  --bind "$HOST:$PORT" \
  --workers 1 \
  --threads 8 \
  --timeout 0 \
  --access-logfile - \
  --error-logfile - \
  app:app
