"""
Drift gate for the golden vectors.

tests/golden/search_engine_vectors.json is the contract an Android backend
(or any reimplementation) is checked against. It is generated from the
engine, never written by hand. This test regenerates it and compares, so a
change in ranking behavior cannot silently invalidate the published
contract: the vectors only move when a human reruns the exporter and
commits the result deliberately.

Also asserts the invariants that make the file usable outside this
repository - portable paths, self-consistent references, and a case list
that actually covers the branches it claims to.
"""

import json
import os

from export_golden_vectors import (
    CASES,
    OUTPUT,
    PLACEHOLDER,
    build_vectors,
    render,
)


def committed():

    with open(OUTPUT, encoding="utf-8") as file:
        return file.read()


def test_vectors_match_the_current_engine():
    """
    The committed vectors must equal what the engine produces today.

    If this fails, ranking changed. Either the change is intended - rerun
    tools/export_golden_vectors.py and commit the result - or it is a
    regression. What must not happen is the file drifting because nobody
    noticed.
    """

    assert render(build_vectors()) == committed(), (
        "golden vectors are stale; rerun "
        ".venv/bin/python tools/export_golden_vectors.py and review the diff"
    )


def test_vectors_are_deterministic():
    """
    Two generations must be byte-identical.

    A contract that varies between runs is not a contract, and would make
    every downstream comparison meaningless.
    """

    assert render(build_vectors()) == render(build_vectors())


def test_no_local_paths_leak_into_the_vectors():
    """
    Absolute paths are replaced with a placeholder.

    Without that the file would embed a temporary directory name and could
    not be compared against an Android device's storage.
    """

    text = committed()

    assert PLACEHOLDER in text

    for forbidden in ("/tmp/", "/home/", "pytest-of"):
        assert forbidden not in text, forbidden


def test_every_referenced_document_is_in_the_recorded_index():

    vectors = json.loads(committed())

    indexed = set(vectors["index"]["documents"])

    assert indexed

    for case in vectors["cases"]:

        expected = case["expected"]

        results = expected["results"] if isinstance(expected, dict) else expected

        for item in results:

            assert item["title"] in indexed, (
                f"case {case['name']!r} returned {item['title']!r}, which "
                f"is not in the recorded index"
            )

            assert PLACEHOLDER in item["path"], (
                f"case {case['name']!r} has a non-portable path: {item['path']!r}"
            )


def test_corpus_declares_how_each_file_is_stored():
    """
    Import sanitizes names, so a consumer must know the stored form.

    "Network Notes.pdf" is indexed as "Network_Notes.pdf". A port that
    expected the original spelling would look for a document that does not
    exist and conclude its indexing was broken.
    """

    vectors = json.loads(committed())

    indexed = set(vectors["index"]["documents"])

    for entry in vectors["corpus"]:

        assert entry["stored_as"] in indexed, (
            f"{entry['filename']!r} claims to be stored as "
            f"{entry['stored_as']!r}, which is not in the index"
        )

    assert len(indexed) == len(vectors["corpus"])


def test_every_declared_case_is_present():

    vectors = json.loads(committed())

    names = [case["name"] for case in vectors["cases"]]

    assert len(names) == len(set(names)), "duplicate case names"

    assert names == [name for name, _, _, _ in CASES]


def test_the_branches_the_vectors_claim_to_cover_are_covered():
    """
    Check the file against its own documentation.

    The exporter says which branches are vectorized and which are left to
    the core tests. If a claim stops being true - a query that used to reach
    the bare-list branch stops doing so - the vectors would quietly cover
    less than they say, so assert the shapes actually observed.
    """

    vectors = json.loads(committed())

    by_name = {case["name"]: case["expected"] for case in vectors["cases"]}

    # The one reachable bare-list branch.
    assert by_name["quoted phrase matching nothing"] == []

    # Its asymmetry: a filetype filter with keywords that match nothing is
    # an empty page object, not a bare list.
    empty_page = by_name["filetype plus keyword that matches nothing"]

    assert isinstance(empty_page, dict)
    assert empty_page["results"] == []

    # The blank query keeps the historical shape with no start/end offsets.
    blank = by_name["blank query"]

    assert blank["results"] == []
    assert "start" not in blank["pagination"]
    assert "end" not in blank["pagination"]

    # Filetype-only queries browse rather than rank.
    browse = by_name["filetype only pdf"]

    assert browse["results"]
    assert all(
        item["tag"] == "Filtered Result"
        for item in browse["results"]
    )

    # Ranked queries carry the full scoring breakdown.
    ranked = by_name["exact filename"]

    assert ranked["results"]
    assert all(
        item["tag"] == "Ranked Result"
        for item in ranked["results"]
    )

    # Clamping policy, asserted through the payload a consumer would see.
    assert by_name["limit zero clamps to one"]["pagination"]["limit"] == 1
    assert by_name["limit above maximum clamps"]["pagination"]["limit"] == 50
    assert by_name["page zero clamps to one"]["pagination"]["page"] == 1

    beyond = by_name["page beyond the end clamps"]["pagination"]

    assert beyond["page"] == beyond["total_pages"]


def test_the_vectors_exercise_filename_word_order():
    """
    The corpus must be able to detect a permuted filename word order.

    That defect - filename words persisted through a set() and compared as a
    space-joined string - is invisible unless a document has enough words
    that a permutation breaks a substring match. This asserts the vectors
    really contain such a case, so a port is checked against it.
    """

    vectors = json.loads(committed())

    words = vectors["index"]["filename_words"]

    longest = max(words.values(), key=len)

    assert len(longest) >= 5, (
        "no document has enough filename words for word order to matter"
    )

    by_name = {case["name"]: case for case in vectors["cases"]}

    exact = by_name["five word filename exact"]["expected"]

    assert exact["results"]

    top = exact["results"][0]

    # Full marks require the exact-name bonus, which only applies when the
    # joined words are in the order the filename is written.
    assert top["filename_score"] >= 100

    partial = by_name["five word filename partial"]["expected"]

    assert partial["results"][0]["filename_score"] >= 50

    # A non-contiguous pair must NOT earn the substring bonus. This is the
    # false positive that a permuted word order creates by accident.
    non_contiguous = by_name["five word filename non contiguous"]["expected"]

    assert non_contiguous["results"][0]["filename_score"] < 50


def test_the_vectors_are_small_enough_to_keep_in_git():
    """
    A contract nobody commits is a contract nobody has.

    Kept deliberately modest so it stays reviewable in a diff.
    """

    size = os.path.getsize(OUTPUT)

    assert size < 256 * 1024, f"vectors grew to {size} bytes"
