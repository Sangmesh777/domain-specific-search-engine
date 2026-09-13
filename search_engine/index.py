"""
In-memory index state.

Four structures form the searchable state:

    inverted_index      term -> {filename: term_count}
    document_metadata   filename -> {title, path, total_words, page_count}
    filename_index      filename -> [filename tokens]
    page_text_index     filename -> [{"page": n, "text": ...}]

Concurrency model (preserved exactly from the original implementation):

* A search captures the four dictionary *references* under the lock, then
  works on them without holding the lock. `snapshot()` therefore returns
  live references, not copies: copying a large corpus per request would be
  a severe performance regression and would change behavior.

* Posting lists are copy-on-write. Adding or removing a document replaces
  the affected `term -> {...}` posting dictionary with a new object rather
  than mutating it, so a search already iterating a posting list keeps a
  stable object.

* A full rebuild builds four brand-new dictionaries off to the side and
  swaps all four references in one locked step, so a search sees either the
  complete old snapshot or the complete new one, never a partial build.

The lock is a plain `threading.Lock`, not a Flask construct: any adapter
that serves concurrent requests (the Flask API, or an Android background
import) needs the same guarantee.
"""

import threading


class IndexSnapshot:
    """
    One coherent view of the index.

    Holds references to the live structures. Treat as read-only.
    """

    __slots__ = (
        "inverted_index",
        "document_metadata",
        "filename_index",
        "page_text_index",
    )

    def __init__(
        self,
        inverted_index,
        document_metadata,
        filename_index,
        page_text_index,
    ):
        self.inverted_index = inverted_index
        self.document_metadata = document_metadata
        self.filename_index = filename_index
        self.page_text_index = page_text_index

    def __repr__(self):
        return (
            "IndexSnapshot("
            f"documents={len(self.document_metadata)}, "
            f"terms={len(self.inverted_index)}, "
            f"filenames={len(self.filename_index)}, "
            f"pages={len(self.page_text_index)})"
        )


class IndexState:
    """Owns the searchable in-memory structures."""

    def __init__(self):

        self._lock = threading.Lock()

        self._inverted_index = {}
        self._document_metadata = {}
        self._filename_index = {}
        self._page_text_index = {}

    # --------------------------------------------------------
    # READ
    # --------------------------------------------------------

    def snapshot(self):
        """
        Capture one coherent active snapshot.

        Returns live references; callers must treat them as read-only.
        """

        with self._lock:

            return IndexSnapshot(
                self._inverted_index,
                self._document_metadata,
                self._filename_index,
                self._page_text_index,
            )

    def counts(self):
        """Sizes of the four structures, read consistently."""

        with self._lock:

            return {
                "documents": len(self._document_metadata),
                "content_terms": len(self._inverted_index),
                "filenames_indexed": len(self._filename_index),
                "page_text_entries": len(self._page_text_index),
            }

    def has_document(self, filename):
        """True when the document is present in the active index."""

        with self._lock:

            return filename in self._document_metadata

    def get_document_metadata(self, filename):
        """Metadata for one document, or None."""

        with self._lock:

            metadata = self._document_metadata.get(
                filename
            )

            return dict(metadata) if metadata else None

    # --------------------------------------------------------
    # WRITE
    # --------------------------------------------------------

    def replace_all(
        self,
        inverted_index,
        document_metadata,
        filename_index,
        page_text_index,
    ):
        """
        Swap all four structures in one step.

        Used by a full rebuild: searches see the old snapshot or the new
        one, never a partial build.
        """

        with self._lock:

            self._inverted_index = inverted_index
            self._document_metadata = document_metadata
            self._filename_index = filename_index
            self._page_text_index = page_text_index

    def add_document(
        self,
        filename,
        metadata,
        filename_words,
        pages,
        term_counts,
    ):
        """
        Add or replace one document in the active structures.

        `term_counts` is {term: count} for the document's content.
        """

        with self._lock:

            self._add_unlocked(
                filename,
                metadata,
                filename_words,
                pages,
                term_counts,
            )

    def remove_document(
        self,
        filename,
        old_term_counts,
    ):
        """
        Remove one document without rebuilding the corpus.

        `old_term_counts` lists the terms this document contributed, so
        only those posting lists are touched.
        """

        with self._lock:

            self._remove_unlocked(
                filename,
                old_term_counts,
            )

    def replace_document(
        self,
        filename,
        old_term_counts,
        metadata,
        filename_words,
        pages,
        term_counts,
    ):
        """
        Remove and re-add one document as a single atomic step.

        Re-indexing an existing document must not be observable as
        "document missing" by a concurrent search, so removal and insertion
        happen under one lock acquisition, exactly as the original
        implementation did.
        """

        with self._lock:

            self._remove_unlocked(
                filename,
                old_term_counts,
            )

            self._add_unlocked(
                filename,
                metadata,
                filename_words,
                pages,
                term_counts,
            )

    def _add_unlocked(
        self,
        filename,
        metadata,
        filename_words,
        pages,
        term_counts,
    ):
        """Insert one document. Caller must hold the lock."""

        self._document_metadata[filename] = metadata

        # The filename index is document-local.
        self._filename_index[filename] = (
            filename_words
        )

        self._page_text_index[filename] = pages

        # Each changed posting list gets a new dictionary object, so a
        # concurrent search holding the old posting list stays stable.
        for term, count in term_counts.items():

            old_postings = self._inverted_index.get(
                term,
                {},
            )

            new_postings = dict(old_postings)

            new_postings[filename] = count

            self._inverted_index[term] = new_postings

    def _remove_unlocked(
        self,
        filename,
        old_term_counts,
    ):
        """Remove one document. Caller must hold the lock."""

        for term in old_term_counts:

            posting_list = self._inverted_index.get(
                term
            )

            if not posting_list:
                continue

            new_posting_list = dict(posting_list)

            new_posting_list.pop(
                filename,
                None,
            )

            if new_posting_list:
                self._inverted_index[term] = (
                    new_posting_list
                )
            else:
                self._inverted_index.pop(
                    term,
                    None,
                )

        self._document_metadata.pop(
            filename,
            None,
        )

        self._filename_index.pop(
            filename,
            None,
        )

        self._page_text_index.pop(
            filename,
            None,
        )

    def document_filenames(self):
        """Every indexed filename, read consistently."""

        with self._lock:

            return list(self._document_metadata.keys())
