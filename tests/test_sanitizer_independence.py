"""
Differential test: our transcribed sanitizer vs werkzeug's real one.

This is the alarm on the dependency removal. `search_engine` must not
import the web stack, so `werkzeug.utils.secure_filename` was transcribed
into `search_engine/sanitize.py`. A transcription is only safe if it is
provably identical, so this test imports the original (available as a
test-only dependency) and compares the two over a corpus built to hit
every branch: the NFKD rounding trip, both separators, whitespace
collapsing, the character strip, the trailing `._` strip, the Windows
device check, and empty results.

If werkzeug is ever missing this test skips rather than passing silently,
and the skip is reported.
"""

import json
import os
import pathlib
import string
import subprocess
import sys

import pytest

from search_engine.sanitize import (
    _secure_filename,
    sanitize_upload_filename,
)

werkzeug = pytest.importorskip(
    "werkzeug.utils",
    reason="werkzeug is a test-only dependency used as the reference",
)

reference = werkzeug.secure_filename


def _adversarial_corpus():
    """Return the list of filenames both implementations must agree on."""

    cases = []

    # Every printable ASCII character, alone and inside a filename.
    for code in range(0x20, 0x7F):
        character = chr(code)
        cases.append(character)
        cases.append(f"a{character}b.txt")

    # Every ASCII control character that can appear in a filename.
    for code in range(0x00, 0x20):
        cases.append(f"a{chr(code)}b.txt")

    # Path shapes: both separators, traversal, absolute and mixed.
    cases.extend([
        "a/b.txt",
        "a\\b.txt",
        "dir/sub/file.txt",
        "dir\\sub\\file.txt",
        "..\\..\\etc\\passwd.txt",
        "../../../etc/passwd.txt",
        "/etc/passwd.txt",
        "C:\\Users\\me\\report.pdf",
        "\\\\server\\share\\doc.docx",
        "dir\\..\\..\\x.txt",
        "\\leading.txt",
        "trailing\\",
        "/",
        "\\",
        "//",
        "\\\\",
        "a//b.txt",
        "a\\\\b.txt",
    ])

    # Leading dot files and dot-only names: the `.strip("._")` tail.
    cases.extend([
        ".hidden.txt",
        "..",
        ".",
        "...",
        "....txt",
        "._.txt",
        "_leading.txt",
        "trailing_",
        "trailing..txt",
        ".-_.-_.txt",
        ".",
        "__init__.py",
    ])

    # Whitespace handling: `split()` collapses runs to underscores.
    cases.extend([
        "  spaced  .txt",
        "tab\there.txt",
        "new\nline.txt",
        "carriage\rreturn.txt",
        "vertical\x0btab.txt",
        "form\x0cfeed.txt",
        "many       spaces.txt",
        " \t\n\r\x0b\x0c ",
        "a\u00a0b.txt",
        "a\u2003b.txt",
        "a\u3000b.txt",
    ])

    # NFKD cases: decomposable characters, combining marks, ligatures,
    # fullwidth forms, and characters that decompose to nothing.
    cases.extend([
        "Ünïcödé Ñämé.docx",
        "i contain cool ümläuts.txt",
        "ünïcödé_ñämé.PDF",
        "ﬁle.txt",
        "①one.txt",
        "ｆｕｌｌｗｉｄｔｈ.txt",
        "half½.txt",
        "ÅÄÖ åäö.txt",
        "e\u0301acute.txt",
        "İstanbul.txt",
        "ß.txt",
        "æøå.txt",
        "emoji\U0001f600.txt",
        "cjk\u4e2d\u6587.txt",
        "rtl\u05d0\u05d1.txt",
        "\ufeffbom.txt",
        "zero\u200bwidth.txt",
    ])

    # Windows device names, in the case forms that must be caught.
    for stem in ("CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9",
                 "CONIN$", "CONOUT$"):
        cases.append(f"{stem}.txt")
        cases.append(f"{stem.lower()}.txt")
        cases.append(stem)
        cases.append(f"{stem}.pdf")

    # Extensions the caller accepts or rejects.
    cases.extend([
        "file.pdf",
        "file.PDF",
        "file.PdF",
        "file.docx",
        "file.txt",
        "file.TXT",
        "file.exe",
        "no_extension",
        "name.pdf.exe",
        ".pdf",
        "pdf",
        "file.pdf.docx",
        "file.docx.pdf",
    ])

    # Length: long repeats and single very long runs.
    cases.extend([
        "x" * 300 + ".txt",
        ("ünïcödé" * 40) + ".pdf",
        ("a." * 100) + "txt",
        " " * 200 + ".txt",
        "." * 200,
        "_" * 200,
    ])

    # Miscellaneous punctuation soup.
    cases.extend([
        "a!b.txt",
        "semi;colon.txt",
        "quote'quote.txt",
        'dq"dq.txt',
        "pipe|pipe.txt",
        "back`tick.txt",
        "star*.txt",
        "question?.txt",
        "lt<gt>.txt",
        "colon:name.txt",
        "brace{s}.txt",
        "bracket[s].txt",
        "paren(s).txt",
        "plus+plus.txt",
        "equals=equals.txt",
        "at@sign.txt",
        "hash#hash.txt",
        "dollar$dollar.txt",
        "percent%percent.txt",
        "caret^caret.txt",
        "tilde~tilde.txt",
        "amp&and.txt",
        "comma,comma.txt",
        "bang!.pdf",
    ])

    # Every printable ASCII string of length 1 and 2 appended to a name,
    # to catch ordering mistakes in the collapse-then-strip pipeline.
    alphabet = string.ascii_letters + string.digits + "._- /\\!@#"
    for first in alphabet:
        for second in alphabet:
            cases.append(f"{first}{second}.txt")

    # Empty and degenerate inputs.
    cases.extend(["", " ", "_", "-", ".", "/", "\\", ".txt", "..txt"])

    # Every Unicode codepoint that normalizes to something surprising is
    # impractical to enumerate, so sample the planes systematically with
    # the separators embedded, which is the operation the OS gate affects.
    for code in range(0x00, 0x300):
        cases.append(f"a{chr(code)}b.txt")

    return cases


CASES = _adversarial_corpus()


def test_the_corpus_is_wide_enough_to_mean_something():
    """Guard against the corpus being emptied or truncated by accident."""

    assert len(CASES) >= 4000, len(CASES)
    assert len(set(CASES)) >= 4000, len(set(CASES))


def test_transcription_matches_werkzeug_on_the_whole_corpus():
    """The core claim: our Flask-free copy is behaviourally identical."""

    divergences = []

    for case in CASES:

        expected = reference(
            case
        )

        actual = _secure_filename(
            case
        )

        if actual != expected:
            divergences.append((case, expected, actual))

    assert not divergences, (
        f"{len(divergences)} divergences from werkzeug in "
        f"{len(CASES)} cases, first five: {divergences[:5]}"
    )


def test_the_public_sanitizer_matches_werkzeug_on_the_whole_corpus():
    """The public function, including the extension gate, is unchanged."""

    divergences = []

    for case in CASES:

        expected = reference(
            os.path.basename(case)
        )

        if not expected.lower().endswith(
            (".pdf", ".docx", ".txt")
        ):
            expected = ""

        actual = sanitize_upload_filename(
            case
        )

        if actual != expected:
            divergences.append((case, expected, actual))

    assert not divergences, (
        f"{len(divergences)} divergences in {len(CASES)} cases, "
        f"first five: {divergences[:5]}"
    )


def test_the_windows_branch_is_exercised_in_a_clean_interpreter():
    """
    Drive the reserved-device-name branch without breaking the runner.

    Patching `os.name` inside this process makes pathlib raise
    `NotImplementedError: cannot instantiate 'WindowsPath'`, which kills
    pytest with an INTERNALERROR. A suite that dies for that unrelated
    reason is indistinguishable from one that found a real divergence,
    so any control built on top of it reports a detection that never
    happened. The check therefore runs in a bare interpreter instead,
    and this test reads its verdict.
    """

    result = subprocess.run(
        [sys.executable, "-m", "tools.sanitizer_windows_check"],
        capture_output=True,
        text=True,
        cwd=str(pathlib.Path(__file__).resolve().parent.parent),
    )

    assert result.returncode == 0, result.stdout + result.stderr

    report = json.loads(
        result.stdout[: result.stdout.index("}") + 1]
    )

    assert report["divergences"] == 0, report["first_divergences"]

    # Proof the branch was live: if a future refactor deletes the device
    # check, `prefixed` collapses to zero and this fails even though the
    # comparison loop itself would still agree.
    assert report["device_names_prefixed"] > 0, report

    assert report["calls"] > 6000, report


def test_the_posix_backslash_answer_is_pinned():
    """
    Python keeps backslashes that a Windows host would convert.

    On POSIX, `os.path.altsep` is `None`, so `\\` is not turned into a
    space and instead falls to the character strip and is deleted. The
    Kotlin port must copy these answers, not the answers a Windows host
    would give, because the server this has to agree with runs POSIX.
    """

    assert _secure_filename("dir\\sub\\file.txt") == "dirsubfile.txt"
    assert reference("dir\\sub\\file.txt") == "dirsubfile.txt"

    assert sanitize_upload_filename("dir\\sub\\file.txt") == "dirsubfile.txt"

    # Under `nt` the very same input becomes `dir_sub_file.txt`, which is
    # why the port cannot simply reuse a Windows-shaped basename helper.
    if os.path.altsep is not None:
        assert _secure_filename("dir\\sub\\file.txt") == "dir_sub_file.txt"


def test_normalization_actually_removes_combining_marks():
    """
    The NFKD step is load-bearing, not decorative.

    A naive `encode("ascii", "ignore")` without the normalization would
    drop `ü` entirely and produce `ab.txt`; with it, the mark is dropped
    and the base letter survives.
    """

    assert _secure_filename("Ünïcödé Ñämé.docx") == "Unicode_Name.docx"
    assert reference("Ünïcödé Ñämé.docx") == "Unicode_Name.docx"

    # Without NFKD these would vanish instead of degrading to a letter.
    assert _secure_filename("ü.txt") == "u.txt"
    assert _secure_filename("é.txt") == "e.txt"
    assert _secure_filename("ñ.txt") == "n.txt"
    assert _secure_filename("\ufb01le.txt") == "file.txt"
    assert _secure_filename("\u2460one.txt") == "1one.txt"

    # Characters with no ASCII decomposition do vanish, as they must.
    # The trailing strip then removes the now-leading dot, so a filename
    # written entirely in a non-ASCII script loses even its extension
    # and is rejected by the caller. That is the shipped behaviour, kept
    # deliberately rather than quietly "improved".
    assert _secure_filename("\u4e2d\u6587.txt") == "txt"
    assert sanitize_upload_filename("\u4e2d\u6587.txt") == ""
