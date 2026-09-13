"""
Tests for the parity tool's delta classifier.

tools/verify_http_parity.py is a safety net, and since the deterministic
filename-order fix it has had to tell two kinds of difference apart: an
expected consequence of the fix, and a regression. A classifier that waves
both through is worse than no classifier at all, because it makes a green
run look like proof.

These tests drive compare_search() with synthesized payloads, so each
decision is checked in both directions without needing two live servers.
"""

import copy

from verify_http_parity import (
    compare_search,
)


def payload(**overrides):
    """A minimal two-result search payload, shaped like the real one."""

    base = {
        "results": [
            {
                "title": "Alpha.pdf",
                "path": "Alpha.pdf",
                "snippet": "the alpha document",
                "page": 1,
                "highlights": [{"start": 0, "end": 5}],
                "filename_score": 100.0,
                "content_score": 0.5,
                "phrase_score": 0.0,
                "phrase_occurrences": 0,
                "exact_content_match": True,
                "prefix_similarity": 0.0,
                "numeric_similarity": 0.0,
                "lexical_match_relevance": 1.0,
                "score": 0.9,
                "match_type": "Filename Match",
                "filetype_filter": None,
                "tag": "Ranked Result",
                "document_url": "/api/documents/Alpha.pdf",
                "page_url": "/api/documents/Alpha.pdf#page=1",
                "open_url": "/api/documents/Alpha.pdf#page=1",
            },
            {
                "title": "Beta.txt",
                "path": "Beta.txt",
                "snippet": "the beta document",
                "page": None,
                "highlights": [],
                "filename_score": 0.0,
                "content_score": 0.25,
                "phrase_score": 0.0,
                "phrase_occurrences": 0,
                "exact_content_match": True,
                "prefix_similarity": 0.0,
                "numeric_similarity": 0.0,
                "lexical_match_relevance": 1.0,
                "score": 0.4,
                "match_type": "Content Match",
                "filetype_filter": None,
                "tag": "Ranked Result",
                "document_url": "/api/documents/Beta.txt",
                "page_url": "/api/documents/Beta.txt",
                "open_url": "/api/documents/Beta.txt",
            },
        ],
        "pagination": {
            "page": 1,
            "limit": 10,
            "total": 2,
            "total_pages": 1,
            "has_next": False,
            "has_previous": False,
            "start": 1,
            "end": 2,
        },
    }

    base.update(overrides)

    return base


def check(old, new, query=None):
    """Run the classifier the way the tool does: as (status, payload)."""

    return compare_search(
        "test",
        (200, old),
        (200, new),
        query=query,
    )


def phrase_payload():
    """
    A page object whose every result is filename-derived.

    This is the shape a quoted filename query produces when it matches:
    both a filename score and a phrase score on every result. It is what
    the shape rule requires, because only the filename path can be flipped
    by word order.
    """

    return payload(results=[
        {
            "title": "BCS502_Module_2.pdf",
            "path": "BCS502_Module_2.pdf",
            "snippet": "bcs502 computer network technologies module 2",
            "page": 1,
            "highlights": [{"start": 0, "end": 6}],
            "filename_score": 160.0,
            "content_score": 0.0703,
            "phrase_score": 130.0,
            "phrase_occurrences": 0,
            "exact_content_match": True,
            "prefix_similarity": 0.0,
            "numeric_similarity": 0.0,
            "lexical_match_relevance": 1.0,
            "score": 1.0,
            "match_type": "Filename + Phrase",
            "filetype_filter": "pdf",
            "tag": "Ranked Result",
            "document_url": "/api/documents/BCS502_Module_2.pdf",
            "page_url": "/api/documents/BCS502_Module_2.pdf#page=1",
            "open_url": "/api/documents/BCS502_Module_2.pdf#page=1",
        },
    ])


def test_identical_payloads_pass():

    assert check(payload(), payload()) == 0


def test_filename_derived_delta_is_expected():
    """
    The fix restores or removes filename bonuses, so filename_score,
    phrase_score, score and match_type may move together.

    This is the case the classifier must NOT fail, and it is the only case
    it must not fail.
    """

    new = payload()

    new["results"][0]["filename_score"] = 160.0
    new["results"][0]["phrase_score"] = 150.0
    new["results"][0]["score"] = 1.0
    new["results"][0]["match_type"] = "Filename + Phrase"

    assert check(payload(), new) == 0


def test_content_score_delta_is_a_regression():
    """
    Content scoring has nothing to do with filename word order.

    A TF-IDF change is exactly the kind of silent ranking rewrite the tool
    exists to catch, so it must fail even though only one number moved.
    """

    new = payload()

    new["results"][1]["content_score"] = 0.26

    assert check(payload(), new) == 1


def test_snippet_delta_is_a_regression():

    new = payload()

    new["results"][0]["snippet"] = "a different snippet"

    assert check(payload(), new) == 1


def test_highlight_delta_is_a_regression():

    new = payload()

    new["results"][0]["highlights"] = [{"start": 1, "end": 6}]

    assert check(payload(), new) == 1


def test_page_delta_is_a_regression():

    new = payload()

    new["results"][0]["page"] = 2

    assert check(payload(), new) == 1


def test_lexical_similarity_delta_is_a_regression():
    """
    Prefix and numeric similarity feed the lexical hierarchy, not filename
    matching, so they are outside the approved delta.
    """

    new = payload()

    new["results"][1]["prefix_similarity"] = 0.5

    assert check(payload(), new) == 1


def test_a_document_disappearing_is_a_regression():
    """
    A filename bonus can lift a score, but it must not change which
    documents are returned. Membership is compared as a set, so this fails
    whichever direction the document moves.
    """

    new = payload()

    new["results"] = new["results"][:1]

    new["pagination"]["total"] = 1
    new["pagination"]["total_pages"] = 1
    new["pagination"]["end"] = 1

    old = payload()

    old["results"] = old["results"][:1]

    old["pagination"]["total"] = 1
    old["pagination"]["total_pages"] = 1
    old["pagination"]["end"] = 1

    assert check(old, new) == 0

    assert check(payload(), new) == 1


def test_pagination_delta_is_a_regression():

    new = payload()

    new["pagination"] = copy.deepcopy(new["pagination"])
    new["pagination"]["total"] = 3

    assert check(payload(), new) == 1


def test_unsorted_new_results_are_a_regression():
    """
    Self-consistency of the build under test, independent of the reference.

    Scores may differ, but a result list that is not sorted by descending
    score means the ranking pipeline broke.
    """

    new = payload()

    new["results"] = list(reversed(new["results"]))

    assert check(payload(), new) == 1


def test_reordering_within_unchanged_scores_is_not_a_regression():
    """
    Tie order is canonicalized before comparison, so two builds may present
    equal-scoring documents in different order.
    """

    old = payload()
    old["results"][0]["score"] = 0.4

    new = payload()
    new["results"][0]["score"] = 0.4
    new["results"] = list(reversed(new["results"]))

    assert check(old, new) == 0


def test_bare_list_payloads_are_never_explainable():
    """
    The legacy bare-list branches answer [] rather than a page object.

    A shape change there is structural, and the classifier has no fields to
    reason about, so it must fail rather than guess.
    """

    assert check([], []) == 0

    assert check(payload(), []) == 1

    assert check([], payload()) == 1


def test_a_quoted_phrase_can_flip_the_response_shape():
    """
    The bare-list branch fires exactly when NO document matched the phrase.

    Whether a document matches is decided by substring-testing the phrase
    against the joined filename words, so a permuted word order can make the
    reference match nothing and answer [], while the fixed build matches and
    answers a page object. Same root cause as the score deltas, louder
    symptom.

    Real case from the seed corpus, query `"BCS502 Module 2.pdf"`:
    reference filename_words ['bcs502', '2', 'module'] joins to
    'bcs502 2 module' (phrase absent -> []); fixed ['bcs502', 'module', '2']
    joins to 'bcs502 module 2' (phrase present -> page).
    """

    quoted = '"BCS502 Module 2.pdf"'

    assert check([], phrase_payload(), query=quoted) == 0

    # Symmetric: the fix can also REMOVE a false-positive phrase match, so
    # the fixed build answers [] where the permuted reference answered a
    # page. Both directions are word-order effects.
    assert check(phrase_payload(), [], query=quoted) == 0


def test_a_shape_change_without_a_quoted_phrase_is_a_regression():
    """
    Only the quoted-phrase branch can be flipped by filename word order.

    An unquoted query answering a page object on one side and a bare list on
    the other is not explainable by the determinism fix, so it must fail.
    """

    assert check([], phrase_payload(), query="BCS502 Module 2.pdf") == 1

    assert check(phrase_payload(), [], query="BCS502 Module 2.pdf") == 1


def test_a_shape_change_with_no_query_context_is_a_regression():
    """
    The conservative default: without the query there is no evidence the
    bare-list branch was reachable, so guess nothing and fail.

    This is why the original bare-list tests still hold - they pass no
    query.
    """

    assert check([], phrase_payload()) == 1

    assert check(phrase_payload(), []) == 1


def test_a_shape_change_needs_an_empty_bare_list():
    """
    The legacy branches only ever answer an empty list. A non-empty bare
    list on either side is not this situation.
    """

    quoted = '"BCS502 Module 2.pdf"'

    assert check(
        ["unexpected"],
        phrase_payload(),
        query=quoted,
    ) == 1


def test_a_shape_change_needs_results_on_the_page_side():
    """
    [] versus an empty page object is the filetype asymmetry, not a
    word-order effect, and must not be waved through.
    """

    quoted = '"BCS502 Module 2.pdf"'

    assert check(
        [],
        payload(results=[], pagination={
            "page": 1,
            "limit": 10,
            "total": 0,
            "total_pages": 0,
            "has_next": False,
            "has_previous": False,
            "start": 0,
            "end": 0,
        }),
        query=quoted,
    ) == 1


def test_a_shape_change_needs_every_result_filename_derived():
    """
    The guard that keeps this rule from excusing a content regression.

    If any result on the page side lacks a filename score or a phrase score,
    the match did not come from the filename path, and word order cannot be
    the explanation.
    """

    quoted = '"BCS502 Module 2.pdf"'

    no_phrase = phrase_payload()
    no_phrase["results"][0]["phrase_score"] = 0.0

    assert check([], no_phrase, query=quoted) == 1

    no_filename = phrase_payload()
    no_filename["results"][0]["filename_score"] = 0.0

    assert check([], no_filename, query=quoted) == 1

    # One content-only result among filename-derived ones is enough to
    # disqualify the whole delta.
    mixed = phrase_payload()
    mixed["results"] = mixed["results"] + payload()["results"][1:]

    assert check([], mixed, query=quoted) == 1


def test_status_code_delta_is_a_regression():

    assert compare_search(
        "test",
        (200, payload()),
        (500, payload()),
    ) == 1
