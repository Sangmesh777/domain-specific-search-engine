"""
Verify the Python engine against the golden search vectors.

This is the parity gate. It replays every recorded vector against a
fresh instance of the real Flask application and compares the complete
response, field by field.

Comparison is exact:

  * the response *shape* must match (bare list vs paginated object)
  * the key set of every object must match exactly
  * every value must be identical
  * floating point values must be bit-identical binary64

Exact comparison is deliberate. The vectors exist to make subtle
ranking drift fail loudly, so approximate matching would defeat the
purpose. Use ``--tolerance`` only to diagnose a mismatch, never to
accept one.

Usage
-----
    python -m tools.verify_golden_vectors
    python -m tools.verify_golden_vectors --tolerance 1e-9
"""

import argparse
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

VECTORS_PATH = REPO_ROOT / "tests" / "golden" / "search_engine_vectors.json"

FLOAT_TOLERANCE = 0.0

MAX_DIFFS_REPORTED = 40


class ComparisonFailure(Exception):
    """Raised internally when two values differ."""


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _describe(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return repr(value)


def compare_values(actual, expected, path, tolerance, diffs, exact_ints=True):
    """
    Recursively compare two decoded JSON values.

    Differences are appended to `diffs` as human readable strings.
    """

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            diffs.append(
                f"{path}: shape mismatch, expected object but got "
                f"{type(actual).__name__}"
            )
            return

        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))

        for key in missing:
            diffs.append(f"{path}.{key}: missing (expected {_describe(expected[key])})")

        for key in extra:
            diffs.append(f"{path}.{key}: unexpected field with value {_describe(actual[key])}")

        for key in sorted(set(expected) & set(actual)):
            compare_values(
                actual[key],
                expected[key],
                f"{path}.{key}",
                tolerance,
                diffs,
            )

        return

    if isinstance(expected, list):
        if not isinstance(actual, list):
            diffs.append(
                f"{path}: shape mismatch, expected list but got "
                f"{type(actual).__name__}"
            )
            return

        if len(actual) != len(expected):
            diffs.append(
                f"{path}: length mismatch, expected {len(expected)} "
                f"but got {len(actual)}"
            )

        for index in range(min(len(actual), len(expected))):
            compare_values(
                actual[index],
                expected[index],
                f"{path}[{index}]",
                tolerance,
                diffs,
            )

        return

    if expected is None or actual is None:
        if actual is not expected:
            diffs.append(
                f"{path}: expected {_describe(expected)} "
                f"but got {_describe(actual)}"
            )
        return

    if isinstance(expected, bool) or isinstance(actual, bool):
        if actual != expected:
            diffs.append(
                f"{path}: expected {_describe(expected)} "
                f"but got {_describe(actual)}"
            )
        return

    if _is_number(expected) and _is_number(actual):
        if tolerance > 0.0:
            if abs(float(actual) - float(expected)) > tolerance:
                diffs.append(
                    f"{path}: expected {expected!r} but got {actual!r} "
                    f"(delta {float(actual) - float(expected):+.3e})"
                )
            return

        # Exact comparison. Integers and floats must agree exactly and
        # must agree in type: 1 and 1.0 are different JSON values even
        # though Python considers them equal.
        if actual != expected or isinstance(actual, float) != isinstance(expected, float):
            if actual != expected:
                diffs.append(
                    f"{path}: expected {expected!r} but got {actual!r}"
                )
            else:
                diffs.append(
                    f"{path}: type mismatch, expected "
                    f"{type(expected).__name__} {expected!r} "
                    f"but got {type(actual).__name__} {actual!r}"
                )
        return

    if actual != expected:
        diffs.append(
            f"{path}: expected {_describe(expected)} but got {_describe(actual)}"
        )




def run_verification(tolerance=0.0, verbose=False):
    """Replay every vector and return a result summary dict."""

    from tools.corpus_materialize import materialize_corpus
    from tools.engine_session import (
        isolated_engine,
        upload_corpus,
        wait_for_index_ready,
    )
    from tools.generate_golden_vectors import (
        all_vectors,
        load_vectors,
        normalize_response,
    )

    document = load_vectors()

    with isolated_engine() as (client, _engine, workspace):
        corpus_dir = os.path.join(workspace, "corpus")

        materialize_corpus(corpus_dir)

        populated = [v for state, v in all_vectors(document) if state == "populated"]
        empty = [v for state, v in all_vectors(document) if state == "empty"]

        results = []

        # --- empty-index vectors -----------------------------------
        wait_for_index_ready(client, 0)

        for vector in empty:
            results.append(
                _check_vector(
                    client, vector, "empty", tolerance, verbose, normalize_response
                )
            )

        # --- populate ---------------------------------------------
        upload_corpus(client, corpus_dir)

        expected_documents = document["corpus"]["document_count"]
        wait_for_index_ready(client, expected_documents)

        for vector in populated:
            results.append(
                _check_vector(
                    client, vector, "populated", tolerance, verbose, normalize_response
                )
            )

        failures = [item for item in results if not item["passed"]]

        return {
            "total": len(results),
            "passed": len(results) - len(failures),
            "failed": len(failures),
            "failures": failures,
            "tolerance": tolerance,
            "corpus_documents": expected_documents,
        }


def _check_vector(client, vector, corpus_state, tolerance, verbose, normalize):
    params = {key: value for key, value in vector["params"].items() if value is not None}
    params["q"] = vector["query"]

    response = client.get("/api/search", query_string=params)
    actual = normalize(response.get_json())

    diffs = []

    if response.status_code != vector["status_code"]:
        diffs.append(
            f"status: expected {vector['status_code']} "
            f"but got {response.status_code}"
        )

    compare_values(
        actual,
        vector["response"],
        "response",
        tolerance,
        diffs,
    )

    result = {
        "id": vector["id"],
        "query": vector["query"],
        "params": vector["params"],
        "corpus_state": corpus_state,
        "passed": not diffs,
        "diff_count": len(diffs),
        "diffs": diffs[:MAX_DIFFS_REPORTED],
        "suppressed_diffs": max(0, len(diffs) - MAX_DIFFS_REPORTED),
    }

    if verbose or diffs:
        mark = "PASS" if not diffs else "FAIL"
        print(f"  [{mark}] {vector['id']}  q={vector['query']!r}")

        for diff in result["diffs"]:
            print(f"         {diff}")

        if result["suppressed_diffs"]:
            print(f"         ... and {result['suppressed_diffs']} more")

    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--tolerance",
        type=float,
        default=FLOAT_TOLERANCE,
        help="Float tolerance for diagnosis only (default: exact).",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every vector, not just failures.",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the summary as JSON.",
    )

    arguments = parser.parse_args(argv)

    print("Verifying Python engine against golden vectors")
    print()

    summary = run_verification(
        tolerance=arguments.tolerance,
        verbose=arguments.verbose,
    )

    if arguments.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print()
        print("=" * 62)
        print("GOLDEN VECTOR PARITY")
        print("=" * 62)
        print(f"corpus documents : {summary['corpus_documents']}")
        print(f"vectors run      : {summary['total']}")
        print(f"passed           : {summary['passed']}")
        print(f"failed           : {summary['failed']}")
        print(f"tolerance        : {'exact binary64' if not summary['tolerance'] else summary['tolerance']}")

        if summary["failed"]:
            print()
            print("FAILURES")
            print("-" * 62)

            for failure in summary["failures"]:
                print(
                    f"{failure['id']} "
                    f"(q={failure['query']!r}, {failure['corpus_state']} corpus):"
                )

                for diff in failure["diffs"]:
                    print(f"    {diff}")

        print("=" * 62)

    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
