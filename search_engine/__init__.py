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
"""

from search_engine.config import (
    SUPPORTED_EXTENSIONS,
)

from search_engine.tokenizer import (
    tokenize,
    tokenize_filename,
)

from search_engine.query import (
    normalize_search_query,
    parse_filetype_filter,
)

from search_engine.filenames import (
    sanitize_upload_filename,
    secure_filename,
)

from search_engine.extraction import (
    extract_pages,
    extract_text,
)

from search_engine.snippets import (
    build_snippet_result,
    count_phrase_occurrences,
    get_snippet_and_page,
)

from search_engine.pagination import (
    paginate,
)


__all__ = [
    "SUPPORTED_EXTENSIONS",
    "build_snippet_result",
    "count_phrase_occurrences",
    "extract_pages",
    "extract_text",
    "get_snippet_and_page",
    "normalize_search_query",
    "paginate",
    "parse_filetype_filter",
    "sanitize_upload_filename",
    "secure_filename",
    "tokenize",
    "tokenize_filename",
]
