"""
Run one complete engine session against an isolated data directory and
dump everything a parity comparison needs.

This is a *driver*, not a checker. `tools/storage_equivalence.py` starts
it twice - once against the current engine and once against the engine
as it was before the persistence layer was extracted - and compares the
two dumps.

It runs as a subprocess for two reasons. A whole engine cannot be
imported twice in one process, because the startup path rebuilds
module-level state; and a restart has to be a genuinely new process or
it is not testing reconstruction at all.

Usage (normally via the equivalence tool):

    python -m tools.storage_probe \
        --engine new|legacy \
        --base <git-revision> \
        --data <data-directory> \
        --corpus <corpus-directory> \
        --phase ingest|reconstruct \
        --out <json-file>
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Phases. `ingest` indexes the corpus, exercises the full-sync path and
# writes the JSON snapshot. `reconstruct` starts a second process over
# the same data directory and dumps what SQLite rebuilds.
PHASES = ("ingest", "reconstruct")

SQLITE_TABLES = ("documents", "term_postings", "filename_terms", "pages")

CONTAINERS = (
    "inverted_index",
    "document_metadata",
    "filename_index",
    "page_text_index",
)


class ProbeError(Exception):
    """Raised when the session cannot be run or dumped."""


def _load_engine():
    """Import the engine from the working tree."""

    import app

    return app

def _load_legacy_engine(base_revision):
    """
    exec the app.py from `base_revision` into a namespace.

    Its `__name__` is deliberately not `__main__`, so the start-server
    block at the bottom of the file does not run. Its `__file__` is
    pointed inside the repository so the engine's BASE_DIR resolves the
    way it does in normal use.
    """

    completed = subprocess.run(
        ["git", "show", f"{base_revision}:app.py"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )

    source = completed.stdout

    if "def sync_sqlite_from_memory" not in source:
        raise ProbeError(
            f"revision {base_revision} does not look like the monolithic "
            "engine"
        )

    # `__file__` must live inside the repository so the engine's BASE_DIR
    # resolves the way it does in normal use.
    fake_path = REPO_ROOT / "_legacy_app_for_parity.py"

    namespace = {
        "__name__": "_legacy_app_for_parity",
        "__file__": str(fake_path),
        "__builtins__": __builtins__,
    }

    exec(compile(source, str(fake_path), "exec"), namespace)

    return namespace


# The engine exposes the same names either way: as module attributes for
# the real module, as namespace entries for the exec'd legacy source.
def _get(engine, name):
    if isinstance(engine, dict):
        return engine[name]

    return getattr(engine, name)


def _snapshot_containers(engine, data_dir=None):
    """
    Return the four published containers, deep-copied into plain data.

    `data_dir` only affects documents stored inside it. The rebuild path
    reads `DATA_FOLDER`, which is per-engine, so a rebuilt document's
    absolute path differs between the two sides by construction - and
    the two sides are exactly what is compared. Paths under the data
    directory are therefore recorded relative to it, identically on both
    sides. Anything outside it - which is every document in the ingest
    phase, since the corpus is shared - is left exactly as it was.

    This normalises an environment-specific value; it is not an
    exemption. A rebuilt document still has to produce the same relative
    path, title, word count and page count on both sides.
    """

    state = _get(engine, "INDEX_STATE")

    published = state.snapshot()

    containers = {
        name: json.loads(json.dumps(published[name]))
        for name in CONTAINERS
    }

    return containers


def _normalise_paths(report, data_dir):
    """
    Make data-directory paths comparable across two isolated engines.

    The rebuild reads `DATA_FOLDER`, which each engine has to itself, so
    a rebuilt document's absolute path differs between the two sides by
    construction - and the two sides are exactly what is compared.

    Every path under the data directory is therefore recorded relative
    to it, in all three places the engine puts one: the published
    containers, the `documents.path` column, and the JSON snapshot
    files. Anything outside the data directory - which is every document
    in an incremental fixture, since the corpus is shared - is left
    alone.

    This normalises an environment-specific value; it is not an
    exemption. A rebuilt document still has to produce the same relative
    path, title, word count and page count on both sides.
    """

    prefix = os.path.realpath(str(data_dir)) + os.sep

    def relative(value):
        if isinstance(value, str) and value.startswith(prefix):
            return value[len(prefix):].replace(os.sep, "/")
        return value

    for metadata in report["containers"]["document_metadata"].values():
        metadata["path"] = relative(metadata.get("path"))

    for key in ("sqlite", "sqlite_after_incremental"):

        dump = report.get(key)

        if not dump or not dump.get("exists"):
            continue

        documents = dump["tables"].get("documents")

        if not documents:
            continue

        columns = documents["columns"]

        if "path" not in columns:
            continue

        index = columns.index("path")

        for row in documents["rows"]:
            row[index] = relative(row[index])

    # `document_meta.json` maps a filename to {"path": ..., ...}.
    for name, payload in (report.get("json_files") or {}).items():

        if not isinstance(payload, dict):
            continue

        for metadata in payload.values():

            if isinstance(metadata, dict) and "path" in metadata:
                metadata["path"] = relative(metadata["path"])


def _dump_sqlite(data_dir):
    """Dump every row of every table, in a deterministic order."""

    import sqlite3

    db_path = Path(data_dir) / "search.db"

    if not db_path.exists():
        return {"exists": False, "tables": {}}

    connection = sqlite3.connect(str(db_path))

    try:
        tables = {}

        for table in SQLITE_TABLES:

            rows = connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()

            columns = [
                description[0]
                for description in connection.execute(
                    f"SELECT * FROM {table} LIMIT 0"
                ).description
            ]

            tables[table] = {
                "columns": columns,
                "rows": [list(row) for row in rows],
            }

        schema = [
            {"type": row[0], "name": row[1], "sql": row[2]}
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "ORDER BY type, name"
            )
        ]

    finally:
        connection.close()

    return {"exists": True, "tables": tables, "schema": schema}


def _dump_json_files(data_dir):
    """
    Dump every JSON file in the data directory.

    Discovered rather than listed: the engine's file names are module
    constants, and hardcoding them here would let a mismatch in those
    constants pass unnoticed because both sides were compared against
    the same wrong list.
    """

    dumped = {}

    for path in sorted(Path(data_dir).glob("*.json")):

        dumped[path.name] = json.loads(
            path.read_text(encoding="utf-8")
        )

    return dumped


def _delete(engine, names):
    """
    Remove documents through the real single-document delete path.

    `incrementally_remove_document` reads the term counts back out of
    SQLite and rewrites both stores, so it is the persistence path with
    the most moving parts.
    """

    incrementally_remove_document = _get(
        engine, "incrementally_remove_document"
    )

    removed = []

    for name in names:
        try:
            incrementally_remove_document(name)
            removed.append(name)

        except Exception as error:
            removed.append(f"FAILED:{name}:{type(error).__name__}")

    return removed


def _bulk_delete(engine, names):
    """
    Remove several documents through the real bulk-delete route.

    This is the only path that shares one SQLite connection across
    documents with `commit=False` and rolls the batch back on failure,
    so it is the one whose transaction behaviour the extraction could
    silently change. Driving the route rather than a helper keeps that
    behaviour in scope.
    """

    flask_app = _get(engine, "app")

    response = flask_app.test_client().post(
        "/api/documents/bulk-delete",
        json={"filenames": list(names)},
    )

    body = response.get_json(silent=True) or {}

    # Only the counts and the name lists: the response also echoes index
    # sizes and a status object, which the container comparison already
    # covers and which would otherwise be compared twice.
    return {
        "status": response.status_code,
        "deleted": body.get("deleted"),
        "not_found": body.get("not_found"),
        "failed": body.get("failed"),
        "deleted_count": body.get("deleted_count"),
        "not_found_count": body.get("not_found_count"),
    }


def _reindex(engine, corpus_dir, names):
    """
    Re-index named documents from `corpus_dir`, replacing them.

    A first-time ingest never exercises the replacement branch: with no
    previous revision there are no stale rows to leave behind, so a
    write path that forgot to delete first would look correct. This is
    the only action that puts a *second* revision of a document through
    the incremental path.
    """

    incrementally_index_document = _get(
        engine, "incrementally_index_document"
    )

    reindexed = []

    for name in names:
        try:
            incrementally_index_document(
                name, str(Path(corpus_dir) / name)
            )
            reindexed.append(name)

        except Exception as error:
            reindexed.append(f"FAILED:{name}:{type(error).__name__}")

    return reindexed


def _rebuild(engine, data_dir, corpus_dir):
    """
    Run the real full rebuild over the data folder.

    The rebuild reads `DATA_FOLDER`, so the corpus is copied into it
    first. Without that the folder holds only `search.db` and the
    rebuild produces an empty snapshot on both sides - equal, and
    meaningless.

    This is the only path that walks a folder and rebuilds every
    container from scratch, and it is the path whose publish order the
    indexing extraction had to preserve, so it gets its own comparison
    rather than being assumed to follow from the incremental one.
    """

    copied = []

    for path in sorted(Path(corpus_dir).iterdir()):

        if not path.is_file():
            continue

        shutil.copy2(path, Path(data_dir) / path.name)

        copied.append(path.name)

    rebuild_database = _get(engine, "rebuild_database")

    rebuild_database()

    return copied


def _ingest(engine, corpus_dir):
    """
    Index the whole corpus through the real single-document path.

    `incrementally_index_document` is the path that writes memory and
    SQLite together, so it is the one whose transaction order this
    milestone must not change.
    """

    incrementally_index_document = _get(
        engine, "incrementally_index_document"
    )

    indexed = []

    for path in sorted(Path(corpus_dir).iterdir()):

        if not path.is_file():
            continue

        try:
            incrementally_index_document(path.name, str(path))
            indexed.append(path.name)

        except Exception as error:

            # A document the engine refuses is part of the observable
            # behaviour and must match too.
            indexed.append(f"FAILED:{path.name}:{type(error).__name__}")

    return indexed


def run_phase(
    engine,
    data_dir,
    corpus_dir,
    phase,
    deletes=(),
    bulk_deletes=(),
    reindex_dir=None,
    reindex=(),
    rebuild=False,
):
    """Run one phase and return its dump."""

    report = {
        "phase": phase,
        "data_dir": str(data_dir),
        "requested_deletes": list(deletes),
        "requested_bulk_deletes": list(bulk_deletes),
        "requested_reindex": list(reindex),
        "rebuild": bool(rebuild),
    }

    if phase == "ingest":

        report["indexed"] = _ingest(engine, corpus_dir)

        report["removed"] = _delete(engine, deletes) if deletes else []

        report["bulk_removed"] = (
            _bulk_delete(engine, bulk_deletes) if bulk_deletes else None
        )

        report["reindexed"] = (
            _reindex(engine, reindex_dir, reindex) if reindex else []
        )

        # Capture the store BEFORE the full sync below.
        #
        # `sync_sqlite_from_memory` deletes every row and rewrites it
        # from memory, so it erases exactly the kind of damage the
        # incremental path can do - a replacement that did not delete
        # first, or a delete that missed a table. Observing only after
        # it runs makes those defects invisible; both were confirmed to
        # survive a whole parity run undetected until this dump was
        # added.
        report["sqlite_after_incremental"] = _dump_sqlite(data_dir)

        if rebuild:

            report["rebuilt"] = _rebuild(engine, data_dir, corpus_dir)

            # The rebuild publishes and syncs on its own, so this is the
            # store the post-sync comparison will also see.
            report["sqlite_after_incremental"] = _dump_sqlite(data_dir)

        # Exercise the full-sync path as well as the incremental one, so
        # the comparison covers both writers.
        sync_sqlite_from_memory = _get(engine, "sync_sqlite_from_memory")

        sync_sqlite_from_memory()

        save_database = _get(engine, "save_database")

        save_database()

    report["containers"] = _snapshot_containers(engine, data_dir)
    report["sqlite"] = _dump_sqlite(data_dir)
    report["json_files"] = _dump_json_files(data_dir)

    _normalise_paths(report, data_dir)

    status = _get(engine, "INDEX_STATUS")

    report["status"] = {
        "state": status.get("state"),
        "generation": status.get("generation"),
    }

    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("new", "legacy"), required=True)
    parser.add_argument("--base", default=None)
    parser.add_argument("--data", required=True)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--phase", choices=PHASES, required=True)
    parser.add_argument("--delete", action="append", default=[])
    parser.add_argument("--bulk-delete", action="append", default=[])
    parser.add_argument("--reindex-dir", default=None)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--reindex", action="append", default=[])
    parser.add_argument("--out", required=True)

    arguments = parser.parse_args(argv)

    os.environ["SEARCH_ENGINE_DATA_DIR"] = str(arguments.data)

    # The engine resolves its data folder at import, so this must be set
    # before the module is loaded.
    sys.path.insert(0, str(REPO_ROOT))

    if arguments.engine == "legacy":
        if not arguments.base:
            raise ProbeError("--base is required for the legacy engine")

        engine = _load_legacy_engine(arguments.base)
    else:
        engine = _load_engine()

    report = run_phase(
        engine,
        arguments.data,
        arguments.corpus,
        arguments.phase,
        deletes=arguments.delete,
        bulk_deletes=arguments.bulk_delete,
        reindex_dir=arguments.reindex_dir,
        reindex=arguments.reindex,
        rebuild=arguments.rebuild,
    )

    Path(arguments.out).write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
