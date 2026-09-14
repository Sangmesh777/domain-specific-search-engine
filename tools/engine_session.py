"""
Boot the real search engine against a throwaway corpus.

`app.py` resolves its data directory at import time, so any tool that
needs an isolated engine must set ``SEARCH_ENGINE_DATA_DIR`` *before*
importing the application. This module centralises that, so the golden
vector generator, the vector verifier and the corpus sidecar builder
cannot drift apart.
"""

import contextlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def isolated_engine(populate_corpus=False):
    """
    Yield ``(client, engine_module, workspace)`` for an isolated engine.

    The engine runs against a fresh temporary data directory, so the
    developer's real corpus is never read or modified.
    """

    workspace = tempfile.mkdtemp(prefix="search_engine_session_")

    os.environ["SEARCH_ENGINE_DATA_DIR"] = os.path.join(workspace, "data")

    original_sys_path = list(sys.path)

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    try:
        import app as engine

        engine.app.config["TESTING"] = True
        client = engine.app.test_client()

        if populate_corpus:
            from tools.corpus_materialize import materialize_corpus

            corpus_dir = os.path.join(workspace, "corpus")
            materialize_corpus(corpus_dir)
            upload_corpus(client, corpus_dir)

        yield client, engine, workspace

    finally:
        sys.path[:] = original_sys_path
        shutil.rmtree(workspace, ignore_errors=True)


def upload_corpus(client, corpus_dir):
    """Upload every file in `corpus_dir`, one request per file."""

    responses = []

    for name in sorted(os.listdir(corpus_dir)):
        path = os.path.join(corpus_dir, name)

        with open(path, "rb") as handle:
            # Werkzeug's file tuple order is
            # (file object, filename, content type).
            response = client.post(
                "/api/upload",
                data={"file": (handle, name, "application/octet-stream")},
            )

        if response.status_code != 200:
            raise RuntimeError(
                f"Upload of {name!r} returned HTTP {response.status_code}: "
                f"{response.get_data(as_text=True)[:300]}"
            )

        responses.append(response.get_json())

    return responses


def wait_for_index_ready(client, expected_documents, timeout=120.0):
    """Block until the engine reports a settled READY index."""

    deadline = time.time() + timeout
    last_status = None

    while time.time() < deadline:
        last_status = client.get("/api/status").get_json()

        if (
            last_status.get("indexing", {}).get("state") == "READY"
            and last_status.get("documents") == expected_documents
        ):
            return last_status

        time.sleep(0.05)

    raise RuntimeError(
        f"Index did not settle at {expected_documents} documents; "
        f"last status was {last_status}"
    )
