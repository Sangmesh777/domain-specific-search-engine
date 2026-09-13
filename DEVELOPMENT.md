# Phase 13 Development Workflow

## Setup

Install development dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

## Run Flask

```powershell
python app.py
```

## Run regression tests

From the project root:

```powershell
python -m pytest
```

Or:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\run_tests.ps1
```

The live tests expect Flask to be available at:

`http://127.0.0.1:5000`

## Development rule

After any backend change:

1. Restart Flask.
2. Run `python -m pytest`.
3. Require all tests to pass before benchmarking or adding the next feature.

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
