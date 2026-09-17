"""
The search engine layer's extraction spec.

This is the last extraction. `execute_search` is the ranking pipeline -
the thing the whole parities-of-record exercise exists to protect - and
it is also the only reason the core cannot be reused off the web. It
reads `request.args`, it captures four module globals under a lock, and
it calls `jsonify` on the way out. All three are the Flask adapter's
business, not the ranking algorithm's.

The transforms below are the entire behavioural surface of that change:

  * `request.args.get(...)` becomes a parameter. The values are passed
    through unchanged and still parsed by the same `try`/`except`, so a
    non-numeric `page` still falls back to 1 exactly as before.
  * the snapshot capture block is deleted, and the four names it bound
    become parameters. The caller holds the lock; the engine receives
    one coherent generation and cannot observe a half-rebound set.
  * `jsonify(X)` becomes `(X)`. That is the whole of "stop being a web
    handler": the payload expression is untouched, so the response body
    is identical, it is just returned rather than serialised.

Nothing about ordering, constants, or the ranking arithmetic changes.

`build_paginated_response` moves with it. It also calls `jsonify`, and
leaving it behind would keep a ranking-adjacent function inside the
adapter and force the engine to import it back.
"""

TITLE = "The ranking pipeline."

DOCSTRING = """
Search ranking, over an explicit index snapshot.

This module is the core. It takes a query and three index containers,
and returns a plain response payload. It has no Flask import, no
`request`, no `jsonify`, no lock, and no module globals: given the same
inputs it returns the same payload, which is what makes the offline
backend able to run it.

The caller is responsible for capturing a coherent snapshot and for
serialising the result. `app.py` does both, under `INDEX_DATA_LOCK`.

Behaviour here is pinned by three independent harnesses, because a
ranking change is the expensive kind of mistake:

* the 79 golden vectors replay complete responses at exact binary64;
* `tools/shadow_parity.py` runs this body and the original monolith
  body over the recorded vectors and adversarial queries and compares
  the results;
* `tools/storage_equivalence.py` compares whole engine sessions.
"""

FUNCTIONS = [
    "build_paginated_response",
    "execute_search",
]

# `execute_search` is the route handler's name in app.py, and the
# adapter keeps it. The moved body is `search_index`, because it is no
# longer a handler and the name should not pretend otherwise.
RENAMES = {
    "execute_search": "search_index",
}

IMPORTS = [
    "import math",
    "import os",
    "from urllib.parse import quote",
    "",
    "from search_engine.extract import count_phrase_occurrences",
    "from search_engine.snippet import get_snippet_and_page",
    "from search_engine.text import (",
    "    normalize_search_query,",
    "    parse_filetype_filter,",
    "    tokenize_filename,",
    ")",
]

TRANSFORMS = {
    # Serialising is the adapter's job. Every `return jsonify(X)` in
    # this body becomes `return (X)`, which is the same expression and
    # the same value, minus the HTTP.
    "build_paginated_response": {
        "replace_all": [["jsonify(", "("]],
    },
    "execute_search": {
        "replace_exact": [
            # The three request parameters. The two numeric ones stay
            # strings so the original `try`/`except` parsing below is
            # reached unchanged - including the fallback on a bad value.
            [
                '    raw_query = request.args.get(\n'
                '        "q",\n'
                '        ""\n'
                '    ).strip()',
                "    raw_query = raw_query_text.strip()",
            ],
            [
                '        requested_page = int(request.args.get("page", "1"))',
                "        requested_page = int(page_text)",
            ],
            [
                '        requested_limit = int(request.args.get("limit", "10"))',
                "        requested_limit = int(limit_text)",
            ],
            # The caller owns the lock and the snapshot; the engine takes
            # the four containers as arguments. Deleting the capture
            # block is what removes the engine's last access to shared
            # mutable state.
            [
                "    # Capture one coherent active snapshot for this request.\n"
                "    with INDEX_DATA_LOCK:\n"
                "        active_inverted_index = REAL_INVERTED_INDEX\n"
                "        active_document_metadata = DOCUMENT_METADATA\n"
                "        active_filename_index = FILENAME_INDEX\n"
                "        active_page_text_index = PAGE_TEXT_INDEX\n",
                "",
            ],
            # The slice includes the decorator, because the AST slice
            # starts at the decorator so the definition is complete.
            # This module must not know about Flask, so the route
            # metadata stays behind in app.py, where it is re-attached
            # to the adapter below. The signature carries the seven
            # inputs the body used to take from `request` and from the
            # four module globals.
            [
                '@app.route(\n'
                '    "/api/search",\n'
                '    methods=["GET"]\n'
                ')\n'
                'def execute_search():\n',
                "def execute_search(\n"
                "    raw_query_text,\n"
                "    page_text,\n"
                "    limit_text,\n"
                "    active_inverted_index,\n"
                "    active_document_metadata,\n"
                "    active_filename_index,\n"
                "    active_page_text_index,\n"
                "):\n",
            ],
        ],
        # The snippet helper was already given an explicit page-text
        # argument in the snippet extraction, and app.py's call site was
        # rewritten then. This call site lived inside `execute_search`,
        # so it kept reading the global; now that the container is a
        # parameter, the name has to follow.
        #
        # This runs after `replace_exact`, so the capture block above is
        # already gone and this rewrites only the remaining call.
        "rename_names": {
            "PAGE_TEXT_INDEX": "active_page_text_index",
        },
        "replace_all": [["jsonify(", "("]],
    },
}

# app.py keeps the route, the request parsing and the lock, and nothing
# else. The decorator that was stripped from the moved body lives here
# again, attached to the adapter, so the URL keeps working.
WRAPPERS = '''# ============================================================
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

'''
