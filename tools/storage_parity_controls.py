"""
Prove that the storage-parity gate can actually fail.

A parity tool that always reports "0 differences" is worth nothing, and
a negative control that silently fails to apply looks exactly like a
pass. Both failure modes were observed while building the storage
layer, so this runner is careful about the difference:

* every patch is applied by anchor, and the number of anchors found is
  asserted before anything is written;
* the file hash is compared before and after, so a patch that matches
  but changes nothing is rejected;
* each control runs the real gate and must make it exit non-zero.

Run it after any change to the storage layer or the probe:

    python3 -m tools.storage_parity_controls

It takes a few minutes: five controls, each running the whole 9-fixture
parity gate. It is deliberately not part of `run_tests.sh`, which would
make every ordinary test run pay for it.

The controls are also a record of what the gate is *for*. If one stops
being detected, the gate has lost coverage in that area - which is
exactly what happened when the first version of the pre-sync dump was
added but never compared, and when the filename-order exemption was
introduced.
"""

import hashlib
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

STORAGE = REPO_ROOT / "search_engine" / "storage.py"
PROBE = REPO_ROOT / "tools" / "storage_probe.py"

INDEXING = REPO_ROOT / "search_engine" / "indexing.py"

WATCHED = (STORAGE, PROBE, INDEXING)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Snapshot:
    """A copy of the files under test, so a control can always undo."""

    def __init__(self):
        self.directory = pathlib.Path(tempfile.mkdtemp(prefix="parity_controls_"))
        self.copies = {}

        for path in WATCHED:
            copy = self.directory / path.name
            shutil.copy2(path, copy)
            self.copies[path] = copy

    def restore(self):
        for path, copy in self.copies.items():
            shutil.copy2(copy, path)

    def cleanup(self):
        shutil.rmtree(self.directory, ignore_errors=True)


def apply_patch(path, old, new, label, expected=1):
    """Replace `old` with `new`, refusing to proceed unless it matched."""

    text = path.read_text(encoding="utf-8")
    found = text.count(old)

    if found != expected:
        raise ControlNotApplied(
            f"anchor matched {found} times, expected {expected}:\n{old}"
        )

    before = digest(path)

    path.write_text(
        text.replace(old, new, expected), encoding="utf-8"
    )

    if digest(path) == before:
        raise ControlNotApplied("the patch matched but changed nothing")


class ControlNotApplied(Exception):
    pass


def run_gate():
    completed = subprocess.run(
        [sys.executable, "-m", "tools.storage_equivalence"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )

    summary = [
        line.strip()
        for line in (completed.stdout + completed.stderr).splitlines()
        if line.strip().startswith(("differences", "STORAGE PARITY"))
    ]

    return completed.returncode, " | ".join(summary)


CONTROLS = []


def control(name):
    def register(function):
        CONTROLS.append((name, function))
        return function

    return register


@control("replacement stops deleting first, so a re-upload merges")
def replacement_merges():
    apply_patch(
        STORAGE,
        '''    delete_document_rows(
        connection,
        filename=filename,
    )

    connection.execute(
        """
        INSERT INTO documents''',
        '''    connection.execute(
        """
        INSERT INTO documents''',
        label="replacement merges",
    )


@control("delete skips the filename_terms table")
def delete_skips_tokens():
    apply_patch(
        STORAGE,
        '''    connection.execute(
        "DELETE FROM filename_terms WHERE filename = ?",
        (filename,),
    )

    connection.execute(
        "DELETE FROM pages WHERE filename = ?",''',
        '''    connection.execute(
        "DELETE FROM pages WHERE filename = ?",''',
        label="delete skips tokens",
    )


@control("replacement sorts the filename tokens before storing them")
def replacement_sorts_tokens():
    apply_patch(
        STORAGE,
        '''            for position, term
            in enumerate(filename_words)''',
        '''            for position, term
            in enumerate(sorted(filename_words))''',
        label="replacement sorts tokens",
    )


@control("delete skips the pages table")
def delete_skips_pages():
    apply_patch(
        STORAGE,
        '''    connection.execute(
        "DELETE FROM pages WHERE filename = ?",
        (filename,),
    )

    connection.execute(
        "DELETE FROM documents WHERE filename = ?",''',
        '''    connection.execute(
        "DELETE FROM documents WHERE filename = ?",''',
        label="delete skips pages",
    )


@control("the index build sorts the filename tokens")
def build_sorts_tokens():
    apply_patch(
        INDEXING,
        "            ] = filename_words",
        "            ] = sorted(filename_words)",
        label="build sorts tokens",
    )


@control("the index build stops skipping documents with no text")
def build_keeps_empty_documents():
    apply_patch(
        INDEXING,
        "            if not content_words:",
        "            if False:",
        label="build keeps empty documents",
    )


@control("re-index silently does nothing")
def reindex_is_a_noop():
    apply_patch(
        PROBE,
        '''    for name in names:
        try:
            incrementally_index_document(
                name, str(Path(corpus_dir) / name)
            )
            reindexed.append(name)''',
        '''    for name in names:
        try:
            reindexed.append(name)''',
        label="re-index is a no-op",
    )


@control("bulk delete is asked to delete nothing")
def bulk_delete_deletes_nothing():
    apply_patch(
        PROBE,
        '''        json={"filenames": list(names)},''',
        '''        json={"filenames": []},''',
        label="bulk delete deletes nothing",
    )


def main():
    snapshot = Snapshot()

    results = []

    try:
        for name, function in CONTROLS:

            print(f"\n=== {name} ===")

            snapshot.restore()

            try:
                function()
            except ControlNotApplied as error:
                print(f"  NOT APPLIED: {error}")
                results.append((name, "not applied"))
                continue

            returncode, summary = run_gate()

            print(f"  {summary}")

            results.append(
                (name, "detected" if returncode != 0 else "NOT DETECTED")
            )

    finally:
        snapshot.restore()
        snapshot.cleanup()

    print("\n" + "=" * 64)

    for name, result in results:
        print(f"  {result:13s} {name}")

    undetected = [name for name, r in results if r != "detected"]

    print()

    if undetected:
        print(
            f"{len(undetected)} control(s) reported no difference. The "
            "gate has lost coverage there."
        )
        return 1

    print(
        "Every control was applied, and every one made the gate fail. "
        "The gate can fail."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
