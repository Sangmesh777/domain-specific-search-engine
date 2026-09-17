"""
Shadow parity gate for the module extraction.

Extracting code out of `app.py` is exactly the kind of refactor that
silently changes behaviour. The golden vectors catch a change in
*results*, but only for inputs someone thought to write down.

This gate is stronger: it loads the original function bodies from a
pinned git revision and compares them against the extracted
implementations over the recorded vectors, adversarial inputs, and a
deterministic random corpus that spans the astral plane and the
whitespace ranges where Python and Java disagree.

If the extraction is ever undone, or a function is edited in one place
and not the other, this fails.
"""

import json

import pytest

from tools.shadow_parity import (
    DEFAULT_BASE,
    EXTRACTED,
    ShadowParityError,
    run,
)


@pytest.fixture(scope="module")
def parity():
    try:
        return run(base_revision=DEFAULT_BASE)
    except ShadowParityError as error:
        pytest.skip(f"shadow parity unavailable: {error}")


def test_every_extracted_function_matches_the_original(parity):
    differences = parity["differences"]

    report = "\n".join(
        f"{difference['function']}({difference['input']!r}): "
        f"original={difference['expected']!r} "
        f"extracted={difference['actual']!r}"
        for difference in differences[:20]
    )

    assert not differences, (
        f"{len(differences)} differences between the extracted modules "
        f"and the original app.py:\n{report}"
    )


def test_comparison_actually_ran(parity):
    """Guard against the gate silently doing nothing."""

    assert parity["inputs"] > 1000
    assert parity["functions"] == len(EXTRACTED)
    assert parity["comparisons"] > 5000


def test_all_expected_functions_were_compared(parity):
    """
    Every function moved out of app.py must be under shadow parity.

    If a function is extracted and not added here, the gate silently
    stops covering it.
    """

    assert set(EXTRACTED) == {
        # Layer 1: pure text and sanitizer
        "tokenize",
        "tokenize_filename",
        "normalize_search_query",
        "parse_filetype_filter",
        "sanitize_upload_filename",
        # Layer 2: extraction and snippet scoring
        "count_phrase_occurrences",
        "extract_text",
        "extract_pages",
        "build_snippet_result",
        "get_snippet_and_page",
        # Layer 5: the ranking pipeline
        "build_paginated_response",
        "execute_search",
    }

    assert parity["functions"] == len(EXTRACTED)
