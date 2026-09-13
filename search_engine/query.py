"""
Query interpretation.

Pure functions that turn a raw user query into the pieces the ranking
pipeline needs:

    "network pdf"            -> ("network", "pdf")
    "BCS502 Module 2.pdf"    -> ("BCS502 Module 2", "pdf")
    '"BCS502 Module 2.pdf"'  -> ('"BCS502 Module 2"', "pdf")
"""

from search_engine.config import (
    FILETYPE_EXTENSIONS,
    FILETYPE_FILTER_TOKENS,
    SUPPORTED_EXTENSIONS,
)


def normalize_search_query(query):
    """
    Normalize a search query before filename/content matching.

    A supported document extension at the end of the query is
    removed so that:

        BCS502 Module 2.pdf
        BCS502 Module 2

    are treated as the same filename search.

    Supported extensions:
        .pdf
        .docx
        .txt
    """

    query = query.strip()

    if not query:
        return ""

    # Handle a quoted query such as:
    # "BCS502 Module 2.pdf"
    quote_wrapped = (
        len(query) >= 2
        and query.startswith('"')
        and query.endswith('"')
    )

    if quote_wrapped:
        query = query[1:-1].strip()

    lower_query = query.lower()

    for extension in SUPPORTED_EXTENSIONS:

        if lower_query.endswith(extension):

            query = query[
                :-len(extension)
            ].rstrip()

            break

    if quote_wrapped:
        return f'"{query}"'

    return query


def parse_filetype_filter(query):
    """
    Extract an optional document-type filter from a raw search query.

    Supported forms:
        pdf
        .pdf
        network pdf
        network .pdf
        BCS502 Module 2.pdf
        "BCS502 Module 2.pdf"

    Returns:
        (remaining_keyword_query, filetype_filter)
    """

    query = query.strip()

    # Handle an extension attached to the final filename token,
    # including a quoted filename.
    for extension, filetype in FILETYPE_EXTENSIONS:

        quoted_suffix = f'{extension}"'

        if query.lower().endswith(
            quoted_suffix
        ):
            return (
                query[
                    :-len(quoted_suffix)
                ].rstrip() + '"'
                if query[
                    :-len(quoted_suffix)
                ].rstrip()
                else "",
                filetype,
            )

        if query.lower().endswith(
            extension
        ):

            return (
                query[
                    :-len(extension)
                ].rstrip(),
                filetype,
            )

    # Handle standalone type tokens, e.g.:
    #   network pdf
    #   customer .docx
    tokens = query.split()

    if not tokens:
        return query, None

    detected_type = None
    remaining_tokens = []

    for token in tokens:

        normalized = token.lower().strip()

        if normalized in FILETYPE_FILTER_TOKENS:

            if detected_type is None:
                detected_type = FILETYPE_FILTER_TOKENS[
                    normalized
                ]

            continue

        remaining_tokens.append(token)

    return (
        " ".join(remaining_tokens).strip(),
        detected_type,
    )
