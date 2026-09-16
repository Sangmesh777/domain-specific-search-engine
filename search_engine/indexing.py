"""
Document layout and index building.

Extracted verbatim from app.py by tools/extract_modules.py.
Do not retype these functions: the bodies must stay byte
identical to the engine that the golden vectors pin.

Turning the document folder into an index, and locating documents
inside it.

This module owns two things the offline mode needs as much as the online
one does:

* which files in a folder become documents, and what a document's
  metadata, tokens and pages look like;
* where a named document actually lives, and whether that path is
  allowed.

It does not own publication. `build_index_from_folder` returns a
complete snapshot and installs nothing; the caller decides when to save
it, when to swap it in, and when to mirror it to SQLite. That keeps the
rebuild ordering in one place - the application - instead of spreading
it across two modules.

A document that cannot be extracted is skipped with a printed reason
rather than failing the rebuild. That behaviour is unchanged.
"""

import os

from search_engine.extract import extract_pages, extract_text
from search_engine.text import tokenize, tokenize_filename


def resolve_document_path(
    data_folder,
    filename,
):
    """
    Resolve an indexed document to a real path inside data_folder.

    Prevents GET/DELETE document paths from escaping data_folder.
    """

    if not filename:
        return None

    data_root = os.path.realpath(data_folder)

    requested_path = os.path.realpath(
        os.path.join(
            data_folder,
            os.path.basename(filename)
        )
    )

    try:
        if os.path.commonpath(
            [data_root, requested_path]
        ) != data_root:
            return None
    except ValueError:
        return None

    return requested_path


def build_index_from_folder(
    data_folder,
):
    """
    Build a complete new index without mutating the active snapshot.

    Searches continue to use the previous complete snapshot until the
    new snapshot has been fully built and persisted. Then all four active
    index dictionaries are swapped together.
    """

    print()
    print("==============================================")
    print("REBUILDING SEARCH DATABASE")
    print("==============================================")

    new_inverted_index = {}
    new_document_metadata = {}
    new_filename_index = {}
    new_page_text_index = {}

    supported_extensions = (
        ".pdf",
        ".docx",
        ".txt",
    )

    for filename in os.listdir(data_folder):

        if not filename.lower().endswith(
            supported_extensions
        ):
            continue

        file_path = os.path.join(
            data_folder,
            filename,
        )

        print(
            f"[REBUILD] Indexing: {filename}"
        )

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
                print(
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

            new_filename_index[
                filename
            ] = filename_words

            new_page_text_index[
                filename
            ] = pages

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

            print(
                f"[REBUILD ERROR] Could not index "
                f"{filename}: {error}"
            )

            continue

    print()
    print(
        "[REBUILD] New snapshot complete: "
        f"{len(new_document_metadata)} documents, "
        f"{len(new_inverted_index)} content terms, "
        f"{len(new_filename_index)} filenames indexed, "
        f"{len(new_page_text_index)} page-text entries"
    )

    return {
        "inverted_index": new_inverted_index,
        "document_metadata": new_document_metadata,
        "filename_index": new_filename_index,
        "page_text_index": new_page_text_index,
    }
