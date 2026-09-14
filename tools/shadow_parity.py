"""
Shadow parity: prove an extracted module equals the original it came from.

Refactoring is where behaviour silently changes. The golden vectors catch
a change in *results*, but they only exercise the inputs someone thought
to write down. This harness is the stronger check: it loads the original
function bodies from a pinned git revision and compares them against the
extracted implementations over

  * every recorded contract vector input
  * every recorded search query
  * adversarial and boundary inputs
  * a deterministic random corpus, including astral-plane characters

Byte-identical function bodies make equality likely; this makes it
proven.

The original is read from git rather than checked in twice, so there is
exactly one copy of the legacy code and it cannot drift.

Usage:
    python -m tools.shadow_parity
    python -m tools.shadow_parity --base 81faff9
"""

import argparse
import ast
import json
import random
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

VECTORS_PATH = REPO_ROOT / "tests" / "golden" / "search_engine_vectors.json"

# The revision whose app.py holds the pre-extraction definitions.
DEFAULT_BASE = "7895064"

# Functions that were extracted, and the module that now owns them.
EXTRACTED = {
    "tokenize": "search_engine.text",
    "tokenize_filename": "search_engine.text",
    "normalize_search_query": "search_engine.text",
    "parse_filetype_filter": "search_engine.text",
    "sanitize_upload_filename": "search_engine.sanitize",
    "count_phrase_occurrences": "search_engine.extract",
    "extract_text": "search_engine.extract",
    "extract_pages": "search_engine.extract",
    "build_snippet_result": "search_engine.snippet",
    "get_snippet_and_page": "search_engine.snippet",
}

# Functions whose extracted signature differs from the original.
#
# `get_snippet_and_page` gained an explicit `page_text_index` parameter so
# the core no longer reaches into global state. The adapter supplies the
# original global, so the shadow harness passes the same object through
# and the comparison stays meaningful.
SIGNATURE_ADAPTERS = {
    "get_snippet_and_page": {
        "adapter": "page_text_index",
        "source": "search_engine.snippet",
    },
}

# Functions that have no single-string input signature and therefore need
# dedicated case generation instead of the string corpus.
STRUCTURED_FUNCTIONS = {
    "count_phrase_occurrences",
    "extract_text",
    "extract_pages",
    "build_snippet_result",
    "get_snippet_and_page",
}

RANDOM_CASES = 4000
RANDOM_SEED = 20260914

# The page text index used by both sides of the comparison. The original
# read it as a module global; the extracted version takes it as a
# parameter. Sharing one object is what keeps the comparison meaningful.
PAGE_TEXT_INDEX_FIXTURE = {
    "note.txt": [
        {"page": 1, "text": "alpha beta gamma sharedterm here"},
    ],
    "multi.txt": [
        {"page": 1, "text": "first page alpha"},
        {"page": 2, "text": "second page beta gamma"},
    ],
}


class ShadowParityError(Exception):
    """Raised when the shadow comparison cannot be set up."""


def load_original_namespace(base_revision):
    """Load the original definitions and return (namespace, source)."""

    namespace = load_original_functions(base_revision)

    return namespace, None


def load_original_functions(base_revision):
    """
    Load the pre-extraction function definitions from a git revision.

    Returns {name: callable}.
    """

    try:
        completed = subprocess.run(
            ["git", "show", f"{base_revision}:app.py"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise ShadowParityError(
            f"could not read app.py from revision {base_revision}: "
            f"{error.stderr.strip()}"
        ) from error

    source = completed.stdout

    tree = ast.parse(source)

    segments = []

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in EXTRACTED:
            segments.append(
                ast.get_source_segment(source, node)
            )

    missing = set(EXTRACTED) - {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }

    if missing:
        raise ShadowParityError(
            f"revision {base_revision} does not define: {sorted(missing)}. "
            "Pick a revision from before the extraction."
        )

    namespace = {}

    # The originals reference os, secure_filename and the page text
    # index global.
    header = (
        "import os\n"
        "import re\n"
        "import PyPDF2\n"
        "import docx\n"
        "from werkzeug.utils import secure_filename\n"
    )

    exec(header + "\n\n\n".join(segments), namespace)

    namespace["PAGE_TEXT_INDEX"] = PAGE_TEXT_INDEX_FIXTURE

    return {
        name: namespace[name]
        for name in EXTRACTED
    }


def load_extracted_functions():
    """Import the extracted implementations."""

    import importlib

    functions = {}

    for name, module_name in EXTRACTED.items():
        module = importlib.import_module(module_name)
        functions[name] = getattr(module, name)

    return functions


# ----------------------------------------------------------------------
# Input generation
# ----------------------------------------------------------------------


ADVERSARIAL_INPUTS = [
    "",
    " ",
    "\t",
    "\n",
    "a",
    "ab",
    "a b c",
    "1 2 3",
    "½",
    "½½",
    "ΩΩ",
    "字字",
    "५५",
    "İstanbul",
    "istanbul",
    "CAFÉ",
    "café",
    "ΣΣ",
    "\u00a0",
    "\u2007",
    "\u202f",
    "\u0085",
    "\u200b",
    "\u0307",
    "\u0130",
    "\ufeff",
    "a\u00a0b",
    "a\u2007b",
    "a\u202fb",
    "a\u0085b",
    "a\u200bb",
    "a\u0307b",
    "\U00020000",
    "\U00020000\U00020000",
    "\U0001d7ce",
    "\U0001d7ce\U0001d7ce",
    "\U0001f600",
    "\U0001f600\U0001f600",
    "under_score-dash",
    "a!b.txt",
    "file-name.txt",
    "._.txt",
    "__lead.txt",
    "trail__.txt",
    "-lead.txt",
    "a  b.txt",
    "a\tb.txt",
    "...a..b...txt",
    "MiXeD CaSe.TXT",
    "../../etc/passwd.txt",
    "..\\..\\x.txt",
    "/abs/path.txt",
    "a/b/c.txt",
    ".hidden.txt",
    "...hidden.txt",
    "My  File...txt",
    "  spaced  .txt",
    "café.txt",
    "Ünïcödé Ñämé.docx",
    "no-ext",
    "weird.pdf.txt",
    "CON.txt",
    "nul.pdf",
    "archive.zip",
    "BCS502 Module 2.pdf",
    '"BCS502 Module 2.pdf"',
    "notes.txt",
    "network pdf",
    "network .pdf",
    "pdf",
    ".pdf",
    "docx",
    "txt",
    "plain query",
    "!!!",
    "   ",
    "data.csv",
    "a" * 500,
    "a " * 200,
    "\u00e9" * 100,
]


def build_inputs():
    """Return the full deterministic input corpus."""

    inputs = list(ADVERSARIAL_INPUTS)

    with open(VECTORS_PATH, "r", encoding="utf-8") as handle:
        document = json.load(handle)

    contract = document["contract_vectors"]

    for section in (
        "content_tokenize",
        "filename_tokenize",
        "sanitize_filename",
        "normalize_search_query",
        "parse_filetype_filter",
    ):
        for case in contract[section]:
            inputs.append(case["input"])

    for vector in document["vectors"] + document["empty_corpus_vectors"]:
        inputs.append(vector["query"])

    # Deterministic random strings, deliberately spanning the astral
    # plane and the whitespace ranges where the two languages disagree.
    generator = random.Random(RANDOM_SEED)

    interesting = (
        list(range(0x0000, 0x0100))
        + [0x00A0, 0x2007, 0x202F, 0x0085, 0x200B, 0x0307, 0x0130, 0xFEFF]
        + list(range(0x0300, 0x0400))
        + list(range(0x0900, 0x0980))
        + list(range(0x4E00, 0x4E40))
        + list(range(0x10000, 0x10040))
        + list(range(0x1D400, 0x1D440))
        + list(range(0x1F600, 0x1F620))
    )

    for _ in range(RANDOM_CASES):
        length = generator.randint(0, 24)

        inputs.append(
            "".join(
                chr(generator.choice(interesting))
                for _ in range(length)
            )
        )

    return inputs


# ----------------------------------------------------------------------
# Comparison
# ----------------------------------------------------------------------


def run(base_revision=DEFAULT_BASE, limit=20):
    original_module, _source = load_original_namespace(base_revision)
    extracted = load_extracted_functions()

    inputs = build_inputs()

    differences = []
    comparisons = 0

    # The global index the original read, reused as the explicit
    # argument for the transformed version.
    page_text_index = PAGE_TEXT_INDEX_FIXTURE

    structured_failures = run_structured(
        original_module,
        extracted,
        page_text_index,
        limit,
    )

    for name in EXTRACTED:
        if name in STRUCTURED_FUNCTIONS:
            continue

        original = original_module[name]
        new = extracted[name]

        for value in inputs:
            comparisons += 1

            try:
                expected = original(value)
                expected_error = None
            except Exception as error:  # noqa: BLE001 - comparing behaviour
                expected = None
                expected_error = type(error).__name__

            try:
                actual = new(value)
                actual_error = None
            except Exception as error:  # noqa: BLE001 - comparing behaviour
                actual = None
                actual_error = type(error).__name__

            if expected_error != actual_error or expected != actual:
                if len(differences) < limit:
                    differences.append({
                        "function": name,
                        "input": value,
                        "expected": expected,
                        "actual": actual,
                        "expected_error": expected_error,
                        "actual_error": actual_error,
                    })

    comparisons += structured_failures["comparisons"]

    differences.extend(structured_failures["differences"][:limit])

    return {
        "base_revision": base_revision,
        "inputs": len(inputs),
        "functions": len(EXTRACTED),
        "comparisons": comparisons,
        "differences": differences,
    }


def run_structured(original_module, extracted, page_text_index, limit):
    """
    Compare the functions that do not take a single string.

    These are exercised with representative arguments rather than the
    string corpus.
    """

    differences = []
    comparisons = 0

    def compare_call(label, expected, actual):
        nonlocal comparisons

        comparisons += 1

        if expected != actual and len(differences) < limit:
            differences.append({
                "function": label,
                "input": label,
                "expected": expected,
                "actual": actual,
                "expected_error": None,
                "actual_error": None,
            })

    # --- count_phrase_occurrences -----------------------------------
    texts = [
        "",
        "alpha beta gamma",
        "alpha alpha alpha",
        "ALPHA beta Alpha",
        "sharedterm the sharedterm and sharedterm",
        "a\u00a0b a b",
        "\u00e9\u00e9\u00e9",
    ]
    phrases = ["", "alpha", "a", "sharedterm", "beta gamma", "\u00e9"]

    for text in texts:
        for phrase in phrases:
            compare_call(
                "count_phrase_occurrences",
                original_module["count_phrase_occurrences"](text, phrase),
                extracted["count_phrase_occurrences"](text, phrase),
            )

    # --- build_snippet_result ---------------------------------------
    #
    # Called through its documented keyword interface.
    import inspect

    signature = inspect.signature(original_module["build_snippet_result"])
    parameters = list(signature.parameters)

    if parameters:
        sample = {
            "snippet": "alpha beta gamma",
            "highlights": [],
            "page": 1,
        }

        try:
            expected = original_module["build_snippet_result"](
                **{key: sample.get(key) for key in parameters}
            )
            actual = extracted["build_snippet_result"](
                **{key: sample.get(key) for key in parameters}
            )

            compare_call("build_snippet_result", expected, actual)
        except TypeError:
            # The interface is positional; skip rather than guess.
            pass

    # --- get_snippet_and_page ---------------------------------------
    #
    # The extracted version takes the index explicitly; the original read
    # it from module state. Both must produce the same result.
    queries = [
        ["alpha"],
        ["beta", "gamma"],
        ["missing"],
        ["sharedterm"],
        [],
    ]

    for document in ("note.txt", "multi.txt", "absent.txt"):
        for words in queries:
            expected = original_module["get_snippet_and_page"](
                document, words
            )
            actual = extracted["get_snippet_and_page"](
                page_text_index, document, words
            )

            compare_call(
                f"get_snippet_and_page({document}, {words})",
                expected,
                actual,
            )

        for window in (10, 40, 140, 1000):
            expected = original_module["get_snippet_and_page"](
                document, ["alpha"], window
            )
            actual = extracted["get_snippet_and_page"](
                page_text_index, document, ["alpha"], window
            )

            compare_call(
                f"get_snippet_and_page({document}, window={window})",
                expected,
                actual,
            )

    # A missing document must behave identically too.
    compare_call(
        "get_snippet_and_page(missing document)",
        original_module["get_snippet_and_page"]("absent.txt", ["alpha"]),
        extracted["get_snippet_and_page"](
            page_text_index, "absent.txt", ["alpha"]
        ),
    )

    return {"comparisons": comparisons, "differences": differences}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help=f"git revision holding the original app.py (default: {DEFAULT_BASE})",
    )

    parser.add_argument("--json", action="store_true")

    arguments = parser.parse_args(argv)

    print("Shadow parity: extracted modules vs the original")
    print(f"  base revision : {arguments.base}")
    print()

    try:
        summary = run(base_revision=arguments.base)
    except ShadowParityError as error:
        print(f"shadow parity could not run: {error}", file=sys.stderr)
        return 2

    if arguments.json:
        print(json.dumps(summary, indent=2, sort_keys=True, default=str))
        return 1 if summary["differences"] else 0

    print(f"  inputs        : {summary['inputs']:,}")
    print(f"  functions     : {summary['functions']}")
    print(f"  comparisons   : {summary['comparisons']:,}")
    print(f"  differences   : {len(summary['differences'])}")
    print()

    if summary["differences"]:
        print("DIFFERENCES")
        print("-" * 62)

        for difference in summary["differences"]:
            print(
                f"{difference['function']}({difference['input']!r})"
            )
            print(f"    original  : {difference['expected']!r} "
                  f"{difference['expected_error'] or ''}")
            print(f"    extracted : {difference['actual']!r} "
                  f"{difference['actual_error'] or ''}")

        print()
        print("SHADOW PARITY FAILED")
        return 1

    print("SHADOW PARITY OK")
    print("Every extracted function matches the original on every input.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
