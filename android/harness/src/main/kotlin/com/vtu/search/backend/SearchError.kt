package com.vtu.search.backend

/**
 * Shared domain error model.
 *
 * Both backends raise these, so the UI never branches on HTTP status
 * codes, SQLite errors or IO exceptions. Translation from a transport
 * failure to a domain error happens inside the backend and nowhere
 * else.
 *
 * The mappings below were verified against the running server.
 */
sealed class SearchError(
    message: String,
    cause: Throwable? = null,
) : Exception(message, cause) {

    /**
     * The document could not be accepted for indexing.
     *
     * Server side: an unsupported extension, or a document with no
     * readable text.
     */
    class InvalidDocument(message: String, cause: Throwable? = null) :
        SearchError(message, cause)

    /**
     * A delete or lookup named a document that is not indexed.
     *
     * Server side, verified: `DELETE /api/documents/<unknown>` returns
     * HTTP 404 with `{"error": "Document is not indexed."}`.
     */
    class DocumentNotIndexed(message: String, cause: Throwable? = null) :
        SearchError(message, cause)

    /** The document exists but its body is unavailable. */
    class DocumentNotFound(message: String, cause: Throwable? = null) :
        SearchError(message, cause)

    /** Local storage failed. */
    class StorageError(message: String, cause: Throwable? = null) :
        SearchError(message, cause)

    /** Indexing a document failed. */
    class IndexingFailed(message: String, cause: Throwable? = null) :
        SearchError(message, cause)

    /** A rebuild failed. The previous snapshot remains searchable. */
    class RebuildFailed(message: String, cause: Throwable? = null) :
        SearchError(message, cause)

    /** The query could not be executed. */
    class SearchError_(message: String, cause: Throwable? = null) :
        SearchError(message, cause)

    /**
     * The remote backend could not complete the request.
     *
     * Raised for connection failures, timeouts and unexpected remote
     * statuses. It must surface to the user rather than triggering a
     * silent fall back to local search: the local and remote corpora
     * can hold different documents, so an automatic fall back would
     * answer a different question than the user asked.
     */
    class RemoteError(
        message: String,
        val statusCode: Int? = null,
        cause: Throwable? = null,
    ) : SearchError(message, cause)
}

/**
 * Map an HTTP status and body from the Flask API onto a domain error.
 *
 * Bulk delete is deliberately absent: the server reports missing names
 * inside a HTTP 200 body, so it is a result rather than an error.
 */
fun mapRemoteFailure(statusCode: Int, body: String): SearchError = when (statusCode) {
    404 -> SearchError.DocumentNotIndexed(body)
    400 -> SearchError.InvalidDocument(body)
    409 -> SearchError.IndexingFailed(body)
    500, 502, 503, 504 -> SearchError.RemoteError(
        "Remote search service failed with HTTP $statusCode",
        statusCode,
    )
    else -> SearchError.RemoteError(
        "Unexpected remote status HTTP $statusCode",
        statusCode,
    )
}
