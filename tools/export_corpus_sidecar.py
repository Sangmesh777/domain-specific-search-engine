"""
Generate the pre-extracted corpus sidecar that the offline Android app ships.

Document extraction is the one part of the search engine that does not port.
PyPDF2 and python-docx have no Android equivalent producing identical output,
so a device that parsed the shipped PDFs itself would rank them differently
from the server - and the difference would be invisible until someone compared
the two modes. ANDROID.md sections 7 and 20 settle on the alternative: extract
once, here, and ship the text.

This tool produces that text.

    .venv/bin/python tools/export_corpus_sidecar.py
    .venv/bin/python tools/export_corpus_sidecar.py --corpus data --out /tmp/s.json

By default it generates the canonical development corpus into a temporary
directory and extracts that, so the output does not depend on the gitignored
data/ folder being populated. Point --corpus at a real folder to extract
something else.

The device consumes this file through SearchEngine.index_extracted(), which
takes text and pages instead of a path. That path is verified equivalent to
indexing the real files by
tests/test_search_engine_core.py::test_index_extracted_matches_index_document,
so shipping text costs nothing in ranking fidelity.

Each document also carries an `expected` block: the filename words, word count
and distinct-term count this engine derives from it. That is not input for
indexing, it is a parity fixture. A ported tokenizer computes the same values
on device and compares, which is how a subtle Unicode or word-order mistake
gets caught before it reaches ranking. See ANDROID.md section 4.

Output is deterministic: documents are sorted by stored name and serialized
with a stable key order, so two runs are byte-identical and the committed
artifact can be diffed.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile


BASE_DIR = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "tools"))

from seed_dev_corpus import build_corpus          # noqa: E402

from search_engine.extraction import (            # noqa: E402
    extract_pages,
    extract_text,
)
from search_engine.filenames import (             # noqa: E402
    sanitize_upload_filename,
)
from search_engine.tokenizer import (             # noqa: E402
    tokenize,
    tokenize_filename,
)


SCHEMA_VERSION = 1

DEFAULT_OUTPUT = os.path.join(
    BASE_DIR,
    "artifacts",
    "android",
    "corpus_sidecar.json",
)


CONTRACT = (
    "Pre-extracted corpus text for the offline Android backend. "
    "Index each document with SearchEngine.index_extracted(stored_as, text, "
    "pages); do not parse the original files on device. The `expected` block "
    "is a parity fixture for the ported tokenizer, not indexing input."
)


def extraction_versions():
    """Record what produced this text, so a diff can be explained."""

    try:
        import PyPDF2
        pdf_version = getattr(PyPDF2, "__version__", "unknown")
    except ImportError:
        pdf_version = "absent"

    try:
        import docx
        docx_version = getattr(docx, "__version__", "unknown")
    except ImportError:
        docx_version = "absent"

    return {
        "python": sys.version.split()[0],
        "pypdf2": pdf_version,
        "python_docx": docx_version,
    }


def describe_document(path):
    """Extract one document and everything a port needs to check itself."""

    source_filename = os.path.basename(path)

    stored_as = sanitize_upload_filename(
        source_filename
    )

    if not stored_as:
        return None

    text = extract_text(path, stored_as)

    pages = extract_pages(path, stored_as)

    terms = tokenize(text)

    return {
        "source_filename": source_filename,
        "stored_as": stored_as,
        "title": stored_as,
        "extension": os.path.splitext(stored_as)[1].lower().lstrip("."),
        "text": text,
        "pages": pages,
        "expected": {
            "filename_words": tokenize_filename(
                os.path.splitext(stored_as)[0]
            ),
            "total_words": len(terms),
            "distinct_terms": len(set(terms)),
            "page_count": len(pages),
        },
    }


def build_sidecar(corpus_directory):
    """Extract every supported document in a directory."""

    entries = []

    skipped = []

    for name in sorted(os.listdir(corpus_directory)):

        path = os.path.join(corpus_directory, name)

        if not os.path.isfile(path):
            continue

        entry = describe_document(path)

        if entry is None:
            skipped.append(name)
            continue

        entries.append(entry)

    # Sorted by stored name, so the artifact does not depend on the order the
    # source directory happened to enumerate in.
    entries.sort(key=lambda item: item["stored_as"])

    return {
        "schema_version": SCHEMA_VERSION,
        "generator": "tools/export_corpus_sidecar.py",
        "contract": CONTRACT,
        "extraction": extraction_versions(),
        "document_count": len(entries),
        "documents": entries,
        "skipped": sorted(skipped),
    }


def render(sidecar):
    """Deterministic JSON: stable key order, trailing newline."""

    return json.dumps(
        sidecar,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ) + "\n"


def main(argv=None):

    parser = argparse.ArgumentParser(
        description="Export the pre-extracted corpus sidecar for offline Android.",
    )

    parser.add_argument(
        "--corpus",
        default=None,
        help="Directory of documents to extract. Defaults to a freshly "
             "generated canonical development corpus in a temporary "
             "directory, so the artifact does not depend on data/ being "
             "populated.",
    )

    parser.add_argument(
        "--out",
        default=DEFAULT_OUTPUT,
        help=f"Output path (default {os.path.relpath(DEFAULT_OUTPUT, BASE_DIR)}).",
    )

    arguments = parser.parse_args(argv)

    temporary = None

    try:

        if arguments.corpus:

            corpus = arguments.corpus

            if not os.path.isdir(corpus):
                parser.error(f"no such corpus directory: {corpus}")

        else:

            temporary = tempfile.mkdtemp()

            corpus = os.path.join(temporary, "corpus")

            build_corpus(corpus)

        sidecar = build_sidecar(corpus)

        output = os.path.abspath(arguments.out)

        os.makedirs(os.path.dirname(output), exist_ok=True)

        with open(output, "w", encoding="utf-8") as file:
            file.write(render(sidecar))

        size = os.path.getsize(output)

        print(f"wrote {output}")
        print(f"  schema_version : {sidecar['schema_version']}")
        print(f"  documents      : {sidecar['document_count']}")
        print(f"  extensions     : "
              f"{sorted({d['extension'] for d in sidecar['documents']})}")
        print(f"  total words    : "
              f"{sum(d['expected']['total_words'] for d in sidecar['documents'])}")
        print(f"  skipped        : {sidecar['skipped']}")
        print(f"  size           : {size} bytes")

    finally:

        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
