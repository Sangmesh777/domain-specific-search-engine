#!/usr/bin/env bash
#
# Full regression gate in one command.
#
#   ./run_tests.sh
#
# Steps:
#   1. resolve a Python interpreter (.venv when present)
#   2. start the Flask API on 127.0.0.1:5000 without the reloader
#   3. wait for /api/status
#   4. seed the development corpus when the server owns no documents
#      (test_corpus_health asserts content_terms > 0 after the suite has
#       deleted every document it created)
#   5. run pytest
#   6. stop the server and propagate the pytest exit code
#
# Pass extra pytest arguments through, e.g.:
#   ./run_tests.sh -q tests/test_search_engine_core.py

set -u

cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
else
    PYTHON="${PYTHON:-python3}"
fi

HOST="127.0.0.1"
PORT="${PORT:-5000}"
BASE_URL="http://${HOST}:${PORT}"

echo "[run_tests] interpreter: $PYTHON"

"$PYTHON" tools/serve.py --host "$HOST" --port "$PORT" > /tmp/vtu_server.log 2>&1 &
SERVER_PID=$!

cleanup() {
    if kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
    fi
}
trap cleanup EXIT

echo "[run_tests] waiting for ${BASE_URL}/api/status"

for attempt in $(seq 1 60); do
    if "$PYTHON" - "$BASE_URL" <<'PY' 2>/dev/null
import sys
import requests
requests.get(f"{sys.argv[1]}/api/status", timeout=2).raise_for_status()
PY
    then
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "[run_tests] server exited during startup" >&2
        cat /tmp/vtu_server.log >&2
        exit 1
    fi
    sleep 1
    if [ "$attempt" = "60" ]; then
        echo "[run_tests] server did not become ready" >&2
        cat /tmp/vtu_server.log >&2
        exit 1
    fi
done

DOCUMENTS=$("$PYTHON" - "$BASE_URL" <<'PY'
import sys
import requests
print(requests.get(f"{sys.argv[1]}/api/status", timeout=10).json()["documents"])
PY
)

echo "[run_tests] corpus documents: ${DOCUMENTS}"

if [ "${DOCUMENTS:-0}" = "0" ]; then
    echo "[run_tests] seeding development corpus"
    "$PYTHON" tools/seed_dev_corpus.py --base-url "$BASE_URL" || exit 1
fi

echo "[run_tests] pytest"
"$PYTHON" -m pytest "$@"
STATUS=$?

echo "[run_tests] pytest exit code: ${STATUS}"
exit "$STATUS"
