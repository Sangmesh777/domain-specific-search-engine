"""
Full corpus rebuild.

A rebuild re-reads every supported document in the data folder and produces
four brand-new structures. It never mutates the active snapshot, so searches
keep being served from the previous complete index until the caller swaps
the new one in.

That split is the point of this module:

    rebuild.build_snapshot_from_folder(...)  -> pure: folder in, snapshot out
    SearchEngine.rebuild()                   -> persist, swap, synchronize

An offline Android backend can therefore run the identical rebuild against
application-private storage, and report progress to its UI through the same
`log` callback, without any of this code knowing what a thread or an HTTP
response is.
"""

import os

from search_engine.extraction import (
    extract_pages,
    extract_text,
)
from search_engine.tokenizer import (
    tokenize,
    tokenize_filename,
)


REBUILD_BANNER = "=============================================="


def build_snapshot_from_folder(
    config,
    log=print,
):
    """
    Index every supported document in the data folder.

    Returns (inverted_index, document_metadata, filename_index,
    page_text_index) as four new dictionaries.

    Unreadable or empty documents are reported and skipped; one bad file
    never aborts a rebuild, because the point of a rebuild is to recover a
    usable index from whatever is actually on disk.
    """

    new_inverted_index = {}
    new_document_metadata = {}
    new_filename_index = {}
    new_page_text_index = {}

    # Sorted, not bare os.listdir.
    #
    # Enumeration order is filesystem-specific, and it becomes the insertion
    # order of all four dictionaries. The final ranking sort is stable, so
    # documents with equal scores keep that insertion order - which made the
    # order of tied results differ between deployments holding identical
    # corpora, and change on any machine that rebuilt after a restore.
    #
    # Sorting costs nothing measurable against the extraction work in this
    # loop and makes a rebuild reproducible.
    for filename in sorted(os.listdir(config.data_folder)):

        if not config.is_supported_document(filename):
            continue

        file_path = config.document_path(filename)

        log(f"[REBUILD] Indexing: {filename}")

        try:
            text = extract_text(
                file_path,
                filename,
            )

            content_words = tokenize(text)

            filename_without_extension = (
                os.path.splitext(filename)[0]
            )

            filename_words = tokenize_filename(
                filename_without_extension
            )

            pages = extract_pages(
                file_path,
                filename,
            )

            if not content_words:
                log(
                    "[REBUILD] Skipped empty document: "
                    f"{filename}"
                )
                continue

            new_document_metadata[filename] = {
                "title": filename,
                "path": os.path.abspath(file_path),
                "total_words": len(content_words),
                "page_count": len(pages),
            }

            new_filename_index[filename] = filename_words

            new_page_text_index[filename] = pages

            for word in content_words:

                if word not in new_inverted_index:
                    new_inverted_index[word] = {}

                if (
                    filename
                    not in new_inverted_index[word]
                ):
                    new_inverted_index[word][filename] = 0

                new_inverted_index[word][filename] += 1

        except Exception as error:

            log(
                f"[REBUILD ERROR] Could not index "
                f"{filename}: {error}"
            )

            continue

    log("")
    log(
        "[REBUILD] New snapshot complete: "
        f"{len(new_document_metadata)} documents, "
        f"{len(new_inverted_index)} content terms, "
        f"{len(new_filename_index)} filenames indexed, "
        f"{len(new_page_text_index)} page-text entries"
    )

    return (
        new_inverted_index,
        new_document_metadata,
        new_filename_index,
        new_page_text_index,
    )
