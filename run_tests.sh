#!/usr/bin/env bash
#
# Full verification gate for the search engine.
#
#   ./run_tests.sh
#
# Runs, in order:
#
#   1. byte-compile check on app.py
#   2. the golden-vector parity gate
#   3. the corpus sidecar integrity check
#   4. the complete pytest suite
#
# The pytest suite is hermetic: the live HTTP tests skip themselves with
# an explicit reason when no server is listening on 127.0.0.1:5000.
# To include them, start the server first:
#
#   python app.py
#
# Exit status is non-zero if any gate fails.

set -o pipefail

cd "$(dirname "$0")" || exit 1

failures=0

step() {
    printf '\n=== %s ===\n' "$1"
}

step "Byte-compiling app.py"
if python3 -m py_compile app.py; then
    echo "app.py compiles"
else
    echo "app.py FAILED to compile"
    failures=$((failures + 1))
fi

step "Golden vector parity"
if python3 -m tools.verify_golden_vectors; then
    echo "golden vectors OK"
else
    echo "golden vectors FAILED"
    failures=$((failures + 1))
fi

step "Corpus sidecar integrity"
if python3 - <<'PY'
import hashlib
import json
import sys
from pathlib import Path

path = Path("artifacts/android/corpus_sidecar.json")

if not path.exists():
    print("sidecar is missing")
    sys.exit(1)

sidecar = json.loads(path.read_text(encoding="utf-8"))

payload = {k: v for k, v in sidecar.items() if k != "integrity"}
serialized = json.dumps(
    payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
)
calculated = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

if calculated != sidecar["integrity"]["canonical_json_sha256"]:
    print("sidecar integrity hash mismatch")
    sys.exit(1)

documents = sidecar["documents"]
terms = {t for d in documents for t in d["content_terms"]}

if len(terms) != sidecar["corpus_totals"]["content_terms"]:
    print("sidecar content term total mismatch")
    sys.exit(1)

print(
    f"sidecar OK: {len(documents)} documents, "
    f"{len(terms)} content terms"
)
PY
then
    echo "sidecar OK"
else
    echo "sidecar FAILED"
    failures=$((failures + 1))
fi

step "Test suite"
if python3 -m pytest -q; then
    echo "tests passed"
else
    echo "tests FAILED"
    failures=$((failures + 1))
fi

printf '\n========================================\n'

if [ "$failures" -eq 0 ]; then
    printf 'ALL GATES PASSED\n'
    exit 0
fi

printf '%d gate(s) FAILED\n' "$failures"
exit 1
