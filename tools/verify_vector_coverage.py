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

Exit code is 0 when the run completes, whatever it finds; this reports, it does
not gate. ANDROID.md records the current result and the reason for each
constant the vectors cannot reach.

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
            "it cannot affect any output. ANDROID.md records which."
        )

    if errors:

        print()
        print("Constants that could not be measured:")

        for item, outcome in errors:
            print(f"  line {item['lineno']}: {outcome}")

    return 0


if __name__ == "__main__":

    sys.exit(main())
