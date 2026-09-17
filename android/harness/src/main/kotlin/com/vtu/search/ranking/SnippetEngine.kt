package com.vtu.search.ranking

import com.vtu.search.backend.Highlight
import com.vtu.search.parity.PythonText
import java.util.Locale
import java.util.regex.Pattern

/**
 * Snippet, highlight and page selection engine.
 * Exact Kotlin port of `search_engine/snippet.py` and `search_engine/extract.py`.
 */
object SnippetEngine {

    data class PageData(val page: Int, val text: String)

    data class SnippetResult(
        val snippet: String,
        val page: Int?,
        val highlights: List<Highlight>,
    )

    /**
     * Count exact adjacent occurrences of a normalized phrase.
     */
    fun countPhraseOccurrences(text: String, phrase: String): Int {
        if (text.isEmpty() || phrase.isEmpty()) return 0

        val normalizedText = PythonText.splitPythonWhitespace(text.lowercase(Locale.ROOT)).joinToString(" ")
        val normalizedPhrase = PythonText.splitPythonWhitespace(phrase.lowercase(Locale.ROOT)).joinToString(" ")

        if (normalizedPhrase.isEmpty()) return 0

        var count = 0
        var start = 0
        while (true) {
            val position = normalizedText.indexOf(normalizedPhrase, start)
            if (position == -1) break
            count++
            start = position + normalizedPhrase.length
        }
        return count
    }

    /**
     * Build the final snippet and highlight ranges around a chosen match.
     */
    fun buildSnippetResult(
        text: String,
        pageNumber: Int?,
        position: Int,
        matchLength: Int,
        window: Int,
        highlightQuery: String,
    ): SnippetResult {
        val start = maxOf(0, position - window)
        val end = minOf(text.length, position + matchLength + window)
        var snippet = text.substring(start, end).trim()

        if (start > 0) {
            val firstSpace = snippet.indexOf(' ')
            if (firstSpace != -1) {
                snippet = snippet.substring(firstSpace + 1)
            }
            snippet = "... $snippet"
        }

        if (end < text.length) {
            val lastSpace = snippet.lastIndexOf(' ')
            if (lastSpace != -1) {
                snippet = snippet.substring(0, lastSpace)
            }
            snippet = "$snippet ..."
        }

        val highlights = ArrayList<Highlight>()
        val lowerSnippet = snippet.lowercase(Locale.ROOT)
        val query = PythonText.splitPythonWhitespace(highlightQuery.lowercase(Locale.ROOT)).joinToString(" ")

        var searchPosition = 0
        if (query.isNotEmpty()) {
            while (true) {
                val found = lowerSnippet.indexOf(query, searchPosition)
                if (found == -1) break
                highlights.add(Highlight(found, found + query.length))
                searchPosition = found + query.length
            }
        }

        if (highlights.isEmpty() && query.isNotEmpty()) {
            val pattern = Pattern.compile(Pattern.quote(query), Pattern.CASE_INSENSITIVE)
            val matcher = pattern.matcher(lowerSnippet)
            while (matcher.find()) {
                highlights.add(Highlight(matcher.start(), matcher.end()))
            }
        }

        highlights.sortBy { it.start }

        val merged = ArrayList<Highlight>()
        for (item in highlights) {
            if (merged.isEmpty()) {
                merged.add(item)
                continue
            }
            val previous = merged[merged.size - 1]
            if (item.start <= previous.end) {
                merged[merged.size - 1] = Highlight(previous.start, maxOf(previous.end, item.end))
            } else {
                merged.add(item)
            }
        }

        return SnippetResult(snippet, pageNumber, merged)
    }

    /**
     * Return the best matching page/snippet.
     */
    fun getSnippetAndPage(
        pages: List<PageData>,
        queryWords: List<String>,
        window: Int = 140,
    ): SnippetResult {
        if (pages.isEmpty()) {
            return SnippetResult("No readable text found.", null, emptyList())
        }

        val cleanedWords = LinkedHashSet<String>()
        for (word in queryWords) {
            val trimmed = word.lowercase(Locale.ROOT).trim()
            if (trimmed.isNotEmpty()) {
                cleanedWords.add(trimmed)
            }
        }

        if (cleanedWords.isEmpty()) {
            return SnippetResult("No matching text found.", null, emptyList())
        }

        val phrase = cleanedWords.joinToString(" ")

        // 1. EXACT PHRASE
        if (cleanedWords.size >= 2) {
            for (pageData in pages) {
                val pageNumber = pageData.page
                val originalText = PythonText.splitPythonWhitespace(pageData.text).joinToString(" ")
                val lowerText = originalText.lowercase(Locale.ROOT)
                val phrasePosition = lowerText.indexOf(phrase)
                if (phrasePosition != -1) {
                    return buildSnippetResult(
                        originalText,
                        pageNumber,
                        phrasePosition,
                        phrase.length,
                        window,
                        phrase,
                    )
                }
            }
        }

        // 2. EXACT TOKEN
        data class ExactCandidate(
            val page: Int,
            val position: Int,
            val length: Int,
            val text: String,
            val word: String,
        )

        val exactCandidates = ArrayList<ExactCandidate>()
        for (pageData in pages) {
            val pageNumber = pageData.page
            val originalText = PythonText.splitPythonWhitespace(pageData.text).joinToString(" ")
            val lowerText = originalText.lowercase(Locale.ROOT)

            for (word in cleanedWords) {
                val pattern = Pattern.compile("(?<![a-z0-9])" + Pattern.quote(word) + "(?![a-z0-9])")
                val matcher = pattern.matcher(lowerText)
                if (matcher.find()) {
                    exactCandidates.add(
                        ExactCandidate(
                            page = pageNumber,
                            position = matcher.start(),
                            length = word.length,
                            text = originalText,
                            word = word,
                        )
                    )
                }
            }
        }

        if (exactCandidates.isNotEmpty()) {
            val best = exactCandidates[0]
            return buildSnippetResult(
                best.text,
                best.page,
                best.position,
                best.length,
                window,
                best.word,
            )
        }

        // 3. PREFIX MATCH
        data class PrefixCandidate(
            val page: Int,
            val position: Int,
            val length: Int,
            val text: String,
            val word: String,
            val score: Double,
        )

        val prefixCandidates = ArrayList<PrefixCandidate>()
        for (pageData in pages) {
            val pageNumber = pageData.page
            val originalText = PythonText.splitPythonWhitespace(pageData.text).joinToString(" ")
            val lowerText = originalText.lowercase(Locale.ROOT)

            for (word in cleanedWords) {
                if (word.length < 3) continue

                val pattern = Pattern.compile("(?<![a-z0-9])" + Pattern.quote(word) + "[a-z0-9]+")
                val matcher = pattern.matcher(lowerText)
                if (matcher.find()) {
                    val matchedText = matcher.group(0)
                    val prefixSimilarity = word.length.toDouble() / maxOf(matchedText.length, 1).toDouble()
                    prefixCandidates.add(
                        PrefixCandidate(
                            page = pageNumber,
                            position = matcher.start(),
                            length = matchedText.length,
                            text = originalText,
                            word = word,
                            score = 50000.0 + prefixSimilarity,
                        )
                    )
                }
            }
        }

        if (prefixCandidates.isNotEmpty()) {
            val best = prefixCandidates.maxByOrNull { it.score }!!
            return buildSnippetResult(
                best.text,
                best.page,
                best.position,
                best.length,
                window,
                best.word,
            )
        }

        // 4. PROXIMITY FALLBACK
        data class FallbackMatch(
            val page: Int,
            val position: Int,
            val length: Int,
            val text: String,
            val score: Int,
        )

        var bestMatch: FallbackMatch? = null
        var bestScore = -1

        for (pageData in pages) {
            val pageNumber = pageData.page
            val originalText = PythonText.splitPythonWhitespace(pageData.text).joinToString(" ")
            val lowerText = originalText.lowercase(Locale.ROOT)
            if (lowerText.isEmpty()) continue

            val candidates = ArrayList<Pair<Int, String>>()
            for (word in cleanedWords) {
                var searchPosition = 0
                while (true) {
                    val position = lowerText.indexOf(word, searchPosition)
                    if (position == -1) break
                    candidates.add(Pair(position, word))
                    searchPosition = position + word.length
                }
            }

            for ((position, matchedWord) in candidates) {
                val localStart = maxOf(0, position - window)
                val localEnd = minOf(originalText.length, position + window)
                val localText = lowerText.substring(localStart, localEnd)

                var nearbyTerms = 0
                for (word in cleanedWords) {
                    if (localText.contains(word)) {
                        nearbyTerms++
                    }
                }

                val candidateScore = nearbyTerms * 1000 + matchedWord.length
                if (candidateScore > bestScore) {
                    bestScore = candidateScore
                    bestMatch = FallbackMatch(
                        page = pageNumber,
                        position = position,
                        length = matchedWord.length,
                        text = originalText,
                        score = candidateScore,
                    )
                }
            }
        }

        if (bestMatch == null) {
            return SnippetResult("No matching text found.", null, emptyList())
        }

        return buildSnippetResult(
            bestMatch.text,
            bestMatch.page,
            bestMatch.position,
            bestMatch.length,
            window,
            phrase,
        )
    }
}
