"""
Tokenizers.

Two tokenizers are deliberately distinct:

    tokenize           -> document content (drops single characters)
    tokenize_filename  -> filenames and queries (keeps numeric tokens)

Keeping numeric tokens such as 1, 2 and 3 reachable is what makes queries
like "999" or "Module 2" work.
"""


def tokenize(text):
    """Tokenize normal document content."""

    text = text.lower()

    cleaned_text = "".join(
        character
        if character.isalnum() or character.isspace()
        else " "
        for character in text
    )

    return [
        word
        for word in cleaned_text.split()
        if len(word) > 1
    ]


def tokenize_filename(text):
    """
    Tokenize filenames and search queries while preserving
    numeric tokens such as 1, 2, and 3.
    """

    text = text.lower()

    cleaned_text = "".join(
        character
        if character.isalnum() or character.isspace()
        else " "
        for character in text
    )

    return [
        word
        for word in cleaned_text.split()
        if word
    ]
