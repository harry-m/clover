"""Utilities for formatting Claude output for user-facing display."""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Sentinel value used internally when Claude produces no output
# This should never appear in user-facing content
NO_OUTPUT_SENTINEL = ""


def format_output(
    output: str,
    fallback_generator: Optional[Callable[[], str]] = None,
    context: str = "output",
    work_type: Optional[str] = None,
    number: Optional[int] = None,
) -> str:
    """Format Claude output for user-facing display.

    This function ensures that user-facing GitHub comments never contain
    unhelpful content like "No output" or empty strings. It applies
    context-appropriate fallbacks when the primary output is missing.

    Args:
        output: Raw output from ClaudeResult.
        fallback_generator: Optional callable to generate fallback content
            (e.g., commit log, diff stats). Called only if output is empty.
        context: Human-readable description of what this output represents
            (e.g., "summary", "review", "changes"). Used in logging and
            the default fallback message.
        work_type: Optional work type for logging (e.g., "issue", "pr_review").
        number: Optional issue/PR number for logging.

    Returns:
        Non-empty, user-appropriate string. Never returns empty string,
        "No output", or whitespace-only content.
    """
    # Clean up the output
    cleaned = output.strip() if output else ""

    # Check if output is usable
    if cleaned and cleaned != "No output":
        return cleaned

    # Log the empty output for debugging
    item_desc = f"{work_type} #{number}" if work_type and number else "unknown item"
    logger.warning(f"Empty Claude output for {item_desc} ({context})")

    # Try the fallback generator
    if fallback_generator:
        try:
            fallback = fallback_generator()
            if fallback and fallback.strip():
                logger.info(f"Using fallback for {item_desc} ({context})")
                return fallback.strip()
        except Exception as e:
            logger.warning(f"Fallback generator failed for {item_desc}: {e}")

    # Return a user-friendly default message
    return f"*No {context} available. Please review the diff for details.*"


def format_commit_log_as_summary(commit_log: str) -> str:
    """Format a git commit log as a bullet-point summary.

    Args:
        commit_log: Newline-separated commit subjects from git log.

    Returns:
        Formatted summary with bullet points, or empty string if no commits.
    """
    if not commit_log or not commit_log.strip():
        return ""

    lines = [line.strip() for line in commit_log.strip().splitlines() if line.strip()]
    if not lines:
        return ""

    return "Commits:\n" + "\n".join(f"- {line}" for line in lines)
