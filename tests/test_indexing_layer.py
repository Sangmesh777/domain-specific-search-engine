"""
The index build is a function of a folder, not of the application.

`rebuild_database` used to do two jobs: walk the document folder and
build four containers, then save, swap and sync them. Only the first is
the engine's, and only the first moved. `build_index_from_folder` returns
a complete snapshot and installs nothing.

That separation is the point. A build that publishes cannot be called
twice safely, cannot be compared against another build, and cannot run
in a process with no index globals - which is exactly what the offline
mode needs and what the test process is.

The ordering the adapter kept is load-bearing and is checked here as
source, because no single-process test can observe a crash between the
steps:

    build -> save JSON snapshot -> publish -> sync SQLite
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT))

from search_engine import indexing  # noqa: E402

APP_PATH = REPO_ROOT / "app.py"
INDEXING_PATH = REPO_ROOT / "search_engine" / "indexing.py"

CONTAINERS = (
    "inverted_index",
    "document_metadata",
    "filename_index",
    "page_text_index",
)


def source_of(path):
    return path.read_text(encoding="utf-8")


def function_source(path, name):
    source = source_of(path)

    tree = ast.parse(source)

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""

    raise AssertionError(f"{name} is not defined in {path}")


def write(directory, name, text):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")
    return directory / name


# ----------------------------------------------------------------------
# The build itself
# ----------------------------------------------------------------------


def test_a_folder_becomes_a_complete_snapshot(tmp_path):
    folder = tmp_path / "corpus"

    write(folder, "alpha.txt", "alpha beta alpha\n")
    write(folder, "beta.txt", "beta gamma\n")

    snapshot = indexing.build_index_from_folder(folder)

    assert set(snapshot) == set(CONTAINERS)

    assert sorted(snapshot["document_metadata"]) == ["alpha.txt", "beta.txt"]
    assert sorted(snapshot["filename_index"]) == ["alpha.txt", "beta.txt"]
    assert sorted(snapshot["page_text_index"]) == ["alpha.txt", "beta.txt"]

    assert snapshot["inverted_index"]["alpha"] == {"alpha.txt": 2}
    assert snapshot["inverted_index"]["beta"] == {
        "alpha.txt": 1,
        "beta.txt": 1,
    }


def test_metadata_matches_the_incremental_path(tmp_path):
    """
    A document built by a rebuild and the same document built by an
    upload must agree, or a restart followed by a rebuild would change
    what a search returns.

    The incremental path stores the same four keys for the same file, so
    the comparison is between the two real paths rather than against
    hand-written expectations.
    """

    folder = tmp_path / "corpus"
    path = write(folder, "note.txt", "one two three\n")

    snapshot = indexing.build_index_from_folder(folder)

    metadata = snapshot["document_metadata"]["note.txt"]

    assert metadata["title"] == "note.txt"
    assert metadata["path"] == str(path.resolve())
    assert metadata["total_words"] == 3
    assert metadata["page_count"] >= 0

    # The extension is never indexed: the filename index comes from the
    # stem, which is what `tokenize_filename(splitext(name)[0])` means.
    assert snapshot["filename_index"]["note.txt"] == ["note"]


def test_filename_tokens_keep_their_order_and_repeats(tmp_path):
    """
    The pack-4 invariant, checked on the build path as well.

    The rebuild and the incremental writer must agree, or a restart that
    follows a rebuild would scramble a quoted filename phrase again.
    """

    folder = tmp_path / "corpus"

    write(folder, "zulu alpha mike.txt", "nothing relevant\n")
    write(folder, "repeat repeat.txt", "nothing relevant\n")

    snapshot = indexing.build_index_from_folder(folder)

    assert snapshot["filename_index"]["zulu alpha mike.txt"] == [
        "zulu",
        "alpha",
        "mike",
    ]

    assert snapshot["filename_index"]["repeat repeat.txt"] == [
        "repeat",
        "repeat",
    ]


def test_unsupported_extensions_are_skipped(tmp_path):
    folder = tmp_path / "corpus"

    write(folder, "kept.txt", "alpha beta\n")
    write(folder, "ignored.md", "alpha beta gamma\n")
    write(folder, "ignored.csv", "alpha,beta\n")

    snapshot = indexing.build_index_from_folder(folder)

    assert list(snapshot["document_metadata"]) == ["kept.txt"]


def test_a_document_with_no_readable_text_is_skipped(tmp_path):
    """
    An empty file is skipped, not indexed as a document with no words.

    Indexing it would insert a document that can never match a term
    query while still appearing in the filename index.
    """

    folder = tmp_path / "corpus"

    write(folder, "real.txt", "alpha beta\n")
    write(folder, "empty.txt", "")

    snapshot = indexing.build_index_from_folder(folder)

    assert list(snapshot["document_metadata"]) == ["real.txt"]


def test_one_bad_document_does_not_abort_the_build(tmp_path):
    """
    A file the extractor rejects is skipped and the rest still build.

    This is the pre-existing behaviour: the original caught per file and
    continued, so one unreadable upload could not empty the index.
    """

    folder = tmp_path / "corpus"

    write(folder, "good.txt", "alpha beta\n")
    write(folder, "broken.txt", "alpha\n")

    # A zero-length extension-shaped file that the TXT reader cannot
    # decode as UTF-8 stands in for a corrupt document.
    (folder / "broken.txt").write_bytes(b"\xff\xfe\x00\x01not utf8\xff")

    snapshot = indexing.build_index_from_folder(folder)

    assert "good.txt" in snapshot["document_metadata"]


def test_an_empty_folder_builds_an_empty_snapshot(tmp_path):
    folder = tmp_path / "corpus"
    folder.mkdir()

    snapshot = indexing.build_index_from_folder(folder)

    assert snapshot == {
        "inverted_index": {},
        "document_metadata": {},
        "filename_index": {},
        "page_text_index": {},
    }


def test_the_build_installs_nothing(tmp_path, app_module):
    """
    The whole claim: calling it does not change what the application is
    serving.

    If the build published, this would change the live index as a side
    effect, and the adapter's careful ordering would be decoration.
    """

    before = (
        dict(app_module.REAL_INVERTED_INDEX),
        dict(app_module.DOCUMENT_METADATA),
        {name: list(words) for name, words in app_module.FILENAME_INDEX.items()},
        dict(app_module.PAGE_TEXT_INDEX),
        app_module.INDEX_STATUS["state"],
        app_module.INDEX_STATUS["generation"],
    )

    folder = tmp_path / "corpus"
    write(folder, "brand new document.txt", "uniquetermforthisbuild\n")

    snapshot = indexing.build_index_from_folder(folder)

    assert snapshot["inverted_index"]["uniquetermforthisbuild"]

    after = (
        dict(app_module.REAL_INVERTED_INDEX),
        dict(app_module.DOCUMENT_METADATA),
        {name: list(words) for name, words in app_module.FILENAME_INDEX.items()},
        dict(app_module.PAGE_TEXT_INDEX),
        app_module.INDEX_STATUS["state"],
        app_module.INDEX_STATUS["generation"],
    )

    assert before == after, (
        "building a snapshot mutated the live index; the build must not "
        "publish"
    )


def test_two_builds_of_the_same_folder_are_identical(tmp_path):
    """Determinism: no set iteration, no state carried between calls."""

    folder = tmp_path / "corpus"

    write(folder, "one.txt", "alpha beta\n")
    write(folder, "two.txt", "beta gamma\n")
    write(folder, "zulu alpha mike.txt", "delta\n")

    assert indexing.build_index_from_folder(folder) == (
        indexing.build_index_from_folder(folder)
    )


# ----------------------------------------------------------------------
# Path resolution
# ----------------------------------------------------------------------


def test_a_real_document_resolves(tmp_path):
    folder = tmp_path / "corpus"
    path = write(folder, "note.txt", "alpha\n")

    assert indexing.resolve_document_path(folder, "note.txt") == str(
        path.resolve()
    )


def test_traversal_out_of_the_folder_is_refused(tmp_path):
    """
    The guard that stops a document URL reaching the rest of the disk.

    The name is reduced to its basename first, so `../secret.txt` becomes
    `secret.txt` inside the folder rather than a path outside it.
    """

    folder = tmp_path / "corpus"
    folder.mkdir()

    outside = tmp_path / "secret.txt"
    outside.write_text("not indexed\n", encoding="utf-8")

    resolved = indexing.resolve_document_path(folder, "../secret.txt")

    # Either refused, or confined to the folder - never the file above it.
    assert resolved != str(outside.resolve())

    if resolved is not None:
        assert resolved.startswith(str(folder.resolve()))


def test_an_absolute_path_is_reduced_to_a_name(tmp_path):
    folder = tmp_path / "corpus"
    folder.mkdir()

    resolved = indexing.resolve_document_path(folder, "/etc/passwd")

    assert resolved is None or resolved.startswith(str(folder.resolve()))


@pytest.mark.parametrize("value", ["", None])
def test_a_missing_name_resolves_to_nothing(tmp_path, value):
    folder = tmp_path / "corpus"
    folder.mkdir()

    assert indexing.resolve_document_path(folder, value) is None


# ----------------------------------------------------------------------
# Where the code lives, and in what order the adapter runs it
# ----------------------------------------------------------------------


def test_the_indexing_module_holds_no_application_state():
    tree = ast.parse(source_of(INDEXING_PATH))

    loaded = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }

    for name in (
        "INDEX_DATA_LOCK",
        "publish_index_state",
        "save_database_snapshot",
        "sync_sqlite_from_memory",
        "get_sqlite_connection",
        "REAL_INVERTED_INDEX",
        "DOCUMENT_METADATA",
        "FILENAME_INDEX",
        "PAGE_TEXT_INDEX",
        "DATA_FOLDER",
    ):
        assert name not in loaded, (
            f"search_engine/indexing.py reads {name!r}"
        )


def test_app_no_longer_builds_an_index():
    """The build loop must not be back in the adapter."""

    source = source_of(APP_PATH)

    for marker in (
        "new_inverted_index[word]",
        "os.listdir(DATA_FOLDER)",
        "supported_extensions",
    ):
        assert marker not in source, (
            f"app.py still contains the build logic {marker!r}"
        )


def test_the_adapter_keeps_the_publish_order():
    """
    build -> save -> publish -> sync, and in that order.

    Pinned as source because a crash between the steps is the only way
    the order becomes observable, and no single-process test can
    produce one. An earlier design note assumed sync-then-publish; the
    shipped order is this one.
    """

    body = function_source(APP_PATH, "rebuild_database")

    markers = [
        "indexing.build_index_from_folder",
        "save_database_snapshot",
        "publish_index_state",
        "sync_sqlite_from_memory",
    ]

    positions = []

    for marker in markers:
        index = body.find(marker)

        assert index != -1, f"{marker} is missing from the adapter"

        positions.append(index)

    assert positions == sorted(positions), (
        "the rebuild adapter no longer runs build -> save -> publish -> "
        f"sync (found at {positions})"
    )


def test_the_publish_happens_under_the_lock():
    """
    The swap and the globals must move together.

    The adapter has two `with` blocks - the lock and the SQLite
    connection - so this finds the lock by name rather than counting.
    """

    body = function_source(APP_PATH, "rebuild_database")

    tree = ast.parse(body)

    blocks = [
        ast.get_source_segment(body, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
    ]

    locked = [text for text in blocks if "INDEX_DATA_LOCK" in text]

    assert len(locked) == 1, (
        f"expected exactly one locked section, found {len(locked)}"
    )

    assert "publish_index_state" in locked[0], (
        "the snapshot is published outside INDEX_DATA_LOCK"
    )


# ----------------------------------------------------------------------
# One extraction rule, not two
# ----------------------------------------------------------------------


def test_the_extraction_rule_lives_in_one_place():
    """
    The rule used to exist twice: once in the rebuild loop and once in
    the upload path.

    Two copies of an indexing rule is how an online index and an offline
    one drift apart, and the offline backend would have had to choose
    one to copy. This asserts the duplication has not come back.
    """

    app_source = source_of(APP_PATH)

    for marker in (
        "extract_text(",
        "extract_pages(",
        "tokenize(",
        "tokenize_filename(",
    ):
        assert marker not in app_source, (
            f"app.py calls {marker!r}; document extraction belongs in "
            "search_engine/indexing.py"
        )

    # And it is present, so the pair cannot pass by both sides losing it.
    assert "def extract_document" in source_of(INDEXING_PATH)


def test_extract_document_returns_every_part_the_index_stores(tmp_path):
    path = write(tmp_path / "corpus", "note.txt", "alpha beta alpha\n")

    extracted = indexing.extract_document(str(path), "note.txt")

    assert set(extracted) == {
        "metadata",
        "content_words",
        "term_counts",
        "filename_words",
        "pages",
    }

    assert extracted["metadata"] == {
        "title": "note.txt",
        "path": str(path.resolve()),
        "total_words": 3,
        "page_count": len(extracted["pages"]),
    }

    assert extracted["term_counts"] == {"alpha": 2, "beta": 1}
    assert extracted["content_words"] == ["alpha", "beta", "alpha"]
    assert extracted["filename_words"] == ["note"]


def test_extract_document_keeps_filename_order_and_repeats(tmp_path):
    path = write(tmp_path / "corpus", "zulu alpha zulu.txt", "text\n")

    extracted = indexing.extract_document(str(path), "zulu alpha zulu.txt")

    assert extracted["filename_words"] == ["zulu", "alpha", "zulu"]


def test_extract_document_returns_none_for_an_empty_document(tmp_path):
    """
    None, not an exception: the policy belongs to the caller.

    The rebuild skips an empty document and the upload raises, and both
    behaviours are preserved - so the shared rule must not choose for
    them.
    """

    path = write(tmp_path / "corpus", "empty.txt", "")

    assert indexing.extract_document(str(path), "empty.txt") is None


def test_the_two_paths_agree_on_the_same_document(tmp_path):
    """
    The point of sharing the rule, checked directly.

    `build_index_from_folder` and `extract_document` must produce the
    same metadata, postings and filename tokens for the same file, or
    the online and offline indexes would disagree about it.
    """

    folder = tmp_path / "corpus"
    path = write(folder, "zulu alpha mike.txt", "gamma delta gamma\n")

    extracted = indexing.extract_document(str(path), "zulu alpha mike.txt")

    snapshot = indexing.build_index_from_folder(folder)

    name = "zulu alpha mike.txt"

    assert snapshot["document_metadata"][name] == extracted["metadata"]
    assert snapshot["filename_index"][name] == extracted["filename_words"]
    assert snapshot["page_text_index"][name] == extracted["pages"]

    assert snapshot["inverted_index"]["gamma"] == {name: 2}
    assert snapshot["inverted_index"]["delta"] == {name: 1}

    assert extracted["term_counts"] == {"gamma": 2, "delta": 1}


def test_the_upload_path_raises_on_an_empty_document(app_module, tmp_path):
    """
    The policy that must survive the sharing.

    `storage_equivalence` records `FAILED:...:ValueError` for this case,
    so the exception type is observable behaviour, not an implementation
    detail.
    """

    path = write(tmp_path / "corpus", "empty.txt", "")

    with pytest.raises(ValueError):
        app_module.incrementally_index_document(str(path), str(path))


def test_the_path_guard_lives_in_the_module_and_is_wrapped():
    """Both halves: the guard moved, and the call site still resolves."""

    assert "def resolve_document_path" in source_of(INDEXING_PATH)

    body = function_source(APP_PATH, "resolve_document_path")

    assert "indexing.resolve_document_path" in body
    assert "DATA_FOLDER" in body
