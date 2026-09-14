"""
Shared parity contract tables.

These tables define the behaviour the Android port must reproduce.
They are consumed twice:

  * by ``tools/generate_golden_vectors.py``, which evaluates every case
    against the real Python engine and embeds the results in the golden
    vector file
  * by ``tests/test_tokenizer_contract.py`` and
    ``tests/test_rounding_contract.py``, which assert the engine still
    behaves this way

The Android golden-vector harness reads the *evaluated* results out of
the golden vector file, so there is exactly one source of truth.

Every expectation was taken from the running engine rather than from
documentation.
"""

# Python str.isalnum() is Unicode-aware and counts No (e.g. ½, ²) and
# Nl (e.g. Ⅻ) numerics as alphanumeric. Java's
# Character.isLetterOrDigit() does not.
PYTHON_ALNUM_CHARACTERS = ["½", "²", "Ⅻ", "५", "字", "Ω", "é", "İ", "a", "Z", "7"]
PYTHON_NON_ALNUM_CHARACTERS = ["\u200b", "\u0307", "!", " ", "\u00a0", "-", "\t", "\n"]

# Python str.isspace() accepts the non-breaking spaces and U+0085 that
# Java's Character.isWhitespace() rejects, and the U+001C-U+001F
# separators that Java's isSpaceChar() rejects.
PYTHON_WHITESPACE_CHARACTERS = [
    "\t",
    "\n",
    "\u000b",
    "\u000c",
    "\r",
    "\u001c",
    "\u001d",
    "\u001e",
    "\u001f",
    " ",
    "\u0085",
    "\u00a0",
    "\u1680",
    "\u2000",
    "\u2007",
    "\u202f",
    "\u205f",
    "\u3000",
]
PYTHON_NON_WHITESPACE_CHARACTERS = ["\u200b", "\u0307", "a", "½", "\u180e"]

CONTENT_TOKENIZE_INPUTS = [
    "Hello World",
    "MIXED Case 123",
    "a b c",
    "x1",
    "1x",
    "café",
    "CAFÉ",
    "½",
    "½½",
    "²²",
    "Ω",
    "ΩΩ",
    "字",
    "字字",
    "५",
    "५५",
    "İstanbul",
    "istanbul",
    "under_score-dash",
    "tab\there",
    "no\u00a0break",
    "figure\u2007space",
    "narrow\u202fspace",
    "next\u0085line",
    "break\u00a0",
    "zero\u200bwidth",
    "\u0307dot",
    "ΣΣ",
    "",
    "   ",
]

FILENAME_TOKENIZE_INPUTS = [
    "a b c",
    "1 2 3",
    "½",
    "minimal_note",
    "İstanbul",
    "BCS502 Module 2",
    "",
]

SANITIZE_INPUTS = [
    "a.txt",
    "My File.pdf",
    "file.TXT",
    "../../etc/passwd.txt",
    "..\\..\\x.txt",
    "a/b/c.txt",
    "/abs/path.txt",
    ".hidden.txt",
    "...hidden.txt",
    "My  File...txt",
    "  spaced  .txt",
    "café.txt",
    "Ünïcödé Ñämé.docx",
    "no-ext",
    "weird.pdf.txt",
    "CON.txt",
    "nul.pdf",
    "archive.zip",
    "book.epub",
    "",
]

# Python round() on a binary64 value is correctly-rounded decimal
# rounding of the *exact* binary value, with ties to even. Java can
# reproduce it with
#     new BigDecimal(value).setScale(digits, RoundingMode.HALF_EVEN)
# but not with BigDecimal.valueOf, not with HALF_UP, and not by
# formatting a decimal string.
ROUND_INPUTS = [
    (2.00005, 4),
    (0.56785, 4),
    (2.675, 2),
    (1.00005, 4),
    (0.125, 2),
    (0.135, 2),
    (0.5, 0),
    (1.5, 0),
    (2.5, 0),
    (-0.5, 0),
    (-1.5, 0),
    (0.0, 4),
    (123.456789, 4),
    (2.6750000000000003, 2),
    (1e-10, 4),
    (1.0, 4),
    (-2.675, 2),
]

QUERY_NORMALIZE_INPUTS = [
    "BCS502 Module 2.pdf",
    "BCS502 Module 2.docx",
    "notes.txt",
    "plain query",
    '"BCS502 Module 2.pdf"',
    "data.csv",
    "",
]

FILETYPE_INPUTS = [
    "pdf",
    ".pdf",
    "network pdf",
    "network .pdf",
    "BCS502 Module 2.pdf",
    '"BCS502 Module 2.pdf"',
    "notes docx",
    "notes txt",
    "plain query",
    "",
]


def build_contract_vectors(engine):
    """
    Evaluate every contract table against `engine`.

    `engine` is the imported ``app`` module. The returned structure is
    JSON-serialisable and is embedded in the golden vector file.
    """

    return {
        "notes": [
            "These vectors pin Unicode, rounding and sanitizer semantics.",
            "The Kotlin port must reproduce every expected value exactly.",
            "Expected values were produced by the Python engine itself.",
        ],
        "character_classes": {
            "alnum": [
                {"codepoint": _codepoint(ch), "isalnum": True}
                for ch in PYTHON_ALNUM_CHARACTERS
            ]
            + [
                {"codepoint": _codepoint(ch), "isalnum": False}
                for ch in PYTHON_NON_ALNUM_CHARACTERS
            ],
            "whitespace": [
                {"codepoint": _codepoint(ch), "isspace": True}
                for ch in PYTHON_WHITESPACE_CHARACTERS
            ]
            + [
                {"codepoint": _codepoint(ch), "isspace": False}
                for ch in PYTHON_NON_WHITESPACE_CHARACTERS
            ],
        },
        "content_tokenize": [
            {"input": text, "expected": engine.tokenize(text)}
            for text in CONTENT_TOKENIZE_INPUTS
        ],
        "filename_tokenize": [
            {"input": text, "expected": engine.tokenize_filename(text)}
            for text in FILENAME_TOKENIZE_INPUTS
        ],
        "sanitize_filename": [
            {"input": text, "expected": engine.sanitize_upload_filename(text)}
            for text in SANITIZE_INPUTS
        ],
        "normalize_search_query": [
            {"input": text, "expected": engine.normalize_search_query(text)}
            for text in QUERY_NORMALIZE_INPUTS
        ],
        "parse_filetype_filter": [
            {
                "input": text,
                "expected_query": engine.parse_filetype_filter(text)[0],
                "expected_filetype": engine.parse_filetype_filter(text)[1],
            }
            for text in FILETYPE_INPUTS
        ],
        "round_half_even": [
            {
                "value": value,
                "digits": digits,
                "expected": round(value, digits),
            }
            for value, digits in ROUND_INPUTS
        ],
    }


def _codepoint(character):
    """Return a stable, readable identifier for a character."""

    return f"U+{ord(character):04X}"
