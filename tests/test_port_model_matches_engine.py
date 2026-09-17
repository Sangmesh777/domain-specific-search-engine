"""
Differential test: the port model against the engine it claims to model.

`tools/port_model.py` is the specification the Kotlin sources were
written from, and `tests/test_port_model.py` checks it against the
recorded contract vectors. That check has a hole: the vectors contain a
single backslash filename, `..\\..\\x.txt`, and both the model and the
engine happen to return `x.txt` for it, so the vectors cannot see the
difference. Comparing the model only against vectors generated from the
engine means a model that disagrees with the engine passes whenever the
recorded cases are not discriminating.

This test closes the hole by comparing the model to the engine directly
over an adversarial corpus. Every divergence is a filename that would
be stored and tokenized differently on Android than on the server,
which is exactly the cross-mode drift the port must not have.
"""

import pytest

from search_engine.engine import search_index as engine_search_index
from search_engine.sanitize import sanitize_upload_filename
from search_engine.text import (
    normalize_search_query as engine_normalize_query,
    parse_filetype_filter as engine_parse_filetype,
    tokenize_filename,
)
from tools import port_model

CORPUS = [
    # Both separators, in every arrangement that changes meaning.
    "dir\\sub\\file.txt",
    "dir/sub/file.txt",
    "..\\..\\etc\\passwd.txt",
    "../../etc/passwd.txt",
    "C:\\Users\\me\\report.pdf",
    "\\\\server\\share\\doc.docx",
    "dir\\..\\..\\x.txt",
    "\\leading.txt",
    "trailing\\",
    "a\\b.txt",
    "a/b.txt",
    "a//b.txt",
    "a\\\\b.txt",
    "mixed\\dir/sub\\leaf.txt",
    "..\\..\\x.txt",
    "..\\x.txt",
    "sub\\..\\name.txt",
    # Ordinary names that must not be disturbed by the fix.
    "a.txt",
    "My File.pdf",
    "café.txt",
    "Ünïcödé Ñämé.docx",
    "  spaced  .txt",
    ".hidden.txt",
    "...hidden.txt",
    "weird.pdf.txt",
    "no-ext",
    "archive.zip",
    "",
    "._.txt",
    "CON.txt",
    "nul.pdf",
    "a!b.txt",
    "file-name.txt",
    "a___b.txt",
    "__lead.txt",
    "trail__.txt",
    "-lead.txt",
    "MiXeD CaSe.TXT",
    "100%.txt",
    "a  b.txt",
]


def _separator_blind_basename(filename):
    """
    The bug this corpus exists to catch, written out in full.

    This is what the model used to do and what the Kotlin source still
    did: treat both separators as directory separators. It is kept here
    so the corpus can be shown to discriminate against the bug class
    even once the model has been fixed, which is the only way to know
    the comparison below has any power.
    """

    end = len(filename)

    while end > 0:
        if filename[end - 1] in ("/", "\\"):
            break
        end -= 1

    return filename[end:]


def _engine_basename(filename):
    """The basename the engine actually sees, i.e. os.path.basename."""

    import os

    return os.path.basename(filename)


def test_the_corpus_is_able_to_detect_a_separator_blind_basename():
    """
    Guard the guard.

    Assert that at least one case separates "split on both separators"
    from "split on `/` only", and that at least one case would produce a
    different stored filename. Had the corpus contained only the
    recorded vector `..\\..\\x.txt`, it would have proven nothing,
    because that input sanitizes to `x.txt` both ways.
    """

    differ_on_basename = [
        name
        for name in CORPUS
        if _separator_blind_basename(name) != _engine_basename(name)
    ]

    assert differ_on_basename, (
        "no case separates the buggy basename from the real one, so the "
        "comparison below cannot detect the drift it exists to detect"
    )

    differ_on_stored_name = [
        name
        for name in CORPUS
        if port_model.secure_filename(_separator_blind_basename(name))
        != sanitize_upload_filename(name)
        and _engine_basename(name) != name
    ]

    assert differ_on_stored_name, differ_on_basename

    # The exact claim: a Windows-shaped path stores differently.
    assert _separator_blind_basename("dir\\sub\\file.txt") == "file.txt"
    assert _engine_basename("dir\\sub\\file.txt") == "dir\\sub\\file.txt"
    assert sanitize_upload_filename("dir\\sub\\file.txt") == "dirsubfile.txt"


@pytest.mark.parametrize("filename", CORPUS)
def test_model_basename_matches_os_path_basename(filename):
    """
    On POSIX, only `/` separates. The model must not treat `\\` as one.
    """

    assert port_model.basename(filename) == _engine_basename(filename), (
        f"{filename!r}: the model splits on backslash, the engine does "
        f"not, so the port would store a different document name"
    )


@pytest.mark.parametrize("filename", CORPUS)
def test_model_sanitizer_matches_the_engine_sanitizer(filename):
    """The public sanitizer, over the whole corpus."""

    assert port_model.sanitize(filename) == sanitize_upload_filename(
        filename
    ), f"{filename!r} sanitizes differently in the port model"


@pytest.mark.parametrize("filename", CORPUS)
def test_model_filename_tokens_match_the_engine(filename):
    """
    A different stored name is a different token stream, so compare the
    tokens too: filename tokens are persisted with positions and are
    searchable, so drift here is visible to a user.
    """

    engine_name = sanitize_upload_filename(filename)

    model_name = port_model.sanitize(filename)

    assert port_model.tokenize_filename(model_name) == tokenize_filename(
        engine_name
    ), f"{filename!r} tokenizes differently in the port model"


def test_the_recorded_contract_can_detect_the_separator_blind_bug():
    """
    The vectors are the Android harness's only oracle.

    Android cannot run the Python engine, so `ContractParityTest.kt`
    compares Kotlin against `tests/golden/search_engine_vectors.json`
    and nothing else. A hole in the vectors is therefore a hole in the
    port's verification, no matter how thorough the Python-side tests
    are. This asserts the recorded contract itself separates the buggy
    behaviour from the correct one, so the gap cannot silently reopen
    if someone trims the input list.
    """

    import json
    from pathlib import Path

    vectors_path = (
        Path(__file__).resolve().parent / "golden" / "search_engine_vectors.json"
    )

    with open(vectors_path, "r", encoding="utf-8") as handle:
        contract = json.load(handle)["contract_vectors"]["sanitize_filename"]

    recorded = {case["input"]: case["expected"] for case in contract}

    assert len(recorded) == len(contract), "duplicate inputs in the contract"

    discriminating = {
        name: expected
        for name, expected in recorded.items()
        if _separator_blind_basename(name) != _engine_basename(name)
        and port_model.secure_filename(_separator_blind_basename(name))
        != expected
    }

    assert discriminating, (
        "the recorded contract contains no case where treating a "
        "backslash as a separator changes the stored filename, so the "
        "Android parity suite could pass while the port stores "
        "different document names than the server"
    )

    assert recorded["dir\\sub\\file.txt"] == "dirsubfile.txt"
    assert "_" not in recorded["dir\\sub\\file.txt"]


@pytest.mark.parametrize(
    "query",
    [
        "BCS502 Module 2.pdf",
        "BCS502 Module 2.docx",
        "notes.txt",
        "plain query",
        '"BCS502 Module 2.pdf"',
        "data.csv",
        "",
        "   ",
        "nested.pdf.txt",
        '"quoted query"',
    ],
)
def test_model_normalizes_query_identically_to_engine(query):
    assert port_model.normalize_search_query(query) == engine_normalize_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "pdf",
        ".pdf",
        "network pdf",
        "network .pdf",
        "BCS502 Module 2.pdf",
        '"BCS502 Module 2.pdf"',
        "notes docx",
        "notes txt",
        "plain query",
        "",
        "notes.txt",
        "deep learning .docx",
        'file.pdf"',
    ],
)
def test_model_parses_filetype_filter_identically_to_engine(query):
    assert port_model.parse_filetype_filter(query) == engine_parse_filetype(query)


def test_model_search_index_matches_engine_search_index():
    inv, meta, fn_idx, pg_idx = port_model.load_sidecar_indexes()
    test_queries = [
        ("network", None, None),
        ("BCS502", "1", "5"),
        ("pdf", None, None),
        ('"network security"', None, None),
        ("999", None, None),
        ("nonexistentwordxyz", None, None),
        ("", None, None),
    ]
    for q, page, limit in test_queries:
        model_res = port_model.search_index(q, page, limit, inv, meta, fn_idx, pg_idx)
        engine_res = engine_search_index(q, page, limit, inv, meta, fn_idx, pg_idx)
        assert model_res == engine_res, f"Mismatch on query={q!r}"
