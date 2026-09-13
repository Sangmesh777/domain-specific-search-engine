"""
Benchmark the search engine core.

No server, no HTTP: these numbers describe the engine an Android backend
would embed, so they are the right basis for judging whether on-device
indexing and search are viable, and where the budget actually goes.

    .venv/bin/python tools/benchmark_core.py
    .venv/bin/python tools/benchmark_core.py --documents 500 --queries 200

Everything runs in a temporary directory and leaves nothing behind. The
repository's own data folder is never touched.

What is measured, and why it matters for a port:

    extraction      parsing dominates import; on Android this is the part
                    that changes library, so it is reported separately from
                    indexing to keep the comparison honest
    indexing        tokenizing and writing the inverted index
    search          the ranking pipeline, reported as a distribution rather
                    than an average, because a p95 is what a user feels
    rebuild         a full re-index from disk, the recovery path
    delete          incremental unindexing
    footprint       SQLite size and resident memory

Search timing uses a cold snapshot per query batch and a warm one, since
the difference is what tells you whether caching matters on a device.
"""

import argparse
import gc
import os
import random
import resource
import shutil
import sys
import tempfile
import time


BASE_DIR = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "tools"))

from pdf_writer import write_pdf                      # noqa: E402

from search_engine.config import EngineConfig         # noqa: E402
from search_engine.engine import SearchEngine         # noqa: E402
from search_engine.extraction import extract_text     # noqa: E402


# Vocabulary large enough that term distribution is not degenerate, and
# small enough that queries hit real postings.
SUBJECTS = [
    "network", "security", "database", "algorithm", "operating",
    "compiler", "graphics", "machine", "learning", "cloud",
    "encryption", "routing", "scheduling", "indexing", "transaction",
    "virtualization", "protocol", "socket", "cache", "partition",
]

FILLER = [
    "the", "of", "and", "a", "to", "in", "is", "for", "with", "on",
    "as", "by", "this", "that", "from", "are", "was", "be", "has", "had",
]


def make_document_text(index, words):
    """Deterministic pseudo-content for one synthetic document."""

    rng = random.Random(index)

    parts = []

    for paragraph in range(4):

        sentence = []

        for _ in range(words // 4):

            if rng.random() < 0.35:
                sentence.append(rng.choice(SUBJECTS))
            else:
                sentence.append(rng.choice(FILLER))

        parts.append(" ".join(sentence))

    return "\n".join(parts)


def build_corpus(directory, count, words, pdf_every):
    """
    Write `count` documents.

    Every `pdf_every`-th document is a real PDF, so extraction timing
    reflects a parser rather than only plain text reads.
    """

    os.makedirs(directory, exist_ok=True)

    paths = []

    for index in range(count):

        text = make_document_text(index, words)

        if pdf_every and index % pdf_every == 0:

            path = os.path.join(directory, f"VTU Module {index}.pdf")

            write_pdf(path, [text, text[:len(text) // 2]])

        else:

            path = os.path.join(directory, f"VTU Notes {index}.txt")

            with open(path, "w", encoding="utf-8") as file:
                file.write(text)

        paths.append(path)

    return paths


def percentile(samples, fraction):

    if not samples:
        return 0.0

    ordered = sorted(samples)

    position = min(
        len(ordered) - 1,
        int(round(fraction * (len(ordered) - 1))),
    )

    return ordered[position]


def resident_megabytes():

    usage = resource.getrusage(resource.RUSAGE_SELF)

    # Linux reports kilobytes; macOS reports bytes.
    if sys.platform == "darwin":
        return usage.ru_maxrss / (1024 * 1024)

    return usage.ru_maxrss / 1024


def measure(label, function):
    """Time one operation, returning (result, seconds)."""

    gc.collect()

    started = time.perf_counter()

    result = function()

    return result, time.perf_counter() - started, label


def report(rows):

    width = max(len(row[0]) for row in rows)

    print()
    print(f"{'measurement'.ljust(width)}   {'value':>14}")
    print(f"{'-' * width}   {'-' * 14}")

    for label, value in rows:
        print(f"{label.ljust(width)}   {value:>14}")


def run(count, words, queries, pdf_every, batch_size):

    temporary = tempfile.mkdtemp()

    try:

        corpus = os.path.join(temporary, "corpus")
        data = os.path.join(temporary, "data")

        os.makedirs(data, exist_ok=True)

        print(f"corpus: {count} documents, ~{words} words each, "
              f"1 in {pdf_every or 0} is a PDF")

        started = time.perf_counter()
        paths = build_corpus(corpus, count, words, pdf_every)
        generation = time.perf_counter() - started

        engine = SearchEngine(EngineConfig(data_folder=data))

        # --- extraction alone, on a sample -----------------------------
        sample = paths[:min(50, len(paths))]

        extraction = []

        for path in sample:

            started = time.perf_counter()

            extract_text(path, os.path.basename(path))

            extraction.append(time.perf_counter() - started)

        # --- import: extraction + tokenizing + SQLite -------------------
        #
        # Imported in batches. Holding every file handle open at once
        # exhausts the process descriptor limit (EMFILE) somewhere past a
        # few hundred documents, and the engine correctly reports those as
        # per-file failures rather than crashing - which would quietly
        # benchmark a fraction of the corpus. Batching keeps the descriptor
        # count bounded, and the tally below makes any shortfall visible.
        def import_all():

            uploaded = rejected = failed = 0

            reasons = []

            for start in range(0, len(paths), batch_size):

                chunk = paths[start:start + batch_size]

                handles = []
                items = []

                for path in chunk:

                    handle = open(path, "rb")
                    handles.append(handle)
                    items.append((os.path.basename(path), handle))

                try:

                    outcome = engine.import_documents(
                        items,
                        log=lambda *args: None,
                    )

                finally:

                    for handle in handles:
                        handle.close()

                uploaded += outcome.uploaded_count
                rejected += outcome.rejected_count
                failed += outcome.failed_count

                reasons.extend(
                    [item["reason"] for item in outcome.rejected]
                    + [item["reason"] for item in outcome.failed]
                )

            return {
                "uploaded": uploaded,
                "rejected": rejected,
                "failed": failed,
                "reasons": reasons,
            }

        imported, import_seconds = measure("import", import_all)[:2]

        if imported["rejected"] or imported["failed"]:

            print()
            print("WARNING: the corpus was not fully imported, so these")
            print("         numbers describe a smaller index than intended.")
            print(f"         rejected={imported['rejected']} "
                  f"failed={imported['failed']}")

            for reason in imported["reasons"][:3]:
                print(f"         first reasons: {reason}")

        counts = engine.counts()

        if counts["documents"] != len(paths):

            print()
            print(f"WARNING: indexed {counts['documents']} of {len(paths)} "
                  f"documents")

        # --- search ------------------------------------------------------
        rng = random.Random(1234)

        query_words = [
            " ".join(
                rng.choice(SUBJECTS)
                for _ in range(rng.choice((1, 1, 1, 2, 2, 3)))
            )
            for _ in range(queries)
        ]

        cold = []

        for query in query_words:

            started = time.perf_counter()

            engine.search(query)

            cold.append(time.perf_counter() - started)

        # Warm: repeated queries, which is the common interactive pattern.
        warm = []

        for query in query_words[:min(50, len(query_words))] * 3:

            started = time.perf_counter()

            engine.search(query)

            warm.append(time.perf_counter() - started)

        hits = sum(
            1
            for query in query_words
            if engine.search(query)["results"]
        )

        # --- rebuild -----------------------------------------------------
        _, rebuild_seconds = measure(
            "rebuild",
            lambda: engine.rebuild(log=lambda *args: None),
        )[:2]

        # --- delete ------------------------------------------------------
        victims = sorted(engine.snapshot().document_metadata)[:20]

        delete_seconds = 0.0

        if victims:

            started = time.perf_counter()

            engine.bulk_delete(victims, log=lambda *args: None)

            delete_seconds = time.perf_counter() - started

        database_bytes = os.path.getsize(engine.config.sqlite_db_file)

        rows = [
            ("corpus files written", f"{generation * 1000:.0f} ms"),
            ("documents indexed", f"{counts['documents']}/{len(paths)}"),
            ("import rejected/failed",
             f"{imported['rejected']}/{imported['failed']}"),
            ("content terms", f"{counts['content_terms']}"),
            ("", ""),
            ("import total", f"{import_seconds * 1000:.0f} ms"),
            ("import per document", f"{import_seconds / max(1, count) * 1000:.2f} ms"),
            (f"extraction p50 (n={len(extraction)})", f"{percentile(extraction, 0.50) * 1000:.2f} ms"),
            (f"extraction p95 (n={len(extraction)})", f"{percentile(extraction, 0.95) * 1000:.2f} ms"),
            ("", ""),
            (f"search p50 (n={len(cold)})", f"{percentile(cold, 0.50) * 1000:.2f} ms"),
            (f"search p95 (n={len(cold)})", f"{percentile(cold, 0.95) * 1000:.2f} ms"),
            (f"search max (n={len(cold)})", f"{max(cold) * 1000:.2f} ms"),
            (f"search p50 warm (n={len(warm)})", f"{percentile(warm, 0.50) * 1000:.2f} ms"),
            ("queries with results", f"{hits}/{len(query_words)}"),
            ("", ""),
            ("rebuild", f"{rebuild_seconds * 1000:.0f} ms"),
            (f"bulk delete (n={len(victims)})", f"{delete_seconds * 1000:.1f} ms"),
            ("", ""),
            ("sqlite size", f"{database_bytes / 1024:.0f} KiB"),
            ("sqlite per document", f"{database_bytes / max(1, count) / 1024:.2f} KiB"),
            ("peak resident memory", f"{resident_megabytes():.1f} MiB"),
        ]

        report(rows)

    finally:

        shutil.rmtree(temporary, ignore_errors=True)


def main(argv=None):

    parser = argparse.ArgumentParser(
        description="Benchmark the transport-independent search engine core.",
    )

    parser.add_argument(
        "--documents",
        type=int,
        default=200,
        help="Synthetic corpus size (default 200).",
    )

    parser.add_argument(
        "--words",
        type=int,
        default=400,
        help="Approximate words per document (default 400).",
    )

    parser.add_argument(
        "--queries",
        type=int,
        default=100,
        help="Queries to time (default 100).",
    )

    parser.add_argument(
        "--pdf-every",
        type=int,
        default=10,
        help="Make every Nth document a real PDF (default 10; 0 for none).",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Documents per import batch (default 100). Bounding this keeps "
             "file descriptors from being exhausted on large corpora.",
    )

    arguments = parser.parse_args(argv)

    run(
        arguments.documents,
        arguments.words,
        arguments.queries,
        arguments.pdf_every,
        arguments.batch_size,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
