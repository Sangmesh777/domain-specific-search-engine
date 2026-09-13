"""
Flask HTTP adapter for the VTU domain-specific search engine.

This module is an ADAPTER, not the search engine. It owns:

    - Flask initialization and CORS
    - path/runtime configuration
    - HTTP request parsing
    - HTTP response serialization
    - routing
    - background rebuild orchestration (threads, INDEXING/READY/ERROR)

The search algorithm, ranking, snippets, index mutation, SQLite
persistence, document extraction and rebuild implementation all live in
the transport-independent `search_engine` package, which is the single
authoritative implementation shared with the offline Android backend.

Nothing in `search_engine/` may import Flask. Nothing in here should
implement domain logic.
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

import os
import threading
import time

from search_engine.config import (
    EngineConfig,
)
from search_engine.engine import (
    SearchEngine,
)
from search_engine.filenames import (
    normalize_requested_filenames,
)
from search_engine.results import (
    DELETE_FAILED,
    DELETE_FILE_NOT_FOUND,
    DELETE_INVALID_PATH,
    DELETE_NOT_INDEXED,
)


# ============================================================
# FLASK SETUP
# ============================================================

app = Flask(__name__)
CORS(app)


# ============================================================
# PATH CONFIGURATION
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_FOLDER = os.path.join(BASE_DIR, "data")

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
# ENGINE
# ============================================================

# One authoritative engine instance.
#
# Everything below this point is HTTP adaptation and runtime
# orchestration. Indexing, ranking, snippets, SQLite persistence,
# extraction and the rebuild implementation all live in the
# transport-independent search_engine package, which the offline Android
# backend uses unchanged.
ENGINE = SearchEngine(
    EngineConfig(
        data_folder=DATA_FOLDER,
        sqlite_db_file=SQLITE_DB_FILE,
        index_file=INDEX_FILE,
        meta_file=META_FILE,
        filename_index_file=FILENAME_INDEX_FILE,
        page_text_file=PAGE_TEXT_FILE,
    )
)


# ============================================================
# BACKGROUND REBUILD RUNTIME
# ============================================================

# Scheduling is a runtime concern, so it stays in the adapter: the engine
# performs a rebuild, this layer decides when one runs, coalesces
# overlapping requests and owns the thread.

INDEX_STATUS_LOCK = threading.Lock()

INDEX_THREAD = None

# If a document changes while a rebuild is already running, remember
# that another rebuild is required immediately after the current one.
INDEX_REBUILD_REQUESTED = False


def rebuild_database_background():

    global INDEX_THREAD
    global INDEX_REBUILD_REQUESTED

    started_at = time.time()

    with INDEX_STATUS_LOCK:
        INDEX_REBUILD_REQUESTED = False

    ENGINE.set_index_status(
        "INDEXING",
        "Rebuilding search index in background...",
        started_at=started_at,
        completed_at=None,
        last_error=None,
    )

    try:
        ENGINE.rebuild()

        completed_at = time.time()

        ENGINE.set_index_status(
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

        ENGINE.set_index_status(
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
# STARTUP REPORT
# ============================================================

STARTUP_COUNTS = ENGINE.counts()

print(
    f"[DATABASE] Data folder: {DATA_FOLDER}"
)

print(
    f"[DATABASE] Documents loaded: "
    f"{STARTUP_COUNTS['documents']}"
)

print(
    f"[DATABASE] Indexed content terms: "
    f"{STARTUP_COUNTS['content_terms']}"
)

print(
    f"[DATABASE] Filenames indexed: "
    f"{STARTUP_COUNTS['filenames_indexed']}"
)

print(
    f"[DATABASE] Page-text entries: "
    f"{STARTUP_COUNTS['page_text_entries']}"
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
    Bulk upload with ONE SQLite transaction for the whole request.

    This route parses multipart and serializes JSON; storing, extracting,
    indexing, committing and status bookkeeping belong to the engine, which
    is the same code an offline Android import runs.
    """

    files = request.files.getlist("file")

    if not files:
        return jsonify({
            "error": "No files found"
        }), 400

    result = ENGINE.import_documents(
        (
            (
                file.filename,
                file.stream,
            )
            for file in files
        )
    )

    counts = ENGINE.counts()

    return jsonify({
        "message": (
            f"Processed {result.uploaded_count} "
            f"file"
            f"{'s' if result.uploaded_count != 1 else ''}: "
            f"{result.created_count} created, "
            f"{result.replaced_count} replaced."
        ),
        "uploaded": result.uploaded,
        "created": result.created,
        "replaced": result.replaced,
        "rejected": result.rejected,
        "failed": result.failed,
        "uploaded_count": result.uploaded_count,
        "created_count": result.created_count,
        "replaced_count": result.replaced_count,
        "rejected_count": result.rejected_count,
        "failed_count": result.failed_count,
        "indexing_started": False,
        "indexing": ENGINE.index_status(),
        "documents": counts["documents"],
        "content_terms": counts["content_terms"],
        "filenames_indexed":
            counts["filenames_indexed"],
        "page_text_entries":
            counts["page_text_entries"],
    }), 200


@app.route(
    "/api/search",
    methods=["GET"]
)
def execute_search():
    """
    HTTP adapter for the core search operation.

    All ranking, snippet, filetype and pagination behavior lives in
    search_engine.search and is shared with the offline Android backend.
    This function only translates HTTP in and HTTP out:

      - query strings become integers, falling back to the documented
        defaults (page 1, limit 10) exactly as before
      - the engine payload is serialized untouched, including the three
        legacy branches that answer with a bare list
    """

    raw_query = request.args.get(
        "q",
        ""
    )

    try:
        requested_page = int(request.args.get("page", "1"))
    except (TypeError, ValueError):
        requested_page = 1

    try:
        requested_limit = int(request.args.get("limit", "10"))
    except (TypeError, ValueError):
        requested_limit = 10

    return jsonify(
        ENGINE.search(
            raw_query,
            requested_page=requested_page,
            requested_limit=requested_limit,
        )
    )


def delete_response(result):
    """
    Render a core DeleteResult as HTTP.

    The outcome vocabulary lives in the engine so the offline Android
    backend can present the same four cases; only this mapping is HTTP.
    """

    if result.outcome == DELETE_INVALID_PATH:
        return jsonify({
            "error":
                "Invalid document path."
        }), 400

    if result.outcome == DELETE_NOT_INDEXED:
        return jsonify({
            "error":
                "Document is not indexed."
        }), 404

    if result.outcome == DELETE_FILE_NOT_FOUND:
        return jsonify({
            "error":
                "Document file not found."
        }), 404

    if result.outcome == DELETE_FAILED:
        return jsonify({
            "error":
                f"Could not delete document: {result.reason}"
        }), 500

    counts = ENGINE.counts()

    return jsonify({
        "message":
            f"Deleted {result.filename} successfully.",
        "deleted":
            result.filename,
        "documents":
            counts["documents"],
        "content_terms":
            counts["content_terms"],
        "filenames_indexed":
            counts["filenames_indexed"],
        "page_text_entries":
            counts["page_text_entries"],
        "indexing_started":
            False,
        "indexing":
            ENGINE.index_status(),
    }), 200


# ============================================================
# DOCUMENT VIEW API
# ============================================================

@app.route(
    "/api/documents/<path:filename>",
    methods=["GET", "DELETE"]
)
def open_document(filename):
    """
    Serve one document, or delete it.

    Deletion is a single engine operation covering the stored file, the
    in-memory index and the SQLite rows, so the offline Android backend
    deletes through exactly the same code path. Path confinement to the data
    folder is enforced by the engine for both verbs.
    """

    if request.method == "DELETE":

        return delete_response(
            ENGINE.delete(filename)
        )

    safe_path = ENGINE.resolve_document_path(
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

    if not ENGINE.has_document(safe_filename):
        return jsonify({
            "error":
                "Document is not indexed."
        }), 404

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

    Request validation happens here; the transaction, the per-document
    accounting and the filesystem removal belong to the engine.
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

    # Normalize before touching the engine so an empty or invalid request
    # cannot bump the indexing generation the way a real batch does.
    requested = normalize_requested_filenames(
        filenames
    )

    if not requested:
        return jsonify({
            "error":
                "No valid filenames supplied."
        }), 400

    result = ENGINE.bulk_delete(requested)

    counts = ENGINE.counts()

    return jsonify({
        "message":
            f"Bulk delete complete: {result.deleted_count} deleted.",
        "deleted": result.deleted,
        "not_found": result.not_found,
        "failed": result.failed,
        "deleted_count": result.deleted_count,
        "not_found_count": result.not_found_count,
        "failed_count": result.failed_count,
        "indexing_started": False,
        "indexing": ENGINE.index_status(),
        "documents": counts["documents"],
        "content_terms": counts["content_terms"],
        "filenames_indexed": counts["filenames_indexed"],
        "page_text_entries": counts["page_text_entries"],
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
            ENGINE.index_status(),

    }), 202


@app.route(
    "/api/status",
    methods=["GET"]
)
def database_status():
    """
    Report corpus counters and indexing state.

    The payload comes straight from the engine, so the offline Android
    status banner and this endpoint can never drift apart.
    """

    return jsonify(
        ENGINE.status()
    )


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
