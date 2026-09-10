#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# Prefer the active virtualenv's interpreter. Override with PYTHON=/path/to/python if needed.
PYTHON="${PYTHON:-python}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    PYTHON=python3
fi

echo "== VITA-49 Chaos Workbench installer =="
"$PYTHON" -c 'import sys; print("Python:", sys.version.replace("\\n", " ")); print("Executable:", sys.executable)'

if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    echo "ERROR: pip is not available for $PYTHON" >&2
    exit 2
fi

# Python 3.6 enterprise systems often ship pip 9, which cannot recognize the
# manylinux2014 PyQt5 wheel and falls back to an unusable source distribution.
if "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 6) else 1)'; then
    echo "Python 3.6 detected; installing compatible packaging-tool pins..."
    # Intentionally ONE shell line. Do not split this command with backslashes:
    # some managed/Windows-mediated lab workflows have mangled line continuations.
    "$PYTHON" -m pip install --upgrade 'pip==21.3.1' 'setuptools==59.6.0' 'wheel==0.37.1'
else
    echo "Modern Python detected; upgrading pip/setuptools/wheel..."
    "$PYTHON" -m pip install --upgrade pip setuptools wheel
fi

echo "Packaging tools:"
"$PYTHON" -m pip --version

echo "Installing runtime dependencies from binary wheels only..."
"$PYTHON" -m pip install --only-binary=:all: -r requirements.txt

echo
"$PYTHON" - <<'PY'
import sys
import numpy
import PyQt5
import pyqtgraph
from PyQt5 import QtCore
print("Dependency check: OK")
print("  Python     ", sys.version.split()[0])
print("  Executable ", sys.executable)
print("  NumPy      ", numpy.__version__)
print("  PyQt       ", QtCore.PYQT_VERSION_STR)
print("  Qt         ", QtCore.QT_VERSION_STR)
print("  pyqtgraph  ", pyqtgraph.__version__)
PY

echo
echo "Install complete. Start with: ./run_demo.sh"
