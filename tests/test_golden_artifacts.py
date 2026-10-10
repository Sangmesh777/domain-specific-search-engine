"""
Golden vector integrity.

These tests keep the checked-in golden vectors honest:

  * ``tests/golden/search_engine_vectors.json``
        every recorded contract case must still be reproduced by the
        engine, so the file cannot silently drift away from the code
        it is supposed to describe

Nothing here needs a running HTTP server.
"""

import json
from pathlib import Path

import pytest

import app

REPO_ROOT = Path(__file__).resolve().parent.parent

VECTORS_PATH = REPO_ROOT / "tests" / "golden" / "search_engine_vectors.json"


@pytest.fixture(scope="module")
def vectors():
    if not VECTORS_PATH.exists():
        pytest.fail(
            f"{VECTORS_PATH.relative_to(REPO_ROOT)} is missing; "
            "regenerate it with 'python -m tools.generate_golden_vectors'"
        )

    with open(VECTORS_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


# ----------------------------------------------------------------------
# Golden vector file
# ----------------------------------------------------------------------


def test_vector_file_has_expected_shape(vectors):
    assert vectors["format"] == "search-engine-golden-vectors"
    assert vectors["version"] == 1
    assert vectors["vectors"], "no corpus vectors recorded"
    assert vectors["empty_corpus_vectors"], "no empty-corpus vectors recorded"
    assert vectors["contract_vectors"], "no contract vectors recorded"


def test_vector_ids_are_unique(vectors):
    identifiers = [
        vector["id"]
        for vector in vectors["vectors"] + vectors["empty_corpus_vectors"]
    ]

    duplicates = sorted(
        identifier
        for identifier in set(identifiers)
        if identifiers.count(identifier) > 1
    )

    assert not duplicates, f"duplicate vector ids: {duplicates}"


def test_corpus_document_names_are_unique(vectors):
    names = vectors["corpus"]["names"]

    assert len(names) == len(set(names))


def test_recorded_index_state_is_consistent(vectors):
    state = vectors["engine_index_state"]

    assert state["documents"] == vectors["corpus"]["document_count"]
    assert state["content_terms"] > 0
    assert state["filenames_indexed"] == state["documents"]
    assert state["page_text_entries"] == state["documents"]


def test_result_objects_never_expose_server_paths(vectors):
    """The absolute server path must not be part of the contract."""

    for vector in vectors["vectors"]:
        response = vector["response"]

        results = response if isinstance(response, list) else response["results"]

        for result in results:
            assert "path" not in result, (
                f"vector {vector['id']} leaked a server filesystem path"
            )


def test_contract_vectors_match_the_engine(vectors):
    """Replaying every recorded case must still reproduce it."""

    contract = vectors["contract_vectors"]
    failures = []

    for case in contract["content_tokenize"]:
        actual = app.tokenize(case["input"])

        if actual != case["expected"]:
            failures.append(
                f"tokenize({case['input']!r}) "
                f"expected {case['expected']!r} but got {actual!r}"
            )

    for case in contract["filename_tokenize"]:
        actual = app.tokenize_filename(case["input"])

        if actual != case["expected"]:
            failures.append(
                f"tokenize_filename({case['input']!r}) "
                f"expected {case['expected']!r} but got {actual!r}"
            )

    for case in contract["sanitize_filename"]:
        actual = app.sanitize_upload_filename(case["input"])

        if actual != case["expected"]:
            failures.append(
                f"sanitize_upload_filename({case['input']!r}) "
                f"expected {case['expected']!r} but got {actual!r}"
            )

    for case in contract["normalize_search_query"]:
        actual = app.normalize_search_query(case["input"])

        if actual != case["expected"]:
            failures.append(
                f"normalize_search_query({case['input']!r}) "
                f"expected {case['expected']!r} but got {actual!r}"
            )

    for case in contract["parse_filetype_filter"]:
        query, filetype = app.parse_filetype_filter(case["input"])

        if (
            query != case["expected_query"]
            or filetype != case["expected_filetype"]
        ):
            failures.append(
                f"parse_filetype_filter({case['input']!r}) "
                f"expected ({case['expected_query']!r}, "
                f"{case['expected_filetype']!r}) but got "
                f"({query!r}, {filetype!r})"
            )

    assert not failures, "\n".join(failures)


def test_rounding_vectors_match_python(vectors):
    """Python round() semantics must be reproduced exactly."""

    failures = []

    for case in vectors["contract_vectors"]["round_half_even"]:
        actual = round(case["value"], case["digits"])

        if actual != case["expected"]:
            failures.append(
                f"round({case['value']!r}, {case['digits']}) "
                f"expected {case['expected']!r} but got {actual!r}"
            )

    assert not failures, "\n".join(failures)


def test_character_class_vectors_match_python(vectors):
    classes = vectors["contract_vectors"]["character_classes"]

    for entry in classes["alnum"]:
        character = chr(int(entry["codepoint"][2:], 16))

        assert character.isalnum() is entry["isalnum"], (
            f"{entry['codepoint']} isalnum mismatch"
        )

    for entry in classes["whitespace"]:
        character = chr(int(entry["codepoint"][2:], 16))

        assert character.isspace() is entry["isspace"], (
            f"{entry['codepoint']} isspace mismatch"
        )


def test_blank_query_shape_is_pinned(vectors):
    """
    The blank-query response is a special published shape with a
    hardcoded page/limit and no start/end keys.
    """

    blank = [
        vector
        for vector in vectors["vectors"]
        if vector["id"].startswith("blank_query_")
    ]

    assert blank, "no blank-query vectors recorded"

    for vector in blank:
        pagination = vector["response"]["pagination"]

        assert vector["response"]["results"] == []
        assert pagination["page"] == 1
        assert pagination["limit"] == 10
        assert pagination["total"] == 0
        assert "start" not in pagination
        assert "end" not in pagination


def test_bare_list_shape_is_pinned(vectors):
    """At least one vector must still exercise the legacy list shape."""

    bare = [
        vector
        for vector in vectors["vectors"] + vectors["empty_corpus_vectors"]
        if isinstance(vector["response"], list)
    ]

    assert bare, "no bare-list vectors recorded"

    for vector in bare:
        assert vector["response"] == []
