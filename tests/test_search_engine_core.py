"""
Core API tests for SearchEngine.

These drive the engine directly: no Flask, no HTTP, no server, no network.
That is the point. If the product's offline mode is real, then every
business operation must be exercisable without a transport, and these tests
are the proof.

Each operation is checked three ways, because a search engine has three
stores that can drift apart:

    memory      the live in-memory index that serves searches
    SQLite      the persisted index that survives a restart
    filesystem  the documents themselves

`assert_consistent` compares all three after every mutation, which is what
catches the class of bug where a delete "works" in the UI but leaves a
ghost posting behind.
"""

import io
import math
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

from pdf_writer import write_pdf

from search_engine.config import (
    EngineConfig,
)
from search_engine.engine import (
    SearchEngine,
)
from search_engine.pagination import (
    empty_query_response,
)
from search_engine.results import (
    DELETE_DELETED,
    DELETE_FILE_NOT_FOUND,
    DELETE_INVALID_PATH,
    DELETE_NOT_INDEXED,
)
from search_engine.storage import (
    atomic_write_json,
)


# ============================================================
# FIXTURES AND HELPERS
# ============================================================

@pytest.fixture
def data_folder(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    return str(folder)


@pytest.fixture
def engine(data_folder):
    """A fresh engine over an empty data folder."""

    return SearchEngine(
        EngineConfig(data_folder=data_folder)
    )


def make_txt(directory, name, body):

    os.makedirs(directory, exist_ok=True)

    path = os.path.join(directory, name)

    with open(path, "w", encoding="utf-8") as file:
        file.write(body)

    return path


def make_pdf(directory, name, pages):

    os.makedirs(directory, exist_ok=True)

    path = os.path.join(directory, name)

    write_pdf(path, pages)

    return path


def make_docx(directory, name, paragraphs):

    import docx

    os.makedirs(directory, exist_ok=True)

    path = os.path.join(directory, name)

    document = docx.Document()

    for paragraph in paragraphs:
        document.add_paragraph(paragraph)

    document.save(path)

    return path


def import_paths(engine, paths):
    """Import real files through the engine's stream-based API."""

    handles = []

    items = []

    try:

        for path in paths:

            handle = open(path, "rb")

            handles.append(handle)

            items.append((
                os.path.basename(path),
                handle,
            ))

        return engine.import_documents(
            items,
            log=lambda *args: None,
        )

    finally:

        for handle in handles:
            handle.close()


def sample_corpus(tmp_path):
    """A small mixed-format corpus covering every supported type."""

    directory = str(tmp_path / "corpus")

    os.makedirs(directory, exist_ok=True)

    return [
        make_pdf(
            directory,
            "Network Notes.pdf",
            [
                "Computer network fundamentals and the OSI model",
                "Routing protocols such as BGP and OSPF reference 999",
            ],
        ),
        make_docx(
            directory,
            "Database Guide.docx",
            [
                "Normalization removes redundancy",
                "Transactions are atomic consistent isolated durable",
            ],
        ),
        make_txt(
            directory,
            "security.txt",
            "Firewalls filter traffic between network zones\n",
        ),
    ]


def documents_on_disk(engine):
    """Supported documents actually present in the data folder."""

    folder = engine.config.data_folder

    return {
        filename
        for filename in os.listdir(folder)
        if engine.config.is_supported_document(filename)
    }


def scratch_files(engine):
    """Leftover .uploading / .replacing files an import should never keep."""

    folder = engine.config.data_folder

    return {
        filename
        for filename in os.listdir(folder)
        if filename.endswith((".uploading", ".replacing"))
    }


def import_bytes(engine, name, payload):
    """Import raw bytes under a filename, as an upload would."""

    return engine.import_documents(
        [(name, io.BytesIO(payload))],
        log=lambda *_: None,
    )


def sqlite_documents(engine):
    """Filenames present in the SQLite documents table."""

    connection = sqlite3.connect(
        engine.config.sqlite_db_file
    )

    try:

        rows = connection.execute(
            "SELECT filename FROM documents"
        ).fetchall()

    finally:

        connection.close()

    return {row[0] for row in rows}


def sqlite_postings(engine):
    """The full (term, filename, count) relation from SQLite."""

    connection = sqlite3.connect(
        engine.config.sqlite_db_file
    )

    try:

        rows = connection.execute(
            "SELECT term, filename, term_count "
            "FROM term_postings"
        ).fetchall()

    finally:

        connection.close()

    return {
        (term, filename): count
        for term, filename, count in rows
    }


def memory_postings(engine):
    """The same relation as held in memory."""

    snapshot = engine.snapshot()

    return {
        (term, filename): count
        for term, postings in snapshot.inverted_index.items()
        for filename, count in postings.items()
    }


def assert_consistent(engine):
    """
    Assert memory == SQLite == filesystem.

    This is the invariant the whole product rests on: a search served from
    memory, a restart restored from SQLite, and a document opened from disk
    must all describe the same corpus.

    One asymmetry remains, narrowed to rebuild: a supported file already
    sitting in the data folder that has no readable text is skipped by a
    rebuild and so is on disk but not indexed. An upload no longer produces
    that state -- a document that fails extraction leaves nothing behind, so
    the filesystem may hold a superset of the index only through rebuild, and
    every extra file must be provably unreadable. Anything else is a leak and
    fails here.
    """

    from search_engine.extraction import extract_text

    snapshot = engine.snapshot()
    counts = engine.counts()
    tables = engine.sqlite_counts()

    indexed = set(snapshot.document_metadata)

    # --- memory self-consistency ---
    assert counts["documents"] == len(indexed)
    assert set(snapshot.filename_index) == indexed
    assert set(snapshot.page_text_index) == indexed

    # --- memory vs SQLite ---
    assert tables["documents"] == len(indexed)
    assert sqlite_documents(engine) == indexed
    assert sqlite_postings(engine) == memory_postings(engine)

    assert tables["pages"] == sum(
        len(pages)
        for pages in snapshot.page_text_index.values()
    )

    # --- memory vs filesystem ---
    on_disk = documents_on_disk(engine)

    assert indexed <= on_disk

    for filename in sorted(on_disk - indexed):

        path = os.path.join(
            engine.config.data_folder,
            filename,
        )

        assert not extract_text(path, filename).strip(), (
            f"{filename} is on disk but unindexed, and it does "
            "have readable text: that is an index leak"
        )

    for filename, metadata in (
        snapshot.document_metadata.items()
    ):
        assert os.path.isfile(metadata["path"])
        assert metadata["title"] == filename


# ============================================================
# IMPORT / UPLOAD
# ============================================================

def test_import_new_documents(engine, tmp_path):

    result = import_paths(
        engine,
        sample_corpus(tmp_path),
    )

    assert result.created_count == 3
    assert result.replaced_count == 0
    assert result.uploaded_count == 3
    assert result.rejected_count == 0
    assert result.failed_count == 0
    assert result.ok

    counts = engine.counts()

    assert counts["documents"] == 3
    assert counts["filenames_indexed"] == 3
    assert counts["page_text_entries"] == 3
    assert counts["content_terms"] > 0

    assert_consistent(engine)


def test_import_is_atomic_per_batch(engine, tmp_path):
    """
    A whole import batch commits as ONE SQLite transaction.

    This is the property that makes a bulk upload all-or-nothing at the
    database level, so it is asserted directly by counting commits rather
    than inferred from the outcome.
    """

    commits = []

    real_connection = engine.store.connection

    class ConnectionProxy:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, *arguments):
            return self._real.__exit__(*arguments)

        def commit(self):
            commits.append("commit")
            return self._real.commit()

    engine.store.connection = (
        lambda: ConnectionProxy(real_connection())
    )

    try:
        result = import_paths(
            engine,
            sample_corpus(tmp_path),
        )
    finally:
        engine.store.connection = real_connection

    assert result.uploaded_count == 3

    # One commit for the batch. Per-document term-count lookups use their
    # own short-lived connections and never call commit() explicitly.
    assert commits == ["commit"]


def test_import_replacement(engine, tmp_path):

    paths = sample_corpus(tmp_path)

    import_paths(engine, paths)

    before = engine.counts()

    # Rewrite one document with completely different content.
    make_txt(
        str(tmp_path / "corpus"),
        "security.txt",
        "replacementtoken uniquereplacementphrase 4242\n",
    )

    result = import_paths(
        engine,
        [os.path.join(str(tmp_path / "corpus"), "security.txt")],
    )

    assert result.uploaded_count == 1
    assert result.replaced_count == 1
    assert result.created_count == 0
    assert result.failed_count == 0

    assert engine.counts()["documents"] == before["documents"]

    snapshot = engine.snapshot()

    # The old content is gone from the index, the new content is present.
    assert "firewalls" not in snapshot.inverted_index
    assert "replacementtoken" in snapshot.inverted_index

    assert "security.txt" in snapshot.document_metadata

    assert_consistent(engine)


# Bytes no supported extractor can read: invalid UTF-8 for a .txt.
UNREADABLE = bytes(range(128, 256))


def test_failed_new_upload_leaves_nothing_on_disk(engine):
    """
    A document that cannot be extracted must not be written into the corpus.

    Before this was pinned, import renamed the upload into place and only
    then extracted, and the cleanup removed the temporary name -- which the
    rename had already consumed. Every failed upload therefore left an
    orphan file that no endpoint listed, no search reached and nothing ever
    collected.
    """

    result = import_bytes(engine, "brandnew.txt", UNREADABLE)

    assert result.failed_count == 1
    assert result.created_count == 0
    assert result.replaced_count == 0
    assert result.failed[0]["filename"] == "brandnew.txt"

    assert not os.path.exists(
        os.path.join(engine.config.data_folder, "brandnew.txt")
    )

    assert engine.counts()["documents"] == 0
    assert scratch_files(engine) == set()

    assert_consistent(engine)


def test_failed_replacement_restores_the_previous_document(engine):
    """
    A failed re-upload must not destroy the document it was replacing.

    This is the serious half of the same defect: the new bytes were renamed
    over the existing file before extraction, so a rejected replacement
    overwrote a good document. The index kept serving the old content while
    the disk held unreadable bytes, and the next rebuild -- reading the disk
    -- dropped the document entirely. The failure was reported, and the data
    was gone anyway.
    """

    good = b"quarterly revenue growth analysis important"

    first = import_bytes(engine, "report.txt", good)
    assert first.created_count == 1

    counts_before = engine.counts()

    second = import_bytes(engine, "report.txt", UNREADABLE)

    assert second.failed_count == 1
    assert second.replaced_count == 0
    assert second.created_count == 0

    path = os.path.join(engine.config.data_folder, "report.txt")

    # The good bytes are still the bytes on disk.
    with open(path, "rb") as file:
        assert file.read() == good

    # The index is untouched, and still agrees with the disk.
    assert engine.counts() == counts_before

    assert engine.has_document("report.txt")

    # And a rebuild, which reads the disk, keeps the document.
    engine.rebuild()

    assert engine.has_document("report.txt")
    assert engine.counts() == counts_before

    assert scratch_files(engine) == set()

    assert_consistent(engine)


def test_failed_replacement_leaves_search_serving_readable_bytes(engine):
    """
    The invariant that actually matters to a user: if search returns a
    document, opening it must yield the text that matched.

    With the old ordering this broke silently. The stale index still matched
    the document while the file behind it had already been overwritten with
    unreadable bytes, so every hit was a dead link.
    """

    from search_engine.extraction import extract_text

    good = b"quarterly revenue growth analysis important"

    import_bytes(engine, "report.txt", good)

    import_bytes(engine, "report.txt", UNREADABLE)

    page = engine.search("quarterly revenue")

    results = page["results"] if isinstance(page, dict) else page

    assert results, "the good document should still be findable"

    metadata = engine.get_document("report.txt")

    assert metadata is not None

    assert extract_text(
        metadata["path"],
        "report.txt",
    ).strip(), "search returned a document whose bytes are unreadable"


def test_import_leaves_no_scratch_files(engine, tmp_path):
    """
    Neither the .uploading nor the .replacing name may survive a batch,
    whether its documents succeed or fail.

    assert_consistent cannot catch these: it only looks at supported
    extensions, and a scratch file ends in neither. They would accumulate
    forever in the data folder.
    """

    paths = sample_corpus(tmp_path)

    import_paths(engine, paths)

    assert scratch_files(engine) == set()

    # Replace one successfully, and fail one.
    import_bytes(
        engine,
        os.path.basename(paths[0]),
        b"completely different replacement content here",
    )

    import_bytes(engine, os.path.basename(paths[1]), UNREADABLE)

    assert scratch_files(engine) == set()

    assert_consistent(engine)


def test_repeated_failed_replacements_do_not_degrade_the_document(engine):
    """
    The rollback has to be repeatable: every failed attempt must step the
    original aside and put it back again, not consume it on the first try.
    """

    good = b"stable original content that must survive"

    import_bytes(engine, "spec.txt", good)

    path = os.path.join(engine.config.data_folder, "spec.txt")

    for attempt in range(5):

        result = import_bytes(engine, "spec.txt", UNREADABLE)

        assert result.failed_count == 1, attempt

        with open(path, "rb") as file:
            assert file.read() == good, attempt

    assert engine.counts()["documents"] == 1
    assert scratch_files(engine) == set()

    assert_consistent(engine)


def test_a_mixed_batch_still_commits_its_good_documents(engine):
    """
    Cleaning up after failures must not tighten into all-or-nothing: one bad
    document in a batch still leaves the good ones indexed, in one
    transaction.
    """

    result = engine.import_documents(
        [
            ("alpha.txt", io.BytesIO(b"alpha content one")),
            ("broken.txt", io.BytesIO(UNREADABLE)),
            ("beta.txt", io.BytesIO(b"beta content two")),
        ],
        log=lambda *_: None,
    )

    assert result.created == ["alpha.txt", "beta.txt"]
    assert [f["filename"] for f in result.failed] == ["broken.txt"]

    assert engine.counts()["documents"] == 2

    assert not os.path.exists(
        os.path.join(engine.config.data_folder, "broken.txt")
    )

    assert scratch_files(engine) == set()

    assert_consistent(engine)


def test_import_unsupported_file_is_rejected(engine, tmp_path):

    path = make_txt(
        str(tmp_path / "corpus"),
        "payload.exe",
        "not a document",
    )

    result = import_paths(engine, [path])

    assert result.uploaded_count == 0
    assert result.rejected_count == 1
    assert result.failed_count == 0

    assert result.rejected[0]["filename"] == "payload.exe"
    assert "Unsupported or invalid filename" in (
        result.rejected[0]["reason"]
    )

    assert engine.counts()["documents"] == 0

    # A rejected file is never written into the data folder.
    assert documents_on_disk(engine) == set()

    assert_consistent(engine)


def test_import_empty_document_fails_without_indexing(
    engine,
    tmp_path,
):

    path = make_txt(
        str(tmp_path / "corpus"),
        "blank.txt",
        "   \n",
    )

    result = import_paths(engine, [path])

    assert result.uploaded_count == 0
    assert result.failed_count == 1
    assert result.rejected_count == 0

    assert "no readable text" in result.failed[0]["reason"]

    assert engine.counts()["documents"] == 0
    assert engine.sqlite_counts()["documents"] == 0


def test_import_rejects_path_traversal(engine, tmp_path):
    """
    A malicious filename must not escape the data folder.

    The stream is a real document, so the only thing standing between the
    caller and arbitrary file placement is the sanitizer.
    """

    path = make_txt(
        str(tmp_path / "corpus"),
        "notes.txt",
        "traversal attempt payload\n",
    )

    with open(path, "rb") as stream:

        result = engine.import_documents(
            [(
                "../../../escaped.txt",
                stream,
            )],
            log=lambda *args: None,
        )

    assert result.uploaded_count == 1

    # Stored under the sanitized base name, inside the data folder.
    assert result.uploaded == ["escaped.txt"]

    assert documents_on_disk(engine) == {"escaped.txt"}

    assert not os.path.exists(
        os.path.join(
            os.path.dirname(engine.config.data_folder),
            "escaped.txt",
        )
    )

    assert_consistent(engine)


def test_import_paths_helper(engine, tmp_path):

    result = engine.import_paths(
        sample_corpus(tmp_path),
        log=lambda *args: None,
    )

    assert result.uploaded_count == 3

    assert_consistent(engine)


def test_index_document_rejects_empty_file(engine, tmp_path):

    path = make_txt(
        engine.config.data_folder,
        "blank.txt",
        "",
    )

    with pytest.raises(ValueError) as error:

        engine.index_document(
            "blank.txt",
            path,
        )

    assert "no readable text" in str(error.value)


def test_index_extracted_matches_index_document(tmp_path):
    """
    Indexing pre-extracted text must produce exactly the index that
    indexing the file produces.

    This is the claim the offline Android architecture rests on: a device
    ships text extracted by this engine and indexes that text without a PDF
    parser, so it never needs a parser matching PyPDF2. If the two paths
    diverged, that design would be broken and a device would rank the shipped
    corpus differently from the server. This is where it would show.

    Nothing is mocked. One engine imports the real files through the normal
    stream path; the other indexes the text those files extract to. The
    documents are copied into the second engine's folder as well, because on
    a device the file is copied into app-private storage and the text arrives
    beside it - so both engines are checked against the same consistency
    invariant.
    """

    from search_engine.extraction import (
        extract_pages,
        extract_text,
    )
    from search_engine.filenames import (
        sanitize_upload_filename,
    )

    corpus = sample_corpus(tmp_path)

    files_folder = os.path.abspath(
        str(tmp_path / "from_files")
    )

    text_folder = os.path.abspath(
        str(tmp_path / "from_text")
    )

    os.makedirs(files_folder, exist_ok=True)
    os.makedirs(text_folder, exist_ok=True)

    from_files = SearchEngine(
        EngineConfig(data_folder=files_folder)
    )

    from_text = SearchEngine(
        EngineConfig(data_folder=text_folder)
    )

    import_paths(from_files, corpus)

    for path in corpus:

        filename = sanitize_upload_filename(
            os.path.basename(path)
        )

        shutil.copy(
            path,
            os.path.join(text_folder, filename),
        )

        from_text.index_extracted(
            filename,
            extract_text(path, filename),
            extract_pages(path, filename),
        )

    assert_consistent(from_files)
    assert_consistent(from_text)

    # Same corpus, same sizes, same persisted rows.
    assert from_files.counts() == from_text.counts()

    assert sqlite_documents(from_files) == sqlite_documents(
        from_text
    )

    assert sqlite_postings(from_files) == sqlite_postings(
        from_text
    )

    # Same in-memory structures.
    source = from_files.snapshot()
    target = from_text.snapshot()

    assert source.inverted_index == target.inverted_index
    assert source.filename_index == target.filename_index
    assert source.page_text_index == target.page_text_index

    # Metadata agrees on everything but the recorded path, which points at
    # each engine's own data folder.
    assert set(source.document_metadata) == set(
        target.document_metadata
    )

    for filename in sorted(source.document_metadata):

        expected = dict(source.document_metadata[filename])
        actual = dict(target.document_metadata[filename])

        assert expected.pop("path") == os.path.join(
            files_folder,
            filename,
        )

        assert actual.pop("path") == os.path.join(
            text_folder,
            filename,
        )

        assert expected == actual, filename

    # And the same answers, which is the property a user would notice.
    queries = [
        "",
        "network",
        "Network Notes.pdf",
        '"network"',
        "normalization",
        "999",
        "transactions",
        "the",
        "pdf",
        "docx",
        "zzzqqq",
    ]

    for query in queries:

        assert canonicalize_data_folder(
            from_files.search(query),
            files_folder,
        ) == canonicalize_data_folder(
            from_text.search(query),
            text_folder,
        ), query


def test_index_extracted_rejects_text_with_no_words(engine):
    """
    The empty-document rule is enforced on the text path too.

    A sidecar carrying an empty extraction must be rejected exactly as an
    unreadable file is, rather than indexed as a document that can never be
    found.
    """

    with pytest.raises(ValueError) as error:

        engine.index_extracted(
            "blank.txt",
            "",
            [{"page": 1, "text": ""}],
        )

    assert "no readable text" in str(error.value)

    assert engine.counts()["documents"] == 0


def test_index_extracted_records_a_page_count_from_the_pages(
    engine,
    tmp_path,
):
    """
    `page_count` comes from the pages supplied, not from the file.

    A device indexing bundled text has no parser to ask, so the sidecar's
    page list is authoritative. Pinning this keeps a port from silently
    recording one page for everything and losing page numbers in snippets.
    """

    engine.index_extracted(
        "lecture.txt",
        "first page text second page text third page text",
        [
            {"page": 1, "text": "first page text"},
            {"page": 2, "text": "second page text"},
            {"page": 3, "text": "third page text"},
        ],
    )

    metadata = engine.get_document_metadata("lecture.txt")

    assert metadata["page_count"] == 3

    assert metadata["path"] == os.path.join(
        engine.config.data_folder,
        "lecture.txt",
    )

    # Snippets and page numbers are cut from the supplied pages.
    result = engine.search("third")

    assert result["results"]
    assert result["results"][0]["page"] == 3


# ============================================================
# DELETE
# ============================================================

def seeded_engine(engine, tmp_path):

    import_paths(engine, sample_corpus(tmp_path))

    return engine


def test_delete_existing_document(engine, tmp_path):

    seeded_engine(engine, tmp_path)

    assert engine.counts()["documents"] == 3

    result = engine.delete("security.txt")

    assert result.outcome == DELETE_DELETED
    assert result.deleted
    assert result.filename == "security.txt"

    assert engine.counts()["documents"] == 2
    assert not engine.has_document("security.txt")
    assert engine.get_document_metadata("security.txt") is None

    # The terms that only that document contributed are gone entirely.
    snapshot = engine.snapshot()

    assert "firewalls" not in snapshot.inverted_index
    assert "security.txt" not in snapshot.page_text_index

    assert_consistent(engine)


def test_delete_missing_document(engine, tmp_path):

    seeded_engine(engine, tmp_path)

    result = engine.delete("does_not_exist.txt")

    assert result.outcome == DELETE_NOT_INDEXED
    assert not result.deleted

    # Nothing else moved.
    assert engine.counts()["documents"] == 3

    assert_consistent(engine)


def test_delete_missing_file(engine, tmp_path):
    """
    Indexed but the file is gone from disk.

    The engine reports file_not_found rather than silently unindexing,
    because deleting a document the user can no longer see is a different
    operation from repairing the index (that is what rebuild is for).
    """

    seeded_engine(engine, tmp_path)

    os.remove(
        os.path.join(
            engine.config.data_folder,
            "security.txt",
        )
    )

    result = engine.delete("security.txt")

    assert result.outcome == DELETE_FILE_NOT_FOUND
    assert not result.deleted

    # Still indexed: the engine did not half-delete it.
    assert engine.has_document("security.txt")


def test_delete_rejects_path_traversal(engine, tmp_path):

    seeded_engine(engine, tmp_path)

    for candidate in (
        "../../../etc/passwd",
        "..%2F..%2Fapp.py",
        "/absolute/path.txt",
    ):

        result = engine.delete(candidate)

        assert result.outcome in (
            DELETE_INVALID_PATH,
            DELETE_NOT_INDEXED,
        ), candidate

    assert engine.counts()["documents"] == 3

    assert_consistent(engine)


def test_resolve_document_path_stays_inside_data_folder(engine):

    assert engine.resolve_document_path("") is None
    assert engine.resolve_document_path(None) is None

    # A bare ".." resolves to the parent of the data folder and is refused.
    assert engine.resolve_document_path("..") is None

    resolved = engine.resolve_document_path("notes.txt")

    assert resolved == os.path.realpath(
        os.path.join(engine.config.data_folder, "notes.txt")
    )

    # Traversal attempts are neutralized by taking the base name, so they
    # resolve inside the data folder rather than escaping it. The security
    # property that matters is the containment, not the refusal.
    data_root = os.path.realpath(engine.config.data_folder)

    for candidate in (
        "../../secret.txt",
        "../../../etc/passwd",
        "/etc/passwd",
        "nested/../../secret.txt",
    ):

        resolved = engine.resolve_document_path(candidate)

        assert resolved is not None, candidate

        assert os.path.commonpath(
            [data_root, resolved]
        ) == data_root, candidate


def test_delete_removes_sqlite_rows(engine, tmp_path):

    seeded_engine(engine, tmp_path)

    before = engine.sqlite_counts()

    engine.delete("security.txt")

    after = engine.sqlite_counts()

    assert after["documents"] == before["documents"] - 1

    assert engine.document_term_counts("security.txt") == {}

    # Pages belonging to the deleted document are gone.
    connection = sqlite3.connect(
        engine.config.sqlite_db_file
    )

    try:

        rows = connection.execute(
            "SELECT COUNT(*) FROM pages WHERE filename = ?",
            ("security.txt",),
        ).fetchone()[0]

    finally:

        connection.close()

    assert rows == 0

    assert_consistent(engine)


# ============================================================
# BULK DELETE
# ============================================================

def test_bulk_delete_multiple(engine, tmp_path):

    seeded_engine(engine, tmp_path)

    result = engine.bulk_delete([
        "security.txt",
        "Network_Notes.pdf",
    ])

    assert result.deleted_count == 2
    assert result.not_found_count == 0
    assert result.failed_count == 0
    assert result.ok

    assert engine.counts()["documents"] == 1

    assert_consistent(engine)


def test_bulk_delete_deduplicates_names(engine, tmp_path):

    seeded_engine(engine, tmp_path)

    result = engine.bulk_delete([
        "security.txt",
        "security.txt",
        "  security.txt  ",
    ])

    assert result.requested == ["security.txt"]
    assert result.deleted_count == 1
    assert result.not_found_count == 0

    assert engine.counts()["documents"] == 2

    assert_consistent(engine)


def test_bulk_delete_nonexistent(engine, tmp_path):

    seeded_engine(engine, tmp_path)

    result = engine.bulk_delete([
        "ghost.txt",
        "phantom.pdf",
    ])

    assert result.deleted_count == 0
    assert result.not_found_count == 2
    assert result.failed_count == 0

    assert result.not_found == ["ghost.txt", "phantom.pdf"]

    assert engine.counts()["documents"] == 3

    assert_consistent(engine)


def test_bulk_delete_mixed_outcomes(engine, tmp_path):

    seeded_engine(engine, tmp_path)

    result = engine.bulk_delete([
        "security.txt",
        "ghost.txt",
        "Database_Guide.docx",
        7,
        None,
    ])

    assert result.deleted == [
        "security.txt",
        "Database_Guide.docx",
    ]
    assert result.not_found == ["ghost.txt"]
    assert result.failed_count == 0

    assert result.deleted_count == 2
    assert result.not_found_count == 1

    assert engine.counts()["documents"] == 1

    assert_consistent(engine)


def test_bulk_delete_is_one_transaction(engine, tmp_path):
    """
    The whole batch must share ONE SQLite transaction.

    A loop of independent deletes would open a connection and commit per
    document, which is exactly what the API promises not to do. Counting
    commits proves the batch is atomic at the database level.
    """

    seeded_engine(engine, tmp_path)

    commits = []

    real_connection = engine.store.connection

    class ConnectionProxy:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, *arguments):
            return self._real.__exit__(*arguments)

        def commit(self):
            commits.append("commit")
            return self._real.commit()

    engine.store.connection = (
        lambda: ConnectionProxy(real_connection())
    )

    try:
        result = engine.bulk_delete([
            "security.txt",
            "Network_Notes.pdf",
            "Database_Guide.docx",
        ])
    finally:
        engine.store.connection = real_connection

    assert result.deleted_count == 3

    assert commits == ["commit"]


def test_bulk_delete_rolls_back_sqlite_on_failure(
    engine,
    tmp_path,
    monkeypatch,
):
    """
    If the batch itself fails, SQLite keeps every document.

    A per-document failure is NOT a batch failure: those are recorded in
    `failed` and the rest of the batch still commits, because one bad
    document must not discard nine good ones. This test forces the commit
    itself to fail, which is the batch-level fault.

    The rollback must leave ALL THREE stores agreeing. That is the point of
    a transaction: either the batch happened or it did not. A rollback that
    keeps the SQLite rows but has already dropped the in-memory entries and
    deleted the files leaves the engine serving results for documents that
    no longer exist on disk, with no way to notice until a rebuild.

    The original implementation mutated memory and the filesystem inside the
    loop, before the commit, so a rollback left them diverged. The engine now
    defers both until after the commit succeeds.
    """

    seeded_engine(engine, tmp_path)

    assert engine.counts()["documents"] == 3

    real_connection = engine.store.connection

    class FailingCommitConnection:
        """
        Delegation proxy whose commit() raises.

        sqlite3.Connection is a C type and rejects attribute assignment, so
        the failure has to be injected by wrapping rather than patching.
        """

        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, *arguments):
            return self._real.__exit__(*arguments)

        def commit(self):
            raise sqlite3.OperationalError(
                "simulated commit failure"
            )

    monkeypatch.setattr(
        engine.store,
        "connection",
        lambda: FailingCommitConnection(
            real_connection()
        ),
    )

    with pytest.raises(sqlite3.OperationalError):

        engine.bulk_delete([
            "security.txt",
            "Network_Notes.pdf",
        ])

    # monkeypatch restores store.connection at teardown.

    # The transactional guarantee holds: SQLite kept every document.
    assert len(sqlite_documents(engine)) == 3

    # And so do the other two stores. Nothing was mutated, because nothing
    # was committed: the files are still on disk and memory still serves
    # them.
    assert os.path.isfile(
        os.path.join(
            engine.config.data_folder,
            "security.txt",
        )
    )
    assert os.path.isfile(
        os.path.join(
            engine.config.data_folder,
            "Network_Notes.pdf",
        )
    )
    assert engine.has_document("security.txt")
    assert engine.has_document("Network_Notes.pdf")
    assert engine.counts()["documents"] == 3

    assert_consistent(engine)

    # The batch is still searchable - the rollback did not corrupt the
    # in-memory index that searches read from.
    payload = engine.search("security")

    assert payload["pagination"]["total"] > 0

    # No rebuild is needed to recover. A restart sees the same three
    # documents, because all three stores still agree.
    reloaded = SearchEngine(engine.config)

    assert reloaded.counts()["documents"] == 3

    assert_consistent(reloaded)


def test_bulk_delete_repairs_missing_file(engine, tmp_path):
    """
    Indexed but absent on disk: still counts as deleted.

    The index entry is the thing being removed, so a half-removed document
    is repaired rather than reported as missing.
    """

    seeded_engine(engine, tmp_path)

    os.remove(
        os.path.join(
            engine.config.data_folder,
            "security.txt",
        )
    )

    result = engine.bulk_delete(["security.txt"])

    assert result.deleted_count == 1
    assert result.not_found_count == 0

    assert not engine.has_document("security.txt")

    assert_consistent(engine)


def test_bulk_delete_empty_request(engine, tmp_path):

    seeded_engine(engine, tmp_path)

    result = engine.bulk_delete([])

    assert result.requested == []
    assert result.deleted_count == 0

    assert engine.counts()["documents"] == 3


def test_bulk_delete_removes_a_file_that_is_not_indexed(
    engine,
    tmp_path,
):
    """
    On disk but absent from the index: the file is still removed.

    The mirror image of `test_bulk_delete_repairs_missing_file`. A caller
    asking to delete a name expects the document gone from the corpus, and
    "not indexed" is not a reason to leave a stray readable file behind
    where the next rebuild would silently pick it back up.
    """

    seeded_engine(engine, tmp_path)

    stray_path = os.path.join(
        engine.config.data_folder,
        "stray_notes.txt",
    )

    with open(stray_path, "w", encoding="utf-8") as file:
        file.write("stray content never indexed\n")

    assert os.path.isfile(stray_path)
    assert not engine.has_document("stray_notes.txt")

    result = engine.bulk_delete([
        "stray_notes.txt",
        "security.txt",
    ])

    assert sorted(result.deleted) == [
        "security.txt",
        "stray_notes.txt",
    ]
    assert result.not_found_count == 0
    assert result.failed_count == 0

    assert not os.path.isfile(stray_path)
    assert not os.path.isfile(
        os.path.join(
            engine.config.data_folder,
            "security.txt",
        )
    )

    # The stray file was never in the index, so only the real document
    # changed the counts.
    assert engine.counts()["documents"] == 2

    assert_consistent(engine)


def test_bulk_delete_orphans_the_file_when_unlinking_fails(
    engine,
    tmp_path,
    monkeypatch,
):
    """
    Phase 2 can only fail after the commit has succeeded.

    When the stored file cannot be unlinked, the document is genuinely
    unindexed - SQLite and memory agree, because the transaction committed -
    and the file is left behind as an orphan. That is the recoverable
    direction: a rebuild re-indexes it. The unrecoverable direction would be
    a deleted file whose index rows survived a rollback, which is what the
    two-phase ordering exists to prevent.

    Note the orphan is a real inconsistency, so `assert_consistent` is not
    called here: it deliberately fails on a readable file that is not
    indexed. This test pins the divergence and its recovery instead.
    """

    seeded_engine(engine, tmp_path)

    assert engine.counts()["documents"] == 3

    real_remove = os.remove

    def refusing_remove(path):

        if str(path).endswith("security.txt"):
            raise OSError("simulated permission denied")

        return real_remove(path)

    monkeypatch.setattr(os, "remove", refusing_remove)

    result = engine.bulk_delete([
        "security.txt",
        "Network_Notes.pdf",
    ])

    monkeypatch.undo()

    # The unlinkable document is reported as failed, with a reason that says
    # what actually happened rather than a bare OS message.
    assert result.failed_count == 1
    assert result.failed[0]["filename"] == "security.txt"
    assert "could not be removed" in result.failed[0]["reason"]

    # The deletable one still succeeded: one bad document does not discard
    # the rest of the batch.
    assert result.deleted == ["Network_Notes.pdf"]

    # Memory and SQLite agree with each other, and both say the orphan is
    # unindexed. Two of three documents were requested; both left the index.
    assert not engine.has_document("security.txt")
    assert "security.txt" not in sqlite_documents(engine)
    assert engine.counts()["documents"] == 1
    assert engine.has_document("Database_Guide.docx")

    # The file itself is still there - that is the orphan.
    assert os.path.isfile(
        os.path.join(
            engine.config.data_folder,
            "security.txt",
        )
    )

    # Recovery: a rebuild picks the orphan back up, and all three stores
    # agree again. Network_Notes.pdf is really gone, so the corpus is now
    # the untouched DOCX plus the resurrected TXT.
    engine.rebuild(log=lambda *args: None)

    assert engine.has_document("security.txt")
    assert engine.counts()["documents"] == 2

    assert_consistent(engine)


# ============================================================
# REBUILD
# ============================================================

def test_rebuild_indexes_files_placed_on_disk(
    engine,
    tmp_path,
):
    """
    Documents dropped into the data folder are invisible until a rebuild.

    This is the recovery path for a corpus edited outside the API, and the
    path an offline Android "reindex" button takes.
    """

    for path in sample_corpus(tmp_path):

        target = os.path.join(
            engine.config.data_folder,
            os.path.basename(path),
        )

        with open(path, "rb") as source:
            with open(target, "wb") as destination:
                destination.write(source.read())

    assert engine.counts()["documents"] == 0

    engine.rebuild(log=lambda *args: None)

    counts = engine.counts()

    assert counts["documents"] == 3
    assert counts["filenames_indexed"] == 3
    assert counts["page_text_entries"] == 3
    assert counts["content_terms"] > 0

    assert_consistent(engine)


def test_rebuild_skips_unsupported_and_empty(engine, tmp_path):

    for path in sample_corpus(tmp_path):

        target = os.path.join(
            engine.config.data_folder,
            os.path.basename(path),
        )

        with open(path, "rb") as source:
            with open(target, "wb") as destination:
                destination.write(source.read())

    make_txt(engine.config.data_folder, "ignored.md", "# hello")
    make_txt(engine.config.data_folder, "blank.txt", "   \n")
    make_txt(engine.config.data_folder, "archive.zip", "junk")

    engine.rebuild(log=lambda *args: None)

    # blank.txt has no readable text, ignored.md and archive.zip are
    # unsupported extensions.
    assert engine.counts()["documents"] == 3

    snapshot = engine.snapshot()

    for skipped in ("blank.txt", "ignored.md", "archive.zip"):
        assert skipped not in snapshot.document_metadata

    assert_consistent(engine)


def test_rebuild_is_repeatable(engine, tmp_path):

    engine.import_paths(
        sample_corpus(tmp_path),
        log=lambda *args: None,
    )

    first = engine.counts()

    engine.rebuild(log=lambda *args: None)

    second = engine.counts()

    assert first == second

    assert_consistent(engine)


def test_rebuild_recovers_after_manual_file_removal(
    engine,
    tmp_path,
):

    engine.import_paths(
        sample_corpus(tmp_path),
        log=lambda *args: None,
    )

    os.remove(
        os.path.join(
            engine.config.data_folder,
            "security.txt",
        )
    )

    # The index still believes the document exists.
    assert engine.has_document("security.txt")

    engine.rebuild(log=lambda *args: None)

    assert not engine.has_document("security.txt")

    assert engine.counts()["documents"] == 2

    assert_consistent(engine)


def test_rebuild_replaces_snapshot_atomically(engine, tmp_path):
    """
    A search holding the old snapshot must not see a half-built index.

    The rebuild swaps four references under one lock, so a snapshot captured
    before the rebuild still describes the complete previous corpus.
    """

    engine.import_paths(
        sample_corpus(tmp_path),
        log=lambda *args: None,
    )

    before = engine.snapshot()

    assert len(before.document_metadata) == 3

    make_txt(
        engine.config.data_folder,
        "extra.txt",
        "an additional document about networks\n",
    )

    engine.rebuild(log=lambda *args: None)

    after = engine.snapshot()

    # The new snapshot sees four documents...
    assert len(after.document_metadata) == 4

    # ...while the previously captured snapshot is untouched and complete.
    assert len(before.document_metadata) == 3
    assert before is not after

    assert_consistent(engine)


def test_rebuild_writes_legacy_json_snapshot(engine, tmp_path):

    engine.import_paths(
        sample_corpus(tmp_path),
        log=lambda *args: None,
    )

    engine.rebuild(log=lambda *args: None)

    for path in (
        engine.config.index_file,
        engine.config.meta_file,
        engine.config.filename_index_file,
        engine.config.page_text_file,
    ):
        assert os.path.isfile(path), path


def test_legacy_json_snapshot_migrates_into_sqlite(tmp_path):
    """
    An existing deployment may hold a JSON corpus and an empty database.

    Startup must migrate it once, and the migrated engine must agree with
    the JSON it came from.
    """

    folder = str(tmp_path / "legacy")

    os.makedirs(folder, exist_ok=True)

    source = SearchEngine(EngineConfig(data_folder=folder))

    source.import_paths(
        sample_corpus(tmp_path),
        log=lambda *args: None,
    )

    snapshot = source.snapshot()

    # Persist the legacy JSON view, then wipe SQLite to simulate an old
    # installation that has never been migrated.
    atomic_write_json(
        source.config.index_file,
        snapshot.inverted_index,
    )
    atomic_write_json(
        source.config.meta_file,
        snapshot.document_metadata,
    )
    atomic_write_json(
        source.config.filename_index_file,
        snapshot.filename_index,
    )
    atomic_write_json(
        source.config.page_text_file,
        snapshot.page_text_index,
    )

    os.remove(source.config.sqlite_db_file)

    for suffix in ("-wal", "-shm"):

        path = source.config.sqlite_db_file + suffix

        if os.path.exists(path):
            os.remove(path)

    migrated = SearchEngine(EngineConfig(data_folder=folder))

    assert migrated.counts() == source.counts()

    # The migration wrote SQLite, so the two stores now agree.
    assert sqlite_documents(migrated) == set(
        snapshot.document_metadata
    )

    assert_consistent(migrated)


# ============================================================
# PERSISTENCE ACROSS RESTART
# ============================================================

def test_state_survives_restart(engine, tmp_path):

    engine.import_paths(
        sample_corpus(tmp_path),
        log=lambda *args: None,
    )

    before = engine.counts()

    reopened = SearchEngine(engine.config)

    assert reopened.counts() == before

    assert set(
        reopened.snapshot().document_metadata
    ) == set(engine.snapshot().document_metadata)

    assert_consistent(reopened)


def test_deletion_survives_restart(engine, tmp_path):

    engine.import_paths(
        sample_corpus(tmp_path),
        log=lambda *args: None,
    )

    engine.delete("security.txt")

    reopened = SearchEngine(engine.config)

    assert reopened.counts()["documents"] == 2
    assert not reopened.has_document("security.txt")

    assert_consistent(reopened)


# ------------------------------------------------------------
# CRASH AND FAILURE RECOVERY
# ------------------------------------------------------------
#
# A rebuild builds the whole new snapshot and only then swaps the active
# pointer, so a failure partway through cannot expose a half-built index.
# These tests drive that failure for real rather than reading the code: they
# make the snapshot builder raise, and check what still serves afterwards.
#
# Index status is deliberately in-memory only, so nothing about INDEXING or
# ERROR is written to SQLite or the JSON snapshot. That is what makes a crash
# mid-rebuild survivable: there is no persisted "still indexing" flag for a
# restarted process to inherit and get stuck on. The tests below assert that
# property, because it is load-bearing for the Android UI in exactly the same
# way - a process death during indexing must not wedge the app.

def test_a_failed_rebuild_leaves_the_previous_snapshot_serving(
    engine,
    tmp_path,
    monkeypatch,
):
    """
    A rebuild that raises must leave the corpus exactly as searchable as it
    was, not blank it and not half-replace it.
    """

    from search_engine import engine as engine_module

    engine.import_paths(
        sample_corpus(tmp_path),
        log=lambda *args: None,
    )

    before = engine.counts()

    def explode(*_args, **_kwargs):
        raise RuntimeError("simulated crash mid-rebuild")

    monkeypatch.setattr(
        engine_module,
        "build_snapshot_from_folder",
        explode,
    )

    with pytest.raises(RuntimeError):
        engine.rebuild(log=lambda *args: None)

    # The active snapshot is the one from before the failed rebuild.
    assert engine.counts() == before

    page = engine.search("firewalls")

    results = page["results"] if isinstance(page, dict) else page

    assert results, "search stopped working after a failed rebuild"

    assert_consistent(engine)


def test_a_failed_rebuild_does_not_corrupt_the_persisted_snapshot(
    engine,
    tmp_path,
    monkeypatch,
):
    """
    The failure must not reach disk either: a restart after a crashed rebuild
    has to come back with the corpus that was there before it.
    """

    from search_engine import engine as engine_module

    engine.import_paths(
        sample_corpus(tmp_path),
        log=lambda *args: None,
    )

    before = engine.counts()

    def explode(*_args, **_kwargs):
        raise RuntimeError("simulated crash mid-rebuild")

    monkeypatch.setattr(
        engine_module,
        "build_snapshot_from_folder",
        explode,
    )

    with pytest.raises(RuntimeError):
        engine.rebuild(log=lambda *args: None)

    reopened = SearchEngine(engine.config)

    assert reopened.counts() == before

    assert_consistent(reopened)


def test_indexing_status_does_not_survive_a_restart(engine, tmp_path):
    """
    A process that dies while a rebuild is in flight leaves no trace of it.

    Status is in-memory, so the restarted engine reports READY and serves the
    corpus. Were INDEXING persisted, a crash mid-rebuild would wedge every
    later start into reporting an index that never finishes.
    """

    engine.import_paths(
        sample_corpus(tmp_path),
        log=lambda *args: None,
    )

    engine.set_index_status(
        "INDEXING",
        "Rebuilding search index in background...",
        started_at=1.0,
    )

    assert engine.index_status()["state"] == "INDEXING"

    reopened = SearchEngine(engine.config)

    assert reopened.index_status()["state"] == "READY"

    assert reopened.counts() == engine.counts()

    page = reopened.search("firewalls")

    results = page["results"] if isinstance(page, dict) else page

    assert results

    assert_consistent(reopened)


def test_an_error_status_still_serves_the_corpus(engine, tmp_path):
    """
    ERROR describes the last rebuild, not the index. Search must keep working
    through it, because the snapshot that was serving before the failure is
    still the one in memory - and a UI that blocks on ERROR would take the
    corpus down over a failed background refresh.
    """

    engine.import_paths(
        sample_corpus(tmp_path),
        log=lambda *args: None,
    )

    engine.set_index_status(
        "ERROR",
        "Rebuild failed.",
        last_error="simulated crash mid-rebuild",
    )

    status = engine.index_status()

    assert status["state"] == "ERROR"
    assert status["last_error"] == "simulated crash mid-rebuild"

    page = engine.search("firewalls")

    results = page["results"] if isinstance(page, dict) else page

    assert results, "an ERROR status must not stop search serving"

    assert engine.counts()["documents"] == 3

    assert_consistent(engine)


def test_a_restart_after_an_error_reports_ready_again(engine, tmp_path):
    """
    ERROR is not sticky across a restart either, so a failed background
    rebuild cannot leave the app permanently reporting a broken index.
    """

    engine.import_paths(
        sample_corpus(tmp_path),
        log=lambda *args: None,
    )

    engine.set_index_status(
        "ERROR",
        "Rebuild failed.",
        last_error="simulated crash mid-rebuild",
    )

    reopened = SearchEngine(engine.config)

    status = reopened.index_status()

    assert status["state"] == "READY"
    assert status["last_error"] is None

    assert_consistent(reopened)


# ============================================================
# STATUS
# ============================================================

def test_status_reports_counts_and_ready_state(engine, tmp_path):

    status = engine.status()

    assert status["documents"] == 0
    assert status["data_folder"] == engine.config.data_folder
    assert status["indexing"]["state"] == "READY"

    engine.import_paths(
        sample_corpus(tmp_path),
        log=lambda *args: None,
    )

    status = engine.status()

    assert status["documents"] == 3
    assert status["content_terms"] > 0
    assert status["indexing"]["state"] == "READY"


def test_status_generation_advances_on_ready(engine, tmp_path):

    first = engine.index_status()["generation"]

    engine.import_paths(
        sample_corpus(tmp_path),
        log=lambda *args: None,
    )

    assert engine.index_status()["generation"] > first


# ============================================================
# SEARCH
# ============================================================
#
# The ranking pipeline moved out of app.py into search_engine/search.py
# byte for byte. tools/verify_http_parity.py is what proves the ranking
# itself did not change: it diffs this build against the pre-refactor
# monolith over ~80 queries.
#
# These tests pin the contract around it, which parity cannot:
#
#   - the payload shapes, including the three legacy bare-list branches
#   - parameter clamping policy
#   - snapshot isolation, i.e. that a search reads one coherent view
#   - that search reflects mutations with no restart and survives a rebuild
#
# Scores are deliberately not hardcoded. They are the product's tuned
# behavior; asserting relationships and ranges keeps these tests honest
# about the contract without duplicating the parity tool's job.


SEARCH_MATCH_TYPES = {
    "Filename + Phrase",
    "Phrase Match",
    "Filename + Content",
    "Filename Match",
    "Content Match",
}


# Import sanitizes upload names (spaces become underscores), so these are
# the filenames that actually reach the index and the search results.
# `title` is that same sanitized filename, extension included.
NETWORK_NOTES = "Network_Notes.pdf"
NETWORK_SECURITY_GUIDE = "Network_Security_Guide.pdf"
DATABASE_GUIDE = "Database_Guide.docx"
MACHINE_LEARNING = "machine_learning.txt"


@pytest.fixture
def search_corpus(tmp_path):
    """
    A corpus designed so each ranking branch has a document that reaches it.

    Each file exists to trigger one specific signal:

        Network Notes.pdf           exact numeric token 999, and a numeric
                                    run buried inside unique777marker
        Network Security Guide.pdf  a >=2-word filename substring, plus an
                                    adjacent "alpha beta" phrase
        Database Guide.docx         content-only matches, and "alpha"/"beta"
                                    present but NOT adjacent
        machine learning.txt        a phrase repeated several times, which is
                                    also the document's own filename
    """

    directory = str(tmp_path / "search_corpus")

    os.makedirs(directory, exist_ok=True)

    return [
        make_pdf(
            directory,
            "Network Notes.pdf",
            [
                "Computer network fundamentals and the OSI model",
                "Routing protocols such as BGP and OSPF reference 999",
                "The token unique777marker hides a numeric run",
            ],
        ),
        make_pdf(
            directory,
            "Network Security Guide.pdf",
            [
                "Firewalls filter traffic between network zones",
                "alpha beta appear adjacent in this document",
            ],
        ),
        make_docx(
            directory,
            "Database Guide.docx",
            [
                "Normalization removes redundancy from relations",
                "alpha appears here and beta appears much later",
            ],
        ),
        make_txt(
            directory,
            "machine learning.txt",
            "machine learning models and machine learning datasets\n"
            "machine learning studies learning systems\n",
        ),
    ]


@pytest.fixture
def search_engine(engine, search_corpus):
    """An engine holding the search corpus, verified consistent."""

    result = import_paths(engine, search_corpus)

    assert result.rejected == []
    assert result.failed == []
    assert result.uploaded_count == 4

    assert_consistent(engine)

    return engine


def titles(payload):
    """Result titles in ranked order."""

    return [
        item["title"]
        for item in payload["results"]
    ]


def paths(payload):
    """Result paths in ranked order."""

    return [
        item["path"]
        for item in payload["results"]
    ]


def ends_with(payload, filename):
    """True when one result points at `filename`."""

    return any(
        item["path"].endswith(filename)
        for item in payload["results"]
    )


# ------------------------------------------------------------
# blank query
# ------------------------------------------------------------

def test_search_blank_query_returns_the_empty_shape(search_engine):
    """
    A blank query is answered before any ranking work.

    Its payload keeps the historical shape, which carries no start/end
    offsets; empty_query_response() is the single definition of it.
    """

    assert search_engine.search("") == empty_query_response()

    assert search_engine.search("   ") == empty_query_response()


def test_search_trims_the_query(search_engine):
    """Padding is query normalization, so it belongs in the core."""

    assert search_engine.search("  network  ") == search_engine.search("network")


# ------------------------------------------------------------
# the three legacy bare-list branches
# ------------------------------------------------------------

def test_search_on_empty_corpus_returns_bare_list(engine):
    """
    No documents at all: the historical API answers `[]`, not a page object.

    This branch is unreachable on a live corpus, which is exactly why the
    parity sweep cannot cover it and a core test must.
    """

    assert engine.search("network") == []


def test_search_quoted_phrase_without_match_returns_bare_list(search_engine):
    """A quoted phrase nothing contains: `[]`, by the same legacy contract."""

    assert search_engine.search('"zzzqqq wwwww"') == []


def test_search_filetype_filter_that_matches_nothing_returns_empty_page(
    search_engine,
):
    """
    A filetype that does exist, plus keywords nothing matches: empty page.

    Not a bare list. document_scores is pre-populated for every document
    before scoring, so narrowing it by a present filetype cannot empty it
    and the bare-list site for that case is unreachable. Pinned because the
    asymmetry with the next test is otherwise invisible.
    """

    payload = search_engine.search("zzzqqq pdf")

    assert isinstance(payload, dict)
    assert payload["results"] == []
    assert payload["pagination"]["total"] == 0


def test_search_filetype_with_no_documents_returns_bare_list(engine, tmp_path):
    """
    The reachable filetype bare-list branch: zero documents of that type.

    total_documents is counted through the same filetype predicate, so a
    corpus of PDFs asked for `docx` has nothing to search and answers with
    the legacy bare list.
    """

    import_paths(engine, [
        make_pdf(
            str(tmp_path / "only_pdf"),
            "Single.pdf",
            ["The only document in this corpus"],
        ),
    ])

    assert engine.search("only")["pagination"]["total"] == 1

    # Keywords plus an absent type reach the bare-list branch.
    assert engine.search("single docx") == []

    # The type on its own does not: the browse branch runs first and
    # answers with an empty page object. Another pinned asymmetry.
    browse = engine.search("docx")

    assert isinstance(browse, dict)
    assert browse["results"] == []
    assert browse["pagination"]["total"] == 0


def test_search_no_match_without_filter_returns_empty_page(search_engine):
    """
    With no filetype filter the same miss is a normal empty page.

    Pinned alongside the three branches above because the asymmetry is
    surprising and is part of the published behavior.
    """

    payload = search_engine.search("zzzqqq")

    assert isinstance(payload, dict)
    assert payload["results"] == []
    assert payload["pagination"]["total"] == 0
    assert payload["pagination"]["total_pages"] == 0


# ------------------------------------------------------------
# filetype-only browse
# ------------------------------------------------------------

def test_search_filetype_only_is_a_browse_operation(search_engine):
    """
    `pdf` alone lists documents; it does not rank them.

    Flat score, title order, and a snippet that says what happened.
    """

    payload = search_engine.search("pdf")

    assert payload["pagination"]["total"] == 2

    for item in payload["results"]:

        assert item["tag"] == "Filtered Result"
        assert item["match_type"] == "File Type: PDF"
        assert item["snippet"] == "Filtered by file type: PDF"
        assert item["score"] == 1.0
        assert item["relevance_score"] == 1.0
        assert item["page"] is None
        assert item["highlights"] == []
        assert item["filename_score"] == 0.0
        assert item["content_score"] == 0.0
        assert item["phrase_score"] == 0.0
        assert item["path"].lower().endswith(".pdf")

    assert titles(payload) == sorted(
        titles(payload),
        key=str.lower,
    )


def test_search_filetype_only_covers_every_supported_type(search_engine):

    assert search_engine.search("pdf")["pagination"]["total"] == 2
    assert search_engine.search("docx")["pagination"]["total"] == 1
    assert search_engine.search("txt")["pagination"]["total"] == 1


# ------------------------------------------------------------
# result contract
# ------------------------------------------------------------

def test_search_results_carry_the_documented_fields(search_engine):

    payload = search_engine.search("network")

    assert payload["results"]

    for item in payload["results"]:

        assert item["tag"] == "Ranked Result"
        assert item["score"] > 0
        assert item["match_type"] in SEARCH_MATCH_TYPES
        assert item["filetype_filter"] is None
        assert isinstance(item["snippet"], str)
        assert isinstance(item["highlights"], list)
        assert isinstance(item["phrase_occurrences"], int)

        for key in (
            "filename_score",
            "content_score",
            "phrase_score",
            "lexical_match_relevance",
            "prefix_similarity",
            "numeric_similarity",
        ):
            assert isinstance(item[key], float)


def test_search_result_urls_point_at_the_document_resource(search_engine):
    """
    The three URL fields are one resource path plus a page fragment.

    They are produced by the core so both adapters share a result shape:
    an online Android client uses them as-is, an offline one uses `path`.
    """

    payload = search_engine.search("network")

    for item in payload["results"]:

        assert item["document_url"].startswith("/api/documents/")

        # A filename with spaces must be percent-encoded to be a valid path.
        assert " " not in item["document_url"]

        assert item["open_url"] == item["page_url"]

        if item["page"] is None:
            assert item["page_url"] == item["document_url"]
        else:
            assert item["page_url"] == (
                f"{item['document_url']}#page={item['page']}"
            )


def test_search_results_are_ranked_by_descending_score(search_engine):

    payload = search_engine.search("network")

    scores = [item["score"] for item in payload["results"]]

    assert scores == sorted(scores, reverse=True)


def test_search_is_deterministic(search_engine):
    """Same corpus, same query, byte-identical payload."""

    assert (
        search_engine.search("network security")
        == search_engine.search("network security")
    )


# ------------------------------------------------------------
# matching signals
# ------------------------------------------------------------

def test_search_exact_filename_outranks_a_content_match(search_engine):
    """
    Naming the file is the strongest possible signal.

    The +100 exact filename bonus must put that document first, ahead of
    documents that merely discuss the same words.
    """

    payload = search_engine.search("Network Security Guide.pdf")

    assert ends_with(payload, NETWORK_SECURITY_GUIDE)

    top = payload["results"][0]

    assert top["title"] == NETWORK_SECURITY_GUIDE
    assert top["filename_score"] >= 100


def test_search_two_word_filename_substring_scores(search_engine):
    """A >=2-word run inside a filename earns the substring bonus."""

    payload = search_engine.search("Network Security")

    top = payload["results"][0]

    assert top["title"] == NETWORK_SECURITY_GUIDE
    assert top["filename_score"] >= 50


def test_search_content_only_match_reports_tfidf(search_engine):
    """
    A word that appears only in document text, never in a filename.

    This is the pure TF-IDF path: no filename signal, no phrase.
    """

    payload = search_engine.search("normalization")

    assert payload["pagination"]["total"] >= 1
    assert ends_with(payload, DATABASE_GUIDE)

    top = payload["results"][0]

    assert top["title"] == DATABASE_GUIDE
    assert top["match_type"] == "Content Match"
    assert top["content_score"] > 0
    assert top["filename_score"] == 0.0


def test_search_prefix_match_stays_inside_its_clamp(search_engine):
    """
    Prefix similarity is len(query)/len(term), clamped to 0.25 .. 0.90.

    Asserting the clamp rather than a value keeps the test meaningful for
    any corpus while still catching a removed or widened bound.
    """

    payload = search_engine.search("netwo")

    assert payload["results"]

    top = payload["results"][0]

    assert 0.25 <= top["prefix_similarity"] <= 0.90
    assert top["lexical_match_relevance"] > 0


def test_search_exact_numeric_token_is_a_lexical_exact_match(search_engine):
    """A digit run that is itself an indexed term matches exactly."""

    payload = search_engine.search("999")

    assert ends_with(payload, NETWORK_NOTES)

    top = payload["results"][0]

    assert top["exact_content_match"] is True
    assert top["lexical_match_relevance"] == 1.0


def test_search_numeric_substring_inside_a_longer_token(search_engine):
    """
    777 is not a term; it is a run inside unique777marker.

    The numeric branch scores it at ratio * 0.70, clamped to 0.20 .. 0.65,
    which must stay strictly below the exact-match weight of 1.0.
    """

    payload = search_engine.search("777")

    assert ends_with(payload, NETWORK_NOTES)

    top = payload["results"][0]

    assert 0.20 <= top["numeric_similarity"] <= 0.65
    assert top["exact_content_match"] is not True
    assert top["lexical_match_relevance"] < 1.0


def test_search_quoted_phrase_counts_occurrences(search_engine):
    """
    Phrase score grows with occurrences, log-scaled.

    The corpus repeats "machine learning" three times in one document.

    phrase_score is deliberately not asserted against a ceiling: a phrase
    that is also the document's own filename earns the occurrence score
    plus a filename bonus, so the total legitimately exceeds the 100 the
    occurrence formula alone is capped at (observed 167.07 here).
    """

    payload = search_engine.search('"machine learning"')

    assert ends_with(payload, MACHINE_LEARNING)

    top = payload["results"][0]

    assert top["phrase_occurrences"] == 3
    assert top["phrase_score"] > 100
    assert top["match_type"] == "Filename + Phrase"


def test_search_quoted_phrase_filters_out_non_adjacent_documents(search_engine):
    """
    Quoting means adjacent.

    Both PDF/DOCX documents contain "alpha" and "beta", but only one has
    them side by side. The other must be dropped entirely, not merely
    outranked - that is the quoted filter, and it is why the branch can
    return a bare list.
    """

    payload = search_engine.search('"alpha beta"')

    assert ends_with(payload, NETWORK_SECURITY_GUIDE)
    assert not ends_with(payload, DATABASE_GUIDE)


def test_search_unquoted_words_do_not_filter(search_engine):
    """
    The same two words unquoted keep both documents.

    Pinned against the test above so the difference is provably the
    quoting, not the vocabulary.
    """

    payload = search_engine.search("alpha beta")

    assert ends_with(payload, NETWORK_SECURITY_GUIDE)
    assert ends_with(payload, DATABASE_GUIDE)


def test_search_combines_filetype_filter_with_keywords(search_engine):
    """
    `network pdf` narrows by type and still ranks.

    Unlike the browse branch, this goes through the full pipeline, so the
    filetype_filter field is set on every result.
    """

    payload = search_engine.search("network pdf")

    assert payload["results"]

    for item in payload["results"]:

        assert item["filetype_filter"] == "pdf"
        assert item["path"].lower().endswith(".pdf")

    assert not ends_with(payload, DATABASE_GUIDE)
    assert not ends_with(payload, MACHINE_LEARNING)


# ------------------------------------------------------------
# pagination policy
# ------------------------------------------------------------

def test_search_clamps_limit_into_range(search_engine):

    assert search_engine.search(
        "network", 1, 0
    )["pagination"]["limit"] == 1

    assert search_engine.search(
        "network", 1, -5
    )["pagination"]["limit"] == 1

    assert search_engine.search(
        "network", 1, 9999
    )["pagination"]["limit"] == 50


def test_search_clamps_page_to_at_least_one(search_engine):

    assert search_engine.search(
        "network", 0, 10
    )["pagination"]["page"] == 1

    assert search_engine.search(
        "network", -5, 10
    )["pagination"]["page"] == 1


def test_search_page_beyond_the_end_clamps_to_the_last_page(search_engine):
    """
    An out-of-range page returns the last page, not an empty one.

    Surprising, and therefore worth pinning: paginate() clamps page to
    total_pages whenever there is at least one page.
    """

    payload = search_engine.search("network", 999, 1)

    pagination = payload["pagination"]

    assert pagination["total_pages"] >= 1
    assert pagination["page"] == pagination["total_pages"]
    assert pagination["has_next"] is False
    assert pagination["has_previous"] is True
    assert payload["results"]


def test_search_pages_partition_the_result_set(search_engine):
    """
    limit=1 pages must tile the full result list with no gaps or repeats.

    This is the property a UI paginator depends on.
    """

    # "network" reaches two documents in this corpus, so limit=1 must
    # produce exactly two pages to tile.
    full = search_engine.search("network", 1, 50)

    total = full["pagination"]["total"]

    assert total >= 2, "corpus no longer paginates this query"

    collected = []

    for page in range(1, total + 1):

        chunk = search_engine.search("network", page, 1)

        assert chunk["pagination"]["page"] == page
        assert len(chunk["results"]) == 1

        collected.append(chunk["results"][0]["path"])

    assert collected == [item["path"] for item in full["results"]]


# ------------------------------------------------------------
# snapshots and mutation visibility
# ------------------------------------------------------------

def test_search_accepts_a_caller_supplied_snapshot(search_engine):
    """
    A caller may reuse one coherent read view across several queries.

    The default path must produce the same payload as an explicit snapshot
    taken immediately before it.
    """

    snapshot = search_engine.snapshot()

    assert search_engine.search(
        "network",
        snapshot=snapshot,
    ) == search_engine.search("network")


def test_snapshot_isolates_a_search_from_a_rebuild(search_engine):
    """
    The guarantee a snapshot actually provides, and the reason it exists.

    rebuild() builds four fresh dicts and swaps all four references at once,
    so a view captured beforehand keeps ranking against the corpus as it
    was. That is what stops a background rebuild - which the Flask adapter
    runs on a worker thread - from tearing a result set in half mid-request.
    """

    snapshot = search_engine.snapshot()

    before = search_engine.search("normalization", snapshot=snapshot)

    assert ends_with(before, DATABASE_GUIDE)

    # Remove the document from disk and rebuild behind the snapshot's back.
    os.remove(
        search_engine.resolve_document_path(DATABASE_GUIDE)
    )

    search_engine.rebuild(log=lambda *args: None)

    frozen = search_engine.search("normalization", snapshot=snapshot)

    assert frozen == before

    fresh = search_engine.search("normalization")

    assert not ends_with(fresh, DATABASE_GUIDE)


def test_snapshot_shares_live_dicts_with_incremental_mutations(search_engine):
    """
    The limit of that guarantee, stated plainly instead of discovered later.

    A snapshot holds references to the four live dicts, not copies, so an
    in-place delete or import is visible through it immediately. That is the
    same visibility the pre-refactor handler had - it read those module-level
    dicts directly - and it is exactly why rebuild() swaps references rather
    than mutating them. Pinned so nobody mistakes snapshot() for a deep copy
    and builds a concurrency argument on top of it.
    """

    snapshot = search_engine.snapshot()

    before = search_engine.search("normalization", snapshot=snapshot)

    assert ends_with(before, DATABASE_GUIDE)

    result = search_engine.delete(DATABASE_GUIDE)

    assert result.outcome == DELETE_DELETED

    frozen = search_engine.search("normalization", snapshot=snapshot)

    assert not ends_with(frozen, DATABASE_GUIDE)

    assert frozen == search_engine.search("normalization")


def test_search_sees_an_import_without_a_restart(search_engine, tmp_path):
    """
    Offline mode has no server to bounce: import then search, immediately.
    """

    assert search_engine.search("kubernetes")["results"] == []

    import_paths(search_engine, [
        make_txt(
            str(tmp_path / "extra"),
            "kubernetes.txt",
            "kubernetes orchestrates containers\n",
        ),
    ])

    payload = search_engine.search("kubernetes")

    assert payload["pagination"]["total"] == 1
    assert ends_with(payload, "kubernetes.txt")


def test_search_sees_a_bulk_delete_immediately(search_engine):
    """The same visibility guarantee for the batch path."""

    assert ends_with(search_engine.search("network"), NETWORK_NOTES)

    result = search_engine.bulk_delete([
        NETWORK_NOTES,
        NETWORK_SECURITY_GUIDE,
    ])

    assert result.deleted_count == 2

    payload = search_engine.search("network")

    assert not ends_with(payload, NETWORK_NOTES)
    assert not ends_with(payload, NETWORK_SECURITY_GUIDE)


def test_rebuild_preserves_search_results(search_engine):
    """
    Rebuild is a persistence operation, not a re-ranking.

    Compared as a sorted (title, score) multiset rather than payload
    equality, because a rebuild legitimately reorders ties: before it, tied
    documents sit in import order, and after it they sit in filename order,
    since rebuild enumerates the folder with sorted(os.listdir()). The
    scores themselves must not move, and that is what this asserts.
    test_rebuild_ranks_ties_in_filename_order pins the new tie order, and
    test_rebuild_is_order_stable_in_place pins that repeating a rebuild
    stops changing anything.
    """

    queries = [
        "network",
        "network security",
        '"machine learning"',
        "normalization",
        "777",
        "pdf",
    ]

    before = {
        query: sorted(
            (item["title"], item["score"])
            for item in search_engine.search(query)["results"]
        )
        for query in queries
    }

    search_engine.rebuild(log=lambda *args: None)

    assert_consistent(search_engine)

    after = {
        query: sorted(
            (item["title"], item["score"])
            for item in search_engine.search(query)["results"]
        )
        for query in queries
    }

    assert after == before


def test_rebuild_is_order_stable_in_place(search_engine):
    """
    Rebuilding the same folder repeatedly must not shuffle results.

    The companion to the multiset comparison above, and the part that IS a
    guarantee: two rebuilds of one data folder enumerate identically, so
    tied scores come back in the same order every time. Search is therefore
    stable across background rebuilds on one device, which is what an
    offline Android client actually needs.

    Compared from the first rebuild onward, because a search before any
    rebuild still reflects import order rather than enumeration order.
    """

    query = "network"

    search_engine.rebuild(log=lambda *args: None)

    settled = search_engine.search(query)

    for _ in range(2):

        search_engine.rebuild(log=lambda *args: None)

        assert search_engine.search(query) == settled

    assert_consistent(search_engine)


CHILD_SCRIPT = """
import json, os, sys

sys.path.insert(0, sys.argv[1])

from search_engine.config import EngineConfig
from search_engine.engine import SearchEngine

data, corpus = sys.argv[2], sys.argv[3]

engine = SearchEngine(EngineConfig(data_folder=data))

handles = []
items = []

for name in sorted(os.listdir(corpus)):

    handle = open(os.path.join(corpus, name), "rb")
    handles.append(handle)
    items.append((name, handle))

engine.import_documents(items, log=lambda *args: None)

for handle in handles:
    handle.close()

# Reopen from disk. This is where a permuted persisted word order surfaces.
reopened = SearchEngine(EngineConfig(data_folder=data))

reopened.load()

queries = [
    "alpha beta gamma delta epsilon.txt",
    "alpha beta gamma",
    "delta epsilon",
    "epsilon delta",
    "gamma",
]

print(json.dumps(
    {query: reopened.search(query) for query in queries},
    sort_keys=True,
))
"""


def test_ranking_is_stable_across_hash_seeds(tmp_path):
    """
    Same corpus, different PYTHONHASHSEED, identical ranking.

    No in-process test can catch this defect class: the seed is fixed for a
    whole pytest run, so a hash-order-dependent write looks perfectly
    deterministic from the inside. filename_terms was deduplicated with
    set(), whose iteration order follows the seed, so a document's filename
    words could be persisted permuted. Ranking compares phrases and
    substrings against a space-joined word list, so the +100 exact-filename,
    +50 substring and phrase-in-filename bonuses were silently lost on the
    next load.

    Verified against the fix rather than assumed. With both storage paths
    reverted to set(), seven seeds produced seven distinct payloads and the
    top score for a phrase that is also a filename fell from 1.0 to 0.7506.
    With dict.fromkeys, every seed agrees. A five-word filename is used
    because a permutation of N words goes unnoticed one time in N!.
    """

    # Repository root, so the child process imports the same package.
    # Taken from this file rather than from search_engine, whose name is
    # shadowed in this module by the search_engine fixture.
    root = os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )

    corpus = tmp_path / "corpus"
    data = tmp_path / "data"

    make_txt(
        str(corpus),
        "alpha beta gamma delta epsilon.txt",
        "body text that is irrelevant to ordering\n",
    )

    payloads = set()

    for seed in ("0", "1", "2", "3"):

        # Same directory each time, index wiped each time. A fresh directory
        # per seed would make every payload differ on the absolute path
        # field and hide the signal being tested for.
        shutil.rmtree(str(data), ignore_errors=True)

        os.makedirs(str(data), exist_ok=True)

        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                CHILD_SCRIPT,
                root,
                str(data),
                str(corpus),
            ],
            capture_output=True,
            text=True,
            env=environment,
        )

        assert completed.returncode == 0, completed.stderr

        payloads.add(completed.stdout.strip())

    assert len(payloads) == 1, (
        f"ranking varied across {len(payloads)} hash seeds"
    )


def canonicalize_data_folder(payload, data_folder):
    """
    Serialize a payload with its absolute data folder replaced.

    Two corpora living in different directories differ in exactly one
    observable way: the `path` metadata records where the file is. Nothing
    else should depend on that, so replacing the folder makes the payloads
    directly comparable. JSON also fixes key order, which dict comparison
    would otherwise leave to luck.
    """

    import json

    return json.dumps(
        payload,
        sort_keys=True,
    ).replace(
        data_folder,
        "{{data_folder}}",
    )


def test_same_corpus_in_different_directories_ranks_identically(tmp_path):
    """
    Where the corpus lives must not change how it ranks.

    Complements the hash-seed test: that one holds the directory constant and
    varies the interpreter, this one varies the directory and holds the
    interpreter constant. Both are needed because the two ways ordering used
    to leak were independent - a permuted filename word order, and a rebuild
    that enumerated the folder in filesystem order.

    The two corpora are also imported in opposite orders and then rebuilt, so
    neither import order nor location can survive into the result. A ranking
    that depended on either would show up as a tie ordering difference.

    The four `uniform_*.txt` documents exist to create that difference on
    purpose. They carry identical content and identical filename weight, so
    they tie exactly, and a stable sort leaves them in enumeration order.
    Without them every query here has distinct scores and the test would pass
    even against a rebuild that enumerated the folder in filesystem order -
    which is precisely the regression it is meant to catch. They are created
    in non-alphabetical order so creation order and sorted order disagree.
    """

    corpus = sample_corpus(tmp_path)

    tie_directory = str(tmp_path / "ties")

    for name in ("zulu", "mike", "alpha", "bravo"):

        corpus.append(
            make_txt(
                tie_directory,
                f"uniform {name}.txt",
                "shared body text so the scores tie exactly\n",
            )
        )

    queries = [
        "",
        "network",
        "Network Notes.pdf",
        '"network"',
        "security",
        "normalization",
        "999",
        "the",
        "pdf",
        "zzzqqq",
        # The tie-sensitive one: four equal scores, so the order is decided
        # entirely by how the rebuild enumerated the folder.
        "uniform",
        "shared",
    ]

    runs = []

    tie_payloads = []

    for name, ordered in (
        ("alpha", corpus),
        ("beta", list(reversed(corpus))),
    ):

        data_folder = os.path.abspath(
            str(tmp_path / name)
        )

        os.makedirs(data_folder, exist_ok=True)

        engine = SearchEngine(
            EngineConfig(data_folder=data_folder)
        )

        import_paths(engine, ordered)

        engine.rebuild(log=lambda *args: None)

        assert_consistent(engine)

        payloads = [
            engine.search(query)
            for query in queries
        ]

        # The "uniform" query is the tie case; keep it for the guard below.
        tie_payloads.append(payloads[queries.index("uniform")])

        runs.append([
            canonicalize_data_folder(payload, data_folder)
            for payload in payloads
        ])

    assert runs[0] == runs[1], (
        "the same corpus ranked differently in two directories"
    )

    # Guard the guard. Without these the comparison could pass by never
    # exercising anything: the corpora must really have been indexed, and the
    # tie query must really have produced equal scores in an order that only
    # enumeration decides.
    assert "Network_Notes.pdf" in runs[0][1]

    for payload in tie_payloads:

        titles = [
            item["title"]
            for item in payload["results"]
        ]

        scores = {
            item["score"]
            for item in payload["results"]
        }

        assert len(titles) == 4, titles

        assert len(scores) == 1, (
            f"the tie documents did not tie: {scores}"
        )

    # And the agreed order is the sorted one, not either import order.
    assert [
        item["title"]
        for item in tie_payloads[0]["results"]
    ] == [
        "uniform_alpha.txt",
        "uniform_bravo.txt",
        "uniform_mike.txt",
        "uniform_zulu.txt",
    ]


def test_filename_terms_persist_in_tokenization_order(engine, tmp_path):
    """
    Row order in filename_terms must equal tokenization order.

    search.py joins these words with spaces, then compares the normalized
    query and phrase against that string, so word order carries meaning: a
    permutation silently drops the +100 exact-filename, +50 substring and
    phrase-in-filename bonuses. Deduplicating with set() made the persisted
    order follow PYTHONHASHSEED instead, so the same corpus ranked
    differently between processes. dict.fromkeys keeps the deduplication
    and the order.

    A five-word name is deliberate: with N words, a permutation goes
    unnoticed only one time in N!, so a two-word name could pass by luck.
    """

    import_paths(engine, [
        make_txt(
            str(tmp_path / "ordered"),
            "alpha beta gamma delta epsilon.txt",
            "body text irrelevant to ordering\n",
        ),
    ])

    filename = "alpha_beta_gamma_delta_epsilon.txt"

    expected = ["alpha", "beta", "gamma", "delta", "epsilon"]

    assert engine.snapshot().filename_index[filename] == expected

    def persisted_order():

        connection = sqlite3.connect(
            engine.config.sqlite_db_file
        )

        try:

            return [
                row[0]
                for row in connection.execute(
                    "SELECT term FROM filename_terms "
                    "WHERE filename = ? ORDER BY rowid",
                    (filename,),
                )
            ]

        finally:

            connection.close()

    # The incremental path (insert_document).
    assert persisted_order() == expected

    # The rebuild path (sync_from_snapshot) must agree with it.
    engine.rebuild(log=lambda *args: None)

    assert persisted_order() == expected

    assert engine.snapshot().filename_index[filename] == expected


def test_rebuild_ranks_ties_in_filename_order(engine, tmp_path):
    """
    Equal-scoring documents must come back in filename order after a rebuild.

    The final sort is descending by score and stable, so tied documents keep
    the order the rebuild inserted them in. That used to be os.listdir
    order - whatever the filesystem returned - so two deployments holding
    identical corpora could disagree about tie order, and one machine could
    see its results reshuffled by a rebuild or a restore.

    Six tied documents, not two or three: an unsorted enumeration happens to
    come out alphabetical one time in N!, so a small corpus could pass by
    luck and prove nothing.
    """

    corpus = str(tmp_path / "tied")

    names = [
        "zulu",
        "yankee",
        "xray",
        "whiskey",
        "victor",
        "uniform",
    ]

    for name in names:

        make_txt(
            corpus,
            f"{name}.txt",
            "identical body text for every document\n",
        )

    import_paths(engine, [
        os.path.join(corpus, f"{name}.txt")
        for name in names
    ])

    # Before a rebuild the tie order is import order, which is legitimate:
    # the caller chose it. Enumerating a folder is what must be normalized.
    before = engine.search("identical")

    assert titles(before) == [f"{name}.txt" for name in names]

    engine.rebuild(log=lambda *args: None)

    after = engine.search("identical")

    scores = [item["score"] for item in after["results"]]

    assert len(scores) == len(names)
    assert len(set(scores)) == 1, "these documents were meant to tie"

    assert titles(after) == sorted(titles(after))

    assert titles(after) == [
        f"{name}.txt"
        for name in sorted(names)
    ]

    assert_consistent(engine)


# ============================================================
# SIMILARITY CLAMPS THE GOLDEN VECTORS CANNOT REACH
# ============================================================
#
# tests/golden/search_engine_vectors.json is the portability contract. A
# perturbation sweep of every ranking constant in search.py - alter one
# constant, regenerate, compare - measures how much of the ranking it
# actually pins: 20 of the 26 change at least one recorded payload. The six
# that do not are each accounted for, and three of them are pinned here.
#
# Structurally dead, so no corpus could ever pin them:
#
#   filename_weight 0.05 in the quoted-only and ordinary branches. Both are
#   entered only when no document scored on the filename, which forces
#   filename_relevance to 0.0 for every document, so the weight multiplies
#   zero. Confirmed by tracing those two locals across all 50 recorded cases.
#
#   final_score = 0.75 for a filetype-only query. A keep-alive, not a ranking
#   constant: those queries publish a hardcoded relevance_score of 1.0, and
#   0.75 only has to be positive to clear the `final_score <= 0` filter.
#
# Live ranking constants that a six-document corpus happens not to reach, and
# which are therefore pinned below with terms built to land on them:
#
#   prefix similarity floor      min(0.90, max(0.25, len(word)/len(term)))
#   numeric similarity ceiling   min(0.65, max(0.20, ratio * 0.70))
#   numeric similarity factor    the 0.70 in that same expression
#
# The prefix ceiling 0.90 is pinned by the vectors themselves, through three
# cases added for exactly that reason.
#
# These do hardcode values, which the SEARCH section otherwise avoids on the
# grounds that tuned scores belong to the vectors and the parity tool. That
# reasoning does not apply here: no other artifact in the repository observes
# these three numbers, so this is the only place a regression or a wrong port
# could be caught.
#
# The contrast cases matter as much as the clamped ones. Asserting only that
# a query yields 0.25 would also pass if the expression collapsed to a
# constant, so each clamp is paired with a nearby query that lands inside the
# unclamped range.

CLAMP_TEXT = "internationalization x12345678901234 abc12345def"


def make_clamp_engine(engine):
    """
    An engine whose only terms are built to reach each similarity clamp.

        internationalization  20 chars, so a 4-char prefix is a 0.20 ratio
        x12345678901234       15 chars with a 14-char numeric run inside it,
                              not at the start, for a 0.9333 ratio
        abc12345def           11 chars with a 5-char numeric run in the
                              middle, for a 0.4545 ratio
    """

    engine.index_extracted(
        "clamps.txt",
        CLAMP_TEXT,
        [{"page": 1, "text": CLAMP_TEXT}],
    )

    return engine


def clamp_result(engine, query):
    """The single ranked row a clamp-corpus query produces."""

    page = engine.search(query)

    rows = page["results"] if isinstance(page, dict) else page

    assert rows, f"{query!r} should have matched the clamp corpus"

    return rows[0]


def test_prefix_similarity_floor_is_pinned(engine):
    """
    "inte" is 4 of the 20 characters in "internationalization", a ratio of
    0.20, which the floor raises to 0.25.
    """

    make_clamp_engine(engine)

    assert clamp_result(engine, "inte")["prefix_similarity"] == 0.25


def test_prefix_similarity_inside_the_range_is_not_clamped(engine):
    """
    The contrast case: "internat" is 8 of 20, a ratio of 0.4, comfortably
    between the 0.25 floor and the 0.90 ceiling, so it passes through
    unchanged. Without this the test above would also pass if prefix
    similarity had collapsed to a constant 0.25.
    """

    make_clamp_engine(engine)

    assert clamp_result(engine, "internat")["prefix_similarity"] == 0.4


def test_numeric_similarity_ceiling_is_pinned(engine):
    """
    "12345678901234" is a 14-character numeric run inside the 15-character
    term "x12345678901234", and it is not a prefix of it, so it takes the
    numeric-substring branch. The ratio 14/15 = 0.9333 times 0.70 is 0.6533,
    which the ceiling caps at 0.65.
    """

    make_clamp_engine(engine)

    assert clamp_result(engine, "12345678901234")["numeric_similarity"] == 0.65


def test_numeric_similarity_multiplier_is_pinned(engine):
    """
    "12345" is 5 of the 11 characters of "abc12345def", a ratio of 0.4545.
    Multiplied by 0.70 that is 0.318181..., which rounds to 0.3182 and sits
    strictly between the 0.20 floor and the 0.65 ceiling, so the multiplier
    itself is what this value reports.

    This is the case the vectors cannot reach: their only numeric-substring
    query is "777" inside "unique777marker", a ratio of 0.2, whose product
    0.14 is below the floor and so reports 0.20 whatever the multiplier is.
    """

    make_clamp_engine(engine)

    assert clamp_result(engine, "12345")["numeric_similarity"] == 0.3182


def test_numeric_similarity_floor_is_pinned(engine):
    """
    The floor the multiplier test depends on, asserted directly: a numeric
    run short enough that ratio * 0.70 falls under 0.20 reports 0.20.
    """

    # The numeric-substring branch needs at least two digits, and the run
    # must not start the term, so "12" sits at the end of a longer word.
    engine.index_extracted(
        "floors.txt",
        "abcdefghij12",
        [{"page": 1, "text": "abcdefghij12"}],
    )

    # 2 of 12 characters is a 0.1667 ratio; 0.1667 * 0.70 = 0.1167, which is
    # under the floor, so 0.20 is reported.
    assert clamp_result(engine, "12")["numeric_similarity"] == 0.2


# ============================================================
# REACHABILITY THRESHOLDS AND THE PHRASE-FREQUENCY TAIL
#
# Constants the six-document vector corpus can never reach:
# query-shape gates (minimum word lengths, minimum quoted-query
# length) and the phrase-frequency tail (two-plus occurrences, the
# 25-occurrence reference cap). tools/verify_vector_coverage.py
# reports every one of these as NOT PINNED BY THE VECTORS; they are
# pinned here instead, and each test was falsified by perturbing
# the exact constant it names. ANDROID.md section 9 keeps the
# full classification of all 32 vector-unpinned constants.
# ============================================================


def test_prefix_match_requires_a_three_character_word(engine):
    """
    The prefix gate is `len(word) >= 3`, and both sides are observable.

    "dat" is 3 of the 8 characters of "database", so it passes the gate
    with a 0.375 ratio. "da" is 2 characters, so the gate refuses it
    before any similarity is computed - and nothing else matches either,
    so the page comes back empty. Raising the gate to 4 kills the "dat"
    row; lowering it to 2 gives "da" a row at the 0.25 floor.
    """

    engine.index_extracted(
        "notes.txt",
        "database migration guide",
        [{"page": 1, "text": "database migration guide"}],
    )

    assert clamp_result(engine, "dat")["prefix_similarity"] == 0.375

    assert engine.search("da")["results"] == []


def test_single_word_quoted_query_still_takes_the_quoted_path(engine):
    """
    Quote detection is `len(query) >= 2` plus surrounding double quotes,
    so even the three-character query '"z"' is a quoted search.

    A one-word phrase never sets phrase_query (that needs two or more
    phrase words), and "z" is not a substring of "beta txt", so the
    filename-phrase bonus cannot fire either. The quoted filter then
    finds no phrase_match documents and answers with the legacy bare
    list - while the same word unquoted answers with the normal
    paginated page. Raising the detection length to 4 or more makes
    '"z"' unquoted and this test fails on the response shape.

    Detection lengths 1, 2 and 3 are provably identical in behavior:
    the only queries that differ between them ('"' and '""') yield an
    empty quoted phrase, and every downstream guard (`if
    quoted_phrase:`) treats that exactly like no quote at all. The
    exact value 2 is therefore unpinnable by ANY test; this pins the
    observable boundary.
    """

    engine.index_extracted(
        "beta.txt",
        "alpha beta gamma",
        [{"page": 1, "text": "alpha beta gamma"}],
    )

    assert engine.search('"z"') == []

    unquoted = engine.search("z")

    assert isinstance(unquoted, dict)
    assert unquoted["results"] == []


def test_minimal_quoted_phrase_scores_through_the_quoted_only_branch(engine):
    """
    The shortest quoted phrase that reaches phrase scoring at all:
    two words, one occurrence, no filename signal - weights
    .05/.15/.80. filename_relevance is 0, content_relevance is 1.0
    (single document, normalized by the max), and phrase_relevance is
    50/100 = 0.5, so the published score is 0.15 + 0.80 * 0.5 = 0.55
    exactly. Were the query to lose its quoted status, the normal
    branch weights .05/.60/.20/.15 would publish a different score.
    """

    engine.index_extracted(
        "notes.txt",
        "alpha beta gamma",
        [{"page": 1, "text": "alpha beta gamma"}],
    )

    row = clamp_result(engine, '"alpha beta"')

    assert row["phrase_occurrences"] == 1
    assert row["phrase_score"] == 50.0
    assert row["score"] == 0.55


def test_phrase_frequency_branch_starts_at_two_occurrences(engine):
    """
    `if phrase_occurrences <= 1` pinned from both sides of the boundary.

    One occurrence takes the flat 50.0. Two occurrences take the
    logarithmic curve: 50 + 50 * ln(2)/ln(25) = 60.7669 rounded. Moving
    the boundary to <= 2 collapses the second document to 50.0 and this
    test fails. Moving it to <= 0 is provably unobservable: the branch
    only runs when occurrences > 0, and at 1 occurrence the curve
    itself yields 50 + 50 * ln(1)/ln(25) = 50.0.

    The expected value is computed from math.log, not transcribed: the
    engine's own reference comment used to claim ~57.5 here, which the
    curve never produced.
    """

    engine.index_extracted(
        "notes-one.txt",
        "alpha beta gamma delta",
        [{"page": 1, "text": "alpha beta gamma delta"}],
    )
    engine.index_extracted(
        "notes-two.txt",
        "alpha beta gamma alpha beta delta",
        [{"page": 1, "text": "alpha beta gamma alpha beta delta"}],
    )

    page = engine.search('"alpha beta"')

    rows = {row["title"]: row for row in page["results"]}

    assert rows["notes-one.txt"]["phrase_occurrences"] == 1
    assert rows["notes-one.txt"]["phrase_score"] == 50.0

    expected_two = round(
        50.0 + 50.0 * math.log(2) / math.log(25),
        4,
    )

    assert expected_two == 60.7669

    assert rows["notes-two.txt"]["phrase_occurrences"] == 2
    assert rows["notes-two.txt"]["phrase_score"] == expected_two


def test_phrase_frequency_ratio_caps_at_the_reference(engine):
    """
    `frequency_ratio = min(ln(n)/ln(25), 1.0)` and the score cap
    `min(content_phrase_score, 100.0)`, pinned at and past the
    25-occurrence reference.

    25 occurrences -> ratio exactly 1.0 -> score exactly 100.0.
    50 occurrences -> the raw ratio would be ln(50)/ln(25) = 1.0879,
    but the cap holds it at 1.0 -> also 100.0. Lowering the ratio cap
    (say to 0.9) reports 95.0 for both documents; lowering the score
    cap below 100 clips the exact 100.0 they produce. Raising the
    ratio cap is unobservable only because the score cap catches the
    overshoot - the two caps are a documented pair (ANDROID.md 9).
    """

    engine.index_extracted(
        "cap25.txt",
        "alpha beta " * 25,
        [{"page": 1, "text": "alpha beta " * 25}],
    )
    engine.index_extracted(
        "cap50.txt",
        "alpha beta " * 50,
        [{"page": 1, "text": "alpha beta " * 50}],
    )

    page = engine.search('"alpha beta"')

    scores = {
        row["title"]: row["phrase_score"]
        for row in page["results"]
    }

    assert scores == {"cap25.txt": 100.0, "cap50.txt": 100.0}


def test_numeric_similarity_is_rounded_to_four_places(engine):
    """
    The published numeric_similarity is round(raw, 4), and this is a
    shape where the rounding actually bites: "7777" is 4 of the 13
    characters of "report7777777", so raw = (4/13) * 0.70 =
    0.2153846... and the published value must be 0.2154. At three
    places it would read 0.215; unrounded it would carry the full
    float. The multiplier test (0.318181... -> 0.3182) exercises the
    same round; this one names the precision directly.
    """

    engine.index_extracted(
        "memo.txt",
        "see report7777777 for details",
        [{"page": 1, "text": "see report7777777 for details"}],
    )

    raw = (4 / 13) * 0.70

    # The chosen lengths must be able to bite, or the test proves nothing.
    assert round(raw, 4) != round(raw, 3)

    assert clamp_result(engine, "7777")["numeric_similarity"] == round(raw, 4)


def test_search_after_restart_matches_search_before_it(
    search_engine,
    data_folder,
):
    """
    A reopened engine must rank identically.

    This is the offline Android promise: the persisted index is not a
    degraded copy of the in-memory one.

    It did not hold while filename words were deduplicated with set(), and
    this test carried an xfail marker recording the mechanism. Both storage
    paths now use dict.fromkeys, so it is asserted outright;
    test_filename_terms_persist_in_tokenization_order pins the cause.
    """

    queries = [
        "network",
        "normalization",
        '"machine learning"',
        "netwo",
        "pdf",
    ]

    before = {
        query: search_engine.search(query)
        for query in queries
    }

    reopened = SearchEngine(EngineConfig(data_folder=data_folder))

    reopened.load()

    after = {
        query: reopened.search(query)
        for query in queries
    }

    assert after == before


def test_get_document_returns_metadata_and_a_usable_path(engine, tmp_path):
    """
    One call gives an offline client everything it needs to open a file.

    The path must be absolute, real, and inside the data folder - that last
    property is the security boundary, not a convenience.
    """

    seeded = seeded_engine(engine, tmp_path)

    document = seeded.get_document("Network_Notes.pdf")

    assert document is not None
    assert document["filename"] == "Network_Notes.pdf"
    assert document["exists"] is True

    metadata = document["metadata"]

    assert metadata["title"] == "Network_Notes.pdf"
    assert metadata["total_words"] > 0
    assert metadata["page_count"] >= 1

    path = document["path"]

    assert os.path.isabs(path)
    assert os.path.isfile(path)

    folder = os.path.realpath(seeded.config.data_folder)

    assert os.path.commonpath([folder, os.path.realpath(path)]) == folder

    # The metadata is a copy, so a caller cannot corrupt the index through it.
    metadata["title"] = "tampered"

    assert (
        seeded.get_document("Network_Notes.pdf")["metadata"]["title"]
        == "Network_Notes.pdf"
    )


def test_get_document_unknown_returns_none(engine, tmp_path):

    seeded = seeded_engine(engine, tmp_path)

    assert seeded.get_document("nope.pdf") is None


def test_get_document_rejects_an_escaping_name(engine, tmp_path):
    """
    A traversal name is not indexed, so there is nothing to return.

    Pinned because the interesting failure mode would be a path outside the
    data folder leaking out through this convenience method.
    """

    seeded = seeded_engine(engine, tmp_path)

    assert seeded.get_document("../../etc/passwd") is None

    assert seeded.get_document("..") is None


def test_get_document_reports_a_file_missing_from_disk(engine, tmp_path):
    """
    Indexed but deleted behind the engine's back: exists must say so.

    An offline client has to distinguish "never indexed" from "the user
    cleared it from a file manager", because only the second is fixable by
    a rebuild.
    """

    seeded = seeded_engine(engine, tmp_path)

    filename = "Network_Notes.pdf"

    os.remove(seeded.resolve_document_path(filename))

    document = seeded.get_document(filename)

    assert document is not None
    assert document["exists"] is False
    assert document["metadata"]["title"] == filename

    seeded.rebuild(log=lambda *args: None)

    assert seeded.get_document(filename) is None

    assert_consistent(seeded)


def test_engine_has_no_web_stack_dependency():
    """
    The core must be usable with no web stack installed.

    Asserted structurally over the AST rather than by scanning text, because
    the modules legitimately *discuss* Flask and jsonify in their docstrings
    while never importing or calling them. An offline Android backend
    inherits whatever this package imports, so a stray `import flask` would
    drag a web server into an APK.
    """

    import ast
    import pathlib

    import search_engine

    package = pathlib.Path(search_engine.__file__).parent

    forbidden_modules = {
        "flask",
        "flask_cors",
        "werkzeug",
        "requests",
        "http",
    }

    forbidden_calls = {"jsonify"}

    offenders = []

    for path in sorted(package.glob("*.py")):

        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
        )

        for node in ast.walk(tree):

            if isinstance(node, ast.Import):

                for alias in node.names:

                    root = alias.name.split(".")[0]

                    if root in forbidden_modules:
                        offenders.append((
                            path.name,
                            f"import {alias.name}",
                        ))

            elif isinstance(node, ast.ImportFrom):

                if node.module:

                    root = node.module.split(".")[0]

                    if root in forbidden_modules:
                        offenders.append((
                            path.name,
                            f"from {node.module}",
                        ))

            elif isinstance(node, ast.Call):

                label = None

                if isinstance(node.func, ast.Name):
                    label = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    label = node.func.attr

                if label in forbidden_calls:
                    offenders.append((
                        path.name,
                        f"{label}()",
                    ))

            elif isinstance(node, ast.Attribute):

                # Scheduling belongs to the runtime layer, never the core.
                if (
                    node.attr == "Thread"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "threading"
                ):
                    offenders.append((
                        path.name,
                        "threading.Thread",
                    ))

    assert offenders == []


def test_flask_adapter_has_no_domain_logic():
    """
    The complementary half of the architecture rule.

    `test_engine_has_no_web_stack_dependency` proves the core does not reach
    up into the transport. This proves the adapter does not reach down into
    the domain. app.py is allowed to parse requests, map engine results onto
    HTTP responses, and own background scheduling - the three things that are
    genuinely transport concerns. It is not allowed to tokenize, extract,
    score, paginate, or touch SQLite, because every one of those would be a
    second implementation that an offline backend does not share.

    Enforced over the AST rather than by grepping for words, for the same
    reason as the core-side guard: the module legitimately discusses ranking
    in its docstrings.

    Two lists, because the boundary is not symmetric. Imports are an allowlist
    (a new dependency is a decision worth failing a test over); domain calls
    are a denylist (the specific functions that would duplicate the engine).
    """

    import ast

    repository_root = os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )

    adapter_path = os.path.join(
        repository_root,
        "app.py",
    )

    assert os.path.isfile(adapter_path)

    tree = ast.parse(
        open(adapter_path, encoding="utf-8").read(),
        filename=adapter_path,
    )

    # Transport, runtime, and the engine's public adapter surface. Anything
    # else means the adapter grew a dependency it should not have.
    allowed_modules = {
        "flask",
        "flask_cors",
        "os",
        "threading",
        "time",
        "search_engine.config",
        "search_engine.engine",
        "search_engine.filenames",
        "search_engine.results",
    }

    # The engine internals. Reaching past the facade into these is how a
    # second ranking implementation starts.
    forbidden_internals = {
        "search_engine.search",
        "search_engine.storage",
        "search_engine.tokenizer",
        "search_engine.extraction",
        "search_engine.index",
        "search_engine.rebuild",
        "search_engine.snippets",
        "search_engine.pagination",
        "search_engine.query",
        "search_engine.status",
    }

    # Modules whose presence would mean the adapter is doing domain work
    # itself: parsing documents, ranking, or persisting.
    forbidden_modules = forbidden_internals | {
        "sqlite3",
        "json",
        "math",
        "re",
        "shutil",
        "PyPDF2",
        "docx",
    }

    # Functions that would duplicate the engine. Note that
    # normalize_requested_filenames is permitted: validating a request body
    # before it reaches the engine is an adapter's job.
    forbidden_calls = {
        "tokenize",
        "tokenize_filename",
        "extract_text",
        "extract_pages",
        "search_documents",
        "build_snapshot_from_folder",
        "paginate",
        "empty_query_response",
        "secure_filename",
        "sanitize_upload_filename",
        "normalize_search_query",
        "parse_filetype_filter",
        "build_snippet_result",
        "get_snippet_and_page",
        "count_phrase_occurrences",
        "read_json_snapshot",
        "write_json_snapshot",
        "atomic_write_json",
    }

    offenders = []

    for node in ast.walk(tree):

        if isinstance(node, ast.Import):

            for alias in node.names:

                if alias.name not in allowed_modules:
                    offenders.append((
                        node.lineno,
                        f"import {alias.name}",
                    ))

        elif isinstance(node, ast.ImportFrom):

            module = node.module or ""

            if module in forbidden_modules:
                offenders.append((
                    node.lineno,
                    f"from {module}",
                ))

            elif module not in allowed_modules:
                offenders.append((
                    node.lineno,
                    f"from {module}",
                ))

        elif isinstance(node, ast.Call):

            if isinstance(node.func, ast.Name):
                label = node.func.id
            elif isinstance(node.func, ast.Attribute):
                label = node.func.attr
            else:
                continue

            if label in forbidden_calls:
                offenders.append((
                    node.lineno,
                    f"{label}()",
                ))

    assert offenders == [], (
        "app.py is no longer a thin adapter; move this domain logic into "
        f"search_engine/: {offenders}"
    )
