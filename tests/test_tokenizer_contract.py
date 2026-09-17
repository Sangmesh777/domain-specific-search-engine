"""
Tokenizer and filename-sanitizer contract.

These tests pin the *exact* Unicode semantics of the Python engine.
They exist because the Android port cannot reproduce them with the
obvious Java/Kotlin equivalents:

    Python                    Java/Kotlin trap
    ------------------------  --------------------------------------
    str.lower()               Character.toLowerCase() drops the
                              U+0130 expansion and the final-sigma
                              rule. Use String.lowercase(Locale.ROOT).
    str.isalnum()             Character.isLetterOrDigit() is false for
                              U+00BD and U+00B2 (category No) and for
                              U+216B (category Nl); Python is true.
    str.isspace()             Character.isWhitespace() is false for
                              U+00A0, U+2007, U+202F and U+0085;
                              Python is true. It is also true for
                              U+001C-U+001F, which Python accepts.

Every case below was taken from the running engine, not from
documentation, so a failure here means the port has drifted.
"""

import pytest

import app

CONTENT_TOKENIZE_CASES = [
    # (input, expected tokens)
    ("Hello World", ["hello", "world"]),
    ("MIXED Case 123", ["mixed", "case", "123"]),
    # Single-character content tokens are dropped.
    ("a b c", []),
    ("x1", ["x1"]),
    ("1x", ["1x"]),
    # Unicode letters survive and are lowercased.
    ("café", ["café"]),
    ("CAFÉ", ["café"]),
    # Non-decimal numerics are alphanumeric in Python.
    ("½", []),
    ("½½", ["½½"]),
    ("²²", ["²²"]),
    # Single-character scripts are dropped, longer runs survive.
    ("Ω", []),
    ("ΩΩ", ["ωω"]),
    ("字", []),
    ("字字", ["字字"]),
    ("५", []),
    ("५५", ["५५"]),
    # The U+0130 expansion: "İstanbul".lower() is "i" + U+0307 +
    # "stanbul". U+0307 is not alphanumeric, so it becomes a separator
    # and the single character "i" is dropped.
    ("İstanbul", ["stanbul"]),
    ("istanbul", ["istanbul"]),
    # Punctuation becomes a separator.
    ("under_score-dash", ["under", "score", "dash"]),
    ("tab\there", ["tab", "here"]),
    # Every Unicode whitespace that Python accepts must split tokens.
    ("no\u00a0break", ["no", "break"]),          # NO-BREAK SPACE
    ("figure\u2007space", ["figure", "space"]),  # FIGURE SPACE
    ("narrow\u202fspace", ["narrow", "space"]),  # NARROW NO-BREAK SPACE
    ("next\u0085line", ["next", "line"]),        # NEXT LINE
    ("break\u00a0", ["break"]),
    # U+200B is neither alphanumeric nor whitespace in Python, so it is
    # replaced by a separator even though Java calls it not-whitespace.
    ("zero\u200bwidth", ["zero", "width"]),
    # A combining mark is not alphanumeric, so it separates.
    ("\u0307dot", ["dot"]),
]

FILENAME_TOKENIZE_CASES = [
    # The filename tokenizer keeps every non-empty token, including
    # single characters and bare digits.
    ("a b c", ["a", "b", "c"]),
    ("1 2 3", ["1", "2", "3"]),
    ("½", ["½"]),
    ("minimal_note", ["minimal", "note"]),
    ("İstanbul", ["i", "stanbul"]),
    ("BCS502 Module 2", ["bcs502", "module", "2"]),
]


@pytest.mark.parametrize("text,expected", CONTENT_TOKENIZE_CASES)
def test_content_tokenizer(text, expected):
    assert app.tokenize(text) == expected


@pytest.mark.parametrize("text,expected", FILENAME_TOKENIZE_CASES)
def test_filename_tokenizer(text, expected):
    assert app.tokenize_filename(text) == expected


def test_lowercase_expands_u0130():
    """Pin the length-changing lowercase that drives İstanbul handling."""

    assert "\u0130".lower() == "i\u0307"
    assert len("\u0130".lower()) == 2


def test_lowercase_applies_final_sigma_rule():
    """Pin Python's context-sensitive final sigma."""

    assert "ΣΣ".lower() == "σς"
    assert app.tokenize("ΣΣ") == ["σς"]


def test_whitespace_classification_matches_python():
    """Document exactly which characters Python treats as whitespace."""

    python_whitespace = [
        "\u00a0",  # NO-BREAK SPACE
        "\u2007",  # FIGURE SPACE
        "\u202f",  # NARROW NO-BREAK SPACE
        "\u0085",  # NEXT LINE
        "\u001c",  # INFORMATION SEPARATOR FOUR
        "\u001f",  # INFORMATION SEPARATOR ONE
        "\t",
        "\n",
        "\r",
    ]

    for character in python_whitespace:
        assert character.isspace(), f"U+{ord(character):04X} should be whitespace"

    not_whitespace = ["\u200b", "\u0307", "a", "½"]

    for character in not_whitespace:
        assert not character.isspace(), (
            f"U+{ord(character):04X} should not be whitespace"
        )


def test_alnum_classification_matches_python():
    """Python counts No and Nl numerics as alphanumeric."""

    alnum = ["½", "²", "Ⅻ", "५", "字", "Ω", "é", "İ"]

    for character in alnum:
        assert character.isalnum(), f"{character!r} should be alnum"

    not_alnum = ["\u200b", "\u0307", "!", " ", "\u00a0"]

    for character in not_alnum:
        assert not character.isalnum(), f"{character!r} should not be alnum"


# ----------------------------------------------------------------------
# Filename sanitizer
# ----------------------------------------------------------------------

SANITIZER_CASES = [
    # Normal names.
    ("a.txt", "a.txt"),
    ("My File.pdf", "My_File.pdf"),
    ("file.TXT", "file.TXT"),
    # Directory components are discarded.
    ("../../etc/passwd.txt", "passwd.txt"),
    ("..\\..\\x.txt", "x.txt"),
    ("a/b/c.txt", "c.txt"),
    ("/abs/path.txt", "path.txt"),
    # Leading dots are stripped.
    (".hidden.txt", "hidden.txt"),
    ("...hidden.txt", "hidden.txt"),
    # Multiple separators collapse to a single underscore.
    ("My  File...txt", "My_File...txt"),
    ("  spaced  .txt", "spaced_.txt"),
    # Non-ASCII is transliterated away rather than kept.
    ("café.txt", "cafe.txt"),
    ("Ünïcödé Ñämé.docx", "Unicode_Name.docx"),
    # An unsupported extension is rejected outright.
    ("no-ext", ""),
    ("noext.pdfx", ""),
    # A dotted stem is fine, the final extension decides.
    ("weird.pdf.txt", "weird.pdf.txt"),
    # Reserved device names are NOT rewritten on this platform.
    ("CON.txt", "CON.txt"),
    ("nul.pdf", "nul.pdf"),
    # Empty and falsy inputs.
    ("", ""),
]


@pytest.mark.parametrize("raw,expected", SANITIZER_CASES)
def test_filename_sanitizer(raw, expected):
    assert app.sanitize_upload_filename(raw) == expected


def test_sanitizer_rejects_unsupported_extensions():
    for raw in ("archive.zip", "book.epub", "image.png", "script.py", "noext"):
        assert app.sanitize_upload_filename(raw) == ""


def test_sanitizer_never_returns_a_path_separator():
    poisoned = [
        "../secret.txt",
        "..%2Fsecret.txt",
        "a/../../b.txt",
        "\\server\\share\\x.txt",
        "C:\\Windows\\system32\\evil.txt",
    ]

    for raw in poisoned:
        result = app.sanitize_upload_filename(raw)

        assert "/" not in result
        assert "\\" not in result
        assert ".." not in result


def test_sanitizer_preserves_identity_for_supported_extensions():
    for name in ("notes.txt", "report.pdf", "memo.docx"):
        assert app.sanitize_upload_filename(name) == name


# ----------------------------------------------------------------------
# Query normalization and filetype parsing
# ----------------------------------------------------------------------


def test_query_normalization_strips_supported_extension():
    assert app.normalize_search_query("BCS502 Module 2.pdf") == "BCS502 Module 2"
    assert app.normalize_search_query("BCS502 Module 2.docx") == "BCS502 Module 2"
    assert app.normalize_search_query("notes.txt") == "notes"
    assert app.normalize_search_query("plain query") == "plain query"


def test_query_normalization_preserves_quoting():
    assert app.normalize_search_query('"BCS502 Module 2.pdf"') == '"BCS502 Module 2"'


def test_query_normalization_leaves_unknown_extension():
    assert app.normalize_search_query("data.csv") == "data.csv"


FILETYPE_CASES = [
    ("pdf", ("", "pdf")),
    (".pdf", ("", "pdf")),
    ("network pdf", ("network", "pdf")),
    ("network .pdf", ("network", "pdf")),
    ("BCS502 Module 2.pdf", ("BCS502 Module 2", "pdf")),
    ('"BCS502 Module 2.pdf"', ('"BCS502 Module 2"', "pdf")),
    ("notes docx", ("notes", "docx")),
    ("notes txt", ("notes", "txt")),
    ("plain query", ("plain query", None)),
    ("", ("", None)),
]


@pytest.mark.parametrize("raw,expected", FILETYPE_CASES)
def test_filetype_filter_parsing(raw, expected):
    assert app.parse_filetype_filter(raw) == expected
