"""
Negative controls for the sanitizer transcription.

`search_engine/sanitize.py` no longer imports werkzeug, so its
correctness rests on `tests/test_sanitizer_independence.py` comparing it
against werkzeug over a corpus. A differential test is only worth
having if it fails when the copy drifts, so each control below applies
one plausible mistake to the transcription and requires the suite to
notice.

Two rules this script obeys, both learned the hard way:

* every patch asserts its anchor matched and that the file digest
  actually changed, so a silently unapplied patch cannot be reported
  as a detected control;
* a control that is unobservable on this platform is reported as
  "not observable", which is different from "detected". Dropping
  `os.path.altsep` from the separator loop is the canonical example:
  it is `None` on POSIX, so no test on this host can ever see it, and
  claiming detection would be a lie.

Usage: python3 -m tools.sanitizer_controls
"""

import hashlib
import pathlib
import subprocess
import sys

TARGET = pathlib.Path("search_engine/sanitize.py")

SUITE = "tests/test_sanitizer_independence.py"

CONTROLS = [
    (
        "drop the NFKD normalization",
        """    filename = unicodedata.normalize(
        "NFKD",
        filename,
    )
""",
        "",
        "observable",
    ),
    (
        "drop the trailing dot/underscore strip",
        """).strip("._")""",
        """)""",
        "observable",
    ),
    (
        "join whitespace runs with nothing instead of an underscore",
        '''"_".join(filename.split())''',
        '''"".join(filename.split())''',
        "observable",
    ),
    (
        "remove the dot from the allowed character class",
        '''_FILENAME_ASCII_STRIP_RE = re.compile(r"[^A-Za-z0-9_.-]")''',
        '''_FILENAME_ASCII_STRIP_RE = re.compile(r"[^A-Za-z0-9_-]")''',
        "observable",
    ),
    (
        "remove the hyphen from the allowed character class",
        '''_FILENAME_ASCII_STRIP_RE = re.compile(r"[^A-Za-z0-9_.-]")''',
        '''_FILENAME_ASCII_STRIP_RE = re.compile(r"[^A-Za-z0-9_.]")''',
        "observable",
    ),
    (
        "skip the ASCII-only decoding step",
        """    filename = filename.encode(
        "ascii",
        "ignore",
    ).decode("ascii")
""",
        "",
        "observable",
    ),
    (
        "case-fold the device-name lookup away",
        """filename.split(".")[0].upper() in _WINDOWS_DEVICE_FILES""",
        """filename.split(".")[0] in _WINDOWS_DEVICE_FILES""",
        "observable-on-nt",
    ),
    (
        "drop os.path.altsep from the separator loop",
        """    for separator in (os.sep, os.path.altsep):""",
        """    for separator in (os.sep,):""",
        "platform-dependent",
    ),
    (
        "accept a prefix instead of the final extension",
        """    if not safe_name.lower().endswith(
        supported_extensions
    ):""",
        """    if not any(
        extension in safe_name.lower()
        for extension in supported_extensions
    ):""",
        "observable",
    ),
    (
        "stop rejecting empty names",
        """    if not safe_name:
        return ""
""",
        "",
        "equivalent",
    ),
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def run_suite():
    """
    Return (verdict, summary) where verdict is True, False, or None.

    True  - the suite failed, so the control was detected
    False - the suite passed, so the control slipped through
    None  - the suite did not run to completion, so nothing is proven

    The distinction is made from pytest's exit status, not from its
    output. Two earlier versions of this function scanned the text:
    one counted a broken `os.name` monkeypatch (which made pytest die
    with INTERNALERROR) as seven detections, and its replacement then
    matched the word INTERNALERROR inside this suite's own docstrings
    and reported a false invalid run on every legitimate failure.
    Exit codes cannot be spoofed by a traceback that quotes source.
    """

    result = subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-q", "--no-header"],
        capture_output=True,
        text=True,
    )

    lines = [
        line for line in result.stdout.strip().splitlines() if line.strip()
    ]

    summary = lines[-1] if lines else "<no output>"

    if result.returncode == 1:
        return True, summary

    if result.returncode == 0:
        return False, summary

    return None, f"exit {result.returncode}: {summary}"


def main():
    original = TARGET.read_text()
    original_digest = digest(TARGET)

    print(f"baseline {TARGET} sha256:{original_digest}")

    baseline_failed, summary = run_suite()

    if baseline_failed:
        print(f"ABORT: the suite fails before any control is applied: {summary}")
        return 2

    print(f"baseline suite passes: {summary}\n")

    failures = []

    for index, (name, old, new, observability) in enumerate(CONTROLS, start=1):

        if original.count(old) != 1:
            print(
                f"[{index}] ANCHOR MISSING x{original.count(old)}: {name}"
            )
            failures.append(name)
            continue

        patched = original.replace(old, new)

        if patched == original:
            print(f"[{index}] PATCH WAS A NO-OP: {name}")
            failures.append(name)
            continue

        TARGET.write_text(patched)

        patched_digest = digest(TARGET)

        if patched_digest == original_digest:
            print(f"[{index}] DIGEST UNCHANGED, patch did not land: {name}")
            failures.append(name)
            continue

        detected, summary = run_suite()

        print(f"      suite {'failed' if detected else 'passed'} -> {summary}")

        TARGET.write_text(original)

        if digest(TARGET) != original_digest:
            print("ABORT: restore failed, refusing to continue")
            return 3

        if detected is None:
            print(f"[{index}] INVALID RUN: {name}\n      {summary}")
            failures.append(name)
            continue

        if detected:
            mark = "DETECTED"
        elif observability == "platform-dependent":
            mark = "invisible on POSIX"
        elif observability == "equivalent":
            mark = "invisible, rewrite is equivalent"
        else:
            mark = "NOT DETECTED"
            failures.append(name)

        print(f"[{index}] {mark:>19} ({observability}): {name}")

    print(f"\nrestored {TARGET} sha256:{digest(TARGET)}")
    print(f"controls: {len(CONTROLS)}   undetected: {len(failures)}")

    if failures:
        print("FAILED CONTROLS:")
        for name in failures:
            print(f"  - {name}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
