"""
Engine configuration.

The engine owns its infrastructure configuration. An adapter configures an
engine once:

    engine = SearchEngine(EngineConfig(data_folder=...))

and then calls business operations without re-passing paths:

    engine.search(...)
    engine.index_document(...)
    engine.delete(...)
    engine.bulk_delete(...)
    engine.rebuild(...)

This matters for the dual-mode product: the Flask adapter points the engine
at a server data folder, while the Android local backend points the very
same engine class at application-private storage. Neither caller threads
database paths through individual operations.
"""

import os


# Document types the engine can extract, index and serve.
#
# Order is significant: query normalization stops at the first supported
# extension it finds at the end of a query.
SUPPORTED_EXTENSIONS = (
    ".pdf",
    ".docx",
    ".txt",
)


# Filetype filter tokens accepted in a search query, mapped to the
# canonical filter value.
FILETYPE_FILTER_TOKENS = {
    "pdf": "pdf",
    ".pdf": "pdf",
    "docx": "docx",
    ".docx": "docx",
    "txt": "txt",
    ".txt": "txt",
}


# Extension -> canonical filetype, in match order.
FILETYPE_EXTENSIONS = (
    (".pdf", "pdf"),
    (".docx", "docx"),
    (".txt", "txt"),
)


def default_data_folder():
    """Repository-local data folder used by the Flask adapter."""

    base_dir = os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )

    return os.path.join(base_dir, "data")


class EngineConfig:
    """
    Where an engine keeps its documents, index and database.

    Only data_folder is required; the remaining locations default to the
    established filenames inside it, so the Flask adapter, the tests and an
    Android local backend all get the same layout:

        <data_folder>/search.db                SQLite index (source of truth)
        <data_folder>/inverted_index.json      legacy JSON snapshot
        <data_folder>/document_meta.json
        <data_folder>/filename_index.json
        <data_folder>/page_text.json

    The JSON files predate SQLite. They are still written by a full rebuild
    and still read at startup, because an existing deployment may hold a
    corpus that has never been migrated. SQLite remains the authoritative
    incremental store.
    """

    def __init__(
        self,
        data_folder,
        sqlite_db_file=None,
        index_file=None,
        meta_file=None,
        filename_index_file=None,
        page_text_file=None,
        supported_extensions=SUPPORTED_EXTENSIONS,
    ):

        self.data_folder = os.path.abspath(
            data_folder
        )

        self.sqlite_db_file = (
            sqlite_db_file
            or os.path.join(
                self.data_folder,
                "search.db",
            )
        )

        self.index_file = (
            index_file
            or os.path.join(
                self.data_folder,
                "inverted_index.json",
            )
        )

        self.meta_file = (
            meta_file
            or os.path.join(
                self.data_folder,
                "document_meta.json",
            )
        )

        self.filename_index_file = (
            filename_index_file
            or os.path.join(
                self.data_folder,
                "filename_index.json",
            )
        )

        self.page_text_file = (
            page_text_file
            or os.path.join(
                self.data_folder,
                "page_text.json",
            )
        )

        self.supported_extensions = tuple(
            supported_extensions
        )

    def ensure_data_folder(self):
        """Create the data folder if it does not exist yet."""

        os.makedirs(
            self.data_folder,
            exist_ok=True,
        )

        return self.data_folder

    def document_path(self, filename):
        """Absolute path of a document stored in the data folder."""

        return os.path.join(
            self.data_folder,
            filename,
        )

    def is_supported_document(self, filename):
        """True when the engine can extract text from this filename."""

        return filename.lower().endswith(
            self.supported_extensions
        )

    def __repr__(self):
        return (
            f"EngineConfig(data_folder={self.data_folder!r}, "
            f"sqlite_db_file={self.sqlite_db_file!r})"
        )
