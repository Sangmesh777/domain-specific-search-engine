"""
Prove the copy-on-write planners reproduce the original in-place helpers.

The two functions that used to mutate the live index containers in place

    _remove_document_from_memory(filename, old_term_counts)
    _add_document_to_memory(filename, metadata, filename_words, pages, content_words)

have been replaced by the pure planners in `search_engine/index_state.py`:

    plan_remove_document(current, filename, old_term_counts)
    plan_add_document(current, filename, metadata, filename_words, pages, content_words)
    plan_upsert_document(...)

The goal of that change was to alter only *when* new state becomes
visible, never *what* the state is. This module is the evidence.

For each case it:

  1. runs the original helper from a pinned git revision against a
     synthetic global index, then reads the four globals back
  2. runs the planner against the same starting state
  3. requires the four resulting containers to be identical
  4. requires the planner to have left its input untouched

Step 4 matters as much as step 3: a planner that produced the right
output by mutating its input would appear correct here and then corrupt
a concurrent reader.

Usage:
    python -m tools.mutation_equivalence
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

from search_engine.index_state import (
    plan_add_document,
    plan_remove_document,
    plan_upsert_document,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# A revision from before the copy-on-write change, which still defines
# the in-place helpers.
DEFAULT_BASE = "5bc945f"

ORIGINAL_NAMES = (
    "_add_document_to_memory",
    "_remove_document_from_memory",
)

CONTAINER_FIELDS = (
    "inverted_index",
    "document_metadata",
    "filename_index",
    "page_text_index",
)


class MutationEquivalenceError(Exception):
    """Raised when the comparison cannot be set up."""


def _git_show(base_revision, path):
    try:
        completed = subprocess.run(
            ["git", "show", f"{base_revision}:{path}"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise MutationEquivalenceError(
            f"could not read {path} from revision {base_revision}: "
            f"{error.stderr.strip()}"
        ) from error

    return completed.stdout


def load_original_helpers(base_revision=DEFAULT_BASE):
    """
    Return a namespace holding the original in-place helpers.

    The namespace also carries the four module globals they mutate, so a
    case can reset them before each run.
    """

    import ast

    source = _git_show(base_revision, "app.py")

    tree = ast.parse(source)

    present = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }

    missing = [name for name in ORIGINAL_NAMES if name not in present]

    if missing:
        raise MutationEquivalenceError(
            f"revision {base_revision} does not define {sorted(missing)}. "
            "Pick a revision from before the copy-on-write change."
        )

    segments = [
        ast.get_source_segment(source, node)
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in ORIGINAL_NAMES
    ]

    namespace = {}

    exec("\n\n\n".join(segments), namespace)

    for name in (
        "REAL_INVERTED_INDEX",
        "DOCUMENT_METADATA",
        "FILENAME_INDEX",
        "PAGE_TEXT_INDEX",
    ):
        namespace[name] = {}

    return namespace


def blank_start(start=None):
    """Return a complete snapshot mapping for a case's starting state."""

    start = start or {}

    return {
        "inverted_index": copy.deepcopy(start.get("inverted_index", {})),
        "document_metadata": copy.deepcopy(start.get("document_metadata", {})),
        "filename_index": copy.deepcopy(start.get("filename_index", {})),
        "page_text_index": copy.deepcopy(start.get("page_text_index", {})),
    }


def cases():
    """Return the comparison cases."""

    return [
        {
            "label": "add to an empty index",
            "operation": "add",
            "start": {},
            "filename": "doc1.txt",
            "metadata": {"title": "doc1.txt", "total_words": 3, "page_count": 1},
            "content_words": ["alpha", "beta", "alpha"],
            "filename_words": ["doc1"],
            "pages": [{"page": 1, "text": "alpha beta alpha"}],
            "old_term_counts": {},
        },
        {
            "label": "add alongside an existing document",
            "operation": "add",
            "start": {
                "inverted_index": {"alpha": {"other.txt": 1}},
                "document_metadata": {"other.txt": {"title": "other.txt"}},
                "filename_index": {"other.txt": ["other"]},
                "page_text_index": {"other.txt": [{"page": 1, "text": "other"}]},
            },
            "filename": "doc2.txt",
            "metadata": {"title": "doc2.txt", "total_words": 2, "page_count": 1},
            "content_words": ["alpha", "epsilon"],
            "filename_words": ["doc2"],
            "pages": [{"page": 1, "text": "alpha epsilon"}],
            "old_term_counts": {},
        },
        {
            "label": "re-upload drops the previous revision's terms",
            "operation": "upsert",
            "start": {
                "inverted_index": {
                    "alpha": {"doc1.txt": 2},
                    "beta": {"doc1.txt": 1},
                    "stale": {"doc1.txt": 4},
                },
                "document_metadata": {"doc1.txt": {"title": "doc1.txt"}},
                "filename_index": {"doc1.txt": ["doc1"]},
                "page_text_index": {"doc1.txt": [{"page": 1, "text": "old"}]},
            },
            "filename": "doc1.txt",
            "metadata": {"title": "doc1.txt", "total_words": 3, "page_count": 1},
            "content_words": ["gamma", "gamma", "delta"],
            "filename_words": ["doc1", "revised"],
            "pages": [{"page": 1, "text": "gamma delta"}],
            "old_term_counts": {"alpha": 2, "beta": 1, "stale": 4},
        },
        {
            "label": "re-upload keeps a term shared with another document",
            "operation": "upsert",
            "start": {
                "inverted_index": {
                    "shared": {"doc1.txt": 1, "other.txt": 5},
                },
                "document_metadata": {
                    "doc1.txt": {"title": "doc1.txt"},
                    "other.txt": {"title": "other.txt"},
                },
                "filename_index": {
                    "doc1.txt": ["doc1"],
                    "other.txt": ["other"],
                },
                "page_text_index": {
                    "doc1.txt": [{"page": 1, "text": "one"}],
                    "other.txt": [{"page": 1, "text": "other"}],
                },
            },
            "filename": "doc1.txt",
            "metadata": {"title": "doc1.txt", "total_words": 2, "page_count": 1},
            "content_words": ["shared", "fresh"],
            "filename_words": ["doc1"],
            "pages": [{"page": 1, "text": "shared fresh"}],
            "old_term_counts": {"shared": 1},
        },
        {
            "label": "upsert with no previous revision in SQLite",
            "operation": "upsert",
            "start": {
                "inverted_index": {},
                "document_metadata": {},
                "filename_index": {},
                "page_text_index": {},
            },
            "filename": "doc3.txt",
            "metadata": {"title": "doc3.txt", "total_words": 2, "page_count": 1},
            "content_words": ["alpha", "zeta"],
            "filename_words": ["doc3"],
            "pages": [{"page": 1, "text": "alpha zeta"}],
            "old_term_counts": {},
        },
        {
            "label": "remove a document sharing a term",
            "operation": "remove",
            "start": {
                "inverted_index": {
                    "shared": {"doc1.txt": 1, "doc2.txt": 2},
                    "solo": {"doc1.txt": 3},
                },
                "document_metadata": {
                    "doc1.txt": {"title": "doc1.txt"},
                    "doc2.txt": {"title": "doc2.txt"},
                },
                "filename_index": {
                    "doc1.txt": ["doc1"],
                    "doc2.txt": ["doc2"],
                },
                "page_text_index": {
                    "doc1.txt": [{"page": 1, "text": "one"}],
                    "doc2.txt": [{"page": 1, "text": "two"}],
                },
            },
            "filename": "doc1.txt",
            "old_term_counts": {"shared": 1, "solo": 3},
        },
        {
            "label": "remove the last document",
            "operation": "remove",
            "start": {
                "inverted_index": {"only": {"doc1.txt": 1}},
                "document_metadata": {"doc1.txt": {"title": "doc1.txt"}},
                "filename_index": {"doc1.txt": ["doc1"]},
                "page_text_index": {"doc1.txt": [{"page": 1, "text": "one"}]},
            },
            "filename": "doc1.txt",
            "old_term_counts": {"only": 1},
        },
        {
            "label": "remove an absent document",
            "operation": "remove",
            "start": {
                "inverted_index": {"alpha": {"doc1.txt": 1}},
                "document_metadata": {"doc1.txt": {"title": "doc1.txt"}},
                "filename_index": {"doc1.txt": ["doc1"]},
                "page_text_index": {"doc1.txt": [{"page": 1, "text": "one"}]},
            },
            "filename": "absent.txt",
            "old_term_counts": {},
        },
        {
            "label": "remove with stale term counts for absent terms",
            "operation": "remove",
            "start": {
                "inverted_index": {"alpha": {"doc1.txt": 1}},
                "document_metadata": {"doc1.txt": {"title": "doc1.txt"}},
                "filename_index": {"doc1.txt": ["doc1"]},
                "page_text_index": {"doc1.txt": [{"page": 1, "text": "one"}]},
            },
            "filename": "doc1.txt",
            "old_term_counts": {"alpha": 1, "never_indexed": 9},
        },
    ]


def _run_original(namespace, case, start):
    """Run the original helper against the globals and read them back."""

    for name, field in (
        ("REAL_INVERTED_INDEX", "inverted_index"),
        ("DOCUMENT_METADATA", "document_metadata"),
        ("FILENAME_INDEX", "filename_index"),
        ("PAGE_TEXT_INDEX", "page_text_index"),
    ):
        namespace[name] = copy.deepcopy(start[field])

    if case["operation"] == "remove":
        namespace["_remove_document_from_memory"](
            case["filename"],
            dict(case["old_term_counts"]),
        )
    else:
        # The indexed path always removed the previous revision first.
        namespace["_remove_document_from_memory"](
            case["filename"],
            dict(case["old_term_counts"]),
        )
        namespace["_add_document_to_memory"](
            case["filename"],
            copy.deepcopy(case["metadata"]),
            list(case["filename_words"]),
            copy.deepcopy(case["pages"]),
            list(case["content_words"]),
        )

    return {
        "inverted_index": namespace["REAL_INVERTED_INDEX"],
        "document_metadata": namespace["DOCUMENT_METADATA"],
        "filename_index": namespace["FILENAME_INDEX"],
        "page_text_index": namespace["PAGE_TEXT_INDEX"],
    }


def _run_planner(case, start):
    """Run the copy-on-write planner against the same starting state."""

    if case["operation"] == "remove":
        return plan_remove_document(
            start,
            case["filename"],
            dict(case["old_term_counts"]),
        )

    if case["operation"] == "upsert":
        return plan_upsert_document(
            start,
            case["filename"],
            copy.deepcopy(case["metadata"]),
            list(case["filename_words"]),
            copy.deepcopy(case["pages"]),
            list(case["content_words"]),
            dict(case["old_term_counts"]),
        )

    return plan_add_document(
        start,
        case["filename"],
        copy.deepcopy(case["metadata"]),
        list(case["filename_words"]),
        copy.deepcopy(case["pages"]),
        list(case["content_words"]),
    )


def run(base_revision=DEFAULT_BASE, limit=10):
    """Compare every case and return a summary."""

    namespace = load_original_helpers(base_revision)

    state_differences = []
    purity_differences = []
    comparisons = 0

    for case in cases():
        start = blank_start(case["start"])
        before = copy.deepcopy(start)

        expected = _run_original(namespace, case, start)
        actual = _run_planner(case, start)

        comparisons += 1

        differing = [
            field
            for field in CONTAINER_FIELDS
            if expected[field] != actual[field]
        ]

        if differing and len(state_differences) < limit:
            state_differences.append({
                "label": case["label"],
                "fields": differing,
                "expected": {field: expected[field] for field in differing},
                "actual": {field: actual[field] for field in differing},
            })

        # The planner must not have touched its input. A planner that
        # got the right answer by mutating `current` would corrupt a
        # concurrent reader that is still using the old snapshot.
        comparisons += 1

        if start != before:
            if len(purity_differences) < limit:
                purity_differences.append({
                    "label": case["label"],
                    "before": before,
                    "after": start,
                })

    return {
        "base_revision": base_revision,
        "cases": len(cases()),
        "comparisons": comparisons,
        "state_differences": state_differences,
        "purity_differences": purity_differences,
    }


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--json", action="store_true")

    arguments = parser.parse_args(argv)

    print("Memory mutation equivalence: planners vs original in-place helpers")
    print(f"  base revision : {arguments.base}")
    print()

    try:
        summary = run(base_revision=arguments.base)
    except MutationEquivalenceError as error:
        print(f"could not run: {error}", file=sys.stderr)
        return 2

    if arguments.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
        return 1 if (summary["state_differences"] or summary["purity_differences"]) else 0

    print(f"  cases         : {summary['cases']}")
    print(f"  comparisons   : {summary['comparisons']}")
    print(f"  state diffs   : {len(summary['state_differences'])}")
    print(f"  purity diffs  : {len(summary['purity_differences'])}")
    print()

    for difference in summary["state_differences"]:
        print(f"STATE MISMATCH: {difference['label']}")
        for field in difference["fields"]:
            print(f"    {field}:")
            print(f"        original : {difference['expected'][field]!r}")
            print(f"        planner  : {difference['actual'][field]!r}")

    for difference in summary["purity_differences"]:
        print(f"INPUT WAS MUTATED: {difference['label']}")
        print(f"    before : {difference['before']!r}")
        print(f"    after  : {difference['after']!r}")

    if summary["state_differences"] or summary["purity_differences"]:
        print()
        print("MUTATION EQUIVALENCE FAILED")
        return 1

    print("MUTATION EQUIVALENCE OK")
    print("Every planner reproduces the original helper's resulting state,")
    print("and no planner mutates the snapshot it is given.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
