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

* `filename_terms` stores a `position` per row and is read back with
  `ORDER BY filename, position`, so a document's token sequence
  round-trips exactly, including repeated tokens. It used to be a
  `set()` under a (filename, term) primary key, which lost both the
  order and the duplicates and changed `normalized_filename` across a
  restart.
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
    position INTEGER NOT NULL,
    term TEXT NOT NULL,
    PRIMARY KEY (filename, position)
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

            # Every occurrence, in order, with its position.
            #
            # This used to be `set(filename_words)` under a primary key
            # of (filename, term). That lost the sequence twice over: set
            # iteration order decided the stored order, and the primary
            # key silently collapsed repeated tokens. Reading back in
            # rowid order therefore produced a different filename_index
            # than the one that was live, which changes
            # `normalized_filename` and breaks quoted filename phrases.
            connection.executemany(
                """
                INSERT INTO filename_terms
                (filename, position, term)
                VALUES (?, ?, ?)
                """,
                [
                    (
                        filename,
                        position,
                        term,
                    )
                    for position, term
                    in enumerate(filename_words)
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


def delete_document_rows(connection, *, filename):
    """
    Remove every persisted row belonging to one document.

    The four statements run in the same order as the inline
    implementation this replaces - postings, filename tokens, pages,
    then the document row - because the caller may be sharing a
    transaction across several documents, and that order is what a
    `rollback()` mid-batch is observed against.
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


def replace_document_rows(
    connection,
    *,
    filename,
    metadata,
    term_counts,
    filename_words,
    pages,
):
    """
    Persist one document's complete row set, replacing anything already
    stored for that name.

    This is the single-document write path behind uploads. It deletes
    first so a re-upload cannot leave the previous revision's postings,
    tokens or pages behind, and it writes the tables in the same order
    as the inline implementation it replaces.

    `filename_words` is stored with an explicit position and without
    deduplication. The sequence is what `normalized_filename` is rebuilt
    from after a restart, so both order and repeats are load-bearing.
    """

    delete_document_rows(
        connection,
        filename=filename,
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
        INSERT INTO filename_terms
        (filename, position, term)
        VALUES (?, ?, ?)
        """,
        [
            (
                filename,
                position,
                term,
            )
            for position, term
            in enumerate(filename_words)
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
            ORDER BY filename, position
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


# The schema revision that gave `filename_terms` an explicit `position`.
# Databases created before it stored one row per distinct token with the
# order implied by insertion, which did not round-trip.
FILENAME_TERM_POSITION_SCHEMA = 2


def filename_term_columns(connection):
    """Return the column names of `filename_terms`, or [] if absent."""

    rows = connection.execute(
        "PRAGMA table_info(filename_terms)"
    ).fetchall()

    return [row[1] for row in rows]


def natural_filename_terms(filename, tokenize_filename):
    """
    Return the filename tokens the writers would produce for `filename`.

    Both writers derive their tokens as
    `tokenize_filename(os.path.splitext(filename)[0])`, so the sequence
    is reproducible from the stored document name alone. That is what
    makes a repair possible without re-reading any document.

    `tokenize_filename` is injected rather than imported: persistence
    should not own tokenization semantics, it should apply whatever the
    engine says they are.
    """

    return tokenize_filename(
        os.path.splitext(filename)[0]
    )


def _read_filename_terms(connection):
    """Return {filename: [(position, term), ...]} in stored order."""

    rows = connection.execute(
        """
        SELECT filename, position, term
        FROM filename_terms
        ORDER BY filename, position
        """
    ).fetchall()

    stored = {}

    for filename, position, term in rows:
        stored.setdefault(filename, []).append((position, term))

    return stored


def migrate_filename_terms(connection, *, tokenize_filename):
    """
    Bring `filename_terms` up to the position-carrying schema and repair
    any document whose stored token sequence is wrong.

    Two things are fixed here, because the old schema lost the sequence
    in two independent ways:

    * it defaulted to a `(filename, term)` primary key, so repeated
      tokens were collapsed;
    * the writers inserted `set(filename_words)`, so the surviving order
      was set-iteration order - different in every process.

    Existing databases may carry either or both. Because the correct
    sequence is derivable from the document name, every document is
    checked against the tokenizer and rewritten when it disagrees. A
    database that is already correct is left untouched, so this is a
    no-op when there is nothing to do, and it is idempotent.

    Returns a summary so the caller - and the tests - can see what
    happened rather than having to infer it.
    """

    columns = filename_term_columns(connection)

    schema_changed = "position" not in columns

    if schema_changed:

        # SQLite cannot alter a primary key, so the table is rebuilt.
        # `filename_terms` holds only derived data, so nothing is lost:
        # every row is regenerated from `documents` below.
        connection.execute(
            "DROP TABLE IF EXISTS filename_terms"
        )

        connection.execute(
            """
            CREATE TABLE filename_terms (
                filename TEXT NOT NULL,
                position INTEGER NOT NULL,
                term TEXT NOT NULL,
                PRIMARY KEY (filename, position)
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_filename_terms_term
            ON filename_terms(term)
            """
        )

    documents = connection.execute(
        "SELECT filename FROM documents ORDER BY filename"
    ).fetchall()

    stored = _read_filename_terms(connection)

    repaired = []

    for (filename,) in documents:

        expected = natural_filename_terms(
            filename,
            tokenize_filename,
        )

        existing = stored.get(filename, [])

        # Compare the term sequence only. Positions are rewritten from
        # scratch, so a correct sequence under wrong positions still
        # counts as correct once the rows are replaced below.
        if (
            not schema_changed
            and [term for _position, term in existing] == expected
        ):
            continue

        connection.execute(
            "DELETE FROM filename_terms WHERE filename = ?",
            (filename,),
        )

        connection.executemany(
            """
            INSERT INTO filename_terms
            (filename, position, term)
            VALUES (?, ?, ?)
            """,
            [
                (filename, position, term)
                for position, term in enumerate(expected)
            ],
        )

        repaired.append(filename)

    # Rows whose document no longer exists would never be reached above
    # and would be served as phantom filename tokens.
    orphans = sorted(set(stored) - {name for (name,) in documents})

    for filename in orphans:
        connection.execute(
            "DELETE FROM filename_terms WHERE filename = ?",
            (filename,),
        )

    return {
        "schema_changed": schema_changed,
        "documents": len(documents),
        "repaired": repaired,
        "orphans_removed": orphans,
    }


def count_documents(connection):
    """Return the number of rows in `documents`."""

    return connection.execute(
        "SELECT COUNT(*) FROM documents"
    ).fetchone()[0]
