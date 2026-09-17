"""
Executable model of the Kotlin parity ports.

The Kotlin sources under ``android/harness`` cannot be compiled in this
environment: there is no JDK, no Android SDK and no Gradle, and the
network blocks every host that could supply them.

That does not mean the port's *algorithms* have to go unverified. This
module is a line-by-line Python model of the Kotlin logic in

    android/harness/src/main/kotlin/com/vtu/search/parity/PythonText.kt
    android/harness/src/main/kotlin/com/vtu/search/parity/SecureFilename.kt

using Java's `Character` predicate semantics rather than Python's, so
that `tests/test_port_model.py` can prove the algorithms reproduce every
golden contract vector.

If this model passes and the Kotlin is a faithful transcription of it,
the algorithm is correct. What remains unverified is only the Kotlin
syntax and the Java standard library behaviour, not the design.

Java predicate semantics are modelled from the Unicode general category:

    Character.isLetter(c)     category starts with "L"
    Character.isDigit(c)      category == "Nd"
    Character.isSpaceChar(c)  category in {"Zs", "Zl", "Zp"}
    Character.isWhitespace(c) category in {"Zs","Zl","Zp"} except the
                              non-breaking spaces, plus the ASCII control
                              whitespace

WARNING: this model uses Python's Unicode tables
-----------------------------------------------
Deriving Java's categories from `unicodedata` is circular: it can only
confirm that the mapping is self-consistent, not that it matches a real
JVM. An earlier version of this module claimed "zero mismatches across
all 1,114,112 code points" on that basis, and a real JVM disproved part
of it.

`tools/verify_jvm_semantics.py` is the authority. It executes the real
`java.lang.Character` methods and found 9,392 differences, every one of
them a code point unassigned in Python's Unicode 14.0.0 but assigned in
the JVM's Unicode 16. That is version skew, not a porting defect, and it
is measured rather than assumed away.

This model remains useful for two things:

  * it pins the *predicate composition*, which the JVM verification
    confirms is correct, and
  * it lets the contract vectors be replayed without a JVM.

It must not be treated as evidence about Java's Unicode tables.
"""

import json
import math
import os
import pathlib
import re
import unicodedata
from urllib.parse import quote

NON_BREAKING_SPACES = (0x00A0, 0x2007, 0x202F)

ASCII_CONTROL_WHITESPACE = (0x09, 0x0A, 0x0B, 0x0C, 0x0D)

PYTHON_NEXT_LINE = 0x0085


def general_category(code_point):
    return unicodedata.category(chr(code_point))


def java_is_letter(code_point):
    return general_category(code_point).startswith("L")


def java_is_digit(code_point):
    return general_category(code_point) == "Nd"


def java_is_space_char(code_point):
    return general_category(code_point) in ("Zs", "Zl", "Zp")


def java_is_whitespace(code_point):
    category = general_category(code_point)

    if category in ("Zs", "Zl", "Zp") and code_point not in NON_BREAKING_SPACES:
        return True

    if code_point in ASCII_CONTROL_WHITESPACE:
        return True

    return 0x1C <= code_point <= 0x1F


def is_python_alnum(code_point):
    """Model of PythonText.isPythonAlnum."""

    return (
        java_is_letter(code_point)
        or java_is_digit(code_point)
        or general_category(code_point) in ("Nl", "No")
    )


def is_python_space(code_point):
    """Model of PythonText.isPythonSpace."""

    return (
        java_is_whitespace(code_point)
        or java_is_space_char(code_point)
        or code_point == PYTHON_NEXT_LINE
    )


def is_python_digit(code_point):
    """Model of PythonText.isPythonDigit."""

    if java_is_digit(code_point):
        return True

    if general_category(code_point) != "No":
        return False

    return (
        (0x00B2 <= code_point <= 0x00B3)
        or code_point == 0x00B9
        or (0x1369 <= code_point <= 0x1371)
        or code_point == 0x19DA
        or code_point == 0x2070
        or (0x2074 <= code_point <= 0x2079)
        or (0x2080 <= code_point <= 0x2089)
        or (0x2460 <= code_point <= 0x2468)
        or (0x2474 <= code_point <= 0x247C)
        or (0x2488 <= code_point <= 0x2490)
        or code_point == 0x24EA
        or (0x24F5 <= code_point <= 0x24FD)
        or code_point == 0x24FF
        or (0x2776 <= code_point <= 0x277E)
        or (0x2780 <= code_point <= 0x2788)
        or (0x278A <= code_point <= 0x2792)
        or (0x10A40 <= code_point <= 0x10A43)
        or (0x10E60 <= code_point <= 0x10E68)
        or (0x11052 <= code_point <= 0x1105A)
        or (0x1F100 <= code_point <= 0x1F10A)
    )


def is_python_digit_string(text):
    """True if text is non-empty and all characters satisfy is_python_digit."""

    return bool(text) and all(is_python_digit(ord(c)) for c in text)


def clean(text):
    """Lowercase the whole string, then neutralise disallowed characters."""

    lowered = text.lower()

    return "".join(
        character
        if is_python_alnum(ord(character)) or is_python_space(ord(character))
        else " "
        for character in lowered
    )


def split_python_whitespace(text):
    """Model of PythonText.splitPythonWhitespace."""

    tokens = []
    current = []

    for character in text:
        if is_python_space(ord(character)):
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(character)

    if current:
        tokens.append("".join(current))

    return tokens


def tokenize(text):
    """Model of PythonText.tokenize, counting characters not UTF-16 units."""

    return [
        token
        for token in split_python_whitespace(clean(text))
        if len(token) > 1
    ]


def tokenize_filename(text):
    """Model of PythonText.tokenizeFilename."""

    return split_python_whitespace(clean(text))


# ----------------------------------------------------------------------
# SecureFilename
# ----------------------------------------------------------------------

ALLOWED_ASCII = (
    set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    | set("abcdefghijklmnopqrstuvwxyz")
    | set("0123456789")
    | {"_", ".", "-"}
)

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".txt")


def to_ascii(value):
    """NFKD-normalise and keep only ASCII characters."""

    normalised = unicodedata.normalize("NFKD", value)

    return "".join(
        character
        for character in normalised
        if 1 <= ord(character) <= 0x7F
    )


def basename(filename):
    """
    Model of SecureFilename.basename, i.e. `os.path.basename` on POSIX.

    Only `/` separates. A backslash is an ordinary character here, not a
    directory separator: `os.path.altsep` is `None` on POSIX, so
    `os.path.basename("dir\\sub\\file.txt")` returns the whole string.
    It is then `secure_filename` that deletes the backslash, giving
    `dirsubfile.txt` for that input.

    This used to split on `\\` as well, on the reasonable-sounding
    grounds that "an upload may arrive from any client". That made the
    model disagree with the engine for every Windows-shaped name, so the
    Kotlin port would have stored `file.txt` where the server stores
    `dirsubfile.txt`. The single recorded backslash vector,
    `..\\..\\x.txt`, returns `x.txt` either way, so the vectors could
    not see it. `tests/test_port_model_matches_engine.py` compares the
    model to the engine directly for that reason.
    """

    end = len(filename)

    while end > 0:
        if filename[end - 1] == "/":
            break
        end -= 1

    return filename[end:]


def secure_filename(filename):
    """Model of SecureFilename.secureFilename."""

    if not filename:
        return ""

    ascii_only = to_ascii(filename).replace("/", " ")

    joined = "_".join(split_python_whitespace(ascii_only))

    filtered = "".join(
        character
        for character in joined
        if character in ALLOWED_ASCII
    )

    return filtered.strip("._")


def has_supported_extension(filename):
    lowered = filename.lower()
    return any(lowered.endswith(ext) for ext in SUPPORTED_EXTENSIONS)


def sanitize(filename):
    """Model of SecureFilename.sanitize."""

    if not filename:
        return ""

    safe_name = secure_filename(basename(filename))

    if not safe_name:
        return ""

    if not has_supported_extension(safe_name):
        return ""

    return safe_name


# ----------------------------------------------------------------------
# Rounding
# ----------------------------------------------------------------------

def round_half_even(value, digits):
    """Model of PythonRound.round."""
    return round(value, digits)


# ----------------------------------------------------------------------
# Query Normalization & Filetype Parsing
# ----------------------------------------------------------------------

def normalize_search_query(query):
    """Model of PythonText.normalizeSearchQuery."""
    query = query.strip()
    if not query:
        return ""

    quote_wrapped = (
        len(query) >= 2
        and query.startswith('"')
        and query.endswith('"')
    )
    if quote_wrapped:
        query = query[1:-1].strip()

    lower_query = query.lower()
    supported_extensions = (".pdf", ".docx", ".txt")

    for extension in supported_extensions:
        if lower_query.endswith(extension):
            query = query[:-len(extension)].rstrip()
            break

    if quote_wrapped:
        return f'"{query}"'
    return query


def parse_filetype_filter(query):
    """Model of PythonText.parseFiletypeFilter."""
    query = query.strip()
    supported_extensions = (
        (".pdf", "pdf"),
        (".docx", "docx"),
        (".txt", "txt"),
    )

    for extension, filetype in supported_extensions:
        quoted_suffix = f'{extension}"'
        if query.lower().endswith(quoted_suffix):
            remaining = query[:-len(quoted_suffix)].rstrip()
            return (remaining + '"' if remaining else "", filetype)
        if query.lower().endswith(extension):
            return (query[:-len(extension)].rstrip(), filetype)

    tokens = split_python_whitespace(query)
    if not tokens:
        return query, None

    supported_types = {
        "pdf": "pdf",
        ".pdf": "pdf",
        "docx": "docx",
        ".docx": "docx",
        "txt": "txt",
        ".txt": "txt",
    }

    detected_type = None
    remaining_tokens = []
    for token in tokens:
        normalized = token.lower().strip()
        if normalized in supported_types:
            if detected_type is None:
                detected_type = supported_types[normalized]
            continue
        remaining_tokens.append(token)

    return (" ".join(remaining_tokens).strip(), detected_type)


# ----------------------------------------------------------------------
# Snippet & Highlight Generation
# ----------------------------------------------------------------------

def count_phrase_occurrences(text, phrase):
    """Model of SnippetEngine.countPhraseOccurrences."""
    if not text or not phrase:
        return 0

    normalized_text = " ".join(split_python_whitespace(text.lower()))
    normalized_phrase = " ".join(split_python_whitespace(phrase.lower()))

    if not normalized_phrase:
        return 0

    count = 0
    start = 0

    while True:
        position = normalized_text.find(normalized_phrase, start)
        if position == -1:
            break
        count += 1
        start = position + len(normalized_phrase)

    return count


def build_snippet_result(
    text,
    page_number,
    position,
    match_length,
    window,
    highlight_query,
):
    """Model of SnippetEngine.buildSnippetResult."""
    start = max(0, position - window)
    end = min(len(text), position + match_length + window)

    snippet = text[start:end].strip()

    if start > 0:
        first_space = snippet.find(" ")
        if first_space != -1:
            snippet = snippet[first_space + 1:]
        snippet = "... " + snippet

    if end < len(text):
        last_space = snippet.rfind(" ")
        if last_space != -1:
            snippet = snippet[:last_space]
        snippet += " ..."

    highlights = []
    lower_snippet = snippet.lower()
    query = " ".join(split_python_whitespace(highlight_query.lower()))

    search_position = 0
    while True:
        found = lower_snippet.find(query, search_position)
        if found == -1:
            break
        highlights.append({
            "start": found,
            "end": found + len(query),
        })
        search_position = found + len(query)

    if not highlights and query:
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        for match in pattern.finditer(lower_snippet):
            highlights.append({
                "start": match.start(),
                "end": match.end(),
            })

    highlights.sort(key=lambda item: item["start"])

    merged = []
    for item in highlights:
        if not merged:
            merged.append(item)
            continue
        previous = merged[-1]
        if item["start"] <= previous["end"]:
            previous["end"] = max(previous["end"], item["end"])
        else:
            merged.append(item)

    return {
        "snippet": snippet,
        "page": page_number,
        "highlights": merged,
    }


def get_snippet_and_page(
    page_text_index,
    filename,
    query_words,
    window=140,
):
    """Model of SnippetEngine.getSnippetAndPage."""
    pages = page_text_index.get(filename, [])
    if not pages:
        return {
            "snippet": "No readable text found.",
            "page": None,
            "highlights": [],
        }

    cleaned_words = [
        word.lower().strip()
        for word in query_words
        if word
    ]
    cleaned_words = list(dict.fromkeys(cleaned_words))

    if not cleaned_words:
        return {
            "snippet": "No matching text found.",
            "page": None,
            "highlights": [],
        }

    phrase = " ".join(cleaned_words)

    # 1. EXACT PHRASE
    if len(cleaned_words) >= 2:
        for page_data in pages:
            page_number = page_data["page"]
            original_text = " ".join(split_python_whitespace(page_data["text"]))
            lower_text = original_text.lower()
            phrase_position = lower_text.find(phrase)

            if phrase_position != -1:
                return build_snippet_result(
                    original_text,
                    page_number,
                    phrase_position,
                    len(phrase),
                    window,
                    phrase,
                )

    # 2. EXACT TOKEN
    exact_candidates = []
    for page_data in pages:
        page_number = page_data["page"]
        original_text = " ".join(split_python_whitespace(page_data["text"]))
        lower_text = original_text.lower()

        for word in cleaned_words:
            pattern = (
                r"(?<![a-z0-9])"
                + re.escape(word)
                + r"(?![a-z0-9])"
            )
            match = re.search(pattern, lower_text)
            if match:
                exact_candidates.append({
                    "page": page_number,
                    "position": match.start(),
                    "length": len(word),
                    "text": original_text,
                    "word": word,
                    "score": 100000,
                })

    if exact_candidates:
        best = exact_candidates[0]
        return build_snippet_result(
            best["text"],
            best["page"],
            best["position"],
            best["length"],
            window,
            best["word"],
        )

    # 3. PREFIX MATCH
    prefix_candidates = []
    for page_data in pages:
        page_number = page_data["page"]
        original_text = " ".join(split_python_whitespace(page_data["text"]))
        lower_text = original_text.lower()

        for word in cleaned_words:
            if len(word) < 3:
                continue

            pattern = (
                r"(?<![a-z0-9])"
                + re.escape(word)
                + r"[a-z0-9]+"
            )
            match = re.search(pattern, lower_text)
            if match:
                matched_text = match.group(0)
                prefix_similarity = len(word) / float(max(len(matched_text), 1))
                prefix_candidates.append({
                    "page": page_number,
                    "position": match.start(),
                    "length": len(matched_text),
                    "text": original_text,
                    "word": word,
                    "score": 50000 + prefix_similarity,
                })

    if prefix_candidates:
        best = max(prefix_candidates, key=lambda item: item["score"])
        return build_snippet_result(
            best["text"],
            best["page"],
            best["position"],
            best["length"],
            window,
            best["word"],
        )

    # 4. PROXIMITY FALLBACK
    best = None
    best_score = -1

    for page_data in pages:
        page_number = page_data["page"]
        original_text = " ".join(split_python_whitespace(page_data["text"]))
        lower_text = original_text.lower()
        if not lower_text:
            continue

        candidates = []
        for word in cleaned_words:
            search_position = 0
            while True:
                position = lower_text.find(word, search_position)
                if position == -1:
                    break
                candidates.append((position, word))
                search_position = position + len(word)

        for position, matched_word in candidates:
            local_start = max(0, position - window)
            local_end = min(len(original_text), position + window)
            local_text = lower_text[local_start:local_end]

            nearby_terms = sum(1 for word in cleaned_words if word in local_text)
            candidate_score = nearby_terms * 1000 + len(matched_word)

            if candidate_score > best_score:
                best_score = candidate_score
                best = {
                    "page": page_number,
                    "position": position,
                    "length": len(matched_word),
                    "text": original_text,
                    "score": candidate_score,
                }

    if best is None:
        return {
            "snippet": "No matching text found.",
            "page": None,
            "highlights": [],
        }

    return build_snippet_result(
        best["text"],
        best["page"],
        best["position"],
        best["length"],
        window,
        phrase,
    )


# ----------------------------------------------------------------------
# Ranking Engine
# ----------------------------------------------------------------------

def build_paginated_response(results, page, limit):
    """Model of RankingEngine.buildPaginatedResponse."""
    total = len(results)
    total_pages = (total + limit - 1) // limit if total > 0 else 0

    if total_pages > 0:
        page = max(1, min(page, total_pages))
    else:
        page = max(1, page)

    start_index = (page - 1) * limit if total_pages > 0 else 0
    end_index = start_index + limit
    page_results = results[start_index:end_index]

    return {
        "results": page_results,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_previous": page > 1 and total_pages > 0,
            "start": (start_index + 1 if page_results else 0),
            "end": (start_index + len(page_results) if page_results else 0),
        },
    }


def search_index(
    raw_query_text,
    page_text,
    limit_text,
    active_inverted_index,
    active_document_metadata,
    active_filename_index,
    active_page_text_index,
):
    """
    Model of the ranking pipeline in Kotlin.
    Uses is_python_digit_string, round_half_even, and Java-safe semantics.
    """
    raw_query = raw_query_text.strip()

    if not raw_query:
        return {
            "results": [],
            "pagination": {
                "page": 1,
                "limit": 10,
                "total": 0,
                "total_pages": 0,
                "has_next": False,
                "has_previous": False,
            },
        }

    try:
        requested_page = int(page_text)
    except (TypeError, ValueError):
        requested_page = 1

    try:
        requested_limit = int(limit_text)
    except (TypeError, ValueError):
        requested_limit = 10

    requested_result_page = max(1, requested_page)
    limit = min(50, max(1, requested_limit))

    keyword_query, filetype_filter = parse_filetype_filter(raw_query)
    normalized_query_text = normalize_search_query(keyword_query)
    query = normalized_query_text.lower()

    quoted_phrase = None
    if len(query) >= 2 and query.startswith('"') and query.endswith('"'):
        quoted_phrase = query[1:-1].strip()

    search_words = tokenize_filename(query)

    if not search_words and not filetype_filter:
        return build_paginated_response([], requested_result_page, limit)

    content_query_words = [word for word in search_words if len(word) > 1]

    # FILE-TYPE-ONLY QUERY
    if filetype_filter and not search_words:
        filtered_results = []
        for filename, metadata in active_document_metadata.items():
            extension = os.path.splitext(filename)[1].lower().lstrip(".")
            if extension != filetype_filter:
                continue

            document_url = "/api/documents/" + quote(filename, safe="")
            filtered_results.append({
                "title": metadata["title"],
                "path": metadata.get("path"),
                "document_url": document_url,
                "page_url": document_url,
                "open_url": document_url,
                "snippet": f"Filtered by file type: {filetype_filter.upper()}",
                "page": None,
                "highlights": [],
                "filename_score": 0.0,
                "content_score": 0.0,
                "phrase_score": 0.0,
                "phrase_occurrences": 0,
                "score": 1.0,
                "relevance_score": 1.0,
                "match_type": f"File Type: {filetype_filter.upper()}",
                "filetype_filter": filetype_filter,
                "tag": "Filtered Result",
            })

        filtered_results.sort(key=lambda item: item["title"].lower())
        return build_paginated_response(
            filtered_results, requested_result_page, limit
        )

    normalized_query = " ".join(search_words)

    if quoted_phrase:
        phrase_words = tokenize_filename(quoted_phrase)
    else:
        phrase_words = list(search_words)

    normalized_phrase = " ".join(phrase_words)
    phrase_query = len(phrase_words) >= 2
    if not search_words:
        phrase_query = False

    if filetype_filter:
        total_documents = sum(
            1
            for filename in active_document_metadata
            if os.path.splitext(filename)[1].lower().lstrip(".") == filetype_filter
        )
    else:
        total_documents = len(active_document_metadata)

    if total_documents == 0:
        return []

    document_scores = {}
    for filename in active_document_metadata:
        document_scores[filename] = {
            "filename_score": 0.0,
            "content_score": 0.0,
            "phrase_score": 0.0,
            "phrase_occurrences": 0,
            "filename_matches": [],
            "content_matches": [],
            "phrase_match": False,
            "exact_content_match": False,
            "best_prefix_similarity": 0.0,
            "best_numeric_similarity": 0.0,
        }

    def filetype_allowed(filename):
        if not filetype_filter:
            return True
        extension = os.path.splitext(filename)[1].lower().lstrip(".")
        return extension == filetype_filter

    # FILENAME SEARCH
    for filename, filename_words in active_filename_index.items():
        if not filetype_allowed(filename):
            continue

        normalized_filename = " ".join(filename_words)

        if normalized_query == normalized_filename:
            document_scores[filename]["filename_score"] += 100.0
        elif len(search_words) >= 2 and normalized_query in normalized_filename:
            document_scores[filename]["filename_score"] += 50.0

        if (
            quoted_phrase
            and normalized_phrase
            and normalized_phrase in normalized_filename
        ):
            document_scores[filename]["phrase_score"] += 30.0
            document_scores[filename]["phrase_match"] = True

        for word in search_words:
            if word not in filename_words:
                continue

            if is_python_digit_string(word):
                non_numeric_query_words = [
                    query_word
                    for query_word in search_words
                    if not is_python_digit_string(query_word)
                ]
                has_related_filename_word = any(
                    query_word in filename_words
                    for query_word in non_numeric_query_words
                )
                if non_numeric_query_words and not has_related_filename_word:
                    continue

            document_scores[filename]["filename_score"] += 20.0
            if word not in document_scores[filename]["filename_matches"]:
                document_scores[filename]["filename_matches"].append(word)

    # CONTENT TF-IDF
    indexed_terms = list(active_inverted_index.keys())

    for word in content_query_words:
        matching_terms = []

        if word in active_inverted_index:
            matching_terms.append((word, 1.0))

        if len(word) >= 3:
            for term in indexed_terms:
                if term == word or not term or not term.startswith(word):
                    continue
                prefix_similarity = len(word) / float(len(term))
                prefix_similarity = min(0.90, max(0.25, prefix_similarity))
                matching_terms.append((term, prefix_similarity))

        if is_python_digit_string(word) and len(word) >= 2:
            for term in indexed_terms:
                if term == word or word not in term or term.startswith(word):
                    continue
                substring_similarity = len(word) / float(len(term))
                substring_similarity = min(
                    0.65, max(0.20, substring_similarity * 0.70)
                )
                matching_terms.append((term, substring_similarity))

        best_term_weights = {}
        for term, match_weight in matching_terms:
            current_weight = best_term_weights.get(term, 0.0)
            if match_weight > current_weight:
                best_term_weights[term] = match_weight

        for term, match_weight in best_term_weights.items():
            docs_containing_word = len(active_inverted_index[term])
            if docs_containing_word == 0:
                continue

            idf = (
                math.log((total_documents + 1) / (docs_containing_word + 1))
                + 1
            )

            for filename, term_count in active_inverted_index[term].items():
                if not filetype_allowed(filename):
                    continue
                if filename not in active_document_metadata:
                    continue

                total_words = active_document_metadata[filename]["total_words"]
                if total_words == 0:
                    continue

                tf = term_count / float(total_words)
                tfidf = tf * idf * match_weight

                document_scores[filename]["content_score"] += tfidf

                if term == word:
                    document_scores[filename]["exact_content_match"] = True
                elif is_python_digit_string(word) and word in term:
                    document_scores[filename]["best_numeric_similarity"] = max(
                        document_scores[filename]["best_numeric_similarity"],
                        match_weight,
                    )
                elif term.startswith(word):
                    document_scores[filename]["best_prefix_similarity"] = max(
                        document_scores[filename]["best_prefix_similarity"],
                        match_weight,
                    )

                if word not in document_scores[filename]["content_matches"]:
                    document_scores[filename]["content_matches"].append(word)

    # PHRASE SEARCH
    if phrase_query:
        for filename in active_document_metadata:
            if not filetype_allowed(filename):
                continue

            pages = active_page_text_index.get(filename, [])
            normalized_page_text = " ".join(
                page["text"] for page in pages
            ).lower()

            phrase_occurrences = count_phrase_occurrences(
                normalized_page_text, normalized_phrase
            )

            if phrase_occurrences > 0:
                document_scores[filename]["phrase_occurrences"] = (
                    phrase_occurrences
                )

                max_frequency_reference = 25.0
                if phrase_occurrences <= 1:
                    content_phrase_score = 50.0
                else:
                    frequency_ratio = min(
                        math.log(phrase_occurrences)
                        / math.log(max_frequency_reference),
                        1.0,
                    )
                    content_phrase_score = 50.0 + (50.0 * frequency_ratio)

                content_phrase_score = min(content_phrase_score, 100.0)
                document_scores[filename]["phrase_score"] = max(
                    document_scores[filename]["phrase_score"],
                    content_phrase_score,
                )
                document_scores[filename]["phrase_match"] = True

            filename_words = active_filename_index.get(filename, [])
            normalized_filename = " ".join(filename_words)

            if normalized_phrase and normalized_phrase in normalized_filename:
                document_scores[filename]["phrase_score"] += 100.0
                document_scores[filename]["phrase_match"] = True

    # Quoted phrase filter
    if quoted_phrase:
        phrase_documents = {
            filename
            for filename, scores in document_scores.items()
            if scores["phrase_match"]
        }

        if phrase_documents:
            for filename in list(document_scores.keys()):
                if filename not in phrase_documents:
                    document_scores.pop(filename)
        else:
            return []

    max_content_score = max(
        (scores["content_score"] for scores in document_scores.values()),
        default=0.0,
    )

    query_has_filename_signal = any(
        scores["filename_score"] > 0 for scores in document_scores.values()
    )

    # FINAL RANKING
    if filetype_filter:
        document_scores = {
            filename: scores
            for filename, scores in document_scores.items()
            if filetype_allowed(filename)
        }
        if not document_scores:
            return []

        max_content_score = max(
            (scores["content_score"] for scores in document_scores.values()),
            default=0.0,
        )

    ranked_documents = []

    for filename, scores in document_scores.items():
        filename_score = scores["filename_score"]
        content_score = scores["content_score"]
        phrase_score = scores["phrase_score"]
        exact_content_match = bool(scores.get("exact_content_match", False))
        best_prefix_similarity = float(
            scores.get("best_prefix_similarity", 0.0)
        )
        best_numeric_similarity = float(
            scores.get("best_numeric_similarity", 0.0)
        )

        if exact_content_match:
            lexical_match_relevance = 1.00
        elif best_prefix_similarity > 0:
            lexical_match_relevance = 0.70 + (0.30 * best_prefix_similarity)
        elif best_numeric_similarity > 0:
            lexical_match_relevance = 0.30 + (0.40 * best_numeric_similarity)
        else:
            lexical_match_relevance = 0.0

        filename_words = active_filename_index.get(filename, [])
        normalized_filename = " ".join(filename_words)
        matched_word_count = sum(
            1 for word in search_words if word in filename_words
        )

        if search_words:
            query_word_coverage = matched_word_count / len(search_words)
        else:
            query_word_coverage = 0.0

        if normalized_query == normalized_filename:
            filename_relevance = 1.0
        elif len(search_words) >= 2 and normalized_query in normalized_filename:
            filename_relevance = 0.95
        elif query_word_coverage > 0:
            filename_relevance = 0.75 * query_word_coverage
        else:
            filename_relevance = 0.0

        if max_content_score > 0:
            content_relevance = content_score / max_content_score
        else:
            content_relevance = 0.0

        phrase_relevance = min(phrase_score / 100.0, 1.0)

        if filetype_filter and not search_words:
            final_score = 0.75
        else:
            if quoted_phrase and query_has_filename_signal:
                filename_weight = 0.80
                content_weight = 0.05
                phrase_weight = 0.15
                lexical_weight = 0.0
            elif quoted_phrase:
                filename_weight = 0.05
                content_weight = 0.15
                phrase_weight = 0.80
                lexical_weight = 0.0
            elif query_has_filename_signal:
                filename_weight = 0.75
                content_weight = 0.15
                phrase_weight = 0.10
                lexical_weight = 0.0
            else:
                filename_weight = 0.05
                content_weight = 0.60
                phrase_weight = 0.20
                lexical_weight = 0.15

            final_score = (
                (filename_relevance * filename_weight)
                + (content_relevance * content_weight)
                + (phrase_relevance * phrase_weight)
                + (lexical_match_relevance * lexical_weight)
            )

        if (
            filename_score == 0
            and content_score == 0
            and phrase_score == 0
        ):
            continue

        if final_score <= 0:
            continue

        ranked_documents.append({
            "filename": filename,
            "filename_score": filename_score,
            "content_score": content_score,
            "phrase_score": phrase_score,
            "phrase_occurrences": scores["phrase_occurrences"],
            "final_score": final_score,
            "filename_matches": scores["filename_matches"],
            "content_matches": scores["content_matches"],
            "phrase_match": scores["phrase_match"],
            "exact_content_match": exact_content_match,
            "prefix_similarity": best_prefix_similarity,
            "numeric_similarity": best_numeric_similarity,
            "lexical_match_relevance": lexical_match_relevance,
        })

    if filetype_filter and not search_words:
        ranked_documents.sort(key=lambda item: item["filename"].lower())
    else:
        ranked_documents.sort(
            key=lambda item: item["final_score"], reverse=True
        )

    results = []

    for item in ranked_documents:
        filename = item["filename"]
        metadata = active_document_metadata[filename]
        filename_matches = item["filename_matches"]
        content_matches = item["content_matches"]
        phrase_match = item["phrase_match"]

        if filetype_filter and not search_words:
            match_type = f"File Type: {filetype_filter.upper()}"
        elif filename_matches and phrase_match:
            match_type = "Filename + Phrase"
        elif phrase_match:
            match_type = "Phrase Match"
        elif filename_matches and content_matches:
            match_type = "Filename + Content"
        elif filename_matches:
            match_type = "Filename Match"
        else:
            match_type = "Content Match"

        if filetype_filter and not search_words:
            snippet = f"Filtered by file type: {filetype_filter.upper()}"
            document_page = None
            highlights = []
        elif content_matches:
            snippet_words = (
                phrase_words if phrase_match else content_matches
            )
            snippet_info = get_snippet_and_page(
                active_page_text_index, filename, snippet_words
            )
            snippet = snippet_info["snippet"]
            document_page = snippet_info["page"]
            highlights = snippet_info["highlights"]
        else:
            snippet = "Filename matched: " + ", ".join(filename_matches)
            document_page = None
            highlights = []

        document_url = "/api/documents/" + quote(filename, safe="")

        if document_page is not None:
            page_url = document_url + f"#page={document_page}"
        else:
            page_url = document_url

        results.append({
            "title": metadata["title"],
            "path": metadata.get("path"),
            "document_url": document_url,
            "page_url": page_url,
            "snippet": snippet,
            "page": document_page,
            "highlights": highlights,
            "phrase_occurrences": item["phrase_occurrences"],
            "filename_score": round_half_even(item["filename_score"], 4),
            "content_score": round_half_even(item["content_score"], 4),
            "exact_content_match": item.get("exact_content_match", False),
            "prefix_similarity": round_half_even(
                item.get("prefix_similarity", 0.0), 4
            ),
            "numeric_similarity": round_half_even(
                item.get("numeric_similarity", 0.0), 4
            ),
            "lexical_match_relevance": round_half_even(
                item.get("lexical_match_relevance", 0.0), 4
            ),
            "phrase_score": round_half_even(item["phrase_score"], 4),
            "score": round_half_even(item["final_score"], 4),
            "match_type": match_type,
            "filetype_filter": filetype_filter,
            "tag": "Ranked Result",
            "open_url": page_url,
        })

    return build_paginated_response(
        results, requested_result_page, limit
    )


def load_sidecar_indexes(sidecar_path=None):
    """
    Build index structures from the corpus sidecar.
    Maintains deterministic document ordering (sorted by source_name).
    """
    if sidecar_path is None:
        sidecar_path = (
            pathlib.Path(__file__).resolve().parent.parent
            / "artifacts"
            / "android"
            / "corpus_sidecar.json"
        )
    with open(sidecar_path, "r", encoding="utf-8") as handle:
        sidecar = json.load(handle)

    docs_sorted = sorted(sidecar["documents"], key=lambda d: d["source_name"])
    inverted_index = {}
    doc_metadata = {}
    filename_index = {}
    page_text_index = {}

    for doc in docs_sorted:
        name = doc["name"]
        doc_metadata[name] = {
            "title": doc["title"],
            "path": None,
            "total_words": doc["total_words"],
            "page_count": doc["page_count"],
        }
        filename_index[name] = doc["filename_terms"]
        page_text_index[name] = doc["pages"]
        for term, freq in doc["content_terms"].items():
            inverted_index.setdefault(term, {})[name] = freq

    return inverted_index, doc_metadata, filename_index, page_text_index

