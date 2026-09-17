#!/usr/bin/env bash
#
# Run the JVM parity harness.
#
# This gate runs BEFORE any Android build. It needs only a JDK, because
# the harness module has no Android dependencies. Do not build a debug
# or release APK until this reports PARITY OK.
#
# Usage:
#     android/tools/run_parity.sh
#
# Requires: JDK 17+, and Gradle (the wrapper is not checked in because
# it could not be generated without network access to
# services.gradle.org).

set -euo pipefail

cd "$(dirname "$0")/.." || exit 1

echo "=== Toolchain ==="
java -version
echo

if command -v gradle >/dev/null 2>&1; then
    echo "gradle: $(command -v gradle)"
elif [ -x "./gradlew" ]; then
    echo "gradle: ./gradlew"
else
    echo "ERROR: no JDK or Gradle available." >&2
    echo "See ../ANDROID.md section 3 for the full toolchain report." >&2
    exit 127
fi

echo
echo "=== Contract and search parity ==="

GRADLE_CMD="gradle"
[ -x "./gradlew" ] && GRADLE_CMD="./gradlew"

$GRADLE_CMD --no-daemon :harness:run

echo
echo "=== Harness unit tests ==="
$GRADLE_CMD --no-daemon :harness:test
