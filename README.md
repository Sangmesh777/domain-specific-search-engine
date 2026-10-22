# Domain-Specific Search Engine

A document search engine with a Python/Flask API and React web interface.
The search engine and its ranking contract are implemented in Python; the
web client calls the API through a same-origin development proxy.

## Features

- Full-text document search with deterministic ranking
- Filename and content matching, including prefix and numeric matching
- Phrase matching and snippets
- File-type filtering for PDF, DOCX and TXT
- Paginated results
- Document upload, opening, deletion and index rebuilding
- SQLite-backed persistence and restart recovery
- Golden-vector and adversarial regression tests

## Architecture

```text
React / Vite frontend
        |
        | same-origin /api requests
        v
Flask API (app.py)
        |
        v
Transport-independent search_engine package
        |
        +-- text normalization and tokenization
        +-- document extraction and indexing
        +-- deterministic ranking and snippets
        +-- SQLite / JSON persistence
```

## Requirements

- Python 3.10 or later
- Dependencies from `requirements-dev.txt`
- Node.js and npm for the React frontend

## Run locally

Install Python dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

Start the API:

```bash
python app.py
```

The API listens on port 5000 by default. Corpus data is stored under the
ignored `data/` directory. Set `SEARCH_ENGINE_DATA_DIR` before starting
the app to use a different corpus directory.

In another terminal, start the frontend:

```bash
cd frontend
npm install
npm run dev
```

The Vite development server proxies `/api` to the Flask API.

## Verify changes

Run the complete Python verification suite:

```bash
./run_tests.sh
```

Or run pytest directly:

```bash
python -m pytest -q
```

The full gate includes byte-compilation, golden-vector checks, extracted
module parity, mutation/storage equivalence checks, negative controls,
and the test suite. Live HTTP tests skip with an explicit reason when no
server is running.

The golden contract lives in `tests/golden/search_engine_vectors.json`.
Do not hand-edit generated expectations; regenerate them through the
documented tooling if search behavior intentionally changes.

## Repository layout

- `app.py`: Flask transport/API adapter
- `search_engine/`: transport-independent search, indexing and storage
- `frontend/`: React and Vite web application
- `tests/`: regression, persistence, concurrency and golden-vector tests
- `tools/`: corpus, parity, extraction and verification tooling
- `run_tests.sh`: full verification gate

This repository focuses on the Python search engine and web application.
Android application code and Android-specific parity artifacts are not
part of this project configuration.
