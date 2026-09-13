from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

import os
import json
import sqlite3
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
# TOKENIZERS
# ============================================================

def tokenize(text):
    """Tokenize normal document content."""

    text = text.lower()

    cleaned_text = "".join(
        character
        if character.isalnum() or character.isspace()
        else " "
        for character in text
    )

    return [
        word
        for word in cleaned_text.split()
        if len(word) > 1
    ]


def tokenize_filename(text):
    """
    Tokenize filenames and search queries while preserving
    numeric tokens such as 1, 2, and 3.
    """

    text = text.lower()

    cleaned_text = "".join(
        character
        if character.isalnum() or character.isspace()
        else " "
        for character in text
    )

    return [
        word
        for word in cleaned_text.split()
        if word
    ]


def normalize_search_query(query):
    """
    Normalize a search query before filename/content matching.

    A supported document extension at the end of the query is
    removed so that:

        BCS502 Module 2.pdf
        BCS502 Module 2

    are treated as the same filename search.

    Supported extensions:
        .pdf
        .docx
        .txt
    """

    query = query.strip()

    if not query:
        return ""

    # Handle a quoted query such as:
    # "BCS502 Module 2.pdf"
    quote_wrapped = (
        len(query) >= 2
        and query.startswith('"')
        and query.endswith('"')
    )

    if quote_wrapped:
        query = query[1:-1].strip()

    lower_query = query.lower()

    supported_extensions = (
        ".pdf",
        ".docx",
        ".txt"
    )

    for extension in supported_extensions:

        if lower_query.endswith(extension):

            query = query[
                :-len(extension)
            ].rstrip()

            break

    if quote_wrapped:
        return f'"{query}"'

    return query


def parse_filetype_filter(query):
    """
    Extract an optional document-type filter from a raw search query.

    Supported forms:
        pdf
        .pdf
        network pdf
        network .pdf
        BCS502 Module 2.pdf
        "BCS502 Module 2.pdf"

    Returns:
        (remaining_keyword_query, filetype_filter)
    """

    query = query.strip()

    supported_extensions = (
        (".pdf", "pdf"),
        (".docx", "docx"),
        (".txt", "txt"),
    )

    # Handle an extension attached to the final filename token,
    # including a quoted filename.
    for extension, filetype in supported_extensions:

        quoted_suffix = f'{extension}"'

        if query.lower().endswith(
            quoted_suffix
        ):
            return (
                query[
                    :-len(quoted_suffix)
                ].rstrip() + '"'
                if query[
                    :-len(quoted_suffix)
                ].rstrip()
                else "",
                filetype,
            )

        if query.lower().endswith(
            extension
        ):

            return (
                query[
                    :-len(extension)
                ].rstrip(),
                filetype,
            )

    # Handle standalone type tokens, e.g.:
    #   network pdf
    #   customer .docx
    tokens = query.split()

    if not tokens:
        return query, None

    supported_types = {
        "pdf": "pdf",
        ".pdf": "pdf",
        "docx": "docx",
        ".docx": "docx",
        "txt": "txt",
        ".txt": "txt",
    }

    detected_type = None
    remaining_tokens = []

    for token in tokens:

        normalized = token.lower().strip()

        if normalized in supported_types:

            if detected_type is None:
                detected_type = supported_types[
                    normalized
                ]

            continue

        remaining_tokens.append(token)

    return (
        " ".join(remaining_tokens).strip(),
        detected_type,
    )


# ============================================================
# DOCUMENT EXTRACTION
# ============================================================

def extract_text(file_path, filename):
    """Extract complete text from PDF, DOCX, or TXT."""

    text = ""

    try:
        filename_lower = filename.lower()

        if filename_lower.endswith(".pdf"):

            with open(file_path, "rb") as file:
                reader = PyPDF2.PdfReader(file)

                for page in reader.pages:

                    extracted = page.extract_text()

                    if extracted:
                        text += extracted + " "

        elif filename_lower.endswith(".docx"):

            document = docx.Document(file_path)

            for paragraph in document.paragraphs:
                text += paragraph.text + " "

        elif filename_lower.endswith(".txt"):

            with open(
                file_path,
                "r",
                encoding="utf-8"
            ) as file:
                text = file.read()

    except Exception as error:

        print(
            f"[ERROR] Could not read {filename}: {error}"
        )

    return text.lower()


def extract_pages(file_path, filename):
    """
    Extract page-level text.

    PDFs:
        returns [{"page": 1, "text": "..."}]

    DOCX/TXT:
        treated as one logical page.
    """

    pages = []

    try:

        filename_lower = filename.lower()

        if filename_lower.endswith(".pdf"):

            with open(file_path, "rb") as file:

                reader = PyPDF2.PdfReader(file)

                for page_number, page in enumerate(
                    reader.pages,
                    start=1
                ):

                    extracted = page.extract_text()

                    if extracted:
                        pages.append({
                            "page": page_number,
                            "text": extracted.lower()
                        })

        elif filename_lower.endswith(".docx"):

            document = docx.Document(file_path)

            text = "\n".join(
                paragraph.text
                for paragraph in document.paragraphs
            )

            if text.strip():
                pages.append({
                    "page": 1,
                    "text": text.lower()
                })

        elif filename_lower.endswith(".txt"):

            with open(
                file_path,
                "r",
                encoding="utf-8"
            ) as file:

                text = file.read()

                if text.strip():
                    pages.append({
                        "page": 1,
                        "text": text.lower()
                    })

    except Exception as error:

        print(
            f"[ERROR] Could not extract pages from "
            f"{filename}: {error}"
        )

    return pages


def count_phrase_occurrences(text, phrase):
    """
    Count exact adjacent occurrences of a normalized phrase.

    A phrase occurrence means the query terms appear next to each
    other in the same order. Word proximity without adjacency is
    not counted.
    """

    if not text or not phrase:
        return 0

    normalized_text = " ".join(
        text.lower().split()
    )

    normalized_phrase = " ".join(
        phrase.lower().split()
    )

    if not normalized_phrase:
        return 0

    count = 0
    start = 0

    while True:

        position = normalized_text.find(
            normalized_phrase,
            start
        )

        if position == -1:
            break

        count += 1

        start = (
            position
            +
            len(normalized_phrase)
        )

    return count


# ============================================================
# PAGE-AWARE SNIPPET
# ============================================================

def get_snippet_and_page(
    filename,
    query_words,
    window=140
):
    """
    Return the best matching page/snippet.

    Match priority:
    1. Exact multi-word phrase.
    2. Exact token occurrence.
    3. Prefix occurrence.
    4. Nearby/proximity fallback.

    Highlights are relative to the returned snippet.
    """

    pages = PAGE_TEXT_INDEX.get(
        filename,
        []
    )

    if not pages:
        return {
            "snippet": "No readable text found.",
            "page": None,
            "highlights": []
        }

    cleaned_words = [
        word.lower().strip()
        for word in query_words
        if word
    ]

    cleaned_words = list(
        dict.fromkeys(cleaned_words)
    )

    if not cleaned_words:
        return {
            "snippet": "No matching text found.",
            "page": None,
            "highlights": []
        }

    phrase = " ".join(cleaned_words)

    # --------------------------------------------------------
    # 1. EXACT PHRASE
    # --------------------------------------------------------
    if len(cleaned_words) >= 2:

        for page_data in pages:

            page_number = page_data["page"]

            original_text = " ".join(
                page_data["text"].split()
            )

            lower_text = original_text.lower()

            phrase_position = lower_text.find(
                phrase
            )

            if phrase_position != -1:

                return build_snippet_result(
                    original_text,
                    page_number,
                    phrase_position,
                    len(phrase),
                    window,
                    phrase
                )

    # --------------------------------------------------------
    # 2. EXACT TOKEN
    # --------------------------------------------------------
    exact_candidates = []

    for page_data in pages:

        page_number = page_data["page"]

        original_text = " ".join(
            page_data["text"].split()
        )

        lower_text = original_text.lower()

        for word in cleaned_words:

            # Token boundary-aware exact matching.
            pattern = (
                r"(?<![a-z0-9])"
                +
                re.escape(word)
                +
                r"(?![a-z0-9])"
            )

            match = re.search(
                pattern,
                lower_text
            )

            if match:

                exact_candidates.append({
                    "page": page_number,
                    "position": match.start(),
                    "length": len(word),
                    "text": original_text,
                    "word": word,
                    "score": 100000
                })

    if exact_candidates:

        # Prefer the first exact candidate in the earliest page only after
        # exactness has been established.
        best = exact_candidates[0]

        return build_snippet_result(
            best["text"],
            best["page"],
            best["position"],
            best["length"],
            window,
            best["word"]
        )

    # --------------------------------------------------------
    # 3. PREFIX MATCH
    # --------------------------------------------------------
    prefix_candidates = []

    for page_data in pages:

        page_number = page_data["page"]

        original_text = " ".join(
            page_data["text"].split()
        )

        lower_text = original_text.lower()

        for word in cleaned_words:

            if len(word) < 3:
                continue

            pattern = (
                r"(?<![a-z0-9])"
                +
                re.escape(word)
                +
                r"[a-z0-9]+"
            )

            match = re.search(
                pattern,
                lower_text
            )

            if match:

                matched_text = match.group(0)

                prefix_similarity = (
                    len(word)
                    /
                    float(
                        max(
                            len(matched_text),
                            1
                        )
                    )
                )

                prefix_candidates.append({
                    "page": page_number,
                    "position": match.start(),
                    "length": len(matched_text),
                    "text": original_text,
                    "word": word,
                    "score":
                        50000
                        +
                        prefix_similarity
                })

    if prefix_candidates:

        best = max(
            prefix_candidates,
            key=lambda item: item["score"]
        )

        return build_snippet_result(
            best["text"],
            best["page"],
            best["position"],
            best["length"],
            window,
            best["word"]
        )

    # --------------------------------------------------------
    # 4. PROXIMITY FALLBACK
    # --------------------------------------------------------
    best = None
    best_score = -1

    for page_data in pages:

        page_number = page_data["page"]

        original_text = " ".join(
            page_data["text"].split()
        )

        lower_text = original_text.lower()

        if not lower_text:
            continue

        candidates = []

        for word in cleaned_words:

            search_position = 0

            while True:

                position = lower_text.find(
                    word,
                    search_position
                )

                if position == -1:
                    break

                candidates.append(
                    (
                        position,
                        word
                    )
                )

                search_position = (
                    position + len(word)
                )

        for (
            position,
            matched_word
        ) in candidates:

            local_start = max(
                0,
                position - window
            )

            local_end = min(
                len(original_text),
                position + window
            )

            local_text = lower_text[
                local_start:local_end
            ]

            nearby_terms = sum(
                1
                for word
                in cleaned_words
                if word in local_text
            )

            candidate_score = (
                nearby_terms * 1000
                +
                len(matched_word)
            )

            if candidate_score > best_score:

                best_score = candidate_score

                best = {
                    "page": page_number,
                    "position": position,
                    "length": len(matched_word),
                    "text": original_text,
                    "score": candidate_score
                }

    if best is None:

        return {
            "snippet": "No matching text found.",
            "page": None,
            "highlights": []
        }

    return build_snippet_result(
        best["text"],
        best["page"],
        best["position"],
        best["length"],
        window,
        phrase
    )


def build_snippet_result(
    text,
    page_number,
    position,
    match_length,
    window,
    highlight_query
):
    """
    Build the final snippet and highlight ranges around a chosen match.
    """

    start = max(
        0,
        position - window
    )

    end = min(
        len(text),
        position + match_length + window
    )

    snippet = text[
        start:end
    ].strip()

    if start > 0:
        first_space = snippet.find(" ")

        if first_space != -1:
            snippet = snippet[
                first_space + 1:
            ]

        snippet = "... " + snippet

    if end < len(text):
        last_space = snippet.rfind(" ")

        if last_space != -1:
            snippet = snippet[
                :last_space
            ]

        snippet += " ..."

    highlights = []

    lower_snippet = snippet.lower()
    query = " ".join(
        highlight_query.lower().split()
    )

    # Exact phrase/token first.
    search_position = 0

    while True:

        found = lower_snippet.find(
            query,
            search_position
        )

        if found == -1:
            break

        highlights.append({
            "start": found,
            "end": found + len(query)
        })

        search_position = (
            found + len(query)
        )

    # If the exact query is not present in the snippet because the selected
    # match is a longer prefix token, highlight the query prefix.
    if not highlights and query:

        pattern = re.compile(
            re.escape(query),
            re.IGNORECASE
        )

        for match in pattern.finditer(
            lower_snippet
        ):

            highlights.append({
                "start": match.start(),
                "end": match.end()
            })

    # Merge overlapping/touching ranges.
    highlights.sort(
        key=lambda item: item["start"]
    )

    merged = []

    for item in highlights:

        if not merged:

            merged.append(item)
            continue

        previous = merged[-1]

        if item["start"] <= previous["end"]:

            previous["end"] = max(
                previous["end"],
                item["end"]
            )

        else:

            merged.append(item)

    return {
        "snippet": snippet,
        "page": page_number,
        "highlights": merged
    }



def sanitize_upload_filename(filename):
    """
    Return a safe local filename for an uploaded document.

    Only the final filename is stored in DATA_FOLDER. Directory
    components are removed to prevent path traversal.

    Returns an empty string when the filename is invalid or the
    extension is unsupported.
    """

    if not filename:
        return ""

    safe_name = secure_filename(
        os.path.basename(filename)
    )

    if not safe_name:
        return ""

    supported_extensions = (
        ".pdf",
        ".docx",
        ".txt",
    )

    if not safe_name.lower().endswith(
        supported_extensions
    ):
        return ""

    return safe_name


# ============================================================
# DATABASE VARIABLES
# ============================================================

REAL_INVERTED_INDEX = {}
DOCUMENT_METADATA = {}
FILENAME_INDEX = {}
PAGE_TEXT_INDEX = {}

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


def get_sqlite_connection():
    connection = sqlite3.connect(
        SQLITE_DB_FILE,
        timeout=30,
    )

    connection.execute(
        "PRAGMA foreign_keys = ON"
    )

    return connection


def initialize_sqlite_store():
    """
    Create the SQLite index.

    If search.db is empty but the legacy JSON index contains data,
    migrate the existing index once. Future document changes use
    incremental SQLite updates instead of full JSON rebuilds.
    """

    os.makedirs(DATA_FOLDER, exist_ok=True)

    with get_sqlite_connection() as connection:

        connection.executescript(
            SQLITE_SCHEMA
        )

        count = connection.execute(
            "SELECT COUNT(*) FROM documents"
        ).fetchone()[0]

        if (
            count == 0
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
    Full synchronization used only for initial migration or an explicit
    manual full rebuild. Normal uploads/deletes do NOT call this.
    """

    close_connection = False

    if connection is None:
        connection = get_sqlite_connection()
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

        for filename, metadata in DOCUMENT_METADATA.items():

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

        for term, posting_list in REAL_INVERTED_INDEX.items():

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

        for filename, filename_words in FILENAME_INDEX.items():

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

        for filename, pages in PAGE_TEXT_INDEX.items():

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


def load_database_from_sqlite():
    """
    Load the complete persisted SQLite snapshot into the existing
    in-memory search structures once at application startup.
    """

    global REAL_INVERTED_INDEX
    global DOCUMENT_METADATA
    global FILENAME_INDEX
    global PAGE_TEXT_INDEX

    with get_sqlite_connection() as connection:

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
            return

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

        with INDEX_DATA_LOCK:

            REAL_INVERTED_INDEX = (
                new_inverted_index
            )

            DOCUMENT_METADATA = (
                new_metadata
            )

            FILENAME_INDEX = (
                new_filename_index
            )

            PAGE_TEXT_INDEX = (
                new_page_text_index
            )


def get_document_term_counts_from_sqlite(
    filename,
):
    with get_sqlite_connection() as connection:

        rows = connection.execute(
            """
            SELECT term, term_count
            FROM term_postings
            WHERE filename = ?
            """,
            (filename,),
        ).fetchall()

    return dict(rows)


def _remove_document_from_memory(
    filename,
    old_term_counts,
):
    """
    Remove one document without rebuilding the corpus.

    Posting-list dictionaries are replaced rather than mutated in-place,
    so searches already holding a posting list keep a stable object.
    """

    for term in old_term_counts:

        posting_list = (
            REAL_INVERTED_INDEX.get(term)
        )

        if not posting_list:
            continue

        new_posting_list = dict(
            posting_list
        )

        new_posting_list.pop(
            filename,
            None,
        )

        if new_posting_list:
            REAL_INVERTED_INDEX[
                term
            ] = new_posting_list
        else:
            REAL_INVERTED_INDEX.pop(
                term,
                None,
            )

    DOCUMENT_METADATA.pop(
        filename,
        None,
    )

    FILENAME_INDEX.pop(
        filename,
        None,
    )

    PAGE_TEXT_INDEX.pop(
        filename,
        None,
    )


def _add_document_to_memory(
    filename,
    metadata,
    filename_words,
    pages,
    content_words,
):
    DOCUMENT_METADATA[filename] = metadata

    # Filename index is document-local.
    FILENAME_INDEX[filename] = (
        filename_words
    )

    PAGE_TEXT_INDEX[filename] = pages

    # Each changed posting list gets a new dictionary object.
    term_counts = {}

    for word in content_words:
        term_counts[word] = (
            term_counts.get(word, 0) + 1
        )

    for term, count in term_counts.items():

        old_postings = REAL_INVERTED_INDEX.get(
            term,
            {}
        )

        new_postings = dict(
            old_postings
        )

        new_postings[filename] = count

        REAL_INVERTED_INDEX[
            term
        ] = new_postings


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

    text = extract_text(
        file_path,
        filename,
    )

    content_words = tokenize(
        text
    )

    term_counts = {}

    for word in content_words:
        term_counts[word] = (
            term_counts.get(word, 0)
            + 1
        )

    pages = extract_pages(
        file_path,
        filename,
    )

    filename_without_extension = (
        os.path.splitext(filename)[0]
    )

    filename_words = tokenize_filename(
        filename_without_extension
    )

    if not content_words:
        raise ValueError(
            "Document contains no readable text."
        )

    metadata = {
        "title": filename,
        "path": os.path.abspath(
            file_path
        ),
        "total_words": len(
            content_words
        ),
        "page_count": len(
            pages
        ),
    }

    old_term_counts = (
        get_document_term_counts_from_sqlite(
            filename
        )
    )

    with INDEX_DATA_LOCK:

        _remove_document_from_memory(
            filename,
            old_term_counts,
        )

        _add_document_to_memory(
            filename,
            metadata,
            filename_words,
            pages,
            content_words,
        )

    # Persist only this document and its postings.
    own_connection = connection is None

    if own_connection:
        connection = get_sqlite_connection()

    try:
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

        _remove_document_from_memory(
            filename,
            old_term_counts,
        )

    own_connection = connection is None

    if own_connection:
        connection = get_sqlite_connection()

    try:
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


def load_database():

    global REAL_INVERTED_INDEX
    global DOCUMENT_METADATA
    global FILENAME_INDEX
    global PAGE_TEXT_INDEX

    if os.path.exists(INDEX_FILE):

        try:

            with open(
                INDEX_FILE,
                "r",
                encoding="utf-8"
            ) as file:

                REAL_INVERTED_INDEX = json.load(
                    file
                )

        except Exception as error:

            print(
                f"[DATABASE ERROR] Could not load "
                f"content index: {error}"
            )

            REAL_INVERTED_INDEX = {}

    if os.path.exists(META_FILE):

        try:

            with open(
                META_FILE,
                "r",
                encoding="utf-8"
            ) as file:

                DOCUMENT_METADATA = json.load(
                    file
                )

        except Exception as error:

            print(
                f"[DATABASE ERROR] Could not load "
                f"metadata: {error}"
            )

            DOCUMENT_METADATA = {}

    if os.path.exists(FILENAME_INDEX_FILE):

        try:

            with open(
                FILENAME_INDEX_FILE,
                "r",
                encoding="utf-8"
            ) as file:

                FILENAME_INDEX = json.load(
                    file
                )

        except Exception as error:

            print(
                f"[DATABASE ERROR] Could not load "
                f"filename index: {error}"
            )

            FILENAME_INDEX = {}

    if os.path.exists(PAGE_TEXT_FILE):

        try:

            with open(
                PAGE_TEXT_FILE,
                "r",
                encoding="utf-8"
            ) as file:

                PAGE_TEXT_INDEX = json.load(
                    file
                )

        except Exception as error:

            print(
                f"[DATABASE ERROR] Could not load "
                f"page text index: {error}"
            )

            PAGE_TEXT_INDEX = {}


# ============================================================
# DATABASE SAVE
# ============================================================

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
    inverted_index,
    document_metadata,
    filename_index,
    page_text_index,
):
    atomic_write_json(
        INDEX_FILE,
        inverted_index,
    )

    atomic_write_json(
        META_FILE,
        document_metadata,
    )

    atomic_write_json(
        FILENAME_INDEX_FILE,
        filename_index,
    )

    atomic_write_json(
        PAGE_TEXT_FILE,
        page_text_index,
    )


def save_database():
    with INDEX_DATA_LOCK:
        snapshot = (
            REAL_INVERTED_INDEX,
            DOCUMENT_METADATA,
            FILENAME_INDEX,
            PAGE_TEXT_INDEX,
        )

    save_database_snapshot(*snapshot)

def rebuild_database():
    """
    Build a complete new index without mutating the active snapshot.

    Searches continue to use the previous complete snapshot until the
    new snapshot has been fully built and persisted. Then all four active
    index dictionaries are swapped together.
    """

    print()
    print("==============================================")
    print("REBUILDING SEARCH DATABASE")
    print("==============================================")

    new_inverted_index = {}
    new_document_metadata = {}
    new_filename_index = {}
    new_page_text_index = {}

    supported_extensions = (
        ".pdf",
        ".docx",
        ".txt",
    )

    for filename in os.listdir(DATA_FOLDER):

        if not filename.lower().endswith(
            supported_extensions
        ):
            continue

        file_path = os.path.join(
            DATA_FOLDER,
            filename,
        )

        print(
            f"[REBUILD] Indexing: {filename}"
        )

        try:
            text = extract_text(
                file_path,
                filename,
            )

            content_words = tokenize(text)

            filename_without_extension = (
                os.path.splitext(filename)[0]
            )

            filename_words = tokenize_filename(
                filename_without_extension
            )

            pages = extract_pages(
                file_path,
                filename,
            )

            if not content_words:
                print(
                    "[REBUILD] Skipped empty document: "
                    f"{filename}"
                )
                continue

            new_document_metadata[filename] = {
                "title": filename,
                "path": os.path.abspath(file_path),
                "total_words": len(content_words),
                "page_count": len(pages),
            }

            new_filename_index[
                filename
            ] = filename_words

            new_page_text_index[
                filename
            ] = pages

            for word in content_words:

                if word not in new_inverted_index:
                    new_inverted_index[word] = {}

                if (
                    filename
                    not in new_inverted_index[word]
                ):
                    new_inverted_index[word][filename] = 0

                new_inverted_index[word][filename] += 1

        except Exception as error:

            print(
                f"[REBUILD ERROR] Could not index "
                f"{filename}: {error}"
            )

            continue

    print()
    print(
        "[REBUILD] New snapshot complete: "
        f"{len(new_document_metadata)} documents, "
        f"{len(new_inverted_index)} content terms, "
        f"{len(new_filename_index)} filenames indexed, "
        f"{len(new_page_text_index)} page-text entries"
    )

    # Write the full new snapshot first.
    save_database_snapshot(
        new_inverted_index,
        new_document_metadata,
        new_filename_index,
        new_page_text_index,
    )

    global REAL_INVERTED_INDEX
    global DOCUMENT_METADATA
    global FILENAME_INDEX
    global PAGE_TEXT_INDEX

    # One pointer swap: searches see old or new, never a partial build.
    with INDEX_DATA_LOCK:
        REAL_INVERTED_INDEX = new_inverted_index
        DOCUMENT_METADATA = new_document_metadata
        FILENAME_INDEX = new_filename_index
        PAGE_TEXT_INDEX = new_page_text_index

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


def build_paginated_response(
    results,
    page,
    limit,
):
    """
    Return a stable paginated search response.

    Ranking happens before this helper. This function only slices
    the ranked result list and returns pagination metadata.
    """

    total = len(results)

    total_pages = (
        (total + limit - 1) // limit
        if total > 0
        else 0
    )

    if total_pages > 0:
        page = max(
            1,
            min(page, total_pages)
        )
    else:
        page = max(1, page)

    start_index = (
        (page - 1) * limit
        if total_pages > 0
        else 0
    )

    end_index = (
        start_index + limit
    )

    page_results = results[
        start_index:end_index
    ]

    return jsonify({
        "results": page_results,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": total_pages,
            "has_next":
                page < total_pages,
            "has_previous":
                page > 1
                and total_pages > 0,
            "start":
                (
                    start_index + 1
                    if page_results
                    else 0
                ),
            "end":
                (
                    start_index
                    + len(page_results)
                    if page_results
                    else 0
                ),
        },
    })


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
        return jsonify({
            "results": [],
            "pagination": {
                "page": 1,
                "limit": 10,
                "total": 0,
                "total_pages": 0,
                "has_next": False,
                "has_previous": False
            }
        })

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
    with INDEX_DATA_LOCK:
        active_inverted_index = REAL_INVERTED_INDEX
        active_document_metadata = DOCUMENT_METADATA
        active_filename_index = FILENAME_INDEX
        active_page_text_index = PAGE_TEXT_INDEX

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
