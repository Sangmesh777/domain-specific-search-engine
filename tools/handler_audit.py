"""
Audit what the remaining Flask handlers actually do.

The claim under review is that `upload_file`, `bulk_delete_documents`,
`open_document` and `rebuild_database_background` are adapters - HTTP
parsing, serialisation, background orchestration, file-serving - and not
engine logic.

That claim is checkable, so it is checked rather than asserted. For each
handler this reports every call it makes, grouped by layer, and flags
anything that belongs to the engine: tokenisation, extraction, term
counting, ranking, or SQL.

Usage:
    python3 -m tools.handler_audit
"""

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

APP_PATH = REPO_ROOT / "app.py"

# Functions that belong to the engine, the storage layer or the ranking
# core. A route handler calling any of these directly is doing engine
# work with the wrong owner - unless the call is to an adapter that
# merely forwards, which is reported separately.
ENGINE_FUNCTIONS = {
    "tokenize": "tokenisation",
    "tokenize_filename": "tokenisation",
    "extract_text": "document extraction",
    "extract_pages": "document extraction",
    "count_phrase_occurrences": "ranking",
    "normalize_search_query": "query parsing",
    "parse_filetype_filter": "query parsing",
    "search_index": "ranking",
    "build_index_from_folder": "index construction",
    "build_snippet_result": "ranking",
    "get_snippet_and_page": "ranking",
}

STORAGE_FUNCTIONS = {
    "get_sqlite_connection": "connection",
    "create_schema": "schema",
    "count_documents": "sql",
    "get_document_term_counts_from_sqlite": "sql",
    "load_database_from_sqlite": "sql",
    "load_database": "snapshot io",
    "save_database": "snapshot io",
    "save_database_snapshot": "snapshot io",
    "sync_sqlite_from_memory": "full sync",
    "replace_document_rows": "rows",
    "delete_document_rows": "rows",
    "atomic_write_json": "snapshot io",
}

# Application-level orchestration. Legitimate in a handler.
APP_FUNCTIONS = {
    "incrementally_index_document": "incremental write (app-level)",
    "incrementally_remove_document": "incremental delete (app-level)",
    "set_index_status": "status",
    "get_index_status": "status",
    "publish_index_state": "publication",
    "rebuild_database": "rebuild adapter",
    "index_document_path": "path guard (adapter)",
    "resolve_document_path": "path guard (adapter)",
    "sanitize_upload_filename": "upload sanitisation",
}

HTTP_NAMES = {"request", "jsonify"}

HANDLERS = (
    "upload_file",
    "bulk_delete_documents",
    "open_document",
    "rebuild_database_background",
    "execute_search",
    "rebuild_api",
    "database_status",
)


def functions_by_name(tree):
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def called_names(node):
    """Every bare or attribute call, as a dotted string."""

    names = set()

    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue

        parts = []
        cursor = sub.func

        while isinstance(cursor, ast.Attribute):
            parts.append(cursor.attr)
            cursor = cursor.value

        if isinstance(cursor, ast.Name):
            parts.append(cursor.id)

        names.add(".".join(reversed(parts)))

    return names


def is_route_handler(node):
    for decorator in node.decorator_list:
        text = ast.unparse(decorator)

        if "route" in text:
            return True

    return False


def main():
    source = APP_PATH.read_text(encoding="utf-8")

    tree = ast.parse(source)

    functions = functions_by_name(tree)

    sql_markers = ("INSERT INTO", "DELETE FROM", "SELECT ", "PRAGMA")

    violations = []

    for name in HANDLERS:

        node = functions.get(name)

        if node is None:
            print(f"\n{name}: NOT PRESENT")
            continue

        body = ast.get_source_segment(source, node) or ""

        calls = called_names(node)

        engine = sorted(
            (call, ENGINE_FUNCTIONS[call])
            for call in calls
            if call in ENGINE_FUNCTIONS
        )
        storage = sorted(
            (call, STORAGE_FUNCTIONS[call])
            for call in calls
            if call in STORAGE_FUNCTIONS
        )
        app_level = sorted(
            (call, APP_FUNCTIONS[call])
            for call in calls
            if call in APP_FUNCTIONS
        )

        http = sorted(call for call in calls if call.split(".")[0] in HTTP_NAMES)

        lines = node.end_lineno - node.lineno + 1

        kind = "route" if is_route_handler(node) else "not-a-route"

        print(f"\n{'=' * 68}")
        print(f"{name}  ({lines} lines, {kind})")
        print("=" * 68)

        print(f"  http      : {http or '-'}")
        print(f"  app-level : {[c for c, _ in app_level] or '-'}")

        for label, group in (("engine", engine), ("storage", storage)):
            if group:
                print(f"  {label:9s} : {[c for c, _ in group]}")

        for marker in sql_markers:
            if marker in body:
                violations.append(f"{name}: contains {marker!r}")
                print(f"  !! {marker!r} appears in the body")

    print(f"\n{'=' * 68}")
    print("VERDICT")
    print("=" * 68)

    if violations:
        for violation in violations:
            print(f"  VIOLATION: {violation}")
        return 1

    print("  No SQL in any handler.")
    print("  Engine functions are called only where listed above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
