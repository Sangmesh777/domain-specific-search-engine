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

    The fixed build enumerates a rebuild with sorted(os.listdir()), so its
    own tie order is deterministic. This canonicalization is still needed
    when diffing against a reference build from before that change, whose
    tie order follows its filesystem.

    Known limit: a tie group split across a page boundary still reports a
    mismatch, because the two servers put different documents on the page.
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


def compare(label, old, new):
    if old != new:
        print(f"MISMATCH {label}")
        print(f"  old: {json.dumps(old, sort_keys=True)[:1200]}")
        print(f"  new: {json.dumps(new, sort_keys=True)[:1200]}")
        return 1

    return 0


# Score fields whose values are computed from the order of a document's
# filename words. A difference confined to these, over an unchanged set of
# documents, is the expected effect of the deterministic filename-order fix
# rather than a regression.
FILENAME_DERIVED_FIELDS = {
    "filename_score",
    "phrase_score",
    "score",
    "match_type",
}


def is_quoted_phrase_query(query):
    """True when the raw query is wrapped in double quotes."""

    if not isinstance(query, str):
        return False

    stripped = query.strip()

    return (
        len(stripped) >= 2
        and stripped.startswith('"')
        and stripped.endswith('"')
    )


def classify_shape_delta(label, query, old_payload, new_payload):
    """
    Recognise the third manifestation of the filename-word-order fix: a
    change of response SHAPE, not just of scores.

    The quoted-phrase branch answers with a bare empty list when no document
    matches the phrase. Whether a document matches is decided by
    `normalized_phrase in " ".join(filename_words)`, so a permuted word order
    can make the reference match nothing at all and take the bare-list branch,
    while the fixed build matches and returns a page object.

    Example from the seed corpus, for the query `"BCS502 Module 2.pdf"`:

        reference   filename_words = ['bcs502', '2', 'module']
                    joined = 'bcs502 2 module'   -> phrase absent -> []
        fixed       filename_words = ['bcs502', 'module', '2']
                    joined = 'bcs502 module 2'   -> phrase present -> page

    The guards are deliberately tight, because this rule is the one place the
    classifier tolerates a shape change and a shape change is otherwise the
    loudest kind of regression:

        one side is a bare list and it is exactly empty
        the other side is a page object with a non-empty result set
        the raw query is wrapped in double quotes
        every result on the page side is filename-derived, i.e. it carries
        both a filename score and a phrase score

    That last guard is what keeps this from excusing a content-search
    regression: only the filename path can be flipped by word order.
    """

    if isinstance(old_payload, list):
        bare, page = old_payload, new_payload
        direction = "reference answered a bare list, fixed build a page"
    elif isinstance(new_payload, list):
        bare, page = new_payload, old_payload
        direction = "fixed build answered a bare list, reference a page"
    else:
        return False

    if not isinstance(page, dict):
        return False

    if bare != []:
        return False

    if not is_quoted_phrase_query(query):
        return False

    results = page.get("results") or []

    if not results:
        return False

    for item in results:

        if not isinstance(item, dict):
            return False

        filename_derived = (
            item.get("filename_score", 0) > 0
            and item.get("phrase_score", 0) > 0
        )

        if not filename_derived:
            return False

    print(f"FILENAME-ORDER DELTA {label} [shape]")
    print(f"  {direction}; the quoted phrase matches a filename only")
    print("  when the filename words are in tokenization order.")

    for item in results:
        print(
            f"    {item.get('title')}: "
            f"filename_score={item.get('filename_score')}, "
            f"phrase_score={item.get('phrase_score')}, "
            f"score={item.get('score')}, "
            f"match_type={item.get('match_type')!r}"
        )

    return True


def compare_search(label, old, new, query=None):
    """
    Compare one /api/search check, structurally rather than by query name.

    Identical payloads pass. A difference confined to filename-derived
    scores, over an unchanged document set and unchanged pagination, is
    reported as a filename-order delta and does not fail. Everything else
    fails.

    `query` is the raw query string, needed only by the shape rule: a
    quoted phrase whose filename match is flipped by word order changes the
    RESPONSE SHAPE (bare empty list versus page object), because the
    bare-list branch fires exactly when no document matched the phrase.
    See classify_shape_delta for the guards.

    Structural classification matters because the pre-fix reference is
    nondeterministic: which of its documents came back with permuted
    filename words depends on the PYTHONHASHSEED of the process that last
    wrote its database. An allowlist of affected query strings therefore
    changed from run to run and could not be trusted in either direction -
    a query that produced a clean diff in one run produced a shape change in
    the next. Classifying the shape of the difference is stable, and still
    fails on any delta that touches content scores, snippets, highlights,
    pages, document membership or pagination.
    """

    if old == new:
        return 0

    old_status, old_payload = old
    new_status, new_payload = new

    if old_status != new_status:
        print(f"MISMATCH {label}: status {old_status} != {new_status}")
        return 1

    if not isinstance(old_payload, dict) or not isinstance(new_payload, dict):

        if classify_shape_delta(
            label,
            query,
            old_payload,
            new_payload,
        ):
            return 0

        print(f"MISMATCH {label}: payload shape "
              f"{type(old_payload).__name__} -> {type(new_payload).__name__}")
        print(f"  old: {json.dumps(old_payload, sort_keys=True)[:800]}")
        print(f"  new: {json.dumps(new_payload, sort_keys=True)[:800]}")
        return 1

    if old_payload.get("pagination") != new_payload.get("pagination"):
        print(f"MISMATCH {label}: pagination block changed")
        print(f"  old: {json.dumps(old_payload.get('pagination'), sort_keys=True)}")
        print(f"  new: {json.dumps(new_payload.get('pagination'), sort_keys=True)}")
        return 1

    old_results = old_payload.get("results", [])
    new_results = new_payload.get("results", [])

    old_titles = {item["title"] for item in old_results}
    new_titles = {item["title"] for item in new_results}

    if old_titles != new_titles:
        print(f"MISMATCH {label}: document set changed")
        print(f"  only in old: {sorted(old_titles - new_titles)}")
        print(f"  only in new: {sorted(new_titles - old_titles)}")
        return 1

    # Ranking must stay sorted by descending score in the build under test,
    # whatever the scores themselves turn out to be.
    new_scores = [item["score"] for item in new_results]

    if new_scores != sorted(new_scores, reverse=True):
        print(f"MISMATCH {label}: new results are not sorted by descending score")
        return 1

    old_by_title = {item["title"]: item for item in old_results}
    new_by_title = {item["title"]: item for item in new_results}

    differing = {}

    for title in old_by_title:

        fields = {
            key
            for key in old_by_title[title]
            if old_by_title[title][key] != new_by_title[title][key]
        }

        if fields:
            differing[title] = fields

    unexplained = {
        title: fields - FILENAME_DERIVED_FIELDS
        for title, fields in differing.items()
    }

    unexplained = {
        title: fields
        for title, fields in unexplained.items()
        if fields
    }

    if unexplained:
        print(f"MISMATCH {label}")
        for title in sorted(unexplained):
            print(f"  {title}: unexpected changes to {sorted(unexplained[title])}")
            for field in sorted(unexplained[title]):
                print(f"     {field}: "
                      f"old={old_by_title[title][field]!r} "
                      f"new={new_by_title[title][field]!r}")
        return 1

    print(f"FILENAME-ORDER DELTA {label}")
    print("  confined to filename-derived scores over an unchanged document")
    print("  set; the expected effect of deterministic filename word order.")

    for title in sorted(differing):
        parts = ", ".join(
            f"{field} {old_by_title[title][field]} -> {new_by_title[title][field]}"
            for field in sorted(differing[title])
        )
        print(f"    {title}: {parts}")

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
        failures += compare_search(
            f"/api/search?q={query!r}",
            fetch(OLD, "GET", "/api/search", params={"q": query}),
            fetch(NEW, "GET", "/api/search", params={"q": query}),
            query=query,
        )

    for query in BRANCH_QUERIES:
        checks += 1
        failures += compare_search(
            f"/api/search?q={query!r} [branch]",
            fetch(OLD, "GET", "/api/search", params={"q": query}),
            fetch(NEW, "GET", "/api/search", params={"q": query}),
            query=query,
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
