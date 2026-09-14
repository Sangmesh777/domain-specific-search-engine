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

# ---------------------------------------------------------------
# Layer 4: persistence.
# ---------------------------------------------------------------
#
# This layer needs more than a rename, so its plan lives in its own
# module and is merged in here. The wrappers below are what keeps the
# layer honest: every function this layer removes from app.py is
# replaced by a thin adapter, so the call sites in the document-import
# and bulk-delete paths keep working untouched, and app.py is left with
# no SQL, no schema and no file handling.

from tools.storage_extraction_spec import (  # noqa: E402
    CONSTANTS as STORAGE_CONSTANTS,
    DOCSTRING as STORAGE_DOCSTRING,
    EXTRA_SOURCE as STORAGE_EXTRA_SOURCE,
    FUNCTIONS as STORAGE_FUNCTIONS,
    IMPORTS as STORAGE_IMPORTS,
    RENAMES as STORAGE_RENAMES,
    REPLACED as STORAGE_REPLACED,
    TITLE as STORAGE_TITLE,
    TRANSFORMS as STORAGE_TRANSFORMS,
)

EXTRACTION_PLAN["search_engine/storage.py"] = {
    "title": STORAGE_TITLE,
    "functions": STORAGE_FUNCTIONS,
    "constants": STORAGE_CONSTANTS,
    "replaced": STORAGE_REPLACED,
    "imports": STORAGE_IMPORTS,
    "docstring": STORAGE_DOCSTRING,
    "extra_source": STORAGE_EXTRA_SOURCE,
    "wrappers": '''
# ============================================================
# PERSISTENCE ADAPTERS
# ============================================================
#
# Every function below is a thin adapter over search_engine/storage.py.
# The storage layer owns the SQL, the schema, the JSON files and the
# filesystem paths; this file owns publication and the module-level
# index globals. Nothing here may grow storage logic back into the
# application.

def get_sqlite_connection():
    """
    Thin adapter over storage.get_sqlite_connection.

    Kept so the document-import and bulk-delete paths - which this
    milestone must not touch - keep their existing call sites.
    """

    return storage.get_sqlite_connection(
        SQLITE_DB_FILE
    )


def initialize_sqlite_store():
    """
    Create the SQLite index, migrating once if it is empty.

    Orchestration only: the schema, the migration write and the
    reconstruction all live in search_engine/storage.py. The single
    connection is held here so the schema, the emptiness check and a
    possible migration happen on one connection, exactly as before.
    """

    os.makedirs(DATA_FOLDER, exist_ok=True)

    with get_sqlite_connection() as connection:

        storage.create_schema(
            connection
        )

        if (
            storage.count_documents(connection) == 0
            and DOCUMENT_METADATA
        ):
            sync_sqlite_from_memory(
                connection
            )

    # SQLite becomes the persistent source of truth for the
    # incremental document index.
    load_database_from_sqlite()


def sync_sqlite_from_memory(connection=None):
    """
    Thin adapter: publish-then-persist bookkeeping stays in app.py.

    The snapshot is read through IndexState rather than off the module
    globals. They name the same objects, but this way the four
    containers provably come from one generation.
    """

    snapshot = INDEX_STATE.snapshot()

    # Named rather than splatted: snapshot() also carries `generation`,
    # which is not part of the persisted state.
    return storage.sync_sqlite_from_memory(
        db_path=SQLITE_DB_FILE,
        connection=connection,
        inverted_index=snapshot["inverted_index"],
        document_metadata=snapshot["document_metadata"],
        filename_index=snapshot["filename_index"],
        page_text_index=snapshot["page_text_index"],
    )


def load_database_from_sqlite():
    """
    Rebuild from SQLite, then publish once under the lock.

    storage returns a complete snapshot and installs nothing, so the
    publication order stays in one place in this file.
    """

    replacement = storage.load_database_from_sqlite(
        db_path=SQLITE_DB_FILE,
    )

    # An empty SQLite store means "leave the active state alone", which
    # is what the original early return did.
    if replacement is None:
        return

    with INDEX_DATA_LOCK:

        publish_index_state(replacement)


def get_document_term_counts_from_sqlite(filename):
    """Thin adapter over storage.get_document_term_counts_from_sqlite."""

    return storage.get_document_term_counts_from_sqlite(
        SQLITE_DB_FILE,
        filename,
    )


def load_database():
    """
    Restore the legacy JSON snapshot files, then publish once.

    storage builds the snapshot from the four files, seeded with the
    current containers so a missing file leaves its container alone.
    """

    with INDEX_DATA_LOCK:

        publish_index_state(
            storage.load_json_snapshot(
                index_file=INDEX_FILE,
                meta_file=META_FILE,
                filename_index_file=FILENAME_INDEX_FILE,
                page_text_file=PAGE_TEXT_FILE,
                current=INDEX_STATE.snapshot(),
            )
        )


def save_database_snapshot(
    inverted_index,
    document_metadata,
    filename_index,
    page_text_index,
):
    """Thin adapter over storage.save_database_snapshot."""

    storage.save_database_snapshot(
        index_file=INDEX_FILE,
        meta_file=META_FILE,
        filename_index_file=FILENAME_INDEX_FILE,
        page_text_file=PAGE_TEXT_FILE,
        inverted_index=inverted_index,
        document_metadata=document_metadata,
        filename_index=filename_index,
        page_text_index=page_text_index,
    )


def save_database():
    """
    Capture one generation under the lock, then write it out.

    No call sites: this was already unreachable before the extraction.
    It is kept because it is part of the persistence API and removing it
    is a separate decision from relocating it.
    """

    with INDEX_DATA_LOCK:

        snapshot = INDEX_STATE.snapshot()

    save_database_snapshot(
        snapshot["inverted_index"],
        snapshot["document_metadata"],
        snapshot["filename_index"],
        snapshot["page_text_index"],
    )

''',
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

# The persistence layer's transforms are declared in their own module;
# merge them once the dict exists.
TRANSFORMS.update(STORAGE_TRANSFORMS)

# A function can also change name on the way in, when its old name would
# be ambiguous in the new module.
EXTRACTION_RENAMES = STORAGE_RENAMES

# Call sites in app.py that must supply the newly explicit state. app.py
# is the adapter and owns the global index, so it passes it in.
#
# Keyed by the function that was moved, and applied ONLY when that
# function was moved in the current run. Applying them unconditionally
# is not idempotent: the needle `get_snippet_and_page(` is still in
# app.py after the first extraction, so every later run would inject the
# argument again and corrupt the call.
CALL_SITE_REWRITES = {
    "get_snippet_and_page": [
        (
            "get_snippet_and_page(",
            "get_snippet_and_page(\n                PAGE_TEXT_INDEX,",
        ),
    ],
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


def _constant_sources(source, path):
    """
    Return {name: (start_line, end_line, source_text)} for module-level
    string constants, so a constant can be moved without retyping it.
    """

    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines(keepends=True)

    found = {}

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue

        if len(node.targets) != 1:
            continue

        target = node.targets[0]

        if not isinstance(target, ast.Name):
            continue

        # Only constants whose value is a literal are moved here; a
        # computed value would need its dependencies too.
        if not isinstance(node.value, ast.Constant):
            continue

        start = node.lineno - 1
        end = node.end_lineno

        found[target.id] = (
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
    )

    # A module may carry its own design notes. They are appended inside
    # the module docstring rather than left in the extraction spec,
    # because this is the file a reader opens.
    extra_docstring = (spec.get("docstring") or "").strip("\n")

    if extra_docstring:
        header += "\n" + extra_docstring + "\n"

    header += '"""\n'

    if imports:
        header += "\n" + "\n".join(imports) + "\n"

    return header


def plan_extraction(source):
    """Return the operations that would be performed."""

    functions = _function_sources(source, APP_PATH)

    operations = []

    constants = _constant_sources(source, APP_PATH)

    for relative_path, spec in EXTRACTION_PLAN.items():
        # Already-extracted modules are skipped, so the tool can be run
        # again to move the next layer without redoing the first.
        if (REPO_ROOT / relative_path).exists():
            continue

        for name in spec.get("constants", []):
            if name not in constants:
                raise ExtractionError(
                    f"{name!r} is not a module-level literal constant in "
                    "app.py"
                )

            start, end, text = constants[name]

            operations.append({
                "module": relative_path,
                "function": name,
                "kind": "constant",
                "start": start,
                "end": end,
                "text": text,
            })

        # A function can be deleted from app.py without being written
        # into the new module, because its replacement is a wrapper that
        # the plan supplies. It is only removed here.
        for name in spec.get("replaced", []):
            if name not in functions:
                raise ExtractionError(
                    f"{name!r} is marked as replaced but is not a "
                    "top-level function in app.py"
                )

            start, end, text = functions[name]

            operations.append({
                "module": relative_path,
                "function": name,
                "kind": "replaced",
                "start": start,
                "end": end,
                "text": text,
            })

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

    # Exact replacements run first, against the pristine slice, so they
    # can match the original text before any renaming happens. Every
    # pattern must be present exactly once: a transform that silently
    # matched nothing would produce a module that looks right and is not.
    for old_text, new_text in transform.get("replace_exact", []):

        occurrences = text.count(old_text)

        if occurrences != 1:
            raise ExtractionError(
                f"the transform for {name!r} expected exactly one "
                f"occurrence of:\n{old_text!r}\nbut found {occurrences}"
            )

        text = text.replace(old_text, new_text, 1)

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

    # A function can also change name on the way in, when its old name
    # would be ambiguous in the new module.
    new_name = EXTRACTION_RENAMES.get(name)

    if new_name:
        text = re.sub(rf"\bdef {name}\b", f"def {new_name}", text)

    return text


def render_module(relative_path, spec, sources, names, constants=()):
    """Render the full text of a new module."""

    parts = [_module_header(relative_path, spec, sources)]

    # Constants first: functions below refer to them at call time, but
    # keeping the file ordered schema-then-code reads better.
    for _name, text in constants:
        parts.append("\n" + text.rstrip("\n") + "\n\n\n")

    body = "\n\n\n".join(
        apply_transform(name, text).rstrip("\n")
        for name, text in zip(names, sources)
    )

    parts.append(body + "\n")

    # Code that has no counterpart in app.py: thin adapters over the
    # moved bodies, written here so they are part of the reviewed
    # extraction rather than an unrecorded manual edit.
    extra = spec.get("extra_source")

    if extra:
        parts.append("\n\n" + extra.strip("\n") + "\n")

    return "".join(parts)


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
    constants_by_module = {}

    for relative_path, spec in EXTRACTION_PLAN.items():
        if (REPO_ROOT / relative_path).exists():
            continue

        by_module[relative_path] = [
            (operation["function"], operation["text"])
            for operation in operations
            if operation["module"] == relative_path
            and operation.get("kind", "function") == "function"
        ]

        constants_by_module[relative_path] = [
            (operation["function"], operation["text"])
            for operation in operations
            if operation["module"] == relative_path
            and operation.get("kind") == "constant"
        ]

    if dry_run:
        for relative_path, entries in by_module.items():
            print(
                f"would write {relative_path} "
                f"({len(entries)} functions, "
                f"{sum(len(text) for _name, text in entries)} bytes)"
            )

            for name, _text in entries:
                print(f"    {name}")

            for name, _text in constants_by_module.get(relative_path, []):
                print(f"    {name} (constant)")

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

        rendered = render_module(
            relative_path,
            spec,
            sources,
            names,
            constants_by_module.get(relative_path, []),
        )

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

    if "sync_sqlite_from_memory" in moved:
        import_block += (
        "from search_engine import storage\n"
        )

    import_block += "\n"

    # Thin wrappers keep app.py's existing call sites working without
    # changing the functions this milestone must not touch (document
    # import, bulk delete). They contain no SQL, no schema, no file
    # handling and no paths beyond the module constants.
    # Derive the wrapper modules from the operations already computed.
    # Re-checking the filesystem here would skip every module, because
    # the module files were written earlier in this same run.
    wrapper_modules = {
        operation["module"]
        for operation in operations
    }

    wrappers = ""

    for module in EXTRACTION_PLAN:
        if module not in wrapper_modules:
            continue

        wrappers += EXTRACTION_PLAN[module].get("wrappers", "")

    kept.insert(adjusted, wrappers + import_block)

    rewritten = "".join(kept)

    for function, rewrites in CALL_SITE_REWRITES.items():
        # Skip rewrites belonging to an earlier layer. Their argument is
        # already in place, and adding it again would be a silent
        # corruption rather than a loud failure.
        if function not in moved:
            continue

        for needle, replacement in rewrites:
            if needle not in rewritten:
                raise ExtractionError(
                    f"call site {needle!r} was not found in app.py, so "
                    "the new parameter cannot be supplied"
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
