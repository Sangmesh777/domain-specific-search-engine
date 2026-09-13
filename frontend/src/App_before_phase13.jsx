import { useEffect, useMemo, useRef, useState } from 'react'
import './App.css'

const API_BASE_URL = 'http://127.0.0.1:5000'

function renderHighlightedSnippet(text, highlights = []) {
  if (!text) {
    return null
  }

  if (!Array.isArray(highlights) || highlights.length === 0) {
    return text
  }

  const sortedHighlights = [...highlights]
    .filter(
      (highlight) =>
        Number.isFinite(highlight?.start) &&
        Number.isFinite(highlight?.end),
    )
    .sort((a, b) => a.start - b.start)

  const parts = []
  let currentPosition = 0

  sortedHighlights.forEach((highlight, index) => {
    const start = Math.max(
      currentPosition,
      Math.min(highlight.start, text.length),
    )

    const end = Math.max(
      start,
      Math.min(highlight.end, text.length),
    )

    if (start > currentPosition) {
      parts.push(
        <span key={`text-${index}`}>
          {text.slice(currentPosition, start)}
        </span>,
      )
    }

    if (end > start) {
      parts.push(
        <mark key={`highlight-${index}`}>
          {text.slice(start, end)}
        </mark>,
      )
    }

    currentPosition = end
  })

  if (currentPosition < text.length) {
    parts.push(
      <span key="text-final">
        {text.slice(currentPosition)}
      </span>,
    )
  }

  return parts
}

function getPageNumbers(currentPage, totalPages) {
  if (totalPages <= 1) {
    return []
  }

  if (totalPages <= 7) {
    return Array.from(
      { length: totalPages },
      (_, index) => index + 1,
    )
  }

  const pages = [1]
  const start = Math.max(2, currentPage - 1)
  const end = Math.min(totalPages - 1, currentPage + 1)

  if (start > 2) {
    pages.push('ellipsis-start')
  }

  for (let page = start; page <= end; page += 1) {
    pages.push(page)
  }

  if (end < totalPages - 1) {
    pages.push('ellipsis-end')
  }

  pages.push(totalPages)

  return pages
}

function App() {
  // ------------------------------------------------------------
  // SEARCH STATE
  // ------------------------------------------------------------

  const [query, setQuery] = useState('network')
  const [isSearching, setIsSearching] = useState(false)
  const [results, setResults] = useState([])
  const [hasSearched, setHasSearched] = useState(false)
  const [searchError, setSearchError] = useState('')

  // ------------------------------------------------------------
  // PAGINATION STATE
  // ------------------------------------------------------------

  const [page, setPage] = useState(1)

  // 10 is the production-friendly default.
  // Use the selector to switch to 20 or 50 when required.
  const [limit, setLimit] = useState(10)

  const [pagination, setPagination] = useState({
    page: 1,
    limit: 10,
    total: 0,
    total_pages: 0,
    has_next: false,
    has_previous: false,
    start: 0,
    end: 0,
  })

  // ------------------------------------------------------------
  // UPLOAD STATE
  // ------------------------------------------------------------

  const [selectedFiles, setSelectedFiles] = useState(null)
  const [uploadMessage, setUploadMessage] = useState('')
  const [deletingDocument, setDeletingDocument] = useState('')

  const [indexingStatus, setIndexingStatus] = useState({
    state: 'READY',
    message: 'Search index is ready.',
    generation: 0,
    last_error: null,
  })

  // Search refresh requested by an upload/delete. The request is
  // executed only after the background index rebuild returns READY.
  const [pendingRefreshPage, setPendingRefreshPage] = useState(null)

  // ------------------------------------------------------------
  // FRONTEND REQUEST CONTROL
  // ------------------------------------------------------------
  //
  // Search requests are cancellable. A newer request invalidates the
  // previous one so stale responses cannot overwrite newer results.
  const searchAbortControllerRef = useRef(null)
  const searchRequestIdRef = useRef(0)
  const statusAbortControllerRef = useRef(null)
  const statusPollTimeoutRef = useRef(null)
  const mountedRef = useRef(true)

  const [isMutating, setIsMutating] = useState(false)

  // ------------------------------------------------------------
  // INDEXING STATUS
  // ------------------------------------------------------------

  useEffect(() => {
    mountedRef.current = true

    const clearStatusPolling = () => {
      if (statusPollTimeoutRef.current) {
        window.clearTimeout(statusPollTimeoutRef.current)
        statusPollTimeoutRef.current = null
      }
    }

    const fetchIndexStatus = async () => {
      statusAbortControllerRef.current?.abort()

      const controller = new AbortController()
      statusAbortControllerRef.current = controller

      try {
        const response = await fetch(
          `${API_BASE_URL}/api/status`,
          {
            signal: controller.signal,
            cache: 'no-store',
          },
        )

        if (!response.ok) {
          throw new Error(
            `Status request failed: ${response.status}`,
          )
        }

        const data = await response.json()

        if (!mountedRef.current || !data.indexing) {
          return
        }

        setIndexingStatus(data.indexing)

        if (data.indexing.state === 'INDEXING') {
          clearStatusPolling()
          statusPollTimeoutRef.current =
            window.setTimeout(
              fetchIndexStatus,
              500,
            )
        } else {
          clearStatusPolling()
        }
      } catch (error) {
        if (error?.name === 'AbortError') {
          return
        }

        console.error(
          'Index status request failed:',
          error,
        )
      }
    }

    fetchIndexStatus()

    return () => {
      mountedRef.current = false
      clearStatusPolling()
      statusAbortControllerRef.current?.abort()
      searchAbortControllerRef.current?.abort()
    }
  }, [])

  const isIndexing =
    indexingStatus.state === 'INDEXING'

  // ------------------------------------------------------------
  // SEARCH
  // ------------------------------------------------------------

  const handleSearch = async (requestedPage = 1) => {
    const trimmedQuery = query.trim()

    if (!trimmedQuery) {
      searchAbortControllerRef.current?.abort()
      searchRequestIdRef.current += 1

      setResults([])
      setPagination({
        page: 1,
        limit,
        total: 0,
        total_pages: 0,
        has_next: false,
        has_previous: false,
        start: 0,
        end: 0,
      })
      setPage(1)
      setSearchError('')
      setHasSearched(true)
      return
    }

    const safePage = Math.max(
      1,
      requestedPage,
    )

    if (isIndexing || isMutating) {
      return
    }

    searchAbortControllerRef.current?.abort()

    const controller = new AbortController()
    searchAbortControllerRef.current = controller

    const requestId =
      searchRequestIdRef.current + 1

    searchRequestIdRef.current = requestId

    setIsSearching(true)
    setSearchError('')

    try {
      const params = new URLSearchParams({
        q: trimmedQuery,
        page: String(safePage),
        limit: String(limit),
      })

      const response = await fetch(
        `${API_BASE_URL}/api/search?${params.toString()}`,
        {
          signal: controller.signal,
          cache: 'no-store',
        },
      )

      if (!response.ok) {
        throw new Error(
          `Search request failed: ${response.status}`,
        )
      }

      const data = await response.json()

      if (
        !mountedRef.current ||
        requestId !== searchRequestIdRef.current
      ) {
        return
      }

      const nextResults = Array.isArray(
        data.results,
      )
        ? data.results
        : []

      const nextPagination = data.pagination || {
        page: safePage,
        limit,
        total: nextResults.length,
        total_pages:
          nextResults.length > 0 ? 1 : 0,
        has_next: false,
        has_previous: safePage > 1,
        start:
          nextResults.length > 0 ? 1 : 0,
        end: nextResults.length,
      }

      setResults(nextResults)
      setPagination(nextPagination)
      setPage(
        nextPagination.page || safePage,
      )
      setHasSearched(true)
    } catch (error) {
      if (error?.name === 'AbortError') {
        return
      }

      console.error(
        'Search failed:',
        error,
      )

      if (
        requestId !== searchRequestIdRef.current
      ) {
        return
      }

      setResults([])
      setPagination({
        page: 1,
        limit,
        total: 0,
        total_pages: 0,
        has_next: false,
        has_previous: false,
        start: 0,
        end: 0,
      })
      setPage(1)
      setHasSearched(true)
      setSearchError(
        'Unable to connect to the search server.',
      )
    } finally {
      if (
        requestId === searchRequestIdRef.current
      ) {
        setIsSearching(false)
      }
    }
  }

  // ------------------------------------------------------------
  // REFRESH SEARCH AFTER INDEXING
  // ------------------------------------------------------------

  useEffect(() => {
    if (
      pendingRefreshPage === null ||
      indexingStatus.state !== 'READY' ||
      isSearching
    ) {
      return
    }

    const nextPage = pendingRefreshPage

    setPendingRefreshPage(null)

    if (query.trim()) {
      handleSearch(nextPage)
    }
  })

  // ------------------------------------------------------------
  // PAGINATION
  // ------------------------------------------------------------

  const handlePageChange = (nextPage) => {
    if (
      isSearching ||
      isIndexing ||
      isMutating
    ) {
      return
    }

    if (nextPage < 1) {
      return
    }

    if (
      pagination.total_pages > 0 &&
      nextPage > pagination.total_pages
    ) {
      return
    }

    handleSearch(nextPage)
  }

  const handleLimitChange = (event) => {
    const nextLimit = Number(
      event.target.value,
    )

    if (![10, 20, 50].includes(nextLimit)) {
      return
    }

    setLimit(nextLimit)
    setPage(1)

    if (
      hasSearched &&
      query.trim() &&
      !isIndexing &&
      !isMutating
    ) {
      searchAbortControllerRef.current?.abort()

      const controller = new AbortController()
      searchAbortControllerRef.current =
        controller

      const requestId =
        searchRequestIdRef.current + 1

      searchRequestIdRef.current =
        requestId

      setIsSearching(true)
      setSearchError('')

      const params = new URLSearchParams({
        q: query.trim(),
        page: '1',
        limit: String(nextLimit),
      })

      fetch(
        `${API_BASE_URL}/api/search?${params.toString()}`,
        {
          signal: controller.signal,
          cache: 'no-store',
        },
      )
        .then((response) => {
          if (!response.ok) {
            throw new Error(
              `Search request failed: ${response.status}`,
            )
          }

          return response.json()
        })
        .then((data) => {
          if (
            requestId !== searchRequestIdRef.current
          ) {
            return
          }

          const nextResults = Array.isArray(
            data.results,
          )
            ? data.results
            : []

          const nextPagination =
            data.pagination || {
              page: 1,
              limit: nextLimit,
              total: nextResults.length,
              total_pages:
                nextResults.length > 0 ? 1 : 0,
              has_next: false,
              has_previous: false,
              start:
                nextResults.length > 0 ? 1 : 0,
              end: nextResults.length,
            }

          setResults(nextResults)
          setPagination(nextPagination)
          setPage(
            nextPagination.page || 1,
          )
        })
        .catch((error) => {
          if (
            error?.name === 'AbortError'
          ) {
            return
          }

          console.error(
            'Page-size change search failed:',
            error,
          )

          if (
            requestId !== searchRequestIdRef.current
          ) {
            return
          }

          setResults([])
          setPagination({
            page: 1,
            limit: nextLimit,
            total: 0,
            total_pages: 0,
            has_next: false,
            has_previous: false,
            start: 0,
            end: 0,
          })
          setPage(1)
          setSearchError(
            'Unable to update the result list.',
          )
        })
        .finally(() => {
          if (
            requestId === searchRequestIdRef.current
          ) {
            setIsSearching(false)
          }
        })
    }
  }

  const pageNumbers = useMemo(
    () =>
      getPageNumbers(
        pagination.page,
        pagination.total_pages,
      ),
    [
      pagination.page,
      pagination.total_pages,
    ],
  )

  // ------------------------------------------------------------
  // FILE SELECTION
  // ------------------------------------------------------------

  const handleFileChange = (event) => {
    setSelectedFiles(event.target.files)
  }

  // ------------------------------------------------------------
  // BULK UPLOAD
  // ------------------------------------------------------------

  const handleUpload = async () => {
    if (
      isSearching ||
      isIndexing ||
      isMutating
    ) {
      return
    }

    if (!selectedFiles || selectedFiles.length === 0) {
      setUploadMessage(
        'Please select at least one file first.',
      )
      return
    }

    const formData = new FormData()

    for (
      let index = 0;
      index < selectedFiles.length;
      index += 1
    ) {
      formData.append(
        'file',
        selectedFiles[index],
      )
    }

    setIsMutating(true)

    try {
      setUploadMessage(
        `Uploading ${selectedFiles.length} file${selectedFiles.length === 1 ? '' : 's'
        }...`,
      )

      const response = await fetch(
        `${API_BASE_URL}/api/upload`,
        {
          method: 'POST',
          body: formData,
        },
      )

      const data = await response.json()

      if (!response.ok) {
        throw new Error(
          data.error || 'Upload failed.',
        )
      }

      const createdCount = Number(
        data.created_count || 0,
      )

      const replacedCount = Number(
        data.replaced_count || 0,
      )

      const rejectedCount = Number(
        data.rejected_count || 0,
      )

      const failedCount = Number(
        data.failed_count || 0,
      )

      const summaryParts = []

      if (createdCount > 0) {
        summaryParts.push(
          `${createdCount} added`,
        )
      }

      if (replacedCount > 0) {
        summaryParts.push(
          `${replacedCount} replaced`,
        )
      }

      if (rejectedCount > 0) {
        summaryParts.push(
          `${rejectedCount} rejected`,
        )
      }

      if (failedCount > 0) {
        summaryParts.push(
          `${failedCount} failed`,
        )
      }

      setUploadMessage(
        summaryParts.length > 0
          ? `Upload complete: ${summaryParts.join(', ')}.`
          : (
            data.message ||
            'Upload completed successfully.'
          ),
      )

      if (data.indexing) {
        setIndexingStatus(data.indexing)
      }

      setSelectedFiles(null)

      // Do not search while the index is being rebuilt.
      // Queue a refresh for when the backend reports READY.
      if (hasSearched && query.trim()) {
        setPendingRefreshPage(1)
      }
    } catch (error) {
      console.error('Upload failed:', error)

      setUploadMessage(
        error.message ||
        'Failed to connect to the server.',
      )
    } finally {
      setIsMutating(false)
    }
  }

  // ------------------------------------------------------------
  // DELETE DOCUMENT
  // ------------------------------------------------------------

  const deleteDocument = async (result) => {
    if (
      isSearching ||
      isIndexing ||
      isMutating
    ) {
      return
    }

    const filename = result?.title

    if (!filename || !result?.document_url) {
      return
    }

    const confirmed = window.confirm(
      `Delete "${filename}" from the search index and data folder?`,
    )

    if (!confirmed) {
      return
    }

    setDeletingDocument(filename)
    setSearchError('')
    setIsMutating(true)

    // A mutation invalidates any in-flight search response.
    searchAbortControllerRef.current?.abort()
    searchRequestIdRef.current += 1

    try {
      const response = await fetch(
        `${API_BASE_URL}${result.document_url}`,
        {
          method: 'DELETE',
        },
      )

      const data = await response.json()

      if (!response.ok) {
        throw new Error(
          data.error || 'Document deletion failed.',
        )
      }

      if (data.indexing) {
        setIndexingStatus(data.indexing)
      }

      // Queue the current page for refresh after indexing is READY.
      if (hasSearched && query.trim()) {
        setPendingRefreshPage(page)
      }
    } catch (error) {
      console.error('Delete failed:', error)

      setSearchError(
        error.message ||
        'Failed to delete the document.',
      )
    } finally {
      setDeletingDocument('')
      setIsMutating(false)
    }
  }

  // ------------------------------------------------------------
  // OPEN DOCUMENT
  // ------------------------------------------------------------

  const openDocument = (result) => {
    if (!result?.open_url) {
      return
    }

    window.open(
      `${API_BASE_URL}${result.open_url}`,
      '_blank',
      'noopener,noreferrer',
    )
  }

  // ------------------------------------------------------------
  // UI
  // ------------------------------------------------------------

  return (
    <div className="container">
      <div className="header">
        <div className="header-title">
          <h2>Domain-Specific Search</h2>
          <span className="header-subtitle">
            Local document retrieval engine
          </span>
        </div>

        <div className="status-badge">
          Nodes: 3 (Master + 2) | O(1) Indexing
        </div>
      </div>

      {(isIndexing || indexingStatus.state === 'ERROR') && (
        <div
          className={
            indexingStatus.state === 'ERROR'
              ? 'indexing-banner indexing-banner-error'
              : 'indexing-banner'
          }
          role="status"
          aria-live="polite"
        >
          <span className="indexing-indicator" />
          <span>
            {indexingStatus.state === 'ERROR'
              ? (
                indexingStatus.last_error ||
                'Search index rebuild failed.'
              )
              : isMutating
                ? 'Updating the search index... Actions are temporarily locked.'
                : 'Indexing documents... Search actions are temporarily locked.'}
          </span>
        </div>
      )}

      <div className="search-box">
        <input
          type="text"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value)
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              handleSearch(1)
            }
          }}
          placeholder="Search documents..."
          aria-label="Search documents"
        />

        <button
          type="button"
          onClick={() => handleSearch(1)}
          disabled={
            isSearching ||
            isIndexing ||
            isMutating
          }
        >
          {isSearching
            ? 'SEARCHING...'
            : 'EXECUTE QUERY'}
        </button>
      </div>

      <div className="search-hint">
        <span>Try:</span>
        <button
          type="button"
          className="search-example"
          onClick={() => {
            setQuery('network layer')
            handleSearch(1)
          }}
        >
          network layer
        </button>
        <button
          type="button"
          className="search-example"
          onClick={() => {
            setQuery('customer pdf')
            handleSearch(1)
          }}
        >
          customer pdf
        </button>
        <button
          type="button"
          className="search-example"
          onClick={() => {
            setQuery('django models')
            handleSearch(1)
          }}
        >
          django models
        </button>
      </div>

      <div className="upload-panel">
        <h3>Upload Documents (Bulk)</h3>

        <div className="upload-row">
          <input
            type="file"
            multiple
            accept=".pdf,.docx,.txt"
            onChange={handleFileChange}
            disabled={
              isSearching ||
              isIndexing ||
              isMutating
            }
          />

          <button
            type="button"
            className="upload-button"
            onClick={handleUpload}
            disabled={
              !selectedFiles ||
              isSearching ||
              isIndexing ||
              isMutating
            }
          >
            Upload Files
          </button>
        </div>

        {uploadMessage && (
          <p className="upload-message">
            {uploadMessage}
          </p>
        )}

        <p className="upload-help">
          Supported: PDF, DOCX, TXT · Re-uploading an existing
          filename replaces its indexed version.
        </p>
      </div>

      {isSearching && (
        <div className="metrics metrics-searching">
          &gt; Querying indexed documents...
        </div>
      )}

      {isMutating && (
        <div className="metrics metrics-searching">
          &gt; Updating the index. Search and document actions are temporarily locked...
        </div>
      )}

      {searchError && (
        <div className="search-error">
          {searchError}
        </div>
      )}

      {hasSearched && !isSearching && !searchError && (
        <>
          <div className="results-toolbar">
            <div className="metrics">
              &gt; Showing {pagination.start || 0}
              {' - '}
              {pagination.end || 0}
              {' of '}
              {pagination.total || 0}
              {' results | TF-IDF + Phrase Search'}
              {indexingStatus.state === 'READY' && (
                <> | Index: READY</>
              )}
            </div>

            {pagination.total > 0 && (
              <label className="page-size-control">
                Results:
                <select
                  value={limit}
                  onChange={handleLimitChange}
                  disabled={isSearching}
                  aria-label="Results per page"
                >
                  <option value={10}>10</option>
                  <option value={20}>20</option>
                  <option value={50}>50</option>
                </select>
              </label>
            )}
          </div>

          <div id="results">
            {results.map((result, index) => (
              <div
                className="result"
                key={`${result.document_url || result.path}-${index}`}
              >
                <button
                  type="button"
                  onClick={() => openDocument(result)}
                  disabled={!result.open_url}
                  className="result-title"
                  aria-label={`Open ${result.title}`}
                >
                  {result.title}
                </button>

                <p className="result-path">
                  {result.path}
                </p>

                <p className="result-snippet">
                  {renderHighlightedSnippet(
                    result.snippet,
                    result.highlights,
                  )}
                </p>

                <div className="tags">
                  <span className="score-tag">
                    Score: {result.score}
                  </span>

                  <span className="score-tag">
                    {result.tag}
                  </span>

                  {result.page && (
                    <span className="score-tag">
                      Page {result.page}
                    </span>
                  )}

                  {result.match_type && (
                    <span className="score-tag">
                      {result.match_type}
                    </span>
                  )}

                  {result.filetype_filter && (
                    <span className="score-tag">
                      Type:{' '}
                      {result.filetype_filter.toUpperCase()}
                    </span>
                  )}
                </div>

                <div className="result-actions">
                  {result.open_url && (
                    <button
                      type="button"
                      onClick={() => openDocument(result)}
                      className="open-document-link"
                    >
                      Open document
                      {result.page
                        ? ` at page ${result.page}`
                        : ''}
                      {' →'}
                    </button>
                  )}

                  <button
                    type="button"
                    onClick={() => deleteDocument(result)}
                    className="delete-document-link"
                    disabled={
                      deletingDocument === result.title ||
                      isSearching ||
                      isIndexing ||
                      isMutating
                    }
                  >
                    {deletingDocument === result.title
                      ? 'Deleting...'
                      : 'Delete'}
                  </button>
                </div>
              </div>
            ))}

            {results.length === 0 && (
              <div className="empty-results">
                <div className="empty-results-title">
                  No matching documents
                </div>
                <div className="empty-results-text">
                  Try a broader query or remove a file-type
                  filter.
                </div>
              </div>
            )}
          </div>

          {pagination.total_pages > 1 && (
            <nav
              className="pagination-controls"
              aria-label="Search results pages"
            >
              <button
                type="button"
                onClick={() => handlePageChange(1)}
                disabled={
                  isSearching ||
                  !pagination.has_previous
                }
                className="pagination-button pagination-first"
              >
                « First
              </button>

              <button
                type="button"
                onClick={() =>
                  handlePageChange(page - 1)
                }
                disabled={
                  isSearching ||
                  !pagination.has_previous
                }
                className="pagination-button"
              >
                ← Prev
              </button>

              <div className="pagination-pages">
                {pageNumbers.map((pageNumber) => {
                  if (
                    typeof pageNumber !== 'number'
                  ) {
                    return (
                      <span
                        key={pageNumber}
                        className="pagination-ellipsis"
                      >
                        …
                      </span>
                    )
                  }

                  const isCurrent =
                    pageNumber === pagination.page

                  return (
                    <button
                      key={pageNumber}
                      type="button"
                      onClick={() =>
                        handlePageChange(
                          pageNumber,
                        )
                      }
                      disabled={
                        isSearching ||
                        isCurrent
                      }
                      className={
                        isCurrent
                          ? 'pagination-page pagination-page-active'
                          : 'pagination-page'
                      }
                      aria-current={
                        isCurrent
                          ? 'page'
                          : undefined
                      }
                    >
                      {pageNumber}
                    </button>
                  )
                })}
              </div>

              <button
                type="button"
                onClick={() =>
                  handlePageChange(page + 1)
                }
                disabled={
                  isSearching ||
                  !pagination.has_next
                }
                className="pagination-button"
              >
                Next →
              </button>

              <button
                type="button"
                onClick={() =>
                  handlePageChange(
                    pagination.total_pages,
                  )
                }
                disabled={
                  isSearching ||
                  !pagination.has_next
                }
                className="pagination-button pagination-last"
              >
                Last »
              </button>
            </nav>
          )}
        </>
      )}
    </div>
  )
}

export default App
