"""
SearchEngine: the single authoritative engine API.

This is the object every adapter talks to. The Flask HTTP API, the offline
Android local backend and the tests all drive the same class, which is what
keeps "one core implementation, multiple adapters" true instead of
aspirational.

The engine exposes business operations, not implementation details:

    engine.load()                  startup: legacy JSON, then SQLite
    engine.index_document(...)     index/replace one document
    engine.import_documents(...)   import a batch in ONE transaction
    engine.remove_document(...)    unindex one document
    engine.delete(...)             delete one document (file + index + db)
    engine.bulk_delete(...)        delete many in ONE transaction
    engine.rebuild()               rebuild the whole corpus
    engine.search(...)             rank and paginate a query
    engine.status() / counts()     introspection
    engine.snapshot()              one coherent read view

What the engine deliberately does NOT do:

* import Flask, read a request, build a response, or jsonify
* create a thread or schedule background work (the runtime layer decides
  when to rebuild; the engine only performs the rebuild)
* decide HTTP status codes (it returns structured results; adapters map them)

Persistence boundary: callers that need several mutations inside ONE
transaction pass `connection` plus `commit=False` and own the commit or
rollback. Callers that pass nothing get a private, committed, closed
connection. That is what makes bulk upload and bulk delete atomic.
"""

import os
import time

from search_engine.config import (
    EngineConfig,
    default_data_folder,
)
from search_engine.extraction import (
    extract_pages,
    extract_text,
)
from search_engine.index import (
    IndexState,
)
from search_engine.rebuild import (
    REBUILD_BANNER,
    build_snapshot_from_folder,
)
from search_engine.status import (
    IndexStatusTracker,
)
from search_engine.storage import (
    SqliteStore,
    read_json_snapshot,
    write_json_snapshot,
)
from search_engine.tokenizer import (
    tokenize,
    tokenize_filename,
)


class SearchEngine:
    """
    Transport-independent search engine.

    Construction is cheap except for `load`, which reads the persisted
    index; pass `load=False` to build an engine without touching storage.
    """

    def __init__(
        self,
        config=None,
        data_folder=None,
        load=True,
    ):

        if config is None:
            config = EngineConfig(
                data_folder
                or default_data_folder()
            )

        self.config = config
        self.state = IndexState()
        self.store = SqliteStore(config)
        self.status_tracker = IndexStatusTracker()

        if load:
            self.load()

    # --------------------------------------------------------
    # STARTUP
    # --------------------------------------------------------

    def load(self):
        """
        Load the persisted index.

        Sequence (unchanged from the original startup):

        1. read the four legacy JSON files into memory, if present
        2. create the SQLite schema; if the database is empty but memory is
           not, migrate memory into SQLite once
        3. load the persisted SQLite snapshot over memory, unless the
           database is empty (in which case the JSON snapshot is kept)

        SQLite is the authoritative incremental store from here on.
        """

        self.config.ensure_data_folder()

        self.state.replace_all(
            *read_json_snapshot(self.config)
        )

        persisted = self.store.initialize(
            self.state.snapshot()
        )

        if persisted:
            self.state.replace_all(*persisted)

        self.mark_ready()

        return self.status()

    # --------------------------------------------------------
    # INTROSPECTION
    # --------------------------------------------------------

    def snapshot(self):
        """One coherent read view of the active index."""

        return self.state.snapshot()

    def counts(self):
        """Sizes of the four in-memory structures."""

        return self.state.counts()

    def sqlite_counts(self):
        """Row counts per SQLite table."""

        return self.store.table_counts()

    def has_document(self, filename):
        """True when the document is indexed."""

        return self.state.has_document(filename)

    def get_document_metadata(self, filename):
        """A copy of one document's metadata, or None."""

        return self.state.get_document_metadata(filename)

    def document_term_counts(self, filename):
        """Terms one document contributes, per SQLite."""

        return self.store.document_term_counts(filename)

    def status(self):
        """
        Corpus counters plus indexing state.

        This is exactly what the status endpoint reports, and what an
        Android status banner reports: the same values from the same source.
        """

        counts = self.state.counts()

        return {
            "documents": counts["documents"],
            "content_terms": counts["content_terms"],
            "filenames_indexed": counts["filenames_indexed"],
            "page_text_entries": counts["page_text_entries"],
            "data_folder": self.config.data_folder,
            "indexing": self.status_tracker.snapshot(),
        }

    def index_status(self):
        """Current INDEXING / READY / ERROR status."""

        return self.status_tracker.snapshot()

    def set_index_status(
        self,
        state,
        message,
        started_at=None,
        completed_at=None,
        last_error=None,
    ):
        """
        Record an indexing status transition.

        Owned by the runtime layer, which is what knows when a rebuild
        starts and ends. The engine performs rebuilds; it does not schedule
        them.
        """

        self.status_tracker.set(
            state,
            message,
            started_at=started_at,
            completed_at=completed_at,
            last_error=last_error,
        )

    def mark_ready(self):
        """Mark the index searchable after a successful mutation."""

        self.status_tracker.mark_ready(
            completed_at=time.time()
        )

    # --------------------------------------------------------
    # PATH SAFETY
    # --------------------------------------------------------

    def resolve_document_path(self, filename):
        """
        Resolve a document to a real path inside the data folder.

        Returns None when the name would escape the data folder, so no
        adapter can be talked into reading or deleting outside the corpus.
        """

        if not filename:
            return None

        data_root = os.path.realpath(
            self.config.data_folder
        )

        requested_path = os.path.realpath(
            os.path.join(
                self.config.data_folder,
                os.path.basename(filename),
            )
        )

        try:
            if os.path.commonpath(
                [data_root, requested_path]
            ) != data_root:
                return None
        except ValueError:
            return None

        return requested_path

    # --------------------------------------------------------
    # INDEXING
    # --------------------------------------------------------

    def index_document(
        self,
        filename,
        file_path,
        connection=None,
        commit=True,
    ):
        """
        Index exactly one document that already lives on disk.

        Complexity is proportional to the document being indexed, not to the
        number of existing documents: the previous postings for this
        filename are removed and the new ones inserted.

        Raises ValueError when the document has no readable text, which is
        how an empty or unparseable upload is rejected.

        Returns the document metadata.
        """

        text = extract_text(
            file_path,
            filename,
        )

        content_words = tokenize(text)

        term_counts = {}

        for word in content_words:
            term_counts[word] = (
                term_counts.get(word, 0)
                + 1
            )

        pages = extract_pages(
            file_path,
            filename,
        )

        filename_without_extension = (
            os.path.splitext(filename)[0]
        )

        filename_words = tokenize_filename(
            filename_without_extension
        )

        if not content_words:
            raise ValueError(
                "Document contains no readable text."
            )

        metadata = {
            "title": filename,
            "path": os.path.abspath(file_path),
            "total_words": len(content_words),
            "page_count": len(pages),
        }

        old_term_counts = self.store.document_term_counts(
            filename
        )

        # One atomic in-memory step: a concurrent search never observes the
        # document as missing while it is being replaced.
        self.state.replace_document(
            filename,
            old_term_counts,
            metadata,
            filename_words,
            pages,
            term_counts,
        )

        own_connection = connection is None

        if own_connection:
            connection = self.store.connection()

        try:
            self.store.insert_document(
                connection,
                filename,
                metadata,
                term_counts,
                filename_words,
                pages,
            )

            if commit:
                connection.commit()

        finally:
            if own_connection:
                connection.close()

        return metadata

    def remove_document(
        self,
        filename,
        connection=None,
        commit=True,
    ):
        """
        Remove exactly one document from memory and SQLite.

        Does not touch the filesystem: deleting the stored file is the
        caller's decision, which is why `delete` and `bulk_delete` exist.
        """

        old_term_counts = self.store.document_term_counts(
            filename
        )

        self.state.remove_document(
            filename,
            old_term_counts,
        )

        own_connection = connection is None

        if own_connection:
            connection = self.store.connection()

        try:
            self.store.delete_document(
                connection,
                filename,
            )

            if commit:
                connection.commit()

        finally:
            if own_connection:
                connection.close()

    # --------------------------------------------------------
    # REBUILD
    # --------------------------------------------------------

    def rebuild(self, log=print):
        """
        Rebuild the complete index from the data folder.

        The new snapshot is fully built and persisted before the active one
        is swapped, so searches never see a partial index. The legacy JSON
        snapshot is rewritten and SQLite is resynchronized, which keeps all
        three views (memory, SQLite, filesystem) consistent.
        """

        log("")
        log(REBUILD_BANNER)
        log("REBUILDING SEARCH DATABASE")
        log(REBUILD_BANNER)

        snapshot = build_snapshot_from_folder(
            self.config,
            log=log,
        )

        # Write the full new snapshot first.
        write_json_snapshot(
            self.config,
            *snapshot,
        )

        # One pointer swap: searches see old or new, never a partial build.
        self.state.replace_all(*snapshot)

        log("[REBUILD] Active snapshot swapped atomically.")

        # Keep SQLite synchronized after an explicit full rebuild.
        with self.store.connection() as connection:
            self.store.sync_from_snapshot(
                self.state.snapshot(),
                connection,
            )

        counts = self.state.counts()

        log("")
        log(
            f"[REBUILD] Complete: "
            f"{counts['documents']} documents, "
            f"{counts['content_terms']} content terms, "
            f"{counts['filenames_indexed']} filenames indexed, "
            f"{counts['page_text_entries']} page-text entries"
        )

        log(REBUILD_BANNER)
        log("")

        return self.status()
