package com.vtu.search.backend

/**
 * The single search contract shared by the offline and online adapters.
 *
 * There is exactly one set of semantics and two execution adapters:
 *
 * ```text
 * UI  ->  SearchBackend  ->  LocalBackend / RemoteBackend
 *                                         |            |
 *                                   engine+SQLite   Flask HTTP
 * ```
 *
 * The UI must never depend on SQLite, HTTP, the ranking algorithm or
 * the filesystem directly.
 */
interface SearchBackend {

    /** Run a paginated query. */
    suspend fun search(query: String, page: Int, limit: Int): SearchResponse

    /** Report the index state and corpus counters. */
    suspend fun status(): CorpusStatus

    /** Add or replace a document. */
    suspend fun indexDocument(name: String, source: DocumentSource): IndexOutcome

    /** Delete one document. Throws [DocumentNotIndexed] when absent. */
    suspend fun delete(name: String): DeleteOutcome

    /**
     * Delete many documents as a single operation.
     *
     * This takes the whole list on purpose. Bulk delete must be one
     * transaction; a UI loop of [delete] calls is a bug, because a
     * partial failure would leave the index in a state the user never
     * asked for.
     */
    suspend fun bulkDelete(names: List<String>): BulkDeleteOutcome

    /** Rebuild the whole index from storage, off the main thread. */
    suspend fun rebuild(): RebuildOutcome

    /** Fetch a document body for the viewer, or null when absent. */
    suspend fun getDocument(name: String): DocumentPayload?

    /** True when the document is in the index. */
    suspend fun hasDocument(name: String): Boolean
}

/** Where a document body comes from. */
sealed interface DocumentSource {
    /** A local file, for the offline backend. */
    data class LocalFile(val path: String) : DocumentSource

    /** An in-memory body, for imports and tests. */
    data class Bytes(val content: ByteArray, val extension: String) : DocumentSource
}

/** A document body handed to the viewer. */
sealed interface DocumentPayload {
    /** A local file path the viewer can open. */
    data class LocalFile(val path: String) : DocumentPayload

    /** Fetched bytes. */
    data class Bytes(val content: ByteArray, val contentType: String?) : DocumentPayload
}

/** Index state. Held in memory only; never persisted. */
enum class IndexState {
    /** The current complete snapshot is searchable. */
    READY,

    /** A rebuild is running; the previous snapshot stays searchable. */
    INDEXING,

    /** The last rebuild failed; the previous snapshot stays searchable. */
    ERROR,
}

data class IndexStatus(
    val state: IndexState,
    val message: String,
    val generation: Int,
    val lastError: String?,
)

data class CorpusStatus(
    val documents: Int,
    val contentTerms: Int,
    val filenamesIndexed: Int,
    val pageTextEntries: Int,
    val indexing: IndexStatus,
)

/** One search result. Field names match the server response. */
data class SearchHit(
    val title: String,
    val snippet: String,
    val page: Int?,
    val highlights: List<Highlight>,
    val phraseOccurrences: Int,
    val filenameScore: Double,
    val contentScore: Double,
    val exactContentMatch: Boolean,
    val prefixSimilarity: Double,
    val numericSimilarity: Double,
    val lexicalMatchRelevance: Double,
    val phraseScore: Double,
    val score: Double,
    val matchType: String,
    val filetypeFilter: String?,
    val tag: String,
    val documentUrl: String,
)

data class Highlight(val start: Int, val end: Int)

data class Pagination(
    val page: Int,
    val limit: Int,
    val total: Int,
    val totalPages: Int,
    val hasNext: Boolean,
    val hasPrevious: Boolean,
    val start: Int,
    val end: Int,
)

/**
 * A search response.
 *
 * The server publishes three shapes and all three are preserved. Do not
 * normalise them into one shape merely because that would be tidier.
 */
sealed interface SearchResponse {
    /** The normal paginated response. */
    data class Paginated(val results: List<SearchHit>, val pagination: Pagination) :
        SearchResponse

    /**
     * The legacy bare list.
     *
     * Reachable on exactly two paths: a quoted phrase that matches no
     * document, and any keyword or prefix query against a corpus with
     * zero documents.
     */
    data class EmptyList(val results: List<SearchHit>) : SearchResponse

    /**
     * The blank-query response.
     *
     * A hardcoded shape that ignores the requested page and limit, and
     * that carries no start or end key.
     */
    data object BlankQuery : SearchResponse
}

data class IndexOutcome(val name: String, val created: Boolean, val replaced: Boolean)

data class DeleteOutcome(val name: String, val deleted: Boolean)

data class BulkDeleteOutcome(
    val deleted: List<String>,
    val notFound: List<String>,
    val failed: List<String>,
)

data class RebuildOutcome(val started: Boolean, val status: IndexStatus)
