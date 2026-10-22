#!/usr/bin/env bash
#
# Full verification gate for the search engine.
#
#   ./run_tests.sh
#
# Runs, in order:
#
#   1. byte-compile check on app.py
#   2. golden-vector parity
#   3. shadow parity for extracted modules
#   4. mutation equivalence for index planners
#   5. storage reconstruction parity
#   6. negative controls for storage parity
#   7. negative controls for sanitizer implementation
#   8. Windows sanitizer branch under a simulated host
#   9. the complete pytest suite
#
# Exit status is non-zero if any gate fails.
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

step "Shadow parity (extracted modules vs original)"
if python3 -m tools.shadow_parity; then
    echo "shadow parity OK"
else
    echo "shadow parity FAILED"
    failures=$((failures + 1))
fi

step "Mutation equivalence (index planners vs original helpers)"
if python3 -m tools.mutation_equivalence; then
    echo "mutation equivalence OK"
else
    echo "mutation equivalence FAILED"
    failures=$((failures + 1))
fi

step "Storage reconstruction parity (extracted layer vs monolith)"
if python3 -m tools.storage_equivalence; then
    echo "storage parity OK"
else
    echo "storage parity FAILED"
    failures=$((failures + 1))
fi

step "Negative controls (storage parity can fail)"
if python3 -m tools.storage_parity_controls; then
    echo "storage controls OK"
else
    echo "storage controls FAILED (a control no longer applies, or no longer fails the gate)"
    failures=$((failures + 1))
fi

step "Negative controls (sanitizer transcription can fail)"
if python3 -m tools.sanitizer_controls; then
    echo "sanitizer controls OK"
else
    echo "sanitizer controls FAILED"
    failures=$((failures + 1))
fi

step "Windows branch of the sanitizer, under a simulated nt host"
if python3 -m tools.sanitizer_windows_check; then
    echo "windows branch OK"
else
    echo "windows branch FAILED"
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
