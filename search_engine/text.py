"""
Text tokenization and query parsing.

Extracted verbatim from app.py by tools/extract_modules.py.
Do not retype these functions: the bodies must stay byte
identical to the engine that the golden vectors pin.
"""

def tokenize(text):
    """Tokenize normal document content."""

    text = text.lower()

    cleaned_text = "".join(
        character
        if character.isalnum() or character.isspace()
        else " "
        for character in text
    )

    return [
        word
        for word in cleaned_text.split()
        if len(word) > 1
    ]


def tokenize_filename(text):
    """
    Tokenize filenames and search queries while preserving
    numeric tokens such as 1, 2, and 3.
    """

    text = text.lower()

    cleaned_text = "".join(
        character
        if character.isalnum() or character.isspace()
        else " "
        for character in text
    )

    return [
        word
        for word in cleaned_text.split()
        if word
    ]


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

    supported_extensions = (
        ".pdf",
        ".docx",
        ".txt"
    )

    for extension in supported_extensions:

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

    supported_extensions = (
        (".pdf", "pdf"),
        (".docx", "docx"),
        (".txt", "txt"),
    )

    # Handle an extension attached to the final filename token,
    # including a quoted filename.
    for extension, filetype in supported_extensions:

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

    supported_types = {
        "pdf": "pdf",
        ".pdf": "pdf",
        "docx": "docx",
        ".docx": "docx",
        "txt": "txt",
        ".txt": "txt",
    }

    detected_type = None
    remaining_tokens = []

    for token in tokens:

        normalized = token.lower().strip()

        if normalized in supported_types:

            if detected_type is None:
                detected_type = supported_types[
                    normalized
                ]

            continue

        remaining_tokens.append(token)

    return (
        " ".join(remaining_tokens).strip(),
        detected_type,
    )
