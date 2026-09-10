#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then PYTHON=python3; fi

if ! "$PYTHON" - <<'PY' >/dev/null 2>&1
from PyQt5 import QtWidgets
import pyqtgraph
import numpy
PY
then
    echo "GUI dependencies are not installed in this Python environment." >&2
    echo "Run: ./install.sh" >&2
    echo "Then retry: ./run_demo.sh" >&2
    exit 2
fi

exec "$PYTHON" app.py demo --dtype be-i16 --fs 250000 "$@"
