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


def extract_document(file_path, filename):
    """
    Turn one document file into everything the index stores for it.

    This is the rule the two write paths share: what a document's
    metadata is, how its content is tokenized and counted, how its
    filename stem is tokenized, and which pages it has. It exists as one
    function because it used to exist as two - the rebuild walked a
    folder and the upload path indexed a saved file, each with its own
    copy of these lines.

    Two copies of an indexing rule is how an online index and an offline
    one drift apart, and the offline backend would have had to choose
    one to copy.

    Returns a dict, or None when the document has no readable text. What
    to do about an empty document is the caller's policy, not this
    function's rule: the rebuild skips it and the upload raises, and
    both of those behaviours are preserved.

    A document the extractor cannot read at all still raises, exactly as
    before.

    `filename_words` is not deduplicated and keeps its order. It is what
    `" ".join(filename_words)` is rebuilt from after a restart, so
    repeated tokens and their sequence are both load-bearing.
    """

    text = extract_text(
        file_path,
        filename,
    )

    content_words = tokenize(text)

    term_counts = {}

    for word in content_words:
        term_counts[word] = (
            term_counts.get(word, 0)
            + 1
        )

    pages = extract_pages(
        file_path,
        filename,
    )

    filename_words = tokenize_filename(
        os.path.splitext(filename)[0]
    )

    if not content_words:
        return None

    return {
        "metadata": {
            "title": filename,
            "path": os.path.abspath(file_path),
            "total_words": len(content_words),
            "page_count": len(pages),
        },
        "content_words": content_words,
        "term_counts": term_counts,
        "filename_words": filename_words,
        "pages": pages,
    }


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
            extracted = extract_document(
                file_path,
                filename,
            )

            if extracted is None:
                print(
                    "[REBUILD] Skipped empty document: "
                    f"{filename}"
                )
                continue

            new_document_metadata[filename] = extracted["metadata"]

            new_filename_index[
                filename
            ] = extracted["filename_words"]

            new_page_text_index[
                filename
            ] = extracted["pages"]

            for word, count in extracted["term_counts"].items():

                if word not in new_inverted_index:
                    new_inverted_index[word] = {}

                new_inverted_index[word][filename] = count

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
