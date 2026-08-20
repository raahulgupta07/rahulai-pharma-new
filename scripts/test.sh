#!/usr/bin/env bash
# The suite, run the fast way, with the interpreter that has the dependencies.
#
# Two mistakes this exists to stop, both made for real:
#
#  1. Running under the SYSTEM python instead of ./venv/bin/python. It does not
#     fail — it SKIPS the three LDAP bind/TLS security guards and reports green,
#     and the only sign is one line in the header nobody reads. This script
#     always uses the venv, and says so if it is missing.
#  2. Running the whole 449-second suite to check one file. Pass paths and it
#     runs just those, serially — forking eight workers for twelve tests is
#     slower than not forking at all.
#
# Parallel by default. The suite was already built for process isolation (a
# database per PID in tests/dbguard.py, a Redis index per PID in conftest), so
# this needed no test changes. Measured here: serial 449s, -n 8 103s, -n 12 80s,
# and 1076 passed / 5 skipped every way.
#
# Eight, not "auto": each worker claims one of 15 Redis indexes, so eight leaves
# room for a second run (or a stale claim from a killed one) instead of failing
# with "every Redis DB is claimed".
#
#   ./scripts/test.sh                       full suite, 8 workers   (~100s)
#   ./scripts/test.sh tests/test_cache.py   just that file, serial  (~5s)
#   ./scripts/test.sh -k burmese            a selection, serial
#   ./scripts/test.sh --serial              full suite, one process (~450s)
#   PYTEST_WORKERS=12 ./scripts/test.sh     more workers
#
# `--serial` is what to reach for when a failure looks like it depends on order,
# or when you need pdb: a breakpoint under xdist has no terminal to talk to.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=./venv/bin/python
if [ ! -x "$PY" ]; then
  echo "no ./venv/bin/python — create it and 'pip install -r requirements-dev.txt'." >&2
  exit 1
fi
if ! "$PY" -c "import xdist" 2>/dev/null; then
  echo "pytest-xdist is not installed: $PY -m pip install -r requirements-dev.txt" >&2
  exit 1
fi

WORKERS="${PYTEST_WORKERS:-8}"
ARGS=()
SERIAL=0
SELECTED=0
for a in "$@"; do
  case "$a" in
    --serial) SERIAL=1 ;;
    -k|-x|--lf|--ff|--pdb) SELECTED=1; ARGS+=("$a") ;;
    -*) ARGS+=("$a") ;;
    *) SELECTED=1; ARGS+=("$a") ;;
  esac
done

# A selection is almost always small, and xdist's startup costs more than it
# saves below roughly a hundred tests.
#
# `${ARGS[@]+"${ARGS[@]}"}` and not `"${ARGS[@]}"`: macOS ships bash 3.2, where
# an EMPTY array under `set -u` is an unbound variable — so the plain spelling
# worked for every invocation with arguments and died on the commonest one,
# `./scripts/test.sh` with none.
if [ "$SERIAL" = 1 ] || [ "$SELECTED" = 1 ]; then
  exec "$PY" -m pytest -q -p no:cacheprovider ${ARGS[@]+"${ARGS[@]}"}
fi
exec "$PY" -m pytest -q -p no:cacheprovider -n "$WORKERS" ${ARGS[@]+"${ARGS[@]}"}
