"""
Index state ownership.

`app.py` holds four parallel dictionaries that together form the search
index:

```text
REAL_INVERTED_INDEX   term -> {document: frequency}
DOCUMENT_METADATA     document -> {title, path, total_words, page_count}
FILENAME_INDEX        document -> ordered filename tokens
PAGE_TEXT_INDEX       document -> [{page, text}]
```

They are maintained with copy-on-write: a mutation builds replacement
containers and then swaps them in under a lock, so a reader sees either
the old snapshot or the new one and never a half-built index. This class
gives that pattern a name and a single owner.

Read-only by contract
---------------------
[IndexState.snapshot] returns *references* to the live containers. That
is deliberate, and it matches what the engine already does:

```python
with INDEX_DATA_LOCK:
    active_inverted_index = REAL_INVERTED_INDEX   # a reference, not a copy
```

A deep copy instead would cost about 83 ms at 1000 documents, which
would dominate search latency. Measured with `tools/measure_snapshot_cost.py`.

The contract is therefore:

  * treat anything returned by [IndexState.snapshot] as read-only
  * to change the index, build replacement containers and call
    [IndexState.replace_snapshot]; never mutate in place

Callers that genuinely need isolation (a background rebuild assembling a
new index, a test asserting independence) use
[IndexState.deep_snapshot].

Thread safety
-------------
A single [threading.RLock] guards reads and replacements. The lock is an
introspection aid, not the coherence mechanism: coherence comes from
never mutating a published container, which is what makes the pointer
swap atomic.
"""

import copy
import threading

REQUIRED_SNAPSHOT_FIELDS = (
    "inverted_index",
    "document_metadata",
    "filename_index",
    "page_text_index",
)


class SnapshotIncompleteError(ValueError):
    """Raised when a replacement snapshot is missing a required field."""


class IndexState:
    """Owns the four core search index structures.

    No Flask import, no HTTP, no filesystem, no ranking logic. This is
    the boundary that the ranking and persistence code migrates onto.
    """

    def __init__(
        self,
        inverted_index=None,
        document_metadata=None,
        filename_index=None,
        page_text_index=None,
    ):
        self._lock = threading.RLock()

        self._inverted_index = (
            inverted_index if inverted_index is not None else {}
        )
        self._document_metadata = (
            document_metadata if document_metadata is not None else {}
        )
        self._filename_index = (
            filename_index if filename_index is not None else {}
        )
        self._page_text_index = (
            page_text_index if page_text_index is not None else {}
        )

        self._generation = 0

    # ------------------------------------------------------------------
    # Live views
    #
    # These hand back the containers themselves. See the module
    # docstring: they are read-only by contract, and copying here would
    # put a deep copy on the search hot path.
    # ------------------------------------------------------------------

    @property
    def inverted_index(self):
        with self._lock:
            return self._inverted_index

    @property
    def document_metadata(self):
        with self._lock:
            return self._document_metadata

    @property
    def filename_index(self):
        with self._lock:
            return self._filename_index

    @property
    def page_text_index(self):
        with self._lock:
            return self._page_text_index

    @property
    def generation(self):
        with self._lock:
            return self._generation

    def snapshot(self):
        """
        Return one coherent view of all four containers.

        The four references are read under a single lock acquisition, so
        a caller cannot observe two containers from different
        generations. The containers are **not copied**; treat them as
        read-only.
        """

        with self._lock:
            return {
                "inverted_index": self._inverted_index,
                "document_metadata": self._document_metadata,
                "filename_index": self._filename_index,
                "page_text_index": self._page_text_index,
                "generation": self._generation,
            }

    def deep_snapshot(self):
        """
        Return a fully independent copy of the state.

        Use this when the caller will mutate the result, or when a test
        asserts that later changes to the state cannot be observed.
        Avoid it on the search path: it costs about 83 ms at 1000
        documents.
        """

        with self._lock:
            return {
                "inverted_index": copy.deepcopy(self._inverted_index),
                "document_metadata": copy.deepcopy(self._document_metadata),
                "filename_index": copy.deepcopy(self._filename_index),
                "page_text_index": copy.deepcopy(self._page_text_index),
                "generation": self._generation,
            }

    def replace_snapshot(self, snapshot, *, bump_generation=True):
        """
        Atomically replace all four containers.

        The mapping must supply all four fields. The containers are
        adopted, not copied, so the caller must not retain or mutate
        them afterwards.

        Raises [SnapshotIncompleteError] before touching anything if a
        field is missing, so a rejected replacement leaves the state
        exactly as it was.
        """

        missing = [
            field
            for field in REQUIRED_SNAPSHOT_FIELDS
            if field not in snapshot
        ]

        if missing:
            raise SnapshotIncompleteError(
                f"snapshot is missing required fields: {sorted(missing)}"
            )

        # Read the replacements out before taking the lock, so the
        # critical section stays a pure pointer swap.
        new_inverted = snapshot["inverted_index"]
        new_metadata = snapshot["document_metadata"]
        new_filename = snapshot["filename_index"]
        new_pages = snapshot["page_text_index"]

        with self._lock:
            self._inverted_index = new_inverted
            self._document_metadata = new_metadata
            self._filename_index = new_filename
            self._page_text_index = new_pages

            if bump_generation:
                self._generation += 1

    # ------------------------------------------------------------------
    # Document helpers
    # ------------------------------------------------------------------

    def has_document(self, filename):
        with self._lock:
            return filename in self._document_metadata

    def get_document(self, filename):
        with self._lock:
            return self._document_metadata.get(filename)

    def document_names(self):
        """
        Return the indexed document names, sorted.

        Sorted rather than insertion-ordered so the result is
        deterministic regardless of how the index was built, which
        matters for anything that diffs two corpora.
        """

        with self._lock:
            return sorted(self._document_metadata.keys())

    def document_count(self):
        with self._lock:
            return len(self._document_metadata)

    def content_term_count(self):
        with self._lock:
            return len(self._inverted_index)

    def counts(self):
        """Return the four corpus counters as a dict."""

        with self._lock:
            return {
                "documents": len(self._document_metadata),
                "content_terms": len(self._inverted_index),
                "filenames_indexed": len(self._filename_index),
                "page_text_entries": len(self._page_text_index),
            }

    def __repr__(self):
        counts = self.counts()

        return (
            f"IndexState(generation={self.generation}, "
            f"documents={counts['documents']}, "
            f"content_terms={counts['content_terms']})"
        )
