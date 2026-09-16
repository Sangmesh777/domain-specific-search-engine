# Domain-Specific Search Engine

A document search engine with one ranking contract and two execution
modes:

```text
                         SEARCH ENGINE
                              │
               ┌──────────────┴──────────────┐
               │                             │
          ONLINE MODE                  OFFLINE MODE
               │                             │
          Flask/API                    Android APK
               │                             │
        RemoteBackend                  LocalBackend
               │                             │
         Server SQLite               Android SQLite
         Server files                Local app files
         Server index                Local index
```

The online half works today. **The offline half is specified but not
built**, because no Android toolchain exists in the environment where
this work was done. See [Status](#status) for exactly what is real.

## Status

| Area | State | Evidence |
| --- | --- | --- |
| Flask search API | **Working** | 275 tests pass with the server running |
| `search_engine/` extraction | **Done** | 5 layers; app.py 4085 -> 1571 lines; engine and storage are Flask-free and parity-proven. Route handlers other than search are still long (see DEVELOPMENT.md) |
| Ranking engine | **Working** | 79/79 golden vectors, exact binary64 |
| React web UI | **Working** | builds cleanly; live search verified through the proxy |
| Test suite | **Working** | 275 pass with a server; 266 pass + 9 skip without one |
| Shadow parity gate | **Working** | 12 functions, 21,454 comparisons, 0 differences |
| Memory mutation coherence | **Closed** | copy-on-write publish; 20 tests, all 5 negative controls confirmed |
| Persistence extraction | **Done** | app.py holds no SQL; 250,388 values compared vs monolith, 0 differences, 6/6 controls detected |
| Filename order across restart | **Fixed** | explicit `position` column; 8/8 hash seeds keep the order; corrupt databases self-repair at startup |
| JVM semantics | **Verified** | real JVM: 0 genuine divergences, rounding exact |
| Golden vector gate | **Working** | catches drift; proven by negative control |
| Corpus sidecar | **Generated** | 13 documents, 191 terms, integrity hash verified |
| Android app | **Not built** | no JDK, no SDK, no Gradle; no APK |
| Android parity core | **Uncompiled** | algorithms verified; Kotlin syntax never compiled |
| Cross-mode parity | **Not run** | needs `LocalBackend` |

There is no APK. Any claim of one would be false.

## What it does

Given a query such as `BCS502 Module 2.pdf`, `distributed ledger` or
`9901`, the engine returns ranked documents with snippets and
highlights. It combines:

* exact content matches
* prefix matching (`net` → `network`)
* numeric substring matching (`990` → `9901xyz`)
* filename matching, including bare numeric filename tokens
* phrase scoring for multi-word and quoted queries
* `pdf` / `docx` / `txt` filetype filtering
* pagination

## Running it

### 1. Install dependencies

```bash
python3 -m pip install -r requirements-dev.txt
```

### 2. Start the API

```bash
python app.py
```

It listens on `0.0.0.0:5000` and stores its corpus in `./data`, which is
gitignored. Set `SEARCH_ENGINE_DATA_DIR` to use a different directory.

### 3. Start the web UI

```bash
cd frontend
npm install
npm run dev
```

The UI is served on `:5173` and proxies `/api` to the Flask engine, so
the browser only ever talks to its own origin.

## Using the API

```bash
# Status: document and term counts, index state
curl http://127.0.0.1:5000/api/status

# Upload
curl -X POST http://127.0.0.1:5000/api/upload -F "file=@notes.txt"

# Search
curl "http://127.0.0.1:5000/api/search?q=network&page=1&limit=10"

# Filter by file type
curl "http://127.0.0.1:5000/api/search?q=sharedterm%20pdf"

# Open a document
curl http://127.0.0.1:5000/api/documents/notes.txt

# Delete one document
curl -X DELETE http://127.0.0.1:5000/api/documents/notes.txt

# Bulk delete: one request, one transaction
curl -X POST http://127.0.0.1:5000/api/documents/bulk-delete \
     -H 'Content-Type: application/json' \
     -d '{"filenames": ["a.txt", "b.txt"]}'

# Rebuild the index from disk
curl -X POST http://127.0.0.1:5000/api/rebuild
```

### Response shapes

The API publishes three shapes, all deliberate and all pinned by tests:

1. **Paginated object** — the normal case, with `results` and
   `pagination`.
2. **Bare JSON list** — a quoted phrase matching nothing, or any
   keyword query against an empty corpus.
3. **Blank-query object** — `?q=` returns a hardcoded `page: 1`,
   `limit: 10` object with no `start`/`end` keys, ignoring the requested
   parameters.

Do not normalise these into one shape. Clients depend on them.

## Running the tests

```bash
./run_tests.sh
```

That runs the byte-compile check, the golden vector gate, the sidecar
integrity check and the pytest suite.

```bash
python3 -m pytest -q          # just the suite
```

The live HTTP tests need a server. Without one they **skip with an
explicit reason** rather than failing, so the suite is always
meaningful:

```text
with a server     : 118 passed
without a server  : 109 passed, 9 skipped
```

## Verifying the search contract

```bash
# Replay every vector and diff the complete response
python3 -m tools.verify_golden_vectors

# Regenerate the vectors (byte-identical across runs)
python3 -m tools.generate_golden_vectors

# Rebuild the Android corpus sidecar
python3 -m tools.build_corpus_sidecar
```

`tests/golden/search_engine_vectors.json` contains 79 complete search
responses and 122 contract cases covering tokenizer, sanitizer, query
parsing and rounding semantics. It is the specification the Android port
must satisfy.

## Layout

```text
app.py                       Flask API and ranking engine
tools/
  golden_corpus.py           The 13-document golden corpus definition
  corpus_materialize.py      Writes it as real TXT/DOCX/PDF files
  contract_vectors.py        Shared parity contract tables
  engine_session.py          Isolated-engine bootstrap shared by tools
  generate_golden_vectors.py Records the vectors
  verify_golden_vectors.py   The parity gate
  build_corpus_sidecar.py    Builds the Android bootstrap artifact
  port_model.py              Executable model of the Kotlin ports
  extract_modules.py         Slices modules out of app.py by AST line range
  storage_extraction_spec.py The persistence layer's extraction spec
  shadow_parity.py           Extracted functions vs the originals
  mutation_equivalence.py    Copy-on-write planners vs the in-place helpers
  storage_probe.py           Dumps one engine session for comparison
  storage_equivalence.py     Reconstruction parity vs the monolith
  storage_parity_controls.py Proves the parity gate can fail
tests/
  test_phase12_live.py       Live HTTP regression suite
  test_tokenizer_contract.py Unicode, sanitizer and query contracts
  test_golden_artifacts.py   Artifact integrity
  test_golden_vectors.py     Full vector replay
  test_restart_recovery.py   Restart, bulk delete and rebuild
  test_port_model.py         Verifies the Kotlin port algorithms
  test_index_state.py        Index ownership and torn-read guards
  test_memory_mutation_atomicity.py  Copy-on-write publication and its AST guards
  test_storage_parity.py     Reconstruction parity and transport independence
  test_filename_order_persistence.py  Filename token order across a restart
  test_incremental_write_path.py  The extracted single-document write path
  test_engine_layer.py       Transport independence of the ranking core
  test_shadow_parity.py      Guards the shadow-parity function set
  test_jvm_semantics.py      JVM semantics behind the Kotlin port
  golden/                    The golden vectors
artifacts/android/
  corpus_sidecar.json        Extracted corpus for the Android bootstrap
frontend/                    React + Vite web UI
android/                     Android port: specification and harness
run_tests.sh                 Full verification gate
```

## Android

See [ANDROID.md](ANDROID.md) for the port specification and
[android/README.md](android/README.md) for the module layout.

The short version: the parity-critical algorithms are written, the rest
is specified, and none of it has been compiled. The harness module is
deliberately Android-free so that parity can be verified with only a
JDK, before the Android SDK is involved.

```bash
android/tools/run_parity.sh    # needs a JDK
```

## Documentation

* [DEVELOPMENT.md](DEVELOPMENT.md) — architecture and development workflow
* [ANDROID.md](ANDROID.md) — Android port specification
* [android/README.md](android/README.md) — Android module layout
