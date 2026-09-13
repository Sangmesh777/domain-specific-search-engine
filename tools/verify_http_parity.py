"""
Refactor safety tool: diff two running servers' HTTP API behavior.

A refactor must not change behavior. This tool proves it by running a
reference build and the refactored build side by side against identical
copies of the same corpus, then comparing responses payload for payload.

Setup (from the repository root):

    # 1. reference build from any git revision
    rm -rf /tmp/baseline_app && mkdir -p /tmp/baseline_app
    git show 81faff9:app.py > /tmp/baseline_app/app.py
    cp -r data /tmp/baseline_app/data

    # 2. start it on its own port
    .venv/bin/python -c "import sys; sys.path.insert(0, '/tmp/baseline_app'); \
import app; app.app.run(host='127.0.0.1', port=5001, use_reloader=False)"

    # 3. start the build under test
    .venv/bin/python tools/serve.py --host 127.0.0.1 --port 5000

    # 4. diff
    .venv/bin/python tools/verify_http_parity.py

Only read-only endpoints are exercised. Mutating endpoints would diverge
the two corpora and make every later comparison meaningless; mutations are
covered instead by tests/test_phase12_live.py and
tests/test_search_engine_core.py, which run against a single server.

Exit code is 0 when every check matches, 1 otherwise.
"""

import argparse
import hashlib
import json
import os

import requests


OLD = "http://127.0.0.1:5001"
NEW = "http://127.0.0.1:5000"

QUERIES = [
    "network",
    "pdf",
    '"network"',
    "999",
    "BCS502",
    "BCS502 Module 2",
    "BCS502 Module 2.pdf",
    '"BCS502 Module 2.pdf"',
    "computer network",
    "routing",
    "rout",
    "network security",
    "machine learning",
    "docx",
    "txt",
    ".pdf",
    "network pdf",
    "normalization",
    "agile",
    "virtualization",
    "uniquephase12term3",
    "zzzznomatch",
    "1",
    "2",
    "12",
    "module 2",
    "OSI",
    "tcp",
    "encryption",
    "transactions",
    "",
    "   ",
    "the",
    "a",
    "Network Security Notes",
    "Network Security Notes.pdf",
    "Cyber Security Fundamentals.txt",
    "Data Structures and Algorithms",
    "pdf docx",
    "cloud",
    "layer",
    "protocols",
]

PAGINATION = [
    ("network", 1, 1),
    ("network", 1, 2),
    ("network", 2, 2),
    ("network", 99, 2),
    ("pdf", 1, 3),
    ("the", 1, 5),
    ("the", 2, 5),
    ("layer", 1, 50),
    ("layer", 3, 50),
]

DOCUMENTS = [
    "BCS502_Module_2.pdf",
    "Network_Security_Notes.pdf",
    "Cyber_Security_Fundamentals.txt",
]


def normalize(payload):
    """
    Rewrite deployment-specific values so two hosts can be compared.

    Exactly three things legitimately differ between two servers running
    the same code on the same corpus, and all three are absolute filesystem
    locations:

        "path"        a document's absolute path inside the data folder
        "data_folder" the data folder itself, reported by /api/status
        "indexing"    wall-clock timestamps and the generation counter

    Everything else — ranking order, scores, snippets, highlight offsets,
    pagination, match types, tags — is behavior, and is compared exactly.
    Normalizing only these keys is what lets the diff stay meaningful after
    a rebuild rewrites stored paths.
    """

    if isinstance(payload, dict):

        normalized = {}

        for key, value in payload.items():

            if key == "path" and isinstance(value, str):
                normalized[key] = os.path.basename(value)
            elif key in ("data_folder", "indexing"):
                normalized[key] = "<normalized>"
            else:
                normalized[key] = normalize(value)

        return normalized

    if isinstance(payload, list):
        return [normalize(item) for item in payload]

    return payload


def fetch(base, method, path, **kwargs):
    response = requests.request(
        method,
        f"{base}{path}",
        timeout=120,
        **kwargs,
    )

    return response.status_code, normalize(response.json())


def compare(label, old, new):
    if old != new:
        print(f"MISMATCH {label}")
        print(f"  old: {json.dumps(old, sort_keys=True)[:1200]}")
        print(f"  new: {json.dumps(new, sort_keys=True)[:1200]}")
        return 1

    return 0


def main(argv=None):
    global OLD
    global NEW

    parser = argparse.ArgumentParser(
        description="Diff two running servers' HTTP API behavior.",
    )

    parser.add_argument(
        "--old",
        default=OLD,
        help="Reference (pre-refactor) base URL.",
    )

    parser.add_argument(
        "--new",
        default=NEW,
        help="Build under test base URL.",
    )

    arguments = parser.parse_args(argv)

    OLD = arguments.old
    NEW = arguments.new

    print(f"parity: old={OLD} new={NEW}")

    failures = 0
    checks = 0

    # normalize() already reduces data_folder and the indexing timestamps,
    # so the corpus counters and the indexing state are compared directly.
    checks += 1
    failures += compare(
        "/api/status",
        fetch(OLD, "GET", "/api/status"),
        fetch(NEW, "GET", "/api/status"),
    )

    checks += 1
    failures += compare(
        "indexing state",
        requests.get(
            f"{OLD}/api/status", timeout=30
        ).json()["indexing"]["state"],
        requests.get(
            f"{NEW}/api/status", timeout=30
        ).json()["indexing"]["state"],
    )

    for query in QUERIES:
        checks += 1
        failures += compare(
            f"/api/search?q={query!r}",
            fetch(OLD, "GET", "/api/search", params={"q": query}),
            fetch(NEW, "GET", "/api/search", params={"q": query}),
        )

    for query, page, limit in PAGINATION:
        checks += 1
        failures += compare(
            f"/api/search?q={query!r}&page={page}&limit={limit}",
            fetch(
                OLD,
                "GET",
                "/api/search",
                params={"q": query, "page": page, "limit": limit},
            ),
            fetch(
                NEW,
                "GET",
                "/api/search",
                params={"q": query, "page": page, "limit": limit},
            ),
        )

    # Non-integer and out-of-range pagination parameters.
    for params in (
        {"q": "network", "page": "abc"},
        {"q": "network", "limit": "xyz"},
        {"q": "network", "page": "0"},
        {"q": "network", "limit": "0"},
        {"q": "network", "limit": "9999"},
        {"q": "network", "page": "-3"},
    ):
        checks += 1
        failures += compare(
            f"/api/search {params}",
            fetch(OLD, "GET", "/api/search", params=params),
            fetch(NEW, "GET", "/api/search", params=params),
        )

    for filename in DOCUMENTS:
        old = requests.get(
            f"{OLD}/api/documents/{filename}",
            timeout=60,
        )
        new = requests.get(
            f"{NEW}/api/documents/{filename}",
            timeout=60,
        )

        checks += 1
        failures += compare(
            f"GET /api/documents/{filename}",
            (
                old.status_code,
                old.headers.get("Content-Type"),
                hashlib.sha256(old.content).hexdigest(),
                len(old.content),
            ),
            (
                new.status_code,
                new.headers.get("Content-Type"),
                hashlib.sha256(new.content).hexdigest(),
                len(new.content),
            ),
        )

    for bad in ("../app.py", "..%2Fapp.py", "nope.txt"):
        old = requests.get(f"{OLD}/api/documents/{bad}", timeout=60)
        new = requests.get(f"{NEW}/api/documents/{bad}", timeout=60)

        checks += 1
        failures += compare(
            f"GET /api/documents/{bad}",
            (old.status_code, old.content[:200].decode("utf-8", "replace")),
            (new.status_code, new.content[:200].decode("utf-8", "replace")),
        )

    print(f"\nchecks: {checks}  mismatches: {failures}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
