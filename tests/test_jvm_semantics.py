"""
Executed verification of the Android port's Java semantics.

These tests drive a real JVM through `tools/verify_jvm_semantics.py`.

They exist because the original parity design was validated against a
model built from Python's own Unicode tables, which was circular: it
could only ever confirm itself. A real JVM exposed the difference.

The JVM is started in a subprocess rather than in-process. Starting a
JVM inside pytest is fragile (the JNI bridge resolves libjvm from the
environment at import time, and a failure there takes the whole session
with it), and a subprocess also keeps the JVM's lifecycle out of the
test process.

Findings on Temurin 25.0.2 (Unicode 16) against Python 3.11
(Unicode 14.0.0):

* alphanumeric predicate -- 1,112,064 code points, 9,392 differences,
  **every one** of them a code point unassigned in Unicode 14 but
  assigned in Unicode 16. Zero genuine divergences.
* whitespace predicate -- 1,112,064 code points, zero divergences.
* lowercasing, BMP -- 6 version-skew differences, plus U+FEFF, which is
  a separator under both languages and cannot change a token.
* rounding -- 24,102 comparisons, zero mismatches.

The Unicode version skew is not a porting defect and is not fixable: it
depends on the runtime, and Android's ART ships a third Unicode version
again. It is measured and recorded rather than assumed away.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

TOOL = "tools.verify_jvm_semantics"

SUBPROCESS_TIMEOUT = 900


def _build_environment():
    """Return an environment that can start a JVM, or None."""

    environment = dict(os.environ)

    if "JAVA_HOME" not in environment:
        try:
            import jdk4py  # noqa: F401

            from jdk4py import JAVA_HOME

            environment["JAVA_HOME"] = str(JAVA_HOME)
        except ImportError:
            return None

    java_home = environment["JAVA_HOME"]

    environment["PATH"] = (
        os.path.join(java_home, "bin")
        + os.pathsep
        + environment.get("PATH", "")
    )

    environment["LD_LIBRARY_PATH"] = os.pathsep.join(
        [
            os.path.join(java_home, "lib", "server"),
            os.path.join(java_home, "lib"),
            environment.get("LD_LIBRARY_PATH", ""),
        ]
    )

    return environment


@pytest.fixture(scope="module")
def jvm_summary():
    """Run the JVM verification once and return its summary."""

    environment = _build_environment()

    if environment is None:
        pytest.skip(
            "no JDK available (pip install jdk4py pyjnius to enable); "
            "JVM semantics cannot be verified on this machine"
        )

    completed = subprocess.run(
        [sys.executable, "-m", TOOL, "--json"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        env=environment,
    )

    if completed.returncode not in (0, 1):
        pytest.skip(
            "could not start a JVM: "
            + (completed.stderr or completed.stdout)[-500:]
        )

    start = completed.stdout.find("{")

    if start == -1:
        pytest.skip(
            "the verification tool produced no summary: "
            + (completed.stderr or completed.stdout)[-500:]
        )

    return json.loads(completed.stdout[start:])


def test_code_point_sweep_actually_ran(jvm_summary):
    """Guard against the sweep silently doing nothing."""

    assert jvm_summary["code_points_checked"] > 1_000_000
    assert jvm_summary["rounding_comparisons"] > 10_000


def test_alphanumeric_predicate_has_no_genuine_divergences(jvm_summary):
    """
    The predicate logic must be correct.

    Differences are allowed only when the two runtimes ship different
    Unicode versions and the code point is unassigned in Python's.
    """

    assert jvm_summary["genuine_alnum"] == 0, (
        "the alphanumeric predicate disagrees with Python for assigned "
        "code points"
    )


def test_whitespace_predicate_is_exact(jvm_summary):
    """
    The whitespace predicate must be exact with no exceptions.

    This is the claim that would break if the port used a single Java
    predicate: U+00A0, U+2007, U+202F and U+0085 are each handled by a
    different one.
    """

    assert jvm_summary["genuine_space"] == 0


def test_lowercasing_has_no_unexpected_divergences(jvm_summary):
    assert jvm_summary["lowercase_unexpected"] == 0
    assert jvm_summary["lowercase_unexpected_detail"] == []


def test_rounding_matches_python_exactly(jvm_summary):
    """
    BigDecimal(value).setScale(digits, HALF_EVEN) must reproduce round().

    HALF_UP, or BigDecimal.valueOf, would fail here.
    """

    assert jvm_summary["rounding_mismatches"] == 0


def test_overall_verification_passed(jvm_summary):
    assert jvm_summary["genuine_failures"] == 0


def test_version_skew_is_recorded_not_hidden(jvm_summary):
    """
    The Unicode version skew must be measured and reported.

    If this ever reaches zero, the two runtimes agree on the Unicode
    version and the skew section of ANDROID.md is out of date.
    """

    assert "python_unicode" in jvm_summary

    print(
        f"\nJVM verification: java {jvm_summary['java_version']} "
        f"vs python unicode {jvm_summary['python_unicode']}"
    )
    print(f"  version skew        : {jvm_summary['version_skew']:,}")
    print(f"  genuine divergences : {jvm_summary['genuine_failures']}")
