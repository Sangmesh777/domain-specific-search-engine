"""
Pagination.

Ranking happens before this module. Pagination only slices an already
ranked list and reports stable metadata, so a page never changes meaning
between requests for the same corpus.

The functions return plain dictionaries. Serializing them (jsonify on the
web adapter, a Kotlin data class on Android) belongs to the adapter.
"""


def paginate(results, page, limit):
    """
    Return a stable paginated search payload.

    Shape:
        {
            "results": [...],
            "pagination": {
                page, limit, total, total_pages,
                has_next, has_previous, start, end
            }
        }
    """

    total = len(results)

    total_pages = (
        (total + limit - 1) // limit
        if total > 0
        else 0
    )

    if total_pages > 0:
        page = max(
            1,
            min(page, total_pages)
        )
    else:
        page = max(1, page)

    start_index = (
        (page - 1) * limit
        if total_pages > 0
        else 0
    )

    end_index = (
        start_index + limit
    )

    page_results = results[
        start_index:end_index
    ]

    return {
        "results": page_results,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": total_pages,
            "has_next":
                page < total_pages,
            "has_previous":
                page > 1
                and total_pages > 0,
            "start":
                (
                    start_index + 1
                    if page_results
                    else 0
                ),
            "end":
                (
                    start_index
                    + len(page_results)
                    if page_results
                    else 0
                ),
        },
    }


def empty_query_response():
    """
    Payload returned for a blank query.

    This deliberately keeps the historical shape of the API: a blank query
    is answered before any ranking work happens and the pagination block
    carries no start/end offsets. Changing it would be an API behavior
    change, not a refactor, so it is preserved and isolated here.
    """

    return {
        "results": [],
        "pagination": {
            "page": 1,
            "limit": 10,
            "total": 0,
            "total_pages": 0,
            "has_next": False,
            "has_previous": False
        }
    }
