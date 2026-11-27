#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
python3 -m unittest discover -s tests/python -p test_control_pi.py -v
node tests/js/pi-extension.test.mjs
