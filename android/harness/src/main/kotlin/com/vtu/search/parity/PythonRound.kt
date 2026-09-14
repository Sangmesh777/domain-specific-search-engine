package com.vtu.search.parity

import java.math.BigDecimal
import java.math.RoundingMode

/**
 * Exact port of Python's `round(x, digits)` for binary64 values.
 *
 * Python rounds the **exact binary value** of the double to the given
 * number of decimal places, with ties going to the even digit. It does
 * not round the shortest decimal representation, and it does not round
 * half away from zero.
 *
 * [BigDecimal]'s `Double` constructor captures the exact binary value,
 * which is what makes this match:
 *
 * ```kotlin
 * BigDecimal(value)                     // exact binary value   correct
 * BigDecimal.valueOf(value)             // decimal string       WRONG
 * RoundingMode.HALF_UP                  // ties away from zero  WRONG
 * "%.4f".format(value)                  // decimal string       WRONG
 * ```
 *
 * Worked examples, all pinned by `contract_vectors.round_half_even`:
 *
 * ```
 * round(2.00005, 4) -> 2.0      the literal is 2.00004999999999988...
 * round(0.56785, 4) -> 0.5678   the literal is 0.56784999999999998...
 * round(2.675,   2) -> 2.67     the literal is 2.67499999999999982...
 * round(0.125,   2) -> 0.12     exact tie, rounds to even
 * round(0.135,   2) -> 0.14     not a tie: 0.13500000000000000888...
 * ```
 */
object PythonRound {

    /**
     * Equivalent to Python's `round(value, digits)`.
     *
     * @param value the binary64 value to round
     * @param digits the number of decimal places to keep
     */
    fun round(value: Double, digits: Int): Double {
        require(digits >= 0) { "digits must not be negative, was $digits" }

        return BigDecimal(value)
            .setScale(digits, RoundingMode.HALF_EVEN)
            .toDouble()
    }

    /**
     * Equivalent to Python's `round(value, digits)` returned as decimal
     * text, which is how the search responses serialise scores.
     *
     * Python serialises with `repr`, which produces the shortest
     * decimal string that round-trips to the same binary64 value.
     */
    fun roundToString(value: Double, digits: Int): String =
        round(value, digits).toString()
}
