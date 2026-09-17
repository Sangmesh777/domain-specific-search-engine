"""
Executable model of the Kotlin parity ports.

The Kotlin sources under ``android/harness`` cannot be compiled in this
environment: there is no JDK, no Android SDK and no Gradle, and the
network blocks every host that could supply them.

That does not mean the port's *algorithms* have to go unverified. This
module is a line-by-line Python model of the Kotlin logic in

    android/harness/src/main/kotlin/com/vtu/search/parity/PythonText.kt
    android/harness/src/main/kotlin/com/vtu/search/parity/SecureFilename.kt

using Java's `Character` predicate semantics rather than Python's, so
that `tests/test_port_model.py` can prove the algorithms reproduce every
golden contract vector.

If this model passes and the Kotlin is a faithful transcription of it,
the algorithm is correct. What remains unverified is only the Kotlin
syntax and the Java standard library behaviour, not the design.

Java predicate semantics are modelled from the Unicode general category:

    Character.isLetter(c)     category starts with "L"
    Character.isDigit(c)      category == "Nd"
    Character.isSpaceChar(c)  category in {"Zs", "Zl", "Zp"}
    Character.isWhitespace(c) category in {"Zs","Zl","Zp"} except the
                              non-breaking spaces, plus the ASCII control
                              whitespace

WARNING: this model uses Python's Unicode tables
-----------------------------------------------
Deriving Java's categories from `unicodedata` is circular: it can only
confirm that the mapping is self-consistent, not that it matches a real
JVM. An earlier version of this module claimed "zero mismatches across
all 1,114,112 code points" on that basis, and a real JVM disproved part
of it.

`tools/verify_jvm_semantics.py` is the authority. It executes the real
`java.lang.Character` methods and found 9,392 differences, every one of
them a code point unassigned in Python's Unicode 14.0.0 but assigned in
the JVM's Unicode 16. That is version skew, not a porting defect, and it
is measured rather than assumed away.

This model remains useful for two things:

  * it pins the *predicate composition*, which the JVM verification
    confirms is correct, and
  * it lets the contract vectors be replayed without a JVM.

It must not be treated as evidence about Java's Unicode tables.
"""

import unicodedata

NON_BREAKING_SPACES = (0x00A0, 0x2007, 0x202F)

ASCII_CONTROL_WHITESPACE = (0x09, 0x0A, 0x0B, 0x0C, 0x0D)

PYTHON_NEXT_LINE = 0x0085


def general_category(code_point):
    return unicodedata.category(chr(code_point))


def java_is_letter(code_point):
    return general_category(code_point).startswith("L")


def java_is_digit(code_point):
    return general_category(code_point) == "Nd"


def java_is_space_char(code_point):
    return general_category(code_point) in ("Zs", "Zl", "Zp")


def java_is_whitespace(code_point):
    category = general_category(code_point)

    if category in ("Zs", "Zl", "Zp") and code_point not in NON_BREAKING_SPACES:
        return True

    if code_point in ASCII_CONTROL_WHITESPACE:
        return True

    return 0x1C <= code_point <= 0x1F


def is_python_alnum(code_point):
    """Model of PythonText.isPythonAlnum."""

    return (
        java_is_letter(code_point)
        or java_is_digit(code_point)
        or general_category(code_point) in ("Nl", "No")
    )


def is_python_space(code_point):
    """Model of PythonText.isPythonSpace."""

    return (
        java_is_whitespace(code_point)
        or java_is_space_char(code_point)
        or code_point == PYTHON_NEXT_LINE
    )


def clean(text):
    """Lowercase the whole string, then neutralise disallowed characters."""

    lowered = text.lower()

    return "".join(
        character
        if is_python_alnum(ord(character)) or is_python_space(ord(character))
        else " "
        for character in lowered
    )


def split_python_whitespace(text):
    """Model of PythonText.splitPythonWhitespace."""

    tokens = []
    current = []

    for character in text:
        if is_python_space(ord(character)):
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(character)

    if current:
        tokens.append("".join(current))

    return tokens


def tokenize(text):
    """Model of PythonText.tokenize, counting characters not UTF-16 units."""

    return [
        token
        for token in split_python_whitespace(clean(text))
        if len(token) > 1
    ]


def tokenize_filename(text):
    """Model of PythonText.tokenizeFilename."""

    return split_python_whitespace(clean(text))


# ----------------------------------------------------------------------
# SecureFilename
# ----------------------------------------------------------------------

ALLOWED_ASCII = (
    set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    | set("abcdefghijklmnopqrstuvwxyz")
    | set("0123456789")
    | {"_", ".", "-"}
)

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".txt")


def to_ascii(value):
    """NFKD-normalise and keep only ASCII characters."""

    normalised = unicodedata.normalize("NFKD", value)

    return "".join(
        character
        for character in normalised
        if 1 <= ord(character) <= 0x7F
    )


def basename(filename):
    """
    Model of SecureFilename.basename, i.e. `os.path.basename` on POSIX.

    Only `/` separates. A backslash is an ordinary character here, not a
    directory separator: `os.path.altsep` is `None` on POSIX, so
    `os.path.basename("dir\\sub\\file.txt")` returns the whole string.
    It is then `secure_filename` that deletes the backslash, giving
    `dirsubfile.txt` for that input.

    This used to split on `\\` as well, on the reasonable-sounding
    grounds that "an upload may arrive from any client". That made the
    model disagree with the engine for every Windows-shaped name, so the
    Kotlin port would have stored `file.txt` where the server stores
    `dirsubfile.txt`. The single recorded backslash vector,
    `..\\..\\x.txt`, returns `x.txt` either way, so the vectors could
    not see it. `tests/test_port_model_matches_engine.py` compares the
    model to the engine directly for that reason.
    """

    end = len(filename)

    while end > 0:
        if filename[end - 1] == "/":
            break
        end -= 1

    return filename[end:]


def secure_filename(filename):
    """Model of SecureFilename.secureFilename."""

    if not filename:
        return ""

    ascii_only = to_ascii(filename).replace("/", " ")

    joined = "_".join(split_python_whitespace(ascii_only))

    filtered = "".join(
        character
        for character in joined
        if character in ALLOWED_ASCII
    )

    return filtered.strip("._")


def has_supported_extension(filename):
    lowered = filename.lower()
    return any(lowered.endswith(ext) for ext in SUPPORTED_EXTENSIONS)


def sanitize(filename):
    """Model of SecureFilename.sanitize."""

    if not filename:
        return ""

    safe_name = secure_filename(basename(filename))

    if not safe_name:
        return ""

    if not has_supported_extension(safe_name):
        return ""

    return safe_name
