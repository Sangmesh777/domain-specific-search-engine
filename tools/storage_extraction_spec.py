"""
Extraction spec for `search_engine/storage.py`.

Kept in its own module because the persistence layer needs more than the
text renames that `extract_modules.py` was built for: whole-signature
replacements, and one body that must *return* a snapshot instead of
publishing one.

Nothing here retypes a function body. Every function listed in
`FUNCTIONS` is sliced out of `app.py` by AST line range and then rewritten
by the exact-text replacements in `TRANSFORMS`. A replacement that does
not match exactly once is an error, so a spec that has drifted out of
date fails loudly instead of producing a module that looks plausible.

The rewrites encode one design rule:

    storage.py knows about SQLite, JSON files and filesystem paths.
    It knows nothing about Flask, about module-level index globals, or
    about how state is published.

That is why `load_database_from_sqlite` and `load_database` return a
complete snapshot rather than installing one, and why
`sync_sqlite_from_memory` takes the four containers as parameters rather
than reading the globals. Publication is the application's job, and it
happens under `INDEX_DATA_LOCK` in `app.py`.
"""

TITLE = "SQLite and JSON persistence."

DOCSTRING = """
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

FUNCTIONS = [
    "get_sqlite_connection",
    "sync_sqlite_from_memory",
    "load_database_from_sqlite",
    "get_document_term_counts_from_sqlite",
    "load_database",
    "atomic_write_json",
    "save_database_snapshot",
    "save_database",
]

# `load_database` is called `load_json_snapshot` here, because in this
# module "the database" is ambiguous between the SQLite store and the
# legacy JSON snapshot files.
RENAMES = {
    "load_database": "load_json_snapshot",
}

CONSTANTS = ["SQLITE_SCHEMA"]

# Removed from app.py, but not written here: the replacement is a wrapper
# in app.py that orchestrates storage primitives, so it has no body to
# move.
REPLACED = ["initialize_sqlite_store"]

IMPORTS = [
    "import json",
    "import os",
    "import sqlite3",
]

EXTRA_SOURCE = '''
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
'''

TRANSFORMS = {
    "get_sqlite_connection": {
        # The path was a module global; make it an argument.
        "replace_exact": [
            [
                "def get_sqlite_connection():",
                "def get_sqlite_connection(db_path):",
            ],
        ],
        "rename_names": {"SQLITE_DB_FILE": "db_path"},
    },
    "sync_sqlite_from_memory": {
        # This used to read the four globals directly. It now takes them,
        # so the caller states exactly which generation is being written
        # to disk instead of the function guessing.
        "replace_exact": [
            [
                "def sync_sqlite_from_memory(connection=None):",
                "def sync_sqlite_from_memory(\n"
                "    *,\n"
                "    db_path,\n"
                "    inverted_index,\n"
                "    document_metadata,\n"
                "    filename_index,\n"
                "    page_text_index,\n"
                "    connection=None,\n"
                "):",
            ],
            [
                "connection = get_sqlite_connection()",
                "connection = get_sqlite_connection(db_path)",
            ],
        ],
        "rename_names": {
            "DOCUMENT_METADATA": "document_metadata",
            "REAL_INVERTED_INDEX": "inverted_index",
            "FILENAME_INDEX": "filename_index",
            "PAGE_TEXT_INDEX": "page_text_index",
        },
    },
    "load_database_from_sqlite": {
        # Returns a snapshot instead of publishing one, and returns None
        # when there is nothing to load, which the caller turns back into
        # the original "leave the active state alone" behaviour.
        "replace_exact": [
            [
                "def load_database_from_sqlite():",
                "def load_database_from_sqlite(*, db_path):",
            ],
            [
                "    global REAL_INVERTED_INDEX\n"
                "    global DOCUMENT_METADATA\n"
                "    global FILENAME_INDEX\n"
                "    global PAGE_TEXT_INDEX\n\n",
                "",
            ],
            [
                "with get_sqlite_connection() as connection:",
                "with get_sqlite_connection(db_path) as connection:",
            ],
            [
                "        if not document_rows:\n            return\n",
                "        if not document_rows:\n            return None\n",
            ],
            [
                "        with INDEX_DATA_LOCK:\n\n"
                "            publish_index_state({\n"
                '                "inverted_index": new_inverted_index,\n'
                '                "document_metadata": new_metadata,\n'
                '                "filename_index": new_filename_index,\n'
                '                "page_text_index": new_page_text_index,\n'
                "            })\n",
                "        return {\n"
                '            "inverted_index": new_inverted_index,\n'
                '            "document_metadata": new_metadata,\n'
                '            "filename_index": new_filename_index,\n'
                '            "page_text_index": new_page_text_index,\n'
                "        }\n",
            ],
        ],
    },
    "get_document_term_counts_from_sqlite": {
        "replace_exact": [
            [
                "def get_document_term_counts_from_sqlite(\n    filename,\n):",
                "def get_document_term_counts_from_sqlite(\n"
                "    db_path,\n"
                "    filename,\n"
                "):",
            ],
            [
                "with get_sqlite_connection() as connection:",
                "with get_sqlite_connection(db_path) as connection:",
            ],
        ],
    },
    "load_database": {
        "replace_exact": [
            [
                "def load_database():",
                "def load_json_snapshot(\n"
                "    *,\n"
                "    index_file,\n"
                "    meta_file,\n"
                "    filename_index_file,\n"
                "    page_text_file,\n"
                "    current,\n"
                "):",
            ],
            [
                "    current = INDEX_STATE.snapshot()\n\n",
                "",
            ],
            [
                "    # Startup restore. This runs once at import, before Flask serves any\n"
                "    # request, so the lock is uncontended - but it is taken anyway so the\n"
                "    # invariant is uniform and checkable: every publication of index\n"
                "    # state happens inside one INDEX_DATA_LOCK critical section.\n"
                "    with INDEX_DATA_LOCK:\n\n"
                "        publish_index_state({\n"
                '            "inverted_index": loaded_inverted_index,\n'
                '            "document_metadata": loaded_document_metadata,\n'
                '            "filename_index": loaded_filename_index,\n'
                '            "page_text_index": loaded_page_text_index,\n'
                "        })\n",
                "    return {\n"
                '        "inverted_index": loaded_inverted_index,\n'
                '        "document_metadata": loaded_document_metadata,\n'
                '        "filename_index": loaded_filename_index,\n'
                '        "page_text_index": loaded_page_text_index,\n'
                "    }\n",
            ],
        ],
        # Ordered so the longer names are rewritten first: renaming
        # INDEX_FILE before FILENAME_INDEX_FILE would corrupt the latter.
        # (\bFILENAME_INDEX\b does not match inside FILENAME_INDEX_FILE,
        # but being explicit here costs nothing and documents the hazard.)
        "rename_names": {
            "FILENAME_INDEX_FILE": "filename_index_file",
            "PAGE_TEXT_FILE": "page_text_file",
            "INDEX_FILE": "index_file",
            "META_FILE": "meta_file",
        },
    },
    "save_database_snapshot": {
        "replace_exact": [
            [
                "def save_database_snapshot(\n"
                "    inverted_index,\n"
                "    document_metadata,\n"
                "    filename_index,\n"
                "    page_text_index,\n"
                "):",
                "def save_database_snapshot(\n"
                "    *,\n"
                "    index_file,\n"
                "    meta_file,\n"
                "    filename_index_file,\n"
                "    page_text_file,\n"
                "    inverted_index,\n"
                "    document_metadata,\n"
                "    filename_index,\n"
                "    page_text_index,\n"
                "):",
            ],
        ],
        "rename_names": {
            "FILENAME_INDEX_FILE": "filename_index_file",
            "PAGE_TEXT_FILE": "page_text_file",
            "INDEX_FILE": "index_file",
            "META_FILE": "meta_file",
        },
    },
    "save_database": {
        "replace_exact": [
            [
                "def save_database():",
                "def save_database(\n"
                "    *,\n"
                "    index_file,\n"
                "    meta_file,\n"
                "    filename_index_file,\n"
                "    page_text_file,\n"
                "    inverted_index,\n"
                "    document_metadata,\n"
                "    filename_index,\n"
                "    page_text_index,\n"
                "):",
            ],
            [
                "    with INDEX_DATA_LOCK:\n"
                "        snapshot = (\n"
                "            REAL_INVERTED_INDEX,\n"
                "            DOCUMENT_METADATA,\n"
                "            FILENAME_INDEX,\n"
                "            PAGE_TEXT_INDEX,\n"
                "        )\n\n"
                "    save_database_snapshot(*snapshot)\n",
                "    save_database_snapshot(\n"
                "        index_file=index_file,\n"
                "        meta_file=meta_file,\n"
                "        filename_index_file=filename_index_file,\n"
                "        page_text_file=page_text_file,\n"
                "        inverted_index=inverted_index,\n"
                "        document_metadata=document_metadata,\n"
                "        filename_index=filename_index,\n"
                "        page_text_index=page_text_index,\n"
                "    )\n",
            ],
        ],
    },
}
