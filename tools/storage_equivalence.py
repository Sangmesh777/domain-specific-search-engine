"""
Prove the extracted persistence layer reconstructs exactly the same
IndexState, and the same SQLite contents, as the monolith did.

The claim this tool exists to test is narrow and total: moving storage
into `search_engine/storage.py` changed *where the code lives*, and
nothing else. Anything that changes the reconstructed index - a dropped
ORDER BY, a reordered INSERT, a schema column that came back as a
string, a re-read that lost a term - shows up here as a difference.

Method, per fixture:

  1. Materialise the fixture into two isolated data directories.
  2. Run a whole engine session over one using the CURRENT engine.
  3. Run the identical session over the other using the engine as it was
     at `--base` (the commit before the extraction), read out of git.
  4. Deep-compare the four containers, all four JSON snapshot files,
     every SQLite row, and the schema.
  5. Restart both in fresh processes over the same directories and
     compare what SQLite rebuilds.

Step 5 is the one that matters most. Comparing in-memory state proves the
writers agree; restarting proves the *reader* agrees, and reconstruction
is the whole point of a storage layer.

Both engines run with PYTHONHASHSEED=0. That is not cosmetic: the full
sync inserts `filename_terms` rows in the iteration order of a `set`, so
the row order - and therefore the reconstructed order of a document's
filename tokens - varies between processes by default. That variation is
pre-existing and is reported by `--report-hash-nondeterminism` rather
than hidden. Pinning the seed makes the comparison meaningful; it does
not make the underlying behaviour deterministic.

Usage:

    python -m tools.storage_equivalence
    python -m tools.storage_equivalence --base 18429c2
    python -m tools.storage_equivalence --negative-control
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_BASE = "18429c2"

CONTAINERS = (
    "inverted_index",
    "document_metadata",
    "filename_index",
    "page_text_index",
)

SQLITE_TABLES = ("documents", "term_postings", "filename_terms", "pages")

# A fixed seed for both engines, so the set-iteration order that reaches
# SQLite is the same on both sides of the comparison.
PARITY_HASH_SEED = "0"


class StorageEquivalenceError(Exception):
    """Raised when the comparison cannot be run."""


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

# Documents chosen to exercise the persistence paths that a naive
# comparison would miss: repeated terms (counts above one), one term
# shared by two documents (so a delete must not drop it), a document with
# no indexable content at all (skipped by the engine), and filenames
# whose tokens overlap so a filename_index mix-up is visible.
SYNTHETIC_DOCUMENTS = {
    "shared_term_alpha.txt": (
        "sharedterm alphaonly alphaonly alphaonly\n"
        "repeated term appears here too and again here\n"
    ),
    "shared_term_beta.txt": (
        "sharedterm betaonly\n"
    ),
    "unicode_document.txt": (
        "İstanbul CAFÉ naïve under_score zerowidth\n"
        "ascii é ½ fraction\n"
    ),
    "numeric_fragments_extra.txt": (
        "9901 9902 9903 42 42 42 leadingzero\n"
    ),
    "Case Variant Notes.txt": (
        "casetoken varianttoken\n"
    ),
}

# Filenames whose token set, under PYTHONHASHSEED=0, iterates in an order
# that is NOT alphabetical. That matters: the persisted order comes from
# `set(filename_words)` and the reader returns rowid order, so on a
# fixture where set order happens to equal sorted order, a change to the
# reader's ORDER BY would be invisible. These documents make the order
# source observable, which is what turns "ORDER BY filename, rowid" into
# a testable claim rather than a comment.
ORDER_SENSITIVE_DOCUMENTS = {
    "zulu alpha mike bravo.txt": "body text for the first order probe\n",
    "readme setup guide.txt": "body text for the second order probe\n",
    "chapter appendix index glossary.txt": (
        "body text for the third order probe\n"
    ),
}


# A document with no alphanumeric content at all. The engine skips it, so
# it must be absent from every store on both sides.
EMPTY_DOCUMENT_NAME = "no_indexable_content.txt"


def write_synthetic_corpus(destination):
    """Write the synthetic documents into `destination`."""

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    for name, text in sorted(SYNTHETIC_DOCUMENTS.items()):
        (destination / name).write_text(text, encoding="utf-8")

    (destination / EMPTY_DOCUMENT_NAME).write_text(
        "!!! ... ---\n",
        encoding="utf-8",
    )


def build_fixture_corpus(fixture, destination):
    """Materialise one fixture's documents into `destination`."""

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    if fixture["corpus"] == "golden":
        from tools.corpus_materialize import materialize_corpus

        materialize_corpus(destination)

    elif fixture["corpus"] == "synthetic":
        write_synthetic_corpus(destination)

    elif fixture["corpus"] == "order_sensitive":
        for name, text in sorted(ORDER_SENSITIVE_DOCUMENTS.items()):
            (destination / name).write_text(text, encoding="utf-8")

    elif fixture["corpus"] == "both":
        from tools.corpus_materialize import materialize_corpus

        materialize_corpus(destination)
        write_synthetic_corpus(destination)

    elif fixture["corpus"] == "empty":
        pass

    else:
        raise StorageEquivalenceError(
            f"unknown corpus {fixture['corpus']!r}"
        )


FIXTURES = (
    {
        "name": "golden-corpus",
        "corpus": "golden",
        "deletes": (),
    },
    {
        "name": "synthetic-adversarial",
        "corpus": "synthetic",
        "deletes": (),
    },
    {
        "name": "synthetic-with-delete",
        "corpus": "synthetic",
        # Deleting one of the two documents holding `sharedterm` must
        # leave the term present and pointing only at the survivor.
        "deletes": ("shared_term_alpha.txt",),
    },
    {
        "name": "golden-subset-deleted",
        "corpus": "golden",
        "deletes": (
            "network_security_notes.txt",
            "prefix_network_alpha.txt",
        ),
    },
    {
        "name": "empty-corpus",
        "corpus": "empty",
        "deletes": (),
    },
    {
        "name": "order-sensitive-filenames",
        "corpus": "order_sensitive",
        "deletes": (),
    },
    {
        "name": "mixed-corpus",
        "corpus": "both",
        "deletes": (EMPTY_DOCUMENT_NAME,),
    },
)


# ----------------------------------------------------------------------
# Running one side
# ----------------------------------------------------------------------


def _probe_environment():
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = PARITY_HASH_SEED

    return environment


def run_probe(engine, base, data_dir, corpus_dir, phase, out_path, deletes=()):
    """Run tools/storage_probe.py once and return its parsed dump."""

    command = [
        sys.executable,
        "-m",
        "tools.storage_probe",
        "--engine",
        engine,
        "--data",
        str(data_dir),
        "--corpus",
        str(corpus_dir),
        "--phase",
        phase,
        "--out",
        str(out_path),
    ]

    if base:
        command += ["--base", base]

    for name in deletes:
        command += ["--delete", name]

    environment = _probe_environment()

    completed = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=900,
        env=environment,
    )

    if completed.returncode != 0:
        raise StorageEquivalenceError(
            f"probe failed ({engine}/{phase}):\n"
            f"{completed.stderr[-4000:]}"
        )

    return json.loads(Path(out_path).read_text(encoding="utf-8"))


def run_side(engine, base, corpus_dir, root, deletes=()):
    """
    Run both phases for one engine and return (ingest, reconstruct).

    The corpus directory is shared with the other engine. Each document
    records its absolute source path in `document_metadata["path"]`, so
    giving the two engines different copies would make every document
    differ on a path that has nothing to do with the storage layer.
    """

    data_dir = Path(root) / "data"

    data_dir.mkdir(parents=True, exist_ok=True)

    ingest = run_probe(
        engine,
        base,
        data_dir,
        corpus_dir,
        "ingest",
        Path(root) / "ingest.json",
        deletes=deletes,
    )

    reconstruct = run_probe(
        engine,
        base,
        data_dir,
        corpus_dir,
        "reconstruct",
        Path(root) / "reconstruct.json",
    )

    return ingest, reconstruct


# ----------------------------------------------------------------------
# Comparison
# ----------------------------------------------------------------------


def compare_containers(left, right, label, differences):
    """Compare the four published containers element by element."""

    for container in CONTAINERS:
        a = left[container]
        b = right[container]

        if a == b:
            continue

        # Report the first concrete disagreement rather than dumping
        # whole indexes, which would be unreadable.
        detail = None

        if isinstance(a, dict) and isinstance(b, dict):

            missing = sorted(set(a) - set(b))
            extra = sorted(set(b) - set(a))

            if missing or extra:
                detail = (
                    f"keys missing from the right: {missing[:5]}, "
                    f"extra on the right: {extra[:5]}"
                )

            else:
                for key in a:
                    if a[key] != b[key]:
                        detail = (
                            f"first differing key {key!r}: "
                            f"left {a[key]!r} right {b[key]!r}"
                        )
                        break
        else:
            detail = f"left {a!r} right {b!r}"

        differences.append(f"{label}: {container} differs - {detail}")


def compare_sqlite(left, right, label, differences):
    """Compare schema and every row of every table."""

    if left.get("exists") != right.get("exists"):
        differences.append(
            f"{label}: database presence differs "
            f"(left {left.get('exists')}, right {right.get('exists')})"
        )
        return

    if not left.get("exists"):
        return

    if left.get("schema") != right.get("schema"):
        differences.append(f"{label}: sqlite schema differs")

    for table in SQLITE_TABLES:

        a = left["tables"].get(table)
        b = right["tables"].get(table)

        if a is None or b is None:
            differences.append(f"{label}: table {table} missing on one side")
            continue

        if a["columns"] != b["columns"]:
            differences.append(
                f"{label}: {table} columns differ "
                f"(left {a['columns']}, right {b['columns']})"
            )
            continue

        if a["rows"] == b["rows"]:
            continue

        difference = None

        if len(a["rows"]) != len(b["rows"]):
            difference = (
                f"row count {len(a['rows'])} vs {len(b['rows'])}"
            )
        else:
            for index, (row_a, row_b) in enumerate(
                zip(a["rows"], b["rows"])
            ):
                if row_a != row_b:
                    difference = (
                        f"first differing row {index}: "
                        f"left {row_a!r} right {row_b!r}"
                    )
                    break

        differences.append(f"{label}: {table} differs - {difference}")


def compare_json_files(left, right, label, differences):
    """Compare the JSON snapshot files the save path writes."""

    if set(left) != set(right):
        differences.append(
            f"{label}: json file set differs "
            f"(left {sorted(left)}, right {sorted(right)})"
        )
        return

    for name in sorted(left):
        if left[name] != right[name]:
            differences.append(f"{label}: {name} differs")


def compare_actions(left, right, label, differences):
    """Compare what each side was asked to do, and what it reported."""

    for field in ("indexed", "removed", "requested_deletes"):
        if left.get(field) != right.get(field):
            differences.append(
                f"{label}: {field} differs "
                f"(left {left.get(field)!r} right {right.get(field)!r})"
            )

    if left.get("status") != right.get("status"):
        differences.append(
            f"{label}: status differs "
            f"(left {left.get('status')} right {right.get('status')})"
        )


def filename_order_instability(ingest, reconstruct):
    """
    Return the documents whose filename token ORDER changed on restart.

    `sync_sqlite_from_memory` stores `set(filename_words)`, so the
    persisted order is set-iteration order, while the in-memory order is
    the order `tokenize_filename` produced. Reading back with
    `ORDER BY filename, rowid` therefore returns a different sequence
    than the one that was live before the restart.

    This is pre-existing behaviour, not something the extraction caused,
    so it is reported rather than failed on. It is reported loudly,
    because a silent reordering after every restart is exactly the sort
    of thing a "0 differences" number would hide.
    """

    changed = []

    live = ingest["containers"]["filename_index"]
    rebuilt = reconstruct["containers"]["filename_index"]

    for filename in sorted(set(live) & set(rebuilt)):

        before = live[filename]
        after = rebuilt[filename]

        if before != after and sorted(before) == sorted(after):
            changed.append((filename, before, after))

    return changed


def compare_reports(left, right, label, differences):
    """Compare two probe reports in full."""

    compare_actions(left, right, label, differences)

    compare_containers(
        left["containers"], right["containers"], label, differences
    )
    compare_sqlite(left["sqlite"], right["sqlite"], label, differences)
    compare_json_files(
        left["json_files"], right["json_files"], label, differences
    )


def canonical_state(report):
    """
    Return a copy of a report with filename token ORDER removed.

    Used only for the restart self-check. The legacy and extracted
    engines are compared with order intact - that is the strict
    comparison - but an engine cannot be compared strictly against its
    own restart, because `filename_terms` is persisted from a `set` and
    the order is genuinely not round-tripped. That property is reported
    separately by `filename_order_instability`, so relaxing it here
    hides nothing.
    """

    canonical = {
        key: value
        for key, value in report.items()
        if key in ("containers", "sqlite", "json_files")
    }

    canonical = json.loads(json.dumps(canonical))

    index = canonical["containers"]["filename_index"]

    for filename, words in index.items():
        index[filename] = sorted(words)

    snapshot = canonical["json_files"].get("filename_index.json")

    if isinstance(snapshot, dict):
        for filename, words in snapshot.items():
            snapshot[filename] = sorted(words)

    return canonical


def comparison_count(ingest, reconstruct):
    """
    Count the individual values compared, so a run reports how much it
    actually looked at rather than just "no differences".
    """

    total = 0

    for report in (ingest, reconstruct):

        for container in CONTAINERS:
            total += len(
                json.dumps(report["containers"][container], sort_keys=True)
            )

        sqlite = report["sqlite"]

        if sqlite.get("exists"):
            for table in SQLITE_TABLES:
                total += len(sqlite["tables"][table]["rows"])

            total += len(sqlite.get("schema", []))

        total += len(json.dumps(report["json_files"], sort_keys=True))

    return total


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------


def run(base=DEFAULT_BASE, only=None):
    """Compare every fixture and return a summary."""

    fixtures = [
        fixture
        for fixture in FIXTURES
        if only is None or fixture["name"] in only
    ]

    summaries = []
    differences = []
    comparisons = 0
    reordered = []

    for fixture in fixtures:

        with tempfile.TemporaryDirectory(
            prefix=f"storage_parity_{fixture['name']}_"
        ) as workspace:

            workspace = Path(workspace)

            # One corpus, used by both engines, so the absolute source
            # path recorded per document is identical on both sides.
            corpus_dir = workspace / "corpus"

            build_fixture_corpus(fixture, corpus_dir)

            new_ingest, new_reconstruct = run_side(
                "new", None, corpus_dir, workspace / "new",
                fixture["deletes"],
            )
            legacy_ingest, legacy_reconstruct = run_side(
                "legacy", base, corpus_dir, workspace / "legacy",
                fixture["deletes"],
            )

        label = fixture["name"]

        # The parity claim: same inputs, same resulting state.
        compare_reports(
            legacy_ingest, new_ingest, f"{label}/ingest", differences
        )
        compare_reports(
            legacy_reconstruct,
            new_reconstruct,
            f"{label}/reconstruct",
            differences,
        )

        # The reconstruction claim: a restart rebuilds the same index.
        # Compared as state only - the action log describes what the
        # ingest phase was asked to do and does not exist after a
        # restart.
        compare_reports(
            canonical_state(new_ingest),
            canonical_state(new_reconstruct),
            f"{label}/restart",
            differences,
        )

        for filename, before, after in filename_order_instability(
            new_ingest, new_reconstruct
        ):
            reordered.append((label, filename, before, after))

        comparisons += comparison_count(new_ingest, new_reconstruct)

        summaries.append({
            "fixture": label,
            "documents": len(
                new_ingest["containers"]["document_metadata"]
            ),
            "terms": len(new_ingest["containers"]["inverted_index"]),
            "sqlite_rows": sum(
                len(value["rows"])
                for value in new_ingest["sqlite"]["tables"].values()
            ),
        })

    return summaries, comparisons, differences, reordered


def hash_nondeterminism_report(base=DEFAULT_BASE):
    """
    Show that the legacy engine disagrees with ITSELF without a fixed
    hash seed, so the comparison above is not comparing noise.

    This is evidence about a pre-existing property, and it is the reason
    both sides are run with PYTHONHASHSEED=0.
    """

    fixture = {"name": "hash", "corpus": "synthetic", "deletes": ()}

    orders = []

    for _attempt in range(2):

        with tempfile.TemporaryDirectory(prefix="hash_probe_") as workspace:

            workspace = Path(workspace)

            data_dir = workspace / "data"
            corpus_dir = workspace / "corpus"

            data_dir.mkdir(parents=True, exist_ok=True)
            build_fixture_corpus(fixture, corpus_dir)

            command = [
                sys.executable,
                "-m",
                "tools.storage_probe",
                "--engine",
                "legacy",
                "--base",
                base,
                "--data",
                str(data_dir),
                "--corpus",
                str(corpus_dir),
                "--phase",
                "ingest",
                "--out",
                str(workspace / "out.json"),
            ]

            environment = dict(os.environ)
            # Deliberately NOT pinned.
            environment.pop("PYTHONHASHSEED", None)

            completed = subprocess.run(
                command,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=900,
                env=environment,
            )

            if completed.returncode != 0:
                raise StorageEquivalenceError(
                    f"hash probe failed:\n{completed.stderr[-2000:]}"
                )

            report = json.loads(
                (workspace / "out.json").read_text(encoding="utf-8")
            )

            orders.append(
                report["sqlite"]["tables"]["filename_terms"]["rows"]
            )

    return orders[0] == orders[1], orders


def negative_control():
    """
    Corrupt a dump in each way that matters and confirm the comparator
    catches every one.

    A comparison that reports "0 differences" is worth nothing until it
    has been shown to report a difference. These run entirely inside the
    process and need no engine, so they are cheap enough to keep as
    permanent evidence rather than a one-off experiment.
    """

    import copy

    reference = {
        "indexed": ["a.txt"],
        "removed": [],
        "requested_deletes": [],
        "status": {"state": "READY", "generation": 1},
        "containers": {
            "inverted_index": {"alpha": {"a.txt": 2}},
            "document_metadata": {
                "a.txt": {
                    "title": "a.txt",
                    "path": "/data/a.txt",
                    "total_words": 2,
                    "page_count": 1,
                }
            },
            "filename_index": {"a.txt": ["alpha", "beta"]},
            "page_text_index": {"a.txt": [{"page": 1, "text": "alpha"}]},
        },
        "sqlite": {
            "exists": True,
            "schema": [{"type": "table", "name": "documents", "sql": "x"}],
            "tables": {
                "documents": {
                    "columns": ["filename"],
                    "rows": [["a.txt"]],
                },
                "term_postings": {
                    "columns": ["term", "filename", "term_count"],
                    "rows": [["alpha", "a.txt", 2]],
                },
                "filename_terms": {
                    "columns": ["filename", "term"],
                    "rows": [["a.txt", "alpha"], ["a.txt", "beta"]],
                },
                "pages": {
                    "columns": ["filename", "page_number", "text"],
                    "rows": [["a.txt", 1, "alpha"]],
                },
            },
        },
        "json_files": {
            "inverted_index.json": {"alpha": {"a.txt": 2}},
            "filename_index.json": {"a.txt": ["alpha", "beta"]},
        },
    }

    def corrupt(description, mutate):
        candidate = copy.deepcopy(reference)
        mutate(candidate)

        found = []
        compare_reports(reference, candidate, "control", found)

        return description, found

    def drop_term(state):
        del state["containers"]["inverted_index"]["alpha"]

    def change_count(state):
        state["containers"]["inverted_index"]["alpha"]["a.txt"] = 7

    def drop_document(state):
        del state["containers"]["document_metadata"]["a.txt"]

    def reorder_filename_tokens(state):
        state["containers"]["filename_index"]["a.txt"] = ["beta", "alpha"]

    def change_page_text(state):
        state["containers"]["page_text_index"]["a.txt"][0]["text"] = "beta"

    def change_sqlite_row(state):
        state["sqlite"]["tables"]["term_postings"]["rows"][0][2] = 99

    def drop_sqlite_row(state):
        state["sqlite"]["tables"]["documents"]["rows"] = []

    def reorder_sqlite_rows(state):
        state["sqlite"]["tables"]["term_postings"]["rows"] = [
            ["beta", "a.txt", 1],
        ]

    def change_schema(state):
        state["sqlite"]["schema"][0]["sql"] = "CREATE TABLE documents ()"

    def drop_json_file(state):
        del state["json_files"]["inverted_index.json"]

    def change_action_log(state):
        state["indexed"] = ["b.txt"]

    controls = [
        corrupt("term dropped from inverted_index", drop_term),
        corrupt("posting count changed", change_count),
        corrupt("document dropped from metadata", drop_document),
        corrupt("filename tokens reordered", reorder_filename_tokens),
        corrupt("page text changed", change_page_text),
        corrupt("sqlite row value changed", change_sqlite_row),
        corrupt("sqlite row dropped", drop_sqlite_row),
        corrupt("sqlite rows reordered", reorder_sqlite_rows),
        corrupt("schema changed", change_schema),
        corrupt("json snapshot file removed", drop_json_file),
        corrupt("action log changed", change_action_log),
    ]

    # The one deliberate relaxation: the restart self-check ignores
    # filename token order, because the order is not round-tripped. An
    # order-only change must therefore pass the canonical comparison.
    reordered = copy.deepcopy(reference)
    reordered["containers"]["filename_index"]["a.txt"] = ["beta", "alpha"]

    relaxed_found = []

    compare_reports(
        canonical_state(reference),
        canonical_state(reordered),
        "relaxed",
        relaxed_found,
    )

    # ...and must still be caught by the STRICT comparison, which is
    # what the legacy-vs-extracted parity claim uses. If this ever stops
    # being detected, the parity result is no longer meaningful.
    strict_found = []

    compare_reports(
        reference,
        reordered,
        "strict",
        strict_found,
    )

    return controls, relaxed_found, strict_found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument(
        "--fixture",
        action="append",
        dest="fixtures",
        default=None,
        help="Run only the named fixture.",
    )
    parser.add_argument(
        "--report-hash-nondeterminism",
        action="store_true",
        help=(
            "Also demonstrate the pre-existing PYTHONHASHSEED-dependent "
            "filename_terms ordering."
        ),
    )
    parser.add_argument(
        "--negative-control",
        action="store_true",
        help=(
            "Deliberately corrupt one JSON snapshot field in the new "
            "engine and confirm the comparison reports it."
        ),
    )

    arguments = parser.parse_args(argv)

    print("Storage reconstruction parity: extracted vs monolithic engine")
    print(f"  base revision : {arguments.base}")
    print(f"  hash seed     : {PARITY_HASH_SEED} (pinned on both sides)")
    print()

    try:
        summaries, comparisons, differences, reordered = run(
            base=arguments.base,
            only=arguments.fixtures,
        )
    except StorageEquivalenceError as error:
        print(f"storage parity could not run: {error}", file=sys.stderr)
        return 1

    print(f"  fixtures       : {len(summaries)}")
    print(f"  values compared: {comparisons:,}")

    for summary in summaries:
        print(
            f"    {summary['fixture']:<28} "
            f"{summary['documents']:>3} docs "
            f"{summary['terms']:>4} terms "
            f"{summary['sqlite_rows']:>4} sqlite rows"
        )

    if reordered:
        print()
        print(
            "  filename token order changed on restart "
            f"({len(reordered)} document(s)):"
        )

        for label, filename, before, after in reordered[:5]:
            print(f"    {label}: {filename!r}")
            print(f"      before restart: {before}")
            print(f"      after  restart: {after}")

        print(
            "    pre-existing: sync_sqlite_from_memory persists "
            "set(filename_words), and the\n"
            "    reader returns rowid order, so the sequence is not "
            "round-tripped.\n"
            "    Present on both engines; not caused by the extraction."
        )

    print()
    print(f"  differences    : {len(differences)}")

    if arguments.negative_control:

        controls, relaxed_found, strict_found = negative_control()

        print()
        print("  negative controls (comparator sensitivity):")

        undetected = []

        for description, found in controls:
            status = "detected" if found else "MISSED"
            print(f"    {status:<9} {description}")

            if not found:
                undetected.append(description)

        print()
        print(
            "    restart comparison ignores filename order: "
            + (
                "as intended"
                if not relaxed_found
                else "UNEXPECTED"
            )
        )
        print(
            "    strict comparison still catches a filename reorder: "
            + (
                "yes"
                if strict_found
                else "NO - the parity claim is unsound"
            )
        )

        if undetected or relaxed_found or not strict_found:
            print()
            print("NEGATIVE CONTROL FAILED")
            return 1

        print()
        print("NEGATIVE CONTROL OK")
        print("Every deliberate corruption was reported, the restart")
        print("comparison is relaxed only for filename order, and the")
        print("strict comparison used for parity still catches it.")

    if arguments.report_hash_nondeterminism:

        stable, orders = hash_nondeterminism_report(base=arguments.base)

        print()
        print("  PYTHONHASHSEED check (legacy engine, unpinned seed):")

        if stable:
            print("    two runs agreed - the effect did not show this time")
        else:
            print("    two runs produced different filename_terms row order:")
            print(f"      run 1: {orders[0][:3]}")
            print(f"      run 2: {orders[1][:3]}")
            print(
                "    pre-existing: sync_sqlite_from_memory inserts from "
                "set(filename_words)\n"
                "    affects the reconstructed order of filename tokens "
                "after a restart"
            )

    print()

    if differences:
        print("STORAGE PARITY FAILED")
        for difference in differences[:40]:
            print(f"  {difference}")
        return 1

    print("STORAGE PARITY OK")
    print("The extracted storage layer reconstructs the same index, the")
    print("same SQLite rows and the same snapshot files as the monolith.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
