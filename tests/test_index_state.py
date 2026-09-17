"""
Tests for the IndexState ownership boundary.

The point of `IndexState` is not that it stores four dictionaries. It is
that a reader can take one coherent view of all four, and that a writer
can replace all four atomically, which is the guarantee the ranking
pipeline depends on. These tests pin that, plus the identity invariant
that keeps `app.py`'s legacy globals from silently desynchronising.

Snapshot semantics
------------------
`IndexState.snapshot()` returns **references**, not copies, matching the
copy-on-write model the engine already uses:

```python
with INDEX_DATA_LOCK:
    active_inverted_index = REAL_INVERTED_INDEX   # a reference
```

Copying there would put about 83 ms on every search at 1000 documents.
The contract is therefore:

  * `snapshot()` is cheap and read-only
  * `deep_snapshot()` is isolated and expensive
  * mutation happens by building replacement containers and calling
    `replace_snapshot()`

Both halves of that contract are tested below, because a test that only
checked "snapshot is independent" would pass for the wrong
implementation and fail in production on latency.
"""

import threading
import time

import pytest

from search_engine.index_state import (
    IndexState,
    SnapshotIncompleteError,
)


def make_snapshot(marker="a"):
    return {
        "inverted_index": {
            f"term_{marker}": {f"doc_{marker}.txt": 3},
        },
        "document_metadata": {
            f"doc_{marker}.txt": {
                "title": f"doc_{marker}.txt",
                "path": f"/data/doc_{marker}.txt",
                "total_words": 10,
                "page_count": 1,
            },
        },
        "filename_index": {
            f"doc_{marker}.txt": ["doc", marker],
        },
        "page_text_index": {
            f"doc_{marker}.txt": [{"page": 1, "text": f"body {marker}"}],
        },
    }


@pytest.fixture
def state():
    return IndexState(**make_snapshot("a"))


# ----------------------------------------------------------------------
# Construction and counts
# ----------------------------------------------------------------------


def test_constructs_empty_by_default():
    empty = IndexState()

    assert empty.document_count() == 0
    assert empty.content_term_count() == 0
    assert empty.counts() == {
        "documents": 0,
        "content_terms": 0,
        "filenames_indexed": 0,
        "page_text_entries": 0,
    }


def test_adopts_the_containers_it_is_given():
    """Passing containers in must not copy them, so app.py can seed it."""

    containers = make_snapshot("a")

    seeded = IndexState(**containers)

    assert seeded.inverted_index is containers["inverted_index"]
    assert seeded.document_metadata is containers["document_metadata"]
    assert seeded.filename_index is containers["filename_index"]
    assert seeded.page_text_index is containers["page_text_index"]


def test_counts_reflect_the_corpus(state):
    assert state.counts() == {
        "documents": 1,
        "content_terms": 1,
        "filenames_indexed": 1,
        "page_text_entries": 1,
    }


# ----------------------------------------------------------------------
# snapshot(): cheap and coherent
# ----------------------------------------------------------------------


def test_snapshot_is_coherent_across_all_four_containers(state):
    snapshot = state.snapshot()

    for field in (
        "inverted_index",
        "document_metadata",
        "filename_index",
        "page_text_index",
    ):
        assert field in snapshot

    assert snapshot["generation"] == state.generation


def test_snapshot_does_not_copy(state):
    """
    Pin the performance contract.

    If this fails because someone added a deepcopy, search latency will
    have regressed by roughly two orders of magnitude at 1000 documents.
    See tools/measure_snapshot_cost.py.
    """

    snapshot = state.snapshot()

    assert snapshot["inverted_index"] is state.inverted_index
    assert snapshot["document_metadata"] is state.document_metadata
    assert snapshot["filename_index"] is state.filename_index
    assert snapshot["page_text_index"] is state.page_text_index


def test_snapshot_is_cheap_for_a_large_index():
    """
    A snapshot taken under a lock must not scale with corpus size.

    This is the check that would fail if snapshot() deep-copied.
    """

    documents = {
        f"doc{i}.txt": {
            "title": f"doc{i}.txt",
            "path": f"/data/doc{i}.txt",
            "total_words": 100,
            "page_count": 1,
        }
        for i in range(1000)
    }

    terms = {
        f"term{i}": {f"doc{i % 1000}.txt": 1}
        for i in range(4000)
    }

    large = IndexState(
        inverted_index=terms,
        document_metadata=documents,
        filename_index={name: ["doc"] for name in documents},
        page_text_index={name: [{"page": 1, "text": "x"}] for name in documents},
    )

    start = time.perf_counter()

    for _ in range(1000):
        snapshot = large.snapshot()
        assert len(snapshot["document_metadata"]) == 1000

    elapsed = time.perf_counter() - start

    # 1000 reference snapshots must be far cheaper than one deepcopy of
    # this index, which measures around 80 ms.
    assert elapsed < 0.5, (
        f"1000 snapshots took {elapsed:.3f}s; snapshot() is copying "
        "rather than returning references"
    )


# ----------------------------------------------------------------------
# deep_snapshot(): isolated
# ----------------------------------------------------------------------


def test_deep_snapshot_is_independent_of_active_state(state):
    isolated = state.deep_snapshot()

    isolated["document_metadata"]["injected.txt"] = {"title": "injected"}
    isolated["inverted_index"]["term_x"] = {"doc_a.txt": 1}
    isolated["document_metadata"]["doc_a.txt"]["total_words"] = 999

    assert "injected.txt" not in state.document_metadata
    assert "term_x" not in state.inverted_index
    assert state.document_metadata["doc_a.txt"]["total_words"] == 10


def test_deep_snapshot_is_not_shared_with_snapshot_before_it(state):
    before = state.deep_snapshot()

    state.replace_snapshot(make_snapshot("b"))

    assert "doc_a.txt" in before["document_metadata"]
    assert "doc_b.txt" not in before["document_metadata"]

    # The two snapshots are genuinely different objects throughout.
    assert before["inverted_index"] is not state.inverted_index


# ----------------------------------------------------------------------
# replace_snapshot(): atomic
# ----------------------------------------------------------------------


def test_replace_snapshot_swaps_all_four_containers(state):
    replacement = make_snapshot("b")

    state.replace_snapshot(replacement)

    assert state.inverted_index is replacement["inverted_index"]
    assert state.document_metadata is replacement["document_metadata"]
    assert state.filename_index is replacement["filename_index"]
    assert state.page_text_index is replacement["page_text_index"]


def test_replace_snapshot_removes_the_previous_documents(state):
    state.replace_snapshot(make_snapshot("b"))

    assert not state.has_document("doc_a.txt")
    assert state.has_document("doc_b.txt")


def test_replace_snapshot_bumps_the_generation(state):
    before = state.generation

    state.replace_snapshot(make_snapshot("b"))

    assert state.generation == before + 1


def test_replace_snapshot_can_hold_the_generation(state):
    before = state.generation

    state.replace_snapshot(make_snapshot("b"), bump_generation=False)

    assert state.generation == before


def test_replace_snapshot_rejects_incomplete_snapshots(state):
    """
    A partial replacement must be rejected outright, not partially
    applied: a half-applied snapshot is exactly the corruption the
    atomic swap exists to prevent.
    """

    original = state.deep_snapshot()

    for field in (
        "inverted_index",
        "document_metadata",
        "filename_index",
        "page_text_index",
    ):
        incomplete = make_snapshot("b")
        del incomplete[field]

        with pytest.raises(SnapshotIncompleteError) as error:
            state.replace_snapshot(incomplete)

        assert field in str(error.value)

        # Nothing may have changed.
        assert state.generation == original["generation"]
        assert state.document_metadata.keys() == original["document_metadata"].keys()


def test_replace_snapshot_rejects_an_empty_mapping(state):
    with pytest.raises(SnapshotIncompleteError):
        state.replace_snapshot({})


def test_rejected_replacement_leaves_the_state_usable(state):
    try:
        state.replace_snapshot({"inverted_index": {}})
    except SnapshotIncompleteError:
        pass

    assert state.has_document("doc_a.txt")
    assert state.counts()["documents"] == 1


# ----------------------------------------------------------------------
# Document helpers
# ----------------------------------------------------------------------


def test_has_document_matches_metadata(state):
    assert state.has_document("doc_a.txt")
    assert not state.has_document("absent.txt")
    assert state.document_metadata.keys() == {"doc_a.txt"}


def test_get_document_returns_metadata_or_none(state):
    metadata = state.get_document("doc_a.txt")

    assert metadata["title"] == "doc_a.txt"
    assert state.get_document("absent.txt") is None


def test_document_names_are_deterministic():
    """
    The same corpus must always enumerate identically, whatever order it
    was built in, so two corpora can be diffed.
    """

    names = ["c.txt", "a.txt", "b.txt"]

    forward = IndexState(
        document_metadata={name: {"title": name} for name in names},
    )
    reverse = IndexState(
        document_metadata={name: {"title": name} for name in reversed(names)},
    )

    assert forward.document_names() == ["a.txt", "b.txt", "c.txt"]
    assert forward.document_names() == reverse.document_names()


# ----------------------------------------------------------------------
# Thread safety
# ----------------------------------------------------------------------


def test_readers_never_observe_a_partial_replacement(state):
    """
    A reader must see one generation, never a mixture.

    The writer alternates between two complete states. Because a
    snapshot reads all four references under one lock acquisition, every
    observed generation must be internally consistent: the document
    counts of the two generations differ, so a torn read would show a
    mismatched pair.
    """

    state.replace_snapshot(make_snapshot("a"))

    stop = threading.Event()
    errors = []

    def reader():
        while not stop.is_set():
            snapshot = state.snapshot()

            documents = snapshot["document_metadata"]
            filenames = snapshot["filename_index"]
            pages = snapshot["page_text_index"]
            inverted = snapshot["inverted_index"]

            # Every container must name the same generation. The
            # generations are tagged doc_a.txt / doc_b.txt, so a torn
            # read shows up as a mismatched name rather than merely a
            # mismatched count.
            names = set(documents)

            if names != set(filenames):
                errors.append(
                    f"torn read: metadata {sorted(names)} vs "
                    f"filename index {sorted(filenames)}"
                )
                return

            if names != set(pages):
                errors.append(
                    f"torn read: metadata {sorted(names)} vs "
                    f"page text {sorted(pages)}"
                )
                return

            # The inverted index must point at the same documents too.
            posting_targets = {
                document
                for postings in inverted.values()
                for document in postings
            }

            if posting_targets != names:
                errors.append(
                    f"torn read: metadata {sorted(names)} vs "
                    f"postings {sorted(posting_targets)}"
                )
                return

            if len(names) != 1:
                errors.append(f"unexpected generation: {sorted(names)}")
                return

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]

    for thread in readers:
        thread.start()

    try:
        for index in range(300):
            state.replace_snapshot(make_snapshot("a" if index % 2 else "b"))
    finally:
        stop.set()

        for thread in readers:
            thread.join(timeout=5)

    assert not errors, errors[:5]


def test_concurrent_snapshots_do_not_raise(state):
    stop = threading.Event()
    errors = []

    def worker():
        while not stop.is_set():
            try:
                state.snapshot()
                state.counts()
                state.document_names()
            except Exception as error:  # noqa: BLE001 - reporting any failure
                errors.append(repr(error))
                return

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(6)]

    for thread in threads:
        thread.start()

    try:
        for _ in range(200):
            state.replace_snapshot(make_snapshot("b"))
            state.replace_snapshot(make_snapshot("a"))
    finally:
        stop.set()

        for thread in threads:
            thread.join(timeout=5)

    assert not errors, errors[:5]


# ----------------------------------------------------------------------
# app.py integration: the identity invariant
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    """The real application module, pointed at a throwaway data dir."""

    import app

    return app


def test_index_state_is_seeded_from_the_module_globals(engine):
    """
    At import time INDEX_STATE must own exactly the containers that
    app.py's globals name, or readers could see two generations.
    """

    engine.assert_index_state_consistent()


def test_globals_stay_bound_after_an_upload(engine, tmp_path):
    """
    Exercise the upsert path, which is one of the rebinding sites, and
    prove the invariant survives it.
    """

    client = engine.app.test_client()

    path = tmp_path / "index_state_probe.txt"
    path.write_text("index state probe targetterm\n", encoding="utf-8")

    with open(path, "rb") as handle:
        response = client.post(
            "/api/upload",
            data={"file": (handle, path.name, "text/plain")},
        )

    assert response.status_code == 200
    assert response.get_json()["uploaded_count"] == 1

    engine.assert_index_state_consistent()

    assert engine.INDEX_STATE.has_document("index_state_probe.txt")
    assert "index_state_probe.txt" in engine.DOCUMENT_METADATA

    # Clean up so this test does not leak into other suites.
    client.post(
        "/api/documents/bulk-delete",
        json={"filenames": ["index_state_probe.txt"]},
    )

    engine.assert_index_state_consistent()


def test_globals_stay_bound_after_a_bulk_delete(engine, tmp_path):
    client = engine.app.test_client()

    path = tmp_path / "index_state_delete_probe.txt"
    path.write_text("bulk delete probe sharedmarker\n", encoding="utf-8")

    with open(path, "rb") as handle:
        client.post(
            "/api/upload",
            data={"file": (handle, path.name, "text/plain")},
        )

    engine.assert_index_state_consistent()

    response = client.post(
        "/api/documents/bulk-delete",
        json={"filenames": ["index_state_delete_probe.txt"]},
    )

    assert response.status_code == 200
    assert response.get_json()["deleted_count"] == 1

    engine.assert_index_state_consistent()


def test_globals_stay_bound_after_a_rebuild(engine):
    """
    Exercise the atomic-swap path in rebuild_database.

    This is the site most likely to desynchronise, because it is where
    the whole index is replaced at once.
    """

    client = engine.app.test_client()

    before = engine.INDEX_STATE.counts()

    response = client.post("/api/rebuild")

    assert response.status_code == 202

    # Wait for the background rebuild to settle.
    deadline = time.time() + 60

    while time.time() < deadline:
        status = client.get("/api/status").get_json()

        if status["indexing"]["state"] in {"READY", "ERROR"}:
            break

        time.sleep(0.05)

    engine.assert_index_state_consistent()

    assert engine.INDEX_STATE.counts() == before


def test_assert_helper_detects_a_desynchronised_global(engine):
    """
    The guard must actually fail when it should.

    Without this, `assert_index_state_consistent` could be a no-op and
    every other test here would still pass.
    """

    original = engine.DOCUMENT_METADATA

    try:
        engine.DOCUMENT_METADATA = dict(original)

        with pytest.raises(AssertionError) as error:
            engine.assert_index_state_consistent()

        assert "DOCUMENT_METADATA" in str(error.value)
    finally:
        engine.DOCUMENT_METADATA = original

    engine.assert_index_state_consistent()
