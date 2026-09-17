"""
The incremental write path belongs to the storage layer, not the routes.

The last SQL in `app.py` lived in `incrementally_index_document` and
`incrementally_remove_document`. Both are now orchestration: they decide
*when* to write and *who owns the transaction*, and the statements
themselves live in `search_engine/storage.py` as
`replace_document_rows` and `delete_document_rows`.

Moving those statements is only safe if they still do exactly what the
inline versions did, including the parts that are easy to lose when a
block of SQL is lifted into a function:

* a re-upload must not leave the previous revision's rows behind;
* a delete must clear all four tables, not just the obvious ones;
* `commit=False` must keep the caller's transaction shared, so a batch
  is still all-or-nothing;
* deleting an unknown document must stay a silent no-op.

`tools/storage_equivalence.py` proves the end state matches the monolith
across 8 fixtures. These tests pin the behaviour directly, including the
transaction shape that the end-state comparison cannot see.
"""

import ast
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT))

from search_engine import storage  # noqa: E402

APP_PATH = REPO_ROOT / "app.py"
STORAGE_PATH = REPO_ROOT / "search_engine" / "storage.py"

TABLES = ("documents", "term_postings", "filename_terms", "pages")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def make_store(tmp_path, name="search.db"):
    """A real database with the real schema, in a throwaway directory."""

    db_path = tmp_path / name

    connection = storage.get_sqlite_connection(str(db_path))

    storage.create_schema(connection)

    return connection


def row_counts(connection):
    """Row count per table, as a dict."""

    return {
        table: connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]
        for table in TABLES
    }


def every_term(connection):
    """All stored terms, as one string, for a `not in` assertion."""

    return " ".join(
        row[0]
        for row in connection.execute("SELECT term FROM term_postings")
    )


def rows_for(connection, filename):
    """Every stored row for one document, in a stable order."""

    return {
        "documents": connection.execute(
            """
            SELECT filename, title, total_words, page_count
            FROM documents WHERE filename = ?
            """,
            (filename,),
        ).fetchall(),
        "term_postings": connection.execute(
            """
            SELECT term, term_count FROM term_postings
            WHERE filename = ? ORDER BY term
            """,
            (filename,),
        ).fetchall(),
        "filename_terms": connection.execute(
            """
            SELECT position, term FROM filename_terms
            WHERE filename = ? ORDER BY position
            """,
            (filename,),
        ).fetchall(),
        "pages": connection.execute(
            """
            SELECT page_number, text FROM pages
            WHERE filename = ? ORDER BY page_number
            """,
            (filename,),
        ).fetchall(),
    }


def document(
    filename,
    *,
    title=None,
    path="/tmp/source.txt",
    terms=None,
    words=None,
    pages=None,
):
    """The three inputs `replace_document_rows` takes, plus metadata."""

    return {
        "metadata": {
            "title": title or filename,
            "path": path,
            "total_words": sum((terms or {"alpha": 1}).values()),
            "page_count": len(pages or [{"page": 1, "text": "alpha"}]),
        },
        "term_counts": terms or {"alpha": 1},
        "filename_words": words or ["alpha"],
        "pages": pages or [{"page": 1, "text": "alpha"}],
    }


def write(connection, filename, **kwargs):
    payload = document(filename, **kwargs)

    storage.replace_document_rows(
        connection,
        filename=filename,
        **payload,
    )

    return payload


# ----------------------------------------------------------------------
# Where the code lives
# ----------------------------------------------------------------------


def source_of(path):
    return path.read_text(encoding="utf-8")


def test_app_still_contains_no_sql():
    """
    The extraction's whole point, restated where it can fail locally.

    `tests/test_storage_parity.py` asserts this too, from the direction
    of "no function holds SQL". This asserts it from the direction of
    literal statements, so a statement added to a lambda or a nested
    scope is still caught.
    """

    source = source_of(APP_PATH)

    for marker in ("INSERT INTO", "DELETE FROM", "SELECT ", "PRAGMA"):
        assert marker not in source, (
            f"app.py contains {marker!r}; statements belong in "
            "search_engine/storage.py"
        )


def test_both_write_functions_are_reachable_from_the_storage_layer():
    """The export surface the adapter calls must exist."""

    for name in ("replace_document_rows", "delete_document_rows"):
        assert callable(getattr(storage, name)), f"missing {name}"


def test_storage_module_is_still_flask_free():
    """The split exists so the Android backend can reuse this module."""

    source = source_of(STORAGE_PATH)

    tree = ast.parse(source)

    imported = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported == {"json", "os", "sqlite3"}, (
        f"search_engine/storage.py imports {sorted(imported)}"
    )


# ----------------------------------------------------------------------
# replace_document_rows
# ----------------------------------------------------------------------


def test_a_fresh_write_lands_in_all_four_tables(tmp_path):
    connection = make_store(tmp_path)

    write(
        connection,
        "alpha.txt",
        terms={"alpha": 2, "beta": 1},
        words=["alpha", "beta"],
        pages=[
            {"page": 1, "text": "alpha"},
            {"page": 2, "text": "beta"},
        ],
    )

    connection.commit()

    assert row_counts(connection) == {
        "documents": 1,
        "term_postings": 2,
        "filename_terms": 2,
        "pages": 2,
    }

    assert rows_for(connection, "alpha.txt") == {
        "documents": [("alpha.txt", "alpha.txt", 3, 2)],
        "term_postings": [("alpha", 2), ("beta", 1)],
        "filename_terms": [(0, "alpha"), (1, "beta")],
        "pages": [(1, "alpha"), (2, "beta")],
    }

    connection.close()


def test_a_replacement_leaves_no_row_from_the_previous_revision(tmp_path):
    """
    The defect this guards is a re-upload that keeps the old postings.

    A replacement is *not* a merge: every table must show the new
    revision's rows and nothing else.
    """

    connection = make_store(tmp_path)

    write(
        connection,
        "alpha.txt",
        terms={"alpha": 5, "stale": 9, "oldterm": 3},
        words=["alpha", "stale"],
        pages=[{"page": 1, "text": "old"}, {"page": 2, "text": "older"}],
    )

    connection.commit()

    write(
        connection,
        "alpha.txt",
        title="alpha.txt (v2)",
        terms={"fresh": 1},
        words=["fresh"],
        pages=[{"page": 1, "text": "new"}],
    )

    connection.commit()

    stored = rows_for(connection, "alpha.txt")

    assert stored["documents"] == [("alpha.txt", "alpha.txt (v2)", 1, 1)]
    assert stored["term_postings"] == [("fresh", 1)]
    assert stored["filename_terms"] == [(0, "fresh")]
    assert stored["pages"] == [(1, "new")]

    assert row_counts(connection) == {
        "documents": 1,
        "term_postings": 1,
        "filename_terms": 1,
        "pages": 1,
    }

    connection.close()


def test_repeated_tokens_keep_every_position(tmp_path):
    """The pack-4 guarantee, restated against the new call path."""

    connection = make_store(tmp_path)

    write(connection, "repeat repeat.txt", words=["repeat", "repeat"])

    connection.commit()

    assert rows_for(connection, "repeat repeat.txt")["filename_terms"] == [
        (0, "repeat"),
        (1, "repeat"),
    ]

    connection.close()


def test_filename_words_are_stored_in_the_given_order(tmp_path):
    """Order comes from the argument, not from iteration order."""

    connection = make_store(tmp_path)

    write(
        connection,
        "zulu alpha mike.txt",
        words=["zulu", "alpha", "mike"],
    )

    connection.commit()

    assert rows_for(connection, "zulu alpha mike.txt")["filename_terms"] == [
        (0, "zulu"),
        (1, "alpha"),
        (2, "mike"),
    ]

    connection.close()


def test_a_document_with_no_pages_or_terms_still_writes_its_row(tmp_path):
    """The empty cases must not raise; they just contribute no child rows."""

    connection = make_store(tmp_path)

    storage.replace_document_rows(
        connection,
        filename="empty.txt",
        metadata={
            "title": "empty.txt",
            "path": "/tmp/empty.txt",
            "total_words": 0,
            "page_count": 0,
        },
        term_counts={},
        filename_words=[],
        pages=[],
    )

    connection.commit()

    assert row_counts(connection) == {
        "documents": 1,
        "term_postings": 0,
        "filename_terms": 0,
        "pages": 0,
    }

    connection.close()


def test_two_documents_do_not_overwrite_each_other(tmp_path):
    """The replace is scoped by filename."""

    connection = make_store(tmp_path)

    write(connection, "one.txt", terms={"shared": 1, "oneonly": 1})
    write(connection, "two.txt", terms={"shared": 1, "twoonly": 1})

    connection.commit()

    assert rows_for(connection, "one.txt")["term_postings"] == [
        ("oneonly", 1),
        ("shared", 1),
    ]
    assert rows_for(connection, "two.txt")["term_postings"] == [
        ("shared", 1),
        ("twoonly", 1),
    ]

    connection.close()


# ----------------------------------------------------------------------
# delete_document_rows
# ----------------------------------------------------------------------


def test_delete_clears_all_four_tables(tmp_path):
    """
    Missing one table is the realistic failure, so each is asserted.

    A leftover `filename_terms` row would resurrect a phantom filename,
    and a leftover `documents` row would keep the document reloading
    after a restart even though its file is gone.
    """

    connection = make_store(tmp_path)

    write(
        connection,
        "alpha.txt",
        terms={"alpha": 2, "beta": 1},
        words=["alpha", "beta"],
        pages=[
            {"page": 1, "text": "alpha"},
            {"page": 2, "text": "beta"},
        ],
    )

    connection.commit()

    storage.delete_document_rows(connection, filename="alpha.txt")

    connection.commit()

    assert row_counts(connection) == {
        "documents": 0,
        "term_postings": 0,
        "filename_terms": 0,
        "pages": 0,
    }

    connection.close()


def test_deleting_an_unknown_document_is_a_silent_no_op(tmp_path):
    """
    The original inline code issued four unconditional DELETEs.

    It did not check for the document first and did not raise, so this
    must not either - `bulk_delete_documents` relies on it while
    iterating a list that may name documents that are not indexed.
    """

    connection = make_store(tmp_path)

    write(connection, "keep.txt")

    connection.commit()

    before = row_counts(connection)

    storage.delete_document_rows(connection, filename="never_existed.txt")

    connection.commit()

    assert row_counts(connection) == before == {
        "documents": 1,
        "term_postings": 1,
        "filename_terms": 1,
        "pages": 1,
    }

    connection.close()


def test_delete_leaves_other_documents_untouched(tmp_path):
    connection = make_store(tmp_path)

    write(connection, "one.txt", terms={"shared": 1})
    write(connection, "two.txt", terms={"shared": 1})
    connection.commit()

    storage.delete_document_rows(connection, filename="one.txt")
    connection.commit()

    assert row_counts(connection) == {
        "documents": 1,
        "term_postings": 1,
        "filename_terms": 1,
        "pages": 1,
    }

    assert rows_for(connection, "two.txt")["term_postings"] == [("shared", 1)]

    connection.close()


def test_delete_is_a_clean_rewrite_point_for_a_re_add(tmp_path):
    """Delete then re-add must behave like a first insert."""

    connection = make_store(tmp_path)

    write(
        connection,
        "alpha.txt",
        terms={"alpha": 4, "stale": 2},
        words=["alpha", "stale"],
    )
    connection.commit()

    storage.delete_document_rows(connection, filename="alpha.txt")
    connection.commit()

    write(connection, "alpha.txt", terms={"alpha": 1}, words=["alpha"])
    connection.commit()

    stored = rows_for(connection, "alpha.txt")

    assert stored["term_postings"] == [("alpha", 1)]
    assert stored["filename_terms"] == [(0, "alpha")]
    assert "stale" not in every_term(connection)

    connection.close()


# ----------------------------------------------------------------------
# The transaction shape - what the end-state comparison cannot see
# ----------------------------------------------------------------------


def test_commit_false_leaves_the_write_uncommitted_for_another_reader(tmp_path):
    """
    `commit=False` is what lets bulk delete share one transaction.

    A second connection must not see the change until the caller
    commits, or the batch has silently become per-document.
    """

    writer = make_store(tmp_path)

    write(writer, "alpha.txt")
    writer.commit()

    storage.delete_document_rows(writer, filename="alpha.txt")

    # Not committed yet.
    observer = storage.get_sqlite_connection(str(tmp_path / "search.db"))

    assert observer.execute(
        "SELECT COUNT(*) FROM documents"
    ).fetchone()[0] == 1, (
        "the delete was visible before commit, so commit=False no longer "
        "defers the write"
    )

    observer.close()

    writer.commit()

    observer = storage.get_sqlite_connection(str(tmp_path / "search.db"))

    assert observer.execute(
        "SELECT COUNT(*) FROM documents"
    ).fetchone()[0] == 0

    observer.close()
    writer.close()


def test_a_batch_rolls_back_as_one_unit(tmp_path):
    """
    The shape `bulk_delete_documents` depends on.

    It commits the whole batch once, and rolls back on any failure. So a
    batch that deleted document one and then failed on document two must
    restore document one as well - the two deletes are one unit.

    A fresh connection has no pending transaction, which is why the
    baseline is committed here before the batch starts: the rollback
    must undo the deletes, not the documents that were already stored.
    """

    connection = make_store(tmp_path)

    write(connection, "one.txt", terms={"one": 1})
    write(connection, "two.txt", terms={"two": 1})

    connection.commit()

    baseline = row_counts(connection)

    assert baseline == {
        "documents": 2,
        "term_postings": 2,
        "filename_terms": 2,
        "pages": 2,
    }

    # The batch: two removals, commit=False, then failure.
    storage.delete_document_rows(connection, filename="one.txt")
    storage.delete_document_rows(connection, filename="two.txt")

    connection.rollback()

    assert row_counts(connection) == baseline, (
        "a rolled-back batch did not restore the earlier documents"
    )

    assert rows_for(connection, "one.txt")["term_postings"] == [("one", 1)]
    assert rows_for(connection, "two.txt")["term_postings"] == [("two", 1)]

    connection.close()


def test_a_batch_commits_as_one_unit(tmp_path):
    """The other half: after the single commit, both are gone."""

    connection = make_store(tmp_path)

    write(connection, "one.txt", terms={"one": 1})
    write(connection, "two.txt", terms={"two": 1})

    connection.commit()

    storage.delete_document_rows(connection, filename="one.txt")
    storage.delete_document_rows(connection, filename="two.txt")

    # Mid-batch, on this connection, both are already invisible.
    assert row_counts(connection) == {
        "documents": 0,
        "term_postings": 0,
        "filename_terms": 0,
        "pages": 0,
    }

    connection.commit()

    assert row_counts(connection) == {
        "documents": 0,
        "term_postings": 0,
        "filename_terms": 0,
        "pages": 0,
    }

    connection.close()


def test_the_app_delete_path_calls_the_storage_helper():
    """
    Pin the wiring, not just the helper.

    `delete_document_rows` can be perfect and still not be called; the
    inline SQL would then have to have been deleted separately for
    `test_app_still_contains_no_sql` to pass, so this closes the loop
    by naming the call site.
    """

    tree = ast.parse(source_of(APP_PATH))

    callers = {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        body = ast.get_source_segment(source_of(APP_PATH), node) or ""

        for helper in ("replace_document_rows", "delete_document_rows"):
            if f"storage.{helper}" in body:
                callers.setdefault(helper, set()).add(node.name)

    assert callers == {
        "replace_document_rows": {"incrementally_index_document"},
        "delete_document_rows": {"incrementally_remove_document"},
    }, f"unexpected call sites: {callers}"
