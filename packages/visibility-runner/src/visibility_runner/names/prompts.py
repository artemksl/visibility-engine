"""Versioned name listing prompts."""

import json

from .check_chat_names import (
    MENTION_EXTRACTION_PROMPT,
    MENTION_EXTRACTION_PROMPT_VERSION,
    MentionExtractionError,
)
from .parse_source_answer import source_parser_prompt, text_sha256


# These exact historical bytes remain usable only when a durable turn already
# bound their hash. New turns extract from the accepted captured source text.
_LEGACY_MENTION_EXTRACTION_PROMPTS = (
    "Return only the clinic/provider names you mentioned in your immediately "
    "preceding answer, in the same order. Preserve the exact spelling from that "
    "answer. Do not add explanations, classifications, offsets, or names you did "
    "not mention. Return a closed JSON array of strings; return [] if none. "
    "Do not wrap the array in Markdown or a code fence.",
    "Return only the clinic/provider names you mentioned in your immediately "
    "preceding answer, in the same order. Copy each name as one uninterrupted "
    "verbatim text span from that answer, preserving spelling, accents, "
    "punctuation and spacing. Do not join a clinic name with an adjacent "
    "citation, source label, link title or other text. Preserve repeated name "
    "occurrences as separate entries. Do not add explanations, classifications, "
    "offsets, or names you did "
    "not mention. Return a closed JSON array of strings; return [] if none. "
    "Do not wrap the array in Markdown or a code fence.",
)


def same_chat_extraction_prompt(
    source: str, *, platform: str | None = None, expected_prompt_sha256: str | None = None,
) -> str:
    """Use captured source text, or reproduce an already fenced turn."""
    if expected_prompt_sha256 is not None:
        for historical in _LEGACY_MENTION_EXTRACTION_PROMPTS:
            if text_sha256(historical) == expected_prompt_sha256:
                return historical
    prompt = source_parser_prompt(source)
    if expected_prompt_sha256 is not None and text_sha256(prompt) == expected_prompt_sha256:
        return prompt
    if platform == "copilot" or expected_prompt_sha256 is not None:
        prompt = (
            "Copy the clinic/provider business names from the source text below. "
            "Preserve exact spelling, first-to-last occurrence order and every repeated occurrence. "
            "Return only a closed JSON array of nonempty strings, or [] if there are no names. "
            "Do no research and add no names absent from the source text. "
            "Do not include explanations or Markdown.\n\nSource text:\n"
            + json.dumps(source, ensure_ascii=False)
        )
    if expected_prompt_sha256 is not None and text_sha256(prompt) != expected_prompt_sha256:
        raise MentionExtractionError("same_chat_extraction_prompt_mismatch")
    return prompt


__all__ = [
    "MENTION_EXTRACTION_PROMPT",
    "MENTION_EXTRACTION_PROMPT_VERSION",
    "same_chat_extraction_prompt",
]
