"""
Upload filename sanitization.

The body of `sanitize_upload_filename` is the one extracted verbatim from
app.py. Its helper is not: `secure_filename` used to be imported from
`werkzeug`, which is a WSGI/HTTP library, and this package is supposed to
be free of the web stack so the offline backend can reuse it unchanged.

The helper below is a transcription of werkzeug's `secure_filename`,
including its exact order of operations, its regex and its Windows
device-name check. Transcription rather than reinvention: the behaviour
is pinned by the golden vectors, by `tools/shadow_parity.py` against the
monolith, and by a differential test that compares this implementation
against werkzeug's over a large adversarial corpus
(`tests/test_sanitizer_independence.py`).

Two host-dependent lines are kept exactly as they were, because changing
them would change stored document names:

* `os.sep` and `os.path.altsep` decide which characters become spaces,
  so a Windows host sanitizes a backslash differently from a POSIX one;
* `os.name == "nt"` gates the device-name check.

The Kotlin port must reproduce the POSIX answers, because that is what
the shipped engine produces on its deployment platform.
"""

import os
import re
import unicodedata

# Every character that survives sanitization. Anything else is removed
# rather than replaced, which is why a JSON blob in a filename collapses
# into a single token instead of growing underscores.
_FILENAME_ASCII_STRIP_RE = re.compile(r"[^A-Za-z0-9_.-]")

# Windows reserves these names in every directory. A file called
# `NUL.txt` cannot be created there.
_WINDOWS_DEVICE_FILES = frozenset({
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "COM\u00b9",
    "COM\u00b2",
    "COM\u00b3",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
    "LPT\u00b9",
    "LPT\u00b2",
    "LPT\u00b3",
    "CONIN$",
    "CONOUT$",
})


def _secure_filename(filename):
    """
    Return an ASCII-only, filesystem-safe version of `filename`.

    Transcribed from `werkzeug.utils.secure_filename`, including the
    NFKD decomposition that turns `Ü` into `U` plus a dropped combining
    mark, the replacement of both path separators with spaces, the
    underscore-joining of whitespace runs, and the trailing dot/underscore
    strip. The result may be empty; the caller decides what that means.
    """

    filename = unicodedata.normalize(
        "NFKD",
        filename,
    )

    filename = filename.encode(
        "ascii",
        "ignore",
    ).decode("ascii")

    for separator in (os.sep, os.path.altsep):

        if separator:
            filename = filename.replace(separator, " ")

    filename = str(
        _FILENAME_ASCII_STRIP_RE.sub(
            "",
            "_".join(filename.split()),
        )
    ).strip("._")

    if (
        os.name == "nt"
        and filename
        and filename.split(".")[0].upper() in _WINDOWS_DEVICE_FILES
    ):
        filename = f"_{filename}"

    return filename


def sanitize_upload_filename(filename):
    """
    Return a safe local filename for an uploaded document.

    Only the final filename is stored in DATA_FOLDER. Directory
    components are removed to prevent path traversal.

    Returns an empty string when the filename is invalid or the
    extension is unsupported.
    """

    if not filename:
        return ""

    safe_name = _secure_filename(
        os.path.basename(filename)
    )

    if not safe_name:
        return ""

    supported_extensions = (
        ".pdf",
        ".docx",
        ".txt",
    )

    if not safe_name.lower().endswith(
        supported_extensions
    ):
        return ""

    return safe_name
