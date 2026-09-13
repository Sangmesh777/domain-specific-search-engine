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

| Suite | Needs a server? | What it covers |
| --- | --- | --- |
| `tests/test_phase12_live.py` | yes | Public HTTP API behavior end to end |
| `tests/test_search_engine_core.py` | no | `SearchEngine` core API directly |
| `tests/test_core_api_parity.py` | yes | Core result parity against the HTTP API |

The live suite is **skipped**, never silently passed, when no server is
reachable at `http://127.0.0.1:5000`. Run `./run_tests.sh` so the gate
actually executes.

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

## Benchmarks

`benchmark_phase11_1000_docs.py`, `benchmark_phase11_1000_docs_fixed.py`
and `benchmark_phase12_bulk_mutations.py` drive the HTTP API with a large
generated corpus. They are performance tools, not part of the pytest gate,
and are kept for latency comparisons before and after refactors.

`phase12_regression_suite.py` is the original script-style regression run
that predates the pytest suite.
