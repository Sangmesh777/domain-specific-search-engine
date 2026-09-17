"""
Snippet, highlight and page selection.

Extracted verbatim from app.py by tools/extract_modules.py.
Do not retype these functions: the bodies must stay byte
identical to the engine that the golden vectors pin.
"""

import re
def build_snippet_result(
    text,
    page_number,
    position,
    match_length,
    window,
    highlight_query
):
    """
    Build the final snippet and highlight ranges around a chosen match.
    """

    start = max(
        0,
        position - window
    )

    end = min(
        len(text),
        position + match_length + window
    )

    snippet = text[
        start:end
    ].strip()

    if start > 0:
        first_space = snippet.find(" ")

        if first_space != -1:
            snippet = snippet[
                first_space + 1:
            ]

        snippet = "... " + snippet

    if end < len(text):
        last_space = snippet.rfind(" ")

        if last_space != -1:
            snippet = snippet[
                :last_space
            ]

        snippet += " ..."

    highlights = []

    lower_snippet = snippet.lower()
    query = " ".join(
        highlight_query.lower().split()
    )

    # Exact phrase/token first.
    search_position = 0

    while True:

        found = lower_snippet.find(
            query,
            search_position
        )

        if found == -1:
            break

        highlights.append({
            "start": found,
            "end": found + len(query)
        })

        search_position = (
            found + len(query)
        )

    # If the exact query is not present in the snippet because the selected
    # match is a longer prefix token, highlight the query prefix.
    if not highlights and query:

        pattern = re.compile(
            re.escape(query),
            re.IGNORECASE
        )

        for match in pattern.finditer(
            lower_snippet
        ):

            highlights.append({
                "start": match.start(),
                "end": match.end()
            })

    # Merge overlapping/touching ranges.
    highlights.sort(
        key=lambda item: item["start"]
    )

    merged = []

    for item in highlights:

        if not merged:

            merged.append(item)
            continue

        previous = merged[-1]

        if item["start"] <= previous["end"]:

            previous["end"] = max(
                previous["end"],
                item["end"]
            )

        else:

            merged.append(item)

    return {
        "snippet": snippet,
        "page": page_number,
        "highlights": merged
    }


def get_snippet_and_page(
    page_text_index,
    filename,
    query_words,
    window=140
):
    """
    Return the best matching page/snippet.

    Match priority:
    1. Exact multi-word phrase.
    2. Exact token occurrence.
    3. Prefix occurrence.
    4. Nearby/proximity fallback.

    Highlights are relative to the returned snippet.
    """

    pages = page_text_index.get(
        filename,
        []
    )

    if not pages:
        return {
            "snippet": "No readable text found.",
            "page": None,
            "highlights": []
        }

    cleaned_words = [
        word.lower().strip()
        for word in query_words
        if word
    ]

    cleaned_words = list(
        dict.fromkeys(cleaned_words)
    )

    if not cleaned_words:
        return {
            "snippet": "No matching text found.",
            "page": None,
            "highlights": []
        }

    phrase = " ".join(cleaned_words)

    # --------------------------------------------------------
    # 1. EXACT PHRASE
    # --------------------------------------------------------
    if len(cleaned_words) >= 2:

        for page_data in pages:

            page_number = page_data["page"]

            original_text = " ".join(
                page_data["text"].split()
            )

            lower_text = original_text.lower()

            phrase_position = lower_text.find(
                phrase
            )

            if phrase_position != -1:

                return build_snippet_result(
                    original_text,
                    page_number,
                    phrase_position,
                    len(phrase),
                    window,
                    phrase
                )

    # --------------------------------------------------------
    # 2. EXACT TOKEN
    # --------------------------------------------------------
    exact_candidates = []

    for page_data in pages:

        page_number = page_data["page"]

        original_text = " ".join(
            page_data["text"].split()
        )

        lower_text = original_text.lower()

        for word in cleaned_words:

            # Token boundary-aware exact matching.
            pattern = (
                r"(?<![a-z0-9])"
                +
                re.escape(word)
                +
                r"(?![a-z0-9])"
            )

            match = re.search(
                pattern,
                lower_text
            )

            if match:

                exact_candidates.append({
                    "page": page_number,
                    "position": match.start(),
                    "length": len(word),
                    "text": original_text,
                    "word": word,
                    "score": 100000
                })

    if exact_candidates:

        # Prefer the first exact candidate in the earliest page only after
        # exactness has been established.
        best = exact_candidates[0]

        return build_snippet_result(
            best["text"],
            best["page"],
            best["position"],
            best["length"],
            window,
            best["word"]
        )

    # --------------------------------------------------------
    # 3. PREFIX MATCH
    # --------------------------------------------------------
    prefix_candidates = []

    for page_data in pages:

        page_number = page_data["page"]

        original_text = " ".join(
            page_data["text"].split()
        )

        lower_text = original_text.lower()

        for word in cleaned_words:

            if len(word) < 3:
                continue

            pattern = (
                r"(?<![a-z0-9])"
                +
                re.escape(word)
                +
                r"[a-z0-9]+"
            )

            match = re.search(
                pattern,
                lower_text
            )

            if match:

                matched_text = match.group(0)

                prefix_similarity = (
                    len(word)
                    /
                    float(
                        max(
                            len(matched_text),
                            1
                        )
                    )
                )

                prefix_candidates.append({
                    "page": page_number,
                    "position": match.start(),
                    "length": len(matched_text),
                    "text": original_text,
                    "word": word,
                    "score":
                        50000
                        +
                        prefix_similarity
                })

    if prefix_candidates:

        best = max(
            prefix_candidates,
            key=lambda item: item["score"]
        )

        return build_snippet_result(
            best["text"],
            best["page"],
            best["position"],
            best["length"],
            window,
            best["word"]
        )

    # --------------------------------------------------------
    # 4. PROXIMITY FALLBACK
    # --------------------------------------------------------
    best = None
    best_score = -1

    for page_data in pages:

        page_number = page_data["page"]

        original_text = " ".join(
            page_data["text"].split()
        )

        lower_text = original_text.lower()

        if not lower_text:
            continue

        candidates = []

        for word in cleaned_words:

            search_position = 0

            while True:

                position = lower_text.find(
                    word,
                    search_position
                )

                if position == -1:
                    break

                candidates.append(
                    (
                        position,
                        word
                    )
                )

                search_position = (
                    position + len(word)
                )

        for (
            position,
            matched_word
        ) in candidates:

            local_start = max(
                0,
                position - window
            )

            local_end = min(
                len(original_text),
                position + window
            )

            local_text = lower_text[
                local_start:local_end
            ]

            nearby_terms = sum(
                1
                for word
                in cleaned_words
                if word in local_text
            )

            candidate_score = (
                nearby_terms * 1000
                +
                len(matched_word)
            )

            if candidate_score > best_score:

                best_score = candidate_score

                best = {
                    "page": page_number,
                    "position": position,
                    "length": len(matched_word),
                    "text": original_text,
                    "score": candidate_score
                }

    if best is None:

        return {
            "snippet": "No matching text found.",
            "page": None,
            "highlights": []
        }

    return build_snippet_result(
        best["text"],
        best["page"],
        best["position"],
        best["length"],
        window,
        phrase
    )
