"""
Transport-independent search engine core.

This package is the single authoritative implementation of indexing,
ranking, snippet generation and persistence for the VTU domain-specific
search engine.

Architectural rule:

    Adapters wrap the engine. The engine never wraps an adapter.

Nothing in this package may import Flask, read an HTTP request, build an
HTTP response or start a thread. Adapters (the Flask API in app.py, and the
Android local backend) call into the engine and own their own transport,
lifecycle and scheduling concerns.

Typical use:

    from search_engine import EngineConfig, SearchEngine

    engine = SearchEngine(EngineConfig(data_folder="/var/lib/vtu"))
    engine.index_document("notes.pdf", "/var/lib/vtu/notes.pdf")
    engine.search("network")
"""

from search_engine.config import (
    FILETYPE_EXTENSIONS,
    FILETYPE_FILTER_TOKENS,
    SUPPORTED_EXTENSIONS,
    EngineConfig,
    default_data_folder,
)

from search_engine.engine import (
    SearchEngine,
)

from search_engine.extraction import (
    extract_pages,
    extract_text,
)

from search_engine.filenames import (
    normalize_requested_filenames,
    sanitize_upload_filename,
    secure_filename,
)

from search_engine.index import (
    IndexSnapshot,
    IndexState,
)

from search_engine.pagination import (
    empty_query_response,
    paginate,
)

from search_engine.query import (
    normalize_search_query,
    parse_filetype_filter,
)

from search_engine.rebuild import (
    build_snapshot_from_folder,
)

from search_engine.snippets import (
    build_snippet_result,
    count_phrase_occurrences,
    get_snippet_and_page,
)

from search_engine.status import (
    ERROR,
    INDEXING,
    READY,
    IndexStatusTracker,
)

from search_engine.storage import (
    SQLITE_SCHEMA,
    SqliteStore,
    atomic_write_json,
    read_json_snapshot,
    write_json_snapshot,
)

from search_engine.tokenizer import (
    tokenize,
    tokenize_filename,
)


__all__ = [
    "ERROR",
    "FILETYPE_EXTENSIONS",
    "FILETYPE_FILTER_TOKENS",
    "INDEXING",
    "READY",
    "SQLITE_SCHEMA",
    "SUPPORTED_EXTENSIONS",
    "EngineConfig",
    "IndexSnapshot",
    "IndexState",
    "IndexStatusTracker",
    "SearchEngine",
    "SqliteStore",
    "atomic_write_json",
    "build_snapshot_from_folder",
    "build_snippet_result",
    "count_phrase_occurrences",
    "default_data_folder",
    "empty_query_response",
    "extract_pages",
    "extract_text",
    "get_snippet_and_page",
    "normalize_requested_filenames",
    "normalize_search_query",
    "paginate",
    "parse_filetype_filter",
    "read_json_snapshot",
    "sanitize_upload_filename",
    "secure_filename",
    "tokenize",
    "tokenize_filename",
    "write_json_snapshot",
]
