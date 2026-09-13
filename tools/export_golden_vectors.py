"""
Export golden vectors: the Python engine's answers, as a portable contract.

An Android backend - or any other reimplementation - is equivalent only if
it produces these payloads for these inputs. The vectors are generated from
the real engine and never written by hand, so they cannot drift into
describing behavior the core does not have.

    .venv/bin/python tools/export_golden_vectors.py

tests/test_golden_vectors.py regenerates them and compares against the
committed file, so a change in ranking shows up as a test failure that a
human must accept deliberately.

What is and is not covered
--------------------------
Covered: indexing and ranking. The corpus is recorded as already-extracted
page text, so a reimplementation builds its index from those strings and
must reproduce every payload exactly. Nothing about this file depends on
Python's PDF or DOCX libraries.

Deliberately not covered: text extraction. Turning a PDF or DOCX into page
text is parser-dependent, and Android will not use PyPDF2 or python-docx.
Folding extraction in would make the vectors impossible to satisfy, so it
is a separate contract to be verified with real documents on a device.

Portability details
-------------------
`path` fields are absolute, so the generating data folder is replaced with
the placeholder `{{data_folder}}`. A consumer substitutes its own location,
or compares basenames.

Floats are written exactly as Python renders them, which is the shortest
representation that round-trips. That is intentional: scores are rounded to
4 decimal places by Python's round(), which is half-to-even on the binary
value. A reimplementation using BigDecimal HALF_EVEN on a decimal literal
will differ in the last digit on some values, and these vectors are meant
to expose that rather than hide it.
"""

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

from pdf_writer import write_pdf                      # noqa: E402

from search_engine.config import EngineConfig         # noqa: E402
from search_engine.engine import SearchEngine         # noqa: E402


SCHEMA_VERSION = 1

OUTPUT = os.path.join(
    BASE_DIR,
    "tests",
    "golden",
    "search_engine_vectors.json",
)

PLACEHOLDER = "{{data_folder}}"


# ------------------------------------------------------------
# THE CORPUS
# ------------------------------------------------------------
#
# Each document exists to reach a specific part of the ranking pipeline.
# Page text is recorded verbatim into the vectors, so these strings - not
# the files - are the contract.

CORPUS = [
    {
        "filename": "Network Notes.pdf",
        "kind": "pdf",
        "purpose": (
            "an exact numeric term (999) and a numeric run buried inside a "
            "longer token (unique777marker), which exercise the exact and "
            "numeric-substring branches of the lexical hierarchy"
        ),
        "pages": [
            "Computer network fundamentals and the OSI model",
            "Routing protocols such as BGP and OSPF reference 999",
            "The token unique777marker hides a numeric run",
        ],
    },
    {
        "filename": "Network Security Guide.pdf",
        "kind": "pdf",
        "purpose": (
            "a multi-word filename that matches a substring query, plus an "
            "adjacent 'alpha beta' pair for phrase matching"
        ),
        "pages": [
            "Firewalls filter traffic between network zones",
            "alpha beta appear adjacent in this document",
        ],
    },
    {
        "filename": "Database Guide.docx",
        "kind": "docx",
        "purpose": (
            "contains 'alpha' and 'beta' but NOT adjacent, so a quoted "
            "phrase query must exclude it while an unquoted one keeps it"
        ),
        "pages": [
            "Normalization removes redundancy from relations",
            "alpha appears here and beta appears much later",
        ],
    },
    {
        "filename": "machine learning.txt",
        "kind": "txt",
        "purpose": (
            "a phrase repeated several times that is also the document's "
            "own filename, so phrase scoring and the filename phrase bonus "
            "combine"
        ),
        "pages": [
            "machine learning models and machine learning datasets\n"
            "machine learning studies learning systems\n",
        ],
    },
    {
        "filename": "alpha beta gamma delta epsilon.txt",
        "kind": "txt",
        "purpose": (
            "a five-word filename, so filename word order carries real "
            "weight and a permuted order would be visible"
        ),
        "pages": [
            "ordering body text that is otherwise unremarkable\n",
        ],
    },
    {
        "filename": "Syllabus Overview.txt",
        "kind": "txt",
        "purpose": (
            "a document that shares the common word 'the' with several "
            "others, producing equal scores to exercise tie ordering"
        ),
        "pages": [
            "the syllabus covers the semester plan\n",
        ],
    },
]


# ------------------------------------------------------------
# THE QUERIES
# ------------------------------------------------------------
#
# Named by the branch each one is meant to reach, so a consumer that fails
# a case knows which rule it got wrong.

CASES = [
    # blank and whitespace
    ("blank query", "", 1, 10),
    ("whitespace query", "   ", 1, 10),

    # filetype browse
    ("filetype only pdf", "pdf", 1, 10),
    ("filetype only txt", "txt", 1, 10),
    ("filetype only docx", "docx", 1, 10),
    ("filetype only with leading dot", ".pdf", 1, 10),

    # bare-list branch: a quoted phrase nothing contains
    ("quoted phrase matching nothing", '"zzzqqq wwwww"', 1, 10),

    # A filetype filter whose keywords match nothing is NOT a bare list:
    # scores are pre-populated for every document, so narrowing by a type
    # the corpus does contain cannot empty them. Recorded because the
    # asymmetry with the case above is otherwise invisible. The remaining
    # bare-list branch - no documents of the filtered type at all - needs a
    # corpus missing that type, so it is covered by
    # tests/test_search_engine_core.py rather than here.
    ("filetype plus keyword that matches nothing", "zzzqqq pdf", 1, 10),

    # filename bonuses
    ("exact filename", "Network Security Guide.pdf", 1, 10),
    ("filename substring two words", "Network Security", 1, 10),
    ("filename substring reversed", "Security Network", 1, 10),
    ("five word filename exact", "alpha beta gamma delta epsilon.txt", 1, 10),
    ("five word filename partial", "alpha beta gamma", 1, 10),
    ("five word filename tail", "delta epsilon", 1, 10),
    ("five word filename non contiguous", "alpha epsilon", 1, 10),

    # quoted phrases
    ("quoted phrase adjacent", '"alpha beta"', 1, 10),
    ("unquoted same words", "alpha beta", 1, 10),
    ("quoted phrase repeated", '"machine learning"', 1, 10),
    ("quoted filename", '"Network Security Guide.pdf"', 1, 10),

    # Case folding and word splitting, which a reimplementation tends to get
    # subtly wrong by reaching for a locale-sensitive lowercase or a
    # different punctuation rule.
    ("uppercase query", "NETWORK", 1, 10),
    ("mixed case filename query", "network security GUIDE.pdf", 1, 10),
    ("hyphenated query", "network-security", 1, 10),
    ("collapsed internal spaces", "network    security", 1, 10),
    ("trailing punctuation", "network.", 1, 10),

    # A numeric word beside a text word. Numeric tokens are skipped for
    # per-word filename scoring unless a non-numeric word also matches.
    ("numeric word with a text word", "reference 999", 1, 10),

    # content and the lexical hierarchy
    ("content only term", "normalization", 1, 10),
    ("common term", "the", 1, 10),
    ("prefix three characters", "netwo", 1, 10),
    ("prefix longer", "normaliza", 1, 10),
    ("exact numeric term", "999", 1, 10),
    ("numeric substring", "777", 1, 10),
    ("single digit", "2", 1, 10),

    # filetype combined with keywords
    ("filetype plus keyword", "network pdf", 1, 10),
    ("filetype excludes matches", "normalization txt", 1, 10),

    # no match at all
    ("nothing matches", "zzzqqq", 1, 10),

    # pagination policy
    ("page one limit one", "the", 1, 1),
    ("page two limit one", "the", 2, 1),
    ("page three limit one", "the", 3, 1),
    ("limit zero clamps to one", "the", 1, 0),
    ("negative limit clamps to one", "the", 1, -5),
    ("limit above maximum clamps", "the", 1, 9999),
    ("page zero clamps to one", "network", 0, 10),
    ("negative page clamps to one", "network", -5, 10),
    ("page beyond the end clamps", "network", 999, 1),
    ("default paging", "network", 1, 10),
]


def build_corpus_files(directory):
    """Materialize the corpus as real files, so import is exercised."""

    os.makedirs(directory, exist_ok=True)

    paths = []

    for document in CORPUS:

        name = document["filename"]
        path = os.path.join(directory, name)

        if document["kind"] == "pdf":
            write_pdf(path, document["pages"])

        elif document["kind"] == "docx":

            import docx

            handle = docx.Document()

            for paragraph in document["pages"]:
                handle.add_paragraph(paragraph)

            handle.save(path)

        else:
            with open(path, "w", encoding="utf-8") as file:
                file.write(document["pages"][0])

        paths.append(path)

    return paths


def replace_folder(value, folder):
    """Make absolute paths portable."""

    if isinstance(value, dict):
        return {
            key: replace_folder(item, folder)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [replace_folder(item, folder) for item in value]

    if isinstance(value, str) and folder in value:
        return value.replace(folder, PLACEHOLDER)

    return value


def build_vectors():
    """
    Generate the vector document from a live engine.

    Deterministic: a temporary data folder is used, every absolute path is
    replaced with a placeholder, and keys are sorted on serialization.
    """

    temporary = tempfile.mkdtemp()

    try:

        data_folder = os.path.abspath(
            os.path.join(temporary, "data")
        )

        corpus_folder = os.path.join(temporary, "corpus")

        paths = build_corpus_files(corpus_folder)

        engine = SearchEngine(
            EngineConfig(data_folder=data_folder)
        )

        handles = []
        items = []

        for path in paths:

            handle = open(path, "rb")
            handles.append(handle)
            items.append((os.path.basename(path), handle))

        imported = engine.import_documents(
            items,
            log=lambda *args: None,
        )

        for handle in handles:
            handle.close()

        if imported.failed or imported.rejected:
            raise RuntimeError(
                f"corpus did not import cleanly: "
                f"failed={imported.failed} rejected={imported.rejected}"
            )

        # Rebuild so the recorded index reflects the folder-enumeration
        # order a consumer will reproduce, not the import order.
        engine.rebuild(log=lambda *args: None)

        snapshot = engine.snapshot()

        index = {
            "documents": {
                filename: snapshot.document_metadata[filename]
                for filename in sorted(snapshot.document_metadata)
            },
            "filename_words": {
                filename: snapshot.filename_index[filename]
                for filename in sorted(snapshot.filename_index)
            },
            "page_text": {
                filename: snapshot.page_text_index[filename]
                for filename in sorted(snapshot.page_text_index)
            },
            "term_postings": {
                term: {
                    filename: count
                    for filename, count in sorted(postings.items())
                }
                for term, postings in sorted(
                    snapshot.inverted_index.items()
                )
            },
        }

        cases = []

        for name, query, page, limit in CASES:

            expected = engine.search(
                query,
                requested_page=page,
                requested_limit=limit,
            )

            cases.append({
                "name": name,
                "query": query,
                "requested_page": page,
                "requested_limit": limit,
                "expected": replace_folder(expected, data_folder),
            })

        vectors = {
            "schema_version": SCHEMA_VERSION,
            "description": (
                "Indexing and ranking contract for the VTU search engine "
                "core. Generated by tools/export_golden_vectors.py from the "
                "engine itself. Build an index from corpus.pages using the "
                "recorded tokenizer rules, then every case must reproduce "
                "its expected payload exactly."
            ),
            "normative_source": (
                "search_engine/search.py is the specification. Its module "
                "docstring states the matching signals, the lexical "
                "hierarchy and the weight sets. These vectors verify a "
                "reimplementation against that behavior; they do not "
                "restate it, so the two cannot disagree."
            ),
            "not_covered": (
                "Text extraction from PDF and DOCX. The corpus below is "
                "recorded as extracted page text, because Android will not "
                "use PyPDF2 or python-docx. Extraction equivalence must be "
                "verified separately against real documents on a device."
            ),
            "portability": {
                "data_folder_placeholder": PLACEHOLDER,
                "paths": (
                    "Every absolute path is written with the placeholder "
                    "above. Substitute the local data folder before "
                    "comparing."
                ),
                "floats": (
                    "Written exactly as Python renders them. Scores are "
                    "round(x, 4), which is half-to-even on the binary "
                    "value; BigDecimal HALF_EVEN on a decimal literal "
                    "differs on some inputs."
                ),
            },
            "tokenizer": {
                "content": (
                    "Lowercase the text, replace every character that is "
                    "neither alphanumeric nor whitespace with a space, "
                    "split on whitespace, and drop tokens shorter than 2 "
                    "characters."
                ),
                "filename_and_query": (
                    "The same, but keep single-character tokens so that "
                    "numeric words such as 1, 2 and 999 stay searchable."
                ),
                "warning": (
                    "'alphanumeric' and 'whitespace' are the Unicode-aware "
                    "Python predicates str.isalnum and str.isspace, not an "
                    "ASCII-only test. A reimplementation must match that, "
                    "or non-ASCII corpora will index differently."
                ),
            },
            "corpus": [
                {
                    "filename": document["filename"],
                    "stored_as": None,
                    "purpose": document["purpose"],
                    "pages": document["pages"],
                }
                for document in CORPUS
            ],
            "index": replace_folder(index, data_folder),
            "cases": cases,
        }

        # Record the name each file was stored under, since import
        # sanitizes spaces to underscores and a consumer must expect that.
        stored = {
            document["filename"]: None
            for document in CORPUS
        }

        for filename in snapshot.document_metadata:

            for document in CORPUS:

                if document["filename"].replace(" ", "_") == filename:
                    stored[document["filename"]] = filename

        for entry in vectors["corpus"]:
            entry["stored_as"] = stored[entry["filename"]]

        return vectors

    finally:

        shutil.rmtree(temporary, ignore_errors=True)


def render(vectors):
    """Serialize deterministically."""

    return json.dumps(
        vectors,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ) + "\n"


def main(argv=None):

    vectors = build_vectors()

    os.makedirs(
        os.path.dirname(OUTPUT),
        exist_ok=True,
    )

    with open(OUTPUT, "w", encoding="utf-8") as file:
        file.write(render(vectors))

    size = os.path.getsize(OUTPUT)

    print(f"wrote {OUTPUT}")
    print(f"  schema_version : {SCHEMA_VERSION}")
    print(f"  documents      : {len(vectors['corpus'])}")
    print(f"  terms          : {len(vectors['index']['term_postings'])}")
    print(f"  cases          : {len(vectors['cases'])}")
    print(f"  size           : {size} bytes")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
