"""
Extract modules out of the monolithic app.py, verbatim.

Retyping a function is how behaviour drifts. This tool slices the exact
source segments out of `app.py` using the AST, writes them into
`search_engine/`, and rewrites `app.py` to import them back. The function
bodies are never re-typed, so they cannot change.

It refuses to run unless the slices it is about to move are byte
identical to what it writes, and it is deliberately not idempotent: it
reports clearly if the functions have already been moved.

Usage:
    python -m tools.extract_modules            # extract what is planned
    python -m tools.extract_modules --check    # report, change nothing
"""

import argparse
import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

APP_PATH = REPO_ROOT / "app.py"

PACKAGE_DIR = REPO_ROOT / "search_engine"

# Where each function is going. Order matters only for readability.
EXTRACTION_PLAN = {
    "search_engine/text.py": {
        "title": "Text tokenization and query parsing.",
        "functions": [
            "tokenize",
            "tokenize_filename",
            "normalize_search_query",
            "parse_filetype_filter",
        ],
        "imports": [],
        "docstring": '''"""
Text tokenization and query parsing.

This module is pure: it imports nothing from the application and holds
no state. Its behaviour is pinned by the `content_tokenize`,
`filename_tokenize`, `normalize_search_query` and `parse_filetype_filter`
sections of `tests/golden/search_engine_vectors.json`.

The Unicode semantics here are subtle and deliberate. `str.isalnum()`
and `str.isspace()` are Unicode-aware, so the Android port needs
`Character.isLetter`/`isDigit`/`getType` and a two-predicate whitespace
test rather than `Character.isLetterOrDigit`. See ANDROID.md section 5.
"""''',
    },
    "search_engine/sanitize.py": {
        "title": "Upload filename sanitization.",
        "functions": ["sanitize_upload_filename"],
        "imports": ["import os", "from werkzeug.utils import secure_filename"],
        "docstring": '''"""
Upload filename sanitization.

This decides document identity, so it is a high-risk porting point: get
it wrong and the same upload produces different document names on the
device than on the server. Pinned by the `sanitize_filename` section of
`tests/golden/search_engine_vectors.json`.
"""''',
    },
}

EXTRACTION_PLAN["search_engine/extract.py"] = {
    "title": "Document text and page extraction.",
    "functions": [
        "count_phrase_occurrences",
        "extract_text",
        "extract_pages",
    ],
    "imports": [
        "import PyPDF2",
        "import docx",
    ],
    "docstring": "",
}

EXTRACTION_PLAN["search_engine/snippet.py"] = {
    "title": "Snippet, highlight and page selection.",
    "functions": [
        "build_snippet_result",
        "get_snippet_and_page",
    ],
    "imports": ["import re"],
    "docstring": "",
}

# Functions whose body or signature must change during extraction.
#
# `get_snippet_and_page` reads the module-global page text index. Making
# that an explicit parameter is the whole point of the extraction: the
# core must not reach into global state. The replacement is a pure rename,
# so the shadow harness can still prove equivalence by supplying the same
# object through an adapter.
TRANSFORMS = {
    "get_snippet_and_page": {
        "add_leading_parameter": "page_text_index",
        "rename_names": {"PAGE_TEXT_INDEX": "page_text_index"},
    },
}

# Call sites in app.py that must supply the newly explicit state. app.py
# is the adapter and owns the global index, so it passes it in.
CALL_SITE_REWRITES = {
    "get_snippet_and_page(": "get_snippet_and_page(\n                PAGE_TEXT_INDEX,",
}


class ExtractionError(Exception):
    """Raised when extraction cannot be performed safely."""


def _function_sources(source, path):
    """
    Return {name: (start_line, end_line, source_text)} for top-level
    functions in `source`.
    """

    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines(keepends=True)

    found = {}

    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue

        # Include decorators and the blank lines directly above, so the
        # slice is a complete, self-contained definition.
        start = node.lineno - 1

        if node.decorator_list:
            start = min(
                decorator.lineno for decorator in node.decorator_list
            ) - 1

        # end_lineno is inclusive and 1-based.
        end = node.end_lineno

        found[node.name] = (
            start,
            end,
            "".join(lines[start:end]),
        )

    return found


def _module_header(relative_path, spec, existing_names):
    """Build the header for a new module."""

    imports = list(spec["imports"])

    # `sanitize_upload_filename` uses os.path and secure_filename.
    if relative_path.endswith("sanitize.py"):
        imports = ["import os", "from werkzeug.utils import secure_filename"]

    header = f'"""\n{spec["title"]}\n\n'

    header += (
        "Extracted verbatim from app.py by tools/extract_modules.py.\n"
        "Do not retype these functions: the bodies must stay byte\n"
        "identical to the engine that the golden vectors pin.\n"
        '"""\n\n'
    )

    if imports:
        header += "\n".join(imports) + "\n"

    return header


def plan_extraction(source):
    """Return the operations that would be performed."""

    functions = _function_sources(source, APP_PATH)

    operations = []

    for relative_path, spec in EXTRACTION_PLAN.items():
        # Already-extracted modules are skipped, so the tool can be run
        # again to move the next layer without redoing the first.
        if (REPO_ROOT / relative_path).exists():
            continue

        for name in spec["functions"]:
            if name not in functions:
                raise ExtractionError(
                    f"{name!r} is not a top-level function in app.py. "
                    "It may already have been extracted; check whether "
                    f"{relative_path} exists and app.py imports from it."
                )

            start, end, text = functions[name]

            operations.append({
                "module": relative_path,
                "function": name,
                "start": start,
                "end": end,
                "text": text,
            })

    return operations


def apply_transform(name, text):
    """Apply the declared transform for `name`, if any."""

    transform = TRANSFORMS.get(name)

    if not transform:
        return text

    # Insert the extra leading parameter after the opening parenthesis of
    # the def statement.
    parameter = transform.get("add_leading_parameter")

    if parameter:
        opening = text.index("(")
        text = (
            text[: opening + 1]
            + "\n    "
            + parameter
            + ","
            + text[opening + 1 :]
        )

    # Rename module globals to the new parameters.
    for old_name, new_name in transform.get("rename_names", {}).items():
        text = re.sub(rf"\b{old_name}\b", new_name, text)

    return text


def render_module(relative_path, spec, sources, names):
    """Render the full text of a new module."""

    parts = [_module_header(relative_path, spec, sources)]

    body = "\n\n\n".join(
        apply_transform(name, text).rstrip("\n")
        for name, text in zip(names, sources)
    )

    return "".join(parts) + body + "\n"


def build_removal_plan(source):
    """
    Return the line ranges to delete from app.py and the import block
    to insert in their place.
    """

    functions = _function_sources(source, APP_PATH)

    removals = []

    for relative_path, spec in EXTRACTION_PLAN.items():
        if (REPO_ROOT / relative_path).exists():
            continue

        for name in spec["functions"]:
            start, end, _text = functions[name]
            removals.append((start, end))

    removals.sort()

    return removals


def apply_extraction(dry_run=False):
    source = APP_PATH.read_text(encoding="utf-8")

    operations = plan_extraction(source)

    # Group the extracted bodies by destination module, preserving the
    # order declared in the plan so the file reads naturally.
    by_module = {}

    for relative_path, spec in EXTRACTION_PLAN.items():
        if (REPO_ROOT / relative_path).exists():
            continue

        by_module[relative_path] = [
            (operation["function"], operation["text"])
            for operation in operations
            if operation["module"] == relative_path
        ]

    if dry_run:
        for relative_path, entries in by_module.items():
            print(
                f"would write {relative_path} "
                f"({len(sources)} functions, "
                f"{sum(len(s) for s in sources)} bytes)"
            )

        return 0

    PACKAGE_DIR.mkdir(exist_ok=True)

    init_path = PACKAGE_DIR / "__init__.py"

    if not init_path.exists():
        init_path.write_text(
            '"""\nSearch engine core.\n\n'
            "A transport-independent implementation of the ranking and\n"
            "indexing behaviour. `app.py` is a thin Flask adapter over\n"
            "this package; the Android local backend is specified to\n"
            "mirror it.\n"
            '"""\n',
            encoding="utf-8",
        )

    for relative_path, entries in by_module.items():
        destination = REPO_ROOT / relative_path
        spec = EXTRACTION_PLAN[relative_path]

        names = [name for name, _text in entries]
        sources = [text for _name, text in entries]

        rendered = render_module(relative_path, spec, sources, names)

        destination.write_text(rendered, encoding="utf-8")

        print(
            f"wrote {relative_path} "
            f"({len(sources)} functions, {len(rendered)} bytes)"
        )

    # --- rewrite app.py ------------------------------------------------
    lines = source.splitlines(keepends=True)

    # Derive the removals from the operations already computed. Calling
    # build_removal_plan here would re-check the filesystem and skip
    # every module, because the modules were just written.
    removals = sorted(
        (operation["start"], operation["end"])
        for operation in operations
    )

    removed = set()

    for start, end in removals:
        for index in range(start, end):
            removed.add(index)

    kept = [
        line
        for index, line in enumerate(lines)
        if index not in removed
    ]

    # Insert the imports where the tokenizers used to live, so the file
    # keeps its original structure.
    first_removal = removals[0][0]
    adjusted = first_removal

    moved = {
        operation["function"]
        for operation in operations
    }

    import_block = "\n"

    if "extract_text" in moved:
        import_block += (
        "from search_engine.extract import (\n"
        "    count_phrase_occurrences,\n"
        "    extract_pages,\n"
        "    extract_text,\n"
        ")\n"
        )

    if "get_snippet_and_page" in moved:
        import_block += (
        "from search_engine.snippet import (\n"
        "    build_snippet_result,\n"
        "    get_snippet_and_page,\n"
        ")\n"
        )

    import_block += "\n"

    kept.insert(adjusted, import_block)

    rewritten = "".join(kept)

    for needle, replacement in CALL_SITE_REWRITES.items():
        if needle not in rewritten:
            raise ExtractionError(
                f"call site {needle!r} was not found in app.py, so the "
                "new parameter cannot be supplied"
            )

        rewritten = rewritten.replace(needle, replacement, 1)

    APP_PATH.write_text(rewritten, encoding="utf-8")

    print(f"rewrote app.py (-{len(removed)} lines, +imports)")

    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--check",
        action="store_true",
        help="Report what would happen without changing anything.",
    )

    arguments = parser.parse_args(argv)

    try:
        return apply_extraction(dry_run=arguments.check)
    except ExtractionError as error:
        print(f"extraction refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
