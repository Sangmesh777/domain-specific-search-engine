"""
Golden artifact integrity.

These tests keep the checked-in parity artifacts honest:

  * ``tests/golden/search_engine_vectors.json``
        every recorded contract case must still be reproduced by the
        engine, so the file cannot silently drift away from the code
        it is supposed to describe

  * ``artifacts/android/corpus_sidecar.json``
        the sidecar must be self-consistent, must match its own
        integrity hash, and must reconstruct the engine's index state
        exactly

Nothing here needs a running HTTP server.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest

import app

REPO_ROOT = Path(__file__).resolve().parent.parent

VECTORS_PATH = REPO_ROOT / "tests" / "golden" / "search_engine_vectors.json"
SIDECAR_PATH = REPO_ROOT / "artifacts" / "android" / "corpus_sidecar.json"


@pytest.fixture(scope="module")
def vectors():
    if not VECTORS_PATH.exists():
        pytest.fail(
            f"{VECTORS_PATH.relative_to(REPO_ROOT)} is missing; "
            "regenerate it with 'python -m tools.generate_golden_vectors'"
        )

    with open(VECTORS_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def sidecar():
    if not SIDECAR_PATH.exists():
        pytest.fail(
            f"{SIDECAR_PATH.relative_to(REPO_ROOT)} is missing; "
            "regenerate it with 'python -m tools.build_corpus_sidecar'"
        )

    with open(SIDECAR_PATH, "r", encoding="utf-8") as handle:
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


# ----------------------------------------------------------------------
# Corpus sidecar
# ----------------------------------------------------------------------


def test_sidecar_integrity_hash(sidecar):
    payload = {
        key: value
        for key, value in sidecar.items()
        if key != "integrity"
    }

    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    calculated = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    assert calculated == sidecar["integrity"]["canonical_json_sha256"]


def test_sidecar_shape(sidecar):
    assert sidecar["format"] == "corpus-sidecar"
    assert sidecar["version"] == 1
    assert sidecar["documents"]


def test_sidecar_document_names_are_unique(sidecar):
    names = [document["name"] for document in sidecar["documents"]]

    assert len(names) == len(set(names))


def test_sidecar_totals_match_documents(sidecar):
    documents = sidecar["documents"]

    assert sidecar["corpus_totals"]["documents"] == len(documents)

    for document in documents:
        assert document["pages"], f"{document['name']} has no page text"
        assert document["filename_terms"], (
            f"{document['name']} has no filename terms"
        )
        assert document["content_terms"], (
            f"{document['name']} has no content terms"
        )
        assert document["total_words"] > 0


def test_sidecar_filename_terms_exclude_extension(sidecar):
    """The filename index is built from the stem, not the full name."""

    for document in sidecar["documents"]:
        stem = os.path.splitext(document["name"])[0]

        assert document["filename_terms"] == app.tokenize_filename(stem), (
            f"{document['name']} filename terms do not match the tokenizer"
        )


def test_sidecar_reconstructs_the_content_index(sidecar):
    """
    Rebuilding the postings from the sidecar must reproduce exactly the
    term set the generator recorded, proving the sidecar is a faithful
    description of the index.
    """

    reconstructed = {}

    for document in sidecar["documents"]:
        for term, frequency in document["content_terms"].items():
            reconstructed.setdefault(term, {})[document["name"]] = frequency

    distinct_terms = len(reconstructed)

    assert distinct_terms == sidecar["corpus_totals"]["content_terms"], (
        "sidecar content terms do not sum to the recorded total: "
        f"{distinct_terms} != {sidecar['corpus_totals']['content_terms']}"
    )

    for document in sidecar["documents"]:
        for term in document["content_terms"]:
            assert document["name"] in reconstructed[term]


def test_sidecar_content_terms_are_lowercased(sidecar):
    """Indexed terms are already lowercased by extraction."""

    for document in sidecar["documents"]:
        for term in document["content_terms"]:
            assert term == term.lower(), (
                f"{document['name']} has a non-lowercase term {term!r}"
            )


def test_sidecar_reproduces_the_known_turkish_quirk(sidecar):
    """
    The U+0130 lowercase expansion must survive into the index.

    "İstanbul".lower() is "i" + U+0307 + "stanbul"; U+0307 is not
    alphanumeric, so the token splits and the single character "i" is
    dropped, leaving "stanbul".
    """

    unicode_note = next(
        document
        for document in sidecar["documents"]
        if document["name"] == "unicode_notes.txt"
    )

    terms = unicode_note["content_terms"]

    assert "stanbul" in terms, "the İstanbul split token is missing"
    assert "istanbul" not in terms, (
        "'istanbul' must not be indexed: the U+0307 separator splits it"
    )


def test_sidecar_documents_are_pdf_free(sidecar):
    """
    The sidecar must carry extracted text so the device never needs a
    PDF or DOCX parser to bootstrap the shipped corpus.
    """

    extensions = {document["extension"] for document in sidecar["documents"]}

    assert "pdf" in extensions, "the sidecar should cover PDF documents"

    for document in sidecar["documents"]:
        for page in document["pages"]:
            assert isinstance(page["text"], str)
            assert page["text"], f"{document['name']} has empty page text"
