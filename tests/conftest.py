"""
Shared pytest configuration.

Makes the repository root and the development tools importable regardless
of how pytest is invoked (`pytest`, `python -m pytest`, or from another
working directory), so the suites do not silently depend on sys.path
side effects.
"""

import os
import sys


REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

for path in (
    REPO_ROOT,
    os.path.join(REPO_ROOT, "tools"),
):

    if path not in sys.path:
        sys.path.insert(0, path)
