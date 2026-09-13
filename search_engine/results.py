"""
Operation results.

Every engine mutation returns a structured result instead of raising for
expected outcomes or inventing transport concepts. Adapters map these to
their own idiom:

    Flask    -> JSON body + HTTP status code
    Android  -> a Kotlin sealed class / coroutine result

Keeping the outcome vocabulary in the core is what stops the two adapters
from drifting: "document is not indexed" means the same thing offline and
online, and only the rendering differs.

Expected outcomes are values, not exceptions. Genuine faults (a database
that cannot be written) still raise, because silently swallowing them is
how corruption hides.
"""


class ImportResult:
    """Outcome of importing a batch of documents."""

    def __init__(self):

        self.uploaded = []
        self.created = []
        self.replaced = []
        self.rejected = []
        self.failed = []

    @property
    def uploaded_count(self):
        return len(self.uploaded)

    @property
    def created_count(self):
        return len(self.created)

    @property
    def replaced_count(self):
        return len(self.replaced)

    @property
    def rejected_count(self):
        return len(self.rejected)

    @property
    def failed_count(self):
        return len(self.failed)

    @property
    def ok(self):
        """True when nothing was rejected and nothing failed."""

        return not self.rejected and not self.failed

    def __repr__(self):
        return (
            "ImportResult("
            f"created={self.created_count}, "
            f"replaced={self.replaced_count}, "
            f"rejected={self.rejected_count}, "
            f"failed={self.failed_count})"
        )


# Outcome vocabulary for a single-document delete.
DELETE_DELETED = "deleted"
DELETE_INVALID_PATH = "invalid_path"
DELETE_NOT_INDEXED = "not_indexed"
DELETE_FILE_NOT_FOUND = "file_not_found"
DELETE_FAILED = "failed"


class DeleteResult:
    """Outcome of deleting one document."""

    def __init__(
        self,
        outcome,
        filename=None,
        reason=None,
    ):

        self.outcome = outcome
        self.filename = filename
        self.reason = reason

    @property
    def deleted(self):
        return self.outcome == DELETE_DELETED

    def __repr__(self):
        return (
            f"DeleteResult({self.outcome!r}, "
            f"filename={self.filename!r}, "
            f"reason={self.reason!r})"
        )


class BulkDeleteResult:
    """
    Outcome of deleting many documents in one transaction.

    `requested` is the normalized, de-duplicated batch actually attempted,
    so an adapter can tell "nothing valid was supplied" apart from
    "everything supplied was missing".
    """

    def __init__(self, requested):

        self.requested = list(requested)
        self.deleted = []
        self.not_found = []
        self.failed = []

    @property
    def deleted_count(self):
        return len(self.deleted)

    @property
    def not_found_count(self):
        return len(self.not_found)

    @property
    def failed_count(self):
        return len(self.failed)

    @property
    def ok(self):
        return not self.failed

    def __repr__(self):
        return (
            "BulkDeleteResult("
            f"deleted={self.deleted_count}, "
            f"not_found={self.not_found_count}, "
            f"failed={self.failed_count})"
        )
