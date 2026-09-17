"""
Verify the Kotlin port model against the golden contract vectors.

The Kotlin sources cannot be compiled in this environment, so the
algorithms are modelled in Python (see `tools/port_model.py`) using
Java's predicate semantics rather than Python's. This test proves that
the modelled algorithms reproduce every recorded contract vector.

A failure here means one of two things, and both matter:

  * the Python engine changed, so the Kotlin sources, the model and
    `ANDROID.md` all need updating together, or
  * the model no longer describes the intended Kotlin behaviour, so the
    Kotlin sources are wrong.

Either way the port must not be trusted until this passes again.
"""

import json
from pathlib import Path

import pytest

from tools.port_model import (
    is_python_alnum,
    is_python_space,
    sanitize,
    tokenize,
    tokenize_filename,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

VECTORS_PATH = REPO_ROOT / "tests" / "golden" / "search_engine_vectors.json"


@pytest.fixture(scope="module")
def contract():
    with open(VECTORS_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)["contract_vectors"]


def test_reference_model_matches_python_isalnum_for_every_code_point():
    """
    The modelled Java predicate must equal Python's str.isalnum() for
    the entire Unicode range.
    """

    mismatches = []

    for code_point in range(0x110000):
        if 0xD800 <= code_point <= 0xDFFF:
            continue

        character = chr(code_point)

        if is_python_alnum(code_point) != character.isalnum():
            mismatches.append(
                f"U+{code_point:04X} "
                f"model={is_python_alnum(code_point)} "
                f"python={character.isalnum()}"
            )

    assert not mismatches, (
        f"{len(mismatches)} alnum mismatches, first 20: "
        + ", ".join(mismatches[:20])
    )


def test_reference_model_matches_python_isspace_for_every_code_point():
    """The same, for str.isspace()."""

    mismatches = []

    for code_point in range(0x110000):
        if 0xD800 <= code_point <= 0xDFFF:
            continue

        character = chr(code_point)

        if is_python_space(code_point) != character.isspace():
            mismatches.append(
                f"U+{code_point:04X} "
                f"model={is_python_space(code_point)} "
                f"python={character.isspace()}"
            )

    assert not mismatches, (
        f"{len(mismatches)} whitespace mismatches, first 20: "
        + ", ".join(mismatches[:20])
    )


def test_model_content_tokenizer_matches_every_contract_case(contract):
    failures = []

    for case in contract["content_tokenize"]:
        actual = tokenize(case["input"])

        if actual != case["expected"]:
            failures.append(
                f"tokenize({case['input']!r}) "
                f"expected {case['expected']!r} but got {actual!r}"
            )

    assert not failures, "\n".join(failures)


def test_model_filename_tokenizer_matches_every_contract_case(contract):
    failures = []

    for case in contract["filename_tokenize"]:
        actual = tokenize_filename(case["input"])

        if actual != case["expected"]:
            failures.append(
                f"tokenize_filename({case['input']!r}) "
                f"expected {case['expected']!r} but got {actual!r}"
            )

    assert not failures, "\n".join(failures)


def test_model_sanitizer_matches_every_contract_case(contract):
    failures = []

    for case in contract["sanitize_filename"]:
        actual = sanitize(case["input"])

        if actual != case["expected"]:
            failures.append(
                f"sanitize({case['input']!r}) "
                f"expected {case['expected']!r} but got {actual!r}"
            )

    assert not failures, "\n".join(failures)


def test_model_covers_the_supplementary_plane(contract):
    """
    The contract must contain supplementary-plane cases, otherwise a
    port that uses String.length instead of a character count would
    pass unnoticed.
    """

    supplementary = [
        case
        for case in contract["content_tokenize"]
        if any(ord(character) > 0xFFFF for character in case["input"])
    ]

    assert supplementary, (
        "no supplementary-plane tokenizer cases in the contract; a "
        "String.length bug would not be caught"
    )

    for case in supplementary:
        assert tokenize(case["input"]) == case["expected"]
