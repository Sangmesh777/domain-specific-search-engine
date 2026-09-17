from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

import os
import math
import re
import PyPDF2
import docx
from urllib.parse import quote
from werkzeug.utils import secure_filename
import threading
import time


# ============================================================
# FLASK SETUP
# ============================================================

app = Flask(__name__)
CORS(app)


# ============================================================
# PATH CONFIGURATION
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# The corpus location can be redirected so that the engine can be run
# against a throwaway directory. This is used by the hermetic test
# suite and by the golden-vector / corpus-sidecar generators, which
# must never touch the developer's real corpus.
#
# Default behaviour is unchanged: <repo>/data
DATA_FOLDER = os.environ.get(
    "SEARCH_ENGINE_DATA_DIR"
) or os.path.join(BASE_DIR, "data")

INDEX_FILE = os.path.join(DATA_FOLDER, "inverted_index.json")
META_FILE = os.path.join(DATA_FOLDER, "document_meta.json")
FILENAME_INDEX_FILE = os.path.join(
    DATA_FOLDER,
    "filename_index.json"
)
PAGE_TEXT_FILE = os.path.join(
    DATA_FOLDER,
    "page_text.json"
)
SQLITE_DB_FILE = os.path.join(DATA_FOLDER, "search.db")


if not os.path.exists(DATA_FOLDER):
    os.makedirs(DATA_FOLDER)


# ============================================================
# TOKENIZERS
# ============================================================


from search_engine.index_state import (
    IndexState,
    SnapshotIncompleteError,
    plan_remove_document,
    plan_upsert_document,
)
from search_engine.sanitize import sanitize_upload_filename
from search_engine.text import (
    normalize_search_query,
    parse_filetype_filter,
    tokenize,
    tokenize_filename,
)









# ============================================================
# DOCUMENT EXTRACTION
# ============================================================


from search_engine.extract import (
    count_phrase_occurrences,
    extract_pages,
    extract_text,
)
from search_engine.snippet import (
    build_snippet_result,
    get_snippet_and_page,
)







# ============================================================
# PAGE-AWARE SNIPPET
# ============================================================








# ============================================================
# DATABASE VARIABLES
# ============================================================

REAL_INVERTED_INDEX = {}
DOCUMENT_METADATA = {}
FILENAME_INDEX = {}
PAGE_TEXT_INDEX = {}

# ------------------------------------------------------------------
# INDEX STATE OWNERSHIP
# ------------------------------------------------------------------
#
# INDEX_STATE owns the four containers above. The legacy global names
# are kept because the ranking and persistence code still reads them
# directly, but they must always refer to the *same objects* as
# INDEX_STATE's, or a reader could see a mixture of two generations.
#
# The four globals are rebound in exactly five places, and every one
# of them goes through publish_index_state(). That function must be
# called under INDEX_DATA_LOCK; it swaps the containers and re-points
# the globals in the same critical section:
#
#   1. load_database_from_sqlite  (SQLite restore at startup)
#   2. load_database              (JSON snapshot restore at startup)
#   3. rebuild_database           (atomic swap after a full rebuild)
#   4. incrementally_index_document  (single-document upsert)
#   5. incrementally_remove_document (single-document delete)
#
# publish_index_state() is the only thing that calls replace_snapshot,
# and two structural tests in tests/test_memory_mutation_atomicity.py
# enforce both halves of that rule against the source. It matters
# because execute_search captures all four globals under this lock, so a
# sync performed after releasing it would let a reader capture a
# half-rebound set - one global from the new generation, one from the
# old.
#
# assert_index_state_consistent() proves the invariant, and
# tests/test_index_state.py exercises those paths.
#
# MEMORY MUTATION IS COPY-ON-WRITE
# ---------------------------------
# Every in-memory index change now builds replacement containers and
# publishes them in a single swap, through publish_index_state() above.
# Nothing mutates a published container any more, so a search that
# captured its four references under INDEX_DATA_LOCK sees one complete
# generation.
#
# The two mutation helpers that used to write into the live containers
# in place (_remove_document_from_memory and _add_document_to_memory)
# are gone. Their logic lives in search_engine/index_state.py as the
# pure functions plan_remove_document, plan_add_document and
# plan_upsert_document, which build and return replacements and mutate
# nothing.
#
# incrementally_index_document uses plan_upsert_document so that a
# re-upload publishes once rather than twice, and always drops the
# previous revision's terms.
#
# tools/shadow_parity.py proves the resulting state is identical to what
# the original in-place helpers produced, by running the originals from
# a pinned git revision against the same input and comparing all four
# containers.
# ------------------------------------------------------------------

INDEX_STATE = IndexState(
    inverted_index=REAL_INVERTED_INDEX,
    document_metadata=DOCUMENT_METADATA,
    filename_index=FILENAME_INDEX,
    page_text_index=PAGE_TEXT_INDEX,
)


def sync_index_globals_from_state():
    """
    Point the legacy module globals at INDEX_STATE's containers.

    Not called directly any more. Use publish_index_state(), which does
    this inside the same INDEX_DATA_LOCK critical section that swaps the
    state. Calling it on its own leaves a window in which the four
    globals name different generations, and execute_search captures all
    four of them under that lock.
    """

    global REAL_INVERTED_INDEX
    global DOCUMENT_METADATA
    global FILENAME_INDEX
    global PAGE_TEXT_INDEX

    snapshot = INDEX_STATE.snapshot()

    REAL_INVERTED_INDEX = snapshot["inverted_index"]
    DOCUMENT_METADATA = snapshot["document_metadata"]
    FILENAME_INDEX = snapshot["filename_index"]
    PAGE_TEXT_INDEX = snapshot["page_text_index"]


def publish_index_state(replacement):
    """
    Publish a replacement snapshot and re-point the legacy globals.

    MUST be called while holding INDEX_DATA_LOCK. Readers capture all
    four globals under that same lock, so publishing outside it would
    let a reader observe two generations at once, which is exactly the
    incoherence this whole mechanism exists to prevent.
    """

    INDEX_STATE.replace_snapshot(replacement)
    sync_index_globals_from_state()


def assert_index_state_consistent():
    """
    Raise AssertionError unless the legacy globals and INDEX_STATE name
    the same four objects.

    A fast identity check, cheap enough to call from tests after every
    path that mutates the index.
    """

    snapshot = INDEX_STATE.snapshot()

    for name, legacy, owned in (
        ("REAL_INVERTED_INDEX", REAL_INVERTED_INDEX, snapshot["inverted_index"]),
        ("DOCUMENT_METADATA", DOCUMENT_METADATA, snapshot["document_metadata"]),
        ("FILENAME_INDEX", FILENAME_INDEX, snapshot["filename_index"]),
        ("PAGE_TEXT_INDEX", PAGE_TEXT_INDEX, snapshot["page_text_index"]),
    ):
        if legacy is not owned:
            raise AssertionError(
                f"{name} has desynchronised from INDEX_STATE: the module "
                "global and the owned container are different objects. "
                "A rebinding site was added without going through "
                "INDEX_STATE.replace_snapshot + sync_index_globals_from_state."
            )

# ============================================================
# INDEXING STATE
# ============================================================

INDEX_STATUS_LOCK = threading.Lock()
INDEX_DATA_LOCK = threading.Lock()

INDEX_STATUS = {
    "state": "READY",
    "message": "Search index is ready.",
    "started_at": None,
    "completed_at": None,
    "last_error": None,
    "generation": 0,
}

INDEX_THREAD = None


def set_index_status(
    state,
    message,
    started_at=None,
    completed_at=None,
    last_error=None,
):
    with INDEX_STATUS_LOCK:
        INDEX_STATUS["state"] = state
        INDEX_STATUS["message"] = message

        if started_at is not None:
            INDEX_STATUS["started_at"] = started_at

        if completed_at is not None:
            INDEX_STATUS["completed_at"] = completed_at

        INDEX_STATUS["last_error"] = last_error

        if state == "READY":
            INDEX_STATUS["generation"] += 1


def get_index_status():
    with INDEX_STATUS_LOCK:
        return dict(INDEX_STATUS)


# If a document changes while a rebuild is already running, remember
# that another rebuild is required immediately after the current one.
INDEX_REBUILD_REQUESTED = False


def rebuild_database_background():
    global INDEX_THREAD
    global INDEX_REBUILD_REQUESTED

    started_at = time.time()

    with INDEX_STATUS_LOCK:
        INDEX_REBUILD_REQUESTED = False

    set_index_status(
        "INDEXING",
        "Rebuilding search index in background...",
        started_at=started_at,
        completed_at=None,
        last_error=None,
    )

    try:
        rebuild_database()

        completed_at = time.time()

        set_index_status(
            "READY",
            "Search index is ready.",
            completed_at=completed_at,
            last_error=None,
        )

        print(
            "[INDEX] Background rebuild completed "
            f"in {completed_at - started_at:.2f}s"
        )

    except Exception as error:

        completed_at = time.time()

        set_index_status(
            "ERROR",
            "Search index rebuild failed.",
            completed_at=completed_at,
            last_error=str(error),
        )

        print(
            "[INDEX ERROR] Background rebuild failed: "
            f"{error}"
        )

    finally:

        with INDEX_STATUS_LOCK:
            pending_rebuild = INDEX_REBUILD_REQUESTED
            INDEX_REBUILD_REQUESTED = False
            INDEX_THREAD = None

        if pending_rebuild:
            print(
                "[INDEX] Changes arrived during rebuild; "
                "starting follow-up rebuild."
            )

            start_background_rebuild()


def start_background_rebuild():
    global INDEX_THREAD
    global INDEX_REBUILD_REQUESTED

    with INDEX_STATUS_LOCK:

        if (
            INDEX_THREAD is not None
            and INDEX_THREAD.is_alive()
        ):
            INDEX_REBUILD_REQUESTED = True

            return False

        INDEX_REBUILD_REQUESTED = False

        INDEX_THREAD = threading.Thread(
            target=rebuild_database_background,
            name="search-index-rebuild",
            daemon=True,
        )

        INDEX_THREAD.start()

        return True


# ============================================================
# DATABASE LOAD
# ============================================================


# ============================================================
# SQLITE INCREMENTAL INDEX
# ============================================================


# ============================================================
# PERSISTENCE ADAPTERS
# ============================================================
#
# Every function below is a thin adapter over search_engine/storage.py.
# The storage layer owns the SQL, the schema, the JSON files and the
# filesystem paths; this file owns publication and the module-level
# index globals. Nothing here may grow storage logic back into the
# application.

def get_sqlite_connection():
    """
    Thin adapter over storage.get_sqlite_connection.

    Kept so the document-import and bulk-delete paths - which this
    milestone must not touch - keep their existing call sites.
    """

    return storage.get_sqlite_connection(
        SQLITE_DB_FILE
    )


def initialize_sqlite_store():
    """
    Create the SQLite index, migrating once if it is empty.

    Orchestration only: the schema, the migration write and the
    reconstruction all live in search_engine/storage.py. The single
    connection is held here so the schema, the emptiness check and a
    possible migration happen on one connection, exactly as before.
    """

    os.makedirs(DATA_FOLDER, exist_ok=True)

    with get_sqlite_connection() as connection:

        storage.create_schema(
            connection
        )

        # Repair databases written before filename_terms carried an
        # explicit position. The correct token sequence is derivable
        # from the document name, so this needs no document re-reading
        # and is a no-op once the table is correct.
        filename_term_migration = storage.migrate_filename_terms(
            connection,
            tokenize_filename=tokenize_filename,
        )

        if (
            filename_term_migration["schema_changed"]
            or filename_term_migration["repaired"]
            or filename_term_migration["orphans_removed"]
        ):
            print(
                "[DATABASE] filename_terms migrated: "
                f"schema_changed="
                f"{filename_term_migration['schema_changed']}, "
                f"repaired={len(filename_term_migration['repaired'])}, "
                f"orphans_removed="
                f"{len(filename_term_migration['orphans_removed'])}"
            )

        if (
            storage.count_documents(connection) == 0
            and DOCUMENT_METADATA
        ):
            sync_sqlite_from_memory(
                connection
            )

    # SQLite becomes the persistent source of truth for the
    # incremental document index.
    load_database_from_sqlite()


def sync_sqlite_from_memory(connection=None):
    """
    Thin adapter: publish-then-persist bookkeeping stays in app.py.

    The snapshot is read through IndexState rather than off the module
    globals. They name the same objects, but this way the four
    containers provably come from one generation.
    """

    snapshot = INDEX_STATE.snapshot()

    # Named rather than splatted: snapshot() also carries `generation`,
    # which is not part of the persisted state.
    return storage.sync_sqlite_from_memory(
        db_path=SQLITE_DB_FILE,
        connection=connection,
        inverted_index=snapshot["inverted_index"],
        document_metadata=snapshot["document_metadata"],
        filename_index=snapshot["filename_index"],
        page_text_index=snapshot["page_text_index"],
    )


def load_database_from_sqlite():
    """
    Rebuild from SQLite, then publish once under the lock.

    storage returns a complete snapshot and installs nothing, so the
    publication order stays in one place in this file.
    """

    replacement = storage.load_database_from_sqlite(
        db_path=SQLITE_DB_FILE,
    )

    # An empty SQLite store means "leave the active state alone", which
    # is what the original early return did.
    if replacement is None:
        return

    with INDEX_DATA_LOCK:

        publish_index_state(replacement)


def get_document_term_counts_from_sqlite(filename):
    """Thin adapter over storage.get_document_term_counts_from_sqlite."""

    return storage.get_document_term_counts_from_sqlite(
        SQLITE_DB_FILE,
        filename,
    )


def load_database():
    """
    Restore the legacy JSON snapshot files, then publish once.

    storage builds the snapshot from the four files, seeded with the
    current containers so a missing file leaves its container alone.
    """

    with INDEX_DATA_LOCK:

        publish_index_state(
            storage.load_json_snapshot(
                index_file=INDEX_FILE,
                meta_file=META_FILE,
                filename_index_file=FILENAME_INDEX_FILE,
                page_text_file=PAGE_TEXT_FILE,
                current=INDEX_STATE.snapshot(),
            )
        )


def save_database_snapshot(
    inverted_index,
    document_metadata,
    filename_index,
    page_text_index,
):
    """Thin adapter over storage.save_database_snapshot."""

    storage.save_database_snapshot(
        index_file=INDEX_FILE,
        meta_file=META_FILE,
        filename_index_file=FILENAME_INDEX_FILE,
        page_text_file=PAGE_TEXT_FILE,
        inverted_index=inverted_index,
        document_metadata=document_metadata,
        filename_index=filename_index,
        page_text_index=page_text_index,
    )


def save_database():
    """
    Capture one generation under the lock, then write it out.

    No call sites: this was already unreachable before the extraction.
    It is kept because it is part of the persistence API and removing it
    is a separate decision from relocating it.
    """

    with INDEX_DATA_LOCK:

        snapshot = INDEX_STATE.snapshot()

    save_database_snapshot(
        snapshot["inverted_index"],
        snapshot["document_metadata"],
        snapshot["filename_index"],
        snapshot["page_text_index"],
    )


from search_engine import storage













def incrementally_index_document(
    filename,
    file_path,
    connection=None,
    commit=True,
):
    """
    Index exactly one document.

    Complexity is proportional to the document being uploaded,
    not to the number of existing documents.
    """

    # The rule for turning a document file into indexed parts lives in
    # the indexing layer, because the rebuild path uses the same rule.
    # Two copies of it is how an online index and an offline index drift
    # apart.
    extracted = indexing.extract_document(
        file_path,
        filename,
    )

    if extracted is None:
        raise ValueError(
            "Document contains no readable text."
        )

    content_words = extracted["content_words"]
    term_counts = extracted["term_counts"]
    filename_words = extracted["filename_words"]
    pages = extracted["pages"]
    metadata = extracted["metadata"]

    old_term_counts = (
        get_document_term_counts_from_sqlite(
            filename
        )
    )

    # Build the replacement containers without publishing, then swap
    # once. A concurrent search sees the old generation or the new one,
    # never a document present in metadata but missing from the filename
    # index.
    with INDEX_DATA_LOCK:

        publish_index_state(
            plan_upsert_document(
                INDEX_STATE.snapshot(),
                filename,
                metadata,
                filename_words,
                pages,
                content_words,
                old_term_counts,
            )
        )

    # Persist only this document and its postings. The statements live
    # in the storage layer; this function decides when they run and
    # whether it owns the transaction.
    own_connection = connection is None

    if own_connection:
        connection = get_sqlite_connection()

    try:
        storage.replace_document_rows(
            connection,
            filename=filename,
            metadata=metadata,
            term_counts=term_counts,
            filename_words=filename_words,
            pages=pages,
        )

        if commit:
            connection.commit()

    finally:
        if own_connection:
            connection.close()

    return metadata


def incrementally_remove_document(
    filename,
    connection=None,
    commit=True,
):
    """
    Remove exactly one document from memory and SQLite.
    """

    old_term_counts = (
        get_document_term_counts_from_sqlite(
            filename
        )
    )

    with INDEX_DATA_LOCK:

        publish_index_state(
            plan_remove_document(
                INDEX_STATE.snapshot(),
                filename,
                old_term_counts,
            )
        )

    own_connection = connection is None

    if own_connection:
        connection = get_sqlite_connection()

    try:
        storage.delete_document_rows(
            connection,
            filename=filename,
        )

        if commit:
            connection.commit()

    finally:
        if own_connection:
            connection.close()


def queue_index_refresh():
    """
    Compatibility helper.

    Incremental indexing normally makes a background rebuild unnecessary.
    This remains available for an explicit full repair operation.
    """

    return False




# ============================================================
# DATABASE SAVE
# ============================================================







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


from search_engine import indexing

load_database()
initialize_sqlite_store()

set_index_status(
    "READY",
    "Search index is ready.",
    completed_at=time.time(),
    last_error=None,
)

print(
    f"[DATABASE] Data folder: {DATA_FOLDER}"
)

print(
    f"[DATABASE] Documents loaded: "
    f"{len(DOCUMENT_METADATA)}"
)

print(
    f"[DATABASE] Indexed content terms: "
    f"{len(REAL_INVERTED_INDEX)}"
)

print(
    f"[DATABASE] Filenames indexed: "
    f"{len(FILENAME_INDEX)}"
)

print(
    f"[DATABASE] Page-text entries: "
    f"{len(PAGE_TEXT_INDEX)}"
)


# ============================================================
# UPLOAD API
# ============================================================

@app.route(
    "/api/upload",
    methods=["POST"]
)
def upload_file():
    """
    Bulk upload with document-local extraction/indexing and ONE SQLite
    transaction for the whole request.
    """

    files = request.files.getlist("file")

    if not files:
        return jsonify({
            "error": "No files found"
        }), 400

    uploaded_names = []
    created_names = []
    replaced_names = []
    rejected_files = []
    failed_files = []

    connection = get_sqlite_connection()

    try:
        for file in files:

            original_filename = (
                file.filename or ""
            ).strip()

            safe_filename = (
                sanitize_upload_filename(
                    original_filename
                )
            )

            if not safe_filename:

                rejected_files.append({
                    "filename": original_filename,
                    "reason": (
                        "Unsupported or invalid filename. "
                        "Allowed: PDF, DOCX, TXT."
                    ),
                })

                continue

            file_path = os.path.join(
                DATA_FOLDER,
                safe_filename,
            )

            existed_before = os.path.isfile(
                file_path
            )

            temp_path = file_path + ".uploading"

            try:

                file.save(temp_path)

                os.replace(
                    temp_path,
                    file_path,
                )

                incrementally_index_document(
                    safe_filename,
                    file_path,
                    connection=connection,
                    commit=False,
                )

                uploaded_names.append(
                    safe_filename
                )

                if existed_before:

                    replaced_names.append(
                        safe_filename
                    )

                    print(
                        "[UPLOAD] Replaced and "
                        "incrementally indexed: "
                        f"{safe_filename}"
                    )

                else:

                    created_names.append(
                        safe_filename
                    )

                    print(
                        "[UPLOAD] Created and "
                        "incrementally indexed: "
                        f"{safe_filename}"
                    )

            except Exception as error:

                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass

                failed_files.append({
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

    set_index_status(
        "READY",
        "Search index is ready.",
        completed_at=time.time(),
        last_error=None,
    )

    return jsonify({
        "message": (
            f"Processed {len(uploaded_names)} "
            f"file"
            f"{'s' if len(uploaded_names) != 1 else ''}: "
            f"{len(created_names)} created, "
            f"{len(replaced_names)} replaced."
        ),
        "uploaded": uploaded_names,
        "created": created_names,
        "replaced": replaced_names,
        "rejected": rejected_files,
        "failed": failed_files,
        "uploaded_count": len(uploaded_names),
        "created_count": len(created_names),
        "replaced_count": len(replaced_names),
        "rejected_count": len(rejected_files),
        "failed_count": len(failed_files),
        "indexing_started": False,
        "indexing": get_index_status(),
        "documents": len(DOCUMENT_METADATA),
        "content_terms": len(REAL_INVERTED_INDEX),
        "filenames_indexed": len(FILENAME_INDEX),
        "page_text_entries": len(PAGE_TEXT_INDEX),
    }), 200


# ============================================================
# SEARCH ADAPTER
# ============================================================
#
# The ranking pipeline now lives in search_engine/engine.py. What is
# left here is the transport: parse the query string, take one coherent
# snapshot under the lock, call the engine, serialise the result.
#
# Nothing in this function may grow ranking, scoring or filtering
# logic. The offline backend has to run the identical pipeline, so any
# rule written here is a rule the two modes can drift apart on.


@app.route(
    "/api/search",
    methods=["GET"]
)
def execute_search():
    """
    Thin adapter over search_engine.engine.search_index.
    """

    raw_query_text = request.args.get(
        "q",
        ""
    )

    page_text = request.args.get(
        "page",
        "1"
    )

    limit_text = request.args.get(
        "limit",
        "10"
    )

    # The engine receives one coherent generation. Capturing all four
    # under the lock is what stops a request seeing, say, the new
    # metadata with the previous postings.
    with INDEX_DATA_LOCK:
        active_inverted_index = REAL_INVERTED_INDEX
        active_document_metadata = DOCUMENT_METADATA
        active_filename_index = FILENAME_INDEX
        active_page_text_index = PAGE_TEXT_INDEX

    return jsonify(
        search_index(
            raw_query_text,
            page_text,
            limit_text,
            active_inverted_index,
            active_document_metadata,
            active_filename_index,
            active_page_text_index,
        )
    )


from search_engine.engine import search_index







# ============================================================
# DOCUMENT VIEW API
# ============================================================

@app.route(
    "/api/documents/<path:filename>",
    methods=["GET", "DELETE"]
)
def open_document(filename):

    safe_path = resolve_document_path(
        filename
    )

    if safe_path is None:
        return jsonify({
            "error":
                "Invalid document path."
        }), 400

    safe_filename = os.path.basename(
        safe_path
    )

    if safe_filename not in DOCUMENT_METADATA:
        return jsonify({
            "error":
                "Document is not indexed."
        }), 404

    if request.method == "DELETE":

        if not os.path.isfile(
            safe_path
        ):
            return jsonify({
                "error":
                    "Document file not found."
            }), 404

        try:

            os.remove(
                safe_path
            )

            incrementally_remove_document(
                safe_filename
            )

        except Exception as error:

            return jsonify({
                "error":
                    f"Could not delete document: {error}"
            }), 500

        set_index_status(
            "READY",
            "Search index is ready.",
            completed_at=time.time(),
            last_error=None,
        )

        print(
            "[DELETE] Removed and "
            "incrementally unindexed: "
            f"{safe_filename}"
        )

        return jsonify({
            "message":
                f"Deleted {safe_filename} successfully.",
            "deleted":
                safe_filename,
            "documents":
                len(DOCUMENT_METADATA),
            "content_terms":
                len(REAL_INVERTED_INDEX),
            "filenames_indexed":
                len(FILENAME_INDEX),
            "page_text_entries":
                len(PAGE_TEXT_INDEX),
            "indexing_started":
                False,
            "indexing":
                get_index_status(),
        }), 200

    return send_from_directory(
        DATA_FOLDER,
        safe_filename,
        as_attachment=False
    )


@app.route(
    "/api/documents/bulk-delete",
    methods=["POST"]
)
def bulk_delete_documents():
    """
    Bulk delete multiple documents in one SQLite transaction.

    JSON body:
        {"filenames": ["one.txt", "two.pdf"]}
    """

    payload = request.get_json(
        silent=True
    ) or {}

    filenames = payload.get(
        "filenames"
    )

    if not isinstance(
        filenames,
        list
    ):
        return jsonify({
            "error":
                "'filenames' must be a JSON array."
        }), 400

    normalized_filenames = []

    for filename in filenames:

        if not isinstance(
            filename,
            str
        ):
            continue

        safe_filename = os.path.basename(
            filename.strip()
        )

        if (
            safe_filename
            and safe_filename not in normalized_filenames
        ):
            normalized_filenames.append(
                safe_filename
            )

    if not normalized_filenames:
        return jsonify({
            "error":
                "No valid filenames supplied."
        }), 400

    deleted = []
    not_found = []
    failed = []

    connection = get_sqlite_connection()

    try:

        for filename in normalized_filenames:

            file_path = os.path.join(
                DATA_FOLDER,
                filename
            )

            try:

                indexed_exists = (
                    filename in DOCUMENT_METADATA
                )

                filesystem_exists = os.path.isfile(
                    file_path
                )

                if (
                    not indexed_exists
                    and not filesystem_exists
                ):
                    not_found.append(
                        filename
                    )
                    continue

                incrementally_remove_document(
                    filename,
                    connection=connection,
                    commit=False,
                )

                if filesystem_exists:
                    os.remove(file_path)

                deleted.append(
                    filename
                )

                print(
                    "[BULK DELETE] Removed and "
                    "incrementally unindexed: "
                    f"{filename}"
                )

            except Exception as error:

                failed.append({
                    "filename": filename,
                    "reason": str(error),
                })

        connection.commit()

    except Exception:

        connection.rollback()
        raise

    finally:

        connection.close()

    set_index_status(
        "READY",
        "Search index is ready.",
        completed_at=time.time(),
        last_error=None,
    )

    return jsonify({
        "message":
            f"Bulk delete complete: {len(deleted)} deleted.",
        "deleted": deleted,
        "not_found": not_found,
        "failed": failed,
        "deleted_count": len(deleted),
        "not_found_count": len(not_found),
        "failed_count": len(failed),
        "indexing_started": False,
        "indexing": get_index_status(),
        "documents": len(DOCUMENT_METADATA),
        "content_terms": len(REAL_INVERTED_INDEX),
        "filenames_indexed": len(FILENAME_INDEX),
        "page_text_entries": len(PAGE_TEXT_INDEX),
    }), 200


@app.route(
    "/api/rebuild",
    methods=["POST"]
)
def rebuild_api():

    indexing_started = start_background_rebuild()

    return jsonify({

        "message":
            (
                "Search database rebuild started."
                if indexing_started
                else
                "Search database rebuild is already running; "
                "new filesystem changes will be picked up automatically."
            ),

        "indexing_started":
            indexing_started,

        "indexing":
            get_index_status(),

    }), 202


@app.route(
    "/api/status",
    methods=["GET"]
)
def database_status():

    return jsonify({

        "documents":
            len(DOCUMENT_METADATA),

        "content_terms":
            len(REAL_INVERTED_INDEX),

        "filenames_indexed":
            len(FILENAME_INDEX),

        "page_text_entries":
            len(PAGE_TEXT_INDEX),

        "data_folder":
            DATA_FOLDER,

        "indexing":
            get_index_status()

    })


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    print(
        "Starting Final VTU Engine "
        "on port 5000..."
    )

    app.run(
        port=5000,
        debug=True
    )
