"""
Build the Android corpus sidecar.

The sidecar lets the Android application bootstrap the shipped corpus
without running PyPDF2 or python-docx on the device. It carries, for
every document, exactly the artefacts the Python engine produced:

  * the extracted, lowercased per-page text
  * the ordered filename tokens
  * the content term frequencies (term -> occurrences)

Because it records the *post-tokenisation* state, the sidecar lets the
Android harness prove that its tokenizer port reproduces the Python
index exactly, instead of merely assuming it.

Usage
-----
    python -m tools.build_corpus_sidecar
"""

import hashlib
import json
import os
from pathlib import Path

from tools.engine_session import (
    isolated_engine,
    wait_for_index_ready,
)
from tools.golden_corpus import GOLDEN_CORPUS

REPO_ROOT = Path(__file__).resolve().parent.parent

OUTPUT_PATH = REPO_ROOT / "artifacts" / "android" / "corpus_sidecar.json"

SIDECAR_FORMAT = "corpus-sidecar"
SIDECAR_VERSION = 1


def _extension_of(name):
    return os.path.splitext(name)[1].lower().lstrip(".")


def build_sidecar(verbose=True):
    """Return the sidecar document as a dict."""

    with isolated_engine(populate_corpus=True) as (client, engine, _workspace):
        wait_for_index_ready(client, len(GOLDEN_CORPUS))

        status = client.get("/api/status").get_json()

        documents = []

        for definition in GOLDEN_CORPUS:
            source_name = definition["name"]

            # The engine stores the sanitized (secure_filename) name,
            # which can differ from the uploaded name.
            stored_name = None

            for candidate in engine.DOCUMENT_METADATA:
                if candidate.lower() == source_name.lower().replace(" ", "_"):
                    stored_name = candidate
                    break

            if stored_name is None:
                # Fall back to matching on the tokenised filename.
                for candidate in engine.DOCUMENT_METADATA:
                    if candidate.replace("_", " ").lower() == source_name.lower():
                        stored_name = candidate
                        break

            if stored_name is None:
                available = ", ".join(sorted(engine.DOCUMENT_METADATA))
                raise RuntimeError(
                    f"Could not locate stored document for {source_name!r}. "
                    f"Indexed documents: {available}"
                )

            metadata = engine.DOCUMENT_METADATA[stored_name]
            filename_terms = engine.FILENAME_INDEX.get(stored_name, [])
            pages = engine.PAGE_TEXT_INDEX.get(stored_name, [])

            content_terms = {}

            for term, postings in engine.REAL_INVERTED_INDEX.items():
                frequency = postings.get(stored_name)

                if frequency:
                    content_terms[term] = frequency

            documents.append({
                "name": stored_name,
                "source_name": source_name,
                "title": metadata["title"],
                "extension": _extension_of(stored_name),
                "total_words": metadata["total_words"],
                "page_count": metadata["page_count"],
                "filename_terms": filename_terms,
                "pages": pages,
                "content_terms": dict(sorted(content_terms.items())),
            })

            if verbose:
                print(
                    f"  {stored_name:<28} "
                    f"pages={len(pages)} "
                    f"filename_terms={len(filename_terms)} "
                    f"content_terms={len(content_terms)}"
                )

        corpus_totals = {
            "documents": status["documents"],
            "content_terms": status["content_terms"],
            "filenames_indexed": status["filenames_indexed"],
            "page_text_entries": status["page_text_entries"],
        }

        sidecar = {
            "format": SIDECAR_FORMAT,
            "version": SIDECAR_VERSION,
            "generated_by": "tools/build_corpus_sidecar.py",
            "tokenizer_contract": {
                "content_tokenize": (
                    "lower() the whole string, replace every character that "
                    "is not (isalnum() or isspace()) with a single space, "
                    "split on whitespace, drop tokens whose length is < 2."
                ),
                "filename_tokenize": (
                    "identical, but keep every non-empty token including "
                    "single characters."
                ),
                "notes": [
                    "Python str.lower() applies full Unicode lowercase and "
                    "may change string length (U+0130 -> U+0069 U+0307).",
                    "Python str.isalnum() is true for non-decimal numerics "
                    "such as U+00BD and U+00B2, which Java's "
                    "Character.isLetterOrDigit rejects.",
                    "Python whitespace for str.split() includes U+001C-U+001F, "
                    "which Java's Character.isWhitespace rejects, while Java "
                    "excludes U+00A0, which Python accepts.",
                ],
            },
            "corpus_totals": corpus_totals,
            "documents": documents,
        }

        sidecar["integrity"] = {
            "algorithm": "sha256",
            "canonical_json_sha256": _canonical_sha256(sidecar),
        }

        return sidecar


def _canonical_sha256(document):
    """Hash a document with the integrity block removed."""

    payload = {key: value for key, value in document.items() if key != "integrity"}

    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def main():
    print("Building Android corpus sidecar")
    print()

    sidecar = build_sidecar()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    OUTPUT_PATH.write_text(
        json.dumps(sidecar, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print()
    print(f"documents        : {sidecar['corpus_totals']['documents']}")
    print(f"content terms    : {sidecar['corpus_totals']['content_terms']}")
    print(f"sha256           : {sidecar['integrity']['canonical_json_sha256']}")
    print(f"written to       : {OUTPUT_PATH.relative_to(REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
