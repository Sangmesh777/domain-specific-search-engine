"""
Upload filename sanitization.

Extracted verbatim from app.py by tools/extract_modules.py.
Do not retype these functions: the bodies must stay byte
identical to the engine that the golden vectors pin.
"""

import os
from werkzeug.utils import secure_filename
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

    safe_name = secure_filename(
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
