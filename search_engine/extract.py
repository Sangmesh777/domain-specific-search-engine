"""
Document text and page extraction.

Extracted verbatim from app.py by tools/extract_modules.py.
Do not retype these functions: the bodies must stay byte
identical to the engine that the golden vectors pin.
"""

import PyPDF2
import docx
def count_phrase_occurrences(text, phrase):
    """
    Count exact adjacent occurrences of a normalized phrase.

    A phrase occurrence means the query terms appear next to each
    other in the same order. Word proximity without adjacency is
    not counted.
    """

    if not text or not phrase:
        return 0

    normalized_text = " ".join(
        text.lower().split()
    )

    normalized_phrase = " ".join(
        phrase.lower().split()
    )

    if not normalized_phrase:
        return 0

    count = 0
    start = 0

    while True:

        position = normalized_text.find(
            normalized_phrase,
            start
        )

        if position == -1:
            break

        count += 1

        start = (
            position
            +
            len(normalized_phrase)
        )

    return count


def extract_text(file_path, filename):
    """Extract complete text from PDF, DOCX, or TXT."""

    text = ""

    try:
        filename_lower = filename.lower()

        if filename_lower.endswith(".pdf"):

            with open(file_path, "rb") as file:
                reader = PyPDF2.PdfReader(file)

                for page in reader.pages:

                    extracted = page.extract_text()

                    if extracted:
                        text += extracted + " "

        elif filename_lower.endswith(".docx"):

            document = docx.Document(file_path)

            for paragraph in document.paragraphs:
                text += paragraph.text + " "

        elif filename_lower.endswith(".txt"):

            with open(
                file_path,
                "r",
                encoding="utf-8"
            ) as file:
                text = file.read()

    except Exception as error:

        print(
            f"[ERROR] Could not read {filename}: {error}"
        )

    return text.lower()


def extract_pages(file_path, filename):
    """
    Extract page-level text.

    PDFs:
        returns [{"page": 1, "text": "..."}]

    DOCX/TXT:
        treated as one logical page.
    """

    pages = []

    try:

        filename_lower = filename.lower()

        if filename_lower.endswith(".pdf"):

            with open(file_path, "rb") as file:

                reader = PyPDF2.PdfReader(file)

                for page_number, page in enumerate(
                    reader.pages,
                    start=1
                ):

                    extracted = page.extract_text()

                    if extracted:
                        pages.append({
                            "page": page_number,
                            "text": extracted.lower()
                        })

        elif filename_lower.endswith(".docx"):

            document = docx.Document(file_path)

            text = "\n".join(
                paragraph.text
                for paragraph in document.paragraphs
            )

            if text.strip():
                pages.append({
                    "page": 1,
                    "text": text.lower()
                })

        elif filename_lower.endswith(".txt"):

            with open(
                file_path,
                "r",
                encoding="utf-8"
            ) as file:

                text = file.read()

                if text.strip():
                    pages.append({
                        "page": 1,
                        "text": text.lower()
                    })

    except Exception as error:

        print(
            f"[ERROR] Could not extract pages from "
            f"{filename}: {error}"
        )

    return pages
