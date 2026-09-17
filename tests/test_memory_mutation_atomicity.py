"""
Copy-on-write memory mutation and publication coherence.

The index is four parallel containers. Before this change, the
single-document path mutated the *live published* containers in place
while holding `INDEX_DATA_LOCK`:

```python
DOCUMENT_METADATA[filename] = metadata
REAL_INVERTED_INDEX.pop(term)
```

Writers were serialised, but a search that captured its four references
under the lock could still observe a partially updated index: a document
present in `DOCUMENT_METADATA` but not yet in `FILENAME_INDEX`.

Mutation now builds complete replacement containers and publishes them
in one swap. These tests pin the resulting semantics, and the
concurrency test asserts specific terms rather than container lengths,
because equal lengths would hide a torn read.
"""

import copy
import sys
import threading
import time

import pytest

from search_engine.index_state import (
    IndexState,
    plan_add_document,
    plan_remove_document,
    plan_upsert_document,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def revision(terms, label="doc1.txt"):
    """Build the arguments for indexing one revision of a document."""

    content_words = []

    for term in terms:
        content_words.append(term)

    return {
        "filename": label,
        "metadata": {
            "title": label,
            "path": f"/data/{label}",
            "total_words": len(content_words),
            "page_count": 1,
        },
        "filename_words": [label.rsplit(".", 1)[0]],
        "pages": [{"page": 1, "text": " ".join(terms)}],
        "content_words": content_words,
    }


def start_state(documents):
    """
    Build a starting snapshot from {filename: terms}.

    The inverted index is derived from the terms so the fixture cannot
    contradict itself.
    """

    inverted = {}

    for filename, terms in documents.items():
        for term in terms:
            inverted.setdefault(term, {})[filename] = 1

    return {
        "inverted_index": inverted,
        "document_metadata": {
            filename: {"title": filename, "total_words": len(terms)}
            for filename, terms in documents.items()
        },
        "filename_index": {
            filename: [filename.rsplit(".", 1)[0]] for filename in documents
        },
        "page_text_index": {
            filename: [{"page": 1, "text": " ".join(terms)}]
            for filename, terms in documents.items()
        },
    }


def terms_for(snapshot, filename):
    """Return the set of terms in `snapshot` whose postings name `filename`."""

    return {
        term
        for term, postings in snapshot["inverted_index"].items()
        if filename in postings
    }


# ----------------------------------------------------------------------
# Planners are pure
# ----------------------------------------------------------------------


def test_planners_never_mutate_the_snapshot_they_are_given():
    """
    A planner that produced the right output by mutating its input would
    corrupt any reader still holding that snapshot.
    """

    start = start_state({"doc1.txt": ["alpha", "beta"]})
    before = copy.deepcopy(start)

    plan_add_document(start, **revision(["gamma"]))
    assert start == before, "plan_add_document mutated its input"

    plan_remove_document(start, "doc1.txt", {"alpha": 1, "beta": 1})
    assert start == before, "plan_remove_document mutated its input"

    plan_upsert_document(
        start,
        old_term_counts={"alpha": 1},
        **revision(["delta"]),
    )
    assert start == before, "plan_upsert_document mutated its input"


def test_planners_do_not_share_top_level_containers_with_the_input():
    """
    The replacement containers must be new objects, or publishing one
    would silently alter whatever the previous snapshot referenced.
    """

    start = start_state({"doc1.txt": ["alpha"]})

    replacement = plan_upsert_document(
        start,
        old_term_counts={"alpha": 1},
        **revision(["beta"]),
    )

    for field in (
        "inverted_index",
        "document_metadata",
        "filename_index",
        "page_text_index",
    ):
        assert replacement[field] is not start[field], (
            f"{field} was reused instead of replaced"
        )


def test_plan_upsert_rejects_an_incomplete_current_state():
    from search_engine.index_state import SnapshotIncompleteError

    with pytest.raises(SnapshotIncompleteError):
        plan_upsert_document(
            {"inverted_index": {}},
            old_term_counts={},
            **revision(["alpha"]),
        )


# ----------------------------------------------------------------------
# Replace an existing document
# ----------------------------------------------------------------------


def test_upsert_replaces_all_four_containers_together():
    start = start_state({"doc1.txt": ["oldterm"]})

    replacement = plan_upsert_document(
        start,
        old_term_counts={"oldterm": 1},
        **revision(["newterm", "alsonew"]),
    )

    assert replacement["document_metadata"]["doc1.txt"]["total_words"] == 2
    assert replacement["filename_index"]["doc1.txt"] == ["doc1"]
    assert replacement["page_text_index"]["doc1.txt"] == [
        {"page": 1, "text": "newterm alsonew"}
    ]
    assert terms_for(replacement, "doc1.txt") == {"newterm", "alsonew"}


def test_upsert_removes_the_previous_revisions_terms():
    """A re-upload must not leave the old revision searchable."""

    start = start_state({"doc1.txt": ["alpha", "beta", "stale"]})

    replacement = plan_upsert_document(
        start,
        old_term_counts={"alpha": 1, "beta": 1, "stale": 1},
        **revision(["gamma"]),
    )

    assert terms_for(replacement, "doc1.txt") == {"gamma"}
    assert "stale" not in replacement["inverted_index"]
    assert "alpha" not in replacement["inverted_index"]


def test_upsert_keeps_a_term_shared_with_another_document():
    start = start_state(
        {"doc1.txt": ["shared"], "doc2.txt": ["shared", "other"]}
    )

    replacement = plan_upsert_document(
        start,
        old_term_counts={"shared": 1},
        **revision(["fresh"]),
    )

    # doc2 keeps the shared term; only doc1's posting is gone.
    assert replacement["inverted_index"]["shared"] == {"doc2.txt": 1}
    assert terms_for(replacement, "doc1.txt") == {"fresh"}


def test_upsert_of_an_unknown_document_matches_a_plain_add():
    start = start_state({"other.txt": ["alpha"]})

    upserted = plan_upsert_document(
        start,
        old_term_counts={},
        **revision(["beta"]),
    )
    added = plan_add_document(start, **revision(["beta"]))

    assert upserted == added


# ----------------------------------------------------------------------
# Delete an existing document
# ----------------------------------------------------------------------


def test_delete_removes_the_document_from_all_four_containers():
    start = start_state({"doc1.txt": ["alpha"], "doc2.txt": ["beta"]})

    replacement = plan_remove_document(
        start,
        "doc1.txt",
        {"alpha": 1},
    )

    assert "doc1.txt" not in replacement["document_metadata"]
    assert "doc1.txt" not in replacement["filename_index"]
    assert "doc1.txt" not in replacement["page_text_index"]
    assert terms_for(replacement, "doc1.txt") == set()

    # The other document is untouched.
    assert replacement["document_metadata"]["doc2.txt"]["total_words"] == 1
    assert terms_for(replacement, "doc2.txt") == {"beta"}


def test_delete_drops_a_term_whose_last_document_is_gone():
    start = start_state({"doc1.txt": ["solo", "shared"], "doc2.txt": ["shared"]})

    replacement = plan_remove_document(
        start,
        "doc1.txt",
        {"solo": 1, "shared": 1},
    )

    assert "solo" not in replacement["inverted_index"], (
        "a term with no remaining documents must disappear"
    )
    assert replacement["inverted_index"]["shared"] == {"doc2.txt": 1}


def test_delete_of_an_absent_document_is_a_no_op():
    start = start_state({"doc1.txt": ["alpha"]})

    replacement = plan_remove_document(start, "absent.txt", {})

    assert replacement == start


def test_delete_tolerates_stale_term_counts():
    """SQLite may name terms this document no longer has."""

    start = start_state({"doc1.txt": ["alpha"]})

    replacement = plan_remove_document(
        start,
        "doc1.txt",
        {"alpha": 1, "never_indexed": 9},
    )

    assert replacement["document_metadata"] == {}


# ----------------------------------------------------------------------
# Publication is atomic
# ----------------------------------------------------------------------


def test_publication_does_not_happen_until_replace_snapshot_is_called():
    """
    "No new term content appears before publication."

    Building a replacement must leave the active state completely
    untouched.
    """

    state = IndexState(**start_state({"doc1.txt": ["oldterm"]}))

    replacement = plan_upsert_document(
        state.snapshot(),
        old_term_counts={"oldterm": 1},
        **revision(["newterm"]),
    )

    # Not published yet.
    assert terms_for(state.snapshot(), "doc1.txt") == {"oldterm"}
    assert "newterm" not in state.inverted_index
    assert state.generation == 0

    state.replace_snapshot(replacement)

    # Now published, all at once.
    assert terms_for(state.snapshot(), "doc1.txt") == {"newterm"}
    assert "oldterm" not in state.inverted_index
    assert state.generation == 1


def test_replace_snapshot_publishes_all_four_fields_of_the_replacement():
    state = IndexState(**start_state({"doc1.txt": ["oldterm"]}))

    replacement = plan_upsert_document(
        state.snapshot(),
        old_term_counts={"oldterm": 1},
        **revision(["newterm"])
    )

    state.replace_snapshot(replacement)

    for field in (
        "inverted_index",
        "document_metadata",
        "filename_index",
        "page_text_index",
    ):
        assert state.snapshot()[field] is replacement[field]


# ----------------------------------------------------------------------
# Failed SQLite persistence
# ----------------------------------------------------------------------


def test_sqlite_failure_preserves_the_pre_refactor_behaviour(app_module, tmp_path):
    """
    Pin the failure semantics that existed before copy-on-write.

    Measured against the pre-change implementation, then asserted here:
    when the SQLite write raises, memory has *already* been updated to
    the new revision, SQLite still holds the old revision, and the two
    are left out of sync while the exception propagates.

    Copy-on-write changed only *when* state becomes visible between
    readers, so this behaviour must be unchanged. Deliberately not
    "fixed": redesigning rollback is a separate decision.
    """

    engine = app_module

    first = tmp_path / "sqlitefail_probe.txt"
    first.write_text("alpha beta gammaterm\n", encoding="utf-8")

    engine.incrementally_index_document("sqlitefail_probe.txt", str(first))

    in_memory_after_first = {
        term
        for term, postings in engine.REAL_INVERTED_INDEX.items()
        if "sqlitefail_probe.txt" in postings
    }

    assert in_memory_after_first == {"alpha", "beta", "gammaterm"}

    second = tmp_path / "probe_v2.txt"
    second.write_text("deltaterm epsilon\n", encoding="utf-8")

    class FailingConnection:
        def execute(self, *arguments, **keywords):
            raise RuntimeError("simulated disk failure")

        def executemany(self, *arguments, **keywords):
            raise RuntimeError("simulated disk failure")

        def commit(self):
            raise RuntimeError("simulated disk failure")

        def close(self):
            pass

    with pytest.raises(RuntimeError, match="simulated disk failure"):
        engine.incrementally_index_document(
            "sqlitefail_probe.txt",
            str(second),
            connection=FailingConnection(),
            commit=False,
        )

    # Memory moved to the new revision ...
    in_memory = {
        term
        for term, postings in engine.REAL_INVERTED_INDEX.items()
        if "sqlitefail_probe.txt" in postings
    }

    assert in_memory == {"deltaterm", "epsilon"}

    # ... while SQLite still holds the old one.
    from_sqlite = set(
        engine.get_document_term_counts_from_sqlite("sqlitefail_probe.txt").keys()
    )

    assert from_sqlite == {"alpha", "beta", "gammaterm"}
    assert in_memory != from_sqlite

    # And the ownership invariant must still hold after a failed write.
    engine.assert_index_state_consistent()

    engine.incrementally_remove_document("sqlitefail_probe.txt")


# ----------------------------------------------------------------------
# Concurrency: the teeth test
# ----------------------------------------------------------------------


def test_a_captured_snapshot_is_frozen_against_later_publication(app_module):
    """
    The deterministic teeth test.

    Copy-on-write means the containers behind a captured snapshot are
    never written to again, so a reader holding them sees a stable
    generation no matter how many times the index is republished
    afterwards.

    Under the previous in-place mutation this fails immediately: the
    captured references *were* the live containers, so every later write
    was visible through them. No race is needed to detect it, which is
    why this is the primary test and the threaded one below is only a
    smoke test.
    """

    engine = app_module
    name = "cowteeth.txt"

    generation_a = {"alphaone", "alphatwo"}
    generation_b = {"betaone", "betatwo"}

    with engine.INDEX_DATA_LOCK:
        engine.publish_index_state(
            plan_add_document(
                engine.INDEX_STATE.snapshot(),
                **revision(sorted(generation_a), label=name),
            )
        )

    # Capture exactly as execute_search does.
    with engine.INDEX_DATA_LOCK:
        captured = engine.INDEX_STATE.snapshot()

    captured_documents = set(captured["document_metadata"])
    captured_terms = terms_for(captured, name)
    captured_filename_words = list(captured["filename_index"][name])

    assert captured_terms == generation_a

    # Republish 100 times, alternating generations, checking after every
    # single publication. Checking only once at the end would be wrong:
    # if the final iteration happened to republish the captured
    # generation the assertion would pass by coincidence, which is
    # exactly how the first version of this test silently lost its teeth.
    for index in range(100):
        terms = generation_a if index % 2 else generation_b

        with engine.INDEX_DATA_LOCK:
            engine.publish_index_state(
                plan_upsert_document(
                    engine.INDEX_STATE.snapshot(),
                    old_term_counts={
                        term: 1 for term in (generation_a | generation_b)
                    },
                    **revision(sorted(terms), label=name),
                )
            )

        assert set(captured["document_metadata"]) == captured_documents, (
            f"after publication {index} the captured metadata changed from "
            f"{sorted(captured_documents)} to "
            f"{sorted(captured['document_metadata'])}"
        )

        assert terms_for(captured, name) == captured_terms, (
            f"after publication {index} the captured term set changed from "
            f"{sorted(captured_terms)} to {sorted(terms_for(captured, name))}"
        )

        assert captured["filename_index"][name] == captured_filename_words
        assert name in captured["page_text_index"]

    # The live index really did move, so the snapshot above was not
    # merely a copy of something static.
    live = terms_for(engine.INDEX_STATE.snapshot(), name)

    assert live in (generation_a, generation_b)
    assert engine.INDEX_STATE.generation > 100

    with engine.INDEX_DATA_LOCK:
        engine.publish_index_state(
            plan_remove_document(
                engine.INDEX_STATE.snapshot(),
                name,
                {term: 1 for term in (generation_a | generation_b)},
            )
        )

    engine.assert_index_state_consistent()


def test_concurrent_readers_never_observe_a_mixed_generation(app_module):
    """
    Threaded smoke test.

    The two generations use disjoint term sets, so a torn read shows up
    as a wrong term set rather than merely a wrong length.
    """

    engine = app_module
    name = "cowthreads.txt"

    generation_a = {"thetaone", "thetatwo"}
    generation_b = {"iotaone", "iotatwo"}

    with engine.INDEX_DATA_LOCK:
        engine.publish_index_state(
            plan_add_document(
                engine.INDEX_STATE.snapshot(),
                **revision(sorted(generation_a), label=name),
            )
        )

    stop = threading.Event()
    failures = []
    observations = []

    def reader():
        while not stop.is_set():
            # Capture under the lock, then read the captured references
            # outside it, exactly as execute_search does. The sleep
            # widens the window in which a writer could interfere; with
            # copy-on-write there is nothing to interfere *with*.
            with engine.INDEX_DATA_LOCK:
                captured = engine.INDEX_STATE.snapshot()

            time.sleep(0.0002)

            observed = terms_for(captured, name)

            if observed not in (generation_a, generation_b):
                failures.append(
                    f"mixed generation observed: {sorted(observed)} "
                    f"(expected exactly {sorted(generation_a)} or "
                    f"{sorted(generation_b)})"
                )
                return

            presence = {
                "document_metadata": name in captured["document_metadata"],
                "filename_index": name in captured["filename_index"],
                "page_text_index": name in captured["page_text_index"],
            }

            observations.append(observed)

            if not all(presence.values()):
                failures.append(f"torn container presence: {presence}")
                return

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]

    # Switch threads as often as possible so a non-atomic publish is
    # actually reachable rather than theoretically possible.
    previous_switch_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-06)

    for thread in readers:
        thread.start()

    try:
        for index in range(2000):
            terms = generation_a if index % 2 else generation_b

            with engine.INDEX_DATA_LOCK:
                engine.publish_index_state(
                    plan_upsert_document(
                        engine.INDEX_STATE.snapshot(),
                        old_term_counts={
                            term: 1 for term in (generation_a | generation_b)
                        },
                        **revision(sorted(terms), label=name),
                    )
                )
    finally:
        stop.set()

        for thread in readers:
            thread.join(timeout=10)

    assert not failures, failures[:5]

    # The readers must have actually looked, and looked often. Without
    # this the test passes when every reader thread exits immediately,
    # which is the one way a "no torn read was observed" test can lie:
    # it observed nothing and reports success.
    # The threshold is well below the ~100 reads this reliably produces,
    # because its job is to rule out "the readers never ran", not to
    # measure throughput. A tight bound here would fail on a slower
    # machine while proving nothing extra.
    assert len(observations) > 20, (
        f"only {len(observations)} reads completed, so the absence of a "
        "torn read says nothing"
    )

    # And both generations must have been seen, otherwise the readers
    # were never running during a mutation.
    assert set(map(frozenset, observations)) == {
        frozenset(generation_a),
        frozenset(generation_b),
    }, "the readers did not span both generations"

    # Every read saw a full generation. Nothing was ever partial.
    assert all(
        frozenset(observed) in (frozenset(generation_a), frozenset(generation_b))
        for observed in observations
    )

    with engine.INDEX_DATA_LOCK:
        engine.publish_index_state(
            plan_remove_document(
                engine.INDEX_STATE.snapshot(),
                name,
                {term: 1 for term in (generation_a | generation_b)},
            )
        )

    engine.assert_index_state_consistent()


# ----------------------------------------------------------------------
# Integration through the real HTTP surface
# ----------------------------------------------------------------------


def test_upload_and_delete_keep_the_index_coherent(app_module, tmp_path):
    """Exercise the real routes, not just the planners."""

    engine = app_module
    client = engine.app.test_client()

    name = "cow_integration_probe.txt"

    path = tmp_path / name
    path.write_text("cowmarker firstterm\n", encoding="utf-8")

    with open(path, "rb") as handle:
        response = client.post(
            "/api/upload",
            data={"file": (handle, name, "text/plain")},
        )

    assert response.status_code == 200
    assert response.get_json()["uploaded_count"] == 1

    engine.assert_index_state_consistent()

    snapshot = engine.INDEX_STATE.snapshot()

    assert name in snapshot["document_metadata"]
    assert name in snapshot["filename_index"]
    assert name in snapshot["page_text_index"]

    # The document is searchable through the real endpoint.
    search = client.get("/api/search", query_string={"q": "cowmarker"})
    titles = [hit["title"] for hit in search.get_json()["results"]]

    assert name in titles

    # Re-upload with different content: the old term must disappear.
    path.write_text("cowmarker replacedterm\n", encoding="utf-8")

    with open(path, "rb") as handle:
        client.post(
            "/api/upload",
            data={"file": (handle, name, "text/plain")},
        )

    engine.assert_index_state_consistent()

    old = client.get("/api/search", query_string={"q": "firstterm"})
    assert [hit["title"] for hit in old.get_json()["results"]] == []

    new = client.get("/api/search", query_string={"q": "replacedterm"})
    assert name in [hit["title"] for hit in new.get_json()["results"]]

    # Delete: gone from all four containers.
    deleted = client.delete(f"/api/documents/{name}")
    assert deleted.status_code == 200

    engine.assert_index_state_consistent()

    snapshot = engine.INDEX_STATE.snapshot()

    for field in (
        "document_metadata",
        "filename_index",
        "page_text_index",
    ):
        assert name not in snapshot[field], f"{name} still in {field}"

    assert name not in {
        document
        for postings in snapshot["inverted_index"].values()
        for document in postings
    }


def test_index_globals_track_the_state_after_each_publication(app_module):
    """
    The legacy globals must name the same objects as INDEX_STATE after
    every publish, since the ranking code still reads them directly.
    """

    engine = app_module

    engine.assert_index_state_consistent()

    with engine.INDEX_DATA_LOCK:
        engine.publish_index_state(
            plan_add_document(
                engine.INDEX_STATE.snapshot(),
                **revision(["coherencemarker"], label="coherence_probe.txt"),
            )
        )

    engine.assert_index_state_consistent()
    assert engine.DOCUMENT_METADATA is engine.INDEX_STATE.document_metadata
    assert engine.REAL_INVERTED_INDEX is engine.INDEX_STATE.inverted_index

    with engine.INDEX_DATA_LOCK:
        engine.publish_index_state(
            plan_remove_document(
                engine.INDEX_STATE.snapshot(),
                "coherence_probe.txt",
                {"coherencemarker": 1},
            )
        )

    engine.assert_index_state_consistent()


# ----------------------------------------------------------------------
# Structural guard
# ----------------------------------------------------------------------


def _publication_lines(app_module):
    """
    Return (publish_span, publish_calls, sync_calls) for app.py.

    `publish_span` is the line range of publish_index_state's own body,
    resolved from the AST rather than hardcoded, so the test does not
    silently stop checking anything when the file is edited.
    """

    import ast
    from pathlib import Path

    tree = ast.parse(Path(app_module.__file__).read_text(encoding="utf-8"))

    publish_span = None

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "publish_index_state":
            publish_span = (node.lineno, node.end_lineno)

    assert publish_span is not None, "publish_index_state is not defined in app.py"

    class PublicationWalk(ast.NodeVisitor):
        def __init__(self):
            self.depth = 0
            self.publish_calls = []
            self.sync_calls = []

        def visit_With(self, node):
            locked = any(
                isinstance(item.context_expr, ast.Name)
                and item.context_expr.id == "INDEX_DATA_LOCK"
                for item in node.items
            )

            if locked:
                self.depth += 1

            # Traversing the whole node keeps the depth correct for
            # anything nested inside the critical section.
            self.generic_visit(node)

            if locked:
                self.depth -= 1

        def visit_Call(self, node):
            function = node.func
            name = (
                function.attr
                if isinstance(function, ast.Attribute)
                else function.id
                if isinstance(function, ast.Name)
                else None
            )

            if name == "publish_index_state":
                self.publish_calls.append((node.lineno, self.depth > 0))

            if name == "sync_index_globals_from_state":
                self.sync_calls.append((node.lineno, self.depth > 0))

            self.generic_visit(node)

    walk = PublicationWalk()
    walk.visit(tree)

    return publish_span, walk.publish_calls, walk.sync_calls, tree


def test_every_publication_site_is_inside_the_lock(app_module):
    """
    Enforce the publication rule on the source itself.

    `publish_index_state` must be called under `INDEX_DATA_LOCK`, because
    `execute_search` captures all four globals under that same lock. Two
    sites used to call `sync_index_globals_from_state()` *after*
    releasing it, which let a reader capture a half-rebound set of
    globals - one from the new generation, one from the old.
    """

    publish_span, publish_calls, sync_calls, _ = _publication_lines(app_module)

    assert publish_calls, "no publish_index_state calls found; the walker is broken"

    unsafe_publish = [line for line, locked in publish_calls if not locked]

    assert not unsafe_publish, (
        f"publish_index_state called without INDEX_DATA_LOCK at lines "
        f"{unsafe_publish}; readers capture the globals under that lock "
        f"and would see a half-published index"
    )

    # Inside publish_index_state itself the lock is held by the caller,
    # so only calls outside its own body are checked here.
    outside_body = [
        line
        for line, locked in sync_calls
        if not (publish_span[0] <= line <= publish_span[1])
    ]

    unsafe_sync = [line for line in outside_body if not dict(sync_calls)[line]]

    assert not unsafe_sync, (
        f"sync_index_globals_from_state called outside INDEX_DATA_LOCK "
        f"at lines {unsafe_sync}; the four globals must be re-pointed in "
        f"the same critical section that swaps the state"
    )


def test_publish_index_state_is_the_only_writer_of_the_state(app_module):
    """Nothing else may reach for replace_snapshot directly."""

    import ast

    publish_span, _, _, tree = _publication_lines(app_module)

    direct = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        function = node.func

        if isinstance(function, ast.Attribute) and function.attr == "replace_snapshot":
            direct.append(node.lineno)

    assert direct, "expected publish_index_state to call replace_snapshot"

    outside = [
        line
        for line in direct
        if not (publish_span[0] <= line <= publish_span[1])
    ]

    assert not outside, (
        f"replace_snapshot called directly at lines {outside}; every "
        f"publication must go through publish_index_state so the globals "
        f"are re-pointed inside the same critical section"
    )
