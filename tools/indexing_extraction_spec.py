"""
The indexing layer's extraction spec.

`rebuild_database` was two things in one function: a build, which walks
the document folder and produces four new containers, and a publish,
which saves, swaps and syncs them. Only the first is the engine's
business. The second is ordering, locking and durability, which belong
to the application - and the order is load-bearing:

    build -> save JSON snapshot -> publish -> sync SQLite

That is the order that ships today. An earlier design note assumed
sync-then-publish; the real order is preserved here so the extraction
does not quietly "correct" it.

So the transform below deletes the publish tail from the moved body and
returns the snapshot instead. The adapter keeps the tail, in the same
order, calling the extracted build. Nothing is retyped: the build moves
verbatim.

`resolve_document_path` moves whole. It is pure, and it is the guard
that stops a document URL escaping the data folder, so it belongs beside
the code that owns document layout rather than inside a route handler.
"""

TITLE = "Document layout and index building."

DOCSTRING = """
Turning the document folder into an index, and locating documents
inside it.

This module owns two things the offline mode needs as much as the online
one does:

* which files in a folder become documents, and what a document's
  metadata, tokens and pages look like;
* where a named document actually lives, and whether that path is
  allowed.

It does not own publication. `build_index_from_folder` returns a
complete snapshot and installs nothing; the caller decides when to save
it, when to swap it in, and when to mirror it to SQLite. That keeps the
rebuild ordering in one place - the application - instead of spreading
it across two modules.

A document that cannot be extracted is skipped with a printed reason
rather than failing the rebuild. That behaviour is unchanged.
"""

FUNCTIONS = [
    "resolve_document_path",
    "rebuild_database",
]

# `rebuild_database` no longer rebuilds *and* publishes, so the name has
# to say what it now does.
RENAMES = {
    "rebuild_database": "build_index_from_folder",
}

IMPORTS = [
    "import os",
    "",
    "from search_engine.extract import extract_pages, extract_text",
    "from search_engine.text import tokenize, tokenize_filename",
]

# The publish tail, taken verbatim from the end of the original body.
# It is replaced by the return below, and re-written in app.py by the
# wrapper in the same order. Embedded with repr() rather than retyped,
# so the match cannot drift on whitespace.
PUBLISH_TAIL = '    # Write the full new snapshot first.\n    save_database_snapshot(\n        new_inverted_index,\n        new_document_metadata,\n        new_filename_index,\n        new_page_text_index,\n    )\n\n    global REAL_INVERTED_INDEX\n    global DOCUMENT_METADATA\n    global FILENAME_INDEX\n    global PAGE_TEXT_INDEX\n\n    # One pointer swap: searches see old or new, never a partial build.\n    # The globals are re-pointed inside the same critical section,\n    # because execute_search captures all four of them under this lock.\n    # Re-pointing them after releasing it would let a reader capture a\n    # half-rebound set - one global from the new generation and one from\n    # the old.\n    with INDEX_DATA_LOCK:\n        publish_index_state({\n            "inverted_index": new_inverted_index,\n            "document_metadata": new_document_metadata,\n            "filename_index": new_filename_index,\n            "page_text_index": new_page_text_index,\n        })\n\n    print(\n        "[REBUILD] Active snapshot swapped atomically."\n    )\n\n    # Keep SQLite synchronized after an explicit full rebuild.\n    with get_sqlite_connection() as connection:\n        sync_sqlite_from_memory(connection)\n\n    print()\n    print(\n        f"[REBUILD] Complete: "\n        f"{len(DOCUMENT_METADATA)} documents, "\n        f"{len(REAL_INVERTED_INDEX)} content terms, "\n        f"{len(FILENAME_INDEX)} filenames indexed, "\n        f"{len(PAGE_TEXT_INDEX)} page-text entries"\n    )\n\n    print("==============================================")\n    print()\n'

TRANSFORMS = {
    "resolve_document_path": {
        # The folder was a module global. A function that decides where a
        # document lives should be told where documents live.
        "replace_exact": [
            [
                "def resolve_document_path(filename):",
                "def resolve_document_path(\n"
                "    data_folder,\n"
                "    filename,\n"
                "):",
            ],
        ],
        "rename_names": {"DATA_FOLDER": "data_folder"},
    },
    "rebuild_database": {
        "replace_exact": [
            [
                "def rebuild_database():",
                "def build_index_from_folder(\n"
                "    data_folder,\n"
                "):",
            ],
            # The publish tail is deleted here and re-attached in app.py.
            # It holds the `global` declarations, the save, the swap and
            # the SQLite sync, so removing it is also what stops this
            # module from reading or rebinding any global.
            [
                PUBLISH_TAIL,
                "    return {\n"
                '        "inverted_index": new_inverted_index,\n'
                '        "document_metadata": new_document_metadata,\n'
                '        "filename_index": new_filename_index,\n'
                '        "page_text_index": new_page_text_index,\n'
                "    }\n",
            ],
        ],
        "rename_names": {"DATA_FOLDER": "data_folder"},
    },
}

WRAPPERS = '''
# ============================================================
# REBUILD AND PATH ADAPTERS
# ============================================================
#
# The build now lives in search_engine/indexing.py and returns a
# snapshot. This adapter keeps the ordering, which is the part that
# belongs to the application:
#
#     build -> save JSON snapshot -> publish -> sync SQLite
#
# That order is the one that shipped before the extraction. An earlier
# design note assumed sync-then-publish. The difference matters: a crash
# between the two leaves either a saved snapshot with an old live index,
# or a live index with an unsaved snapshot, and only one of those is
# recoverable.


def rebuild_database():
    """
    Build a complete new index without mutating the active snapshot.

    Searches continue to use the previous complete snapshot until the
    new snapshot has been fully built and persisted. Then all four active
    index dictionaries are swapped together.
    """

    new_snapshot = indexing.build_index_from_folder(
        DATA_FOLDER
    )

    new_inverted_index = new_snapshot["inverted_index"]
    new_document_metadata = new_snapshot["document_metadata"]
    new_filename_index = new_snapshot["filename_index"]
    new_page_text_index = new_snapshot["page_text_index"]

    # Write the full new snapshot first.
    save_database_snapshot(
        new_inverted_index,
        new_document_metadata,
        new_filename_index,
        new_page_text_index,
    )

    # One pointer swap: searches see old or new, never a partial build.
    # The globals are re-pointed inside the same critical section,
    # because execute_search captures all four of them under this lock.
    # Re-pointing them after releasing it would let a reader capture a
    # half-rebound set - one global from the new generation and one from
    # the old.
    with INDEX_DATA_LOCK:
        publish_index_state({
            "inverted_index": new_inverted_index,
            "document_metadata": new_document_metadata,
            "filename_index": new_filename_index,
            "page_text_index": new_page_text_index,
        })

    print(
        "[REBUILD] Active snapshot swapped atomically."
    )

    # Keep SQLite synchronized after an explicit full rebuild.
    with get_sqlite_connection() as connection:
        sync_sqlite_from_memory(connection)

    print()
    print(
        f"[REBUILD] Complete: "
        f"{len(DOCUMENT_METADATA)} documents, "
        f"{len(REAL_INVERTED_INDEX)} content terms, "
        f"{len(FILENAME_INDEX)} filenames indexed, "
        f"{len(PAGE_TEXT_INDEX)} page-text entries"
    )

    print("==============================================")
    print()


def resolve_document_path(filename):
    """
    Thin adapter: the data folder is the application's.

    The guard itself lives beside the code that owns document layout.
    """

    return indexing.resolve_document_path(
        DATA_FOLDER,
        filename,
    )

'''
