# Development Guide

## Setup

```bash
python3 -m pip install -r requirements-dev.txt
```

## Running the engine

```bash
python app.py
```

Listens on `0.0.0.0:5000`. The corpus lives in `./data`, which is
gitignored. Override it with `SEARCH_ENGINE_DATA_DIR`:

```bash
SEARCH_ENGINE_DATA_DIR=/tmp/scratch-corpus python app.py
```

The tools and the test suite use that variable, so they never touch a
developer's real corpus. The data directory is resolved at import time,
so it must be set **before** `app.py` is imported.

## Running the tests

```bash
./run_tests.sh              # full gate
python3 -m pytest -q        # suite only
```

`./run_tests.sh` runs four gates in order: byte-compile, golden vectors,
sidecar integrity, then pytest. Any failure exits non-zero.

### Two classes of test

**Hermetic.** Import the application in-process against a throwaway data
directory. They need nothing running:

```text
tests/test_tokenizer_contract.py   72  Unicode, sanitizer, query parsing
tests/test_golden_artifacts.py     19  Artifact integrity and drift
tests/test_restart_recovery.py     16  Restart, bulk delete, rebuild
tests/test_port_model.py            6  Kotlin port algorithms
tests/test_golden_vectors.py        2  Full 79-vector replay
```

**Live.** `tests/test_phase12_live.py` drives a real HTTP server on
`127.0.0.1:5000`. It is the only place `requests` is used. Started by
`tests/conftest.py`, these tests skip themselves with an explicit reason
when nothing is listening:

```text
with a server     : 118 passed
without a server  : 109 passed, 9 skipped
```

## Architecture

```text
app.py
  tokenizers ── normalize_search_query ── parse_filetype_filter
       │
  extraction (PyPDF2 / python-docx / plain text)
       │
  in-memory index
       REAL_INVERTED_INDEX   term -> {document: frequency}
       DOCUMENT_METADATA     document -> {title, path, total_words, page_count}
       FILENAME_INDEX        document -> ordered filename tokens
       PAGE_TEXT_INDEX       document -> [{page, text}]
       │
  SQLite mirror (data/search.db)  ← durability
  JSON snapshots                  ← legacy compatibility
       │
  ranking pipeline → build_paginated_response → Flask routes
```

Key invariants:

* the filename index is built from the **stem**, so the extension is
  never indexed: `tokenize_filename(splitext(name)[0])`
* filename token **order is meaningful**; `" ".join(tokens)` drives
  phrase matching inside filenames
* index status is held **in memory only** and is never persisted
* a rebuild builds a complete snapshot and swaps it atomically; failure
  leaves the previous snapshot serving

### Index state model

| State | Meaning |
| --- | --- |
| `READY` | search the current complete snapshot |
| `INDEXING` | a rebuild is running; the previous snapshot stays searchable |
| `ERROR` | the last rebuild failed; the previous snapshot stays searchable |

Status is deliberately in memory. A fresh process always starts `READY`
with `generation: 1`, so a process death during a rebuild can never
leave the application wedged in `INDEXING`. This is asserted by
`tests/test_restart_recovery.py`.

## The parity contract

`tests/golden/search_engine_vectors.json` is the single source of truth
for search behaviour. It is generated, never hand-edited.

| Section | Count | Contents |
| --- | --- | --- |
| `vectors` | 74 | Complete responses over the golden corpus |
| `empty_corpus_vectors` | 5 | Responses before anything is imported |
| `contract_vectors` | 122 | Tokenizer, sanitizer, query, rounding cases |

### Generating

```bash
python3 -m tools.generate_golden_vectors
```

Records the full response for every query, dropping only `path` (the
absolute server filesystem path, which is environment specific and must
never be a document identity). Output is byte-identical across runs —
treat any diff as a behaviour change.

### Verifying

```bash
python3 -m tools.verify_golden_vectors
```

Replays every vector against a fresh engine and diffs the complete
response: shape, key sets, and values at exact binary64. Approximate
matching would defeat the purpose, so `--tolerance` exists only to
diagnose a failure, never to accept one.

The gate is proven to catch drift. Corrupting the recorded
`exact_content` score to `9.9999` fails with:

```text
exact_content (q='network'): response.results[0].score:
    expected 9.9999 but got 0.7125
```

### Adding a vector

1. Add the query to `VECTOR_MATRIX` in
   `tools/generate_golden_vectors.py`.
2. Regenerate.
3. Inspect the recorded response — do not assume it.
4. Re-run `./run_tests.sh`.

Add a vector whenever behaviour changes intentionally. If a vector
changes without you meaning it to, that is the gate doing its job.

## Tooling

All tools share one bootstrap, `tools/engine_session.py`, so
`SEARCH_ENGINE_DATA_DIR` is always set before `app.py` is imported and
the tools cannot drift apart.

| Tool | Purpose |
| --- | --- |
| `golden_corpus.py` | The 13-document corpus definition |
| `corpus_materialize.py` | Writes it as real TXT/DOCX/PDF files |
| `engine_session.py` | Isolated-engine bootstrap |
| `generate_golden_vectors.py` | Records the vectors |
| `verify_golden_vectors.py` | The parity gate |
| `contract_vectors.py` | Shared contract tables |
| `build_corpus_sidecar.py` | Builds the Android bootstrap artifact |
| `port_model.py` | Executable model of the Kotlin ports |

The corpus is materialised as real files so uploads exercise the true
extraction path instead of bypassing it.

## Porting rule

The Android port is under `android/`. Because Kotlin cannot be compiled
in this environment, `tools/port_model.py` models the Kotlin logic in
Python **using Java's predicate semantics** and is checked against every
contract vector by `tests/test_port_model.py`.

The model also verifies the trickiest claim directly: the modelled
predicates equal Python's `str.isalnum()` and `str.isspace()` for all
1,114,112 code points, with zero mismatches.

If you change a tokenizer, sanitizer or rounding rule, all of these must
change together:

1. `app.py`
2. the Kotlin sources under `android/harness`
3. `tools/port_model.py`
4. `ANDROID.md`

`tests/test_port_model.py` is what stops those from drifting.

## Development workflow

1. Make the change.
2. `./run_tests.sh` — all gates must pass.
3. If behaviour changed intentionally, regenerate the vectors and review
   the diff.
4. Commit with a message that says what was verified and how.

## Commit and checkpoint strategy

Use milestone commits, not one giant final commit. Existing checkpoints:

```text
81faff9  Clean v1 release artifacts
6a38294  Add golden-vector parity gate and hermetic test suite
592f9a9  Add Android port specification, parity harness and port model
0294e59  Serve the UI through a same-origin API proxy
```

## What not to commit

Already gitignored: `__pycache__/`, `*.py[cod]`, `.pytest_cache/`,
`data/`, `frontend/node_modules/`, `frontend/dist/`, `.env`, `*.log`,
Android build output, `*.apk`, `*.keystore`.

Generated artifacts that **are** committed on purpose, because they are
the contract and must be reviewable in diffs:

* `tests/golden/search_engine_vectors.json`
* `artifacts/android/corpus_sidecar.json`
