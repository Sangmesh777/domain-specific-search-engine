"""
Golden vector replay.

This is the parity gate. It runs the full vector replay in a fresh
interpreter, because the engine resolves its data directory at import
time and a subprocess is the only way to guarantee a clean index.

The same vectors are consumed by the Android harness, so a failure
here means the Python side has drifted away from the published
contract.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

VECTORS_PATH = REPO_ROOT / "tests" / "golden" / "search_engine_vectors.json"


@pytest.mark.slow
def test_python_engine_matches_golden_vectors():
    """Replay every vector and require exact binary64 agreement."""

    if not VECTORS_PATH.exists():
        pytest.fail(
            f"{VECTORS_PATH.relative_to(REPO_ROOT)} is missing; "
            "regenerate it with 'python -m tools.generate_golden_vectors'"
        )

    completed = subprocess.run(
        [sys.executable, "-m", "tools.verify_golden_vectors", "--json"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )

    # The tool prints engine log lines before its JSON summary, so
    # extract the trailing JSON object rather than parsing stdout whole.
    start = completed.stdout.find("{")

    if start == -1:
        pytest.fail(
            "verify_golden_vectors produced no JSON summary.\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )

    try:
        summary = json.loads(completed.stdout[start:])
    except json.JSONDecodeError as error:
        pytest.fail(
            f"could not parse the verifier summary: {error}\n"
            f"stdout:\n{completed.stdout}"
        )

    if summary["failed"]:
        report = "\n".join(
            f"{failure['id']} (q={failure['query']!r}): "
            + "; ".join(failure["diffs"][:5])
            for failure in summary["failures"][:20]
        )

        pytest.fail(
            f"{summary['failed']} of {summary['total']} golden vectors "
            f"failed:\n{report}"
        )

    assert summary["passed"] == summary["total"]
    assert summary["total"] > 0
    assert completed.returncode == 0


def test_golden_vector_summary_is_reported():
    """Report the parity numbers in the test output for the record."""

    if not VECTORS_PATH.exists():
        pytest.skip("golden vectors not generated")

    with open(VECTORS_PATH, "r", encoding="utf-8") as handle:
        document = json.load(handle)

    total = len(document["vectors"]) + len(document["empty_corpus_vectors"])

    print(
        f"\nGolden vectors: {total} "
        f"({len(document['vectors'])} corpus, "
        f"{len(document['empty_corpus_vectors'])} empty-corpus) "
        f"over {document['corpus']['document_count']} documents, "
        f"{document['engine_index_state']['content_terms']} content terms"
    )

    assert total > 0
