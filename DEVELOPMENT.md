# Development Workflow

## Setup

Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
```

## Run the API server

```bash
python app.py
```

Or without the Werkzeug reloader (exactly one process owns the port, which
is what scripts and the test runner need):

```bash
python tools/serve.py --host 127.0.0.1 --port 5000
```

## Run the full regression gate

One command starts the server, seeds the development corpus when it is
empty, runs pytest, and shuts the server down again:

```bash
./run_tests.sh
```

Windows:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\run_tests.ps1
```

Extra pytest arguments are forwarded:

```bash
./run_tests.sh -q tests/test_phase12_live.py
```

## Test suites

253 tests in total.

| Suite | Count | Needs a server? | What it covers |
| --- | --- | --- | --- |
| `tests/test_core_units.py` | 94 | no | Individual core modules: tokenizer, filenames, snippets, pagination, storage |
| `tests/test_search_engine_core.py` | 99 | no | `SearchEngine` public API, determinism, rebuild and delete semantics, adapter boundary |
| `tests/test_parity_tool.py` | 19 | no | The parity tool's own diff classifier, in both directions |
| `tests/test_golden_vectors.py` | 9 | no | Drift gate on the published golden vectors |
| `tests/test_corpus_sidecar.py` | 6 | no | The pre-extracted corpus artifact and the offline indexing path |
| `tests/test_phase12_live.py` | 18 | yes | Public HTTP API end to end, including invalid input and access boundaries |
| `tests/test_vector_coverage_tool.py` | 8 | no | The coverage tool's enumeration, perturbation sizing, and its guarantee that it restores `search.py` byte for byte |

The live suite is **skipped**, never silently passed, when no server is
reachable at `http://127.0.0.1:5000`. Run `./run_tests.sh` so the gate
actually executes.

## Parity and golden vectors

Two tools check that behavior has not moved, and they check different things.

`tools/verify_http_parity.py` runs the refactored HTTP API against the
pre-refactor monolith over 90 checks. It classifies each diff structurally
rather than by an allowlist of query names, so the seven filename-order deltas
introduced deliberately by `18f185b` are reported and pass, while any other
change in ranking, snippets, pagination or response shape fails. The classifier
itself is unit-tested by `tests/test_parity_tool.py`.

`tools/export_golden_vectors.py` writes
`tests/golden/search_engine_vectors.json`: a small corpus, the exact index the
engine builds from it, and 50 named queries with their complete expected
payloads. It is the contract an Android backend or any reimplementation is
checked against, and it records extracted page text rather than parser
behavior so ranking can be tested without porting extraction. Absolute paths
are written as `{{data_folder}}`.

The vectors are generated, never hand-edited. `tests/test_golden_vectors.py`
regenerates and compares them, so a ranking change cannot silently invalidate
the published contract. After an *intended* change:

```bash
.venv/bin/python tools/export_golden_vectors.py
git diff tests/golden/search_engine_vectors.json   # review, then commit
```

See `ANDROID.md` for how a port consumes them.

## Pre-extracted corpus sidecar

`artifacts/android/corpus_sidecar.json` is the second generated contract, and
it exists because document extraction is the one part of the engine that cannot
be ported: PyPDF2 and python-docx have no Android equivalent producing
identical output, so a device that parsed the corpus itself would rank it
differently from the server.

```bash
.venv/bin/python tools/export_corpus_sidecar.py
git diff artifacts/android/corpus_sidecar.json     # review, then commit
```

It records the canonical 13-document corpus as sanitized name, lowercased text,
page views, and an `expected` block of tokenizer outputs, plus the extractor
versions that produced it. An offline backend indexes it through
`SearchEngine.index_extracted(filename, text, pages)` — the engine entry point
that takes text instead of a path, which `index_document` delegates to after
extracting, so both routes share one tokenizing and scoring path.

`tests/test_corpus_sidecar.py` proves the strategy instead of asserting it: one
engine parses the real corpus, another is built purely from the artifact with
no extraction library in the loop, and their indexes and their answers to 19
queries must match. It also gates drift, determinism, schema, the
self-validating `expected` block, and survival across a restart.

## Development corpus

`data/` is gitignored, so a fresh clone has no documents. The live suite
deletes every document it creates and then asserts corpus health
(`content_terms > 0`), which requires a persistent corpus to already
exist.

`tools/seed_dev_corpus.py` builds a deterministic 13-document corpus
(PDF, DOCX, TXT) and uploads it through the real HTTP API:

```bash
python tools/seed_dev_corpus.py
python tools/seed_dev_corpus.py --generate-only --out /tmp/corpus
```

Seeding is idempotent: re-running replaces the same filenames. The corpus
deliberately contains documents matching the parity queries used during
refactoring: `network`, `pdf`, `"network"`, `999`, `BCS502`, pagination and
empty query.

`tools/pdf_writer.py` is a standard-library PDF writer used only by that
seeder, so real multi-page PDFs (and therefore page-level extraction and
page numbers) are exercised without binary fixtures in Git.

## Development rule

After any backend change:

1. Restart the server.
2. Run `./run_tests.sh`.
3. Require all tests to pass before benchmarking or adding the next feature.

A refactor must not change behavior: if a test fails, fix the code, not the
assertion.

## Current regression coverage

- Batch upload accounting
- Exact token search
- Numeric substring search
- Prefix search
- Pagination
- Incremental replacement
- Incremental deletion
- Transactional bulk deletion
- Corpus health
- Filename words persisted in tokenization order (`18f185b`)
- Ranking stable across `PYTHONHASHSEED` values
- Rebuild enumerates the corpus in sorted order, so tied scores rank
  deterministically (`61de47a`)
- Same corpus in different directories ranks identically
- Bulk delete rollback leaves memory, SQLite and the filesystem agreeing
- Bulk delete orphans a file it cannot unlink rather than losing an indexed
  document, and a rebuild recovers it
- Indexing pre-extracted text produces the same index as parsing the file
- `app.py` stays a thin adapter: no domain imports, no domain calls (AST
  enforced, in addition to the core-side guard that it never imports a web
  stack)
- Golden vectors and the corpus sidecar cannot drift from the engine unnoticed
- 74 of the 106 numeric constants in `search_engine/search.py` are pinned by the
  golden vectors, measured by `tools/verify_vector_coverage.py`. The other 32
  are accounted for by group in ANDROID.md: 18 are not ranking values, 3 are
  structurally dead, 3 are live constants pinned by engine tests instead, and 8
  are live thresholds and ceilings the six-document corpus cannot reach, which
  a port must copy by inspection
- A rebuild that fails partway leaves the previous snapshot serving, and a
  restart after a crash mid-rebuild comes back READY and searchable rather
  than wedged on a persisted INDEXING or ERROR
- A failed import leaves nothing behind: no orphan file for a new document,
  and a rejected replacement restores the document it was overwriting
- Invalid HTTP input is refused with a 4xx and leaves the corpus untouched:
  malformed JSON, non-array and empty `filenames`, missing upload part
- No document endpoint can be made to serve anything outside the data folder,
  including the application's own source

## Contract coverage

`tools/verify_vector_coverage.py` answers a question the golden vectors cannot
answer about themselves: what could a reimplementation get wrong and still
reproduce them exactly?

```bash
.venv/bin/python tools/verify_vector_coverage.py
.venv/bin/python tools/verify_vector_coverage.py --filter weight
```

It enumerates every numeric literal in `search_engine/search.py` from the AST,
perturbs one at a time, regenerates the vectors in a subprocess and reports
which perturbations moved a recorded payload. It restores the module byte for
byte afterwards and verifies that it did.

This reports; it does not gate, so it is not part of `./run_tests.sh`. Run it
after any change to the ranking constants, and after adding vector cases, to
see whether coverage improved. ANDROID.md §9 records the current result and
accounts for every constant the vectors cannot reach.

Two failure modes it exists to avoid, both of which yield confident wrong
answers: a perturbation too small to cross a clamp band reports that band as
pinned, and a same-size edit made inside one wall-clock second is served from a
stale `.pyc` and reports a false negative. The tool perturbs coarsely and purges
`__pycache__` before each regeneration.

## Benchmarks

`tools/benchmark_core.py` benchmarks the engine directly, with no server and
no HTTP, in a temporary directory that is cleaned up afterwards. These are the
numbers that describe the code an on-device backend would embed:

```bash
.venv/bin/python tools/benchmark_core.py --documents 1000 --queries 150
```

It reports extraction, import, a search latency distribution (p50/p95/max,
cold and warm), rebuild, bulk delete, SQLite size and peak memory. Import runs
in batches and reports rejected/failed counts, because holding every file
handle open at once exhausts the descriptor limit past a few hundred documents
and would otherwise benchmark a fraction of the corpus while looking like
success.

`benchmark_phase11_1000_docs.py`, `benchmark_phase11_1000_docs_fixed.py`
and `benchmark_phase12_bulk_mutations.py` drive the HTTP API with a large
generated corpus. They are performance tools, not part of the pytest gate,
and are kept for latency comparisons before and after refactors.

`phase12_regression_suite.py` is the original script-style regression run
that predates the pytest suite.

## Android / offline mode

`ANDROID.md` holds the feasibility analysis and architecture design for the
offline Android backend: what ports and what does not, the four portability
traps that silently change ranking (float rounding, Unicode tokenization,
filename sanitization, `math.log` precision), the `SearchBackend` /
`LocalBackend` / `RemoteBackend` design, scoped storage, and the measured
performance budget above.

It deliberately contains no Kotlin. There is no JDK, Gradle or Android SDK in
this environment and no network access to fetch one, so nothing could be
compiled or tested; unverified source would be a liability rather than a
deliverable. What is delivered instead is everything that *can* be verified
here and that a port consumes directly:

| Artifact | Purpose |
| --- | --- |
| `tests/golden/search_engine_vectors.json` | 50 search cases with exact expected payloads — the ranking contract |
| `artifacts/android/corpus_sidecar.json` | Pre-extracted corpus text — indexes offline with no PDF parser |
| `SearchEngine.index_extracted()` | The engine entry point that sidecar is loaded through |
| `ANDROID.md` §11 | Risk-ordered build plan, starting with the vector harness |

Both artifacts are generated, committed, and protected by a drift gate, so a
change to ranking or extraction surfaces as a reviewed diff.
