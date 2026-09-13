"""
Minimal PDF writer used by development tooling.

The regression suite needs a seeded document corpus, and the corpus must
contain real PDF files so that page-level extraction, page numbers and
PDF filetype filtering are actually exercised.

Only the standard library is used: the generated files are simple
single-font PDFs whose text PyPDF2 can extract page by page.

This module is development infrastructure. It is not imported by the
search engine core or by the Flask adapter.
"""

import os


def _escape_pdf_text(text):
    return (
        text
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def _content_stream(page_text):
    parts = [
        "BT /F1 12 Tf 14 TL 72 720 Td"
    ]

    for line_number, line in enumerate(
        page_text.split("\n")
    ):
        escaped = _escape_pdf_text(line)

        if line_number == 0:
            parts.append(f"({escaped}) Tj")
        else:
            parts.append(f"({escaped}) '")

    parts.append("ET")

    return "\n".join(parts)


def write_pdf(path, pages):
    """
    Write a PDF whose page N contains pages[N - 1].

    pages is a list of plain-text page bodies.
    """

    page_count = len(pages)

    catalog_id = 1
    pages_id = 2
    font_id = 3
    first_page_id = 4

    page_ids = [
        first_page_id + (index * 2)
        for index in range(page_count)
    ]

    content_ids = [
        first_page_id + (index * 2) + 1
        for index in range(page_count)
    ]

    objects = {}

    children = " ".join(
        f"{page_id} 0 R"
        for page_id in page_ids
    )

    objects[catalog_id] = (
        f"<< /Type /Catalog /Pages {pages_id} 0 R >>"
    )

    objects[pages_id] = (
        f"<< /Type /Pages /Kids [{children}] "
        f"/Count {page_count} >>"
    )

    objects[font_id] = (
        "<< /Type /Font /Subtype /Type1 "
        "/BaseFont /Helvetica >>"
    )

    for index, page_text in enumerate(pages):

        objects[page_ids[index]] = (
            f"<< /Type /Page /Parent {pages_id} 0 R "
            f"/MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_ids[index]} 0 R >>"
        )

        stream = _content_stream(page_text)

        objects[content_ids[index]] = (
            f"<< /Length {len(stream)} >>\n"
            f"stream\n{stream}\nendstream"
        )

    output = bytearray(b"%PDF-1.4\n")

    offsets = {}

    for object_id in sorted(objects):

        offsets[object_id] = len(output)

        output += (
            f"{object_id} 0 obj\n"
            f"{objects[object_id]}\n"
            f"endobj\n"
        ).encode("latin-1")

    xref_position = len(output)

    highest_id = max(objects)

    output += (
        f"xref\n0 {highest_id + 1}\n"
    ).encode("latin-1")

    output += b"0000000000 65535 f \n"

    for object_id in range(1, highest_id + 1):

        output += (
            f"{offsets[object_id]:010d} 00000 n \n"
        ).encode("latin-1")

    output += (
        f"trailer\n"
        f"<< /Size {highest_id + 1} "
        f"/Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref_position}\n%%EOF\n"
    ).encode("latin-1")

    directory = os.path.dirname(os.path.abspath(path))

    os.makedirs(directory, exist_ok=True)

    with open(path, "wb") as file:
        file.write(bytes(output))

    return path
