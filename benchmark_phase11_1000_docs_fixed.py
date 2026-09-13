import os
import statistics
import tempfile
import time
from pathlib import Path

import requests


BASE_URL = "http://127.0.0.1:5000"
BENCH_PREFIX = "bench_phase11_"
FILE_COUNT = 1000
SEARCH_RUNS = 20


def api_json(method, url, **kwargs):
    kwargs.setdefault("timeout", 120)

    response = requests.request(
        method,
        url,
        **kwargs,
    )
    response.raise_for_status()
    return response.json()


def get_status():
    return api_json(
        "GET",
        f"{BASE_URL}/api/status",
    )


def search(query):
    started = time.perf_counter()

    data = api_json(
        "GET",
        f"{BASE_URL}/api/search",
        params={"q": query},
        timeout=30,
    )

    elapsed = time.perf_counter() - started
    return data, elapsed


def upload_files(paths):
    files = []

    opened = []

    try:
        for path in paths:
            handle = open(path, "rb")
            opened.append(handle)
            files.append(
                (
                    "file",
                    (
                        path.name,
                        handle,
                        "text/plain",
                    ),
                )
            )

        started = time.perf_counter()

        response = requests.post(
            f"{BASE_URL}/api/upload",
            files=files,
            timeout=300,
        )

        response.raise_for_status()

        elapsed = time.perf_counter() - started

        return response.json(), elapsed

    finally:
        for handle in opened:
            handle.close()


def cleanup_benchmark_documents():
    """
    Delete any benchmark documents left behind by an interrupted run.
    """
    deleted = 0

    for index in range(1, FILE_COUNT + 2):
        filename = f"{BENCH_PREFIX}{index:04d}.txt"

        try:
            response = requests.delete(
                f"{BASE_URL}/api/documents/{filename}",
                timeout=30,
            )

            if response.status_code == 200:
                deleted += 1

        except Exception:
            pass

    return deleted


def delete_file(filename):
    started = time.perf_counter()

    response = requests.delete(
        f"{BASE_URL}/api/documents/{filename}",
        timeout=30,
    )

    response.raise_for_status()

    elapsed = time.perf_counter() - started
    return response.json(), elapsed


def main():
    print("=" * 72)
    print("PHASE 11 — 1000 DOCUMENT INCREMENTAL INDEX BENCHMARK")
    print("=" * 72)

    print("\n[1/6] Checking server...")
    baseline = get_status()

    print(
        f"Documents before benchmark : "
        f"{baseline.get('documents')}"
    )

    print(
        f"Content terms before       : "
        f"{baseline.get('content_terms')}"
    )

    if baseline.get("documents") is None:
        raise RuntimeError(
            "Unexpected /api/status response."
        )

    with tempfile.TemporaryDirectory(
        prefix="phase11_benchmark_"
    ) as temp_dir:

        temp_path = Path(temp_dir)

        print("\n[2/6] Generating 1000 synthetic TXT documents...")

        paths = []

        started = time.perf_counter()

        for index in range(1, FILE_COUNT + 1):

            path = (
                temp_path
                /
                f"{BENCH_PREFIX}{index:04d}.txt"
            )

            path.write_text(
                (
                    f"benchmark document {index}\n"
                    f"benchmarktopic commonterm\n"
                    f"unique_benchmark_term_{index}\n"
                    f"engineering search benchmark corpus\n"
                ),
                encoding="utf-8",
            )

            paths.append(path)

        generation_time = (
            time.perf_counter()
            - started
        )

        print(
            f"Generated {len(paths)} files in "
            f"{generation_time:.3f}s"
        )

        print("\n[3/6] Uploading 1000 files...")
        print(
            "This is intentionally a real API test; "
            "the benchmark may take a while."
        )

        upload_data, batch_upload_time = upload_files(
            paths
        )

        print(
            f"Batch upload/index time: "
            f"{batch_upload_time:.3f}s"
        )

        print(
            f"Uploaded count          : "
            f"{upload_data.get('uploaded_count')}"
        )

        print(
            f"Created count           : "
            f"{upload_data.get('created_count')}"
        )

        after_batch = get_status()

        print(
            f"Documents after batch   : "
            f"{after_batch.get('documents')}"
        )

        print("\n[4/6] Testing search latency...")

        queries = [
            "benchmarktopic",
            "engineering search benchmark",
            "unique_benchmark_term_0500",
            f"{BENCH_PREFIX}0500",
        ]

        timings = []

        for run_index in range(SEARCH_RUNS):

            query = queries[
                run_index % len(queries)
            ]

            data, elapsed = search(query)

            timings.append(elapsed)

            if not data.get("results"):
                raise RuntimeError(
                    f"Benchmark search returned no results: {query}"
                )

        median_ms = (
            statistics.median(timings)
            * 1000
        )

        p95_ms = (
            statistics.quantiles(
                timings,
                n=20,
                method="inclusive",
            )[18]
            * 1000
        )

        max_ms = (
            max(timings)
            * 1000
        )

        print(
            f"Search median : {median_ms:.2f} ms"
        )

        print(
            f"Search p95    : {p95_ms:.2f} ms"
        )

        print(
            f"Search max    : {max_ms:.2f} ms"
        )

        print("\n[5/6] Testing ONE additional upload...")

        single_path = (
            temp_path
            /
            f"{BENCH_PREFIX}1001.txt"
        )

        single_path.write_text(
            (
                "benchmark document 1001\n"
                "benchmarktopic commonterm\n"
                "incremental_single_upload_test\n"
            ),
            encoding="utf-8",
        )

        _, single_upload_time = upload_files(
            [single_path]
        )

        after_single = get_status()

        print(
            f"Single-file upload/index time: "
            f"{single_upload_time:.3f}s"
        )

        print(
            f"Documents after single upload : "
            f"{after_single.get('documents')}"
        )

        print("\n[6/6] Cleaning up benchmark documents...")

        cleanup_started = time.perf_counter()

        deleted = 0

        for index in range(
            1,
            FILE_COUNT + 2,
        ):

            filename = (
                f"{BENCH_PREFIX}{index:04d}.txt"
            )

            delete_file(filename)

            deleted += 1

            if deleted % 100 == 0:
                print(
                    f"Deleted {deleted}/{FILE_COUNT + 1}"
                )

        cleanup_time = (
            time.perf_counter()
            - cleanup_started
        )

        final_status = get_status()

    print("\n" + "=" * 72)
    print("BENCHMARK RESULT")
    print("=" * 72)

    print(
        f"Batch index 1000 docs : "
        f"{batch_upload_time:.3f}s"
    )

    print(
        f"Single new document   : "
        f"{single_upload_time:.3f}s"
    )

    print(
        f"Search median         : "
        f"{median_ms:.2f} ms"
    )

    print(
        f"Search p95            : "
        f"{p95_ms:.2f} ms"
    )

    print(
        f"Cleanup {FILE_COUNT + 1} docs  : "
        f"{cleanup_time:.3f}s"
    )

    print(
        f"Final documents       : "
        f"{final_status.get('documents')}"
    )

    print(
        f"Final content terms  : "
        f"{final_status.get('content_terms')}"
    )

    print("\nIMPORTANT:")
    print(
        "The key metric is SINGLE new document time. "
        "It should not grow linearly with the 1000-document corpus."
    )


if __name__ == "__main__":
    import sys

    if "--cleanup-only" in sys.argv:
        print(
            f"Deleted benchmark documents: "
            f"{cleanup_benchmark_documents()}"
        )
    else:
        main()
