package com.vtu.search.parity

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * Loader and comparator for `tests/golden/search_engine_vectors.json`.
 *
 * This mirrors `tools/verify_golden_vectors.py` exactly. Both compare
 * the **complete** response:
 *
 *  * response shape (bare list vs paginated object)
 *  * every object's key set
 *  * every value
 *  * floating point values as bit-identical binary64
 *
 * Comparing only `title` and `score` would not catch the ranking drift
 * this file exists to detect.
 */
object GoldenVectors {

    /**
     * Fields removed before comparison.
     *
     * `path` is the absolute server filesystem path. It is environment
     * specific, is not part of the search contract, and must never be
     * exposed to a client as a document identity.
     */
    val DROPPED_RESULT_FIELDS = setOf("path")

    private val json = Json {
        ignoreUnknownKeys = true
        isLenient = false
    }

    fun parse(document: String): JsonObject =
        json.parseToJsonElement(document) as JsonObject

    /** One recorded vector. */
    data class Vector(
        val id: String,
        val query: String,
        val page: Int?,
        val limit: Int?,
        val corpusState: String,
        val expected: JsonElement,
    )

    /** A single difference between expected and actual. */
    data class Difference(val path: String, val message: String) {
        override fun toString(): String = "$path: $message"
    }

    /** The result of comparing one vector. */
    data class Outcome(
        val vector: Vector,
        val differences: List<Difference>,
    ) {
        val passed: Boolean get() = differences.isEmpty()
    }

    /** Parse the whole vector document into a flat list of vectors. */
    fun load(document: JsonObject): List<Vector> {
        val vectors = ArrayList<Vector>()

        vectors += readVectors(document, "vectors", "populated")
        vectors += readVectors(document, "empty_corpus_vectors", "empty")

        return vectors
    }

    private fun readVectors(
        document: JsonObject,
        key: String,
        corpusState: String,
    ): List<Vector> {
        val array = document[key] as? JsonArray ?: return emptyList()

        return array.mapNotNull { element ->
            val vector = element as? JsonObject ?: return@mapNotNull null

            val params = vector["params"] as? JsonObject

            Vector(
                id = (vector["id"] as JsonPrimitive).content,
                query = (vector["query"] as JsonPrimitive).content,
                page = params?.get("page")?.let { primitiveOrNull(it)?.intOrNull() },
                limit = params?.get("limit")?.let { primitiveOrNull(it)?.intOrNull() },
                corpusState = corpusState,
                expected = vector["response"]!!,
            )
        }
    }

    private fun primitiveOrNull(element: JsonElement): JsonPrimitive? {
        val primitive = element as? JsonPrimitive ?: return null
        return if (primitive is JsonNull || primitive.content == "null") null else primitive
    }

    private fun JsonPrimitive.intOrNull(): Int? = content.toIntOrNull()

    /**
     * Normalise a live response the same way the generator did.
     *
     * Reports rather than throws, so one malformed response does not
     * hide the rest of the run.
     */
    fun normalizeResponse(payload: JsonElement): JsonElement = when (payload) {
        is JsonArray -> JsonArray(payload.map { normalizeResult(it) })
        is JsonObject -> JsonObject(
            payload.mapValues { (key, value) ->
                if (key == "results" && value is JsonArray) {
                    JsonArray(value.map { normalizeResult(it) })
                } else {
                    value
                }
            },
        )
        else -> payload
    }

    private fun normalizeResult(element: JsonElement): JsonElement {
        val result = element as? JsonObject ?: return element

        return JsonObject(
            result.filterKeys { it !in DROPPED_RESULT_FIELDS },
        )
    }

    /**
     * Compare a live response against a recorded one.
     *
     * @param tolerance float tolerance for diagnosis only; leave at 0.0
     *   for the real gate. A non-zero tolerance can only ever be used to
     *   explain a failure, never to accept one.
     */
    fun compare(
        actual: JsonElement,
        expected: JsonElement,
        tolerance: Double = 0.0,
    ): List<Difference> {
        val differences = ArrayList<Difference>()
        compareInto(actual, expected, "response", tolerance, differences)
        return differences
    }

    private fun compareInto(
        actual: JsonElement,
        expected: JsonElement,
        path: String,
        tolerance: Double,
        differences: MutableList<Difference>,
    ) {
        when (expected) {
            is JsonObject -> {
                if (actual !is JsonObject) {
                    differences += Difference(
                        path,
                        "shape mismatch, expected object but got ${actual::class.simpleName}",
                    )
                    return
                }

                for (key in expected.keys - actual.keys) {
                    differences += Difference(
                        "$path.$key",
                        "missing (expected ${render(expected[key]!!)})",
                    )
                }

                for (key in actual.keys - expected.keys) {
                    differences += Difference(
                        "$path.$key",
                        "unexpected field with value ${render(actual[key]!!)}",
                    )
                }

                for (key in (expected.keys intersect actual.keys).sorted()) {
                    compareInto(
                        actual[key]!!,
                        expected[key]!!,
                        "$path.$key",
                        tolerance,
                        differences,
                    )
                }
            }

            is JsonArray -> {
                if (actual !is JsonArray) {
                    differences += Difference(
                        path,
                        "shape mismatch, expected list but got ${actual::class.simpleName}",
                    )
                    return
                }

                if (actual.size != expected.size) {
                    differences += Difference(
                        path,
                        "length mismatch, expected ${expected.size} but got ${actual.size}",
                    )
                }

                for (index in 0 until minOf(actual.size, expected.size)) {
                    compareInto(
                        actual[index],
                        expected[index],
                        "$path[$index]",
                        tolerance,
                        differences,
                    )
                }
            }

            else -> compareScalar(actual, expected, path, tolerance, differences)
        }
    }

    private fun compareScalar(
        actual: JsonElement,
        expected: JsonElement,
        path: String,
        tolerance: Double,
        differences: MutableList<Difference>,
    ) {
        val expectedPrimitive = expected as? JsonPrimitive
        val actualPrimitive = actual as? JsonPrimitive

        if (expectedPrimitive == null || actualPrimitive == null) {
            if (actual != expected) {
                differences += Difference(
                    path,
                    "expected ${render(expected)} but got ${render(actual)}",
                )
            }
            return
        }

        if (expectedPrimitive is JsonNull || actualPrimitive is JsonNull) {
            if (expected !is JsonNull || actual !is JsonNull) {
                differences += Difference(
                    path,
                    "expected ${render(expected)} but got ${render(actual)}",
                )
            }
            return
        }

        if (!expectedPrimitive.isString && !actualPrimitive.isString) {
            compareNumbers(actualPrimitive, expectedPrimitive, path, tolerance, differences)
            return
        }

        if (actualPrimitive.content != expectedPrimitive.content ||
            actualPrimitive.isString != expectedPrimitive.isString
        ) {
            differences += Difference(
                path,
                "expected ${render(expected)} but got ${render(actual)}",
            )
        }
    }

    private fun compareNumbers(
        actual: JsonPrimitive,
        expected: JsonPrimitive,
        path: String,
        tolerance: Double,
        differences: MutableList<Difference>,
    ) {
        val expectedIsIntegral = isIntegralLiteral(expected.content)
        val actualIsIntegral = isIntegralLiteral(actual.content)

        if (expectedIsIntegral != actualIsIntegral) {
            differences += Difference(
                path,
                "type mismatch, expected ${render(expected)} but got ${render(actual)}",
            )
            return
        }

        val expectedValue = expected.content.toDoubleOrNull()
        val actualValue = actual.content.toDoubleOrNull()

        if (expectedValue == null || actualValue == null) {
            if (actual.content != expected.content) {
                differences += Difference(
                    path,
                    "expected ${render(expected)} but got ${render(actual)}",
                )
            }
            return
        }

        if (tolerance > 0.0) {
            if (kotlin.math.abs(actualValue - expectedValue) > tolerance) {
                differences += Difference(
                    path,
                    "expected $expectedValue but got $actualValue",
                )
            }
            return
        }

        // Exact comparison. binary64 equality, not approximate
        // equality: the vectors exist to make small ranking drift fail.
        if (actualValue != expectedValue) {
            differences += Difference(
                path,
                "expected $expectedValue but got $actualValue",
            )
        }
    }

    /**
     * Distinguish `0` from `0.0`, matching Python's JSON type
     * distinction, which the Python verifier also enforces.
     */
    private fun isIntegralLiteral(literal: String): Boolean =
        !literal.contains('.') &&
            !literal.contains('e') &&
            !literal.contains('E')

    private fun render(element: JsonElement): String = element.toString()
}
