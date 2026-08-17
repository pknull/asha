#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PYTHONPATH="$ROOT" python3 -m unittest -v \
  tests.python.test_orchestration_config \
  tests.python.test_orchestration_model \
  tests.python.test_orchestration_store \
  tests.python.test_orchestration_graph
