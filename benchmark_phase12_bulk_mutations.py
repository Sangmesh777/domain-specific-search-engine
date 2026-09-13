import tempfile
import time
from pathlib import Path

import requests


BASE_URL = "http://127.0.0.1:5000"
PREFIX = "phase12_bulk_bench_"
COUNT = 1000


def api_json(method, url, **kwargs):
    kwargs.setdefault("timeout", 300)
    response = requests.request(
        method,
        url,
        **kwargs,
    )
    response.raise_for_status()
    return response.json()


def create_test_files(folder):
    paths = []

    for index in range(1, COUNT + 1):
        path = folder / f"{PREFIX}{index:04d}.txt"
        path.write_text(
            (
                f"phase 12 benchmark document {index}\n"
                f"bulkbenchmark commonterm {index}\n"
                f"unique_phase12_term_{index}\n"
            ),
            encoding="utf-8",
        )
        paths.append(path)

    return paths


def upload_files(paths):
    handles = []
    files = []

    try:
        for path in paths:
            handle = open(path, "rb")
            handles.append(handle)
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
            timeout=600,
        )
        response.raise_for_status()

        elapsed = time.perf_counter() - started
        return response.json(), elapsed

    finally:
        for handle in handles:
            handle.close()


def bulk_delete(filenames):
    payload = {
        "filenames": filenames
    }

    started = time.perf_counter()

    data = api_json(
        "POST",
        f"{BASE_URL}/api/documents/bulk-delete",
        json=payload,
        timeout=600,
    )

    elapsed = time.perf_counter() - started

    return data, elapsed


def main():
    print("=" * 72)
    print("PHASE 12 — BULK MUTATION BENCHMARK")
    print("=" * 72)

    baseline = api_json(
        "GET",
        f"{BASE_URL}/api/status",
    )

    print(
        f"Documents before benchmark : "
        f"{baseline.get('documents')}"
    )

    with tempfile.TemporaryDirectory(
        prefix="phase12_bulk_benchmark_"
    ) as temp_dir:

        folder = Path(temp_dir)

        print("\n[1/4] Generating 1000 benchmark files...")
        started = time.perf_counter()
        paths = create_test_files(folder)
        generation_time = time.perf_counter() - started

        print(
            f"Generated {len(paths)} files in "
            f"{generation_time:.3f}s"
        )

        print("\n[2/4] Batch uploading 1000 files...")
        upload_data, upload_time = upload_files(paths)

        print(
            f"Batch upload/index time : "
            f"{upload_time:.3f}s"
        )
        print(
            f"Uploaded count           : "
            f"{upload_data.get('uploaded_count')}"
        )

        status_after_upload = api_json(
            "GET",
            f"{BASE_URL}/api/status",
        )

        print(
            f"Documents after upload   : "
            f"{status_after_upload.get('documents')}"
        )

        filenames = [
            path.name
            for path in paths
        ]

        print("\n[3/4] Bulk deleting the same 1000 files...")
        delete_data, delete_time = bulk_delete(
            filenames
        )

        print(
            f"Bulk delete time         : "
            f"{delete_time:.3f}s"
        )
        print(
            f"Deleted count            : "
            f"{delete_data.get('deleted_count')}"
        )
        print(
            f"Not found count          : "
            f"{delete_data.get('not_found_count')}"
        )
        print(
            f"Failed count             : "
            f"{delete_data.get('failed_count')}"
        )

        print("\n[4/4] Verifying final database state...")
        final_status = api_json(
            "GET",
            f"{BASE_URL}/api/status",
        )

        print(
            f"Final documents          : "
            f"{final_status.get('documents')}"
        )
        print(
            f"Final content terms      : "
            f"{final_status.get('content_terms')}"
        )

    print("\n" + "=" * 72)
    print("PHASE 12 BENCHMARK RESULT")
    print("=" * 72)

    print(
        f"Batch upload 1000 docs : "
        f"{upload_time:.3f}s"
    )
    print(
        f"Bulk delete 1000 docs  : "
        f"{delete_time:.3f}s"
    )
    print(
        f"Final documents        : "
        f"{final_status.get('documents')}"
    )

    print("\nReference from Phase 11:")
    print(
        "Deleting 1001 documents individually took "
        "~44.411s."
    )


if __name__ == "__main__":
    main()
