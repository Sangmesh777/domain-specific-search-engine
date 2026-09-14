"""
Filename token order and repetition must survive a restart.

Two independent defects used to break this, and both changed
`normalized_filename`, which `execute_search` builds as
`" ".join(filename_words)` and then tests with `in`:

* the writers inserted `set(filename_words)`, so the stored order was
  set-iteration order and therefore different in every process;
* the table's primary key was `(filename, term)` and the insert was
  `INSERT OR IGNORE`, so a repeated token was silently collapsed.

A quoted phrase is filtered on `phrase_match`, so either defect turns a
hit into no hit at all. `network security notes.txt` stopped answering
`"network security notes"` after a restart, and `repeat repeat.txt`
stopped answering `"repeat repeat"`.

These tests run the engine in a subprocess, because a restart has to be
a genuinely new process or it is not testing reconstruction.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def engine_driver(tmp_path_factory):
    """Write a driver that indexes a file, searches, and prints JSON."""

    path = tmp_path_factory.mktemp("driver") / "drive.py"

    path.write_text(
        '''
import json
import os
import sys

sys.path.insert(0, os.environ["REPO_ROOT"])

import app

PHASE = sys.argv[1]
NAME = sys.argv[2]
CORPUS = sys.argv[3]
QUERY = sys.argv[4]

if PHASE == "ingest":
    app.incrementally_index_document(NAME, os.path.join(CORPUS, NAME))
elif PHASE == "delete":
    app.incrementally_remove_document(NAME)

client = app.app.test_client()

response = client.get("/api/search", query_string={"q": QUERY})
body = response.get_json()
results = body["results"] if isinstance(body, dict) else body

print(json.dumps({
    "filename_index": app.FILENAME_INDEX.get(NAME),
    "normalized_filename": " ".join(app.FILENAME_INDEX.get(NAME, [])),
    "hits": [hit["title"] for hit in results],
    "status": app.INDEX_STATUS["state"],
}))
''',
        encoding="utf-8",
    )

    return path


def run_engine(driver, phase, name, corpus, query, data_dir, seed=None):
    """Run one engine process and return its JSON report."""

    environment = dict(os.environ)
    environment["SEARCH_ENGINE_DATA_DIR"] = str(data_dir)
    environment["REPO_ROOT"] = str(REPO_ROOT)

    if seed is not None:
        environment["PYTHONHASHSEED"] = str(seed)

    completed = subprocess.run(
        [sys.executable, str(driver), phase, name, str(corpus), query],
        capture_output=True,
        text=True,
        timeout=300,
        env=environment,
        cwd=str(REPO_ROOT),
    )

    assert completed.returncode == 0, (
        f"engine failed:\n{completed.stdout[-2000:]}\n"
        f"{completed.stderr[-2000:]}"
    )

    # The engine prints its startup banner before the payload.
    payload = completed.stdout[
        completed.stdout.index("{"):
    ]

    return json.loads(payload)


def write_corpus(directory, name, text="body text only\n"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")
    return directory


def stored_filename_terms(data_dir, filename):
    """Return the stored (position, term) rows for one document."""

    connection = sqlite3.connect(str(Path(data_dir) / "search.db"))

    try:
        return connection.execute(
            """
            SELECT position, term
            FROM filename_terms
            WHERE filename = ?
            ORDER BY position
            """,
            (filename,),
        ).fetchall()

    finally:
        connection.close()


# ----------------------------------------------------------------------
# The two reported failures
# ----------------------------------------------------------------------


# Tokens chosen so that set-iteration order under a fixed seed is NOT the
# natural order. With a filename whose set order happens to be
# alphabetical the defect is invisible, which is how it survived.
@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5, 6, 7])
def test_quoted_filename_phrase_survives_a_restart(
    engine_driver, tmp_path, seed
):
    """
    A phrase that appears only in the filename must match before and
    after a restart, on every hash seed.
    """

    name = "zulu alpha mike bravo.txt"
    query = '"zulu alpha mike bravo"'

    corpus = write_corpus(tmp_path / "corpus", name)
    data_dir = tmp_path / "data"

    before = run_engine(
        engine_driver, "ingest", name, corpus, query, data_dir, seed=seed
    )
    after = run_engine(
        engine_driver, "restart", name, corpus, query, data_dir, seed=seed
    )

    assert before["filename_index"] == ["zulu", "alpha", "mike", "bravo"]

    assert before["hits"] == [name], (
        f"seed {seed}: the document did not match before the restart"
    )
    assert after["hits"] == [name], (
        f"seed {seed}: after the restart the quoted filename phrase "
        f"stopped matching (filename_index={after['filename_index']})"
    )
    assert after["normalized_filename"] == before["normalized_filename"]


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5])
def test_repeated_filename_tokens_survive_a_restart(
    engine_driver, tmp_path, seed
):
    """
    A repeated token must not be collapsed.

    The primary key used to be (filename, term) with INSERT OR IGNORE,
    so `repeat repeat` became `repeat` and its quoted phrase stopped
    matching.
    """

    name = "repeat repeat.txt"
    query = '"repeat repeat"'

    corpus = write_corpus(tmp_path / "corpus", name)
    data_dir = tmp_path / "data"

    before = run_engine(
        engine_driver, "ingest", name, corpus, query, data_dir, seed=seed
    )
    after = run_engine(
        engine_driver, "restart", name, corpus, query, data_dir, seed=seed
    )

    assert before["filename_index"] == ["repeat", "repeat"]
    assert after["filename_index"] == ["repeat", "repeat"], (
        "the repeated token was collapsed across the restart"
    )

    assert before["hits"] == [name]
    assert after["hits"] == [name], (
        f"seed {seed}: the repeated-token phrase stopped matching"
    )


# ----------------------------------------------------------------------
# SQLite rows
# ----------------------------------------------------------------------


def test_stored_rows_match_the_natural_token_order(
    engine_driver, tmp_path
):
    """
    Position is explicit, so the stored sequence is inspectable without
    relying on rowid.
    """

    name = "chapter appendix index glossary.txt"

    corpus = write_corpus(tmp_path / "corpus", name)
    data_dir = tmp_path / "data"

    run_engine(
        engine_driver, "ingest", name, corpus, '"chapter"', data_dir
    )

    rows = stored_filename_terms(data_dir, name)

    assert rows == [
        (0, "chapter"),
        (1, "appendix"),
        (2, "index"),
        (3, "glossary"),
    ]


def test_positions_are_contiguous_and_unique(engine_driver, tmp_path):
    """A gap or a repeated position would misorder the reconstruction."""

    name = "repeat repeat.txt"

    corpus = write_corpus(tmp_path / "corpus", name)
    data_dir = tmp_path / "data"

    run_engine(
        engine_driver, "ingest", name, corpus, '"repeat"', data_dir
    )

    rows = stored_filename_terms(data_dir, name)

    assert [position for position, _term in rows] == list(
        range(len(rows))
    )
    assert rows == [(0, "repeat"), (1, "repeat")]


# ----------------------------------------------------------------------
# Replacement, delete and re-add
# ----------------------------------------------------------------------


def test_replacement_writes_the_new_filename_order(
    engine_driver, tmp_path
):
    """
    A re-upload deletes the old rows first, so no stale order survives.
    """

    name = "zulu alpha mike bravo.txt"
    replacement = "bravo mike alpha zulu.txt"

    corpus = write_corpus(tmp_path / "corpus", name)
    data_dir = tmp_path / "data"

    run_engine(engine_driver, "ingest", name, corpus, '"zulu"', data_dir)

    # Same document name, new content: the filename is what decides the
    # stored tokens, so index a differently-named document under the
    # same stored name by rewriting the file.
    (corpus / name).write_text("different body\n", encoding="utf-8")

    run_engine(engine_driver, "ingest", name, corpus, '"zulu"', data_dir)

    rows = stored_filename_terms(data_dir, name)

    assert [term for _position, term in rows] == [
        "zulu",
        "alpha",
        "mike",
        "bravo",
    ]

    # And a genuinely different name must not inherit the old order.
    write_corpus(corpus, replacement)

    run_engine(
        engine_driver, "ingest", replacement, corpus, '"bravo"', data_dir
    )

    assert [
        term for _position, term in stored_filename_terms(
            data_dir, replacement
        )
    ] == ["bravo", "mike", "alpha", "zulu"]


def test_delete_then_re_add_has_no_stale_rows(engine_driver, tmp_path):
    """Deleting must clear the rows, so a re-add cannot inherit them."""

    name = "zulu alpha mike bravo.txt"

    corpus = write_corpus(tmp_path / "corpus", name)
    data_dir = tmp_path / "data"

    run_engine(engine_driver, "ingest", name, corpus, '"zulu"', data_dir)
    assert stored_filename_terms(data_dir, name)

    run_engine(engine_driver, "delete", name, corpus, '"zulu"', data_dir)
    assert stored_filename_terms(data_dir, name) == []

    run_engine(engine_driver, "ingest", name, corpus, '"zulu"', data_dir)

    rows = stored_filename_terms(data_dir, name)

    assert [term for _position, term in rows] == [
        "zulu",
        "alpha",
        "mike",
        "bravo",
    ]


def test_order_survives_a_rebuild(engine_driver, tmp_path):
    """
    A full rebuild rewrites every row and must keep the order.

    The document has to live in the data folder, because
    `rebuild_database` indexes that directory rather than the corpus it
    was uploaded from - so the file is written there and indexed in
    place, which is what an upload does.
    """

    name = "chapter appendix index glossary.txt"

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    corpus = write_corpus(data_dir, name)

    run_engine(engine_driver, "ingest", name, corpus, '"chapter"', data_dir)

    driver_script = tmp_path / "rebuild.py"
    driver_script.write_text(
        """
import os
import sys

sys.path.insert(0, os.environ["REPO_ROOT"])

import app

app.rebuild_database()
print("rebuilt")
""",
        encoding="utf-8",
    )

    environment = dict(os.environ)
    environment["SEARCH_ENGINE_DATA_DIR"] = str(data_dir)
    environment["REPO_ROOT"] = str(REPO_ROOT)

    completed = subprocess.run(
        [sys.executable, str(driver_script)],
        capture_output=True,
        text=True,
        timeout=300,
        env=environment,
        cwd=str(REPO_ROOT),
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "rebuilt" in completed.stdout

    rows = stored_filename_terms(data_dir, name)

    assert [term for _position, term in rows] == [
        "chapter",
        "appendix",
        "index",
        "glossary",
    ]


# ----------------------------------------------------------------------
# Migration of an already-corrupted database
# ----------------------------------------------------------------------


OLD_SCHEMA = """
CREATE TABLE documents (
    filename TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    path TEXT NOT NULL,
    total_words INTEGER NOT NULL,
    page_count INTEGER NOT NULL
);

CREATE TABLE filename_terms (
    filename TEXT NOT NULL,
    term TEXT NOT NULL,
    PRIMARY KEY (filename, term)
);
"""


def build_corrupted_database(path, documents):
    """
    Write a database in the pre-fix shape: old primary key, and rows
    deliberately stored in a scrambled order with duplicates collapsed.
    """

    connection = sqlite3.connect(str(path))
    connection.executescript(OLD_SCHEMA)

    for filename, scrambled in documents:
        connection.execute(
            """
            INSERT INTO documents
            (filename, title, path, total_words, page_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            (filename, filename, f"/data/{filename}", 2, 1),
        )

        connection.executemany(
            "INSERT INTO filename_terms (filename, term) VALUES (?, ?)",
            [(filename, term) for term in scrambled],
        )

    connection.commit()
    connection.close()


def read_migration_report(driver, data_dir, corpus, name="probe.txt"):
    """Start an engine over `data_dir` and return what it printed."""

    environment = dict(os.environ)
    environment["SEARCH_ENGINE_DATA_DIR"] = str(data_dir)
    environment["REPO_ROOT"] = str(REPO_ROOT)

    started = subprocess.run(
        [
            sys.executable,
            str(driver),
            "restart",
            name,
            str(corpus),
            '"probe"',
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=environment,
        cwd=str(REPO_ROOT),
    )

    return started


def test_an_already_corrupted_database_is_repaired(
    engine_driver, tmp_path
):
    """
    Existing databases carry the scrambled order and the collapsed
    duplicates. A repair must fix both, from the document names alone,
    and must report what it did.
    """

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    corpus = write_corpus(tmp_path / "corpus", "unused.txt")

    build_corrupted_database(
        data_dir / "search.db",
        [
            # Scrambled order and a collapsed duplicate.
            ("zulu alpha mike bravo.txt", ["mike", "zulu", "bravo"]),
            ("repeat repeat.txt", ["repeat"]),
            ("chapter appendix index glossary.txt", ["glossary", "chapter"]),
        ],
    )

    first = read_migration_report(engine_driver, data_dir, corpus)

    assert first.returncode == 0, first.stderr[-3000:]
    assert "filename_terms migrated" in first.stdout, (
        "the repair did not run over a corrupted database"
    )
    assert "schema_changed=True" in first.stdout
    assert "repaired=3" in first.stdout

    assert [
        term for _position, term in stored_filename_terms(
            data_dir, "zulu alpha mike bravo.txt"
        )
    ] == ["zulu", "alpha", "mike", "bravo"]

    assert [
        term for _position, term in stored_filename_terms(
            data_dir, "repeat repeat.txt"
        )
    ] == ["repeat", "repeat"], "the collapsed duplicate was not restored"

    assert [
        term for _position, term in stored_filename_terms(
            data_dir, "chapter appendix index glossary.txt"
        )
    ] == ["chapter", "appendix", "index", "glossary"]

    columns = sqlite3.connect(
        str(data_dir / "search.db")
    ).execute("PRAGMA table_info(filename_terms)").fetchall()

    assert [column[1] for column in columns] == [
        "filename",
        "position",
        "term",
    ]

    # Second start: nothing left to do, and nothing written.
    second = read_migration_report(engine_driver, data_dir, corpus)

    assert second.returncode == 0
    assert "filename_terms migrated" not in second.stdout, (
        "the repair ran again on an already-correct database"
    )


def test_the_repair_removes_rows_for_vanished_documents(
    engine_driver, tmp_path
):
    """
    A row whose document no longer exists would be served as a filename
    token for a document that is not there.
    """

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    corpus = write_corpus(tmp_path / "corpus", "unused.txt")

    build_corrupted_database(
        data_dir / "search.db",
        [("present document.txt", ["document", "present"])],
    )

    connection = sqlite3.connect(str(data_dir / "search.db"))
    connection.execute(
        "INSERT INTO filename_terms (filename, term) VALUES (?, ?)",
        ("vanished.txt", "vanished"),
    )
    connection.commit()
    connection.close()

    completed = read_migration_report(engine_driver, data_dir, corpus)

    assert completed.returncode == 0
    assert stored_filename_terms(data_dir, "vanished.txt") == []


def test_a_fresh_database_is_not_reported_as_migrated(
    engine_driver, tmp_path
):
    """A clean install must not claim to have repaired anything."""

    name = "chapter appendix index glossary.txt"

    corpus = write_corpus(tmp_path / "corpus", name)
    data_dir = tmp_path / "data"

    first = read_migration_report(
        engine_driver, data_dir, corpus, name=name
    )

    assert first.returncode == 0
    assert "filename_terms migrated" not in first.stdout

    columns = sqlite3.connect(
        str(data_dir / "search.db")
    ).execute("PRAGMA table_info(filename_terms)").fetchall()

    assert [column[1] for column in columns] == [
        "filename",
        "position",
        "term",
    ]


# ----------------------------------------------------------------------
# The reader follows `position`, not rowid
# ----------------------------------------------------------------------


def test_the_reader_follows_position_not_rowid(tmp_path):
    """
    Reconstruction must use the stored positions.

    Rows are written in order, so rowid order normally coincides with
    position order and the two are indistinguishable end to end. They
    are only distinguishable when the rows arrive out of rowid order
    relative to their positions, which is what this builds directly.
    Without it, swapping the reader back to `ORDER BY rowid` would be
    invisible to the whole suite - the position column would look
    decorative while the order silently depended on insertion again.
    """

    from search_engine import storage

    db_path = str(tmp_path / "search.db")

    with storage.get_sqlite_connection(db_path) as connection:
        storage.create_schema(connection)

        connection.execute(
            """
            INSERT INTO documents
            (filename, title, path, total_words, page_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("out of order.txt", "out of order.txt", "/data/x", 1, 1),
        )

        # Inserted index-first, so rowid order is the REVERSE of the
        # order the positions describe.
        connection.executemany(
            """
            INSERT INTO filename_terms
            (filename, position, term)
            VALUES (?, ?, ?)
            """,
            [
                ("out of order.txt", 2, "index"),
                ("out of order.txt", 0, "chapter"),
                ("out of order.txt", 1, "appendix"),
            ],
        )

        connection.commit()

    snapshot = storage.load_database_from_sqlite(db_path=db_path)

    assert snapshot is not None

    assert snapshot["filename_index"]["out of order.txt"] == [
        "chapter",
        "appendix",
        "index",
    ], (
        "reconstruction followed rowid instead of position"
    )
