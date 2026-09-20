package com.vtu.search.parity

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.io.File

/**
 * Runs the contract vectors from `tests/golden/search_engine_vectors.json`
 * against the Kotlin ports.
 *
 * The contract section is self-contained: it needs no corpus and no
 * backend, so it can prove tokenizer, rounding and sanitizer parity with
 * nothing but a JDK. Search-vector replay additionally needs a
 * [SearchOracle] implementation (the local engine), and is reported as
 * not-run when none is supplied rather than being silently skipped.
 *
 * Exit status is non-zero when any executed check fails.
 */
object ContractHarness {

    /** Supplies live search responses so vectors can be replayed. */
    interface SearchOracle {
        /** Run a query and return the response as a JSON element. */
        fun search(query: String, page: Int?, limit: Int?): JsonElement
    }

    data class Report(
        val contractChecks: Int,
        val contractFailures: List<String>,
        val searchChecks: Int,
        val searchFailures: List<String>,
        val searchBlocked: Boolean,
    ) {
        val passed: Boolean
            get() = contractFailures.isEmpty() && searchFailures.isEmpty()
    }

    fun defaultVectorsPath(): File =
        System.getProperty("golden.vectors.path")
            ?.let(::File)
            ?: File("../tests/golden/search_engine_vectors.json").let {
                if (it.exists()) it else File("tests/golden/search_engine_vectors.json")
            }

    fun defaultSidecarPath(): File =
        System.getProperty("corpus.sidecar.path")
            ?.let(::File)
            ?: File("../artifacts/android/corpus_sidecar.json").let {
                if (it.exists()) it else File("artifacts/android/corpus_sidecar.json")
            }

    fun run(vectorsPath: File = defaultVectorsPath(), oracle: SearchOracle? = null): Report {
        val document = GoldenVectors.parse(vectorsPath.readText(encoding = Charsets.UTF_8))

        val contractFailures = ArrayList<String>()
        var contractChecks = 0

        contractChecks += checkContentTokenizer(document, contractFailures)
        contractChecks += checkFilenameTokenizer(document, contractFailures)
        contractChecks += checkSanitizer(document, contractFailures)
        contractChecks += checkRounding(document, contractFailures)
        contractChecks += checkNormalizeSearchQuery(document, contractFailures)
        contractChecks += checkParseFiletypeFilter(document, contractFailures)

        val activeOracle: SearchOracle? = oracle ?: run {
            val sidecarFile = defaultSidecarPath()
            if (sidecarFile.exists()) {
                com.vtu.search.ranking.RankingEngine.fromSidecarJson(
                    sidecarFile.readText(encoding = Charsets.UTF_8),
                )
            } else {
                null
            }
        }

        val searchFailures = ArrayList<String>()
        var searchChecks = 0

        if (activeOracle != null) {
            for (vector in GoldenVectors.load(document)) {
                searchChecks++

                val actual = GoldenVectors.normalizeResponse(
                    activeOracle.search(vector.query, vector.page, vector.limit),
                )

                val differences = GoldenVectors.compare(actual, vector.expected)

                if (differences.isNotEmpty()) {
                    searchFailures +=
                        "${vector.id} (q=${vector.query}, ${vector.corpusState} corpus): " +
                        differences.take(5).joinToString("; ")
                }
            }
        }

        return Report(
            contractChecks = contractChecks,
            contractFailures = contractFailures,
            searchChecks = searchChecks,
            searchFailures = searchFailures,
            searchBlocked = activeOracle == null,
        )
    }

    private fun contractSection(document: JsonObject): JsonObject =
        document["contract_vectors"] as JsonObject

    private fun checkContentTokenizer(
        document: JsonObject,
        failures: MutableList<String>,
    ): Int {
        val cases = contractSection(document)["content_tokenize"] as JsonArray

        for (element in cases) {
            val case = element as JsonObject
            val input = (case["input"] as JsonPrimitive).content
            val expected = readStringList(case["expected"]!!)
            val actual = PythonText.tokenize(input)

            if (actual != expected) {
                failures += "tokenize($input) expected $expected but got $actual"
            }
        }

        return cases.size
    }

    private fun checkFilenameTokenizer(
        document: JsonObject,
        failures: MutableList<String>,
    ): Int {
        val cases = contractSection(document)["filename_tokenize"] as JsonArray

        for (element in cases) {
            val case = element as JsonObject
            val input = (case["input"] as JsonPrimitive).content
            val expected = readStringList(case["expected"]!!)
            val actual = PythonText.tokenizeFilename(input)

            if (actual != expected) {
                failures += "tokenizeFilename($input) expected $expected but got $actual"
            }
        }

        return cases.size
    }

    private fun checkSanitizer(
        document: JsonObject,
        failures: MutableList<String>,
    ): Int {
        val cases = contractSection(document)["sanitize_filename"] as JsonArray

        for (element in cases) {
            val case = element as JsonObject
            val input = (case["input"] as JsonPrimitive).content
            val expected = (case["expected"] as JsonPrimitive).content
            val actual = SecureFilename.sanitize(input)

            if (actual != expected) {
                failures += "sanitize($input) expected \"$expected\" but got \"$actual\""
            }
        }

        return cases.size
    }

    private fun checkRounding(
        document: JsonObject,
        failures: MutableList<String>,
    ): Int {
        val cases = contractSection(document)["round_half_even"] as JsonArray

        for (element in cases) {
            val case = element as JsonObject
            val value = (case["value"] as JsonPrimitive).content.toDouble()
            val digits = (case["digits"] as JsonPrimitive).content.toInt()
            val expected = (case["expected"] as JsonPrimitive).content.toDouble()

            val actual = PythonRound.round(value, digits)

            if (actual != expected) {
                failures += "round($value, $digits) expected $expected but got $actual"
            }
        }

        return cases.size
    }

    private fun checkNormalizeSearchQuery(
        document: JsonObject,
        failures: MutableList<String>,
    ): Int {
        val cases = contractSection(document)["normalize_search_query"] as? JsonArray ?: return 0

        for (element in cases) {
            val case = element as JsonObject
            val input = (case["input"] as JsonPrimitive).content
            val expected = (case["expected"] as JsonPrimitive).content
            val actual = PythonText.normalizeSearchQuery(input)

            if (actual != expected) {
                failures += "normalizeSearchQuery($input) expected \"$expected\" but got \"$actual\""
            }
        }

        return cases.size
    }

    private fun checkParseFiletypeFilter(
        document: JsonObject,
        failures: MutableList<String>,
    ): Int {
        val cases = contractSection(document)["parse_filetype_filter"] as? JsonArray ?: return 0

        for (element in cases) {
            val case = element as JsonObject
            val input = (case["input"] as JsonPrimitive).content
            val expectedQuery = (case["expected_query"] as JsonPrimitive).content
            val expectedFiletype = (case["expected_filetype"] as? JsonPrimitive)?.let {
                if (it is JsonNull || it.content == "null") null else it.content
            }
            val (actualQuery, actualFiletype) = PythonText.parseFiletypeFilter(input)

            if (actualQuery != expectedQuery || actualFiletype != expectedFiletype) {
                failures += "parseFiletypeFilter($input) expected ($expectedQuery, $expectedFiletype) but got ($actualQuery, $actualFiletype)"
            }
        }

        return cases.size
    }

    private fun readStringList(element: JsonElement): List<String> =
        (element as JsonArray).map { (it as JsonPrimitive).content }
}

/** Command line entry point: `./gradlew :harness:run`. */
fun main() {
    val path = ContractHarness.defaultVectorsPath()

    println("Kotlin parity harness")
    println("vectors: ${path.absolutePath}")
    println()

    val report = ContractHarness.run(path)

    println("contract checks : ${report.contractChecks}")
    println("contract failures: ${report.contractFailures.size}")
    println("search vectors  : ${if (report.searchBlocked) "NOT RUN (no SearchOracle)" else report.searchChecks.toString()}")
    println("search failures : ${report.searchFailures.size}")
    println()

    for (failure in report.contractFailures.take(40)) {
        println("  FAIL $failure")
    }

    for (failure in report.searchFailures.take(40)) {
        println("  FAIL $failure")
    }

    if (report.searchBlocked) {
        println(
            "No SearchOracle was supplied and no corpus sidecar was found, " +
                "so the search vectors were not replayed. Contract checks above are still authoritative.",
        )
    }

    if (!report.passed) {
        println()
        println("PARITY FAILED")
        kotlin.system.exitProcess(1)
    }

    println("PARITY OK")
}
