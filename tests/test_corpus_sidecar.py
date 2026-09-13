"""
Tests for the pre-extracted corpus sidecar.

artifacts/android/corpus_sidecar.json is what the offline Android backend
ships instead of a PDF parser. These tests check two things: that the
committed artifact still matches what this engine extracts, and - the part
that matters - that an engine built purely from that artifact, with no
extraction library involved, answers queries exactly like an engine built
from the real files.

The second test is the one that justifies the whole strategy. If indexing
pre-extracted text ranked differently from indexing files, shipping text
would buy portability at the cost of parity, and the dual-mode product would
show different results depending on which mode the user was in.
"""

import json
import os
import shutil
import tempfile

from export_corpus_sidecar import (
    DEFAULT_OUTPUT,
    build_sidecar,
    render,
)

from seed_dev_corpus import build_corpus

from search_engine.config import (
    EngineConfig,
)
from search_engine.engine import (
    SearchEngine,
)


QUERIES = [
    "",
    "network",
    "network security",
    "Network Security Notes.pdf",
    '"Network Security Notes.pdf"',
    '"machine learning"',
    "BCS502",
    "module 2",
    "502 module",
    "Cyber Security",
    "cyber fundamentals",
    "Data Structures and Algorithms",
    "normalization",
    "999",
    "pdf",
    "docx",
    "txt",
    "the",
    "zzzqqq",
]


def sidecar():

    with open(DEFAULT_OUTPUT, encoding="utf-8") as file:
        return json.load(file)


def canonical(payload, data_folder):
    """Compare payloads from two engines in two different directories."""

    return json.dumps(
        payload,
        sort_keys=True,
    ).replace(
        data_folder,
        "{{data_folder}}",
    )


def test_artifact_matches_what_the_engine_extracts():
    """
    The committed artifact must equal a fresh generation.

    If extraction or the corpus generator changes, this fails and the
    artifact has to be regenerated deliberately - the same discipline as the
    golden vectors, for the same reason.
    """

    temporary = tempfile.mkdtemp()

    try:

        corpus = os.path.join(temporary, "corpus")

        build_corpus(corpus)

        fresh = render(build_sidecar(corpus))

    finally:

        shutil.rmtree(temporary, ignore_errors=True)

    with open(DEFAULT_OUTPUT, encoding="utf-8") as file:
        committed = file.read()

    assert fresh == committed, (
        "corpus sidecar is stale; rerun "
        ".venv/bin/python tools/export_corpus_sidecar.py and review the diff"
    )


def test_artifact_is_deterministic():

    temporary = tempfile.mkdtemp()

    try:

        corpus = os.path.join(temporary, "corpus")

        build_corpus(corpus)

        assert render(build_sidecar(corpus)) == render(
            build_sidecar(corpus)
        )

    finally:

        shutil.rmtree(temporary, ignore_errors=True)


def test_artifact_declares_every_document_completely():
    """
    A device reads this file without asking questions, so every field the
    indexing path needs must be present and self-consistent.
    """

    data = sidecar()

    assert data["schema_version"] == 1
    assert data["document_count"] == len(data["documents"])
    assert data["document_count"] > 0
    assert data["skipped"] == []

    # All three supported types are represented, so a port cannot pass while
    # silently mishandling one of them.
    extensions = {
        document["extension"]
        for document in data["documents"]
    }

    assert extensions == {"pdf", "docx", "txt"}

    stored = [
        document["stored_as"]
        for document in data["documents"]
    ]

    assert stored == sorted(stored), "documents must be sorted by stored name"
    assert len(set(stored)) == len(stored), "duplicate stored names"

    for document in data["documents"]:

        assert document["stored_as"]
        assert document["title"] == document["stored_as"]
        assert document["source_filename"]
        assert isinstance(document["text"], str)
        assert document["text"].strip()

        # Text is the lowercased content view; a port that ships the original
        # casing would tokenize differently.
        assert document["text"] == document["text"].lower()

        pages = document["pages"]

        assert pages
        assert [page["page"] for page in pages] == list(
            range(1, len(pages) + 1)
        )

        expected = document["expected"]

        assert expected["page_count"] == len(pages)
        assert expected["total_words"] > 0
        assert expected["distinct_terms"] > 0
        assert expected["distinct_terms"] <= expected["total_words"]
        assert expected["filename_words"]

        # PDFs are page-accurate; DOCX and TXT are one logical page. That is
        # existing product behavior, recorded so a port reproduces it.
        if document["extension"] == "pdf":
            assert expected["page_count"] >= 1
        else:
            assert expected["page_count"] == 1


def test_an_engine_built_from_the_artifact_answers_like_the_real_one():
    """
    The end-to-end claim: index the sidecar's text with no extraction library
    in the loop, and get the same answers as indexing the files.

    One engine imports the generated corpus through the normal path, which
    parses every PDF and DOCX. The other never touches the files at all - it
    is fed the artifact's text and pages through index_extracted, which is
    exactly what the offline Android backend does. Their indexes and their
    answers must be identical.

    The artifact-fed engine has no files on disk, on purpose: that is the
    point being proved, so assert_consistent (which requires every indexed
    document to exist) is not applied to it.
    """

    temporary = tempfile.mkdtemp()

    try:

        corpus_directory = os.path.join(temporary, "corpus")

        build_corpus(corpus_directory)

        corpus_paths = [
            os.path.join(corpus_directory, name)
            for name in sorted(os.listdir(corpus_directory))
        ]

        files_folder = os.path.abspath(
            os.path.join(temporary, "from_files")
        )

        artifact_folder = os.path.abspath(
            os.path.join(temporary, "from_artifact")
        )

        os.makedirs(files_folder, exist_ok=True)
        os.makedirs(artifact_folder, exist_ok=True)

        from_files = SearchEngine(
            EngineConfig(data_folder=files_folder)
        )

        from_artifact = SearchEngine(
            EngineConfig(data_folder=artifact_folder)
        )

        # The real path: parse and index every document.
        from_files.import_paths(
            corpus_paths,
            log=lambda *args: None,
        )

        # The Android path: no parsing, only the shipped text.
        data = sidecar()

        for document in data["documents"]:

            from_artifact.index_extracted(
                document["stored_as"],
                document["text"],
                document["pages"],
            )

        assert_consistent_counts(from_files, from_artifact)

        # Same index, structure by structure.
        source = from_files.snapshot()
        target = from_artifact.snapshot()

        assert source.inverted_index == target.inverted_index
        assert source.filename_index == target.filename_index
        assert source.page_text_index == target.page_text_index
        assert set(source.document_metadata) == set(
            target.document_metadata
        )

        # Same answers for every query.
        for query in QUERIES:

            assert canonical(
                from_files.search(query),
                files_folder,
            ) == canonical(
                from_artifact.search(query),
                artifact_folder,
            ), query

    finally:

        shutil.rmtree(temporary, ignore_errors=True)


def assert_consistent_counts(from_files, from_artifact):
    """Same corpus size and the same documents, by name."""

    assert from_files.counts() == from_artifact.counts()

    assert sorted(from_files.snapshot().document_metadata) == sorted(
        from_artifact.snapshot().document_metadata
    )


def test_the_artifact_expected_block_is_self_validating():
    """
    The `expected` block must match what the engine derives from the text.

    A ported tokenizer runs the same computation on device and compares, so
    these values have to be right or the fixture will teach the port the
    wrong thing. Checking them against the engine here is what makes them a
    contract rather than a claim.
    """

    from search_engine.tokenizer import (
        tokenize,
        tokenize_filename,
    )

    data = sidecar()

    for document in data["documents"]:

        expected = document["expected"]

        terms = tokenize(document["text"])

        assert expected["total_words"] == len(terms), (
            document["stored_as"]
        )

        assert expected["distinct_terms"] == len(set(terms)), (
            document["stored_as"]
        )

        assert expected["filename_words"] == tokenize_filename(
            os.path.splitext(document["stored_as"])[0]
        ), document["stored_as"]

        assert expected["page_count"] == len(document["pages"]), (
            document["stored_as"]
        )


def test_the_artifact_survives_a_restart():
    """
    Offline means the index is still there when the app reopens.

    An artifact-fed index must persist to SQLite and reload identically,
    which is what makes "close the app, search again" work on a device with
    no network.
    """

    temporary = tempfile.mkdtemp()

    try:

        folder = os.path.abspath(
            os.path.join(temporary, "data")
        )

        os.makedirs(folder, exist_ok=True)

        config = EngineConfig(data_folder=folder)

        engine = SearchEngine(config)

        data = sidecar()

        for document in data["documents"]:

            engine.index_extracted(
                document["stored_as"],
                document["text"],
                document["pages"],
            )

        before = [
            canonical(engine.search(query), folder)
            for query in QUERIES
        ]

        reloaded = SearchEngine(config)

        after = [
            canonical(reloaded.search(query), folder)
            for query in QUERIES
        ]

        assert before == after

        assert reloaded.counts() == engine.counts()

    finally:

        shutil.rmtree(temporary, ignore_errors=True)
