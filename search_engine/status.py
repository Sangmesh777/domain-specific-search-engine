"""
Indexing status.

The engine reports whether it is searchable right now:

    READY      -> searches are served from a complete snapshot
    INDEXING   -> a rebuild is running; the previous snapshot still serves
    ERROR      -> the last rebuild failed; last_error explains why

This is domain state, not HTTP state: the Flask adapter renders it into
`/api/status` and the rebuild endpoint, and an Android UI renders the very
same values as a progress banner. What the engine deliberately does NOT own
is *scheduling* — starting threads and deciding when to rebuild belongs to
the runtime layer, so no thread is ever created inside the core.

`generation` counts completed rebuilds. A client can poll it to learn that
a rebuild it triggered has finished without parsing messages.
"""

import threading


READY = "READY"
INDEXING = "INDEXING"
ERROR = "ERROR"

READY_MESSAGE = "Search index is ready."


class IndexStatusTracker:
    """Thread-safe INDEXING / READY / ERROR status."""

    def __init__(self):

        self._lock = threading.Lock()

        self._status = {
            "state": READY,
            "message": READY_MESSAGE,
            "started_at": None,
            "completed_at": None,
            "last_error": None,
            "generation": 0,
        }

    def set(
        self,
        state,
        message,
        started_at=None,
        completed_at=None,
        last_error=None,
    ):
        """
        Record a status transition.

        Timestamps are only overwritten when supplied, so a caller can set
        started_at when work begins and completed_at when it ends without
        clobbering the other. last_error is always replaced, which clears it
        on a successful transition.
        """

        with self._lock:

            self._status["state"] = state
            self._status["message"] = message

            if started_at is not None:
                self._status["started_at"] = started_at

            if completed_at is not None:
                self._status["completed_at"] = completed_at

            self._status["last_error"] = last_error

            if state == READY:
                self._status["generation"] += 1

    def mark_ready(self, completed_at):
        """Convenience transition used after any successful mutation."""

        self.set(
            READY,
            READY_MESSAGE,
            completed_at=completed_at,
            last_error=None,
        )

    def snapshot(self):
        """A copy of the current status, safe to hand to a serializer."""

        with self._lock:

            return dict(self._status)

    @property
    def state(self):

        with self._lock:

            return self._status["state"]
