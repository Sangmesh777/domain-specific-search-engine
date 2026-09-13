"""
Phase 12 live regression tests.

Prerequisite:
    python app.py

Run:
    python -m pytest tests/test_phase12_live.py -v

The tests create a small temporary corpus through the real HTTP API,
exercise the public behavior, and clean up their own documents.
"""

from pathlib import Path
import tempfile

import pytest
import requests


BASE_URL = "http://127.0.0.1:5000"
PREFIX = "pytest_phase12_"
TEST_COUNT = 5


def api_json(method, path, **kwargs):
    kwargs.setdefault("timeout", 60)

    response = requests.request(
        method,
        f"{BASE_URL}{path}",
        **kwargs,
    )

    response.raise_for_status()
    return response.json()


def status():
    return api_json("GET", "/api/status")


def search(query, **params):
    return api_json(
        "GET",
        "/api/search",
        params={"q": query, **params},
    )


def upload(paths):
    handles = []
    multipart = []

    try:
        for path in paths:
            handle = open(path, "rb")
            handles.append(handle)
            multipart.append(
                (
                    "file",
                    (
                        path.name,
                        handle,
                        "text/plain",
                    ),
                )
            )

        return api_json(
            "POST",
            "/api/upload",
            files=multipart,
            timeout=120,
        )

    finally:
        for handle in handles:
            handle.close()


def delete_one(filename):
    return api_json(
        "DELETE",
        f"/api/documents/{filename}",
    )


def bulk_delete(filenames):
    return api_json(
        "POST",
        "/api/documents/bulk-delete",
        json={"filenames": filenames},
        timeout=120,
    )


@pytest.fixture(scope="module")
def baseline():
    data = status()
    return {
        "documents": data["documents"],
        "content_terms": data["content_terms"],
    }


@pytest.fixture
def test_corpus(tmp_path):
    paths = []

    for index in range(1, TEST_COUNT + 1):
        path = tmp_path / f"{PREFIX}{index}.txt"

        path.write_text(
            (
                "phase12regression commonterm "
                f"doc{index}\n"
                f"uniquephase12term{index}\n"
                f"numericfragment 99{index}xyz\n"
            ),
            encoding="utf-8",
        )

        paths.append(path)

    result = upload(paths)

    assert result["uploaded_count"] == TEST_COUNT
    assert result["failed_count"] == 0

    yield paths

    bulk_delete(
        [path.name for path in paths]
    )


def test_batch_upload_accounting(baseline, test_corpus):
    data = status()

    assert data["documents"] == (
        baseline["documents"] + TEST_COUNT
    )


def test_exact_token_search(test_corpus):
    result = search("uniquephase12term3")

    titles = {
        item["title"]
        for item in result["results"]
    }

    assert f"{PREFIX}3.txt" in titles


def test_numeric_substring_search(test_corpus):
    result = search("993")

    titles = {
        item["title"]
        for item in result["results"]
    }

    assert f"{PREFIX}3.txt" in titles


def test_prefix_search(test_corpus):
    result = search("uniquephase12")

    titles = {
        item["title"]
        for item in result["results"]
    }

    assert f"{PREFIX}1.txt" in titles
    assert f"{PREFIX}5.txt" in titles


def test_pagination(test_corpus):
    page1 = search(
        "phase12regression",
        page=1,
        limit=2,
    )

    assert page1["pagination"]["total"] == TEST_COUNT
    assert page1["pagination"]["total_pages"] == 3
    assert page1["pagination"]["has_next"] is True

    page2 = search(
        "phase12regression",
        page=2,
        limit=2,
    )

    assert page2["pagination"]["has_previous"] is True


def test_incremental_replacement(test_corpus):
    replacement = test_corpus[2]

    replacement.write_text(
        (
            "phase12regression commonterm "
            "replacementuniquephase12\n"
            "replacement999\n"
        ),
        encoding="utf-8",
    )

    result = upload([replacement])

    assert result["uploaded_count"] == 1
    assert result["replaced_count"] == 1
    assert result["failed_count"] == 0

    old = search("uniquephase12term3")
    old_titles = {
        item["title"]
        for item in old["results"]
    }
    assert f"{PREFIX}3.txt" not in old_titles

    new = search("replacementuniquephase12")
    new_titles = {
        item["title"]
        for item in new["results"]
    }
    assert f"{PREFIX}3.txt" in new_titles


def test_incremental_single_delete(test_corpus):
    filename = test_corpus[2].name

    delete_result = delete_one(filename)

    assert delete_result["deleted"] == filename

    result = search("replacementuniquephase12")

    titles = {
        item["title"]
        for item in result["results"]
    }

    assert filename not in titles


def test_bulk_delete_transaction(test_corpus):
    filenames = [
        test_corpus[0].name,
        test_corpus[1].name,
        test_corpus[3].name,
        test_corpus[4].name,
    ]

    result = bulk_delete(filenames)

    assert result["deleted_count"] == len(filenames)
    assert result["not_found_count"] == 0
    assert result["failed_count"] == 0


def test_corpus_health(baseline):
    data = status()

    # Every test using test_corpus cleans its own files. The live server
    # may legitimately contain user documents created outside the suite,
    # so assert that the corpus did not lose baseline documents.
    assert data["documents"] >= baseline["documents"]
    assert data["content_terms"] > 0
