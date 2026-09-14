"""
Restart, recovery and index-state invariants.

Every command runs in a **fresh Python process**, so these tests exercise
real process restart rather than reusing module state.

The engine keeps its index status in memory on purpose. This suite
verifies that consequence: a restart must never come back wedged in
``INDEXING``, and the previous complete snapshot must remain
searchable.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

PROBE = REPO_ROOT / "tests" / "engine_probe.py"

CORPUS = {
    "one.txt": "alpha beta gamma\ntargetterm here\nsharedmarker\n",
    "two.txt": "delta epsilon\ntargetterm also\n",
    "three.txt": "zeta eta theta\nsharedmarker only\n",
}


def run_probe(data_dir, *arguments, timeout=180):
    """Run the probe in a fresh process and return its JSON result."""

    completed = subprocess.run(
        [sys.executable, str(PROBE), str(data_dir), *map(str, arguments)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if completed.returncode != 0:
        raise AssertionError(
            f"probe {' '.join(map(str, arguments))} failed with "
            f"exit code {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )

    lines = [
        line
        for line in completed.stdout.splitlines()
        if line.strip().startswith("{")
    ]

    if not lines:
        raise AssertionError(
            f"probe produced no JSON result:\n{completed.stdout}"
        )

    return json.loads(lines[-1])


@pytest.fixture
def corpus_dir(tmp_path):
    directory = tmp_path / "corpus"
    directory.mkdir()

    for name, text in CORPUS.items():
        (directory / name).write_text(text, encoding="utf-8")

    return directory


@pytest.fixture
def indexed(tmp_path, corpus_dir):
    """Return (data_dir, upload_result) for an indexed corpus."""

    data_dir = tmp_path / "data"
    result = run_probe(data_dir, "corpus", corpus_dir)

    assert result["failed"] == []
    assert result["documents"] == len(CORPUS)

    return data_dir, result


# ----------------------------------------------------------------------
# Restart
# ----------------------------------------------------------------------


def test_restart_reloads_the_snapshot(indexed):
    data_dir, upload = indexed

    status = run_probe(data_dir, "status")

    assert status["documents"] == upload["documents"]
    assert status["content_terms"] == upload["content_terms"]
    assert status["filenames_indexed"] == upload["filenames_indexed"]
    assert status["page_text_entries"] == upload["page_text_entries"]


def test_restart_reports_ready_not_stuck_indexing(indexed):
    """A restart must never resume an INDEXING state."""

    data_dir, _upload = indexed

    status = run_probe(data_dir, "status")

    assert status["indexing"]["state"] == "READY"
    assert status["indexing"]["last_error"] is None


def test_index_status_is_not_persisted(indexed):
    """
    Status lives in memory, so a fresh process starts a fresh
    generation counter instead of resuming the previous one.
    """

    data_dir, upload = indexed

    assert upload["indexing"]["generation"] > 1

    restarted = run_probe(data_dir, "status")

    assert restarted["indexing"]["state"] == "READY"


def test_search_is_identical_across_restart(indexed):
    data_dir, _upload = indexed

    before = run_probe(data_dir, "search", "targetterm")

    # Force a full process restart between the two searches.
    _ = run_probe(data_dir, "status")

    after = run_probe(data_dir, "search", "targetterm")

    assert before["titles"] == after["titles"]
    assert before["scores"] == after["scores"]
    assert before["shape"] == after["shape"]


def test_documents_are_listed_after_restart(indexed):
    data_dir, _upload = indexed

    listed = run_probe(data_dir, "list")

    assert set(listed["documents"]) == set(CORPUS)


# ----------------------------------------------------------------------
# Bulk delete
# ----------------------------------------------------------------------


def test_bulk_delete_removes_exactly_the_requested_documents(indexed):
    data_dir, _upload = indexed

    result = run_probe(data_dir, "bulk-delete", "one.txt", "two.txt")

    assert result["deleted_count"] == 2

    remaining = run_probe(data_dir, "list")["documents"]

    assert remaining == ["three.txt"]


def test_bulk_delete_tolerates_unknown_names(indexed):
    """
    A bulk delete mixing known and unknown names must delete exactly
    the known ones and must not corrupt the index.
    """

    data_dir, _upload = indexed

    result = run_probe(
        data_dir,
        "bulk-delete",
        "one.txt",
        "does_not_exist.txt",
        "three.txt",
    )

    assert result["deleted_count"] == 2
    assert result["not_found"] == ["does_not_exist.txt"]

    remaining = run_probe(data_dir, "list")["documents"]

    assert remaining == ["two.txt"]

    status = run_probe(data_dir, "status")

    assert status["documents"] == 1
    assert status["indexing"]["state"] == "READY"


def test_bulk_delete_leaves_a_searchable_snapshot(indexed):
    data_dir, _upload = indexed

    run_probe(data_dir, "bulk-delete", "one.txt", "three.txt")

    search = run_probe(data_dir, "search", "targetterm")

    assert search["titles"] == ["two.txt"]


def test_bulk_delete_of_nothing_is_a_no_op(indexed):
    data_dir, upload = indexed

    result = run_probe(data_dir, "bulk-delete", "missing_a.txt", "missing_b.txt")

    assert result["deleted_count"] == 0

    status = run_probe(data_dir, "status")

    assert status["documents"] == upload["documents"]


def test_repeated_bulk_delete_is_idempotent(indexed):
    data_dir, _upload = indexed

    first = run_probe(data_dir, "bulk-delete", "one.txt")
    second = run_probe(data_dir, "bulk-delete", "one.txt")

    assert first["deleted_count"] == 1
    assert second["deleted_count"] == 0

    assert run_probe(data_dir, "list")["documents"] == ["three.txt", "two.txt"]


# ----------------------------------------------------------------------
# Single delete
# ----------------------------------------------------------------------


def test_single_delete_removes_the_document(indexed):
    data_dir, _upload = indexed

    run_probe(data_dir, "delete", "two.txt")

    listed = run_probe(data_dir, "list")["documents"]

    assert listed == ["one.txt", "three.txt"]

    search = run_probe(data_dir, "search", "targetterm")

    assert search["titles"] == ["one.txt"]


def test_deleting_an_unknown_document_reports_not_indexed(indexed):
    """
    Deleting an unknown document is an error, not a silent success.

    This is the contract the Android backends map onto their
    DocumentNotIndexed domain error.
    """

    data_dir, upload = indexed

    result = run_probe(data_dir, "delete", "not_here.txt")

    assert result["status_code"] == 404
    assert result["error"] == "Document is not indexed."

    # The index must be completely undisturbed.
    status = run_probe(data_dir, "status")

    assert status["documents"] == upload["documents"]
    assert status["content_terms"] == upload["content_terms"]
    assert status["indexing"]["state"] == "READY"


def test_bulk_delete_reports_not_found_names_separately(indexed):
    """Bulk delete distinguishes deleted, missing and failed names."""

    data_dir, _upload = indexed

    result = run_probe(
        data_dir,
        "bulk-delete",
        "one.txt",
        "missing_a.txt",
        "missing_b.txt",
    )

    assert result["status_code"] == 200
    assert result["deleted"] == ["one.txt"]
    assert result["deleted_count"] == 1
    assert result["not_found_count"] == 2
    assert set(result["not_found"]) == {"missing_a.txt", "missing_b.txt"}
    assert result["failed_count"] == 0


# ----------------------------------------------------------------------
# Rebuild
# ----------------------------------------------------------------------


def test_rebuild_reproduces_the_same_index(indexed):
    data_dir, upload = indexed

    before = run_probe(data_dir, "search", "targetterm")

    rebuild = run_probe(data_dir, "rebuild")

    assert rebuild["settled"]["indexing"]["state"] == "READY"

    after = run_probe(data_dir, "search", "targetterm")

    assert before["titles"] == after["titles"]
    assert before["scores"] == after["scores"]


def test_rebuild_preserves_document_count(indexed):
    data_dir, upload = indexed

    rebuild = run_probe(data_dir, "rebuild")

    assert rebuild["settled"]["documents"] == upload["documents"]
    assert rebuild["settled"]["content_terms"] == upload["content_terms"]


def test_search_remains_available_after_rebuild(indexed):
    data_dir, _upload = indexed

    run_probe(data_dir, "rebuild")

    status = run_probe(data_dir, "status")

    assert status["indexing"]["state"] == "READY"
    assert status["documents"] == len(CORPUS)
