"""
Golden corpus definition.

This module is the single source of truth for the corpus used by the
golden-vector harness and by the Android corpus sidecar.

The corpus is deliberately small (13 documents) but is designed so that
every ranking dimension in the engine is exercised by at least one
vector:

    exact content match ....... `network`
    prefix match .............. `net` / `netw`
    numeric substring ......... `9901`
    filename match ............ `BCS502 Module 2`
    filename numeric token .... `BCS502`
    phrase scoring ............ `"distributed ledger"`
    filetype filter ........... `txt` / `pdf` / `docx`
    pagination ................ `sharedterm` (12 documents, limit 10)
    snippet windowing ......... `long_document.txt`
    Unicode tokens ............ `unicode_notes.txt`

Documents are materialised as real files (TXT / DOCX / PDF) and are
uploaded through the real HTTP API, so the extraction path is
exercised rather than bypassed.

Text below is written in mixed case on purpose: the engine lowercases
during extraction, so the corpus proves that step is applied.
"""

# Extension -> how the document is materialised on disk.
FORMAT_TXT = "txt"
FORMAT_DOCX = "docx"
FORMAT_PDF = "pdf"

GOLDEN_CORPUS = [
    {
        "name": "network_security_notes.txt",
        "format": FORMAT_TXT,
        "text": (
            "Network security notes\n"
            "A network connects hosts. Networking adds protocols.\n"
            "Networked systems exchange packets over a shared medium.\n"
            "sharedterm appears in the opening paragraph.\n"
        ),
    },
    {
        "name": "BCS502 Module 2.pdf",
        "format": FORMAT_PDF,
        "text": (
            "BCS502 Module 2 lecture handout\n"
            "This document covers routing and switching fundamentals.\n"
            "sharedterm appears in the lecture handout.\n"
        ),
    },
    {
        "name": "cryptography_basics.docx",
        "format": FORMAT_DOCX,
        "text": (
            "Cryptography basics\n"
            "Symmetric ciphers use one key. Asymmetric ciphers use two.\n"
            "sharedterm appears in the cryptography primer.\n"
        ),
    },
    {
        "name": "distributed_ledger.txt",
        "format": FORMAT_TXT,
        "text": (
            "A distributed ledger records transactions.\n"
            "The distributed ledger is replicated across nodes.\n"
            "Every distributed ledger keeps an append only history.\n"
            "sharedterm appears in the ledger note.\n"
        ),
    },
    {
        "name": "numeric_fragments.txt",
        "format": FORMAT_TXT,
        "text": (
            "Serial numbers and codes\n"
            "The batch identifier is 9901xyz.\n"
            "A second identifier is 9902xyz.\n"
            "sharedterm appears in the numeric note.\n"
        ),
    },
    {
        "name": "unicode_notes.txt",
        "format": FORMAT_TXT,
        # Deliberately includes:
        #   U+00E9 e acute, U+00BD vulgar one half, U+03A9 omega,
        #   U+096B devanagari five, U+5B57 CJK, U+00A0 no-break space,
        #   U+2007 figure space, U+202F narrow no-break space,
        #   U+0085 next line, U+200B zero width space,
        #   U+0130 latin capital I with dot above
        "text": (
            "Unicode notes\n"
            "caf\u00e9 r\u00e9sum\u00e9 na\u00efve\n"
            "half \u00bd and a quarter \u00bc\n"
            "omega \u03a9 and sigma \u03c3\n"
            "devanagari \u096b digits\n"
            "cjk \u5b57 character\n"
            "no\u00a0break figure\u2007space narrow\u202fspace\n"
            "next\u0085line and zero\u200bwidth\n"
            "\u0130stanbul capital dotted i\n"
            "sharedterm appears in the unicode note.\n"
        ),
    },
    {
        "name": "prefix_network_alpha.txt",
        "format": FORMAT_TXT,
        "text": (
            "Network alpha report\n"
            "The network topology is a mesh.\n"
            "sharedterm appears in the alpha report.\n"
        ),
    },
    {
        "name": "prefix_network_beta.txt",
        "format": FORMAT_TXT,
        "text": (
            "Network beta report\n"
            "The network topology is a star.\n"
            "sharedterm appears in the beta report.\n"
        ),
    },
    {
        "name": "long_document.txt",
        "format": FORMAT_TXT,
        "text": (
            "Long document\n"
            + " ".join(f"filler{i} padding text" for i in range(1, 60))
            + "\nThe keyword beacon appears only at the very end of this document.\n"
            "sharedterm appears in the long document.\n"
        ),
    },
    {
        "name": "search_engine_design.txt",
        "format": FORMAT_TXT,
        "text": (
            "Search engine design\n"
            "An inverted index maps terms to documents.\n"
            "Ranking combines a filename score and a content score.\n"
            "sharedterm appears in the design note.\n"
        ),
    },
    {
        "name": "Title Case Notes.txt",
        "format": FORMAT_TXT,
        "text": (
            "Title case notes\n"
            "Spaces in the filename are preserved by the sanitizer.\n"
            "sharedterm appears in the title case note.\n"
        ),
    },
    {
        "name": "page_text_probe.txt",
        "format": FORMAT_TXT,
        "text": (
            "Page text probe\n"
            "This document verifies page level text extraction.\n"
            "sharedterm appears in the probe.\n"
        ),
    },
    {
        "name": "minimal_note.txt",
        "format": FORMAT_TXT,
        "text": (
            "Minimal note\n"
            "sharedterm appears in the minimal note.\n"
        ),
    },
]


def corpus_names():
    """Return the corpus document names in definition order."""
    return [document["name"] for document in GOLDEN_CORPUS]


def total_text_characters():
    """Return the number of characters of source text in the corpus."""
    return sum(len(document["text"]) for document in GOLDEN_CORPUS)
