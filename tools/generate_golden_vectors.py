"""
Generate the golden search vectors.

The vectors are the executable definition of the search contract shared
by the Python engine and the Android ports. They are produced by
running the real Flask application against the golden corpus and
recording complete search responses.

Determinism
-----------
The generator is intentionally free of timestamps and absolute paths so
that running it twice produces byte-identical output.

Normalization
-------------
Every result object returned by ``/api/search`` is recorded verbatim
except for one field:

    ``path``
        The absolute server-side filesystem path of the document
        (e.g. ``/srv/corpus/network_security_notes.txt``). This is
        environment specific, is not part of the search contract, and
        must never be exposed to a client as a document identity.
        It is replaced by ``<dropped>``.

All other fields are recorded exactly as the engine produced them:
``title``, ``snippet``, ``page``, ``highlights``, ``phrase_occurrences``,
``filename_score``, ``content_score``, ``exact_content_match``,
``prefix_similarity``, ``numeric_similarity``,
``lexical_match_relevance``, ``phrase_score``, ``score``,
``match_type``, ``filetype_filter``, ``tag``, ``document_url``,
``open_url``, ``page_url``.

Floating point values are serialised with Python's ``repr`` semantics
via ``json``, which round-trips binary64 exactly.

Usage
-----
    python -m tools.generate_golden_vectors
"""

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

OUTPUT_PATH = REPO_ROOT / "tests" / "golden" / "search_engine_vectors.json"

VECTOR_FORMAT = "search-engine-golden-vectors"
VECTOR_VERSION = 1

# Fields that are not part of the portable search contract.
DROPPED_RESULT_FIELDS = ("path",)

# The query matrix. Each entry is (vector id, query, params).
#
# The matrix is grouped by the ranking dimension it pins.
VECTOR_MATRIX = [
    # --- exact content match ------------------------------------
    ("exact_content", "network", {}),
    ("exact_content_mixed_case", "NETWORK", {}),
    ("exact_content_long_term", "topology", {}),
    ("exact_content_single_document", "ranking", {}),

    # --- prefix match -------------------------------------------
    ("prefix_short", "net", {}),
    ("prefix_medium", "netw", {}),
    ("prefix_very_short_one_char", "n", {}),
    ("prefix_numeric_fragment", "filler5", {}),

    # --- numeric matching ---------------------------------------
    ("numeric_substring", "9901", {}),
    ("numeric_substring_partial", "990", {}),
    ("numeric_filename_token", "BCS502", {}),
    ("numeric_filler_late", "filler57", {}),

    # --- filename matching --------------------------------------
    ("filename_single_token", "probe", {}),
    ("filename_multi_token", "BCS502 Module 2", {}),
    ("filename_with_extension", "BCS502 Module 2.pdf", {}),
    ("filename_quoted_with_extension", "\"BCS502 Module 2.pdf\"", {}),
    ("filename_title_case", "title", {}),
    ("filename_spaces", "Title Case Notes", {}),
    ("filename_underscore_token", "cryptography_basics", {}),

    # --- phrase scoring -----------------------------------------
    ("phrase_two_terms", "distributed ledger", {}),
    ("phrase_quoted", "\"distributed ledger\"", {}),
    ("phrase_three_terms", "append only history", {}),
    ("phrase_no_match_terms", "ledger zebra", {}),
    ("phrase_repeated", "network connects", {}),

    # --- snippet windowing --------------------------------------
    ("snippet_long_document", "beacon", {}),
    ("snippet_common_term", "sharedterm", {"limit": 3}),

    # --- filetype filtering -------------------------------------
    ("filetype_only_txt", "txt", {}),
    ("filetype_only_pdf", "pdf", {}),
    ("filetype_only_docx", "docx", {}),
    ("filetype_only_dotted_pdf", ".pdf", {}),
    ("filetype_with_keyword_txt", "sharedterm txt", {"limit": 50}),
    ("filetype_with_keyword_pdf", "sharedterm pdf", {"limit": 50}),
    ("filetype_with_keyword_docx", "sharedterm docx", {"limit": 50}),
    ("filetype_no_match", "zzzznomatch pdf", {}),

    # --- pagination ---------------------------------------------
    ("pagination_page_1", "sharedterm", {}),
    ("pagination_page_2", "sharedterm", {"page": 2}),
    ("pagination_page_3", "sharedterm", {"page": 3}),
    ("pagination_small_limit_page_1", "sharedterm", {"limit": 5}),
    ("pagination_small_limit_page_3", "sharedterm", {"page": 3, "limit": 5}),
    ("pagination_limit_clamped", "sharedterm", {"limit": 999}),
    ("pagination_page_zero", "sharedterm", {"page": 0}),
    ("pagination_page_negative", "sharedterm", {"page": -4}),
    ("pagination_page_beyond_end", "sharedterm", {"page": 99}),
    ("pagination_page_not_a_number", "sharedterm", {"page": "abc"}),
    ("pagination_limit_not_a_number", "sharedterm", {"limit": "abc"}),

    # --- Unicode tokenization -----------------------------------
    ("unicode_ascii_extension", "caf", {}),
    ("unicode_accented", "café", {}),
    ("unicode_accented_resume", "résumé", {}),
    ("unicode_vulgar_half", "½", {}),
    ("unicode_vulgar_quarter", "¼", {}),
    ("unicode_greek_omega", "Ω", {}),
    ("unicode_greek_sigma", "σ", {}),
    ("unicode_devanagari_digit", "५", {}),
    ("unicode_cjk", "字", {}),
    ("unicode_turkish_dotted_i", "İstanbul", {}),
    ("unicode_turkish_lowered", "istanbul", {}),
    ("unicode_uppercase_query", "CAFÉ", {}),
    ("unicode_nbsp_token", "break", {}),
    ("unicode_figure_space_token", "space", {}),

    # --- query hygiene ------------------------------------------
    ("query_punctuation_trailing", "network,", {}),
    ("query_punctuation_inner", "dis-tri-buted", {}),
    ("query_extra_whitespace", "  network   security  ", {}),
    ("query_quoted_simple", "\"network\"", {}),

    # --- no results ---------------------------------------------
    ("no_results_single", "zzzznomatch", {}),
    ("no_results_multi", "zzzznomatch qqqqnomatch", {}),
    ("no_results_punctuation_only", "!!!", {}),

    # --- blank query (special published shape) ------------------
    ("blank_query_default", "", {}),
    ("blank_query_with_page", "", {"page": 3}),
    ("blank_query_with_limit", "", {"limit": 25}),
    ("blank_query_whitespace", "   ", {}),

    # --- bare-list (legacy) response shape ----------------------
    #
    # The engine publishes a bare JSON list, not a paginated
    # object, on several reachable no-result paths. These are
    # published behaviour and must be preserved by every port.
    ("bare_list_quoted_phrase_no_match", "\"zzzznope\"", {}),
    ("bare_list_quoted_phrase_no_match_multi", "\"zzzz noppe qqqq\"", {}),

    # A filetype filter that removes every candidate still returns a
    # paginated object with total 0, *not* a bare list. This is a
    # trap for ports that assume "no results" always means a list.
    ("filetype_eliminates_all_results", "beacon pdf", {}),
    ("filetype_eliminates_all_results_docx", "beacon docx", {}),
]

# Vectors captured against a completely empty index, before the golden
# corpus is uploaded. This is the Android first-run scenario: the user
# searches before importing anything.
EMPTY_CORPUS_MATRIX = [
    ("empty_blank_query", "", {}),
    ("empty_keyword_query", "network", {}),
    ("empty_filetype_only_query", "pdf", {}),
    ("empty_quoted_phrase", "\"network\"", {}),
    ("empty_prefix_query", "net", {}),
]


def normalize_result(result):
    """Return a portable copy of one search result object."""

    normalized = {}

    for key, value in result.items():
        if key in DROPPED_RESULT_FIELDS:
            continue

        normalized[key] = value

    return normalized


def normalize_response(payload):
    """Return a portable copy of a search response."""

    if isinstance(payload, list):
        # The engine publishes a bare list on some no-result paths.
        return [
            normalize_result(item)
            for item in payload
        ]

    if isinstance(payload, dict):
        normalized = {}

        for key, value in payload.items():
            if key == "results" and isinstance(value, list):
                normalized[key] = [
                    normalize_result(item)
                    for item in value
                ]
            else:
                normalized[key] = value

        return normalized

    return payload


def _capture_vectors(client, matrix, verbose=True):
    """Run a query matrix and return the recorded vectors."""

    vectors = []

    for vector_id, query, params in matrix:
        request_params = dict(params)
        request_params["q"] = query

        response = client.get("/api/search", query_string=request_params)
        payload = response.get_json()

        vectors.append({
            "id": vector_id,
            "query": query,
            "params": _normalize_params(params),
            "status_code": response.status_code,
            "response": normalize_response(payload),
        })

        if verbose:
            shape = "list" if isinstance(payload, list) else "object"
            print(
                f"  vector {vector_id:<42} "
                f"status={response.status_code} shape={shape}"
            )

    return vectors



def generate(verbose=True):
    """Generate the vector document and return it as a dict."""

    from tools.corpus_materialize import materialize_corpus
    from tools.engine_session import (
        isolated_engine,
        upload_corpus,
        wait_for_index_ready,
    )
    from tools.contract_vectors import build_contract_vectors
    from tools.golden_corpus import GOLDEN_CORPUS, corpus_names

    with isolated_engine() as (client, _engine, workspace):
        corpus_dir = os.path.join(workspace, "corpus")

        materialize_corpus(corpus_dir)

        # ------------------------------------------------------------
        # Capture the empty-index vectors first. The application is
        # imported against a fresh directory, so the index is empty
        # until the golden corpus is uploaded.
        # ------------------------------------------------------------

        empty_status = wait_for_index_ready(client, 0)

        if empty_status["documents"] != 0:
            raise RuntimeError(
                "Expected an empty index before upload but found "
                f"{empty_status['documents']} documents"
            )

        empty_corpus_vectors = _capture_vectors(
            client, EMPTY_CORPUS_MATRIX, verbose=verbose
        )

        # ------------------------------------------------------------
        # Populate the corpus and capture the main vectors.
        # ------------------------------------------------------------

        responses = upload_corpus(client, corpus_dir)

        failed = [
            name
            for response in responses
            for name in response.get("failed", [])
        ]

        if failed:
            raise RuntimeError(f"Golden corpus upload failed for: {failed}")

        wait_for_index_ready(client, len(GOLDEN_CORPUS))

        vectors = _capture_vectors(client, VECTOR_MATRIX, verbose=verbose)

        status_payload = wait_for_index_ready(client, len(GOLDEN_CORPUS))

        return {
            "format": VECTOR_FORMAT,
            "version": VECTOR_VERSION,
            "generated_by": "tools/generate_golden_vectors.py",
            "normalization": {
                "dropped_result_fields": list(DROPPED_RESULT_FIELDS),
                "notes": [
                    "Result objects are recorded verbatim except that the "
                    "absolute server filesystem path is removed.",
                    "Floating point values are Python binary64 repr values "
                    "and must be compared as binary64, not as decimals.",
                    "The blank-query response is a special published shape "
                    "with a hardcoded page/limit of 1/10 and no start/end.",
                ],
            },
            "corpus": {
                "document_count": len(GOLDEN_CORPUS),
                "names": corpus_names(),
                "documents": [
                    {
                        "name": item["name"],
                        "text": item["text"],
                    }
                    for item in GOLDEN_CORPUS
                ],
            },
            "engine_index_state": {
                "documents": status_payload["documents"],
                "content_terms": status_payload["content_terms"],
                "filenames_indexed": status_payload["filenames_indexed"],
                "page_text_entries": status_payload["page_text_entries"],
            },
            "vectors": vectors,
            "empty_corpus_vectors": empty_corpus_vectors,
            "contract_vectors": build_contract_vectors(_engine),
        }



def _normalize_params(params):
    """Return the requested params with defaults filled in."""

    return {
        "page": params.get("page"),
        "limit": params.get("limit"),
    }


def total_vector_count(document):
    """Return the total number of vectors in a generated document."""

    return len(document["vectors"]) + len(document["empty_corpus_vectors"])


def load_vectors(path=None):
    """Load a generated vector document from disk."""

    path = path or OUTPUT_PATH

    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def all_vectors(document):
    """
    Return every vector as (corpus_state, vector) pairs.

    ``corpus_state`` is ``"populated"`` or ``"empty"``.
    """

    for vector in document["vectors"]:
        yield "populated", vector

    for vector in document["empty_corpus_vectors"]:
        yield "empty", vector



def main():
    print("Generating golden search vectors")

    document = generate()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    serialized = json.dumps(
        document,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )

    OUTPUT_PATH.write_text(serialized + "\n", encoding="utf-8")

    print()
    print(f"corpus documents      : {document['corpus']['document_count']}")
    print(f"corpus vectors        : {len(document['vectors'])}")
    print(f"empty-corpus vectors  : {len(document['empty_corpus_vectors'])}")
    print(f"total vectors         : {total_vector_count(document)}")
    contract = document["contract_vectors"]
    print(f"content terms         : {document['engine_index_state']['content_terms']}")
    print(f"contract cases        : {sum(len(v) for k, v in contract.items() if isinstance(v, list))}")
    print(f"written to            : {OUTPUT_PATH.relative_to(REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
