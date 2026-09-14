package com.vtu.search.parity

import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * Replays every contract vector from
 * `tests/golden/search_engine_vectors.json` against the Kotlin ports.
 *
 * These tests are the reason the harness module is pure JVM: they need
 * no Android SDK, so tokenizer, sanitizer and rounding parity can be
 * verified the moment a JDK exists.
 */
class ContractParityTest {

    private val vectorsPath: File =
        System.getProperty("golden.vectors.path")
            ?.let(::File)
            ?: File("../tests/golden/search_engine_vectors.json")

    private val document = GoldenVectors.parse(
        vectorsPath.readText(encoding = Charsets.UTF_8),
    )

    private val contract = document["contract_vectors"]!!

    @Test
    fun `golden vector file is present`() {
        assertTrue(
            vectorsPath.exists(),
            "expected golden vectors at ${vectorsPath.absolutePath}",
        )
    }

    @Test
    fun `every contract vector passes`() {
        val report = ContractHarness.run(vectorsPath)

        assertTrue(
            report.contractFailures.isEmpty(),
            "contract failures:\n" + report.contractFailures.joinToString("\n"),
        )

        assertTrue(report.contractChecks > 0, "no contract checks were run")
    }

    @Test
    fun `content tokenizer matches every recorded case`() {
        val failures = ArrayList<String>()

        for (case in cases("content_tokenize")) {
            val actual = PythonText.tokenize(case.input)

            if (actual != case.expected) {
                failures += "tokenize(${case.input}) expected ${case.expected} but got $actual"
            }
        }

        assertTrue(failures.isEmpty(), failures.joinToString("\n"))
    }

    @Test
    fun `filename tokenizer matches every recorded case`() {
        val failures = ArrayList<String>()

        for (case in cases("filename_tokenize")) {
            val actual = PythonText.tokenizeFilename(case.input)

            if (actual != case.expected) {
                failures +=
                    "tokenizeFilename(${case.input}) expected ${case.expected} but got $actual"
            }
        }

        assertTrue(failures.isEmpty(), failures.joinToString("\n"))
    }

    @Test
    fun `sanitizer matches every recorded case`() {
        val failures = ArrayList<String>()

        for (case in cases("sanitize_filename")) {
            val actual = SecureFilename.sanitize(case.input)

            if (actual != case.expected.singleOrNull()) {
                failures +=
                    "sanitize(${case.input}) expected ${case.expected} but got [$actual]"
            }
        }

        assertTrue(failures.isEmpty(), failures.joinToString("\n"))
    }

    @Test
    fun `rounding matches every recorded case`() {
        val failures = ArrayList<String>()

        val rounds = contract["round_half_even"] as kotlinx.serialization.json.JsonArray

        for (element in rounds) {
            val case = element as kotlinx.serialization.json.JsonObject

            val value = case["value"].toString().toDouble()
            val digits = case["digits"].toString().toInt()
            val expected = case["expected"].toString().toDouble()

            val actual = PythonRound.round(value, digits)

            if (actual != expected) {
                failures += "round($value, $digits) expected $expected but got $actual"
            }
        }

        assertTrue(failures.isEmpty(), failures.joinToString("\n"))
    }

    private data class StringCase(val input: String, val expected: List<String>)

    private fun cases(section: String): List<StringCase> {
        val array = contract[section] as kotlinx.serialization.json.JsonArray

        return array.map { element ->
            val obj = element as kotlinx.serialization.json.JsonObject

            StringCase(
                input = (obj["input"] as kotlinx.serialization.json.JsonPrimitive).content,
                expected = (obj["expected"] as kotlinx.serialization.json.JsonArray)
                    .map { (it as kotlinx.serialization.json.JsonPrimitive).content },
            )
        }
    }
}

/** Focused tests for the Unicode divergences that motivate the port. */
class PythonTextDivergenceTest {

    @Test
    fun `u0130 expands and splits`() {
        assertEquals(listOf("stanbul"), PythonText.tokenize("\u0130stanbul"))
        assertEquals(listOf("i", "stanbul"), PythonText.tokenizeFilename("\u0130stanbul"))
    }

    @Test
    fun `ascii istanbul is not a match for the dotted capital`() {
        // "istanbul" is genuinely absent from the Python index.
        assertEquals(listOf("istanbul"), PythonText.tokenize("istanbul"))
    }

    @Test
    fun `non decimal numerics are alphanumeric`() {
        assertTrue(PythonText.isPythonAlnum(0x00BD), "U+00BD should be alnum")
        assertTrue(PythonText.isPythonAlnum(0x00B2), "U+00B2 should be alnum")
        assertTrue(PythonText.isPythonAlnum(0x216B), "U+216B should be alnum")
        assertFalse(PythonText.isPythonAlnum(0x0307), "U+0307 should not be alnum")
    }

    @Test
    fun `python whitespace includes the non breaking spaces and u0085`() {
        for (codePoint in listOf(0x00A0, 0x2007, 0x202F, 0x0085, 0x001C, 0x001F)) {
            assertTrue(
                PythonText.isPythonSpace(codePoint),
                "U+%04X should be Python whitespace".format(codePoint),
            )
        }

        assertFalse(PythonText.isPythonSpace(0x200B), "U+200B should not be whitespace")
    }

    @Test
    fun `final sigma rule is preserved`() {
        assertEquals(listOf("\u03c3\u03c2"), PythonText.tokenize("\u03a3\u03a3"))
    }

    @Test
    fun `token length counts characters not utf16 units`() {
        // U+20000 is one Python character but two UTF-16 units, so the
        // len>1 filter must drop a single one.
        assertEquals(1, PythonText.characterCount("\uD840\uDC00"))
        assertEquals(2, "\uD840\uDC00".length)

        assertEquals(emptyList(), PythonText.tokenize("\uD840\uDC00"))
        assertEquals(listOf("\uD840\uDC00\uD840\uDC00"), PythonText.tokenize("\uD840\uDC00\uD840\uDC00"))
    }

    @Test
    fun `content tokenizer drops single characters and filename tokenizer keeps them`() {
        assertEquals(emptyList(), PythonText.tokenize("a b c"))
        assertEquals(listOf("a", "b", "c"), PythonText.tokenizeFilename("a b c"))
    }
}

/** Focused tests for the sanitizer rules that are easy to get wrong. */
class SecureFilenameTest {

    @Test
    fun `disallowed characters are removed not replaced`() {
        assertEquals("ab.txt", SecureFilename.sanitize("a!b.txt"))
        assertEquals("abc.txt", SecureFilename.sanitize("a#b$c.txt"))
        assertEquals("100.txt", SecureFilename.sanitize("100%.txt"))
    }

    @Test
    fun `hyphen is in the allowed set`() {
        assertEquals("file-name.txt", SecureFilename.sanitize("file-name.txt"))
    }

    @Test
    fun `whitespace runs collapse to a single underscore`() {
        assertEquals("a_b.txt", SecureFilename.sanitize("a  b.txt"))
        assertEquals("My_File.pdf", SecureFilename.sanitize("My File.pdf"))
    }

    @Test
    fun `path traversal is destroyed`() {
        assertEquals("passwd.txt", SecureFilename.sanitize("../../etc/passwd.txt"))
        assertEquals("c.txt", SecureFilename.sanitize("a/b/c.txt"))
    }

    @Test
    fun `reserved device names are not rewritten on posix`() {
        assertEquals("CON.txt", SecureFilename.sanitize("CON.txt"))
        assertEquals("nul.pdf", SecureFilename.sanitize("nul.pdf"))
    }

    @Test
    fun `unsupported extensions are rejected`() {
        assertEquals("", SecureFilename.sanitize("archive.zip"))
        assertEquals("", SecureFilename.sanitize("no-ext"))
        assertEquals("", SecureFilename.sanitize(""))
    }

    @Test
    fun `filename index uses the stem`() {
        assertEquals(
            listOf("bcs502", "module", "2"),
            PythonText.tokenizeFilename(SecureFilename.stemOf("BCS502_Module_2.pdf")),
        )
    }
}

/** Focused tests for the exact rounding port. */
class PythonRoundTest {

    @Test
    fun `exact binary value is rounded not the decimal literal`() {
        assertEquals(2.0, PythonRound.round(2.00005, 4))
        assertEquals(0.5678, PythonRound.round(0.56785, 4))
        assertEquals(2.67, PythonRound.round(2.675, 2))
        assertEquals(1.0001, PythonRound.round(1.00005, 4))
    }

    @Test
    fun `ties round to even`() {
        assertEquals(0.0, PythonRound.round(0.5, 0))
        assertEquals(2.0, PythonRound.round(1.5, 0))
        assertEquals(2.0, PythonRound.round(2.5, 0))
        assertEquals(0.12, PythonRound.round(0.125, 2))
    }

    @Test
    fun `half up would be wrong for the documented examples`() {
        // Guards the port against a HALF_UP regression: these are the
        // values that differ between the two modes.
        assertEquals(2.67, PythonRound.round(2.675, 2))
        assertEquals(0.12, PythonRound.round(0.125, 2))
        assertEquals(2.0, PythonRound.round(2.5, 0))
    }
}
