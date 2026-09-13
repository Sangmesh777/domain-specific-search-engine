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

201 tests in total.

| Suite | Count | Needs a server? | What it covers |
| --- | --- | --- | --- |
| `tests/test_core_units.py` | 94 | no | Individual core modules: tokenizer, filenames, snippets, pagination, storage |
| `tests/test_search_engine_core.py` | 76 | no | `SearchEngine` public API, determinism, rebuild and delete semantics |
| `tests/test_parity_tool.py` | 13 | no | The parity tool's own diff classifier, in both directions |
| `tests/test_golden_vectors.py` | 9 | no | Drift gate on the published golden vectors |
| `tests/test_phase12_live.py` | 9 | yes | Public HTTP API behavior end to end |

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
engine builds from it, and 45 named queries with their complete expected
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
- Golden vectors cannot drift from the engine unnoticed

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
performance budget above. It deliberately contains no Kotlin — no JDK, Gradle
or Android SDK is available to compile or test it here.
