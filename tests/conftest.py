"""
Shared pytest configuration.

Two classes of test live in this directory:

  * hermetic tests
        Golden vectors, sidecar integrity, tokenizer/rounding contracts
        and other pure checks. These import the application in-process
        against a throwaway data directory and need nothing running.

  * live tests
        ``tests/test_phase12_live.py`` drives a real HTTP server on
        ``127.0.0.1:5000``. These are skipped, with an explicit reason,
        when no server is listening, so that ``python -m pytest`` is
        always meaningful instead of collapsing into connection errors.
"""

import os
import socket
import tempfile

import pytest

# The engine resolves its data directory at import time. Point it at a
# throwaway directory before any test imports ``app``, so running the
# suite never reads or writes the developer's real corpus.
os.environ.setdefault(
    "SEARCH_ENGINE_DATA_DIR",
    tempfile.mkdtemp(prefix="pytest_engine_data_"),
)

LIVE_HOST = "127.0.0.1"
LIVE_PORT = 5000

LIVE_TEST_MODULE = "test_phase12_live.py"


def server_is_listening(host=LIVE_HOST, port=LIVE_PORT, timeout=0.5):
    """Return True when something is accepting connections on host:port."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def pytest_collection_modifyitems(config, items):
    """Skip the live HTTP suite when no server is listening."""

    if server_is_listening():
        return

    skip_marker = pytest.mark.skip(
        reason=(
            f"live server not listening on {LIVE_HOST}:{LIVE_PORT} - "
            "start it with 'python app.py' to run this suite"
        )
    )

    for item in items:
        if os.path.basename(str(item.fspath)) == LIVE_TEST_MODULE:
            item.add_marker(skip_marker)


@pytest.fixture
def app_module():
    """
    The real application module.

    Imported lazily so the SEARCH_ENGINE_DATA_DIR set above is always in
    effect before app.py resolves its data folder.
    """

    import app

    return app
