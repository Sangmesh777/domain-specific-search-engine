"""
Query cost must not scale with the corpus.

A response only ever contains one page, but the snippet is the expensive
part of building a result object. An earlier version built a snippet for
every ranked document and then discarded all but the page, which made a
search over 1,000 documents do ~770 snippet builds to return 10.

These tests pin the cheap behaviour structurally — by counting snippet
builds, not by timing — so they cannot flake on a slow machine.
"""

import random

from search_engine import engine
from search_engine.engine import search_index
from search_engine.text import tokenize_filename


def _index(doc_count, words_per_doc=40, vocab=200, seed=7):
    """A synthetic corpus large enough that a naive path is obvious."""
    rng = random.Random(seed)
    terms = [f"t{i}" for i in range(vocab)]

    docs = {
        f"doc{i:04d}.txt": [rng.choice(terms) for _ in range(words_per_doc)]
        for i in range(doc_count)
    }

    inverted = {}
    for name, words in docs.items():
        for word in words:
            postings = inverted.setdefault(word, {})
            postings[name] = postings.get(name, 0) + 1

    metadata = {
        name: {"title": name, "path": "/tmp/" + name, "total_words": len(words)}
        for name, words in docs.items()
    }
    filename_index = {
        name: tokenize_filename(name.rsplit(".", 1)[0]) for name in docs
    }
    page_text = {
        name: [{"page": 1, "text": " ".join(words)}]
        for name, words in docs.items()
    }

    return inverted, metadata, filename_index, page_text


def test_snippets_are_built_only_for_the_returned_page(monkeypatch):
    inverted, metadata, filename_index, page_text = _index(500)

    calls = {"count": 0}
    real = engine.get_snippet_and_page

    def counting(*args, **kwargs):
        calls["count"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, "get_snippet_and_page", counting)

    # "t1" prefix-matches t1, t10..t19 and t100..t199, so it matches a
    # large share of the corpus. A naive implementation would build a
    # snippet for every one of them.
    response = search_index(
        "t1", None, "10", inverted, metadata, filename_index, page_text
    )

    assert response["pagination"]["total"] > 50, (
        "the query should match many documents, or this proves nothing"
    )
    assert len(response["results"]) == 10
    assert calls["count"] <= 10, (
        f"built {calls['count']} snippets to return a 10-result page"
    )


def test_result_objects_are_built_only_for_the_returned_page(monkeypatch):
    """The same windowing must apply to the whole result object, not just
    the snippet: the discarded documents must not even be assembled."""
    inverted, metadata, filename_index, page_text = _index(500)

    built = {"count": 0}
    real_quote = engine.quote

    def counting_quote(*args, **kwargs):
        built["count"] += 1
        return real_quote(*args, **kwargs)

    monkeypatch.setattr(engine, "quote", counting_quote)

    response = search_index(
        "t1", None, "10", inverted, metadata, filename_index, page_text
    )

    assert response["pagination"]["total"] > 50
    assert built["count"] <= 10, (
        f"built {built['count']} result URLs to return a 10-result page"
    )


def test_pagination_still_reports_the_full_total(monkeypatch):
    """Skipping discarded work must not change the reported totals."""
    inverted, metadata, filename_index, page_text = _index(500)

    response = search_index(
        "t1", None, "10", inverted, metadata, filename_index, page_text
    )

    pagination = response["pagination"]
    assert pagination["total"] > 50
    assert pagination["limit"] == 10
    assert pagination["page"] == 1
    assert pagination["total_pages"] == (pagination["total"] + 9) // 10
    assert pagination["has_next"] is True
    assert pagination["has_previous"] is False
    assert pagination["start"] == 1
    assert pagination["end"] == 10
