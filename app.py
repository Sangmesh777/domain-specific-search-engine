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
import math
from urllib.parse import quote
import threading
import time

from search_engine.config import (
    EngineConfig,
)
from search_engine.engine import (
    SearchEngine,
)
from search_engine.filenames import (
    sanitize_upload_filename,
)
from search_engine.pagination import (
    empty_query_response,
    paginate,
)
from search_engine.query import (
    normalize_search_query,
    parse_filetype_filter,
)
from search_engine.snippets import (
    count_phrase_occurrences,
    get_snippet_and_page,
)
from search_engine.tokenizer import (
    tokenize_filename,
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

    connection = ENGINE.store.connection()

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

                ENGINE.index_document(
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

    ENGINE.mark_ready()

    counts = ENGINE.counts()

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
        "indexing": ENGINE.index_status(),
        "documents": counts["documents"],
        "content_terms": counts["content_terms"],
        "filenames_indexed":
            counts["filenames_indexed"],
        "page_text_entries":
            counts["page_text_entries"],
    }), 200


def build_paginated_response(
    results,
    page,
    limit,
):
    """
    Serialize an already-ranked result list as an HTTP response.

    Pagination itself lives in the core; this adapter only turns the
    core payload into JSON.
    """

    return jsonify(
        paginate(
            results,
            page,
            limit,
        )
    )


@app.route(
    "/api/search",
    methods=["GET"]
)
def execute_search():

    raw_query = request.args.get(
        "q",
        ""
    ).strip()

    if not raw_query:
        return jsonify(
            empty_query_response()
        )

    try:
        requested_page = int(request.args.get("page", "1"))
    except (TypeError, ValueError):
        requested_page = 1

    try:
        requested_limit = int(request.args.get("limit", "10"))
    except (TypeError, ValueError):
        requested_limit = 10

    requested_result_page = max(
        1,
        requested_page
    )

    limit = min(
        50,
        max(1, requested_limit)
    )

    # Capture one coherent active snapshot for this request.
    active_snapshot = ENGINE.snapshot()

    active_inverted_index = active_snapshot.inverted_index
    active_document_metadata = (
        active_snapshot.document_metadata
    )
    active_filename_index = active_snapshot.filename_index
    active_page_text_index = (
        active_snapshot.page_text_index
    )

    # Normalize document extensions before tokenization.
    # Examples:
    #   BCS502 Module 2.pdf
    #   "BCS502 Module 2.pdf"
    # become:
    #   BCS502 Module 2
    #   "BCS502 Module 2"
    keyword_query, filetype_filter = parse_filetype_filter(
        raw_query
    )

    normalized_query_text = normalize_search_query(
        keyword_query
    )

    query = normalized_query_text.lower()

    quoted_phrase = None

    if (
        len(query) >= 2
        and query.startswith('"')
        and query.endswith('"')
    ):

        quoted_phrase = query[
            1:-1
        ].strip()

    search_words = tokenize_filename(
        query
    )

    if (
        not search_words
        and
        not filetype_filter
    ):
        return build_paginated_response(
            [],
            requested_result_page,
            limit
        )

    # Keep numeric tokens searchable. A query such as "999" must be
    # able to match an indexed token containing 999. The tokenizer already
    # preserves numeric tokens; removing them here made them unreachable
    # from the content index.
    content_query_words = [
        word
        for word in search_words
        if len(word) > 1
    ]

    # --------------------------------------------------------
    # FILE-TYPE-ONLY QUERY
    # --------------------------------------------------------
    #
    # A query such as "pdf" is a filter/browse operation.
    # It must never enter the normal keyword ranking pipeline.
    # --------------------------------------------------------

    if filetype_filter and not search_words:

        filtered_results = []

        for filename, metadata in active_document_metadata.items():

            extension = os.path.splitext(
                filename
            )[1].lower().lstrip(".")

            if extension != filetype_filter:
                continue

            document_url = (
                "/api/documents/"
                +
                quote(
                    filename,
                    safe=""
                )
            )

            filtered_results.append({

                "title":
                    metadata["title"],

                "path":
                    metadata["path"],

                "document_url":
                    document_url,

                "page_url":
                    document_url,

                "open_url":
                    document_url,

                "snippet":
                    (
                        f"Filtered by file type: "
                        f"{filetype_filter.upper()}"
                    ),

                "page":
                    None,

                "highlights":
                    [],

                "filename_score":
                    0.0,

                "content_score":
                    0.0,

                "phrase_score":
                    0.0,

                "phrase_occurrences":
                    0,

                "score":
                    1.0,

                "relevance_score":
                    1.0,

                "match_type":
                    (
                        f"File Type: "
                        f"{filetype_filter.upper()}"
                    ),

                "filetype_filter":
                    filetype_filter,

                "tag":
                    "Filtered Result"

            })

        filtered_results.sort(
            key=lambda item:
                item["title"].lower()
        )

        return build_paginated_response(
            filtered_results,
            requested_result_page,
            limit
        )

    normalized_query = " ".join(
        search_words
    )

    if quoted_phrase:

        phrase_words = tokenize_filename(
            quoted_phrase
        )

    else:

        phrase_words = list(
            search_words
        )

    normalized_phrase = " ".join(
        phrase_words
    )

    phrase_query = (
        len(phrase_words) >= 2
    )

    if not search_words:
        phrase_query = False

    if filetype_filter:

        total_documents = sum(
            1
            for filename in active_document_metadata
            if os.path.splitext(filename)[1]
            .lower()
            .lstrip(".")
            == filetype_filter
        )

    else:

        total_documents = len(
            active_document_metadata
        )

    if total_documents == 0:
        return jsonify([])

    # --------------------------------------------------------
    # SCORE STORAGE
    # --------------------------------------------------------

    document_scores = {}

    for filename in active_document_metadata:

        document_scores[filename] = {

            "filename_score": 0.0,

            "content_score": 0.0,

            "phrase_score": 0.0,

            "phrase_occurrences": 0,

            "filename_matches": [],

            "content_matches": [],

            "phrase_match": False,

            # Explicit content-match hierarchy.
            "exact_content_match": False,

            "best_prefix_similarity": 0.0,

            "best_numeric_similarity": 0.0

        }

    def filetype_allowed(filename):

        if not filetype_filter:
            return True

        extension = os.path.splitext(
            filename
        )[1].lower().lstrip(".")

        return extension == filetype_filter

    # --------------------------------------------------------
    # FILENAME SEARCH
    # --------------------------------------------------------

    for filename, filename_words in (
        active_filename_index.items()
    ):

        if not filetype_allowed(filename):
            continue

        normalized_filename = " ".join(
            filename_words
        )

        if (
            normalized_query
            ==
            normalized_filename
        ):

            document_scores[
                filename
            ]["filename_score"] += 100.0

        elif (
            len(search_words) >= 2
            and
            normalized_query
            in normalized_filename
        ):

            document_scores[
                filename
            ]["filename_score"] += 50.0

        if (
            quoted_phrase
            and
            normalized_phrase
            and
            normalized_phrase
            in normalized_filename
        ):

            document_scores[
                filename
            ]["phrase_score"] += 30.0

            document_scores[
                filename
            ]["phrase_match"] = True

        for word in search_words:

            if word not in filename_words:
                continue

            if word.isdigit():

                non_numeric_query_words = [
                    query_word
                    for query_word in search_words
                    if not query_word.isdigit()
                ]

                has_related_filename_word = any(
                    query_word in filename_words
                    for query_word
                    in non_numeric_query_words
                )

                if (
                    non_numeric_query_words
                    and
                    not has_related_filename_word
                ):

                    continue

            document_scores[
                filename
            ]["filename_score"] += 20.0

            if word not in document_scores[
                filename
            ]["filename_matches"]:

                document_scores[
                    filename
                ]["filename_matches"].append(
                    word
                )

    # --------------------------------------------------------
    # CONTENT TF-IDF
    # --------------------------------------------------------

    # Content matching levels:
    #
    # 1. Exact token:
    #       999 -> 999
    #
    # 2. Prefix token:
    #       replacementuni -> replacementunique999
    #
    # 3. Numeric substring:
    #       999 -> replacementunique999
    #
    # Numeric substring matching is deliberately explicit because users
    # commonly search for a numeric fragment embedded in an alphanumeric
    # identifier/code. Exact matches remain strongest, prefix matches are
    # next, and numeric-substring matches receive a lower lexical weight.

    indexed_terms = list(
        active_inverted_index.keys()
    )

    for word in content_query_words:

        matching_terms = []

        # ----------------------------------------------------
        # EXACT TOKEN
        # ----------------------------------------------------
        if word in active_inverted_index:

            matching_terms.append(
                (
                    word,
                    1.0
                )
            )

        # ----------------------------------------------------
        # PREFIX TOKEN
        # ----------------------------------------------------
        if len(word) >= 3:

            for term in indexed_terms:

                if term == word:
                    continue

                if not term.startswith(word):
                    continue

                if not term:
                    continue

                prefix_similarity = (
                    len(word)
                    /
                    float(len(term))
                )

                prefix_similarity = min(
                    0.90,
                    max(
                        0.25,
                        prefix_similarity
                    )
                )

                matching_terms.append(
                    (
                        term,
                        prefix_similarity
                    )
                )

        # ----------------------------------------------------
        # NUMERIC SUBSTRING
        # ----------------------------------------------------
        if word.isdigit() and len(word) >= 2:

            for term in indexed_terms:

                if term == word:
                    continue

                if word not in term:
                    continue

                # Prefix matches already have a stronger lexical
                # interpretation, so do not add a weaker duplicate.
                if term.startswith(word):
                    continue

                substring_similarity = (
                    len(word)
                    /
                    float(len(term))
                )

                substring_similarity = min(
                    0.65,
                    max(
                        0.20,
                        substring_similarity * 0.70
                    )
                )

                matching_terms.append(
                    (
                        term,
                        substring_similarity
                    )
                )

        # Keep the strongest matching interpretation for each indexed term.
        best_term_weights = {}

        for (
            term,
            match_weight
        ) in matching_terms:

            current_weight = (
                best_term_weights.get(
                    term,
                    0.0
                )
            )

            if match_weight > current_weight:

                best_term_weights[
                    term
                ] = match_weight

        for (
            term,
            match_weight
        ) in best_term_weights.items():

            docs_containing_word = len(
                active_inverted_index[term]
            )

            if docs_containing_word == 0:
                continue

            idf = math.log(
                (total_documents + 1)
                /
                (docs_containing_word + 1)
            ) + 1

            for (
                filename,
                term_count
            ) in active_inverted_index[term].items():

                if not filetype_allowed(filename):
                    continue

                if filename not in active_document_metadata:
                    continue

                total_words = active_document_metadata[
                    filename
                ]["total_words"]

                if total_words == 0:
                    continue

                tf = (
                    term_count
                    /
                    float(total_words)
                )

                tfidf = (
                    tf
                    *
                    idf
                    *
                    match_weight
                )

                document_scores[
                    filename
                ]["content_score"] += tfidf

                # --------------------------------------------
                # Explicit lexical match hierarchy
                # --------------------------------------------
                #
                # Exact indexed token:
                #     999 -> 999
                #
                # Prefix:
                #     replacementuni -> replacementunique999
                #
                # Numeric substring:
                #     999 -> replacementunique999
                #
                # Keep these signals separate from TF-IDF so the
                # final ranking does not lose lexical intent merely
                # because one document contains a term frequently.
                if term == word:
                    document_scores[
                        filename
                    ]["exact_content_match"] = True

                elif word.isdigit() and word in term:
                    document_scores[
                        filename
                    ]["best_numeric_similarity"] = max(
                        document_scores[
                            filename
                        ]["best_numeric_similarity"],
                        match_weight
                    )

                elif term.startswith(word):
                    document_scores[
                        filename
                    ]["best_prefix_similarity"] = max(
                        document_scores[
                            filename
                        ]["best_prefix_similarity"],
                        match_weight
                    )

                if word not in document_scores[
                    filename
                ]["content_matches"]:

                    document_scores[
                        filename
                    ]["content_matches"].append(
                        word
                    )

# --------------------------------------------------------
    # PHRASE SEARCH
    # --------------------------------------------------------

    if phrase_query:

        for filename in active_document_metadata:

            if not filetype_allowed(filename):
                continue

            pages = active_page_text_index.get(
                filename,
                []
            )

            normalized_page_text = " ".join(
                page["text"]
                for page in pages
            ).lower()

            # ------------------------------------------------
            # Count exact phrase occurrences in document text.
            # Repeated exact phrases receive increasing but
            # capped phrase relevance.
            # ------------------------------------------------

            phrase_occurrences = count_phrase_occurrences(
                normalized_page_text,
                normalized_phrase
            )

            if phrase_occurrences > 0:

                document_scores[
                    filename
                ]["phrase_occurrences"] = (
                    phrase_occurrences
                )

                # Exact phrase frequency uses a logarithmic
                # curve. More exact occurrences increase
                # relevance, but with diminishing returns.
                #
                # Reference points:
                #   1 occurrence  -> 50.0
                #   2 occurrences -> ~57.5
                #   7 occurrences -> ~80.4
                #   16 occurrences -> ~93.1
                #   25 occurrences -> 100.0
                #
                # This is deliberately capped at 100 so phrase
                # repetition cannot overwhelm the other signals.

                max_frequency_reference = 25.0

                if phrase_occurrences <= 1:

                    content_phrase_score = 50.0

                else:

                    frequency_ratio = min(
                        math.log(
                            phrase_occurrences
                        )
                        /
                        math.log(
                            max_frequency_reference
                        ),
                        1.0
                    )

                    content_phrase_score = (
                        50.0
                        +
                        (
                            50.0
                            *
                            frequency_ratio
                        )
                    )

                content_phrase_score = min(
                    content_phrase_score,
                    100.0
                )

                document_scores[
                    filename
                ]["phrase_score"] = max(
                    document_scores[
                        filename
                    ]["phrase_score"],
                    content_phrase_score
                )

                document_scores[
                    filename
                ]["phrase_match"] = True

            filename_words = active_filename_index.get(
                filename,
                []
            )

            normalized_filename = " ".join(
                filename_words
            )

            # Exact phrase inside the filename is a separate,
            # stronger filename signal.
            if (
                normalized_phrase
                and
                normalized_phrase
                in normalized_filename
            ):

                document_scores[
                    filename
                ]["phrase_score"] += 100.0

                document_scores[
                    filename
                ]["phrase_match"] = True

    # --------------------------------------------------------
    # Explicit quoted phrase filter
    # --------------------------------------------------------

    if quoted_phrase:

        phrase_documents = {
            filename
            for filename, scores
            in document_scores.items()
            if scores["phrase_match"]
        }

        if phrase_documents:

            for filename in list(
                document_scores.keys()
            ):

                if filename not in phrase_documents:

                    document_scores.pop(
                        filename
                    )

        else:

            return jsonify([])

    # --------------------------------------------------------
    # Strongest content score
    # --------------------------------------------------------

    max_content_score = max(

        (
            scores["content_score"]
            for scores
            in document_scores.values()
        ),

        default=0.0
    )

    query_has_filename_signal = any(

        scores["filename_score"] > 0

        for scores
        in document_scores.values()

    )

    # --------------------------------------------------------
    # FINAL RANKING
    # --------------------------------------------------------

    if filetype_filter:

        document_scores = {
            filename: scores
            for filename, scores
            in document_scores.items()
            if filetype_allowed(filename)
        }

        if not document_scores:
            return jsonify([])

        max_content_score = max(
            (
                scores["content_score"]
                for scores
                in document_scores.values()
            ),
            default=0.0
        )

    ranked_documents = []

    for filename, scores in (
        document_scores.items()
    ):

        filename_score = scores[
            "filename_score"
        ]

        content_score = scores[
            "content_score"
        ]

        phrase_score = scores[
            "phrase_score"
        ]

        exact_content_match = bool(
            scores.get(
                "exact_content_match",
                False
            )
        )

        best_prefix_similarity = float(
            scores.get(
                "best_prefix_similarity",
                0.0
            )
        )

        best_numeric_similarity = float(
            scores.get(
                "best_numeric_similarity",
                0.0
            )
        )

        # Explicit lexical hierarchy:
        #
        # 1. exact content token
        # 2. strongest prefix completion
        # 3. numeric substring
        # 4. general TF-IDF
        #
        # The lexical signal is deliberately bounded so it improves
        # ranking without allowing a weak lexical match to overwhelm
        # filename/phrase relevance.
        if exact_content_match:
            lexical_match_relevance = 1.00
        elif best_prefix_similarity > 0:
            lexical_match_relevance = (
                0.70
                +
                (0.30 * best_prefix_similarity)
            )
        elif best_numeric_similarity > 0:
            lexical_match_relevance = (
                0.30
                +
                (0.40 * best_numeric_similarity)
            )
        else:
            lexical_match_relevance = 0.0

        filename_words = active_filename_index.get(
            filename,
            []
        )

        normalized_filename = " ".join(
            filename_words
        )

        matched_word_count = sum(
            1
            for word in search_words
            if word in filename_words
        )

        if search_words:

            query_word_coverage = (
                matched_word_count
                /
                len(search_words)
            )

        else:

            query_word_coverage = 0.0

        if (
            normalized_query
            ==
            normalized_filename
        ):

            filename_relevance = 1.0

        elif (
            len(search_words) >= 2
            and
            normalized_query
            in normalized_filename
        ):

            filename_relevance = 0.95

        elif query_word_coverage > 0:

            filename_relevance = (
                0.75
                *
                query_word_coverage
            )

        else:

            filename_relevance = 0.0

        if max_content_score > 0:

            content_relevance = (
                content_score
                /
                max_content_score
            )

        else:

            content_relevance = 0.0

        phrase_relevance = min(
            phrase_score / 100.0,
            1.0
        )

        if (
            filetype_filter
            and
            not search_words
        ):

            # Type-only query: this is a filtered document list.
            final_score = 0.75

        else:

            if quoted_phrase and query_has_filename_signal:

                # Exact quoted document-title search.
                # All weights sum to 1.00.
                filename_weight = 0.80
                content_weight = 0.05
                phrase_weight = 0.15
                lexical_weight = 0.0

            elif quoted_phrase:

                # Exact quoted content phrase.
                # All weights sum to 1.00.
                filename_weight = 0.05
                content_weight = 0.15
                phrase_weight = 0.80
                lexical_weight = 0.0

            elif query_has_filename_signal:

                # Filename/title-oriented search.
                # All weights sum to 1.00.
                filename_weight = 0.75
                content_weight = 0.15
                phrase_weight = 0.10
                lexical_weight = 0.0

            else:

                # Normal topic/content search.
                # Preserve the original ranking signals while giving
                # explicit lexical intent its own bounded contribution.
                filename_weight = 0.05
                content_weight = 0.60
                phrase_weight = 0.20
                lexical_weight = 0.15

            final_score = (

                (
                    filename_relevance
                    *
                    filename_weight
                )

                +

                (
                    content_relevance
                    *
                    content_weight
                )

                +

                (
                    phrase_relevance
                    *
                    phrase_weight
                )

                +

                (
                    lexical_match_relevance
                    *
                    lexical_weight
                )
            )

        if (
            filename_score == 0
            and
            content_score == 0
            and
            phrase_score == 0
        ):
            continue

        if final_score <= 0:
            continue

        ranked_documents.append({

            "filename":
                filename,

            "filename_score":
                filename_score,

            "content_score":
                content_score,

            "phrase_score":
                phrase_score,

            "phrase_occurrences":
                scores[
                    "phrase_occurrences"
                ],

            "final_score":
                final_score,

            "filename_matches":
                scores[
                    "filename_matches"
                ],

            "content_matches":
                scores[
                    "content_matches"
                ],

            "phrase_match":
                scores[
                    "phrase_match"
                ],

            "exact_content_match":
                exact_content_match,

            "prefix_similarity":
                best_prefix_similarity,

            "numeric_similarity":
                best_numeric_similarity,

            "lexical_match_relevance":
                lexical_match_relevance

        })

    if (
        filetype_filter
        and
        not search_words
    ):

        ranked_documents.sort(
            key=lambda item:
                item["filename"].lower()
        )

    else:

        ranked_documents.sort(
            key=lambda item:
                item["final_score"],
            reverse=True
        )

    # --------------------------------------------------------
    # BUILD RESPONSE
    # --------------------------------------------------------

    results = []

    for item in ranked_documents:

        filename = item[
            "filename"
        ]

        metadata = active_document_metadata[
            filename
        ]

        filename_matches = item[
            "filename_matches"
        ]

        content_matches = item[
            "content_matches"
        ]

        phrase_match = item[
            "phrase_match"
        ]

        if (
            filetype_filter
            and
            not search_words
        ):

            match_type = (
                f"File Type: "
                f"{filetype_filter.upper()}"
            )

        elif (
            filename_matches
            and
            phrase_match
        ):

            match_type = (
                "Filename + Phrase"
            )

        elif phrase_match:

            match_type = "Phrase Match"

        elif (
            filename_matches
            and
            content_matches
        ):

            match_type = (
                "Filename + Content"
            )

        elif filename_matches:

            match_type = "Filename Match"

        else:

            match_type = "Content Match"

        if (
            filetype_filter
            and
            not search_words
        ):

            snippet = (
                f"Filtered by file type: "
                f"{filetype_filter.upper()}"
            )

            document_page = None
            highlights = []

        elif content_matches:

            snippet_words = (
                phrase_words
                if phrase_match
                else content_matches
            )

            snippet_info = get_snippet_and_page(
                active_page_text_index,
                filename,
                snippet_words
            )

            snippet = snippet_info[
                "snippet"
            ]

            document_page = snippet_info[
                "page"
            ]

            highlights = snippet_info[
                "highlights"
            ]

        else:

            snippet = (
                "Filename matched: "
                +
                ", ".join(
                    filename_matches
                )
            )

            document_page = None
            highlights = []

        document_url = (
            "/api/documents/"
            + quote(
                filename,
                safe=""
            )
        )

        if document_page is not None:
            page_url = (
                document_url
                + f"#page={document_page}"
            )
        else:
            page_url = document_url

        results.append({

            "title":
                metadata["title"],

            "path":
                metadata["path"],

            "document_url":
                document_url,

            "page_url":
                page_url,

            "snippet":
                snippet,

            "page":
                document_page,

            "highlights":
                highlights,

            "phrase_occurrences":
                item["phrase_occurrences"],

            "filename_score":
                round(
                    item["filename_score"],
                    4
                ),

            "content_score":
                round(
                    item["content_score"],
                    4
                ),

            "exact_content_match":
                item.get(
                    "exact_content_match",
                    False
                ),

            "prefix_similarity":
                round(
                    item.get(
                        "prefix_similarity",
                        0.0
                    ),
                    4
                ),

            "numeric_similarity":
                round(
                    item.get(
                        "numeric_similarity",
                        0.0
                    ),
                    4
                ),

            "lexical_match_relevance":
                round(
                    item.get(
                        "lexical_match_relevance",
                        0.0
                    ),
                    4
                ),

            "phrase_score":
                round(
                    item["phrase_score"],
                    4
                ),

            "score":
                round(
                    item["final_score"],
                    4
                ),

            "match_type":
                match_type,

            "filetype_filter":
                filetype_filter,

            "tag":
                "Ranked Result",

            "open_url":
                page_url

        })

    return build_paginated_response(
        results,
        requested_result_page,
        limit
    )


def resolve_document_path(filename):
    """
    Resolve an indexed document to a real path inside DATA_FOLDER.

    Prevents GET/DELETE document paths from escaping DATA_FOLDER.
    """

    if not filename:
        return None

    data_root = os.path.realpath(DATA_FOLDER)

    requested_path = os.path.realpath(
        os.path.join(
            DATA_FOLDER,
            os.path.basename(filename)
        )
    )

    try:
        if os.path.commonpath(
            [data_root, requested_path]
        ) != data_root:
            return None
    except ValueError:
        return None

    return requested_path


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

    if not ENGINE.has_document(safe_filename):
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

            ENGINE.remove_document(
                safe_filename
            )

        except Exception as error:

            return jsonify({
                "error":
                    f"Could not delete document: {error}"
            }), 500

        ENGINE.mark_ready()

        print(
            "[DELETE] Removed and "
            "incrementally unindexed: "
            f"{safe_filename}"
        )

        counts = ENGINE.counts()

        return jsonify({
            "message":
                f"Deleted {safe_filename} successfully.",
            "deleted":
                safe_filename,
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

    connection = ENGINE.store.connection()

    try:

        for filename in normalized_filenames:

            file_path = os.path.join(
                DATA_FOLDER,
                filename
            )

            try:

                indexed_exists = ENGINE.has_document(
                    filename
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

                ENGINE.remove_document(
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

    ENGINE.mark_ready()

    counts = ENGINE.counts()

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
