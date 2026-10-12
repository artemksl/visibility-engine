"""Provide exact name matching against a recorded response, in order."""

from .check_chat_names import MentionExtractionError, find_name_positions

__all__ = ["MentionExtractionError", "find_name_positions"]
