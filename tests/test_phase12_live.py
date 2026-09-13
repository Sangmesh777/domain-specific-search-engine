"""
Phase 12 live regression tests.

Prerequisite:
    python app.py

Run:
    python -m pytest tests/test_phase12_live.py -v

The tests create a small temporary corpus through the real HTTP API,
exercise the public behavior, and clean up their own documents.
"""

import os

import pytest
import requests


# Overridable so the same suite can be pointed at a reference build, which
# is how tools/verify_http_parity.py proves a refactor preserved mutation
# behavior and not just read behavior.
BASE_URL = os.environ.get(
    "VTU_API_BASE_URL",
    "http://127.0.0.1:5000",
)
PREFIX = "pytest_phase12_"
TEST_COUNT = 5


def server_available():
    """True when the live API is reachable."""

    try:
        requests.get(
            f"{BASE_URL}/api/status",
            timeout=2,
        )
        return True
    except requests.RequestException:
        return False


# These are live HTTP regression tests. They assert real server behavior,
# so they are skipped (never silently passed) when no server is running.
# Use ./run_tests.sh to start the server, seed the corpus and run the
# whole gate in one command.
pytestmark = pytest.mark.skipif(
    not server_available(),
    reason=(
        "live API regression tests require a server at "
        f"{BASE_URL} (python app.py, or ./run_tests.sh)"
    ),
)


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


# ============================================================
# INVALID INPUT AND ACCESS BOUNDARIES
# ============================================================
#
# Malformed requests must be refused with a 4xx and must leave the corpus
# untouched. A rejection that half-applies is worse than no validation, and
# these are the tests that would catch one. They are also the HTTP half of the
# file-safety story: the engine confines every name to the data folder, and
# this proves no endpoint hands back anything outside it.

def raw(method, path, **kwargs):
    """Like api_json, but returns 4xx/5xx responses instead of raising."""

    kwargs.setdefault("timeout", 60)

    return requests.request(
        method,
        f"{BASE_URL}{path}",
        **kwargs,
    )


def assert_corpus_unchanged(before):

    after = status()

    assert after["documents"] == before["documents"]
    assert after["content_terms"] == before["content_terms"]
    assert after["filenames_indexed"] == before["filenames_indexed"]
    assert after["page_text_entries"] == before["page_text_entries"]


def test_upload_with_no_file_part_is_refused():

    before = status()

    response = raw("POST", "/api/upload", data={})

    assert response.status_code == 400
    assert response.json()["error"] == "No files found"

    assert_corpus_unchanged(before)


def test_bulk_delete_refuses_malformed_json():

    before = status()

    response = raw(
        "POST",
        "/api/documents/bulk-delete",
        data="{this is not json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert "must be a JSON array" in response.json()["error"]

    assert_corpus_unchanged(before)


def test_bulk_delete_refuses_a_non_array_filenames_field():

    before = status()

    for value in ("one.txt", 42, {"name": "one.txt"}, None, True):

        response = raw(
            "POST",
            "/api/documents/bulk-delete",
            json={"filenames": value},
        )

        assert response.status_code == 400, value
        assert "must be a JSON array" in response.json()["error"], value

    assert_corpus_unchanged(before)


def test_bulk_delete_refuses_a_list_with_no_usable_names():
    """
    Names that normalize to nothing are not a batch.

    Checked separately from the array-shape error because the refusal has to
    happen before the engine is touched: an empty or fully invalid request
    must not bump the indexing generation the way a real batch does.
    """

    before = status()

    payloads = [
        [],
        ["", "   "],
        [None, 123, 4.5],
        ["/", "//", "///"],
    ]

    # Note "..": it is NOT in this list. os.path.basename("..") is "..", a
    # non-empty name, so a batch of [".."] normalizes to [".."] and is a real
    # request rather than an empty one. It is covered separately below, where
    # the interesting question is what it can reach.

    for payload in payloads:

        response = raw(
            "POST",
            "/api/documents/bulk-delete",
            json={"filenames": payload},
        )

        assert response.status_code == 400, payload
        assert (
            response.json()["error"]
            == "No valid filenames supplied."
        ), payload

    assert_corpus_unchanged(before)


def test_bulk_delete_refuses_traversal_names_without_touching_anything():
    """
    A traversal name mixed into an otherwise valid batch is reduced to its
    base name, so it cannot reach outside the data folder - and because no
    document has that name it is simply reported, not honored.

    The seeded corpus is asserted intact afterwards, which is the part that
    matters: the request must not delete the real documents either.
    """

    before = status()

    assert before["documents"] > 0

    response = raw(
        "POST",
        "/api/documents/bulk-delete",
        json={
            "filenames": [
                "../../../etc/passwd",
                "..%2F..%2Fapp.py",
                "/absolute/path/app.py",
            ]
        },
    )

    assert response.status_code == 200

    body = response.json()

    assert body["deleted"] == []
    assert body["deleted_count"] == 0
    assert body["failed"] == []

    # Reduced to base names, so nothing outside the corpus is addressable.
    # The percent-encoded entry is NOT decoded: URL decoding happens on the
    # path of a request, not inside a JSON body, so it stays a literal (and
    # nonexistent) filename. That asymmetry is deliberate and worth pinning,
    # because assuming otherwise is how a traversal hole opens.
    assert sorted(body["not_found"]) == [
        "..%2F..%2Fapp.py",
        "app.py",
        "passwd",
    ]

    assert_corpus_unchanged(before)


def test_document_access_never_leaves_the_data_folder():

    before = status()

    candidates = [
        "..",
        "..%2Fapp.py",
        "..%2F..%2Fetc%2Fpasswd",
        "%2Fetc%2Fpasswd",
        "app.py",
        "search_engine%2Fengine.py",
        "nope.txt",
    ]

    for candidate in candidates:

        response = raw(
            "GET",
            f"/api/documents/{candidate}",
        )

        assert response.status_code in (400, 404), (
            f"{candidate} returned {response.status_code}"
        )

        # The specific harm to rule out: source code served as a document.
        assert b"from flask import" not in response.content, candidate
        assert b"def search_documents" not in response.content, candidate

    assert_corpus_unchanged(before)


def test_document_delete_never_leaves_the_data_folder():

    before = status()

    for candidate in ("..", "..%2Fapp.py", "app.py"):

        response = raw(
            "DELETE",
            f"/api/documents/{candidate}",
        )

        assert response.status_code in (400, 404), (
            f"{candidate} returned {response.status_code}"
        )

    assert_corpus_unchanged(before)

    # The application source is still on disk and still serving.
    assert raw("GET", "/api/status").status_code == 200


def test_search_falls_back_to_default_pagination_on_garbage():
    """
    Non-numeric pagination is not an error; it falls back to page 1, limit 10.

    Pinned here as well as in the parity sweep because the fallback is a
    documented default rather than an accident, and a port that returned 400
    for `page=abc` would be wrong.
    """

    payload = search("network", page="abc", limit="xyz")

    assert payload["pagination"]["page"] == 1
    assert payload["pagination"]["limit"] == 10

    negative = search("network", page="-3", limit="-5")

    assert negative["pagination"]["page"] == 1
    assert negative["pagination"]["limit"] == 1


def test_dot_dot_is_harmless_on_both_delete_endpoints():
    """
    The one degenerate name the two delete endpoints disagree about.

    `DELETE /api/documents/%2E%2E` reaches the engine, fails the containment
    check and answers 400 "Invalid document path.". The same name inside a
    bulk-delete body answers 200 with `not_found`, because bulk delete reduces
    names with basename and then simply cannot find "..". Both are safe - the
    parent of the data folder is never addressable through either - but they
    are not the same status code, and a port that assumes symmetry will report
    the wrong thing.

    A literal ".." in a URL path never reaches the application at all: the WSGI
    layer normalizes it away and answers its own 404. Only the percent-encoded
    form arrives, which is why both spellings are checked.

    Percent-decoding is also asymmetric between transports and is pinned here:
    `%2F` in a URL path decodes to a separator that basename then strips, while
    `%2F` in a JSON body stays literal text.
    """

    before = status()

    encoded = raw("DELETE", "/api/documents/%2E%2E")

    assert encoded.status_code == 400
    assert encoded.json()["error"] == "Invalid document path."

    literal = raw("DELETE", "/api/documents/..")

    assert literal.status_code == 404
    assert b"def search_documents" not in literal.content

    bulk = raw(
        "POST",
        "/api/documents/bulk-delete",
        json={"filenames": ["..", "../", "../../"]},
    )

    assert bulk.status_code == 200

    body = bulk.json()

    assert body["deleted"] == []
    assert body["deleted_count"] == 0
    assert body["failed"] == []

    # "../" and "../../" normalize to empty and drop out; ".." survives as a
    # name and is simply not found.
    assert body["not_found"] == [".."]

    # In a URL path the encoded separator IS decoded, then stripped to a base
    # name, so this is an ordinary not-indexed lookup rather than a traversal.
    through_path = raw("GET", "/api/documents/..%2Fapp.py")

    assert through_path.status_code == 404
    assert through_path.json()["error"] == "Document is not indexed."
    assert b"from flask import" not in through_path.content

    assert_corpus_unchanged(before)

    # Nothing outside the corpus was touched, and the app still serves.
    assert raw("GET", "/api/status").status_code == 200
