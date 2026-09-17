"""
Keep the port's coverage honest and visible.

`tests/test_port_model.py` checks the model against the vectors, and
`tools/port_gap.py` reports which sections have a model at all. This
test pins the contract and ranking coverage so the gap cannot quietly grow,
and so that moving a section forces the recorded list and `ANDROID.md` to be
updated together.
"""

import json
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

VECTORS = REPO_ROOT / "tests" / "golden" / "search_engine_vectors.json"

# All contract sections are now modelled in Python and ported to Kotlin.
MODELLED_SECTIONS = (
    "sanitize_filename",
    "content_tokenize",
    "filename_tokenize",
    "character_classes",
    "normalize_search_query",
    "parse_filetype_filter",
    "round_half_even",
)

UNMODELLED_SECTIONS = ()


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


def test_all_contract_sections_are_modelled():
    """
    Every contract section now has an executable model and Kotlin port.
    """

    assert len(UNMODELLED_SECTIONS) == 0

    result = subprocess.run(
        [sys.executable, "-m", "tools.port_gap"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )

    assert result.returncode == 0, result.stdout + result.stderr

    report = result.stdout

    assert "(none - all contract sections are modelled)" in report
    assert "unmodelled : 0 contract vectors" in report


def test_the_ranking_vectors_are_covered():
    """
    The ranking pipeline is modelled and verified against all 79 corpus vectors.
    """

    result = subprocess.run(
        [sys.executable, "-m", "tools.port_gap"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )

    assert result.returncode == 0, result.stdout + result.stderr

    report = result.stdout

    assert "RANKING PIPELINE COVERAGE" in report
    assert "79/ 79  OK" in report
    assert "+ 0 ranking vectors" in report
