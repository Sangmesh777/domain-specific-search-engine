"""
The core must import and run with the web stack absent.

`search_engine` is the transport-independent core: the same code has to
serve the Flask API and the offline Android backend, so it cannot depend
on Flask, Werkzeug, `requests`, `http` or `urllib.request`. An import
scan proves the modules do not *name* those packages, but it cannot
prove they would still work without them. These tests block the imports
outright, in a subprocess, and then import and exercise the package.

`urllib.parse` is deliberately allowed. It is percent-encoding of a
string, it has no transport behaviour, and the monolith used
`quote()` for the same URLs. Reimplementing percent-encoding by hand
would add risk and change nothing; `urllib.request` and `urllib.error`
are blocked, so nothing can reach the network through the allowance.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

BLOCKED = (
    "flask",
    "werkzeug",
    "requests",
    "http",
    "aiohttp",
    "urllib.request",
    "urllib.error",
    "socket",
)

PRELUDE = f"""
import sys

BLOCKED = {BLOCKED!r}

class Blocker:
    def find_module(self, name, path=None):
        root = name.split(".")[0]
        if name in BLOCKED or root in BLOCKED:
            return self
        return None

    def load_module(self, name):
        raise ImportError(
            "blocked by the transport-free test: " + name
        )

sys.meta_path.insert(0, Blocker())

# A stray already-imported copy would defeat the point.
for name in list(sys.modules):
    if name.split(".")[0] in BLOCKED:
        del sys.modules[name]
"""


def run_blocked(body):
    """Run `body` in a subprocess with the web stack unimportable."""

    script = PRELUDE + "\n" + body

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )

    return result


def test_every_core_module_imports_with_the_web_stack_blocked():
    """The whole package, not just the engine."""

    result = run_blocked(
        """
import importlib
import pathlib

names = sorted(
    p.stem
    for p in pathlib.Path("search_engine").glob("*.py")
    if p.stem != "__init__"
)

for name in names:
    importlib.import_module("search_engine." + name)

print("IMPORTED:", ",".join(names))
"""
    )

    assert result.returncode == 0, result.stderr

    assert "IMPORTED:" in result.stdout, result.stdout

    imported = result.stdout.split("IMPORTED:")[1].strip().split(",")

    # If a module were renamed or added, the list would move and this
    # would fail, which is wanted: a new core module must be checked.
    assert imported == [
        "engine",
        "extract",
        "index_state",
        "indexing",
        "sanitize",
        "snippet",
        "storage",
        "text",
    ], imported


def test_the_engine_produces_a_payload_with_the_web_stack_blocked():
    """
    Import is not enough; the ranking pipeline has to run.

    A document, a query and the resulting payload, with no Flask, no
    request context and no test client anywhere in the process.
    """

    result = run_blocked(
        """
import json

from search_engine.engine import search_index

inverted_index = {"alpha": {"a.txt": 2}, "beta": {"b.txt": 1}}
document_metadata = {
    "a.txt": {"title": "a.txt", "path": "/tmp/a.txt", "total_words": 2},
    "b.txt": {"title": "b.txt", "path": "/tmp/b.txt", "total_words": 1},
}
filename_index = {"a.txt": ["a"], "b.txt": ["b"]}
pages = {}

payload = search_index(
    "alpha",
    None,
    None,
    inverted_index,
    document_metadata,
    filename_index,
    pages,
)

print("JSON:" + json.dumps(payload, sort_keys=True))
"""
    )

    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout.split("JSON:", 1)[1])

    assert set(payload) == {"results", "pagination"}

    assert payload["pagination"]["total"] == 1

    assert payload["results"][0]["document_url"].endswith("a.txt")


def test_the_sanitizer_runs_with_the_web_stack_blocked():
    """
    The specific regression this session fixed.

    `sanitize.py` used to import `werkzeug.utils.secure_filename`, so
    this call used to raise ImportError. The assertion on the value is
    secondary; the point is that the call happens at all.
    """

    result = run_blocked(
        """
from search_engine.sanitize import sanitize_upload_filename

print("A:" + sanitize_upload_filename("My File.pdf"))
print("B:" + sanitize_upload_filename("dir\\\\sub\\\\file.txt"))
print("C:" + sanitize_upload_filename("../../etc/passwd.txt"))
print("D:" + repr(sanitize_upload_filename("archive.zip")))
"""
    )

    assert result.returncode == 0, result.stderr

    assert "A:My_File.pdf" in result.stdout

    # The backslash is not a separator on POSIX; it is deleted by the
    # character filter, so the whole name survives as one token.
    assert "B:dirsubfile.txt" in result.stdout

    assert "C:passwd.txt" in result.stdout

    assert "D:''" in result.stdout


def test_the_blocker_would_actually_block_something():
    """
    Guard the guard.

    If the import hook were ineffective, all three tests above would
    pass while proving nothing, because an ordinary interpreter would
    import these modules happily.
    """

    result = run_blocked(
        """
try:
    import flask
except ImportError as error:
    print("BLOCKED:" + str(error))
else:
    print("NOT BLOCKED")
"""
    )

    assert result.returncode == 0, result.stderr

    assert "BLOCKED:" in result.stdout, result.stdout

    assert "NOT BLOCKED" not in result.stdout.split("BLOCKED:")[0]
