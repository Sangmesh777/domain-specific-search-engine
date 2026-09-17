"""
Report how much of the recorded contract the port model reproduces.

When the Kotlin sources cannot be compiled - which is the case in this
environment, where no javac, kotlinc, gradle or Android SDK exists -
`tools/port_model.py` is the only thing standing in for them. It is
written with Java's predicate semantics so that a passing model is
evidence about the Kotlin, not merely about Python calling Python.

That substitution is only as good as its coverage, and coverage is easy
to overstate. This prints it section by section: what the model
reproduces, what it does not, and the ranking pipeline.

Usage: python3 -m tools.port_gap          exit 0 when every modelled
                                          section is at 100%
"""

import json
import pathlib
import sys

from tools import port_model

VECTORS = (
    pathlib.Path(__file__).resolve().parent.parent
    / "tests"
    / "golden"
    / "search_engine_vectors.json"
)

# Sections the model reproduces, with the callable that answers each.
MODELLED = {
    "sanitize_filename": lambda case: port_model.sanitize(
        case["input"]
    ) == case["expected"],
    "content_tokenize": lambda case: port_model.tokenize(
        case["input"]
    ) == case["expected"],
    "filename_tokenize": lambda case: port_model.tokenize_filename(
        case["input"]
    ) == case["expected"],
    "normalize_search_query": lambda case: port_model.normalize_search_query(
        case["input"]
    ) == case["expected"],
    "parse_filetype_filter": lambda case: port_model.parse_filetype_filter(
        case["input"]
    ) == (case["expected_query"], case["expected_filetype"]),
    "round_half_even": lambda case: port_model.round_half_even(
        case["value"], case["digits"]
    ) == case["expected"],
}

# Sections with no model on the Python side. Each is a real gap for the
# port, and none of them is covered by the differential tests.
UNMODELLED = ()


def character_class_score(contract):
    """Score the two `character_classes` lists separately."""

    alnum = contract["alnum"]
    whitespace = contract["whitespace"]

    alnum_ok = sum(
        1
        for case in alnum
        if port_model.is_python_alnum(int(case["codepoint"][2:], 16))
        == case["isalnum"]
    )

    space_ok = sum(
        1
        for case in whitespace
        if port_model.is_python_space(int(case["codepoint"][2:], 16))
        == case["isspace"]
    )

    return alnum_ok + space_ok, len(alnum) + len(whitespace)


def ranking_score(document):
    """Score the ranking vectors using the port model against sidecar indexes."""
    from tools.generate_golden_vectors import normalize_response
    from tools.verify_golden_vectors import compare_values

    inv, meta, fn_idx, pg_idx = port_model.load_sidecar_indexes()

    passed = 0
    total = 0

    for vector in document["vectors"]:
        total += 1
        actual = normalize_response(
            port_model.search_index(
                vector["query"],
                vector["params"]["page"],
                vector["params"]["limit"],
                inv,
                meta,
                fn_idx,
                pg_idx,
            )
        )
        diffs = []
        compare_values(actual, vector["response"], "root", 0.0, diffs)
        if not diffs:
            passed += 1

    for vector in document["empty_corpus_vectors"]:
        total += 1
        actual = normalize_response(
            port_model.search_index(
                vector["query"],
                vector["params"]["page"],
                vector["params"]["limit"],
                {},
                {},
                {},
                {},
            )
        )
        diffs = []
        compare_values(actual, vector["response"], "root", 0.0, diffs)
        if not diffs:
            passed += 1

    return passed, total


def main():

    document = json.loads(VECTORS.read_text(encoding="utf-8"))
    contract = document["contract_vectors"]

    print("PORT MODEL COVERAGE OF THE RECORDED CONTRACT")
    print("=" * 58)

    incomplete = []

    modelled_total = 0

    for section, predicate in MODELLED.items():

        cases = contract[section]

        passed = sum(1 for case in cases if predicate(case))

        modelled_total += len(cases)

        mark = "OK" if passed == len(cases) else "GAP"

        if mark == "GAP":
            incomplete.append(section)

        print(f"  {section:24} {passed:3d}/{len(cases):3d}  {mark}")

    passed, total = character_class_score(
        contract["character_classes"]
    )

    modelled_total += total

    mark = "OK" if passed == total else "GAP"

    if mark == "GAP":
        incomplete.append("character_classes")

    print(f"  {'character_classes':24} {passed:3d}/{total:3d}  {mark}")

    print()
    print("NOT MODELLED IN PYTHON")
    print("=" * 58)

    unmodelled_total = 0

    if not UNMODELLED:
        print("  (none - all contract sections are modelled)")

    for section in UNMODELLED:

        count = len(contract[section])

        unmodelled_total += count

        note = ""

        if section == "round_half_even":
            note = "  (Kotlin exists, no Python model)"

        print(f"  {section:24} {count:3d} vectors  GAP{note}")

    print()
    print("RANKING PIPELINE COVERAGE")
    print("=" * 58)

    ranking_passed, ranking_total = ranking_score(document)
    ranking_mark = "OK" if ranking_passed == ranking_total else "GAP"

    if ranking_mark == "GAP":
        incomplete.append("ranking_pipeline")

    print(
        f"  {'corpus + empty-corpus':24} {ranking_passed:3d}/{ranking_total:3d}  {ranking_mark}"
    )

    print()
    print(
        f"  modelled   : {modelled_total} contract vectors + {ranking_passed} ranking vectors"
    )
    print(
        f"  unmodelled : {unmodelled_total} contract vectors + {ranking_total - ranking_passed} ranking vectors"
    )

    if incomplete:
        print()
        print(
            "MODELLED SECTIONS BELOW 100%: " + ", ".join(incomplete)
        )
        return 1

    print()
    print(
        "Every section with a model reproduces the contract exactly. "
        "The gaps above are what a compiler would still have to settle."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
