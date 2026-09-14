"""
SQLite and JSON persistence.

Extracted verbatim from app.py by tools/extract_modules.py.
Do not retype these functions: the bodies must stay byte
identical to the engine that the golden vectors pin.

This module is deliberately free of Flask, of module-level index
globals, and of any knowledge of how published state is swapped. It
reads and writes files and rows; the caller decides what to do with the
result.

The split matters for the offline mode. The Android backend needs the
same schema and the same reconstruction rules, and it can only reuse
them if none of this reaches back into the web application.

Two invariants are load-bearing and easy to break by accident:

* `filename_terms` is read back with `ORDER BY filename, rowid`, so the
  order of a document's filename tokens is whatever insertion order
  produced. It is not sorted, and it is not stable across a delete and
  re-add. That is the existing behaviour, and the parity tool compares
  against it rather than against an idealised version.
* `save_database_snapshot` writes four files in sequence, each
  individually atomic. It is not atomic across all four, and never was.

`load_database_from_sqlite` and `load_json_snapshot` return a complete
snapshot and install nothing. `sync_sqlite_from_memory` takes the four
containers rather than reading them. Publication belongs to the
application, which does it under one lock.
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


def get_sqlite_connection(db_path):
    connection = sqlite3.connect(
        db_path,
        timeout=30,
    )

    connection.execute(
        "PRAGMA foreign_keys = ON"
    )

    return connection


def sync_sqlite_from_memory(
    *,
    db_path,
    inverted_index,
    document_metadata,
    filename_index,
    page_text_index,
    connection=None,
):
    """
    Full synchronization used only for initial migration or an explicit
    manual full rebuild. Normal uploads/deletes do NOT call this.
    """

    close_connection = False

    if connection is None:
        connection = get_sqlite_connection(db_path)
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

        for filename, metadata in document_metadata.items():

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

        for term, posting_list in inverted_index.items():

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

        for filename, filename_words in filename_index.items():

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

        for filename, pages in page_text_index.items():

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


def load_database_from_sqlite(*, db_path):
    """
    Load the complete persisted SQLite snapshot into the existing
    in-memory search structures once at application startup.
    """

    with get_sqlite_connection(db_path) as connection:

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

        new_metadata = {}

        for (
            filename,
            title,
            path,
            total_words,
            page_count,
        ) in document_rows:

            new_metadata[filename] = {
                "title": title,
                "path": path,
                "total_words": total_words,
                "page_count": page_count,
            }

        new_inverted_index = {}

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

            if term not in new_inverted_index:
                new_inverted_index[term] = {}

            new_inverted_index[term][
                filename
            ] = term_count

        new_filename_index = {}

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

            new_filename_index.setdefault(
                filename,
                [],
            ).append(term)

        new_page_text_index = {}

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

            new_page_text_index.setdefault(
                filename,
                [],
            ).append({
                "page": page_number,
                "text": text,
            })

        return {
            "inverted_index": new_inverted_index,
            "document_metadata": new_metadata,
            "filename_index": new_filename_index,
            "page_text_index": new_page_text_index,
        }


def get_document_term_counts_from_sqlite(
    db_path,
    filename,
):
    with get_sqlite_connection(db_path) as connection:

        rows = connection.execute(
            """
            SELECT term, term_count
            FROM term_postings
            WHERE filename = ?
            """,
            (filename,),
        ).fetchall()

    return dict(rows)


def load_json_snapshot(
    *,
    index_file,
    meta_file,
    filename_index_file,
    page_text_file,
    current,
):

    # Seed from the current state so that a missing snapshot file
    # leaves that container untouched, which is what the previous
    # implementation did by never rebinding in that case.
    loaded_inverted_index = current["inverted_index"]
    loaded_document_metadata = current["document_metadata"]
    loaded_filename_index = current["filename_index"]
    loaded_page_text_index = current["page_text_index"]

    if os.path.exists(index_file):

        try:

            with open(
                index_file,
                "r",
                encoding="utf-8"
            ) as file:

                loaded_inverted_index = json.load(
                    file
                )

        except Exception as error:

            print(
                f"[DATABASE ERROR] Could not load "
                f"content index: {error}"
            )

            loaded_inverted_index = {}

    if os.path.exists(meta_file):

        try:

            with open(
                meta_file,
                "r",
                encoding="utf-8"
            ) as file:

                loaded_document_metadata = json.load(
                    file
                )

        except Exception as error:

            print(
                f"[DATABASE ERROR] Could not load "
                f"metadata: {error}"
            )

            loaded_document_metadata = {}

    if os.path.exists(filename_index_file):

        try:

            with open(
                filename_index_file,
                "r",
                encoding="utf-8"
            ) as file:

                loaded_filename_index = json.load(
                    file
                )

        except Exception as error:

            print(
                f"[DATABASE ERROR] Could not load "
                f"filename index: {error}"
            )

            loaded_filename_index = {}

    if os.path.exists(page_text_file):

        try:

            with open(
                page_text_file,
                "r",
                encoding="utf-8"
            ) as file:

                loaded_page_text_index = json.load(
                    file
                )

        except Exception as error:

            print(
                f"[DATABASE ERROR] Could not load "
                f"page text index: {error}"
            )

            loaded_page_text_index = {}

    return {
        "inverted_index": loaded_inverted_index,
        "document_metadata": loaded_document_metadata,
        "filename_index": loaded_filename_index,
        "page_text_index": loaded_page_text_index,
    }


def atomic_write_json(path, data):
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


def save_database_snapshot(
    *,
    index_file,
    meta_file,
    filename_index_file,
    page_text_file,
    inverted_index,
    document_metadata,
    filename_index,
    page_text_index,
):
    atomic_write_json(
        index_file,
        inverted_index,
    )

    atomic_write_json(
        meta_file,
        document_metadata,
    )

    atomic_write_json(
        filename_index_file,
        filename_index,
    )

    atomic_write_json(
        page_text_file,
        page_text_index,
    )


def save_database(
    *,
    index_file,
    meta_file,
    filename_index_file,
    page_text_file,
    inverted_index,
    document_metadata,
    filename_index,
    page_text_index,
):
    save_database_snapshot(
        index_file=index_file,
        meta_file=meta_file,
        filename_index_file=filename_index_file,
        page_text_file=page_text_file,
        inverted_index=inverted_index,
        document_metadata=document_metadata,
        filename_index=filename_index,
        page_text_index=page_text_index,
    )


def create_schema(connection):
    """
    Create every table and index the store needs.

    `SQLITE_SCHEMA` is idempotent (`IF NOT EXISTS` throughout), so this
    is safe on an existing database. It takes a connection rather than a
    path so the caller can run the schema and the migration check inside
    one connection, which is what app.py did before this was extracted.
    """

    connection.executescript(
        SQLITE_SCHEMA
    )


def count_documents(connection):
    """Return the number of rows in `documents`."""

    return connection.execute(
        "SELECT COUNT(*) FROM documents"
    ).fetchone()[0]
