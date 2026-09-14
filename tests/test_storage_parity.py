"""
The storage layer's two claims: it reconstructs exactly, and it is
transport-independent.

The reconstruction claim is proved by `tools/storage_equivalence.py`,
which runs whole engine sessions against the extracted layer and against
the monolith read out of git, then compares every structure, every
SQLite row and every snapshot file. These tests drive that tool and
enforce the structural properties it depends on.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

STORAGE_PATH = REPO_ROOT / "search_engine" / "storage.py"
APP_PATH = REPO_ROOT / "app.py"


def run_tool(*arguments, timeout=900):
    """Run a tools/ module and return the completed process."""

    return subprocess.run(
        [sys.executable, "-m", *arguments],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def source_of(path):
    return path.read_text(encoding="utf-8")


def toplevel_functions(path):
    tree = ast.parse(source_of(path))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


# ----------------------------------------------------------------------
# Reconstruction parity
# ----------------------------------------------------------------------


def test_storage_parity_holds_for_the_golden_corpus():
    """
    The strongest single check: the extracted storage layer and the
    monolith must agree on the 13-document corpus, both after ingest and
    after a restart.
    """

    completed = run_tool(
        "tools.storage_equivalence",
        "--fixture",
        "golden-corpus",
    )

    assert completed.returncode == 0, (
        f"storage parity failed:\n{completed.stdout[-4000:]}"
        f"\n{completed.stderr[-2000:]}"
    )

    assert "STORAGE PARITY OK" in completed.stdout
    assert "differences    : 0" in completed.stdout


def test_storage_parity_holds_for_adversarial_fixtures():
    """
    Deletes, an empty store, shared terms and order-sensitive filenames.

    The order-sensitive fixture is not decoration: it is the only
    fixture on which a change to the reader's ORDER BY is observable, so
    without it the rowid-order claim would be untested.
    """

    completed = run_tool(
        "tools.storage_equivalence",
        "--fixture",
        "synthetic-with-delete",
        "--fixture",
        "order-sensitive-filenames",
        "--fixture",
        "empty-corpus",
    )

    assert completed.returncode == 0, (
        f"storage parity failed:\n{completed.stdout[-4000:]}"
    )

    assert "differences    : 0" in completed.stdout


def test_the_comparator_detects_every_deliberate_corruption():
    """
    A tool that reports "0 differences" is worthless until it has been
    shown to report a difference.
    """

    completed = run_tool(
        "tools.storage_equivalence",
        "--negative-control",
        "--fixture",
        "empty-corpus",
    )

    assert completed.returncode == 0, (
        f"negative control failed:\n{completed.stdout[-4000:]}"
    )

    assert "NEGATIVE CONTROL OK" in completed.stdout

    # Nothing may be silently missed.
    assert "MISSED" not in completed.stdout


def test_the_restart_comparison_is_relaxed_only_for_filename_order():
    """
    The restart self-check ignores filename token order because that
    order is not round-tripped. It must not ignore anything else, and the
    strict comparison used for the parity claim must still catch a
    reorder.
    """

    completed = run_tool(
        "tools.storage_equivalence",
        "--negative-control",
        "--fixture",
        "empty-corpus",
    )

    assert "restart comparison ignores filename order: as intended" in (
        completed.stdout
    )
    assert (
        "strict comparison still catches a filename reorder: yes"
        in completed.stdout
    )


# ----------------------------------------------------------------------
# The storage module is transport-independent
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    ("flask", "werkzeug", "request", "jsonify", "app"),
)
def test_storage_does_not_import_the_web_layer(forbidden):
    """storage.py must be reusable by a non-HTTP backend."""

    tree = ast.parse(source_of(STORAGE_PATH))

    imported = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])

    assert forbidden not in imported, (
        f"search_engine/storage.py imports {forbidden!r}; it must stay "
        "usable without the web application"
    )


@pytest.mark.parametrize(
    "symbol",
    (
        "REAL_INVERTED_INDEX",
        "DOCUMENT_METADATA",
        "FILENAME_INDEX",
        "PAGE_TEXT_INDEX",
        "INDEX_DATA_LOCK",
        "INDEX_STATE",
        "publish_index_state",
    ),
)
def test_storage_does_not_reach_into_application_state(symbol):
    """
    The four index globals and the publication machinery belong to the
    application. A storage function that read them would not be reusable,
    and could not be tested without one.
    """

    source = source_of(STORAGE_PATH)

    assert symbol not in source, (
        f"search_engine/storage.py references {symbol!r}; it must take "
        "its inputs as parameters and return its results"
    )


def test_reconstruction_functions_return_a_snapshot_without_installing_it():
    """
    `load_database_from_sqlite` and `load_json_snapshot` build a
    complete snapshot and hand it back. Publication happens in app.py,
    under one lock, so there is exactly one place where state becomes
    visible.
    """

    functions = toplevel_functions(STORAGE_PATH)

    for name in ("load_database_from_sqlite", "load_json_snapshot"):

        assert name in functions, f"{name} is missing from storage.py"

        node = functions[name]

        returns = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Return) and child.value is not None
        ]

        assert returns, f"{name} never returns its snapshot"

        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                function = child.func
                called = (
                    function.attr
                    if isinstance(function, ast.Attribute)
                    else function.id
                    if isinstance(child.func, ast.Name)
                    else None
                )

                assert called != "publish_index_state", (
                    f"{name} publishes state; storage must not"
                )


# ----------------------------------------------------------------------
# No storage logic left in app.py
# ----------------------------------------------------------------------


# SQL that must be gone entirely: everything the persistence layer owns.
REMOVED_FROM_APP = (
    "CREATE TABLE",
    "CREATE INDEX",
    "PRAGMA",
    "SELECT ",
    "json.dump",
    "json.load(",
    "os.fsync",
)

# SQL that legitimately remains, and where. These are the incremental
# write paths, which are the NEXT milestone: moving them would merge
# storage with indexing, which this pack forbids. Naming the exact
# functions keeps the remaining surface visible and makes any growth
# fail this test instead of going unnoticed.
REMAINING_SQL = {
    "incrementally_index_document",
    "incrementally_remove_document",
}


@pytest.mark.parametrize("marker", REMOVED_FROM_APP)
def test_app_no_longer_contains_the_persistence_logic(marker):
    """Every statement the storage layer owns should now live there."""

    assert marker not in source_of(APP_PATH), (
        f"app.py still contains {marker!r}; it belongs in "
        "search_engine/storage.py"
    )


def test_remaining_sql_in_app_is_confined_to_the_incremental_paths():
    """
    Document, precisely, how much SQL is left and where.

    This is deliberately a positive assertion rather than a wish. If a
    future change adds SQL to another function, or removes these, the
    test fails and the claim in DEVELOPMENT.md has to be updated with it.
    """

    source = source_of(APP_PATH)
    tree = ast.parse(source)

    holders = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        body = ast.get_source_segment(source, node) or ""

        if any(
            marker in body
            for marker in ("INSERT INTO", "INSERT OR IGNORE INTO", "DELETE FROM")
        ):
            holders.add(node.name)

    assert holders == REMAINING_SQL, (
        f"the set of functions still holding SQL changed: {sorted(holders)}. "
        f"Expected {sorted(REMAINING_SQL)}. If the extraction advanced, "
        "update REMAINING_SQL and DEVELOPMENT.md together."
    )


def test_app_does_not_import_sqlite():
    """Connections are opened only through the storage layer."""

    tree = ast.parse(source_of(APP_PATH))

    imported = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])

    assert "sqlite3" not in imported


def test_app_no_longer_imports_json():
    """
    `json` was only needed for the snapshot files, which moved.

    `jsonify` is a different name from a different module and is
    unaffected.
    """

    tree = ast.parse(source_of(APP_PATH))

    imported = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])

    assert "json" not in imported


def test_the_schema_is_defined_once():
    """A second copy of the schema would drift."""

    storage_source = source_of(STORAGE_PATH)
    app_source = source_of(APP_PATH)

    assert "CREATE TABLE IF NOT EXISTS documents" in storage_source
    assert "CREATE TABLE IF NOT EXISTS documents" not in app_source
