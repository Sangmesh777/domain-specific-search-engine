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

# One query per scoring branch, so a change in any single weight or
# threshold shows up as a named mismatch instead of hiding inside a
# generic query list. Comments name the branch each query exercises.
BRANCH_QUERIES = [
    # filetype-only browse: flat score, sorted by title
    "pdf",
    "docx",
    "txt",

    # bare-list branch: filetype filter with no scoring document
    "zzzqqq pdf",

    # bare-list branch: quoted phrase matching nothing
    '"zzzqqq wwwww"',

    # quoted phrase that matches: phrase weight set 0.05/0.15/0.80/0
    '"network security"',
    '"machine learning"',

    # quoted phrase inside a filename: the +100 filename bonus
    '"Module 2"',

    # quoted filename: the +30 quoted filename bonus
    '"Network Security Notes.pdf"',

    # exact filename: the +100 bonus
    "Network Security Notes.pdf",

    # >=2-word filename substring: the +50 bonus
    "Cyber Security",

    # partial multi-word filename: 0.75 * coverage filename relevance
    "cyber fundamentals",

    # per-word filename bonus (+20), numeric word skipped
    "module 2",
    "502 module",

    # prefix match, len >= 3: similarity len(q)/len(t) clamped 0.25..0.90
    "netwo",
    "encrypt",
    "virtual",

    # numeric substring, digit run >= 2 and not a prefix: ratio * 0.70
    "502",
    "999",
    "12",

    # ordinary topic search: weights 0.05/0.60/0.20/0.15
    "network",
    "algorithm",
    "encryption",

    # no match at all
    "zzzqqq",
]

# Of the three bare-list sites in search_engine/search.py, only two are
# reachable, and only one of those over HTTP against a populated corpus:
#
#   1. no documents at all, or no documents of the filtered type. The live
#      corpus holds all three supported types, so HTTP cannot reach it;
#      tests/test_search_engine_core.py covers both variants directly.
#   2. a quoted phrase that no document contains. Reachable here.
#   3. filetype narrowing that empties document_scores. Unreachable: scores
#      are pre-populated for every document, and total_documents is counted
#      through the same filetype predicate, so site 1 always fires first.
BARE_LIST_QUERIES = [
    '"zzzqqq wwwww"',
]

# Behavior changes approved after the refactor parity proof was closed out
# (90 checks / 0 mismatches at commit 9d3a05a). These are fixes, not
# refactor drift, and each is pinned by a test in
# tests/test_search_engine_core.py.
#
# Cause: filename_terms was deduplicated with set(filename_words), whose
# iteration order follows PYTHONHASHSEED. Ranking compares the query against
# a space-joined word list, so a permuted order silently changed which
# filename bonuses applied. Both storage paths now use dict.fromkeys.
#
# These reproduce only when the reference build has RESTARTED after a
# rebuild, because a running server holds the correct order in memory and
# the permutation exists only in the persisted rows. If they do not
# reproduce, the tool says so rather than failing.
APPROVED_CHANGES = {
    "computer network": (
        "Computer_Network_Technologies.docx regains the +50 filename "
        "substring bonus (score 0.7625 -> 0.9625); its words were persisted "
        "as computer/technologies/network."
    ),
    "machine learning": (
        "Machine_Learning_Introduction.pdf regains the +50 substring bonus "
        "(0.7625 -> 0.9625); persisted as introduction/learning/machine."
    ),
    '"machine learning"': (
        "Same document, quoted form (0.725 -> 0.96)."
    ),
    "Cyber Security": (
        "Cyber_Security_Fundamentals.txt regains the +50 substring bonus "
        "(0.7625 -> 0.9625); persisted as security/cyber/fundamentals."
    ),
    "Cyber Security Fundamentals.txt": (
        "Exact-filename query regains the +100 bonus and reaches the full "
        "score (0.7625 -> 1.0, filename_score 60 -> 160)."
    ),
    "Data Structures and Algorithms": (
        "Exact-name query regains the +100 bonus (0.7625 -> 1.0, "
        "filename_score 80 -> 180); the words were persisted fully reversed "
        "as algorithms/and/structures/data."
    ),
    "cyber fundamentals": (
        "The only score that DROPS, and it is a false positive being "
        "removed (0.9625 -> 0.7125, filename_score 90 -> 40). The permuted "
        "order security/cyber/fundamentals made 'cyber fundamentals' a "
        "contiguous substring of the filename by accident; in the real "
        "order cyber/security/fundamentals it is not, so the +50 substring "
        "bonus was never owed. Per-word bonuses still apply."
    ),
}


DOCUMENTS = [
    "BCS502_Module_2.pdf",
    "Network_Security_Notes.pdf",
    "Cyber_Security_Fundamentals.txt",
]


def canonicalize_ranking(payload):
    """
    Normalize the order of equal-scoring results, and nothing else.

    Results are sorted by descending score with a stable sort, so documents
    with identical scores keep the order they were inserted in - and after a
    rebuild that order is `os.listdir` order of the data folder. Two servers
    on two filesystem locations therefore legitimately disagree about the
    order of a tie, while agreeing on every score, snippet and field.

    Sorting each run of equal scores by title removes exactly that
    deployment-specific freedom. Ordering between distinct scores, and every
    value inside a result, is still compared exactly.

    Known limit: a tie group split across a page boundary still reports a
    mismatch, because the two servers put different documents on the page.
    The durable fix is deterministic enumeration in rebuild, which is a
    behavior change and is reported separately rather than made here.
    """

    if not isinstance(payload, dict):
        return payload

    if "results" not in payload or "pagination" not in payload:
        return payload

    results = payload["results"]

    ordered = []
    group = []
    previous = None

    for item in results:

        if group and item.get("score") != previous:
            ordered.extend(
                sorted(group, key=lambda entry: entry.get("title", ""))
            )
            group = []

        group.append(item)
        previous = item.get("score")

    if group:
        ordered.extend(
            sorted(group, key=lambda entry: entry.get("title", ""))
        )

    canonical = dict(payload)
    canonical["results"] = ordered

    return canonical


def normalize(payload):
    """
    Rewrite deployment-specific values so two hosts can be compared.

    Exactly three things legitimately differ between two servers running
    the same code on the same corpus, and all three are absolute filesystem
    locations:

        "path"        a document's absolute path inside the data folder
        "data_folder" the data folder itself, reported by /api/status
        "indexing"    wall-clock timestamps and the generation counter

    A fourth difference is the order of equal-scoring results, handled by
    canonicalize_ranking once the paths above are reduced.

    Everything else — ranking order, scores, snippets, highlight offsets,
    pagination, match types, tags — is behavior, and is compared exactly.
    Normalizing only these is what lets the diff stay meaningful after a
    rebuild rewrites stored paths.
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

        return canonicalize_ranking(normalized)

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


def compare(label, old, new, approved=None):
    """
    Compare one check.

    `approved` marks a difference that is an intended behavior change rather
    than a regression. Approved differences are reported and do not fail the
    run; everything else does. An approved difference that stops reproducing
    is noted, because that means the reference build or the fix moved.
    """

    if old != new:

        if approved:
            print(f"APPROVED CHANGE {label}")
            print(f"  {approved}")
            return 0

        print(f"MISMATCH {label}")
        print(f"  old: {json.dumps(old, sort_keys=True)[:1200]}")
        print(f"  new: {json.dumps(new, sort_keys=True)[:1200]}")
        return 1

    if approved:
        print(f"NOTE {label}")
        print(f"  approved change did not reproduce: {approved}")

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
            approved=APPROVED_CHANGES.get(query),
        )

    for query in BRANCH_QUERIES:
        checks += 1
        failures += compare(
            f"/api/search?q={query!r} [branch]",
            fetch(OLD, "GET", "/api/search", params={"q": query}),
            fetch(NEW, "GET", "/api/search", params={"q": query}),
            approved=APPROVED_CHANGES.get(query),
        )

    # Guard the quirk itself: if these ever come back as a paginated
    # object, the corpus no longer exercises the bare-list branches and
    # the sweep would be silently weaker.
    for query in BARE_LIST_QUERIES:

        old_shape = type(
            fetch(OLD, "GET", "/api/search", params={"q": query})[1]
        ).__name__

        new_shape = type(
            fetch(NEW, "GET", "/api/search", params={"q": query})[1]
        ).__name__

        checks += 1
        failures += compare(
            f"/api/search?q={query!r} reaches the bare-list branch",
            ["list", "list"],
            [old_shape, new_shape],
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
