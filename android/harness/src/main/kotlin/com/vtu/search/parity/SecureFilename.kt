package com.vtu.search.parity

import java.text.Normalizer

/**
 * Exact port of `sanitize_upload_filename` from `app.py`.
 *
 * The sanitizer decides document identity, so it is a high-risk port:
 * get it wrong and the same upload produces different document names on
 * the device than on the server.
 *
 * Python takes the basename and delegates to Werkzeug's
 * `secure_filename`, then rejects unsupported extensions. The Werkzeug
 * algorithm is:
 *
 * ```python
 * filename = unicodedata.normalize("NFKD", filename)
 * filename = filename.encode("ascii", "ignore").decode("ascii")
 *
 * for sep in os.sep, os.path.altsep:
 *     if sep:
 *         filename = filename.replace(sep, " ")
 *
 * filename = str(
 *     _filename_ascii_strip_re.sub("", "_".join(filename.split()))
 * ).strip("._")
 * ```
 *
 * with `_filename_ascii_strip_re = re.compile(r"[^A-Za-z0-9_.-]")`.
 *
 * Three details are easy to get wrong, and all three are pinned by
 * `contract_vectors.sanitize_filename`:
 *
 *  * Disallowed characters are **removed**, not replaced.
 *    `a!b.txt` is `ab.txt`, not `a_b.txt`.
 *  * Hyphen is in the allowed set, so `file-name.txt` is unchanged.
 *  * Whitespace runs become a **single underscore**, because Werkzeug
 *    joins `filename.split()` with `"_"`. `a  b.txt` is `a_b.txt`.
 *
 * Trailing dots and underscores survive: `trail__.txt` is unchanged.
 * Reserved Windows device names are **not** rewritten, because Werkzeug
 * only filters them when `os.name == "nt"` and the server runs on
 * Linux; adding that filtering here would diverge from the server.
 */
object SecureFilename {

    val SUPPORTED_EXTENSIONS = listOf(".pdf", ".docx", ".txt")

    /** The exact Werkzeug character class `[^A-Za-z0-9_.-]`. */
    private fun isAllowedAsciiCharacter(character: Char): Boolean =
        (character in 'A'..'Z') ||
            (character in 'a'..'z') ||
            (character in '0'..'9') ||
            character == '_' ||
            character == '.' ||
            character == '-'

    /**
     * Return the safe local filename for an upload, or the empty string
     * when the name is unusable.
     */
    fun sanitize(filename: String?): String {
        if (filename.isNullOrEmpty()) {
            return ""
        }

        val safeName = secureFilename(basename(filename))

        if (safeName.isEmpty()) {
            return ""
        }

        if (!hasSupportedExtension(safeName)) {
            return ""
        }

        return safeName
    }

    /** True when the name ends in a supported extension. */
    fun hasSupportedExtension(filename: String): Boolean {
        val lowered = filename.lowercase()
        return SUPPORTED_EXTENSIONS.any { lowered.endsWith(it) }
    }

    /**
     * Return the extension without the dot, lowercased.
     *
     * Mirrors `os.path.splitext(filename)[1].lower().lstrip(".")`.
     */
    fun extensionOf(filename: String): String {
        val index = filename.lastIndexOf('.')

        if (index <= 0) {
            return ""
        }

        return filename.substring(index + 1).lowercase()
    }

    /**
     * Return the stem, mirroring `os.path.splitext(filename)[0]`.
     *
     * The filename index is built from the stem, so the extension is
     * never part of the indexed filename terms.
     */
    fun stemOf(filename: String): String {
        val index = filename.lastIndexOf('.')

        if (index <= 0) {
            return filename
        }

        return filename.substring(0, index)
    }

    /**
     * Strip every directory component.
     *
     * This mirrors `os.path.basename` **on the platform the engine runs
     * on**, which is POSIX, and there only `/` separates. A backslash is
     * an ordinary character: `os.path.basename("dir\\sub\\file.txt")`
     * returns the whole string, and it is `secureFilename` that then
     * deletes the backslash, yielding `dirsubfile.txt`.
     *
     * This previously broke on both separators, with a comment claiming
     * that mirrored `os.path.basename` because "an upload may arrive
     * from any client". It does not mirror it, and the effect was a real
     * cross-mode divergence: every Windows-shaped upload name would have
     * been stored as `file.txt` on Android and `dirsubfile.txt` on the
     * server, producing different filename tokens and different search
     * results for the same document.
     *
     * The recorded contract vector `..\\..\\x.txt` returns `x.txt`
     * under both behaviours, so the vectors could not catch it; the
     * divergence was found by comparing this file's model against the
     * engine directly, which `tests/test_port_model_matches_engine.py`
     * now does permanently.
     */
    fun basename(filename: String): String {
        var end = filename.length

        while (end > 0) {
            if (filename[end - 1] == '/') {
                break
            }

            end--
        }

        return filename.substring(end)
    }

    /**
     * The Werkzeug `secure_filename` transformation.
     *
     * Order matters and is reproduced exactly: transliterate, replace
     * separators with spaces, collapse whitespace runs into
     * underscores, remove the remaining disallowed characters, then
     * strip leading and trailing dots and underscores.
     */
    fun secureFilename(filename: String): String {
        if (filename.isEmpty()) {
            return ""
        }

        // 1. NFKD-decompose and discard anything outside ASCII. This is
        //    what turns "café" into "cafe".
        val ascii = toAscii(filename)

        // 2. '/', os.sep, becomes a space. os.path.altsep is None on
        //    POSIX, so backslash is NOT replaced here. It is removed by
        //    the character filter instead.
        val separated = ascii.replace('/', ' ')

        // 3. Whitespace runs become a single underscore.
        val joined = PythonText.splitPythonWhitespace(separated).joinToString("_")

        // 4. Remove every character outside [A-Za-z0-9_.-].
        val filtered = joined.filter { isAllowedAsciiCharacter(it) }

        // 5. Strip leading and trailing dots and underscores.
        return filtered.trim('.', '_')
    }

    /**
     * NFKD-normalise and keep only ASCII characters.
     *
     * Werkzeug encodes with ASCII and decodes with `ignore`, which is
     * equivalent to discarding every character outside ASCII after
     * normalisation.
     */
    private fun toAscii(value: String): String {
        val normalised = Normalizer.normalize(value, Normalizer.Form.NFKD)

        val builder = StringBuilder(normalised.length)

        for (character in normalised) {
            if (character.code in 1..0x7F) {
                builder.append(character)
            }
        }

        return builder.toString()
    }
}
