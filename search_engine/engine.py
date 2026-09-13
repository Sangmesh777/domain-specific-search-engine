"""
SearchEngine: the single authoritative engine API.

This is the object every adapter talks to. The Flask HTTP API, the offline
Android local backend and the tests all drive the same class, which is what
keeps "one core implementation, multiple adapters" true instead of
aspirational.

The engine exposes business operations, not implementation details:

    engine.load()                  startup: legacy JSON, then SQLite
    engine.index_document(...)     index/replace one document from a file
    engine.index_extracted(...)    index one document from text extracted
                                   elsewhere (the offline backend's path)
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
import shutil
import time

from search_engine.config import (
    EngineConfig,
    default_data_folder,
)
from search_engine.extraction import (
    extract_pages,
    extract_text,
)
from search_engine.filenames import (
    normalize_requested_filenames,
    sanitize_upload_filename,
)
from search_engine.index import (
    IndexState,
)
from search_engine.rebuild import (
    REBUILD_BANNER,
    build_snapshot_from_folder,
)
from search_engine.results import (
    DELETE_DELETED,
    DELETE_FAILED,
    DELETE_FILE_NOT_FOUND,
    DELETE_INVALID_PATH,
    DELETE_NOT_INDEXED,
    BulkDeleteResult,
    DeleteResult,
    ImportResult,
)
from search_engine.search import (
    search_documents,
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


# Werkzeug writes an upload with shutil.copyfileobj at 16 KiB. The engine uses
# the same copy and the same buffer size, so stored bytes are identical whether
# the stream arrived in an HTTP multipart body or from an Android content
# resolver.
_STREAM_BUFFER_SIZE = 16384


def _write_stream_to_path(stream, path):
    """
    Copy a binary stream to a path.

    The caller owns the stream, so it is not closed here: the Flask adapter
    passes FileStorage.stream and closes it with the request, while
    import_paths opens and closes its own handles.
    """

    with open(path, "wb") as destination:

        shutil.copyfileobj(
            stream,
            destination,
            _STREAM_BUFFER_SIZE,
        )


def _discard_path(path):
    """
    Remove a scratch file if it is there, ignoring a missing one.

    Used for the temporary, backup and rejected-upload files an import
    leaves behind. Cleanup must never mask the real error, so an OSError
    is swallowed: a leftover scratch file is a wart, while losing the
    original exception would hide why the upload failed.
    """

    try:
        os.remove(path)
    except OSError:
        pass


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
    # SEARCH
    # --------------------------------------------------------

    def search(
        self,
        raw_query,
        requested_page=1,
        requested_limit=10,
        snapshot=None,
    ):
        """
        Rank the corpus for one query.

        This is the single search implementation in the system. The Flask
        route and an offline Android search screen both call it and get the
        same payload for the same corpus, which is the whole point of the
        split.

        `raw_query` is the user's text, untrimmed. Splitting it into keywords
        and a filetype filter is domain logic, so it happens here rather than
        in an adapter.

        `requested_page` and `requested_limit` are already integers; turning
        query strings into integers is HTTP parsing and stays in the adapter.
        Clamping (page >= 1, 1 <= limit <= 50) is policy and happens here.

        `snapshot` lets a caller reuse one coherent read view across several
        queries; by default a fresh one is taken.

        Returns a JSON-ready payload. Normally that is the paginated object;
        three legacy branches answer with a bare list instead. Both shapes
        are part of the published API and are preserved deliberately, so
        adapters serialize whatever comes back and add nothing.
        """

        if snapshot is None:
            snapshot = self.state.snapshot()

        return search_documents(
            snapshot,
            raw_query,
            requested_page,
            requested_limit,
        )

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

    def get_document(self, filename):
        """
        Everything an adapter needs to open one document, in a single call.

        Returns None when the document is not indexed. Otherwise:

            filename    the indexed name
            metadata    title, path, total_words, page_count (a copy)
            path        resolved absolute path, guaranteed inside the data
                        folder, or None when the name escapes it
            exists      whether that path is really on disk

        Metadata and path resolution already existed separately, and the web
        adapter combines them itself. An offline client has to do the same
        thing to open a local file, and would otherwise have to reimplement
        the containment check - which is the one piece of this that is a
        security boundary rather than a convenience.
        """

        metadata = self.state.get_document_metadata(filename)

        if metadata is None:
            return None

        path = self.resolve_document_path(filename)

        return {
            "filename": filename,
            "metadata": metadata,
            "path": path,
            "exists": (
                path is not None
                and os.path.isfile(path)
            ),
        }

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

        This owns extraction and nothing else: it reads the file, then hands
        the text to index_extracted, which is where tokenizing, scoring and
        persistence happen. Keeping the split there means a caller that
        already has the text - an offline backend indexing a pre-extracted
        shipped corpus - goes through the identical path instead of a second
        one.

        Returns the document metadata.
        """

        text = extract_text(
            file_path,
            filename,
        )

        pages = extract_pages(
            file_path,
            filename,
        )

        return self.index_extracted(
            filename,
            text,
            pages,
            source_path=file_path,
            connection=connection,
            commit=commit,
        )

    def index_extracted(
        self,
        filename,
        text,
        pages,
        source_path=None,
        connection=None,
        commit=True,
    ):
        """
        Index one document from text that was extracted somewhere else.

        This is the entry point the offline Android backend indexes its
        shipped corpus through. Document extraction is the one part of the
        engine that does not port: PyPDF2 and python-docx have no Android
        equivalent producing identical output, so a device that parsed the
        PDFs itself would rank them differently from the server. Extracting
        once, here, and shipping the text removes that divergence instead of
        chasing it. See ANDROID.md sections 7 and 20.

        It is also how the golden vectors are consumed: they record extracted
        page text, so a port can validate ranking without a parser.

        `text` is the full lowercased document text that term frequencies are
        counted from - exactly what extract_text returns. `pages` is the list
        of {"page": n, "text": ...} views that snippets and page numbers are
        cut from - exactly what extract_pages returns. Passing anything else
        produces an index that disagrees with one built from the file.

        `source_path` is the absolute path recorded in the document metadata,
        which is what an adapter resolves to open the file later. It defaults
        to the document's location in the data folder, because on a device the
        text arrives from a bundled asset while the document itself has
        already been copied into app-private storage.

        Everything after extraction is shared with index_document, so there is
        one tokenizing, scoring and persistence path rather than two.
        """

        content_words = tokenize(text)

        term_counts = {}

        for word in content_words:
            term_counts[word] = (
                term_counts.get(word, 0)
                + 1
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

        if source_path is None:
            source_path = self.config.document_path(
                filename
            )

        metadata = {
            "title": filename,
            "path": os.path.abspath(source_path),
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
    # IMPORT
    # --------------------------------------------------------

    def import_documents(
        self,
        items,
        log=print,
    ):
        """
        Import a batch of documents in ONE SQLite transaction.

        `items` is an iterable of (original_filename, binary stream) pairs.
        A stream is anything with `read()`, so the Flask adapter hands over
        `FileStorage.stream` and an offline Android backend hands over a
        ContentResolver stream; neither knows about the other, and neither
        needs a network.

        Per document:

        1. sanitize the name (rejecting unsupported/unsafe names)
        2. write the bytes to `<name>.uploading`, then atomically rename
        3. index it into memory and into the shared transaction

        Step 2 first steps any existing document of the same name aside to
        `<name>.replacing`. That is what makes a replacement safe: if step 3
        fails, the document that was there is put back and the rejected bytes
        are discarded, so the index and the disk still describe the same file.
        Without it a failed re-upload overwrote a good document, the stale
        index kept serving it, and the next rebuild silently dropped it.

        A document that cannot be extracted or indexed is recorded as a
        failure and leaves nothing behind on disk, but the rest of the batch
        still commits: one bad upload must not discard nine good ones. The
        transaction is rolled back only if the batch itself fails.

        Returns an ImportResult.
        """

        result = ImportResult()

        connection = self.store.connection()

        try:
            for original_filename, stream in items:

                original_filename = (
                    original_filename or ""
                ).strip()

                safe_filename = sanitize_upload_filename(
                    original_filename
                )

                if not safe_filename:

                    result.rejected.append({
                        "filename": original_filename,
                        "reason": (
                            "Unsupported or invalid filename. "
                            "Allowed: PDF, DOCX, TXT."
                        ),
                    })

                    continue

                file_path = self.config.document_path(
                    safe_filename
                )

                existed_before = os.path.isfile(
                    file_path
                )

                temp_path = file_path + ".uploading"
                backup_path = file_path + ".replacing"

                try:

                    _write_stream_to_path(
                        stream,
                        temp_path,
                    )

                    # Step the existing document aside before overwriting
                    # it, so a replacement that fails extraction can be
                    # rolled back instead of destroying the good copy.
                    # A rename, not a copy: no second read of the bytes.
                    if existed_before:
                        os.replace(
                            file_path,
                            backup_path,
                        )

                    os.replace(
                        temp_path,
                        file_path,
                    )

                    self.index_document(
                        safe_filename,
                        file_path,
                        connection=connection,
                        commit=False,
                    )

                    # The new document is indexed, so the copy that was
                    # stepped aside is no longer needed.
                    _discard_path(backup_path)

                    result.uploaded.append(
                        safe_filename
                    )

                    if existed_before:

                        result.replaced.append(
                            safe_filename
                        )

                        log(
                            "[UPLOAD] Replaced and "
                            "incrementally indexed: "
                            f"{safe_filename}"
                        )

                    else:

                        result.created.append(
                            safe_filename
                        )

                        log(
                            "[UPLOAD] Created and "
                            "incrementally indexed: "
                            f"{safe_filename}"
                        )

                except Exception as error:

                    _discard_path(temp_path)

                    if os.path.exists(backup_path):
                        # A replacement failed: put the document that was
                        # there back, so the index and the disk still agree.
                        try:
                            os.replace(
                                backup_path,
                                file_path,
                            )
                        except OSError:
                            _discard_path(backup_path)
                    else:
                        # A brand-new document failed: leave no orphan
                        # behind that nothing indexes and nothing serves.
                        _discard_path(file_path)

                    result.failed.append({
                        "filename": safe_filename,
                        "reason":
                            f"Could not index file: {error}",
                    })

            connection.commit()

        except Exception:

            connection.rollback()
            raise

        finally:

            connection.close()

        self.mark_ready()

        return result

    def import_paths(
        self,
        paths,
        log=print,
    ):
        """
        Import documents that already exist as files on disk.

        Convenience for callers that have paths rather than streams: an
        Android import flow that has already copied through the content
        resolver, or a bulk migration script pointed at a folder. All files
        are opened up front and closed by this method, and the whole batch
        still shares one transaction.
        """

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

            return self.import_documents(
                items,
                log=log,
            )

        finally:

            for handle in handles:
                handle.close()

    # --------------------------------------------------------
    # DELETE
    # --------------------------------------------------------

    def delete(self, filename, log=print):
        """
        Delete one document: file, memory index and SQLite rows.

        Returns a DeleteResult whose outcome is one of:

            deleted          file removed and document unindexed
            invalid_path     name would escape the data folder
            not_indexed      no such document in the index
            file_not_found   indexed, but the file is gone
            failed           reason explains the fault

        Expected outcomes are values, not exceptions, so an adapter can map
        them onto HTTP status codes or an Android dialog without catching
        control flow.
        """

        safe_path = self.resolve_document_path(filename)

        if safe_path is None:
            return DeleteResult(
                DELETE_INVALID_PATH,
                filename=filename,
            )

        safe_filename = os.path.basename(safe_path)

        if not self.state.has_document(safe_filename):
            return DeleteResult(
                DELETE_NOT_INDEXED,
                filename=safe_filename,
            )

        if not os.path.isfile(safe_path):
            return DeleteResult(
                DELETE_FILE_NOT_FOUND,
                filename=safe_filename,
            )

        try:

            os.remove(safe_path)

            self.remove_document(safe_filename)

        except Exception as error:

            return DeleteResult(
                DELETE_FAILED,
                filename=safe_filename,
                reason=str(error),
            )

        self.mark_ready()

        log(
            "[DELETE] Removed and "
            "incrementally unindexed: "
            f"{safe_filename}"
        )

        return DeleteResult(
            DELETE_DELETED,
            filename=safe_filename,
        )

    def bulk_delete(
        self,
        filenames,
        log=print,
    ):
        """
        Delete many documents inside ONE SQLite transaction.

        This deliberately does NOT loop over `delete()`: that would open a
        connection and commit per document, losing the all-or-nothing
        property the API promises. Instead one connection is opened, every
        document is unindexed with `commit=False`, and the batch is committed
        once at the end (or rolled back if the batch itself fails).

        Accounting is per document:

            deleted     unindexed and, when present, removed from disk
            not_found   neither indexed nor on disk
            failed      raised; reason recorded, batch continues

        A document that is indexed but whose file is missing is still
        unindexed and counted as deleted, which is how a half-removed
        document is repaired.

        Ordering is what makes the rollback honest. Memory and the
        filesystem cannot participate in a SQLite transaction, so touching
        them before the commit means a rollback leaves all three stores
        disagreeing - and worse, it deletes files that the index still
        claims exist. The batch therefore runs in two phases:

            phase 1  plan and delete SQLite rows, inside one transaction
            commit   the point of no return; on failure roll back and
                     re-raise, having changed nothing anywhere
            phase 2  drop the in-memory entries and unlink the files

        A rollback now leaves the corpus exactly as it was. The residual
        risk moves to phase 2: if unlinking a file fails after the commit,
        that document is genuinely unindexed and the file is left behind as
        an orphan, reported in `failed` with the reason. An orphan is
        recoverable - a rebuild re-indexes it - whereas a deleted file
        belonging to a rolled-back index entry is not.
        """

        requested = normalize_requested_filenames(
            filenames
        )

        result = BulkDeleteResult(requested)

        connection = self.store.connection()

        # filename, resolved path, terms it contributed, and whether a file
        # is actually on disk to unlink.
        planned = []

        try:

            for filename in requested:

                file_path = self.config.document_path(
                    filename
                )

                try:

                    indexed_exists = (
                        self.state.has_document(filename)
                    )

                    filesystem_exists = os.path.isfile(
                        file_path
                    )

                    if (
                        not indexed_exists
                        and not filesystem_exists
                    ):
                        result.not_found.append(filename)
                        continue

                    # Read before deleting: the postings are what phase 2
                    # needs to unwind the in-memory term counts.
                    old_term_counts = (
                        self.store.document_term_counts(
                            filename
                        )
                    )

                    self.store.delete_document(
                        connection,
                        filename,
                    )

                    planned.append((
                        filename,
                        file_path,
                        old_term_counts,
                        filesystem_exists,
                    ))

                except Exception as error:

                    result.failed.append({
                        "filename": filename,
                        "reason": str(error),
                    })

            connection.commit()

        except Exception:

            connection.rollback()
            raise

        finally:

            connection.close()

        for (
            filename,
            file_path,
            old_term_counts,
            filesystem_exists,
        ) in planned:

            try:

                self.state.remove_document(
                    filename,
                    old_term_counts,
                )

                if filesystem_exists:
                    os.remove(file_path)

                result.deleted.append(filename)

                log(
                    "[BULK DELETE] Removed and "
                    "incrementally unindexed: "
                    f"{filename}"
                )

            except Exception as error:

                result.failed.append({
                    "filename": filename,
                    "reason": (
                        "Unindexed, but the stored file "
                        f"could not be removed: {error}"
                    ),
                })

        self.mark_ready()

        return result

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
