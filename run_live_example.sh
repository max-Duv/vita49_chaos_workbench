#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
exec python3 app.py live \
  --group 239.254.253.252 --port 52102 \
  --interface eno8403 \
  --out-group 239.255.77.77 --out-port 52102 \
  --out-interface eno8403 \
  --dtype be-i32 --fs 250000 "$@"
