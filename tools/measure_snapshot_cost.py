"""
Measure the cost of taking an index snapshot.

This exists to justify a design decision with numbers rather than
opinion. `IndexState.snapshot()` returns references; `deep_snapshot()`
copies. If `snapshot()` deep-copied, every search would pay for a full
copy of the index before it could look at a single posting list.

The ranking pipeline captures its view of the index once per request.
Whatever that costs is paid on every search, so it must not scale with
corpus size.

Usage:
    python -m tools.measure_snapshot_cost
"""

import copy
import random
import time

from search_engine.index_state import IndexState

CORPUS_SIZES = (100, 500, 1000)

TERMS_PER_DOCUMENT = 150

REPEATS = 200

SEED = 20260914


def build_state(document_count, seed=SEED):
    """Build a synthetic index of a realistic shape."""

    generator = random.Random(seed)

    inverted = {}
    metadata = {}
    filenames = {}
    pages = {}

    for index in range(document_count):
        name = f"doc{index}.txt"

        metadata[name] = {
            "title": name,
            "path": f"/data/{name}",
            "total_words": 400,
            "page_count": 1,
        }

        filenames[name] = ["doc", str(index)]
        pages[name] = [{"page": 1, "text": "lorem ipsum " * 200}]

        for term in generator.sample(range(4000), TERMS_PER_DOCUMENT):
            inverted.setdefault(f"term{term}", {})[name] = generator.randint(1, 5)

    return IndexState(
        inverted_index=inverted,
        document_metadata=metadata,
        filename_index=filenames,
        page_text_index=pages,
    ), len(inverted)


def measure(state, operation, repeats=REPEATS):
    """Return the average milliseconds per call."""

    # Warm up so the first-call cost does not skew the result.
    operation()

    start = time.perf_counter()

    for _ in range(repeats):
        operation()

    elapsed = time.perf_counter() - start

    return (elapsed / repeats) * 1000.0


def main():
    print("Snapshot cost")
    print()

    header = (
        f"{'documents':>10} {'terms':>8} "
        f"{'snapshot() us':>14} {'deep_snapshot() ms':>20} {'ratio':>10}"
    )

    print(header)
    print("-" * len(header))

    for document_count in CORPUS_SIZES:
        state, term_count = build_state(document_count)

        reference_ms = measure(state, state.snapshot)
        deep_ms = measure(state, state.deep_snapshot, repeats=10)

        ratio = deep_ms / reference_ms if reference_ms else float("inf")

        print(
            f"{document_count:>10} {term_count:>8} "
            f"{reference_ms * 1000:>14.2f} {deep_ms:>20.2f} {ratio:>9,.0f}x"
        )

    print()
    print("snapshot() is O(1) in corpus size: it reads four references")
    print("under one lock acquisition. deep_snapshot() is O(corpus size).")
    print()
    print("This is why IndexState.snapshot() returns references, and why")
    print("the ranking pipeline must always use it rather than deep_snapshot().")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
