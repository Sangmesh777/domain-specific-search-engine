"""
Tests for tools/verify_vector_coverage.py.

That tool edits search_engine/search.py in place, once per numeric constant, to
measure what the golden vectors pin. A tool that perturbs the ranking module is
only safe if it reliably puts it back, so the restoration guarantee is tested
here rather than trusted - a sweep interrupted or raising halfway must not leave
the repository with edited ranking constants.

A full sweep is roughly thirty seconds, so the helper tests here exercise the
sweep only through filters narrow enough to stay fast - with exactly one
deliberate exception. test_full_sweep_taxonomy_is_exact runs the whole sweep
and gates the published classification of every unpinned constant (74 pinned,
A 17, B 5, C 10, D 0, E 0, nothing stale, drifted, or unclassified). That is
the enforcement behind "never silently leave live behavior uncovered": the
slow gate is the point, and any legitimate change that moves those numbers
must update the test and ANDROID.md section 9 deliberately.
"""

import os
import sys

import pytest


TOOLS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools",
)

ROOT = os.path.dirname(TOOLS)

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


def test_classification_map_matches_the_live_source():
    """
    Static staleness guard, fast enough to run every time: every classified
    line must still hold a constant with the expected value and anchor text.
    A map that outlives the code it describes would otherwise classify
    whatever now happens to sit on that line.
    """

    _source, found = coverage.constants()

    by_line = {}

    for item in found:
        by_line.setdefault(item["lineno"], []).append(item)

    assert coverage.CLASSIFICATIONS, "the map must not be empty"

    for lineno, entry in sorted(coverage.CLASSIFICATIONS.items()):

        assert lineno in by_line, (
            f"line {lineno} holds no numeric constant anymore"
        )

        assert entry["category"] in coverage.CATEGORY_LABELS

        match = [
            item for item in by_line[lineno]
            if item["value"] == entry["value"]
            and entry["anchor"] in item["line"]
            and (
                "name" not in entry
                or item["name"] == entry["name"]
            )
        ]

        assert match, (
            f"line {lineno}: no constant {entry['value']!r} "
            f"with anchor {entry['anchor']!r}"
        )


def test_no_live_behavior_is_left_uncovered():
    """
    Category D - live, reachable, pinned by nothing - must be empty. That is
    the whole point of the taxonomy, so it is asserted directly rather than
    only through the full-sweep counts.
    """

    uncovered = [
        lineno
        for lineno, entry in coverage.CLASSIFICATIONS.items()
        if entry["category"] == "D"
    ]

    assert uncovered == [], (
        f"live constants pinned by nothing: {uncovered}"
    )


def test_every_c_classification_names_real_tests():
    """
    A category-C entry claims an engine test pins the constant. If that test
    is deleted or renamed, the claim rots silently - so every cited name must
    exist in the core suite.
    """

    path = os.path.join(
        ROOT, "tests", "test_search_engine_core.py"
    )

    with open(path, encoding="utf-8") as file:
        suite = file.read()

    cited = 0

    for lineno, entry in sorted(coverage.CLASSIFICATIONS.items()):

        if entry["category"] != "C":
            continue

        assert entry.get("tests"), (
            f"line {lineno}: category C without test names"
        )

        for name in entry["tests"]:
            assert f"def {name}(" in suite, (
                f"line {lineno} cites missing test {name}"
            )
            cited += 1

    assert cited >= 10, cited


def test_full_sweep_taxonomy_is_exact():
    """
    The slow gate (~30 s): run the entire perturbation sweep and hold the
    published accounting. Any legitimate change to search.py or the vectors
    that moves these numbers must update this test and ANDROID.md section 9
    deliberately - the failure message is the reminder.
    """

    before = source_text()

    try:

        _committed, rows = coverage.sweep()

        report = coverage.classify(rows)

    finally:

        assert source_text() == before, (
            "the full sweep left search_engine/search.py modified"
        )

    assert report["errors"] == [], report["errors"]
    assert report["stale"] == [], report["stale"]
    assert report["drifted"] == [], report["drifted"]
    assert report["unclassified"] == [], report["unclassified"]

    assert report["pinned"] == 74, report["pinned"]

    counts = {
        category: len(entries)
        for category, entries in report["categorized"].items()
    }

    assert counts == {"A": 17, "B": 5, "C": 10, "D": 0, "E": 0}, counts
