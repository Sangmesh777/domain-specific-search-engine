"""
The ranking pipeline is transport-independent.

This is the last extraction, and the one the offline mode depends on.
`execute_search` used to be a Flask route handler that read
`request.args`, captured four module globals under a lock, and returned
`jsonify(...)`. The ranking itself now lives in
`search_engine/engine.py` as `search_index(...)`, which takes all seven
of those inputs as arguments and returns a plain payload.

Three separate claims are being made, and they need separate evidence:

1. **Structural** - the engine module does not touch Flask, `request`,
   `jsonify`, the lock or any index global, and app.py holds no ranking
   logic. Checked here by AST.
2. **Behavioural** - the response is unchanged. Checked by the 79 golden
   vectors at exact binary64, by shadow parity against the monolith over
   adversarial queries, and by the live HTTP suite.
3. **Independence** - the engine can be called with no Flask app, no
   request context and no globals, and produces the same answer. That is
   the property Android's LocalBackend needs, and it is the one claim a
   golden-vector replay cannot make, because the vectors go through the
   HTTP route either way.

These tests cover (1) and (3) directly. (2) lives in the gates.
"""

import ast
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT))

from search_engine import engine  # noqa: E402

APP_PATH = REPO_ROOT / "app.py"
ENGINE_PATH = REPO_ROOT / "search_engine" / "engine.py"

# Names that must not appear in the engine module at all, as code.
FORBIDDEN_GLOBALS = (
    "INDEX_DATA_LOCK",
    "REAL_INVERTED_INDEX",
    "DOCUMENT_METADATA",
    "FILENAME_INDEX",
    "PAGE_TEXT_INDEX",
    "INDEX_STATE",
    "publish_index_state",
)


def function_source(path, name):
    """The source segment of one top-level function."""

    source = source_of(path)

    tree = ast.parse(source)

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""

    raise AssertionError(f"{name} is not defined in {path}")


def source_of(path):
    return path.read_text(encoding="utf-8")


def loaded_names(tree):
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def imported_roots(tree):
    roots = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])

    return roots


def snapshot():
    """A small index, built by hand so it does not depend on the code."""

    return {
        "inverted_index": {
            "alpha": {"note.txt": 2, "multi.txt": 1},
            "beta": {"note.txt": 1, "multi.txt": 3},
        },
        "document_metadata": {
            "note.txt": {
                "title": "note.txt",
                "path": "/corpus/note.txt",
                "total_words": 4,
                "page_count": 1,
            },
            "multi.txt": {
                "title": "multi.txt",
                "path": "/corpus/multi.txt",
                "total_words": 6,
                "page_count": 2,
            },
        },
        "filename_index": {
            "note.txt": ["note"],
            "multi.txt": ["multi"],
        },
        "page_text_index": {
            "note.txt": [{"page": 1, "text": "alpha beta"}],
            "multi.txt": [
                {"page": 1, "text": "alpha"},
                {"page": 2, "text": "beta beta beta"},
            ],
        },
    }


def search(query, page="1", limit="10"):
    """Call the engine directly, with no Flask app in existence."""

    index = snapshot()

    return engine.search_index(
        query,
        page,
        limit,
        index["inverted_index"],
        index["document_metadata"],
        index["filename_index"],
        index["page_text_index"],
    )


# ----------------------------------------------------------------------
# 1. Structural: the engine is not a web handler
# ----------------------------------------------------------------------


def test_engine_does_not_import_flask():
    assert imported_roots(ast.parse(source_of(ENGINE_PATH))) == {
        "math",
        "os",
        "search_engine",
        "urllib",
    }


def test_engine_never_mentions_the_request_or_the_response_helpers():
    """
    Checked as loaded names, not as substrings.

    The module docstring says the words "request" and "jsonify"; what
    matters is that no expression reads them.
    """

    names = loaded_names(ast.parse(source_of(ENGINE_PATH)))

    assert "request" not in names
    assert "jsonify" not in names


@pytest.mark.parametrize("name", FORBIDDEN_GLOBALS)
def test_engine_holds_no_module_globals(name):
    """
    The whole point of the extraction: the ranking is a function of its
    arguments. A global here is state the offline backend cannot supply.
    """

    names = loaded_names(ast.parse(source_of(ENGINE_PATH)))

    assert name not in names, (
        f"search_engine/engine.py reads the global {name!r}"
    )


def test_engine_module_has_no_module_level_state():
    """Only imports, constants used as literals, and the two functions."""

    tree = ast.parse(source_of(ENGINE_PATH))

    allowed = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Expr)

    for node in tree.body:
        assert isinstance(node, allowed), (
            f"unexpected module-level {type(node).__name__} at line "
            f"{node.lineno}"
        )


def test_app_no_longer_holds_the_ranking_pipeline():
    """app.py is the adapter; the pipeline must not be back in it."""

    source = source_of(APP_PATH)

    tree = ast.parse(source)

    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }

    assert "execute_search" in functions

    body = ast.get_source_segment(source, functions["execute_search"])

    # The adapter is small and mechanical. A pipeline in it would be
    # hundreds of lines; this bound is what stops one growing back.
    assert len(body.splitlines()) < 60, (
        f"execute_search is {len(body.splitlines())} lines; it should be "
        "a thin adapter over search_engine.engine"
    )

    for marker in (
        "document_scores",
        "best_term_weights",
        "final_score",
        "ranked_documents",
    ):
        assert marker not in source, (
            f"app.py still contains the ranking logic {marker!r}"
        )


def test_the_adapter_is_the_only_caller_of_the_engine():
    tree = ast.parse(source_of(APP_PATH))

    callers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and "search_index(" in (
            ast.get_source_segment(source_of(APP_PATH), node) or ""
        )
    }

    assert callers == {"execute_search"}, f"unexpected callers: {callers}"


def test_the_adapter_captures_the_snapshot_under_the_lock():
    """
    The engine receives one coherent generation.

    The capture used to happen inside the ranking function. If the
    adapter ever captured only some of the four containers, a request
    could rank against the new postings and the previous metadata.
    """

    source = source_of(APP_PATH)

    tree = ast.parse(source)

    adapter = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "execute_search"
    )

    locks = [
        node
        for node in ast.walk(adapter)
        if isinstance(node, ast.With)
    ]

    assert len(locks) == 1, "expected exactly one lock acquisition"

    with_source = ast.get_source_segment(source, locks[0]) or ""

    assert "INDEX_DATA_LOCK" in with_source

    for container in (
        "REAL_INVERTED_INDEX",
        "DOCUMENT_METADATA",
        "FILENAME_INDEX",
        "PAGE_TEXT_INDEX",
    ):
        assert container in with_source, (
            f"{container} is not captured under the lock"
        )


def test_the_route_still_decorates_the_adapter():
    """The URL must survive the extraction."""

    tree = ast.parse(source_of(APP_PATH))

    adapter = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "execute_search"
    )

    decorators = [
        ast.unparse(decorator) for decorator in adapter.decorator_list
    ]

    # `ast.unparse` normalises quoting, so compare with it normalised
    # on both sides rather than assuming the source's style.
    assert any("/api/search" in text for text in decorators), (
        f"the search route moved: {decorators}"
    )


# ----------------------------------------------------------------------
# 2. The remaining handlers are adapters
# ----------------------------------------------------------------------

# Every function that belongs to the engine, the storage layer or the
# ranking core. A route handler calling one of these directly is doing
# engine work with the wrong owner.
ENGINE_OWNED = (
    "tokenize(",
    "tokenize_filename(",
    "extract_text(",
    "extract_pages(",
    "count_phrase_occurrences(",
    "normalize_search_query(",
    "parse_filetype_filter(",
    "build_index_from_folder(",
    "replace_document_rows(",
    "delete_document_rows(",
)

HANDLERS = (
    "upload_file",
    "bulk_delete_documents",
    "open_document",
    "rebuild_database_background",
    "execute_search",
)


@pytest.mark.parametrize("handler", HANDLERS)
def test_handlers_hold_no_engine_logic(handler):
    """
    The remaining handlers are transport.

    Their length comes from request parsing, response construction and
    status bookkeeping. This is what stops engine rules growing back
    into them, which is the failure mode the whole extraction exists to
    prevent.
    """

    body = function_source(APP_PATH, handler)

    for marker in ENGINE_OWNED:
        assert marker not in body, (
            f"{handler} calls {marker!r} directly; that belongs behind "
            "the engine or storage boundary"
        )

    # Delegating to the engine is the adapter's job, so `search_index`
    # is deliberately not on the forbidden list. It is asserted the
    # other way round in `test_the_adapter_is_the_only_caller_of_the_engine`.


@pytest.mark.parametrize("handler", HANDLERS)
def test_handlers_hold_no_sql(handler):
    body = function_source(APP_PATH, handler)

    for marker in ("INSERT INTO", "DELETE FROM", "SELECT ", "PRAGMA"):
        assert marker not in body, f"{handler} contains {marker!r}"


def test_the_directory_walk_lives_in_the_indexing_layer():
    """Rebuilding a folder is the indexing layer's job, not a route's."""

    for marker in ("os.listdir", "supported_extensions"):
        assert marker not in source_of(APP_PATH), (
            f"app.py contains {marker!r}"
        )

    assert "os.listdir" in source_of(
        REPO_ROOT / "search_engine" / "indexing.py"
    )


def test_the_background_handler_only_schedules():
    """
    It may start a build and report status, but must not build inline.

    A build inside the thread body would run the extraction inside the
    request-serving process without the status transitions around it.
    """

    body = function_source(APP_PATH, "rebuild_database_background")

    assert "rebuild_database(" in body

    for marker in ENGINE_OWNED:
        assert marker not in body


# ----------------------------------------------------------------------
# 3. Independence: callable with no Flask, no request, no globals
# ----------------------------------------------------------------------


def test_search_runs_without_a_flask_app():
    """
    No `app`, no request context, no test client.

    This is the claim the offline backend rests on, and the one a
    golden-vector replay cannot make, because the vectors have to route
    through HTTP to be compared.
    """

    # This used to assert `"flask" not in sys.modules or True`, which is
    # always true, so the one test named "runs without a flask app"
    # asserted nothing about Flask. An assertion about what is in
    # `sys.modules` here would be no better: whether Flask has been
    # imported depends on which other test files ran first, which makes
    # the result an accident of ordering rather than a property of the
    # engine. The real proof is `tests/test_core_is_transport_free.py`,
    # which blocks the import outright in a subprocess and then imports
    # and runs the package. What is checked here is that the call itself
    # needs no app, no request context and no client.
    payload = search("alpha")

    assert isinstance(payload, dict)
    assert set(payload) == {"results", "pagination"}
    assert payload["pagination"]["total"] == 2


def test_the_payload_is_json_serialisable():
    """
    A plain payload, not a Response.

    A Flask `Response` would serialise fine here too, so this asserts
    the type as well as the round trip - the caller is what turns it
    into HTTP.
    """

    payload = search("alpha")

    assert not hasattr(payload, "get_json")

    assert json.loads(json.dumps(payload)) == payload


def test_the_same_inputs_give_the_same_payload_twice():
    """Determinism, with no hidden state to carry between calls."""

    assert search("alpha") == search("alpha")


def test_two_different_snapshots_do_not_affect_each_other():
    """
    No global index, so one call cannot leak into the next.

    With the old globals this passed only because the corpus happened to
    be the same; here a second, smaller snapshot is searched and the
    first is searched again afterwards.
    """

    first = search("alpha")
    assert first["pagination"]["total"] == 2

    small = {
        "inverted_index": {"alpha": {"only.txt": 1}},
        "document_metadata": {
            "only.txt": {
                "title": "only.txt",
                "path": "/other/only.txt",
                "total_words": 1,
                "page_count": 1,
            }
        },
        "filename_index": {"only.txt": ["only"]},
        "page_text_index": {"only.txt": [{"page": 1, "text": "alpha"}]},
    }

    second = engine.search_index(
        "alpha",
        "1",
        "10",
        small["inverted_index"],
        small["document_metadata"],
        small["filename_index"],
        small["page_text_index"],
    )

    assert second["pagination"]["total"] == 1
    assert [item["title"] for item in second["results"]] == ["only.txt"]

    assert search("alpha") == first


def test_an_empty_index_returns_the_original_bare_list():
    """The zero-document early return, unchanged by the extraction."""

    empty = engine.search_index("alpha", "1", "10", {}, {}, {}, {})

    assert empty == []


def test_the_query_parameter_is_not_mutated_by_stripping():
    """
    The adapter passes the raw string and the engine strips it.

    If the engine stripped in place and the adapter reused the value
    there would be a subtle coupling; there is no shared object here.
    """

    payload = search("   alpha   ")

    assert payload["pagination"]["total"] == 2


@pytest.mark.parametrize(
    "page,limit",
    [
        ("0", "10"),
        ("-5", "10"),
        ("1", "0"),
        ("1", "-1"),
        ("1", "999"),
        ("not-a-number", "also-not"),
        ("", ""),
    ],
)
def test_bad_pagination_is_handled_by_the_engine(page, limit):
    """
    The parsing `try`/`except` moved with the body, so these still fall
    back rather than raising. The bounds are `max(1, ...)` then
    `min(50, ...)`, which is why `limit="0"` becomes 1.
    """

    payload = search("alpha", page, limit)

    assert isinstance(payload, dict)

    assert payload["pagination"]["limit"] == min(
        50, max(1, int(limit) if limit.lstrip("-").isdigit() else 10)
    )

    assert payload["pagination"]["page"] >= 1
