"""
Engine configuration.

The engine owns its infrastructure configuration. Callers configure an
engine once and then use business operations:

    engine.search(...)
    engine.index_document(...)
    engine.delete(...)
    engine.bulk_delete(...)
    engine.rebuild(...)

instead of passing sqlite_db_file / data_folder into every call.
"""

import os


# Document types the engine can extract, index and serve.
#
# Order is significant: query normalization stops at the first supported
# extension it finds at the end of a query.
SUPPORTED_EXTENSIONS = (
    ".pdf",
    ".docx",
    ".txt",
)


# Filetype filter tokens accepted in a search query, mapped to the
# canonical filter value.
FILETYPE_FILTER_TOKENS = {
    "pdf": "pdf",
    ".pdf": "pdf",
    "docx": "docx",
    ".docx": "docx",
    "txt": "txt",
    ".txt": "txt",
}


# Extension -> canonical filetype, in match order.
FILETYPE_EXTENSIONS = (
    (".pdf", "pdf"),
    (".docx", "docx"),
    (".txt", "txt"),
)


def default_data_folder():
    """Repository-local data folder used by the Flask adapter."""

    base_dir = os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )

    return os.path.join(base_dir, "data")
