# Domain-Specific Search Engine

A document search engine with a Python/Flask API and a React web interface.

## Features

- Search document content and filenames with deterministic ranking
- Phrase, prefix, numeric, and filename matching
- Ranked results with snippets and highlights
- PDF, DOCX, and TXT document support
- File-type filtering and paginated results
- Upload, open, delete, bulk-delete, and re-index documents
- SQLite-backed persistence with recovery on restart

## Stack

- **Backend:** Python, Flask, SQLite
- **Frontend:** React and Vite

## Run locally

### 1. Start the API

Use Python 3.10 or newer.

```bash
python -m venv .venv
```

Activate the environment, then install the backend dependencies:

```bash
# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate
```

```bash
python -m pip install -r requirements.txt
python app.py
```

The Flask API listens on `http://127.0.0.1:5000`. Search data is stored in
the local `data/` directory, which is intentionally excluded from version
control. Set `SEARCH_ENGINE_DATA_DIR` before starting the API to use another
data directory.

### 2. Start the web interface

Open a second terminal:

```bash
cd frontend
npm ci
npm run dev
```

Vite serves the frontend and proxies `/api` requests to the Flask API.

To create a production frontend build:

```bash
npm run build
```

## API quick check

With the backend running:

```bash
curl http://127.0.0.1:5000/api/status
curl "http://127.0.0.1:5000/api/search?q=network"
```

## Project layout

```text
app.py                 Flask API and application orchestration
search_engine/         Search, text processing, indexing, ranking, storage
frontend/              React/Vite user interface
requirements.txt       Python runtime dependencies
```
