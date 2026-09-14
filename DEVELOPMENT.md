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

`./run_tests.sh` runs seven gates in order: byte-compile, golden vectors,
shadow parity, mutation equivalence, storage reconstruction parity,
sidecar integrity, then pytest. Any failure exits non-zero.

### Two classes of test

**Hermetic.** Import the application in-process against a throwaway data
directory. They need nothing running:

```text
tests/test_tokenizer_contract.py        72  Unicode, sanitizer, query parsing
tests/test_storage_parity.py            28  The storage layer's separation
tests/test_index_state.py               25  Ownership and atomic publication
tests/test_filename_order_persistence.py 23  Filename order across a restart
tests/test_memory_mutation_atomicity.py 20  Copy-on-write, torn reads
tests/test_golden_artifacts.py          19  Artifact integrity and drift
tests/test_restart_recovery.py          16  Restart, bulk delete, rebuild
tests/test_jvm_semantics.py              7  JVM semantics for the Kotlin port
tests/test_port_model.py                 6  Kotlin port algorithms
tests/test_shadow_parity.py              3  Extraction coverage guard
tests/test_golden_vectors.py             2  Full 79-vector replay
```

**Live.** `tests/test_phase12_live.py` drives a real HTTP server on
`127.0.0.1:5000`. It is the only place `requests` is used. Started by
`tests/conftest.py`, these tests skip themselves with an explicit reason
when nothing is listening:

```text
with a server     : 230 passed
without a server  : 221 passed, 9 skipped
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

### Coherence gap: closed

Layer 3 left one hole. `incrementally_index_document` held
`INDEX_DATA_LOCK` and called `_remove_document_from_memory` and
`_add_document_to_memory`, which mutated the **live published
containers in place** rather than building replacements and swapping:

```python
DOCUMENT_METADATA[filename] = metadata
REAL_INVERTED_INDEX.pop(term)
```

Writers were serialised by the lock and object identity was preserved,
so the ownership invariant held and no test regressed. But a search
that captured its references could observe a partially updated index —
a document present in `DOCUMENT_METADATA` but not yet in
`FILENAME_INDEX`. `rebuild_database` never had this problem.

Fixing that surfaced a **second, distinct hole** in the same family.
Two rebuild sites — `load_database_from_sqlite` and `rebuild_database` —
already swapped the snapshot atomically, but called
`sync_index_globals_from_state()` *after* releasing `INDEX_DATA_LOCK`:

```python
with INDEX_DATA_LOCK:
    INDEX_STATE.replace_snapshot({...})

sync_index_globals_from_state()   # outside the critical section
```

`execute_search` captures the four globals under that lock, so a reader
could acquire the lock in that window and capture a **half-rebound set**
— `REAL_INVERTED_INDEX` from the new generation and `DOCUMENT_METADATA`
from the old. That is a genuine torn read, and it was invisible to every
test because it needs a concurrent reader to hit a window of a few
microseconds.

All five publication sites now go through `publish_index_state` inside
the critical section, including the import-time `load_database()`, where
the lock is uncontended but is taken anyway so the rule has no
exceptions. Two structural tests enforce it against the source: one
walks the AST checking that every `publish_index_state` call is inside
an `INDEX_DATA_LOCK` `with` block, the other that `replace_snapshot` is
never called directly outside `publish_index_state`'s own body. They
resolve `publish_index_state`'s line span from the AST rather than
hardcoding it, so editing `app.py` cannot silently disarm them.

Both helpers are now **deleted**. Their callers build a replacement
through a pure planner and publish it once:

```python
with INDEX_DATA_LOCK:
    publish_index_state(
        plan_upsert_document(INDEX_STATE.snapshot(), old_term_counts, ...)
    )
```

`publish_index_state(replacement)` is `replace_snapshot` followed by
`sync_index_globals_from_state`, and **must** be called under
`INDEX_DATA_LOCK`, because `execute_search` captures all four globals
under that same lock. Every mutation path in the application is now
copy-on-write, so a reader holding a captured snapshot can never
observe a later writer.

Two pieces of evidence support that, and they are separate claims:

* `tools/mutation_equivalence.py` replays both original helpers from
  `5bc945f:app.py` and compares the resulting state against the
  planners — 9 cases, 18 comparisons, 0 state diffs, 0 purity diffs.
  So the rewrite preserves behaviour rather than quietly changing it.
* `tests/test_memory_mutation_atomicity.py` pins the coherence itself.
  Its primary test captures a snapshot, republishes 100 times, and
  asserts after **every** publication that the captured containers did
  not move.

Both were validated against deliberately broken implementations, since
a passing test proves nothing until it has been seen to fail:

| Break | Observed failure |
| --- | --- |
| Drop the stale-term removal loop | `STATE MISMATCH: re-upload drops the previous revision's terms` |
| Alias `new_metadata = metadata_index` | `INPUT WAS MUTATED` |
| `replace_snapshot` mutates containers in place | `after publication 0 the captured term set changed from ['alphaone', 'alphatwo'] to ['betaone', 'betatwo']` |
| Same, threaded reader | `mixed generation observed: []` and `torn container presence: {'document_metadata': True, 'filename_index': True, 'page_text_index': False}` |
| `rebuild_database` reverts to syncing globals after the lock | `sync_index_globals_from_state called outside INDEX_DATA_LOCK at lines [1304]` |
| Same, checked from the other direction | `replace_snapshot called directly at lines [1297]` |

The threaded reader is pushed to `sys.setswitchinterval(1e-06)` so a
non-atomic publish is actually reachable. The deterministic capture test
is the authority; the threaded one guards against reintroducing
in-place writes and is not what the argument rests on.

One behavioural oddity survives deliberately. When the SQLite write
fails mid-upload, memory has already moved to the new revision while
SQLite still holds the old one, and the exception propagates — memory
and disk are left out of sync. This was measured against the
pre-refactor implementation and pinned by
`test_sqlite_failure_preserves_the_pre_refactor_behaviour`, so it is
unchanged rather than unnoticed. Redesigning that rollback is a
separate decision, not a side effect of copy-on-write.

### Layer 4: `search_engine/storage.py` (done)

All SQLite and JSON persistence moved out of `app.py`: the connection
factory, the schema, the full sync, the incremental-write helpers'
read path, the SQLite rebuild, the JSON snapshot loader, the JSON
writer and the save path. `app.py` keeps thin adapters, so the
document-import and bulk-delete call sites did not have to change.

The layer is deliberately ignorant of the application. It has no Flask
import, no reference to `INDEX_DATA_LOCK`, and no reference to the four
index globals or `INDEX_STATE`. It takes its inputs as parameters and
returns its results:

```python
def load_database_from_sqlite(*, db_path) -> dict   # returns, installs nothing
def load_json_snapshot(*, index_file, meta_file, filename_index_file,
                       page_text_file, current) -> dict
def sync_sqlite_from_memory(*, db_path, inverted_index, document_metadata,
                           filename_index, page_text_index, connection=None)
def save_database_snapshot(*, index_file, meta_file, filename_index_file,
                           page_text_file, inverted_index, document_metadata,
                           filename_index, page_text_index)
def atomic_write_json(path, payload)
def get_sqlite_connection(db_path)
def get_document_term_counts_from_sqlite(db_path, filename)
def create_schema(connection)
def count_documents(connection)
```

`load_database_from_sqlite` returns `None` when SQLite is empty, and the
adapter turns that back into the original "leave the active state alone"
early return.

`tests/test_storage_parity.py` enforces the separation structurally: no
Flask import, no global names, and both reconstruction functions must
contain a `return` and must not call `publish_index_state`.

### Proving reconstruction

`tools/storage_equivalence.py` runs a whole engine session - ingest,
then a restart in a fresh process - against both the extracted layer and
the monolith read out of git at `18429c2`, then compares:

* all four containers, element by element and order-sensitively
* every row of all four SQLite tables, in row order
* the schema
* all four JSON snapshot files
* the restart's reconstructed state against the ingest state

7 fixtures, **230,538 values compared, 0 differences**.

Two things about that number are worth stating plainly, because either
one could have made it meaningless:

* Both engines run with `PYTHONHASHSEED=0`. Without it the comparison
  is comparing noise - see the finding below.
* One deliberately insensitive fixture had to be added. On the golden
  corpus the stored row order happens to equal sorted order under seed
  0, so changing the reader's `ORDER BY filename, rowid` to
  `ORDER BY filename, term` produced **zero** differences. The
  `order-sensitive-filenames` fixture uses filenames whose token sets
  iterate in a non-alphabetical order under seed 0, which makes the
  order source observable. With it, the same edit is caught.

Negative controls, all observed:

| Break | Result |
| --- | --- |
| 13 in-process corruptions (dropped term, changed count, dropped document, reordered filename tokens, changed page text, changed/dropped/reordered SQLite rows, changed schema, changed/dropped `filename_terms` content, removed snapshot file, changed action log) | all 13 reported |
| `ORDER BY filename, rowid` -> `ORDER BY filename, term` | `order-sensitive-filenames/reconstruct: filename_index differs` |
| `term_count` read as `term_count + 1` | `reconstruct: inverted_index differs` |
| `total_words` and `page_count` swapped in the SELECT | `reconstruct: document_metadata differs` |
| page text read as `text \|\| ' TAMPERED'` | `reconstruct: page_text_index differs` |

Two of those attempts initially produced a false "no difference", and in
both cases the control was wrong rather than the tool: one patch matched
the schema comment instead of the SQL, and one "reorder" replaced a
one-element list with itself. Both were fixed before being believed.

### Fixed: filename token order now survives a restart

Pre-existing, reproduced identically on the monolith, and
**ranking-visible**. Found while proving the reconstructed index against
the monolith, then fixed as its own milestone before the incremental
write paths were extracted, because the defect lived in exactly those two
functions.

`sync_sqlite_from_memory` and `incrementally_index_document` both wrote
`filename_terms` from `set(filename_words)` under a
`PRIMARY KEY (filename, term)` with `INSERT OR IGNORE`, so the persisted
data lost **both** the token order and repeated tokens.
`load_database_from_sqlite` read it back with `ORDER BY filename, rowid`.

That matters because `execute_search` builds

```python
normalized_filename = " ".join(filename_words)
```

and tests `normalized_phrase in normalized_filename`. When a query is
quoted, documents without `phrase_match` are filtered out entirely.

Measured, on an isolated corpus whose only copy of the phrase is in the
filename, with the content deliberately unrelated:

```text
before restart   "network security notes" -> ['network security notes.txt']
after  restart   "network security notes" -> []      # monolith
after  restart   "network security notes" -> ['network security notes.txt']  # fixed
```

Reproduced on 6/6 hash seeds for the extracted engine and 3/3 for the
monolith at `18429c2`. In the live corpus **5 of the 13 documents** were
stored in a scrambled order; the breakage is usually masked because the
phrase also appears in the document's content, which is why it went
unnoticed.

**Fix.** `filename_terms` gained an explicit ordinal, matching the
Android specification's `(document, position, token)` model:

```sql
filename_terms(filename TEXT, position INTEGER NOT NULL, term TEXT,
               PRIMARY KEY (filename, position))
```

Both writers now iterate `enumerate(filename_words)` - every occurrence,
in natural order - and the reader orders by `filename, position`.

**Existing data.** `storage.migrate_filename_terms()` runs once at
startup, immediately after `create_schema()`. It recreates the table when
`position` is missing, deletes orphan rows, and rewrites any document
whose stored term sequence differs from
`tokenize_filename(os.path.splitext(filename)[0])`. It reports
`schema_changed`, `documents`, `repaired` and `orphans_removed`, and
prints nothing when the database is already correct, so a healthy
database pays nothing per start.

The live database was migrated with it: before, columns
`['filename', 'term']` with 5/13 documents scrambled; the first start
reported `schema_changed=True, repaired=13, orphans_removed=0`; the
second start was silent; after, 0/13 scrambled, 33 rows, primary key
`(filename, position)`.

Because the repair runs before anything reads the table, a database
written by the buggy code self-heals on the first start against the new
build. It also means **end-to-end restart tests are structurally unable
to police the writers** - the repair sanitises whatever they wrote. The
tests that have teeth are the writer-level SQLite inspections and
`test_the_reader_follows_position_not_rowid`; see "Negative controls"
below.

Storage parity keeps the strict comparison for SQLite content and for
ingest, and relaxes exactly one field (`filename_index` on the
reconstruction comparison) with the divergence counted and printed per
document. 44 documents across the 7 fixtures diverge there, which *is*
the fix; everything else still compares byte for byte.

`tools/storage_equivalence.py --negative-control` now also corrupts the
exempted table directly (a changed token value, a dropped row) and
requires both to be reported, so the exemption cannot widen into a hole.

#### Negative controls

`tests/test_filename_order_persistence.py` is 23 tests. Five deliberate
breaks were applied to the shipped code and the observed failures
recorded:

| Break | Observed failure |
| --- | --- |
| writer iterates `set(filename_words)` | 4 failed, incl. `test_positions_are_contiguous_and_unique` |
| writer dedupes with `dict.fromkeys` | 1 failed (repeated filename token collapsed) |
| reader orders by `filename, rowid` | `test_the_reader_follows_position_not_rowid` |
| repair refuses to rewrite anything | `test_an_already_corrupted_database_is_repaired` |
| repair derives the expected order reversed | 9 failed |

Three of those breaks - both writers and the reader - were applied
**before** the reader test existed, and all three passed the suite. The
reason is worth keeping: the startup repair rewrites the rows before
anything reads them, so every restart-based assertion sees repaired,
correct data no matter what the writer did. A green restart test here is
evidence about the repair, not about the writer. That is exactly the
case a negative control exists to catch, and it is why the file also
inspects the table directly and why
`test_the_reader_follows_position_not_rowid` inserts rows *out of rowid
order*: written in order, `ORDER BY rowid` and `ORDER BY position` agree,
so only an out-of-order insert makes the difference observable.

Two of the five controls were themselves wrong on the first attempt (a
patch that matched the module docstring instead of the SQL, and one that
was a no-op on the tested path). Both were corrected before the table
above was recorded.

### Remaining extraction surface (measured)

`tools/` reports this via the dependency analysis below. After layer 4,
`app.py` contains **no** schema creation, no connection factory, no
`SELECT`, and no JSON file I/O. The only SQL left is the incremental
write path, confined to exactly two functions and asserted by
`test_remaining_sql_in_app_is_confined_to_the_incremental_paths`:

| Function | Lines | Reads |
| --- | --- | --- |
| `execute_search` | 1378 | the four index dicts, `INDEX_DATA_LOCK`, and `jsonify` |
| `incrementally_index_document` | 178 | `INDEX_DATA_LOCK`, `INSERT INTO` / `DELETE FROM` |
| `upload_file` | 157 | the four index dicts, `DATA_FOLDER` |
| `rebuild_database` | 153 | the four index dicts, `DATA_FOLDER`, `INDEX_DATA_LOCK` |
| `bulk_delete_documents` | 146 | the four index dicts, `DATA_FOLDER` |
| `open_document` | 85 | the four index dicts, `DATA_FOLDER` |
| `incrementally_remove_document` | 53 | `INDEX_DATA_LOCK`, `DELETE FROM` |
| persistence adapters | ~160 | thin wrappers over `search_engine/storage.py` |

`import sqlite3` and `import json` were removed from `app.py`; both are
now unused, and a test fails if either comes back.

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
3. ~~Move persistence (`load_database`, `save_database`,
   `save_database_snapshot`, `atomic_write_json`, the SQLite helpers).~~
   Done as layer 4.
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
