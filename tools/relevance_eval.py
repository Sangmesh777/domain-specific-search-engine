"""
Relevance evaluation over the golden corpus.

The ranking has always been pinned *exactly* — the golden vectors prove
the engine returns the same response — but it was never *measured*. Pinning
says nothing about whether what it returns is any good.

This runs a small labelled query set against the real engine and reports
standard information-retrieval metrics, so a ranking change can be judged
rather than only diffed:

    MRR           mean reciprocal rank of the first relevant result
    nDCG@10       graded gain, discounted by position, normalised
    precision@5   share of the top five that are relevant
    recall@10     share of the labelled documents found in the top ten

The labels are deliberately conservative: every query names documents
whose relevance is not in doubt, so the numbers are stable and the whole
set can be reviewed by reading it.

    python3 -m tools.relevance_eval
"""

import math
from urllib.parse import unquote

from tools.engine_session import isolated_engine, wait_for_index_ready
from tools.golden_corpus import corpus_names

# query -> {filename: grade}. Grade 2 is a document the query is really
# about; grade 1 is a document the query genuinely touches.
LABELLED_QUERIES = [
    ("distributed ledger", {"distributed_ledger.txt": 2}),
    ("network security", {"network_security_notes.txt": 2}),
    ("cryptography", {"cryptography_basics.docx": 2}),
    ("BCS502", {"BCS502_Module_2.pdf": 2}),
    ("routing and switching", {"BCS502_Module_2.pdf": 2}),
    ("inverted index", {"search_engine_design.txt": 2}),
    ("unicode", {"unicode_notes.txt": 2}),
    ("beacon", {"long_document.txt": 2}),
    ("9901xyz", {"numeric_fragments.txt": 2}),
    (
        "network topology",
        {"prefix_network_alpha.txt": 1, "prefix_network_beta.txt": 1},
    ),
    ("page level text extraction", {"page_text_probe.txt": 2}),
    ("title case", {"Title_Case_Notes.txt": 2}),
]

K = 10


def _filenames(response):
    """Pull the ranked filenames out of whatever response shape comes back."""
    results = response if isinstance(response, list) else response["results"]

    names = []
    for result in results:
        url = result["document_url"]
        names.append(unquote(url.rsplit("/", 1)[-1]))
    return names


def _dcg(gains):
    return sum(
        (2**gain - 1) / math.log2(position + 2) for position, gain in enumerate(gains)
    )


def evaluate():
    """Return (metrics, per_query) for the labelled set."""
    rows = []

    with isolated_engine(populate_corpus=True) as (client, engine, workspace):
        wait_for_index_ready(client, len(corpus_names()))

        for query, labels in LABELLED_QUERIES:
            response = client.get(
                "/api/search", query_string={"q": query, "limit": K}
            ).get_json()

            ranked = _filenames(response)[:K]
            gains = [labels.get(name, 0) for name in ranked]

            first = next((i for i, name in enumerate(ranked) if name in labels), None)
            reciprocal_rank = 1.0 / (first + 1) if first is not None else 0.0

            ideal = sorted(labels.values(), reverse=True)
            dcg = _dcg(gains)
            idcg = _dcg(ideal)
            ndcg = dcg / idcg if idcg else 0.0

            hits5 = sum(1 for name in ranked[:5] if name in labels)
            found = sum(1 for name in ranked if name in labels)

            rows.append(
                {
                    "query": query,
                    "reciprocal_rank": reciprocal_rank,
                    "ndcg": ndcg,
                    "precision_at_5": hits5 / 5.0,
                    "recall_at_10": found / len(labels),
                    "top": ranked[:3],
                }
            )

    n = len(rows)
    metrics = {
        "queries": n,
        "mrr": sum(r["reciprocal_rank"] for r in rows) / n,
        "ndcg_at_10": sum(r["ndcg"] for r in rows) / n,
        "precision_at_5": sum(r["precision_at_5"] for r in rows) / n,
        "recall_at_10": sum(r["recall_at_10"] for r in rows) / n,
    }
    return metrics, rows


def main():
    metrics, rows = evaluate()

    print(f"{'query':<30} {'RR':>5} {'nDCG':>6} {'P@5':>5} {'R@10':>6}  top-3")
    print("-" * 96)
    for row in rows:
        print(
            f"{row['query']:<30} {row['reciprocal_rank']:>5.2f} "
            f"{row['ndcg']:>6.2f} {row['precision_at_5']:>5.2f} "
            f"{row['recall_at_10']:>6.2f}  {', '.join(row['top'])}"
        )

    print("-" * 96)
    print(
        f"{'mean over ' + str(metrics['queries']) + ' queries':<30} "
        f"{metrics['mrr']:>5.2f} {metrics['ndcg_at_10']:>6.2f} "
        f"{metrics['precision_at_5']:>5.2f} {metrics['recall_at_10']:>6.2f}"
    )
    return metrics


if __name__ == "__main__":
    main()
