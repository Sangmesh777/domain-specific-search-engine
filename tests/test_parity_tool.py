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


def check(old, new):
    """Run the classifier the way the tool does: as (status, payload)."""

    return compare_search(
        "test",
        (200, old),
        (200, new),
    )


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


def test_status_code_delta_is_a_regression():

    assert compare_search(
        "test",
        (200, payload()),
        (500, payload()),
    ) == 1
