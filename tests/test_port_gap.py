"""
Keep the port's coverage honest and visible.

`tests/test_port_model.py` checks the model against the vectors, and
`tools/port_gap.py` reports which sections have a model at all. This
test pins the second number so the gap cannot quietly grow, and so that
adding a model for `normalize_search_query`, `parse_filetype_filter` or
`round_half_even` forces the recorded gap to be updated rather than
leaving a stale claim in `ANDROID.md`.
"""

import json
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

VECTORS = REPO_ROOT / "tests" / "golden" / "search_engine_vectors.json"

# What the model reproduces today, and what it does not. The numbers are
# asserted rather than described so that a section going from 100% to
# 90% fails here.
MODELLED_SECTIONS = (
    "sanitize_filename",
    "content_tokenize",
    "filename_tokenize",
    "character_classes",
)

UNMODELLED_SECTIONS = (
    "normalize_search_query",
    "parse_filetype_filter",
    "round_half_even",
)


def test_the_section_lists_still_describe_the_contract():
    """
    If the contract gains or loses a section, this list is out of date.
    """

    with open(VECTORS, "r", encoding="utf-8") as handle:
        contract = json.load(handle)["contract_vectors"]

    described = set(MODELLED_SECTIONS) | set(UNMODELLED_SECTIONS) | {"notes"}

    assert set(contract) == described, (
        f"unaccounted contract sections: {set(contract) - described}; "
        f"described but missing: {described - set(contract)}"
    )


def test_every_modelled_section_is_still_completely_covered():
    """
    A drop below 100% here means the model no longer describes the
    engine, which is the failure that matters when no compiler is
    available to check the Kotlin directly.
    """

    result = subprocess.run(
        [sys.executable, "-m", "tools.port_gap"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )

    assert result.returncode == 0, result.stdout + result.stderr

    report = result.stdout

    for section in MODELLED_SECTIONS:

        line = next(
            (
                row
                for row in report.splitlines()
                if row.strip().startswith(section)
            ),
            None,
        )

        assert line is not None, f"{section} missing from the report"

        assert "OK" in line, line

        assert "GAP" not in line, line


def test_the_unmodelled_sections_are_reported_as_gaps():
    """
    The gap list is deliberately asserted, not merely printed.

    When one of these gets a model, this test fails and the author has
    to move the section into `MODELLED_SECTIONS`, which also means
    updating `ANDROID.md`. That is the intended friction.
    """

    result = subprocess.run(
        [sys.executable, "-m", "tools.port_gap"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )

    report = result.stdout

    for section in UNMODELLED_SECTIONS:
        assert section in report, section
        assert "GAP" in report.split(section, 1)[1][:60], section


def test_the_ranking_vectors_are_reported_as_unreachable():
    """
    No Kotlin ranking exists, so the 79 corpus vectors cover nothing.

    Recording that here stops anyone reading a green parity suite as
    evidence about ranking behaviour on Android.
    """

    result = subprocess.run(
        [sys.executable, "-m", "tools.port_gap"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )

    assert "NEEDS THE RANKING PIPELINE" in result.stdout

    assert "79 vectors  GAP" in result.stdout
