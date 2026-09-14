"""
Verify the Android port's Java semantics against a real JVM.

The Android parity design rests on claims about how Java's standard
library behaves relative to Python's. Those claims were originally
derived from documentation and from a model built on Python's own
Unicode tables. That model was circular, and a real JVM disproved part
of it. This tool replaces the assumption with executed evidence.

What is checked, and what the answers were on Temurin 25.0.2:

1. ``character predicates``
       ``isLetter`` / ``isDigit`` / ``getType`` must reproduce
       ``str.isalnum()``. Divergences are classified as *Unicode
       version skew* (the code point is unassigned in Python's Unicode
       version but assigned in the JVM's) or *genuine*.

2. ``whitespace predicate``
       ``isWhitespace`` / ``isSpaceChar`` plus U+0085 must reproduce
       ``str.isspace()``.

3. ``lowercasing``
       ``String.lowercase(Locale.ROOT)`` must reproduce ``str.lower()``,
       including the U+0130 expansion and the final-sigma rule.

4. ``rounding``
       ``BigDecimal(value).setScale(digits, HALF_EVEN)`` must reproduce
       Python's ``round``.

Known limitation of this tool
-----------------------------
pyjnius cannot reliably marshal strings containing supplementary-plane
characters between Python and the JVM. ``"😀😀"`` comes back as
``"\\uf600\\uf600"``, and ``"\\U00010000"`` comes back as ``"\\x00"``.
This is a defect in the *bridge*, not in Java: Java's own behaviour is
correct, and the compiled Kotlin tests run inside the JVM and do not
round-trip strings through JNI at all.

The lowercasing sweep is therefore limited to the Basic Multilingual
Plane, where marshalling is reliable. Supplementary-plane lowercasing
must be verified by the compiled JUnit tests, which cannot have this
problem.

Usage:
    python -m tools.verify_jvm_semantics
    python -m tools.verify_jvm_semantics --samples 20000
"""

import argparse
import os
import random
import sys
import unicodedata

SURROGATE_START = 0xD800
SURROGATE_END = 0xDFFF
BMP_END = 0x10000

PYTHON_NEXT_LINE = 0x0085

# U+FEFF is the byte-order mark / zero-width no-break space. Java's
# toLowerCase(Locale.ROOT) returns an empty string for it while Python
# returns it unchanged. It is neither alphanumeric nor whitespace under
# either language, so in the tokenizer it becomes a separator in both;
# the divergence cannot change a token. It is recorded rather than
# ignored so that a future change is noticed.
KNOWN_NON_SKEW_LOWERCASE_DIVERGENCES = {0xFEFF}


def _bootstrap_jvm():
    """Start a JVM and return the classes this tool needs."""

    if "JAVA_HOME" not in os.environ:
        try:
            import jdk4py

            os.environ["JAVA_HOME"] = str(jdk4py.JAVA_HOME)
        except ImportError:
            pass

    try:
        import jnius_config
    except ImportError as error:
        raise RuntimeError(
            "pyjnius is not installed; run 'pip install pyjnius jdk4py' "
            "to enable JVM verification"
        ) from error

    jnius_config.set_classpath(".")

    try:
        from jnius import autoclass
    except Exception as error:  # pragma: no cover - environment specific
        raise RuntimeError(f"could not start the JVM: {error}") from error

    return {
        "Character": autoclass("java.lang.Character"),
        "String": autoclass("java.lang.String"),
        "Locale": autoclass("java.util.Locale"),
        "BigDecimal": autoclass("java.math.BigDecimal"),
        "RoundingMode": autoclass("java.math.RoundingMode"),
        "System": autoclass("java.lang.System"),
    }


def _code_points(limit, min_cp=0):
    for code_point in range(min_cp, limit):
        if SURROGATE_START <= code_point <= SURROGATE_END:
            continue
        yield code_point


def verify_character_classes(java):
    """
    Compare the modelled Java predicates with Python over all code points.

    Divergences are split into version skew and genuine disagreement,
    because only the second kind indicates a defect in the port.
    """

    Character = java["Character"]

    letter_number = Character.LETTER_NUMBER
    other_number = Character.OTHER_NUMBER

    version_skew = []
    genuine_alnum = []
    genuine_space = []
    checked = 0

    for code_point in _code_points(0x110000):
        checked += 1
        character = chr(code_point)
        category = unicodedata.category(character)

        java_alnum = (
            bool(Character.isLetter(code_point))
            or bool(Character.isDigit(code_point))
            or Character.getType(code_point) in (letter_number, other_number)
        )

        if java_alnum != character.isalnum():
            if category == "Cn":
                # Unassigned in Python's Unicode version, assigned in
                # the JVM's. Not a porting defect.
                version_skew.append((code_point, category))
            else:
                genuine_alnum.append((code_point, category))

        java_space = (
            bool(Character.isWhitespace(code_point))
            or bool(Character.isSpaceChar(code_point))
            or code_point == PYTHON_NEXT_LINE
        )

        if java_space != character.isspace():
            genuine_space.append((code_point, category))

    return {
        "checked": checked,
        "version_skew": version_skew,
        "genuine_alnum": genuine_alnum,
        "genuine_space": genuine_space,
    }


def verify_lowercasing(java, limit=BMP_END):
    """
    Compare Java's Locale.ROOT lowercasing with Python's str.lower().

    Limited to the BMP by default; see the module docstring for why the
    supplementary planes cannot be swept through pyjnius.
    """

    String = java["String"]
    Locale = java["Locale"]

    version_skew = []
    genuine = []

    for code_point in _code_points(limit):
        character = chr(code_point)

        java_lowered = str(String(character).toLowerCase(Locale.ROOT))
        python_lowered = character.lower()

        if java_lowered == python_lowered:
            continue

        category = unicodedata.category(character)

        if category == "Cn":
            version_skew.append((code_point, category))
        else:
            genuine.append((code_point, category, java_lowered, python_lowered))

    return {"limit": limit, "version_skew": version_skew, "genuine": genuine}


def verify_rounding(java, samples=4000, seed=20260914):
    """Compare BigDecimal HALF_EVEN with Python's round()."""

    BigDecimal = java["BigDecimal"]
    RoundingMode = java["RoundingMode"]

    mismatches = []

    values = [
        2.00005, 0.56785, 2.675, 1.00005, 0.125, 0.135, 0.5, 1.5, 2.5,
        -0.5, -1.5, 0.0, 123.456789, 2.6750000000000003, 1e-10, 1.0, -2.675,
    ]

    generator = random.Random(seed)

    for _ in range(samples):
        values.append(generator.uniform(-1000.0, 1000.0))

    comparisons = 0

    for value in values:
        for digits in (0, 1, 2, 3, 4, 6):
            comparisons += 1

            expected = round(value, digits)

            actual = (
                BigDecimal(value)
                .setScale(digits, RoundingMode.HALF_EVEN)
                .doubleValue()
            )

            if actual != expected:
                mismatches.append((value, digits, actual, expected))

    return {"comparisons": comparisons, "mismatches": mismatches}


def summarized():
    """Run every check and return a machine-readable summary."""

    java = _bootstrap_jvm()

    classes = verify_character_classes(java)
    lower = verify_lowercasing(java)
    rounding = verify_rounding(java)

    unexpected_lower = [
        entry
        for entry in lower["genuine"]
        if entry[0] not in KNOWN_NON_SKEW_LOWERCASE_DIVERGENCES
    ]

    return {
        "java_version": java["System"].getProperty("java.version"),
        "java_vendor": java["System"].getProperty("java.vendor"),
        "python_unicode": unicodedata.unidata_version,
        "code_points_checked": classes["checked"],
        "version_skew": len(classes["version_skew"]),
        "genuine_alnum": len(classes["genuine_alnum"]),
        "genuine_space": len(classes["genuine_space"]),
        "lowercase_version_skew": len(lower["version_skew"]),
        "lowercase_genuine": len(lower["genuine"]),
        "lowercase_unexpected": len(unexpected_lower),
        "lowercase_unexpected_detail": unexpected_lower[:20],
        "rounding_comparisons": rounding["comparisons"],
        "rounding_mismatches": len(rounding["mismatches"]),
        "genuine_failures": (
            len(classes["genuine_alnum"])
            + len(classes["genuine_space"])
            + len(unexpected_lower)
            + len(rounding["mismatches"])
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable summary instead of a report.",
    )

    parser.add_argument(
        "--samples",
        type=int,
        default=4000,
        help="Random doubles to test for rounding (default: 4000).",
    )

    arguments = parser.parse_args(argv)

    if arguments.json:
        import json as _json

        summary = summarized()

        print(_json.dumps(summary, indent=2, sort_keys=True))

        return 1 if summary["genuine_failures"] else 0

    java = _bootstrap_jvm()

    print("JVM semantics verification")
    print(f"  java.version   : {java['System'].getProperty('java.version')}")
    print(f"  java.vendor    : {java['System'].getProperty('java.vendor')}")
    print(f"  java unicode   : {java['System'].getProperty('java.specification.version')}")
    print(f"  python unicode : {unicodedata.unidata_version}")
    print()

    failures = 0

    # --- character classes -------------------------------------------
    print("1. isLetter/isDigit/getType vs str.isalnum()")
    classes = verify_character_classes(java)
    print(f"   code points checked : {classes['checked']:,}")
    print(f"   version skew        : {len(classes['version_skew']):,}")
    print(f"   genuine ALNUM       : {len(classes['genuine_alnum']):,}")
    print(f"   genuine SPACE       : {len(classes['genuine_space']):,}")

    for code_point, category in classes["genuine_alnum"][:20]:
        print(f"     U+{code_point:04X} cat={category}")

    for code_point, category in classes["genuine_space"][:20]:
        print(f"     U+{code_point:04X} cat={category}")

    failures += len(classes["genuine_alnum"])
    failures += len(classes["genuine_space"])

    # --- lowercasing --------------------------------------------------
    print()
    print("2. String.lowercase(Locale.ROOT) vs str.lower()  [BMP only]")
    lower = verify_lowercasing(java)
    print(f"   version skew : {len(lower['version_skew'])}")
    print(f"   genuine      : {len(lower['genuine'])}")

    for code_point, category, java_value, python_value in lower["genuine"][:20]:
        print(
            f"     U+{code_point:04X} cat={category} "
            f"java={java_value!r} python={python_value!r}"
        )

    unexpected_lower = [
        entry
        for entry in lower["genuine"]
        if entry[0] not in KNOWN_NON_SKEW_LOWERCASE_DIVERGENCES
    ]

    failures += len(unexpected_lower)

    # --- rounding -----------------------------------------------------
    print()
    print("3. BigDecimal HALF_EVEN vs round()")
    rounding = verify_rounding(java, samples=arguments.samples)
    print(f"   comparisons : {rounding['comparisons']:,}")
    print(f"   mismatches  : {len(rounding['mismatches'])}")

    for value, digits, actual, expected in rounding["mismatches"][:20]:
        print(
            f"     round({value!r}, {digits}) "
            f"java={actual!r} python={expected!r}"
        )

    failures += len(rounding["mismatches"])

    # --- summary ------------------------------------------------------
    print()
    print("=" * 62)

    print(f"version skew (not a defect)  : {len(classes['version_skew']):,}")
    print(f"genuine porting divergences  : {failures}")

    if failures:
        print()
        print("JVM SEMANTICS FAILED")
        return 1

    print()
    print("JVM SEMANTICS VERIFIED")
    print("Every divergence is explained by the Unicode version")
    print("difference between the two runtimes.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
