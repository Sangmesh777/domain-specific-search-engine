package com.vtu.search.ranking

import com.vtu.search.backend.Highlight
import com.vtu.search.backend.Pagination
import com.vtu.search.backend.SearchHit
import com.vtu.search.backend.SearchResponse
import com.vtu.search.parity.ContractHarness
import com.vtu.search.parity.PythonRound
import com.vtu.search.parity.PythonText
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.util.Locale
import kotlin.math.ln

/**
 * Exact Kotlin port of the ranking engine in `search_engine/engine.py`.
 */
class RankingEngine(
    val invertedIndex: Map<String, Map<String, Int>>,
    val documentMetadata: LinkedHashMap<String, DocumentMetadata>,
    val filenameIndex: Map<String, List<String>>,
    val pageTextIndex: Map<String, List<SnippetEngine.PageData>>,
) : ContractHarness.SearchOracle {

    data class DocumentMetadata(
        val title: String,
        val path: String?,
        val totalWords: Int,
        val pageCount: Int,
    )

    private data class IntermediateScore(
        var filenameScore: Double = 0.0,
        var contentScore: Double = 0.0,
        var phraseScore: Double = 0.0,
        var phraseOccurrences: Int = 0,
        val filenameMatches: ArrayList<String> = ArrayList(),
        val contentMatches: ArrayList<String> = ArrayList(),
        var phraseMatch: Boolean = false,
        var exactContentMatch: Boolean = false,
        var bestPrefixSimilarity: Double = 0.0,
        var bestNumericSimilarity: Double = 0.0,
    )

    private data class ScoredDocument(
        val filename: String,
        val filenameScore: Double,
        val contentScore: Double,
        val phraseScore: Double,
        val phraseOccurrences: Int,
        val finalScore: Double,
        val filenameMatches: List<String>,
        val contentMatches: List<String>,
        val phraseMatch: Boolean,
        val exactContentMatch: Boolean,
        val prefixSimilarity: Double,
        val numericSimilarity: Double,
        val lexicalMatchRelevance: Double,
    )

    override fun search(query: String, page: Int?, limit: Int?): JsonElement {
        return searchToJson(query, page?.toString(), limit?.toString())
    }

    /**
     * Run search and return the serialised JSON element matching the server API.
     */
    fun searchToJson(rawQueryText: String, pageText: String?, limitText: String?): JsonElement {
        val rawQuery = rawQueryText.trim()

        if (rawQuery.isEmpty()) {
            return JsonObject(
                mapOf(
                    "results" to JsonArray(emptyList()),
                    "pagination" to JsonObject(
                        mapOf(
                            "page" to JsonPrimitive(1),
                            "limit" to JsonPrimitive(10),
                            "total" to JsonPrimitive(0),
                            "total_pages" to JsonPrimitive(0),
                            "has_next" to JsonPrimitive(false),
                            "has_previous" to JsonPrimitive(false),
                        ),
                    ),
                ),
            )
        }

        val requestedPage = pageText?.toIntOrNull() ?: 1
        val requestedLimit = limitText?.toIntOrNull() ?: 10
        val requestedResultPage = maxOf(1, requestedPage)
        val limit = minOf(50, maxOf(1, requestedLimit))

        val (keywordQuery, filetypeFilter) = PythonText.parseFiletypeFilter(rawQuery)
        val normalizedQueryText = PythonText.normalizeSearchQuery(keywordQuery)
        val query = normalizedQueryText.lowercase(Locale.ROOT)

        var quotedPhrase: String? = null
        if (query.length >= 2 && query.startsWith('"') && query.endsWith('"')) {
            quotedPhrase = query.substring(1, query.length - 1).trim()
        }

        val searchWords = PythonText.tokenizeFilename(query)

        if (searchWords.isEmpty() && filetypeFilter == null) {
            return buildPaginatedResponseJson(emptyList(), requestedResultPage, limit)
        }

        val contentQueryWords = searchWords.filter { it.length > 1 }

        fun urlEncode(name: String): String =
            URLEncoder.encode(name, StandardCharsets.UTF_8.name())
                .replace("+", "%20")
                .replace("%2A", "*")
                .replace("%7E", "~")

        fun fileExtension(name: String): String {
            val dot = name.lastIndexOf('.')
            return if (dot != -1) name.substring(dot + 1).lowercase(Locale.ROOT) else ""
        }

        // FILE-TYPE-ONLY QUERY
        if (filetypeFilter != null && searchWords.isEmpty()) {
            val filteredResults = ArrayList<JsonObject>()

            for ((filename, metadata) in documentMetadata) {
                val extension = fileExtension(filename)
                if (extension != filetypeFilter) continue

                val docUrl = "/api/documents/" + urlEncode(filename)

                filteredResults.add(
                    JsonObject(
                        mapOf(
                            "title" to JsonPrimitive(metadata.title),
                            "document_url" to JsonPrimitive(docUrl),
                            "page_url" to JsonPrimitive(docUrl),
                            "open_url" to JsonPrimitive(docUrl),
                            "snippet" to JsonPrimitive("Filtered by file type: ${filetypeFilter.uppercase(Locale.ROOT)}"),
                            "page" to JsonNull,
                            "highlights" to JsonArray(emptyList()),
                            "filename_score" to JsonPrimitive(0.0),
                            "content_score" to JsonPrimitive(0.0),
                            "phrase_score" to JsonPrimitive(0.0),
                            "phrase_occurrences" to JsonPrimitive(0),
                            "score" to JsonPrimitive(1.0),
                            "relevance_score" to JsonPrimitive(1.0),
                            "match_type" to JsonPrimitive("File Type: ${filetypeFilter.uppercase(Locale.ROOT)}"),
                            "filetype_filter" to JsonPrimitive(filetypeFilter),
                            "tag" to JsonPrimitive("Filtered Result"),
                        ),
                    ),
                )
            }

            filteredResults.sortBy { (it["title"] as JsonPrimitive).content.lowercase(Locale.ROOT) }
            return buildPaginatedResponseJson(filteredResults, requestedResultPage, limit)
        }

        val normalizedQuery = searchWords.joinToString(" ")
        val phraseWords = if (quotedPhrase != null) {
            PythonText.tokenizeFilename(quotedPhrase)
        } else {
            ArrayList(searchWords)
        }
        val normalizedPhrase = phraseWords.joinToString(" ")
        val phraseQuery = phraseWords.size >= 2 && searchWords.isNotEmpty()

        val totalDocuments = if (filetypeFilter != null) {
            documentMetadata.keys.count { fileExtension(it) == filetypeFilter }
        } else {
            documentMetadata.size
        }

        if (totalDocuments == 0) {
            return JsonArray(emptyList())
        }

        val documentScores = LinkedHashMap<String, IntermediateScore>()
        for (filename in documentMetadata.keys) {
            documentScores[filename] = IntermediateScore()
        }

        fun filetypeAllowed(filename: String): Boolean {
            if (filetypeFilter == null) return true
            return fileExtension(filename) == filetypeFilter
        }

        // FILENAME SEARCH
        for ((filename, filenameWords) in filenameIndex) {
            if (!filetypeAllowed(filename)) continue

            val normalizedFilename = filenameWords.joinToString(" ")
            val scores = documentScores[filename] ?: continue

            if (normalizedQuery == normalizedFilename) {
                scores.filenameScore += 100.0
            } else if (searchWords.size >= 2 && normalizedFilename.contains(normalizedQuery)) {
                scores.filenameScore += 50.0
            }

            if (quotedPhrase != null && normalizedPhrase.isNotEmpty() && normalizedFilename.contains(normalizedPhrase)) {
                scores.phraseScore += 30.0
                scores.phraseMatch = true
            }

            for (word in searchWords) {
                if (!filenameWords.contains(word)) continue

                if (PythonText.isPythonDigit(word)) {
                    val nonNumeric = searchWords.filter { !PythonText.isPythonDigit(it) }
                    val hasRelated = nonNumeric.any { filenameWords.contains(it) }
                    if (nonNumeric.isNotEmpty() && !hasRelated) {
                        continue
                    }
                }

                scores.filenameScore += 20.0
                if (!scores.filenameMatches.contains(word)) {
                    scores.filenameMatches.add(word)
                }
            }
        }

        // CONTENT TF-IDF
        val indexedTerms = invertedIndex.keys.toList()

        for (word in contentQueryWords) {
            val matchingTerms = ArrayList<Pair<String, Double>>()

            if (invertedIndex.containsKey(word)) {
                matchingTerms.add(Pair(word, 1.0))
            }

            if (word.length >= 3) {
                for (term in indexedTerms) {
                    if (term == word || term.isEmpty() || !term.startsWith(word)) continue
                    val prefixSim = word.length.toDouble() / term.length.toDouble()
                    val clamped = minOf(0.90, maxOf(0.25, prefixSim))
                    matchingTerms.add(Pair(term, clamped))
                }
            }

            if (PythonText.isPythonDigit(word) && word.length >= 2) {
                for (term in indexedTerms) {
                    if (term == word || !term.contains(word) || term.startsWith(word)) continue
                    val subSim = word.length.toDouble() / term.length.toDouble()
                    val clamped = minOf(0.65, maxOf(0.20, subSim * 0.70))
                    matchingTerms.add(Pair(term, clamped))
                }
            }

            val bestTermWeights = HashMap<String, Double>()
            for ((term, matchWeight) in matchingTerms) {
                val current = bestTermWeights[term] ?: 0.0
                if (matchWeight > current) {
                    bestTermWeights[term] = matchWeight
                }
            }

            for ((term, matchWeight) in bestTermWeights) {
                val postings = invertedIndex[term] ?: continue
                val docsContainingWord = postings.size
                if (docsContainingWord == 0) continue

                val idf = ln((totalDocuments + 1).toDouble() / (docsContainingWord + 1).toDouble()) + 1.0

                for ((filename, termCount) in postings) {
                    if (!filetypeAllowed(filename)) continue
                    val metadata = documentMetadata[filename] ?: continue
                    val totalWords = metadata.totalWords
                    if (totalWords == 0) continue

                    val tf = termCount.toDouble() / totalWords.toDouble()
                    val tfidf = tf * idf * matchWeight

                    val docScore = documentScores[filename] ?: continue
                    docScore.contentScore += tfidf

                    if (term == word) {
                        docScore.exactContentMatch = true
                    } else if (PythonText.isPythonDigit(word) && term.contains(word)) {
                        docScore.bestNumericSimilarity = maxOf(docScore.bestNumericSimilarity, matchWeight)
                    } else if (term.startsWith(word)) {
                        docScore.bestPrefixSimilarity = maxOf(docScore.bestPrefixSimilarity, matchWeight)
                    }

                    if (!docScore.contentMatches.contains(word)) {
                        docScore.contentMatches.add(word)
                    }
                }
            }
        }

        // PHRASE SEARCH
        if (phraseQuery) {
            for (filename in documentMetadata.keys) {
                if (!filetypeAllowed(filename)) continue

                val pages = pageTextIndex[filename] ?: emptyList()
                val normalizedPageText = pages.joinToString(" ") { it.text }.lowercase(Locale.ROOT)

                val phraseOccurrences = SnippetEngine.countPhraseOccurrences(normalizedPageText, normalizedPhrase)

                if (phraseOccurrences > 0) {
                    val docScore = documentScores[filename]!!
                    docScore.phraseOccurrences = phraseOccurrences

                    val contentPhraseScore = if (phraseOccurrences <= 1) {
                        50.0
                    } else {
                        val frequencyRatio = minOf(ln(phraseOccurrences.toDouble()) / ln(25.0), 1.0)
                        50.0 + (50.0 * frequencyRatio)
                    }

                    val capped = minOf(contentPhraseScore, 100.0)
                    docScore.phraseScore = maxOf(docScore.phraseScore, capped)
                    docScore.phraseMatch = true
                }

                val filenameWords = filenameIndex[filename] ?: emptyList()
                val normalizedFilename = filenameWords.joinToString(" ")
                if (normalizedPhrase.isNotEmpty() && normalizedFilename.contains(normalizedPhrase)) {
                    val docScore = documentScores[filename]!!
                    docScore.phraseScore += 100.0
                    docScore.phraseMatch = true
                }
            }
        }

        // Quoted phrase filter
        if (quotedPhrase != null) {
            val phraseDocuments = documentScores.filter { it.value.phraseMatch }.keys
            if (phraseDocuments.isNotEmpty()) {
                val keysToRemove = documentScores.keys.filter { it !in phraseDocuments }
                for (k in keysToRemove) {
                    documentScores.remove(k)
                }
            } else {
                return JsonArray(emptyList())
            }
        }

        var maxContentScore = documentScores.values.maxOfOrNull { it.contentScore } ?: 0.0
        val queryHasFilenameSignal = documentScores.values.any { it.filenameScore > 0 }

        // FINAL RANKING
        if (filetypeFilter != null) {
            val keysToRemove = documentScores.keys.filter { !filetypeAllowed(it) }
            for (k in keysToRemove) {
                documentScores.remove(k)
            }
            if (documentScores.isEmpty()) {
                return JsonArray(emptyList())
            }
            maxContentScore = documentScores.values.maxOfOrNull { it.contentScore } ?: 0.0
        }

        val rankedDocuments = ArrayList<ScoredDocument>()

        for ((filename, scores) in documentScores) {
            val lexicalMatchRelevance = when {
                scores.exactContentMatch -> 1.00
                scores.bestPrefixSimilarity > 0 -> 0.70 + (0.30 * scores.bestPrefixSimilarity)
                scores.bestNumericSimilarity > 0 -> 0.30 + (0.40 * scores.bestNumericSimilarity)
                else -> 0.0
            }

            val filenameWords = filenameIndex[filename] ?: emptyList()
            val normalizedFilename = filenameWords.joinToString(" ")
            val matchedWordCount = searchWords.count { filenameWords.contains(it) }

            val queryWordCoverage = if (searchWords.isNotEmpty()) {
                matchedWordCount.toDouble() / searchWords.size.toDouble()
            } else {
                0.0
            }

            val filenameRelevance = when {
                normalizedQuery == normalizedFilename -> 1.0
                searchWords.size >= 2 && normalizedFilename.contains(normalizedQuery) -> 0.95
                queryWordCoverage > 0 -> 0.75 * queryWordCoverage
                else -> 0.0
            }

            val contentRelevance = if (maxContentScore > 0) {
                scores.contentScore / maxContentScore
            } else {
                0.0
            }

            val phraseRelevance = minOf(scores.phraseScore / 100.0, 1.0)

            val finalScore: Double
            if (filetypeFilter != null && searchWords.isEmpty()) {
                finalScore = 0.75
            } else {
                val filenameWeight: Double
                val contentWeight: Double
                val phraseWeight: Double
                val lexicalWeight: Double

                if (quotedPhrase != null && queryHasFilenameSignal) {
                    filenameWeight = 0.80
                    contentWeight = 0.05
                    phraseWeight = 0.15
                    lexicalWeight = 0.0
                } else if (quotedPhrase != null) {
                    filenameWeight = 0.05
                    contentWeight = 0.15
                    phraseWeight = 0.80
                    lexicalWeight = 0.0
                } else if (queryHasFilenameSignal) {
                    filenameWeight = 0.75
                    contentWeight = 0.15
                    phraseWeight = 0.10
                    lexicalWeight = 0.0
                } else {
                    filenameWeight = 0.05
                    contentWeight = 0.60
                    phraseWeight = 0.20
                    lexicalWeight = 0.15
                }

                finalScore = (filenameRelevance * filenameWeight) +
                    (contentRelevance * contentWeight) +
                    (phraseRelevance * phraseWeight) +
                    (lexicalMatchRelevance * lexicalWeight)
            }

            if (scores.filenameScore == 0.0 && scores.contentScore == 0.0 && scores.phraseScore == 0.0) {
                continue
            }

            if (finalScore <= 0) {
                continue
            }

            rankedDocuments.add(
                ScoredDocument(
                    filename = filename,
                    filenameScore = scores.filenameScore,
                    contentScore = scores.contentScore,
                    phraseScore = scores.phraseScore,
                    phraseOccurrences = scores.phraseOccurrences,
                    finalScore = finalScore,
                    filenameMatches = scores.filenameMatches,
                    contentMatches = scores.contentMatches,
                    phraseMatch = scores.phraseMatch,
                    exactContentMatch = scores.exactContentMatch,
                    prefixSimilarity = scores.bestPrefixSimilarity,
                    numericSimilarity = scores.bestNumericSimilarity,
                    lexicalMatchRelevance = lexicalMatchRelevance,
                ),
            )
        }

        if (filetypeFilter != null && searchWords.isEmpty()) {
            rankedDocuments.sortBy { it.filename.lowercase(Locale.ROOT) }
        } else {
            // Stable sort by finalScore descending (preserves deterministic insertion order)
            rankedDocuments.sortWith { a, b -> b.finalScore.compareTo(a.finalScore) }
        }

        val results = ArrayList<JsonObject>()

        for (item in rankedDocuments) {
            val filename = item.filename
            val metadata = documentMetadata[filename]!!

            val matchType = when {
                filetypeFilter != null && searchWords.isEmpty() -> "File Type: ${filetypeFilter.uppercase(Locale.ROOT)}"
                item.filenameMatches.isNotEmpty() && item.phraseMatch -> "Filename + Phrase"
                item.phraseMatch -> "Phrase Match"
                item.filenameMatches.isNotEmpty() && item.contentMatches.isNotEmpty() -> "Filename + Content"
                item.filenameMatches.isNotEmpty() -> "Filename Match"
                else -> "Content Match"
            }

            val snippet: String
            val documentPage: Int?
            val highlights: List<Highlight>

            if (filetypeFilter != null && searchWords.isEmpty()) {
                snippet = "Filtered by file type: ${filetypeFilter.uppercase(Locale.ROOT)}"
                documentPage = null
                highlights = emptyList()
            } else if (item.contentMatches.isNotEmpty()) {
                val snippetWords = if (item.phraseMatch) phraseWords else item.contentMatches
                val snippetInfo = SnippetEngine.getSnippetAndPage(
                    pageTextIndex[filename] ?: emptyList(),
                    snippetWords,
                )
                snippet = snippetInfo.snippet
                documentPage = snippetInfo.page
                highlights = snippetInfo.highlights
            } else {
                snippet = "Filename matched: " + item.filenameMatches.joinToString(", ")
                documentPage = null
                highlights = emptyList()
            }

            val docUrl = "/api/documents/" + urlEncode(filename)
            val pageUrl = if (documentPage != null) "$docUrl#page=$documentPage" else docUrl

            val jsonHighlights = JsonArray(
                highlights.map {
                    JsonObject(
                        mapOf(
                            "start" to JsonPrimitive(it.start),
                            "end" to JsonPrimitive(it.end),
                        ),
                    )
                },
            )

            results.add(
                JsonObject(
                    mapOf(
                        "title" to JsonPrimitive(metadata.title),
                        "document_url" to JsonPrimitive(docUrl),
                        "page_url" to JsonPrimitive(pageUrl),
                        "open_url" to JsonPrimitive(pageUrl),
                        "snippet" to JsonPrimitive(snippet),
                        "page" to (documentPage?.let { JsonPrimitive(it) } ?: JsonNull),
                        "highlights" to jsonHighlights,
                        "phrase_occurrences" to JsonPrimitive(item.phraseOccurrences),
                        "filename_score" to JsonPrimitive(PythonRound.round(item.filenameScore, 4)),
                        "content_score" to JsonPrimitive(PythonRound.round(item.contentScore, 4)),
                        "exact_content_match" to JsonPrimitive(item.exactContentMatch),
                        "prefix_similarity" to JsonPrimitive(PythonRound.round(item.prefixSimilarity, 4)),
                        "numeric_similarity" to JsonPrimitive(PythonRound.round(item.numericSimilarity, 4)),
                        "lexical_match_relevance" to JsonPrimitive(PythonRound.round(item.lexicalMatchRelevance, 4)),
                        "phrase_score" to JsonPrimitive(PythonRound.round(item.phraseScore, 4)),
                        "score" to JsonPrimitive(PythonRound.round(item.finalScore, 4)),
                        "match_type" to JsonPrimitive(matchType),
                        "filetype_filter" to (filetypeFilter?.let { JsonPrimitive(it) } ?: JsonNull),
                        "tag" to JsonPrimitive("Ranked Result"),
                    ),
                ),
            )
        }

        return buildPaginatedResponseJson(results, requestedResultPage, limit)
    }

    private fun buildPaginatedResponseJson(
        results: List<JsonObject>,
        page: Int,
        limit: Int,
    ): JsonObject {
        val total = results.size
        val totalPages = if (total > 0) (total + limit - 1) / limit else 0
        val clampedPage = if (totalPages > 0) maxOf(1, minOf(page, totalPages)) else maxOf(1, page)
        val startIndex = if (totalPages > 0) (clampedPage - 1) * limit else 0
        val endIndex = minOf(total, startIndex + limit)
        val pageResults = if (startIndex < total) results.subList(startIndex, endIndex) else emptyList()

        val paginationMap = mutableMapOf<String, JsonElement>(
            "page" to JsonPrimitive(clampedPage),
            "limit" to JsonPrimitive(limit),
            "total" to JsonPrimitive(total),
            "total_pages" to JsonPrimitive(totalPages),
            "has_next" to JsonPrimitive(clampedPage < totalPages),
            "has_previous" to JsonPrimitive(clampedPage > 1 && totalPages > 0),
        )

        if (pageResults.isNotEmpty()) {
            paginationMap["start"] = JsonPrimitive(startIndex + 1)
            paginationMap["end"] = JsonPrimitive(startIndex + pageResults.size)
        } else {
            paginationMap["start"] = JsonPrimitive(0)
            paginationMap["end"] = JsonPrimitive(0)
        }

        return JsonObject(
            mapOf(
                "results" to JsonArray(pageResults),
                "pagination" to JsonObject(paginationMap),
            ),
        )
    }

    companion object {
        /**
         * Reconstruct a [RankingEngine] from the sidecar JSON string.
         * Documents are sorted by source_name to preserve deterministic tie-breaking.
         */
        fun fromSidecarJson(sidecarJsonText: String): RankingEngine {
            val json = Json { ignoreUnknownKeys = true }
            val root = json.parseToJsonElement(sidecarJsonText) as JsonObject
            val docsArray = root["documents"] as JsonArray

            // Sort documents by source_name
            val sortedDocs = docsArray.map { it as JsonObject }
                .sortedBy { (it["source_name"] as JsonPrimitive).content }

            val invertedIndex = HashMap<String, MutableMap<String, Int>>()
            val metadataMap = LinkedHashMap<String, DocumentMetadata>()
            val filenameIndex = HashMap<String, List<String>>()
            val pageTextIndex = HashMap<String, List<SnippetEngine.PageData>>()

            for (doc in sortedDocs) {
                val name = (doc["name"] as JsonPrimitive).content
                val title = (doc["title"] as JsonPrimitive).content
                val totalWords = (doc["total_words"] as JsonPrimitive).content.toInt()
                val pageCount = (doc["page_count"] as JsonPrimitive).content.toInt()

                metadataMap[name] = DocumentMetadata(
                    title = title,
                    path = null,
                    totalWords = totalWords,
                    pageCount = pageCount,
                )

                val fnTerms = (doc["filename_terms"] as JsonArray).map { (it as JsonPrimitive).content }
                filenameIndex[name] = fnTerms

                val pages = (doc["pages"] as JsonArray).map { pageEl ->
                    val pageObj = pageEl as JsonObject
                    SnippetEngine.PageData(
                        page = (pageObj["page"] as JsonPrimitive).content.toInt(),
                        text = (pageObj["text"] as JsonPrimitive).content,
                    )
                }
                pageTextIndex[name] = pages

                val contentTerms = doc["content_terms"] as JsonObject
                for ((term, freqEl) in contentTerms) {
                    val freq = (freqEl as JsonPrimitive).content.toInt()
                    invertedIndex.getOrPut(term) { HashMap() }[name] = freq
                }
            }

            return RankingEngine(
                invertedIndex = invertedIndex,
                documentMetadata = metadataMap,
                filenameIndex = filenameIndex,
                pageTextIndex = pageTextIndex,
            )
        }
    }
}
