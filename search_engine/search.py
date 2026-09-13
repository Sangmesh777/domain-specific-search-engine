"""
Ranking and result assembly.

This is the heart of the product and the one module that must never be
"improved" casually: every constant below is observed behavior that users
and the regression suite depend on.

Query interpretation
--------------------
A raw query is split into a keyword part and an optional filetype filter, so
`network pdf`, `BCS502 Module 2.pdf` and `"BCS502 Module 2.pdf"` all behave
as a user would expect. A quoted query is additionally treated as an exact
phrase.

Matching signals
----------------
Four independent signals are collected per document:

    filename_score   exact filename 100, >=2-word substring 50,
                     quoted phrase 30, per-token 20
    content_score    TF-IDF over the inverted index
    phrase_score     exact adjacent phrase occurrences, log-scaled to 100
    lexical match    the explicit hierarchy below

Content matching has an explicit lexical hierarchy, because users search for
identifiers and partial words as well as whole words:

    1. exact token          "999" matching the term "999"
                            weight 1.00

    2. prefix token         "repl" matching the term "replacement"
                            similarity = len(query) / len(term),
                            clamped to 0.25 .. 0.90

    3. numeric substring    "999" inside the term "bcs999"
                            (digit run >= 2, and not already a prefix hit)
                            similarity = ratio * 0.70,
                            clamped to 0.20 .. 0.65

Only the best match weight is kept per query term, and the hierarchy is
turned into a lexical relevance score:

    exact           1.00
    prefix          0.70 + 0.30 * similarity
    numeric         0.30 + 0.40 * similarity

Those are kept separate from TF-IDF so a document that merely repeats a term
cannot outrank one that matches the user's actual intent.

Final ranking
-------------
The signals are combined with weights chosen by query shape; each set sums
to 1.00:

    quoted phrase + filename signal   0.80 / 0.05 / 0.15 / 0.00
    quoted phrase                     0.05 / 0.15 / 0.80 / 0.00
    filename signal                   0.75 / 0.15 / 0.10 / 0.00
    ordinary topic search             0.05 / 0.60 / 0.20 / 0.15
                                      (filename / content / phrase / lexical)

A filetype-only query (`pdf`) is a browse operation, not a ranking one: it
returns the filtered document list sorted by title at a flat score.

Transport
---------
The function takes an index snapshot plus plain values and returns a
JSON-ready payload. It never touches Flask. Three legacy branches answer
with a bare list instead of a paginated object; that shape is preserved
because it is part of the published API, and each site is marked.

Only two of those three sites are reachable:

    no documents to search      an empty corpus, or a filetype filter for
                                which the corpus holds no documents
    quoted phrase missing       no document contains the phrase adjacently

The third - filetype narrowing emptying `document_scores` - cannot happen.
Scores are pre-populated for every document before matching, and
`total_documents` is counted through the same filetype predicate, so the
first site always fires before narrowing runs. It is kept because removing
dead code is a change, and this refactor changes no behavior; it is marked
so the next reader does not hunt for the query that reaches it.

The `document_url` / `page_url` / `open_url` fields are server resource
paths. They are produced here so both adapters share one result shape: an
online Android client uses them directly, an offline one uses `path`.
"""

import math
import os
from urllib.parse import quote

from search_engine.pagination import (
    empty_query_response,
    paginate,
)
from search_engine.query import (
    normalize_search_query,
    parse_filetype_filter,
)
from search_engine.snippets import (
    count_phrase_occurrences,
    get_snippet_and_page,
)
from search_engine.tokenizer import (
    tokenize_filename,
)


def search_documents(
    snapshot,
    raw_query,
    requested_page=1,
    requested_limit=10,
):
    """
    Rank every document matching a query and paginate the result.

    `snapshot` is one coherent IndexSnapshot captured by the caller, so a
    concurrent mutation cannot tear a result set.

    Returns a JSON-ready payload: normally the paginated object built by
    `paginate`, and in three legacy branches a bare list (see the module
    docstring). Callers serialize; this function does not.
    """

    raw_query = (raw_query or "").strip()

    if not raw_query:
        return empty_query_response()

    requested_result_page = max(
        1,
        requested_page
    )

    limit = min(
        50,
        max(1, requested_limit)
    )

    # The caller captured one coherent active snapshot.
    active_inverted_index = snapshot.inverted_index
    active_document_metadata = snapshot.document_metadata
    active_filename_index = snapshot.filename_index
    active_page_text_index = snapshot.page_text_index

    # Normalize document extensions before tokenization.
    # Examples:
    #   BCS502 Module 2.pdf
    #   "BCS502 Module 2.pdf"
    # become:
    #   BCS502 Module 2
    #   "BCS502 Module 2"
    keyword_query, filetype_filter = parse_filetype_filter(
        raw_query
    )

    normalized_query_text = normalize_search_query(
        keyword_query
    )

    query = normalized_query_text.lower()

    quoted_phrase = None

    if (
        len(query) >= 2
        and query.startswith('"')
        and query.endswith('"')
    ):

        quoted_phrase = query[
            1:-1
        ].strip()

    search_words = tokenize_filename(
        query
    )

    if (
        not search_words
        and
        not filetype_filter
    ):
        return paginate(
            [],
            requested_result_page,
            limit
        )

    # Keep numeric tokens searchable. A query such as "999" must be
    # able to match an indexed token containing 999. The tokenizer already
    # preserves numeric tokens; removing them here made them unreachable
    # from the content index.
    content_query_words = [
        word
        for word in search_words
        if len(word) > 1
    ]

    # --------------------------------------------------------
    # FILE-TYPE-ONLY QUERY
    # --------------------------------------------------------
    #
    # A query such as "pdf" is a filter/browse operation.
    # It must never enter the normal keyword ranking pipeline.
    # --------------------------------------------------------

    if filetype_filter and not search_words:

        filtered_results = []

        for filename, metadata in active_document_metadata.items():

            extension = os.path.splitext(
                filename
            )[1].lower().lstrip(".")

            if extension != filetype_filter:
                continue

            document_url = (
                "/api/documents/"
                +
                quote(
                    filename,
                    safe=""
                )
            )

            filtered_results.append({

                "title":
                    metadata["title"],

                "path":
                    metadata["path"],

                "document_url":
                    document_url,

                "page_url":
                    document_url,

                "open_url":
                    document_url,

                "snippet":
                    (
                        f"Filtered by file type: "
                        f"{filetype_filter.upper()}"
                    ),

                "page":
                    None,

                "highlights":
                    [],

                "filename_score":
                    0.0,

                "content_score":
                    0.0,

                "phrase_score":
                    0.0,

                "phrase_occurrences":
                    0,

                "score":
                    1.0,

                "relevance_score":
                    1.0,

                "match_type":
                    (
                        f"File Type: "
                        f"{filetype_filter.upper()}"
                    ),

                "filetype_filter":
                    filetype_filter,

                "tag":
                    "Filtered Result"

            })

        filtered_results.sort(
            key=lambda item:
                item["title"].lower()
        )

        return paginate(
            filtered_results,
            requested_result_page,
            limit
        )

    normalized_query = " ".join(
        search_words
    )

    if quoted_phrase:

        phrase_words = tokenize_filename(
            quoted_phrase
        )

    else:

        phrase_words = list(
            search_words
        )

    normalized_phrase = " ".join(
        phrase_words
    )

    phrase_query = (
        len(phrase_words) >= 2
    )

    if not search_words:
        phrase_query = False

    if filetype_filter:

        total_documents = sum(
            1
            for filename in active_document_metadata
            if os.path.splitext(filename)[1]
            .lower()
            .lstrip(".")
            == filetype_filter
        )

    else:

        total_documents = len(
            active_document_metadata
        )

    if total_documents == 0:
        # Legacy API shape: this branch answers with a bare list
        # rather than a paginated object. Preserved deliberately;
        # see the module docstring.
        return []

    # --------------------------------------------------------
    # SCORE STORAGE
    # --------------------------------------------------------

    document_scores = {}

    for filename in active_document_metadata:

        document_scores[filename] = {

            "filename_score": 0.0,

            "content_score": 0.0,

            "phrase_score": 0.0,

            "phrase_occurrences": 0,

            "filename_matches": [],

            "content_matches": [],

            "phrase_match": False,

            # Explicit content-match hierarchy.
            "exact_content_match": False,

            "best_prefix_similarity": 0.0,

            "best_numeric_similarity": 0.0

        }

    def filetype_allowed(filename):

        if not filetype_filter:
            return True

        extension = os.path.splitext(
            filename
        )[1].lower().lstrip(".")

        return extension == filetype_filter

    # --------------------------------------------------------
    # FILENAME SEARCH
    # --------------------------------------------------------

    for filename, filename_words in (
        active_filename_index.items()
    ):

        if not filetype_allowed(filename):
            continue

        normalized_filename = " ".join(
            filename_words
        )

        if (
            normalized_query
            ==
            normalized_filename
        ):

            document_scores[
                filename
            ]["filename_score"] += 100.0

        elif (
            len(search_words) >= 2
            and
            normalized_query
            in normalized_filename
        ):

            document_scores[
                filename
            ]["filename_score"] += 50.0

        if (
            quoted_phrase
            and
            normalized_phrase
            and
            normalized_phrase
            in normalized_filename
        ):

            document_scores[
                filename
            ]["phrase_score"] += 30.0

            document_scores[
                filename
            ]["phrase_match"] = True

        for word in search_words:

            if word not in filename_words:
                continue

            if word.isdigit():

                non_numeric_query_words = [
                    query_word
                    for query_word in search_words
                    if not query_word.isdigit()
                ]

                has_related_filename_word = any(
                    query_word in filename_words
                    for query_word
                    in non_numeric_query_words
                )

                if (
                    non_numeric_query_words
                    and
                    not has_related_filename_word
                ):

                    continue

            document_scores[
                filename
            ]["filename_score"] += 20.0

            if word not in document_scores[
                filename
            ]["filename_matches"]:

                document_scores[
                    filename
                ]["filename_matches"].append(
                    word
                )

    # --------------------------------------------------------
    # CONTENT TF-IDF
    # --------------------------------------------------------

    # Content matching levels:
    #
    # 1. Exact token:
    #       999 -> 999
    #
    # 2. Prefix token:
    #       replacementuni -> replacementunique999
    #
    # 3. Numeric substring:
    #       999 -> replacementunique999
    #
    # Numeric substring matching is deliberately explicit because users
    # commonly search for a numeric fragment embedded in an alphanumeric
    # identifier/code. Exact matches remain strongest, prefix matches are
    # next, and numeric-substring matches receive a lower lexical weight.

    indexed_terms = list(
        active_inverted_index.keys()
    )

    for word in content_query_words:

        matching_terms = []

        # ----------------------------------------------------
        # EXACT TOKEN
        # ----------------------------------------------------
        if word in active_inverted_index:

            matching_terms.append(
                (
                    word,
                    1.0
                )
            )

        # ----------------------------------------------------
        # PREFIX TOKEN
        # ----------------------------------------------------
        if len(word) >= 3:

            for term in indexed_terms:

                if term == word:
                    continue

                if not term.startswith(word):
                    continue

                if not term:
                    continue

                prefix_similarity = (
                    len(word)
                    /
                    float(len(term))
                )

                prefix_similarity = min(
                    0.90,
                    max(
                        0.25,
                        prefix_similarity
                    )
                )

                matching_terms.append(
                    (
                        term,
                        prefix_similarity
                    )
                )

        # ----------------------------------------------------
        # NUMERIC SUBSTRING
        # ----------------------------------------------------
        if word.isdigit() and len(word) >= 2:

            for term in indexed_terms:

                if term == word:
                    continue

                if word not in term:
                    continue

                # Prefix matches already have a stronger lexical
                # interpretation, so do not add a weaker duplicate.
                if term.startswith(word):
                    continue

                substring_similarity = (
                    len(word)
                    /
                    float(len(term))
                )

                substring_similarity = min(
                    0.65,
                    max(
                        0.20,
                        substring_similarity * 0.70
                    )
                )

                matching_terms.append(
                    (
                        term,
                        substring_similarity
                    )
                )

        # Keep the strongest matching interpretation for each indexed term.
        best_term_weights = {}

        for (
            term,
            match_weight
        ) in matching_terms:

            current_weight = (
                best_term_weights.get(
                    term,
                    0.0
                )
            )

            if match_weight > current_weight:

                best_term_weights[
                    term
                ] = match_weight

        for (
            term,
            match_weight
        ) in best_term_weights.items():

            docs_containing_word = len(
                active_inverted_index[term]
            )

            if docs_containing_word == 0:
                continue

            idf = math.log(
                (total_documents + 1)
                /
                (docs_containing_word + 1)
            ) + 1

            for (
                filename,
                term_count
            ) in active_inverted_index[term].items():

                if not filetype_allowed(filename):
                    continue

                if filename not in active_document_metadata:
                    continue

                total_words = active_document_metadata[
                    filename
                ]["total_words"]

                if total_words == 0:
                    continue

                tf = (
                    term_count
                    /
                    float(total_words)
                )

                tfidf = (
                    tf
                    *
                    idf
                    *
                    match_weight
                )

                document_scores[
                    filename
                ]["content_score"] += tfidf

                # --------------------------------------------
                # Explicit lexical match hierarchy
                # --------------------------------------------
                #
                # Exact indexed token:
                #     999 -> 999
                #
                # Prefix:
                #     replacementuni -> replacementunique999
                #
                # Numeric substring:
                #     999 -> replacementunique999
                #
                # Keep these signals separate from TF-IDF so the
                # final ranking does not lose lexical intent merely
                # because one document contains a term frequently.
                if term == word:
                    document_scores[
                        filename
                    ]["exact_content_match"] = True

                elif word.isdigit() and word in term:
                    document_scores[
                        filename
                    ]["best_numeric_similarity"] = max(
                        document_scores[
                            filename
                        ]["best_numeric_similarity"],
                        match_weight
                    )

                elif term.startswith(word):
                    document_scores[
                        filename
                    ]["best_prefix_similarity"] = max(
                        document_scores[
                            filename
                        ]["best_prefix_similarity"],
                        match_weight
                    )

                if word not in document_scores[
                    filename
                ]["content_matches"]:

                    document_scores[
                        filename
                    ]["content_matches"].append(
                        word
                    )

# --------------------------------------------------------
    # PHRASE SEARCH
    # --------------------------------------------------------

    if phrase_query:

        for filename in active_document_metadata:

            if not filetype_allowed(filename):
                continue

            pages = active_page_text_index.get(
                filename,
                []
            )

            normalized_page_text = " ".join(
                page["text"]
                for page in pages
            ).lower()

            # ------------------------------------------------
            # Count exact phrase occurrences in document text.
            # Repeated exact phrases receive increasing but
            # capped phrase relevance.
            # ------------------------------------------------

            phrase_occurrences = count_phrase_occurrences(
                normalized_page_text,
                normalized_phrase
            )

            if phrase_occurrences > 0:

                document_scores[
                    filename
                ]["phrase_occurrences"] = (
                    phrase_occurrences
                )

                # Exact phrase frequency uses a logarithmic
                # curve. More exact occurrences increase
                # relevance, but with diminishing returns.
                #
                # Reference points:
                #   1 occurrence  -> 50.0
                #   2 occurrences -> ~57.5
                #   7 occurrences -> ~80.4
                #   16 occurrences -> ~93.1
                #   25 occurrences -> 100.0
                #
                # This is deliberately capped at 100 so phrase
                # repetition cannot overwhelm the other signals.

                max_frequency_reference = 25.0

                if phrase_occurrences <= 1:

                    content_phrase_score = 50.0

                else:

                    frequency_ratio = min(
                        math.log(
                            phrase_occurrences
                        )
                        /
                        math.log(
                            max_frequency_reference
                        ),
                        1.0
                    )

                    content_phrase_score = (
                        50.0
                        +
                        (
                            50.0
                            *
                            frequency_ratio
                        )
                    )

                content_phrase_score = min(
                    content_phrase_score,
                    100.0
                )

                document_scores[
                    filename
                ]["phrase_score"] = max(
                    document_scores[
                        filename
                    ]["phrase_score"],
                    content_phrase_score
                )

                document_scores[
                    filename
                ]["phrase_match"] = True

            filename_words = active_filename_index.get(
                filename,
                []
            )

            normalized_filename = " ".join(
                filename_words
            )

            # Exact phrase inside the filename is a separate,
            # stronger filename signal.
            if (
                normalized_phrase
                and
                normalized_phrase
                in normalized_filename
            ):

                document_scores[
                    filename
                ]["phrase_score"] += 100.0

                document_scores[
                    filename
                ]["phrase_match"] = True

    # --------------------------------------------------------
    # Explicit quoted phrase filter
    # --------------------------------------------------------

    if quoted_phrase:

        phrase_documents = {
            filename
            for filename, scores
            in document_scores.items()
            if scores["phrase_match"]
        }

        if phrase_documents:

            for filename in list(
                document_scores.keys()
            ):

                if filename not in phrase_documents:

                    document_scores.pop(
                        filename
                    )

        else:

            # Legacy API shape: this branch answers with a bare list
            # rather than a paginated object. Preserved deliberately;
            # see the module docstring.
            return []

    # --------------------------------------------------------
    # Strongest content score
    # --------------------------------------------------------

    max_content_score = max(

        (
            scores["content_score"]
            for scores
            in document_scores.values()
        ),

        default=0.0
    )

    query_has_filename_signal = any(

        scores["filename_score"] > 0

        for scores
        in document_scores.values()

    )

    # --------------------------------------------------------
    # FINAL RANKING
    # --------------------------------------------------------

    if filetype_filter:

        document_scores = {
            filename: scores
            for filename, scores
            in document_scores.items()
            if filetype_allowed(filename)
        }

        # UNREACHABLE. document_scores was pre-populated with every
        # indexed document, and total_documents above counted the same
        # filetype predicate, so a filter that would empty this dict has
        # already returned at the "no documents to search" site. Kept as
        # found: this refactor moves behavior, it does not edit it.
        if not document_scores:
            # Legacy API shape: this branch answers with a bare list
            # rather than a paginated object. Preserved deliberately;
            # see the module docstring.
            return []

        max_content_score = max(
            (
                scores["content_score"]
                for scores
                in document_scores.values()
            ),
            default=0.0
        )

    ranked_documents = []

    for filename, scores in (
        document_scores.items()
    ):

        filename_score = scores[
            "filename_score"
        ]

        content_score = scores[
            "content_score"
        ]

        phrase_score = scores[
            "phrase_score"
        ]

        exact_content_match = bool(
            scores.get(
                "exact_content_match",
                False
            )
        )

        best_prefix_similarity = float(
            scores.get(
                "best_prefix_similarity",
                0.0
            )
        )

        best_numeric_similarity = float(
            scores.get(
                "best_numeric_similarity",
                0.0
            )
        )

        # Explicit lexical hierarchy:
        #
        # 1. exact content token
        # 2. strongest prefix completion
        # 3. numeric substring
        # 4. general TF-IDF
        #
        # The lexical signal is deliberately bounded so it improves
        # ranking without allowing a weak lexical match to overwhelm
        # filename/phrase relevance.
        if exact_content_match:
            lexical_match_relevance = 1.00
        elif best_prefix_similarity > 0:
            lexical_match_relevance = (
                0.70
                +
                (0.30 * best_prefix_similarity)
            )
        elif best_numeric_similarity > 0:
            lexical_match_relevance = (
                0.30
                +
                (0.40 * best_numeric_similarity)
            )
        else:
            lexical_match_relevance = 0.0

        filename_words = active_filename_index.get(
            filename,
            []
        )

        normalized_filename = " ".join(
            filename_words
        )

        matched_word_count = sum(
            1
            for word in search_words
            if word in filename_words
        )

        if search_words:

            query_word_coverage = (
                matched_word_count
                /
                len(search_words)
            )

        else:

            query_word_coverage = 0.0

        if (
            normalized_query
            ==
            normalized_filename
        ):

            filename_relevance = 1.0

        elif (
            len(search_words) >= 2
            and
            normalized_query
            in normalized_filename
        ):

            filename_relevance = 0.95

        elif query_word_coverage > 0:

            filename_relevance = (
                0.75
                *
                query_word_coverage
            )

        else:

            filename_relevance = 0.0

        if max_content_score > 0:

            content_relevance = (
                content_score
                /
                max_content_score
            )

        else:

            content_relevance = 0.0

        phrase_relevance = min(
            phrase_score / 100.0,
            1.0
        )

        if (
            filetype_filter
            and
            not search_words
        ):

            # Type-only query: this is a filtered document list.
            final_score = 0.75

        else:

            if quoted_phrase and query_has_filename_signal:

                # Exact quoted document-title search.
                # All weights sum to 1.00.
                filename_weight = 0.80
                content_weight = 0.05
                phrase_weight = 0.15
                lexical_weight = 0.0

            elif quoted_phrase:

                # Exact quoted content phrase.
                # All weights sum to 1.00.
                filename_weight = 0.05
                content_weight = 0.15
                phrase_weight = 0.80
                lexical_weight = 0.0

            elif query_has_filename_signal:

                # Filename/title-oriented search.
                # All weights sum to 1.00.
                filename_weight = 0.75
                content_weight = 0.15
                phrase_weight = 0.10
                lexical_weight = 0.0

            else:

                # Normal topic/content search.
                # Preserve the original ranking signals while giving
                # explicit lexical intent its own bounded contribution.
                filename_weight = 0.05
                content_weight = 0.60
                phrase_weight = 0.20
                lexical_weight = 0.15

            final_score = (

                (
                    filename_relevance
                    *
                    filename_weight
                )

                +

                (
                    content_relevance
                    *
                    content_weight
                )

                +

                (
                    phrase_relevance
                    *
                    phrase_weight
                )

                +

                (
                    lexical_match_relevance
                    *
                    lexical_weight
                )
            )

        if (
            filename_score == 0
            and
            content_score == 0
            and
            phrase_score == 0
        ):
            continue

        if final_score <= 0:
            continue

        ranked_documents.append({

            "filename":
                filename,

            "filename_score":
                filename_score,

            "content_score":
                content_score,

            "phrase_score":
                phrase_score,

            "phrase_occurrences":
                scores[
                    "phrase_occurrences"
                ],

            "final_score":
                final_score,

            "filename_matches":
                scores[
                    "filename_matches"
                ],

            "content_matches":
                scores[
                    "content_matches"
                ],

            "phrase_match":
                scores[
                    "phrase_match"
                ],

            "exact_content_match":
                exact_content_match,

            "prefix_similarity":
                best_prefix_similarity,

            "numeric_similarity":
                best_numeric_similarity,

            "lexical_match_relevance":
                lexical_match_relevance

        })

    if (
        filetype_filter
        and
        not search_words
    ):

        ranked_documents.sort(
            key=lambda item:
                item["filename"].lower()
        )

    else:

        ranked_documents.sort(
            key=lambda item:
                item["final_score"],
            reverse=True
        )

    # --------------------------------------------------------
    # BUILD RESPONSE
    # --------------------------------------------------------

    results = []

    for item in ranked_documents:

        filename = item[
            "filename"
        ]

        metadata = active_document_metadata[
            filename
        ]

        filename_matches = item[
            "filename_matches"
        ]

        content_matches = item[
            "content_matches"
        ]

        phrase_match = item[
            "phrase_match"
        ]

        if (
            filetype_filter
            and
            not search_words
        ):

            match_type = (
                f"File Type: "
                f"{filetype_filter.upper()}"
            )

        elif (
            filename_matches
            and
            phrase_match
        ):

            match_type = (
                "Filename + Phrase"
            )

        elif phrase_match:

            match_type = "Phrase Match"

        elif (
            filename_matches
            and
            content_matches
        ):

            match_type = (
                "Filename + Content"
            )

        elif filename_matches:

            match_type = "Filename Match"

        else:

            match_type = "Content Match"

        if (
            filetype_filter
            and
            not search_words
        ):

            snippet = (
                f"Filtered by file type: "
                f"{filetype_filter.upper()}"
            )

            document_page = None
            highlights = []

        elif content_matches:

            snippet_words = (
                phrase_words
                if phrase_match
                else content_matches
            )

            snippet_info = get_snippet_and_page(
                active_page_text_index,
                filename,
                snippet_words
            )

            snippet = snippet_info[
                "snippet"
            ]

            document_page = snippet_info[
                "page"
            ]

            highlights = snippet_info[
                "highlights"
            ]

        else:

            snippet = (
                "Filename matched: "
                +
                ", ".join(
                    filename_matches
                )
            )

            document_page = None
            highlights = []

        document_url = (
            "/api/documents/"
            + quote(
                filename,
                safe=""
            )
        )

        if document_page is not None:
            page_url = (
                document_url
                + f"#page={document_page}"
            )
        else:
            page_url = document_url

        results.append({

            "title":
                metadata["title"],

            "path":
                metadata["path"],

            "document_url":
                document_url,

            "page_url":
                page_url,

            "snippet":
                snippet,

            "page":
                document_page,

            "highlights":
                highlights,

            "phrase_occurrences":
                item["phrase_occurrences"],

            "filename_score":
                round(
                    item["filename_score"],
                    4
                ),

            "content_score":
                round(
                    item["content_score"],
                    4
                ),

            "exact_content_match":
                item.get(
                    "exact_content_match",
                    False
                ),

            "prefix_similarity":
                round(
                    item.get(
                        "prefix_similarity",
                        0.0
                    ),
                    4
                ),

            "numeric_similarity":
                round(
                    item.get(
                        "numeric_similarity",
                        0.0
                    ),
                    4
                ),

            "lexical_match_relevance":
                round(
                    item.get(
                        "lexical_match_relevance",
                        0.0
                    ),
                    4
                ),

            "phrase_score":
                round(
                    item["phrase_score"],
                    4
                ),

            "score":
                round(
                    item["final_score"],
                    4
                ),

            "match_type":
                match_type,

            "filetype_filter":
                filetype_filter,

            "tag":
                "Ranked Result",

            "open_url":
                page_url

        })

    return paginate(
        results,
        requested_result_page,
        limit
    )
