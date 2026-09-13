import os
import sys
import tempfile
import time
from pathlib import Path

import requests


BASE_URL = "http://127.0.0.1:5000"
PREFIX = "phase12_regression_"
TEST_COUNT = 5


def request_json(method, url, **kwargs):
    kwargs.setdefault("timeout", 60)
    response = requests.request(
        method,
        url,
        **kwargs,
    )
    response.raise_for_status()
    return response.json()


def get_status():
    return request_json(
        "GET",
        f"{BASE_URL}/api/status",
    )


def search(query, **params):
    return request_json(
        "GET",
        f"{BASE_URL}/api/search",
        params={
            "q": query,
            **params,
        },
    )


def upload(paths):
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

        return request_json(
            "POST",
            f"{BASE_URL}/api/upload",
            files=files,
            timeout=120,
        )

    finally:
        for handle in handles:
            handle.close()


def replace_one(path):
    return upload([path])


def delete_one(filename):
    response = requests.delete(
        f"{BASE_URL}/api/documents/{filename}",
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def bulk_delete(filenames):
    return request_json(
        "POST",
        f"{BASE_URL}/api/documents/bulk-delete",
        json={
            "filenames": filenames
        },
        timeout=120,
    )


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    print("=" * 72)
    print("PHASE 12 — SEARCH / INDEX REGRESSION SUITE")
    print("=" * 72)

    baseline = get_status()
    baseline_documents = baseline["documents"]

    print(
        f"Baseline documents: "
        f"{baseline_documents}"
    )

    test_files = []

    with tempfile.TemporaryDirectory(
        prefix="phase12_regression_"
    ) as temp_dir:

        temp_path = Path(temp_dir)

        # ------------------------------------------------------------
        # Create five controlled documents.
        # ------------------------------------------------------------
        for index in range(1, TEST_COUNT + 1):

            path = (
                temp_path
                /
                f"{PREFIX}{index}.txt"
            )

            path.write_text(
                (
                    "phase12regression commonterm "
                    f"doc{index}\n"
                    f"uniquephase12term{index}\n"
                    f"numericfragment 99{index}xyz\n"
                ),
                encoding="utf-8",
            )

            test_files.append(path)

        filenames = [
            path.name
            for path in test_files
        ]

        # ------------------------------------------------------------
        # TEST 1 — Batch upload accounting.
        # ------------------------------------------------------------
        print("\n[TEST 1] Batch upload response accounting")

        result = upload(test_files)

        assert_true(
            result["uploaded_count"]
            == TEST_COUNT,
            (
                "uploaded_count mismatch: "
                f"{result['uploaded_count']} "
                f"!= {TEST_COUNT}"
            ),
        )

        assert_true(
            result["created_count"]
            == TEST_COUNT,
            (
                "created_count mismatch: "
                f"{result['created_count']} "
                f"!= {TEST_COUNT}"
            ),
        )

        assert_true(
            result["failed_count"] == 0,
            "Unexpected upload failures.",
        )

        status = get_status()

        assert_true(
            status["documents"]
            == baseline_documents + TEST_COUNT,
            (
                "Document count after upload is wrong: "
                f"{status['documents']}"
            ),
        )

        print("PASS")

        # ------------------------------------------------------------
        # TEST 2 — Exact search.
        # ------------------------------------------------------------
        print("\n[TEST 2] Exact token search")

        result = search(
            "uniquephase12term3"
        )

        titles = {
            item["title"]
            for item in result["results"]
        }

        assert_true(
            f"{PREFIX}3.txt" in titles,
            "Exact-token search failed.",
        )

        print("PASS")

        # ------------------------------------------------------------
        # TEST 3 — Numeric substring search.
        # ------------------------------------------------------------
        print("\n[TEST 3] Numeric substring search")

        result = search("993")

        titles = {
            item["title"]
            for item in result["results"]
        }

        assert_true(
            f"{PREFIX}3.txt" in titles,
            "Numeric substring search failed.",
        )

        print("PASS")

        # ------------------------------------------------------------
        # TEST 4 — Prefix search.
        # ------------------------------------------------------------
        print("\n[TEST 4] Prefix search")

        result = search(
            "uniquephase12"
        )

        titles = {
            item["title"]
            for item in result["results"]
        }

        assert_true(
            f"{PREFIX}1.txt" in titles
            and f"{PREFIX}5.txt" in titles,
            "Prefix search failed.",
        )

        print("PASS")

        # ------------------------------------------------------------
        # TEST 5 — Pagination.
        # ------------------------------------------------------------
        print("\n[TEST 5] Pagination")

        result = search(
            "phase12regression",
            page=1,
            limit=2,
        )

        pagination = result["pagination"]

        assert_true(
            pagination["limit"] == 2,
            "Pagination limit is incorrect.",
        )

        assert_true(
            pagination["total"]
            == TEST_COUNT,
            (
                "Pagination total is incorrect: "
                f"{pagination['total']}"
            ),
        )

        assert_true(
            pagination["total_pages"] == 3,
            (
                "Pagination total_pages should be 3, got "
                f"{pagination['total_pages']}"
            ),
        )

        assert_true(
            pagination["has_next"] is True,
            "Page 1 should have a next page.",
        )

        page2 = search(
            "phase12regression",
            page=2,
            limit=2,
        )

        assert_true(
            page2["pagination"]["has_previous"] is True,
            "Page 2 should have a previous page.",
        )

        print("PASS")

        # ------------------------------------------------------------
        # TEST 6 — Incremental replacement.
        # ------------------------------------------------------------
        print("\n[TEST 6] Incremental replacement")

        replacement_path = (
            temp_path
            /
            f"{PREFIX}3.txt"
        )

        replacement_path.write_text(
            (
                "phase12regression commonterm "
                "replacementuniquephase12\n"
                "replacement999\n"
            ),
            encoding="utf-8",
        )

        replacement_result = replace_one(
            replacement_path
        )

        assert_true(
            replacement_result["replaced_count"] == 1,
            "Replacement count is incorrect.",
        )

        old_result = search(
            "uniquephase12term3"
        )

        old_titles = {
            item["title"]
            for item in old_result["results"]
        }

        assert_true(
            f"{PREFIX}3.txt"
            not in old_titles,
            "Old replaced content is still searchable.",
        )

        new_result = search(
            "replacementuniquephase12"
        )

        new_titles = {
            item["title"]
            for item in new_result["results"]
        }

        assert_true(
            f"{PREFIX}3.txt" in new_titles,
            "New replacement content is not searchable.",
        )

        print("PASS")

        # ------------------------------------------------------------
        # TEST 7 — Single deletion.
        # ------------------------------------------------------------
        print("\n[TEST 7] Incremental single deletion")

        delete_result = delete_one(
            f"{PREFIX}3.txt"
        )

        assert_true(
            delete_result["deleted"]
            == f"{PREFIX}3.txt",
            "Single-delete response is incorrect.",
        )

        deleted_search = search(
            "replacementuniquephase12"
        )

        deleted_titles = {
            item["title"]
            for item in deleted_search["results"]
        }

        assert_true(
            f"{PREFIX}3.txt"
            not in deleted_titles,
            "Deleted document remains searchable.",
        )

        print("PASS")

        # ------------------------------------------------------------
        # TEST 8 — Bulk deletion.
        # ------------------------------------------------------------
        print("\n[TEST 8] Transactional bulk deletion")

        remaining = [
            f"{PREFIX}1.txt",
            f"{PREFIX}2.txt",
            f"{PREFIX}4.txt",
            f"{PREFIX}5.txt",
        ]

        bulk_result = bulk_delete(
            remaining
        )

        assert_true(
            bulk_result["deleted_count"]
            == len(remaining),
            (
                "Bulk-delete count mismatch: "
                f"{bulk_result['deleted_count']}"
            ),
        )

        assert_true(
            bulk_result["not_found_count"] == 0,
            "Bulk-delete unexpectedly reported missing files.",
        )

        assert_true(
            bulk_result["failed_count"] == 0,
            "Bulk-delete reported failures.",
        )

        print("PASS")

        # ------------------------------------------------------------
        # TEST 9 — Corpus restored.
        # ------------------------------------------------------------
        print("\n[TEST 9] Corpus restoration")

        final_status = get_status()

        assert_true(
            final_status["documents"]
            == baseline_documents,
            (
                "Corpus was not restored to baseline. "
                f"Expected {baseline_documents}, "
                f"got {final_status['documents']}"
            ),
        )

        print("PASS")

    print("\n" + "=" * 72)
    print("REGRESSION RESULT")
    print("=" * 72)
    print("Tests passed: 9/9")
    print("Search correctness: PASS")
    print("Pagination: PASS")
    print("Incremental replacement: PASS")
    print("Incremental deletion: PASS")
    print("Bulk transactional deletion: PASS")
    print("Corpus restoration: PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("\nREGRESSION FAILED")
        print(f"Reason: {error}")
        sys.exit(1)
