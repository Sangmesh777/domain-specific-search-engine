"""
Tests for tools/verify_vector_coverage.py.

That tool edits search_engine/search.py in place, once per numeric constant, to
measure what the golden vectors pin. A tool that perturbs the ranking module is
only safe if it reliably puts it back, so the restoration guarantee is tested
here rather than trusted - a sweep interrupted or raising halfway must not leave
the repository with edited ranking constants.

A full sweep is roughly twenty seconds, which is too slow for the regression
gate, so these tests exercise the helpers and run the sweep only through a
filter narrow enough to stay fast.
"""

import os
import sys

import pytest


TOOLS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools",
)

if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import verify_vector_coverage as coverage      # noqa: E402


def source_text():

    with open(coverage.TARGET, encoding="utf-8") as file:
        return file.read()


def test_it_finds_the_ranking_module():
    """The tool must point at the real ranking source, not a stale path."""

    assert os.path.isfile(coverage.TARGET)
    assert coverage.TARGET.endswith("search_engine/search.py")

    assert os.path.isfile(coverage.VECTOR_FILE)
    assert coverage.VECTOR_FILE.endswith(
        "tests/golden/search_engine_vectors.json"
    )


def test_enumeration_covers_the_known_ranking_weights():
    """
    Enumeration is from the AST, so a constant cannot be missed by forgetting
    to add it to a table. The first hand-written sweep omitted
    `lexical_weight = 0.0` in three branches; this is the regression test for
    that omission.
    """

    _source, found = coverage.constants()

    names = {item["name"] for item in found}

    for expected in (
        "filename_weight",
        "content_weight",
        "phrase_weight",
        "lexical_weight",
    ):
        assert expected in names, expected

    weights = [
        item for item in found
        if item["name"] in (
            "filename_weight",
            "content_weight",
            "phrase_weight",
            "lexical_weight",
        )
    ]

    # Four branches, four weights each.
    assert len(weights) == 16

    assert sum(1 for item in weights if item["name"] == "lexical_weight") == 4


def test_enumeration_excludes_subscript_indices():
    """
    `os.path.splitext(filename)[1]` is positional access, not a tunable
    magnitude. Perturbing it to `[2]` raises IndexError, which the tool would
    otherwise report as a constant it could not measure.
    """

    _source, found = coverage.constants()

    positions = {(item["lineno"], item["col"]) for item in found}

    text = source_text()

    assert "splitext(" in text

    # Every literal in the module that indexes a subscript must be absent.
    import ast

    tree = ast.parse(text)

    excluded = 0

    for node in ast.walk(tree):

        if not isinstance(node, ast.Subscript):
            continue

        for child in ast.walk(node.slice):

            if isinstance(child, ast.Constant) and isinstance(
                child.value, int
            ):
                assert (child.lineno, child.col_offset) not in positions
                excluded += 1

    assert excluded > 0, "expected at least one subscript index to exclude"


def test_perturbation_is_coarse_enough_to_cross_a_clamp_band():
    """
    A perturbation too small to cross the band a clamp guards reports that
    clamp as pinned when nothing observes it. Moving 0.25 to 0.26 changes
    nothing unless a similarity lands between them, so the step has a floor.
    """

    assert coverage.perturbed(0.25) == pytest.approx(0.30)
    assert coverage.perturbed(0.90) == pytest.approx(0.99)
    assert coverage.perturbed(0.65) == pytest.approx(0.715)

    # Zero has no magnitude to scale, so the absolute floor carries it.
    assert coverage.perturbed(0.0) == pytest.approx(0.05)

    # Large values move proportionally.
    assert coverage.perturbed(100.0) == pytest.approx(110.0)

    # Integers step by one, which is how the IDF offset gets measured.
    assert coverage.perturbed(1) == 2
    assert coverage.perturbed(4) == 5

    # And every perturbation actually changes the value.
    for value in (0.0, 0.05, 0.25, 0.7, 0.9, 1.0, 25.0, 100.0, 1, 4):
        assert coverage.perturbed(value) != value, value


def test_replacement_text_stays_valid_python():
    """
    An integer perturbed to a float must keep its decimal point, or `+ 1`
    becoming `+ 1.5` would truncate back to an integer and measure nothing.
    """

    assert coverage.literal_text(1, 2) == "2"
    assert coverage.literal_text(1, 1.5) == "1.5"
    assert coverage.literal_text(0.25, 0.30) == "0.3"

    # Every rendered replacement must parse as a Python literal.
    for text in (
        coverage.literal_text(1, coverage.perturbed(1)),
        coverage.literal_text(0.0, coverage.perturbed(0.0)),
        coverage.literal_text(0.75, coverage.perturbed(0.75)),
        coverage.literal_text(100.0, coverage.perturbed(100.0)),
    ):
        assert isinstance(eval(text), (int, float)), text


def test_sweep_restores_the_ranking_module_byte_for_byte():
    """
    The safety property the whole tool rests on. It edits the ranking source in
    place and must put it back exactly, including when a filter matches nothing
    and when it matches something.
    """

    before = source_text()

    _committed, rows = coverage.sweep(filter_text="lexical_weight")

    assert rows, "the filter should have matched the lexical weights"

    assert source_text() == before, (
        "verify_vector_coverage left search_engine/search.py modified"
    )


def test_sweep_with_an_impossible_filter_changes_nothing():
    """An empty sweep must also leave the module untouched."""

    before = source_text()

    _committed, rows = coverage.sweep(
        filter_text="zzz-no-such-constant-zzz"
    )

    assert rows == []

    assert source_text() == before


def test_a_pinned_constant_reports_as_differing():
    """
    End to end, through a filter narrow enough to stay quick: perturbing a
    weight the vectors do observe must report DIFFERS. Without this the tool
    could be reporting IDENTICAL for everything and look like it worked.
    """

    before = source_text()

    try:

        _committed, rows = coverage.sweep(
            filter_text="content_weight"
        )

        outcomes = {outcome for _item, outcome in rows}

        assert "DIFFERS" in outcomes, rows

        assert not any(
            outcome.startswith("ERROR") for _item, outcome in rows
        ), rows

    finally:

        assert source_text() == before
