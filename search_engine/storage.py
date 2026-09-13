"""
Persistence.

SQLite is the authoritative incremental store:

    documents        filename, title, path, total_words, page_count
    term_postings    term, filename, term_count
    filename_terms   filename, term
    pages            filename, page_number, text

Four legacy JSON files predate SQLite. They are still written by a full
rebuild and still read at startup, because an existing deployment may hold a
corpus that has never been migrated. `SqliteStore.initialize()` performs that
one-time migration when it finds an empty database next to a non-empty
in-memory snapshot.

Connection/transaction policy is deliberately explicit, because it is the
behavior that makes bulk operations atomic:

* Callers that need several mutations in ONE transaction pass a `connection`
  and `commit=False`, then commit or roll back themselves.
* Callers that pass no connection get a private connection that is committed
  and closed here.

Nothing in this module imports Flask or starts a thread.
"""

import json
import os
import sqlite3


SQLITE_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS documents (
    filename TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    path TEXT NOT NULL,
    total_words INTEGER NOT NULL,
    page_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS term_postings (
    term TEXT NOT NULL,
    filename TEXT NOT NULL,
    term_count INTEGER NOT NULL,
    PRIMARY KEY (term, filename)
);

CREATE INDEX IF NOT EXISTS idx_term_postings_term
ON term_postings(term);

CREATE INDEX IF NOT EXISTS idx_term_postings_filename
ON term_postings(filename);

CREATE TABLE IF NOT EXISTS filename_terms (
    filename TEXT NOT NULL,
    term TEXT NOT NULL,
    PRIMARY KEY (filename, term)
);

CREATE INDEX IF NOT EXISTS idx_filename_terms_term
ON filename_terms(term);

CREATE TABLE IF NOT EXISTS pages (
    filename TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (filename, page_number)
);
"""


def atomic_write_json(path, data):
    """
    Write JSON so a crash cannot leave a half-written index behind.

    Writes a sibling temporary file, flushes and fsyncs it, then renames it
    over the target. The rename is atomic on POSIX and on Windows.
    """

    temp_path = path + ".tmp"

    with open(
        temp_path,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(data, file, indent=2)
        file.flush()
        os.fsync(file.fileno())

    os.replace(temp_path, path)


def read_json_snapshot(config):
    """
    Read the four legacy JSON files.

    Each file is independent: a missing file leaves that structure empty and
    an unreadable one is reported and treated as empty, exactly as the
    original loader did.
    """

    sources = (
        (
            config.index_file,
            "content index",
        ),
        (
            config.meta_file,
            "metadata",
        ),
        (
            config.filename_index_file,
            "filename index",
        ),
        (
            config.page_text_file,
            "page text index",
        ),
    )

    loaded = []

    for path, label in sources:

        data = {}

        if os.path.exists(path):

            try:

                with open(
                    path,
                    "r",
                    encoding="utf-8"
                ) as file:
                    data = json.load(file)

            except Exception as error:

                print(
                    f"[DATABASE ERROR] Could not load "
                    f"{label}: {error}"
                )

                data = {}

        loaded.append(data)

    return tuple(loaded)


def write_json_snapshot(
    config,
    inverted_index,
    document_metadata,
    filename_index,
    page_text_index,
):
    """Persist the four legacy JSON snapshots atomically."""

    atomic_write_json(
        config.index_file,
        inverted_index,
    )

    atomic_write_json(
        config.meta_file,
        document_metadata,
    )

    atomic_write_json(
        config.filename_index_file,
        filename_index,
    )

    atomic_write_json(
        config.page_text_file,
        page_text_index,
    )


class SqliteStore:
    """SQLite persistence for one engine configuration."""

    def __init__(self, config):

        self.config = config

    # --------------------------------------------------------
    # CONNECTIONS
    # --------------------------------------------------------

    def connection(self):
        """
        Open a connection to the engine's database.

        WAL mode (set by the schema) lets readers proceed while a writer
        holds a transaction, which is what keeps search responsive during a
        bulk upload.
        """

        connection = sqlite3.connect(
            self.config.sqlite_db_file,
            timeout=30,
        )

        connection.execute(
            "PRAGMA foreign_keys = ON"
        )

        return connection

    # --------------------------------------------------------
    # SCHEMA / MIGRATION
    # --------------------------------------------------------

    def initialize(self, snapshot):
        """
        Create the schema and perform the one-time JSON -> SQLite migration.

        `snapshot` is the in-memory state loaded from the legacy JSON files.
        When the database holds no documents but memory does, memory is
        written out once. Later document changes are incremental.

        Returns the persisted snapshot, or None when the database is empty
        (in which case the caller keeps whatever it already loaded).
        """

        self.config.ensure_data_folder()

        with self.connection() as connection:

            connection.executescript(
                SQLITE_SCHEMA
            )

            count = connection.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()[0]

            if (
                count == 0
                and snapshot.document_metadata
            ):
                self.sync_from_snapshot(
                    snapshot,
                    connection,
                )

        return self.load_snapshot()

    def sync_from_snapshot(
        self,
        snapshot,
        connection=None,
    ):
        """
        Full synchronization used only for migration or an explicit rebuild.

        Normal uploads and deletes never call this: they are incremental.
        """

        close_connection = False

        if connection is None:
            connection = self.connection()
            close_connection = True

        try:
            connection.execute(
                "DELETE FROM term_postings"
            )

            connection.execute(
                "DELETE FROM filename_terms"
            )

            connection.execute(
                "DELETE FROM pages"
            )

            connection.execute(
                "DELETE FROM documents"
            )

            for (
                filename,
                metadata,
            ) in snapshot.document_metadata.items():

                connection.execute(
                    """
                    INSERT INTO documents
                    (filename, title, path, total_words, page_count)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        filename,
                        metadata["title"],
                        metadata["path"],
                        metadata["total_words"],
                        metadata["page_count"],
                    ),
                )

            for (
                term,
                posting_list,
            ) in snapshot.inverted_index.items():

                connection.executemany(
                    """
                    INSERT INTO term_postings
                    (term, filename, term_count)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (
                            term,
                            filename,
                            count,
                        )
                        for filename, count
                        in posting_list.items()
                    ],
                )

            for (
                filename,
                filename_words,
            ) in snapshot.filename_index.items():

                # Store unique terms only.
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO filename_terms
                    (filename, term)
                    VALUES (?, ?)
                    """,
                    [
                        (
                            filename,
                            term,
                        )
                        for term in set(filename_words)
                    ],
                )

            for (
                filename,
                pages,
            ) in snapshot.page_text_index.items():

                connection.executemany(
                    """
                    INSERT INTO pages
                    (filename, page_number, text)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (
                            filename,
                            page_data["page"],
                            page_data["text"],
                        )
                        for page_data in pages
                    ],
                )

            connection.commit()

        finally:

            if close_connection:
                connection.close()

    def load_snapshot(self):
        """
        Read the whole persisted index.

        Returns None when the database holds no documents, so the caller can
        keep an already-loaded legacy JSON snapshot instead of wiping it.
        """

        with self.connection() as connection:

            document_rows = connection.execute(
                """
                SELECT
                    filename,
                    title,
                    path,
                    total_words,
                    page_count
                FROM documents
                """
            ).fetchall()

            if not document_rows:
                return None

            document_metadata = {}

            for (
                filename,
                title,
                path,
                total_words,
                page_count,
            ) in document_rows:

                document_metadata[filename] = {
                    "title": title,
                    "path": path,
                    "total_words": total_words,
                    "page_count": page_count,
                }

            inverted_index = {}

            posting_rows = connection.execute(
                """
                SELECT
                    term,
                    filename,
                    term_count
                FROM term_postings
                """
            ).fetchall()

            for (
                term,
                filename,
                term_count,
            ) in posting_rows:

                if term not in inverted_index:
                    inverted_index[term] = {}

                inverted_index[term][filename] = term_count

            filename_index = {}

            filename_rows = connection.execute(
                """
                SELECT
                    filename,
                    term
                FROM filename_terms
                ORDER BY filename, rowid
                """
            ).fetchall()

            for filename, term in filename_rows:

                filename_index.setdefault(
                    filename,
                    [],
                ).append(term)

            page_text_index = {}

            page_rows = connection.execute(
                """
                SELECT
                    filename,
                    page_number,
                    text
                FROM pages
                ORDER BY filename, page_number
                """
            ).fetchall()

            for (
                filename,
                page_number,
                text,
            ) in page_rows:

                page_text_index.setdefault(
                    filename,
                    [],
                ).append({
                    "page": page_number,
                    "text": text,
                })

        return (
            inverted_index,
            document_metadata,
            filename_index,
            page_text_index,
        )

    # --------------------------------------------------------
    # PER-DOCUMENT OPERATIONS
    # --------------------------------------------------------

    def document_term_counts(self, filename):
        """Terms contributed by one document, as {term: count}."""

        with self.connection() as connection:

            rows = connection.execute(
                """
                SELECT term, term_count
                FROM term_postings
                WHERE filename = ?
                """,
                (filename,),
            ).fetchall()

        return dict(rows)

    def insert_document(
        self,
        connection,
        filename,
        metadata,
        term_counts,
        filename_words,
        pages,
    ):
        """
        Write one document's rows, replacing anything already stored for it.

        Does not commit: the caller owns the transaction boundary.
        """

        connection.execute(
            "DELETE FROM term_postings WHERE filename = ?",
            (filename,),
        )

        connection.execute(
            "DELETE FROM filename_terms WHERE filename = ?",
            (filename,),
        )

        connection.execute(
            "DELETE FROM pages WHERE filename = ?",
            (filename,),
        )

        connection.execute(
            "DELETE FROM documents WHERE filename = ?",
            (filename,),
        )

        connection.execute(
            """
            INSERT INTO documents
            (filename, title, path, total_words, page_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                filename,
                metadata["title"],
                metadata["path"],
                metadata["total_words"],
                metadata["page_count"],
            ),
        )

        connection.executemany(
            """
            INSERT INTO term_postings
            (term, filename, term_count)
            VALUES (?, ?, ?)
            """,
            [
                (
                    term,
                    filename,
                    count,
                )
                for term, count in term_counts.items()
            ],
        )

        connection.executemany(
            """
            INSERT OR IGNORE INTO filename_terms
            (filename, term)
            VALUES (?, ?)
            """,
            [
                (
                    filename,
                    term,
                )
                for term in set(filename_words)
            ],
        )

        connection.executemany(
            """
            INSERT INTO pages
            (filename, page_number, text)
            VALUES (?, ?, ?)
            """,
            [
                (
                    filename,
                    page_data["page"],
                    page_data["text"],
                )
                for page_data in pages
            ],
        )

    def delete_document(
        self,
        connection,
        filename,
    ):
        """
        Remove every row belonging to one document.

        Does not commit: the caller owns the transaction boundary.
        """

        connection.execute(
            "DELETE FROM term_postings WHERE filename = ?",
            (filename,),
        )

        connection.execute(
            "DELETE FROM filename_terms WHERE filename = ?",
            (filename,),
        )

        connection.execute(
            "DELETE FROM pages WHERE filename = ?",
            (filename,),
        )

        connection.execute(
            "DELETE FROM documents WHERE filename = ?",
            (filename,),
        )

    # --------------------------------------------------------
    # INTROSPECTION (used by tests and parity checks)
    # --------------------------------------------------------

    def table_counts(self):
        """Row counts per table, for consistency assertions."""

        with self.connection() as connection:

            return {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "documents",
                    "term_postings",
                    "filename_terms",
                    "pages",
                )
            }
