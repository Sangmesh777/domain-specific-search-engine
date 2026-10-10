"""
Relevance floors for the labelled query set.

The golden vectors pin the ranking *exactly*; they say nothing about
whether it is any good. These tests say whether it is. A ranking change
that keeps the vectors green but drops these numbers is a regression,
and this is what catches it.
"""

from tools.relevance_eval import evaluate


def test_the_labelled_set_is_sound_and_meets_its_floor():
    metrics, rows = evaluate()

    # If a label named a document that is not in the corpus, its query
    # would retrieve nothing relevant and deflate the metrics silently.
    # Recall of zero is the tell, so treat it as a broken label.
    unresolved = [row["query"] for row in rows if row["recall_at_10"] == 0.0]

    assert not unresolved, (
        f"no labelled document was retrieved for: {unresolved} — either a "
        f"label is wrong or the ranking stopped finding it"
    )

    assert metrics["mrr"] >= 0.9, metrics
    assert metrics["ndcg_at_10"] >= 0.9, metrics
    assert metrics["recall_at_10"] >= 0.9, metrics
