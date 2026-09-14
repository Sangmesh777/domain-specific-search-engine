# VTU Search Engine

A domain-specific document search engine over a corpus of PDF, DOCX and TXT
files, with one ranking implementation behind two transports.

```text
                ONE SEARCH ENGINE CONTRACT
                          │
            ┌─────────────┴─────────────┐
            │                           │
      OFFLINE / LOCAL              ONLINE / REMOTE
            │                           │
      Android APK                  Flask HTTP API
            │                           │
      LocalBackend                 RemoteBackend
            │                           │
      local SQLite                 server SQLite
      local documents              server documents
```

The point of the split is that ranking exists once. `search_engine/` owns
tokenization, scoring, snippets and pagination; the Flask app is an adapter that
parses HTTP and serializes JSON. An offline Android client is meant to embed the
same engine against device storage, and to be checked against the same published
contract as the server, so the two modes cannot drift into being two different
search products.

## Status

Stated plainly, because "designed" and "working" are different claims:

| Component | State |
| --- | --- |
| `search_engine/` core | **Implemented and tested.** Transport-independent, no web-stack imports, AST-enforced. |
| Flask HTTP API | **Implemented and tested.** Adapters only; domain logic in routes is AST-enforced absent. |
| React frontend | **Present**, drives the HTTP API. |
| Regression suite | **263 tests passing** across 7 suites, including live-HTTP tests. |
| Parity vs the pre-refactor monolith | **Verified**: 90 checks, 0 mismatches against commit `81faff9`. |
| Golden vectors (ranking contract) | **Published and drift-gated**: 6 documents, 50 cases. |
| Pre-extracted Android corpus | **Published and drift-gated**: 13 documents. |
| Android app | **Designed, not implemented.** No Kotlin, no Gradle project, no APK. |
| Android device/emulator testing | **Not done.** |

The Android half is blocked on toolchain, not on design. The environment this
was written in has no JDK, Gradle or Android SDK, and the package mirrors that
would supply them are unreachable — see
[ANDROID.md](ANDROID.md) §12 for what exists instead, and §11 for the build
order to follow once a toolchain is available. Nothing here should be read as
claiming an APK exists.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt

# start the API on http://127.0.0.1:5000
.venv/bin/python tools/serve.py --host 127.0.0.1 --port 5000
```

The data folder is `./data`, created on first run and gitignored. To populate it
with a development corpus:

```bash
.venv/bin/python tools/seed_dev_corpus.py --base-url http://127.0.0.1:5000
```

Supported document types are PDF, DOCX and TXT. PDFs are indexed page-accurately;
DOCX and TXT have no fixed page geometry and are treated as one logical page,
which is long-standing product behavior rather than an oversight.

## Tests

One command runs the whole gate. It starts the API, seeds the corpus if the
server owns no documents, runs pytest, and stops the API again:

```bash
./run_tests.sh
```

```text
263 passed
```

The live-HTTP suite is **skipped, never silently passed**, when no server is
reachable at `http://127.0.0.1:5000`. Use `./run_tests.sh` so the gate actually
executes rather than reporting a smaller number. On Windows, `run_tests.ps1` is
the equivalent path.

| Suite | Count | Needs a server? | Covers |
| --- | --- | --- | --- |
| `tests/test_core_units.py` | 94 | no | Tokenizer, filenames, snippets, pagination, storage |
| `tests/test_search_engine_core.py` | 105 | no | Engine API, determinism, delete/rebuild semantics, crash recovery, reachability thresholds and the phrase-frequency tail, adapter boundary |
| `tests/test_parity_tool.py` | 19 | no | The parity tool's own diff classifier |
| `tests/test_golden_vectors.py` | 9 | no | Drift gate on the published ranking contract |
| `tests/test_corpus_sidecar.py` | 6 | no | The pre-extracted corpus artifact and offline indexing path |
| `tests/test_phase12_live.py` | 18 | **yes** | Public HTTP API end to end, invalid input, access boundaries |
| `tests/test_vector_coverage_tool.py` | 12 | no | The coverage tool's enumeration, its restore guarantee, and the full-sweep taxonomy gate (no constant left unclassified or live-uncovered) |

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/upload` | Multipart upload; one SQLite transaction for the whole batch |
| `GET` | `/api/search` | Search, with `q`, `page`, `limit` |
| `GET` | `/api/documents/<filename>` | Document metadata |
| `DELETE` | `/api/documents/<filename>` | Delete one document |
| `POST` | `/api/documents/bulk-delete` | Delete many; JSON `{"filenames": [...]}` |
| `POST` | `/api/rebuild` | Rebuild the whole index from the data folder |
| `GET` | `/api/status` | Counts and `READY` / `INDEXING` / `ERROR` |

Batch operations stay partial by design: a bulk delete or upload that hits one
bad document returns `200` with per-document `deleted`, `not_found`, `rejected`
and `failed` lists, rather than discarding the good work in the batch.

Document identity is the **filename**, never a path. Paths are derived data that
a rebuild regenerates, so a client should send filenames back and treat any path
it received as display-only.

## Frontend

React + Vite, in `frontend/`:

```bash
cd frontend
npm install
npm run dev
```

## Repository layout

```text
app.py                  Flask HTTP adapter — routing, parsing, serialization
search_engine/          the engine: ranking, indexing, storage, extraction
  engine.py             SearchEngine public API
  search.py             the single ranking implementation
  tokenizer.py          Unicode-aware tokenization
  filenames.py          vendored secure_filename behavior
  storage.py            SQLite persistence
  index.py              in-memory snapshot
  rebuild.py            full rebuild from the data folder
  status.py             READY / INDEXING / ERROR
  results.py            outcome vocabulary for mutations
  extraction.py         PDF / DOCX / TXT text extraction
  query.py  snippets.py  pagination.py  config.py
tools/                  generators, benchmarks and verification
tests/                  7 suites; tests/golden/ holds the ranking contract
artifacts/android/      pre-extracted corpus for the offline backend
frontend/               React UI
ANDROID.md              the Android port: architecture, traps, contracts
DEVELOPMENT.md          how the pieces fit and how to verify them
```

## Tools

```bash
# regenerate the golden vectors (the ranking contract)
.venv/bin/python tools/export_golden_vectors.py

# regenerate the pre-extracted corpus sidecar for offline Android
.venv/bin/python tools/export_corpus_sidecar.py

# measure which ranking constants the golden vectors actually pin
.venv/bin/python tools/verify_vector_coverage.py

# core performance benchmarks, no server involved
.venv/bin/python tools/benchmark_core.py --documents 1000 --queries 150

# HTTP parity against the pre-refactor monolith (needs both servers running)
.venv/bin/python tools/verify_http_parity.py
```

`tools/verify_vector_coverage.py` is worth running after any change to the
ranking constants. It answers a question the contract cannot answer about
itself — what could a reimplementation get wrong and still reproduce the vectors
exactly — by perturbing each numeric constant in `search_engine/search.py` and
reporting which ones move a recorded payload. Current result: 106 constants, 74
pinned; the 32 the vectors cannot reach are classified in the tool itself — 17
not ranking values, 5 provably inert, 10 pinned by named falsified engine
tests, **0 live and uncovered** — and the suite gates that taxonomy, so it
cannot rot. [ANDROID.md](ANDROID.md) §9 publishes the full accounting.

## Performance

Development-machine numbers for the engine directly, 1000 documents of roughly
400 words each, 150 queries:

| Measurement | Value |
| --- | --- |
| Search p50 / p95 / max | 72.1 / 109.9 / 119.3 ms |
| Search p50 warm | 49.8 ms |
| Import per document | 1.22 ms |
| Full rebuild | 852 ms |
| Bulk delete (20) | 26.3 ms |
| SQLite per document | 8.79 KiB |
| Peak resident memory | 48.9 MiB |

These are laptop figures and say nothing about a phone. Search is
O(corpus size) rather than O(matches), which is the thing to watch when the
corpus grows. On-device numbers do not exist yet.

## Documentation

- **[DEVELOPMENT.md](DEVELOPMENT.md)** — architecture, the adapter boundary and
  how it is enforced, determinism guarantees, benchmarks, contract coverage.
- **[ANDROID.md](ANDROID.md)** — the Android port: feasibility verdict,
  architecture, `SearchBackend` / `LocalBackend` / `RemoteBackend`, the error
  model, the index state model, on-device storage, extraction strategy, the
  portability traps that silently change ranking, the golden vectors and what
  they do and do not pin, and the recommended build order.

## Requirements

Python 3.11. Flask, flask-cors, PyPDF2, python-docx; pytest and requests for
the test suite. PyPDF2 is deprecated upstream in favour of `pypdf` and emits a
warning on import; migrating it would change extraction output and therefore the
published corpus artifacts, so it has been left alone deliberately.
