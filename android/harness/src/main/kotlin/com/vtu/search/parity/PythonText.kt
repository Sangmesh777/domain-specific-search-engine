package com.vtu.search.parity

import java.util.Locale

/**
 * Exact port of the Python tokenizers in `app.py`.
 *
 * The Python implementation is:
 *
 * ```python
 * def tokenize(text):
 *     text = text.lower()
 *     cleaned_text = "".join(
 *         character
 *         if character.isalnum() or character.isspace()
 *         else " "
 *         for character in text
 *     )
 *     return [word for word in cleaned_text.split() if len(word) > 1]
 * ```
 *
 * and `tokenize_filename` is identical except that it keeps every
 * non-empty token.
 *
 * Three things make the obvious Kotlin translation wrong. Each is
 * covered by `tests/golden/search_engine_vectors.json`:
 *
 *  1. **Lowercasing.** `str.lower()` is applied to the whole string and
 *     may change its length. `"İstanbul".lower()` is `"i" + U+0307 +
 *     "stanbul"`. `Char.toLowerCase()` cannot express that, so this
 *     port uses [String.lowercase] with [Locale.ROOT].
 *
 *  2. **`isalnum`.** Python accepts category `No` and `Nl` numerics
 *     (U+00BD, U+00B2, U+216B) which `Character.isLetterOrDigit`
 *     rejects.
 *
 *  3. **`isspace`.** Python accepts the non-breaking spaces and U+0085
 *     that `Character.isWhitespace` rejects, and the U+001C-U+001F
 *     separators that `Character.isSpaceChar` rejects. See
 *     [isPythonSpace].
 *
 * There is a fourth trap that is not about character classes at all:
 * **token length must be counted in characters, not UTF-16 units.**
 * Python's `len` counts code points, so a single U+20000 has length 1
 * and is dropped by the `len(word) > 1` filter, while
 * `"𠀀".length` in Kotlin is 2. Use [characterCount].
 */
object PythonText {

    /**
     * True when Python's `str.isalnum()` would be true for this code
     * point.
     *
     * Python's `isalnum()` is `isalpha() or isdecimal() or isdigit()
     * or isnumeric()`. `Character.isLetter` covers `isalpha`,
     * `Character.isDigit` covers the decimal digits, and the two
     * remaining number categories must be added explicitly.
     */
    fun isPythonAlnum(codePoint: Int): Boolean {
        val type = Character.getType(codePoint)

        return Character.isLetter(codePoint) ||
            Character.isDigit(codePoint) ||
            type == Character.LETTER_NUMBER.toInt() ||
            type == Character.OTHER_NUMBER.toInt()
    }

    /**
     * True when Python's `str.isspace()` would be true for this code
     * point.
     *
     * Java splits this concept across two predicates and Python's set
     * is not the union of them:
     *
     * | Character              | Python | isWhitespace | isSpaceChar |
     * |------------------------|--------|--------------|-------------|
     * | U+00A0 NO-BREAK SPACE  | true   | false        | true        |
     * | U+2007 FIGURE SPACE    | true   | false        | true        |
     * | U+202F NARROW NBSP     | true   | false        | true        |
     * | U+0085 NEXT LINE       | true   | false        | false       |
     * | U+001C-U+001F          | true   | true         | false       |
     * | U+200B ZERO WIDTH SPACE| false  | false        | false       |
     *
     * U+0085 is the case that neither Java predicate reports, so it is
     * added by hand.
     */
    fun isPythonSpace(codePoint: Int): Boolean =
        Character.isWhitespace(codePoint) ||
            Character.isSpaceChar(codePoint) ||
            codePoint == 0x0085

    /**
     * Lowercase the whole string, then replace every character that is
     * neither alphanumeric nor whitespace with a single space.
     */
    private fun clean(text: String): String {
        val lowered = text.lowercase(Locale.ROOT)

        val builder = StringBuilder(lowered.length)
        var index = 0

        while (index < lowered.length) {
            val codePoint = lowered.codePointAt(index)

            if (isPythonAlnum(codePoint) || isPythonSpace(codePoint)) {
                builder.appendCodePoint(codePoint)
            } else {
                builder.append(' ')
            }

            index += Character.charCount(codePoint)
        }

        return builder.toString()
    }

    /**
     * Split on runs of Python whitespace, dropping empty tokens.
     *
     * This is Python's argument-free `str.split()`. It cannot be
     * replaced with a regex `\s+` split because Java's `\s` is
     * ASCII-only and because the whitespace set differs (see
     * [isPythonSpace]).
     */
    fun splitPythonWhitespace(text: String): List<String> = pythonSplit(text)

    private fun pythonSplit(text: String): List<String> {
        val tokens = ArrayList<String>()
        val current = StringBuilder()

        var index = 0

        while (index < text.length) {
            val codePoint = text.codePointAt(index)

            if (isPythonSpace(codePoint)) {
                if (current.isNotEmpty()) {
                    tokens.add(current.toString())
                    current.setLength(0)
                }
            } else {
                current.appendCodePoint(codePoint)
            }

            index += Character.charCount(codePoint)
        }

        if (current.isNotEmpty()) {
            tokens.add(current.toString())
        }

        return tokens
    }

    /**
     * Number of characters, matching Python's `len`.
     *
     * A supplementary-plane character is two UTF-16 units but one
     * Python character.
     */
    fun characterCount(token: String): Int = token.codePointCount(0, token.length)

    /**
     * Tokenize document content. Tokens shorter than two characters
     * are dropped, counted in characters rather than UTF-16 units.
     */
    fun tokenize(text: String): List<String> =
        pythonSplit(clean(text)).filter { characterCount(it) > 1 }

    /**
     * Tokenize filenames and search queries. Every non-empty token is
     * kept, including single characters and bare digits.
     */
    fun tokenizeFilename(text: String): List<String> =
        pythonSplit(clean(text)).filter { it.isNotEmpty() }
}
