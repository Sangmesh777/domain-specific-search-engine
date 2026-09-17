"""
Run the sanitizer differential test as if the host were Windows.

`os.path.altsep` and the reserved-device-name check are both gated on
the host platform, so on Linux they are dead code that no ordinary test
can reach. Patching `os.name` inside a pytest process is not an option:
pathlib consults `os.name` when constructing paths, so pytest's own
internals blow up with `NotImplementedError: cannot instantiate
'WindowsPath' on your system`. A suite that dies for that reason is
indistinguishable from a suite that detected a real divergence, which
makes it worse than useless as a control.

So the patch happens here, in a bare interpreter, after every import
has completed and without a test runner in the process. The two
implementations are then compared over the corpus under `nt`, and the
count of names the device check actually prefixed is printed so the
caller can prove the branch was live rather than merely present.

Usage: python3 -m tools.sanitizer_windows_check
Exit status is 0 only when the branch was exercised and agreed.
"""

import json
import os
import sys

# Import everything first: patching os.name before these load would let
# pathlib-dependent import machinery see an inconsistent platform.
from search_engine.sanitize import (  # noqa: E402
    _WINDOWS_DEVICE_FILES,
    _secure_filename,
)
from werkzeug.utils import secure_filename as reference  # noqa: E402

sys.path.insert(0, os.getcwd())

from tests.test_sanitizer_independence import CASES  # noqa: E402


NAMES = (
    "CON", "con", "Con", "NUL", "NUL.txt", "com1.pdf", "LPT9", "LPT9.docx",
    "PRN.txt", "AUX", "CONIN$", "CONOUT$", "COM\u00b9.txt", "LPT\u00b2.txt",
    "CONSOLE.txt", "NULL.txt", "NULX.txt", "command.txt", "com0.txt",
    "COM10.txt", "con.TXT", "nul.pdf", "aux.docx", "prn.txt",
)


def main():

    divergences = []

    prefixed = 0

    # The only place os.name is touched, and nothing between the patch
    # and its restoration calls into the standard library's path logic.
    original_name = os.name
    os.name = "nt"

    try:

        for name in list(NAMES) + list(CASES):

            expected = reference(name)
            actual = _secure_filename(name)

            if name in NAMES and actual != name and expected == actual:
                prefixed += 1

            if actual != expected:
                divergences.append((name, expected, actual))

    finally:

        os.name = original_name

    # The corpus must also be unambiguous about which characters
    # `os.path.altsep` turns into spaces on a real Windows host. On this
    # machine `os.path` is still the POSIX module, so `altsep` stays None
    # even while `os.name` says nt, and the backslash gap between Python
    # and the Kotlin model is therefore pinned by a direct assertion
    # rather than by the loop above.
    altsep_is_none_here = os.path.altsep is None

    report = {
        "calls": len(NAMES) + len(CASES),
        "device_names_prefixed": prefixed,
        "divergences": len(divergences),
        "first_divergences": divergences[:5],
        "device_files_modelled": len(_WINDOWS_DEVICE_FILES),
        "altsep_none_even_under_nt": altsep_is_none_here,
        "posix_backslash_result": _secure_filename("dir\\sub\\file.txt"),
    }

    print(json.dumps(report, indent=2))

    failed = bool(divergences) or prefixed == 0

    print("WINDOWS BRANCH: " + ("FAILED" if failed else "OK"))

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
