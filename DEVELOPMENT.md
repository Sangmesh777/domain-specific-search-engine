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

## Module extraction (in progress)

`app.py` was a 4093-line monolith. It is being decomposed into
`search_engine/`, with `app.py` becoming a thin Flask adapter. Two
layers are done:

| Module | Contents | Lines moved |
| --- | --- | --- |
| `search_engine/text.py` | `tokenize`, `tokenize_filename`, `normalize_search_query`, `parse_filetype_filter` | 220 |
| `search_engine/sanitize.py` | `sanitize_upload_filename` | |
| `search_engine/extract.py` | `count_phrase_occurrences`, `extract_text`, `extract_pages` | 605 |
| `search_engine/snippet.py` | `build_snippet_result`, `get_snippet_and_page` | |

`app.py` is down from 4093 to 3289 lines.

### How the extraction is kept safe

Two tools, both wired into `run_tests.sh`:

**`tools/extract_modules.py`** slices the exact source segments out of
`app.py` using the AST and rewrites `app.py` to import them. Function
bodies are never retyped, so they cannot drift through transcription. It
also applies declared transforms (adding a leading parameter, renaming a
module global to it, rewriting call sites) and refuses to run if a call
site it must patch has moved. It is incremental, so one layer can be
moved without redoing the previous ones.

**`tools/shadow_parity.py`** is the gate. The golden vectors only catch a
change in *results*, for inputs someone wrote down. Shadow parity loads
the original bodies from a pinned git revision and compares them against
the extracted implementations over the recorded vectors, adversarial
inputs, a deterministic random corpus spanning the astral plane, and
structured cases for multi-page documents, window sizes and missing
documents.

Current result: **10 functions, 21,365 comparisons, 0 differences.**

If a function is extracted and not added to `EXTRACTED` in
`tools/shadow_parity.py`, the gate stops covering it — which is why
`tests/test_shadow_parity.py` asserts the exact expected function set.

### Making global state explicit

`get_snippet_and_page` read the module global `PAGE_TEXT_INDEX`. It now
takes `page_text_index` as its first parameter, and `app.py` — the
adapter, which owns the index — supplies it. Withdrawing the core's
access to globals is the point of the exercise.

### Layer 3: `search_engine/index_state.py` (done)

The four parallel dictionaries that form the index —

```text
REAL_INVERTED_INDEX   term -> {document: frequency}
DOCUMENT_METADATA     document -> {title, path, total_words, page_count}
FILENAME_INDEX        document -> ordered filename tokens
PAGE_TEXT_INDEX       document -> [{page, text}]
```

— now have a single owner, `IndexState`. No Flask, no HTTP, no
filesystem, no ranking logic.

The engine maintains them with copy-on-write: build replacement
containers, then swap them in under a lock, so a reader sees the old
snapshot or the new one and never a half-built index. `IndexState` gives
that pattern a name. All three rebinding sites now go through
`INDEX_STATE.replace_snapshot` + `sync_index_globals_from_state`:

| Function | What it publishes |
| --- | --- |
| `load_database_from_sqlite` | SQLite restore at startup |
| `load_database` | JSON snapshot restore at startup |
| `rebuild_database` | atomic swap after a full rebuild |

`app.py` keeps the legacy global names because the ranking and
persistence code still reads them directly, so they must name the *same
objects* as `INDEX_STATE`'s or a reader could see two generations.
`assert_index_state_consistent()` checks that identity, and
`tests/test_index_state.py` calls it after exercising each path. There
is a test that deliberately desynchronises a global and requires the
guard to catch it, so the guard cannot quietly become a no-op.

#### `snapshot()` returns references, on purpose

`IndexState.snapshot()` hands back the live containers; it does not
copy. This matches what the engine already did:

```python
with INDEX_DATA_LOCK:
    active_inverted_index = REAL_INVERTED_INDEX   # a reference
```

Copying there would put a full index copy on every search.
`tools/measure_snapshot_cost.py` measures the difference:

```text
 documents    terms  snapshot() us   deep_snapshot() ms      ratio
       100     3929           0.34                11.30    33,659x
       500     4000           0.32                42.50   131,222x
      1000     4000           0.51                82.39   162,652x
```

So the contract is three-part, and all three parts are tested:
`snapshot()` is cheap and read-only, `deep_snapshot()` is isolated and
expensive, and mutation means building replacements and calling
`replace_snapshot()`. A test that only checked "the snapshot is
independent" would pass for the wrong implementation and fail in
production on latency.

`test_readers_never_observe_a_partial_replacement` was checked against a
deliberately broken, non-atomic `replace_snapshot`; it fails with
`torn read: metadata ['doc_a.txt'] vs postings ['doc_b.txt']`, so the
test genuinely detects tearing rather than merely running.

### Known coherence gap, not fixed by layer 3

`incrementally_index_document` holds `INDEX_DATA_LOCK` and calls
`_remove_document_from_memory` and `_add_document_to_memory`, which
mutate the **live published containers in place** rather than building
replacements and swapping:

```python
DOCUMENT_METADATA[filename] = metadata
REAL_INVERTED_INDEX.pop(term)
```

Writers are serialised by the lock and object identity is preserved, so
the ownership invariant holds and no test regresses. But a search that
captured its references can still observe a partially updated index — a
document present in `DOCUMENT_METADATA` but not yet in `FILENAME_INDEX`,
for example. `rebuild_database` does not have this problem.

This is pre-existing behaviour and was deliberately left unchanged:
layer 3 adds an ownership boundary, it does not alter mutation
semantics. **Fixing it is the next step**, and it is the reason the next
phase is "move `_add_document_to_memory` and `_remove_document_from_memory`
onto `IndexState`" rather than a cosmetic relocation: those two helpers
must build replacement containers and return them for the caller to
swap in, so every mutation path becomes copy-on-write.

### Remaining extraction surface (measured)

`tools/` reports this via the dependency analysis below. The remaining
coupled code and what each part reads:

| Function | Lines | Reads |
| --- | --- | --- |
| `execute_search` | 1378 | the four index dicts, `INDEX_DATA_LOCK`, and `jsonify` |
| `rebuild_database` | 153 | the four index dicts, `DATA_FOLDER`, `INDEX_DATA_LOCK` |
| `upload_file` | 157 | the four index dicts, `DATA_FOLDER` |
| `bulk_delete_documents` | 146 | the four index dicts, `DATA_FOLDER` |
| `sync_sqlite_from_memory` | 106 | the four index dicts |
| `load_database` | 97 | the four snapshot file paths |
| `incrementally_index_document` | 178 | `INDEX_DATA_LOCK` plus memory/index helpers |
| `incrementally_remove_document` | 53 | `INDEX_DATA_LOCK`, SQLite helpers |
| `open_document` | 85 | the four index dicts, `DATA_FOLDER` |
| persistence helpers | ~200 | file paths, `sqlite3`, `INDEX_STATUS` |

The four globals to replace with an explicit index-state object are:

```text
REAL_INVERTED_INDEX   term -> {document: frequency}
DOCUMENT_METADATA     document -> {title, path, total_words, page_count}
FILENAME_INDEX        document -> ordered filename tokens
PAGE_TEXT_INDEX       document -> [{page, text}]
```

Order of work, each step gated by shadow parity plus the golden vectors:

1. Introduce `search_engine/index_state.py` holding those four
   structures behind an object, and prove it equals the globals.
2. Move the memory mutation helpers
   (`_add_document_to_memory`, `_remove_document_from_memory`,
   `rebuild_database`, `sync_sqlite_from_memory`) onto that object.
3. Move persistence (`load_database`, `save_database`,
   `save_database_snapshot`, `atomic_write_json`, the SQLite helpers).
4. Move `execute_search` last, as `SearchEngine.search(query, page,
   limit)` returning a plain dict. It currently calls `jsonify`
   directly; that call must move to the adapter, which is the single
   change that makes the core transport-independent.
5. Reduce the route handlers in `app.py` to argument parsing, a call,
   and `jsonify`.

Step 4 is the one the Android `LocalBackend` depends on, because it is
what turns the ranking pipeline into a callable function of an explicit
snapshot rather than a web request.

Do not attempt steps 1-5 in one commit. Each should leave `./run_tests.sh`
green and `tools/shadow_parity.py` at zero differences.

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
