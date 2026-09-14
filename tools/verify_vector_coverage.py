"""
Measure how much of the ranking the golden vectors actually pin.

tests/golden/search_engine_vectors.json is called the portability contract, so
the question that matters is what a reimplementation could get wrong and still
reproduce it exactly. That is measurable rather than a matter of opinion:

    alter one numeric constant in the ranking pipeline
    regenerate the vectors
    compare against the committed file

A constant whose perturbation changes at least one recorded payload is PINNED:
a port that gets it wrong fails the suite. One whose perturbation changes
nothing is NOT PINNED, and needs either a case added to the vectors, an engine
test, or an explanation of why it cannot affect any output at all.

    .venv/bin/python tools/verify_vector_coverage.py
    .venv/bin/python tools/verify_vector_coverage.py --filter weight

Every constant the vectors do NOT pin carries a classification in the
CLASSIFICATIONS map below, in the five categories the port contract defines:

    A  not a ranking value at all (defaults, initializers, guards)
    B  structurally inert: provably cannot change any output, with the proof
    C  live ranking behavior, pinned by a named falsified engine test
    D  live and uncovered - this category MUST stay empty
    E  intentionally excluded from pinning (none today)

The map is guarded against rot in both directions: each entry records the
expected constant value and a source anchor, so a line that moves or changes
reports as STALE, and a classified constant that the vectors start pinning
reports as DRIFT. tests/test_vector_coverage_tool.py runs the full sweep and
fails on any stale, drifted, unclassified, or category-D constant - so the
tool itself reports, and the suite gates. ANDROID.md section 9 publishes the
same accounting in prose.

Two measurement traps this tool exists to avoid, both of which produce wrong
answers silently:

  Perturbation size. Nudging a clamp bound by 0.01 changes nothing unless a
  value happens to fall inside that 0.01 band, so small steps report a clamp as
  pinned when it is not. Every perturbation here moves a constant far enough to
  cross any band it guards.

  Stale bytecode. A perturbation such as 0.75 -> 0.80 leaves the file the same
  size, and a sweep of dozens of constants finishes inside a few seconds, so
  successive runs can share a pyc mtime second and Python reuses the cached
  module. The comparison then reports "no change" for a constant that really
  did change. Every regeneration here runs with -B and PYTHONDONTWRITEBYTECODE
  after purging __pycache__.

Constants are enumerated from the AST rather than listed by hand, so a new
constant cannot be missed by forgetting to add it to a table.
"""

import argparse
import ast
import os
import shutil
import subprocess
import sys


BASE_DIR = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

TARGET = os.path.join(
    BASE_DIR,
    "search_engine",
    "search.py",
)

VECTOR_FILE = os.path.join(
    BASE_DIR,
    "tests",
    "golden",
    "search_engine_vectors.json",
)

# Regenerate the vectors in a subprocess and report only whether they moved.
# A subprocess because the perturbed module has to be imported fresh, and the
# parent already holds an unperturbed copy.
CHECK = r"""
import sys, os
sys.path.insert(0, os.path.join(os.getcwd(), "tools"))
sys.path.insert(0, os.getcwd())
from export_golden_vectors import build_vectors, render, OUTPUT
with open(OUTPUT, encoding="utf-8") as file:
    committed = file.read()
print("DIFFERS" if render(build_vectors()) != committed else "IDENTICAL")
"""


def purge_bytecode():
    """
    Drop every __pycache__ under the repository.

    Without this a same-size edit inside the same wall-clock second is served
    from a stale .pyc and the sweep reports a false negative.
    """

    for root, dirs, _files in os.walk(BASE_DIR):

        if ".venv" in root or ".git" in root:
            dirs[:] = []
            continue

        if "__pycache__" in dirs:
            shutil.rmtree(
                os.path.join(root, "__pycache__"),
                ignore_errors=True,
            )


def perturbed(value):
    """
    A value far enough away to cross any band the original guards.

    Clamp bounds are the reason: moving 0.25 to 0.26 only matters if some
    similarity lands between them, so the step is deliberately coarse.
    """

    if isinstance(value, float):
        return value + max(0.05, abs(value) * 0.1)

    return value + 1


def literal_text(value, replacement):
    """
    Render a replacement so it stays valid Python source.

    Floats are written with repr, which round-trips. An integer that becomes a
    float keeps a decimal point, so `+ 1` turning into `+ 1.5` still parses as
    a float rather than silently truncating.
    """

    if isinstance(replacement, float):
        return repr(replacement)

    return str(replacement)


def constants():
    """
    Every numeric literal in the ranking module, with its enclosing context.

    Enumerated from the AST, so nothing depends on a hand-maintained list.
    Booleans are excluded because they are not ranking magnitudes.
    """

    with open(TARGET, encoding="utf-8") as file:
        source = file.read()

    lines = source.split("\n")

    tree = ast.parse(source)

    # Map each literal to the name it is assigned to, when it is assigned to
    # one, because "0.80" alone says nothing in a report and
    # "filename_weight = 0.80" says which branch it belongs to.
    assigned = {}

    for node in ast.walk(tree):

        if isinstance(node, ast.Assign):

            targets = []

            for target in node.targets:
                if isinstance(target, ast.Name):
                    targets.append(target.id)

            if not targets:
                continue

            for child in ast.walk(node.value):
                if isinstance(child, ast.Constant):
                    assigned[child.lineno, child.col_offset] = (
                        " = ".join(targets)
                    )

    # Subscript indices are positional access, not tunable magnitudes.
    # `os.path.splitext(filename)[1]` perturbed to `[2]` raises IndexError
    # rather than telling anyone anything about ranking, so they are excluded
    # by position rather than reported as failures to measure.
    subscript_indices = set()

    for node in ast.walk(tree):

        if not isinstance(node, ast.Subscript):
            continue

        for child in ast.walk(node.slice):
            if isinstance(child, ast.Constant):
                subscript_indices.add(
                    (child.lineno, child.col_offset)
                )

    found = []

    for node in ast.walk(tree):

        if not isinstance(node, ast.Constant):
            continue

        if isinstance(node.value, bool):
            continue

        if not isinstance(node.value, (int, float)):
            continue

        if (node.lineno, node.col_offset) in subscript_indices:
            continue

        found.append({
            "lineno": node.lineno,
            "col": node.col_offset,
            "end_col": node.end_col_offset,
            "value": node.value,
            "name": assigned.get(
                (node.lineno, node.col_offset),
                "",
            ),
            "line": lines[node.lineno - 1].strip(),
        })

    found.sort(key=lambda item: (item["lineno"], item["col"]))

    return source, found


# ------------------------------------------------------------------
# CLASSIFICATION OF EVERY CONSTANT THE VECTORS DO NOT PIN
# ------------------------------------------------------------------
#
# Keyed by line number in search_engine/search.py. Each entry records the
# expected constant value and an anchor substring of the source line, so a
# map that outlives the code it describes reports as stale instead of
# silently classifying whatever now happens to sit on that line. Category C
# entries name the falsified engine tests that pin them; a test that is
# deleted or renamed fails the suite through those names.

CATEGORY_LABELS = {
    "A": "not a ranking value",
    "B": "structurally inert (cannot change any output; proof recorded)",
    "C": "live ranking behavior, pinned by falsified engine tests",
    "D": "LIVE AND UNCOVERED - must stay empty",
    "E": "intentionally excluded from pinning",
}

CLASSIFICATIONS = {

    113: {
        "value": 1,
        "anchor": "requested_page",
        "category": "A",
        "reason": (
            "requested_page argument default; pagination plumbing, and "
            "every vector case passes page explicitly"
        ),
    },

    114: {
        "value": 10,
        "anchor": "requested_limit",
        "category": "A",
        "reason": (
            "requested_limit argument default; pagination plumbing, and "
            "every vector case passes limit explicitly"
        ),
    },

    168: {
        "value": 2,
        "anchor": "len(query) >= 2",
        "category": "C",
        "tests": (
            "test_single_word_quoted_query_still_takes_the_quoted_path",
        ),
        "reason": (
            "minimum quoted-query length. The test kills thresholds >= 4 "
            "('\"z\"' would lose its bare-list shape). Lengths 1-3 are "
            "provably identical: the only queries that differ are '\"' and "
            "'\"\"', whose empty quoted phrase is falsy at every "
            "`if quoted_phrase:` guard, so no test can pin the exact 2"
        ),
    },

    199: {
        "value": 1,
        "anchor": "len(word) > 1",
        "category": "B",
        "reason": (
            "redundant content-word gate: the tokenizer indexes no "
            "one-character terms, the prefix gate needs >= 3 (line 535) "
            "and the numeric gate >= 2 (line 572), so a one-character "
            "word that survived here could still match nothing. Verified "
            "empirically: perturbing to > 0 survives the full core suite"
        ),
    },

    348: {
        "value": 0,
        "anchor": "total_documents == 0",
        "category": "A",
        "reason": "early-return guard; a comparison, not a magnitude",
    },

    535: {
        "value": 3,
        "anchor": "len(word) >= 3",
        "category": "C",
        "tests": (
            "test_prefix_match_requires_a_three_character_word",
        ),
        "reason": (
            "prefix-match minimum word length. The test kills both "
            "directions: 3->4 drops the 'dat' row, 3->2 adds a 'da' row"
        ),
    },

    557: {
        "value": 0.25,
        "anchor": "0.25",
        "category": "C",
        "tests": (
            "test_prefix_similarity_floor_is_pinned",
        ),
        "reason": (
            "prefix-similarity floor; the corpus would need a term more "
            "than 4x the query word, the test supplies "
            "'inte'/'internationalization'"
        ),
    },

    572: {
        "value": 2,
        "anchor": "len(word) >= 2",
        "category": "C",
        "tests": (
            "test_numeric_similarity_floor_is_pinned",
        ),
        "reason": (
            "numeric-substring minimum digits. The floor test's '12' "
            "query kills >= 3. The >= 1 direction is inert: "
            "one-character words never arrive, the line-199 gate drops "
            "them first"
        ),
    },

    594: {
        "value": 0.65,
        "anchor": "0.65",
        "category": "C",
        "tests": (
            "test_numeric_similarity_ceiling_is_pinned",
        ),
        "reason": (
            "numeric-substring ceiling; the test supplies a 14/15 ratio "
            "(0.6533 -> 0.65), the corpus a 0.2 ratio that never "
            "approaches it"
        ),
    },

    597: {
        "value": 0.7,
        "anchor": "0.70",
        "category": "C",
        "tests": (
            "test_numeric_similarity_multiplier_is_pinned",
        ),
        "reason": (
            "numeric-substring multiplier; the test supplies a 5/11 ratio "
            "whose product 0.3182 sits strictly between floor and "
            "ceiling. The corpus's only numeric case reports the floor "
            "whatever the multiplier is"
        ),
    },

    619: {
        "value": 0.0,
        "anchor": "0.0",
        "name": "current_weight",
        "category": "A",
        "reason": "accumulator initializer",
    },

    662: {
        "value": 0,
        "anchor": "total_words == 0",
        "category": "A",
        "reason": "division guard; a comparison, not a magnitude",
    },

    790: {
        "value": 1,
        "anchor": "phrase_occurrences <= 1",
        "category": "C",
        "tests": (
            "test_phrase_frequency_branch_starts_at_two_occurrences",
        ),
        "reason": (
            "single/multiple-occurrence phrase boundary. The test kills "
            "<= 2 (the two-occurrence document collapses to 50.0). <= 0 "
            "is provably identical: the branch runs only when occurrences "
            "> 0, and at 1 occurrence the curve itself yields "
            "50 + 50*ln(1)/ln(25) = 50.0"
        ),
    },

    804: {
        "value": 1.0,
        "anchor": "1.0",
        "category": "C",
        "tests": (
            "test_phrase_frequency_ratio_caps_at_the_reference",
        ),
        "reason": (
            "frequency_ratio cap at the 25-occurrence reference. The test "
            "kills lower caps (0.9 reports 95.0 for 25 and 50 "
            "occurrences). Higher caps are observable only together with "
            "line 819, which catches the overshoot - the documented cap "
            "pair"
        ),
    },

    819: {
        "value": 100.0,
        "anchor": "100.0",
        "category": "C",
        "tests": (
            "test_phrase_frequency_ratio_caps_at_the_reference",
        ),
        "reason": (
            "phrase-score cap. The test kills caps under 100 "
            "(min(100.0, 99.0) clips the exact-100.0 score). Above 100 "
            "is provably inert: the input can never exceed 100.0, "
            "because the single-occurrence branch gives 50.0 and the "
            "curve branch gives 50 + 50*min(ratio, 1.0)"
        ),
    },

    905: {
        "value": 0.0,
        "anchor": "default=0.0",
        "name": "max_content_score",
        "category": "A",
        "reason": "initializer (empty-max default)",
    },

    910: {
        "value": 0,
        "anchor": "filename_score",
        "category": "A",
        "reason": "filename-signal predicate; a comparison, not a magnitude",
    },

    947: {
        "value": 0.0,
        "anchor": "default=0.0",
        "name": "max_content_score",
        "category": "A",
        "reason": "re-initializer inside the filetype branch",
    },

    978: {
        "value": 0.0,
        "anchor": "0.0",
        "name": "best_prefix_similarity",
        "category": "A",
        "reason": "initializer",
    },

    985: {
        "value": 0.0,
        "anchor": "0.0",
        "name": "best_numeric_similarity",
        "category": "A",
        "reason": "initializer",
    },

    1041: {
        "value": 0.0,
        "anchor": "query_word_coverage",
        "category": "A",
        "reason": "initializer",
    },

    1096: {
        "value": 0.75,
        "anchor": "final_score = 0.75",
        "category": "B",
        "reason": (
            "filetype-only keep-alive: those queries publish a hardcoded "
            "relevance of 1.0, and 0.75 only has to be positive to clear "
            "the final_score <= 0 filter"
        ),
    },

    1113: {
        "value": 0.05,
        "anchor": "filename_weight = 0.05",
        "category": "B",
        "reason": (
            "quoted-only-branch filename_weight: the branch runs only "
            "when no document scored on the filename, which forces "
            "filename_relevance to 0.0 for every document. The weight "
            "multiplies zero"
        ),
    },

    1132: {
        "value": 0.05,
        "anchor": "filename_weight = 0.05",
        "category": "B",
        "reason": (
            "ordinary-branch filename_weight: the same structural zero, "
            "from the same branch-condition argument"
        ),
    },

    1171: {
        "value": 0,
        "anchor": "filename_score == 0",
        "category": "A",
        "reason": "skip-filter comparison",
    },

    1173: {
        "value": 0,
        "anchor": "content_score == 0",
        "category": "A",
        "reason": "skip-filter comparison",
    },

    1175: {
        "value": 0,
        "anchor": "phrase_score == 0",
        "category": "A",
        "reason": "skip-filter comparison",
    },

    1421: {
        "value": 4,
        "anchor": "4",
        "category": "B",
        "reason": (
            "filename_score rounding precision: filename_score is always "
            "a multiple of 10 (initialized 0.0, incremented only by "
            "100.0/50.0/20.0), so it is exact at any precision and the "
            "round can never bite. Verified empirically: 4 -> 3 survives "
            "the full core suite"
        ),
    },

    1440: {
        "value": 0.0,
        "anchor": "0.0",
        "category": "A",
        "reason": (
            ".get default for prefix_similarity; ranked rows always set "
            "the field, so it never fires"
        ),
    },

    1449: {
        "value": 0.0,
        "anchor": "0.0",
        "category": "A",
        "reason": (
            ".get default for numeric_similarity; ranked rows always set "
            "the field"
        ),
    },

    1451: {
        "value": 4,
        "anchor": "4",
        "category": "C",
        "tests": (
            "test_numeric_similarity_is_rounded_to_four_places",
            "test_numeric_similarity_multiplier_is_pinned",
        ),
        "reason": (
            "published numeric_similarity precision. The rounding test "
            "supplies a 4/13 ratio (raw 0.2153846..., published 0.2154) "
            "and the multiplier test passes through the same round "
            "(0.318181... -> 0.3182). Precision 3 kills both"
        ),
    },

    1458: {
        "value": 0.0,
        "anchor": "0.0",
        "category": "A",
        "reason": (
            ".get default for lexical_match_relevance; ranked rows always "
            "set the field"
        ),
    },
}


def classify(rows):
    """
    Cross-reference sweep rows against the classification map.

    Only meaningful over an unfiltered sweep: a filtered one never visits
    most classified lines and would report them all as stale.

    Returns a report with:
        pinned        count of constants the vectors pin
        categorized   {category: [(item, entry), ...]} for the unpinned
        unclassified  unpinned constants with no map entry
        stale         entries whose line, value, or anchor no longer matches
        drifted       entries the vectors now pin (the map says they do not)
        errors        constants the sweep could not measure
    """

    unpinned = {}
    pinned_lines = set()
    errors = []
    collisions = []

    for item, outcome in rows:

        if outcome == "IDENTICAL":
            # The map is keyed by line, so two unpinned constants sharing
            # a line cannot both be looked up. Surface the second one as
            # unclassified instead of silently dropping it.
            if item["lineno"] in unpinned:
                collisions.append(item)
            else:
                unpinned[item["lineno"]] = item
        elif outcome == "DIFFERS":
            pinned_lines.add(item["lineno"])
        else:
            errors.append((item, outcome))

    categorized = {
        category: [] for category in CATEGORY_LABELS
    }

    stale = []
    drifted = []

    for lineno, entry in sorted(CLASSIFICATIONS.items()):

        item = unpinned.get(lineno)

        if item is not None:

            if (
                item["value"] == entry["value"]
                and entry["anchor"] in item["line"]
                and (
                    "name" not in entry
                    or item["name"] == entry["name"]
                )
            ):
                categorized[entry["category"]].append(
                    (item, entry)
                )
            else:
                stale.append((lineno, entry, item))

        elif lineno in pinned_lines:
            drifted.append((lineno, entry))

        else:
            stale.append((lineno, entry, None))

    unclassified = collisions + [
        item
        for lineno, item in sorted(unpinned.items())
        if lineno not in CLASSIFICATIONS
    ]

    return {
        "pinned": len(pinned_lines),
        "categorized": categorized,
        "unclassified": unclassified,
        "stale": stale,
        "drifted": drifted,
        "errors": errors,
    }


def print_classification(report):
    """The five-category accounting, printed after the sweep table."""

    print()
    print(
        "Classification of the "
        f"{sum(len(v) for v in report['categorized'].values())}"
        " constants the vectors do not pin:"
    )

    for category in sorted(CATEGORY_LABELS):

        entries = report["categorized"][category]

        print()
        print(
            f"  {category} - {CATEGORY_LABELS[category]} "
            f"({len(entries)})"
        )

        for item, entry in entries:

            label = item["name"] or item["line"][:24]

            print(
                f"    line {item['lineno']:<5d} "
                f"{label:<24.24s} = {item['value']}"
            )
            print(f"      {entry['reason']}")

            for name in entry.get("tests", ()):
                print(f"      test: {name}")

    problems = 0

    for heading, key in (
        ("unclassified", "unclassified"),
        ("stale", "stale"),
        ("drifted (vectors now pin these)", "drifted"),
    ):

        entries = report[key]
        problems += len(entries)

        if entries:

            print()
            print(f"  {heading.upper()}: {len(entries)}")

            for entry in entries:
                print(f"    {entry}")

    print()

    if problems or report["categorized"]["D"] or report["errors"]:
        print(
            "TAXONOMY VIOLATED: stale, drifted, unclassified, category-D, "
            "or unmeasurable constants present. The suite gates on this."
        )
    else:
        print(
            "Taxonomy holds: every unpinned constant is classified, no "
            "live behavior is uncovered, nothing is stale or drifted."
        )


def sweep(filter_text=None):
    """
    Perturb each constant in turn and record whether the vectors moved.
    """

    source, found = constants()

    with open(VECTOR_FILE, encoding="utf-8") as file:
        committed = file.read()

    environment = dict(
        os.environ,
        PYTHONDONTWRITEBYTECODE="1",
    )

    lines = source.split("\n")

    rows = []

    try:

        for item in found:

            if filter_text and filter_text not in (
                item["name"] + item["line"]
            ):
                continue

            original = lines[item["lineno"] - 1]

            replacement = literal_text(
                item["value"],
                perturbed(item["value"]),
            )

            lines[item["lineno"] - 1] = (
                original[:item["col"]]
                + replacement
                + original[item["end_col"]:]
            )

            try:

                with open(TARGET, "w", encoding="utf-8") as file:
                    file.write("\n".join(lines))

                purge_bytecode()

                procedure = subprocess.run(
                    [sys.executable, "-B", "-c", CHECK],
                    cwd=BASE_DIR,
                    capture_output=True,
                    text=True,
                    env=environment,
                )

                output = (
                    procedure.stdout.strip().split("\n") or [""]
                )[-1]

                if output not in ("DIFFERS", "IDENTICAL"):
                    output = "ERROR: " + (
                        procedure.stderr.strip()[-60:]
                        or "no output"
                    )

                rows.append((item, output))

            finally:

                lines[item["lineno"] - 1] = original

    finally:

        # Whatever happened, the module goes back byte for byte. A sweep that
        # left the ranking edited would be worse than no sweep at all.
        with open(TARGET, "w", encoding="utf-8") as file:
            file.write(source)

        purge_bytecode()

    return committed, rows


def main(argv=None):

    parser = argparse.ArgumentParser(
        description=(
            "Report which ranking constants the golden vectors pin."
        ),
    )

    parser.add_argument(
        "--filter",
        default=None,
        help="Only test constants whose name or line contains this text.",
    )

    arguments = parser.parse_args(argv)

    with open(TARGET, encoding="utf-8") as file:
        before = file.read()

    _committed, rows = sweep(arguments.filter)

    with open(TARGET, encoding="utf-8") as file:
        after = file.read()

    if before != after:
        print(
            "FATAL: search.py was not restored byte for byte",
            file=sys.stderr,
        )
        return 1

    print()
    print(f"{'line':>5s}  {'constant':<24s} {'value':>7s}  result")
    print("-" * 62)

    pinned = 0
    unpinned = []
    errors = []

    for item, outcome in rows:

        label = item["name"] or item["line"][:24]

        print(
            f"{item['lineno']:>5d}  {label:<24.24s} "
            f"{item['value']:>7}  {outcome}"
        )

        if outcome == "DIFFERS":
            pinned += 1
        elif outcome == "IDENTICAL":
            unpinned.append((item, label))
        else:
            errors.append((item, outcome))

    print("-" * 62)
    print(
        f"perturbed {len(rows)} constants: "
        f"{pinned} pinned, {len(unpinned)} not pinned, "
        f"{len(errors)} errors"
    )

    if unpinned:

        print()
        print("Not pinned by the vectors:")

        for item, label in unpinned:
            print(
                f"  line {item['lineno']:<5d} {label:<24.24s} "
                f"= {item['value']}"
            )

        print()
        print(
            "Each of these needs a vector case, an engine test, or a reason "
            "it cannot affect any output - classified below, and gated by "
            "tests/test_vector_coverage_tool.py."
        )

    if arguments.filter is None:
        print_classification(classify(rows))

    if errors:

        print()
        print("Constants that could not be measured:")

        for item, outcome in errors:
            print(f"  line {item['lineno']}: {outcome}")

    return 0


if __name__ == "__main__":

    sys.exit(main())
