"""
Filename safety.

Uploaded and imported documents are stored flat inside the engine's data
folder, so a filename must never carry a directory component or escape the
data folder.

`secure_filename` is vendored here, behavior-for-behavior, from
`werkzeug.utils.secure_filename` so that the core has no dependency on the
web stack. The Flask adapter and an offline Android local backend therefore
sanitize identically on the same platform. `tests/test_filename_safety.py`
asserts parity against the Werkzeug original across a broad input corpus
instead of trusting the vendoring.

The platform-dependent branches are preserved on purpose: separator
handling uses `os.sep` / `os.altsep` and the reserved-device-name guard uses
`os.name`, exactly as the original does, so behavior stays identical on
Windows, Linux and Android (which is Linux-based).
"""

import os
import re
import unicodedata

from search_engine.config import (
    SUPPORTED_EXTENSIONS,
)


_FILENAME_ASCII_STRIP_RE = re.compile(
    r"[^A-Za-z0-9_.-]"
)


_WINDOWS_DEVICE_FILES = frozenset((
    "AUX",
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
    "CON",
    "CONIN$",
    "CONOUT$",
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
    "NUL",
    "PRN",
))


def secure_filename(filename):
    """
    Return an ASCII-only filesystem-safe version of filename.

    Mirrors werkzeug.utils.secure_filename. May return an empty string,
    which callers must treat as "reject".
    """

    filename = unicodedata.normalize(
        "NFKD",
        filename,
    )

    filename = filename.encode(
        "ascii",
        "ignore",
    ).decode("ascii")

    for separator in os.sep, os.altsep:

        if separator:
            filename = filename.replace(
                separator,
                " ",
            )

    filename = str(
        _FILENAME_ASCII_STRIP_RE.sub(
            "",
            "_".join(filename.split()),
        )
    ).strip("._")

    # On Windows a handful of reserved device names exist in every folder.
    # Prepend an underscore so the target file is never one of them.
    if (
        os.name == "nt"
        and filename
        and filename.split(".")[0].upper()
        in _WINDOWS_DEVICE_FILES
    ):
        filename = f"_{filename}"

    return filename


def sanitize_upload_filename(filename):
    """
    Return a safe local filename for an uploaded document.

    Only the final filename is stored in the data folder. Directory
    components are removed to prevent path traversal.

    Returns an empty string when the filename is invalid or the
    extension is unsupported.
    """

    if not filename:
        return ""

    safe_name = secure_filename(
        os.path.basename(filename)
    )

    if not safe_name:
        return ""

    if not safe_name.lower().endswith(
        SUPPORTED_EXTENSIONS
    ):
        return ""

    return safe_name


def normalize_requested_filenames(filenames):
    """
    Reduce a requested filename list to unique, safe base names.

    Order is preserved and duplicates are dropped, so a bulk operation
    reports each document once. Non-string entries are ignored.
    """

    normalized = []

    for filename in filenames:

        if not isinstance(filename, str):
            continue

        safe_filename = os.path.basename(
            filename.strip()
        )

        if (
            safe_filename
            and safe_filename not in normalized
        ):
            normalized.append(safe_filename)

    return normalized
