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

import os
import sqlite3

import pytest

from pdf_writer import write_pdf

from search_engine.config import (
    EngineConfig,
)
from search_engine.engine import (
    SearchEngine,
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

    One asymmetry is intentional and pre-existing: a supported file that has
    no readable text is never indexed (an upload records it as failed, a
    rebuild skips it) yet it does remain on disk. So the filesystem may hold
    a superset of the index, and every extra file must be provably
    unreadable. Anything else is a leak and fails here.
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

    Known limitation, preserved from the original implementation and pinned
    here on purpose: memory is mutated before the commit, so after a
    rollback the in-memory index and SQLite disagree until the next
    rebuild. Fixing that is a deliberate behavior change tracked separately;
    this test documents today's contract so the change is visible when it
    happens.
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

    # Known limitation, pinned deliberately: the filesystem removal and the
    # in-memory mutation are not participants in the transaction, so they
    # already happened. This matches the original implementation.
    assert not os.path.isfile(
        os.path.join(
            engine.config.data_folder,
            "security.txt",
        )
    )
    assert not engine.has_document("security.txt")

    # Recovery path: a restart reloads from SQLite (the authoritative
    # store), and a rebuild re-synchronizes all three stores with what is
    # actually on disk.
    reloaded = SearchEngine(engine.config)

    assert reloaded.counts()["documents"] == 3

    reloaded.rebuild(log=lambda *args: None)

    assert reloaded.counts()["documents"] == 1

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
