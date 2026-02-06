"""Tests for output formatting utilities."""

import pytest

from ..output_utils import format_output, format_commit_log_as_summary


class TestFormatOutput:
    """Tests for format_output function."""

    def test_returns_valid_output(self):
        """Test that valid output is returned unchanged."""
        result = format_output("This is valid output")
        assert result == "This is valid output"

    def test_strips_whitespace(self):
        """Test that whitespace is stripped from output."""
        result = format_output("  valid output  \n")
        assert result == "valid output"

    def test_empty_string_uses_fallback(self):
        """Test that empty string triggers fallback."""
        result = format_output("", fallback_generator=lambda: "fallback content")
        assert result == "fallback content"

    def test_whitespace_only_uses_fallback(self):
        """Test that whitespace-only string triggers fallback."""
        result = format_output("   \n\t  ", fallback_generator=lambda: "fallback content")
        assert result == "fallback content"

    def test_no_output_sentinel_uses_fallback(self):
        """Test that 'No output' string triggers fallback."""
        result = format_output("No output", fallback_generator=lambda: "fallback content")
        assert result == "fallback content"

    def test_none_uses_fallback(self):
        """Test that None triggers fallback."""
        result = format_output(None, fallback_generator=lambda: "fallback content")
        assert result == "fallback content"

    def test_fallback_not_called_when_output_valid(self):
        """Test that fallback is not called when output is valid."""
        called = []

        def fallback():
            called.append(True)
            return "fallback"

        result = format_output("valid output", fallback_generator=fallback)
        assert result == "valid output"
        assert not called

    def test_default_message_when_no_fallback(self):
        """Test default message when output is empty and no fallback provided."""
        result = format_output("", context="summary")
        assert "No summary available" in result
        assert "diff" in result.lower()

    def test_default_message_uses_context(self):
        """Test that default message includes the context."""
        result = format_output("", context="review")
        assert "review" in result

    def test_fallback_exception_handled(self):
        """Test that exceptions in fallback are handled gracefully."""

        def bad_fallback():
            raise ValueError("Fallback failed")

        result = format_output("", fallback_generator=bad_fallback, context="test")
        assert "No test available" in result

    def test_empty_fallback_uses_default(self):
        """Test that empty fallback result uses default message."""
        result = format_output("", fallback_generator=lambda: "", context="output")
        assert "No output available" in result

    def test_whitespace_fallback_uses_default(self):
        """Test that whitespace-only fallback result uses default message."""
        result = format_output("", fallback_generator=lambda: "   ", context="output")
        assert "No output available" in result


class TestFormatCommitLogAsSummary:
    """Tests for format_commit_log_as_summary function."""

    def test_formats_single_commit(self):
        """Test formatting a single commit."""
        result = format_commit_log_as_summary("fix: resolve bug")
        assert result == "Commits:\n- fix: resolve bug"

    def test_formats_multiple_commits(self):
        """Test formatting multiple commits."""
        commit_log = "feat: add feature\nfix: resolve bug\ntest: add tests"
        result = format_commit_log_as_summary(commit_log)
        assert result == "Commits:\n- feat: add feature\n- fix: resolve bug\n- test: add tests"

    def test_handles_empty_string(self):
        """Test that empty string returns empty string."""
        result = format_commit_log_as_summary("")
        assert result == ""

    def test_handles_whitespace_only(self):
        """Test that whitespace-only string returns empty string."""
        result = format_commit_log_as_summary("   \n\t  ")
        assert result == ""

    def test_handles_none(self):
        """Test that None returns empty string."""
        result = format_commit_log_as_summary(None)
        assert result == ""

    def test_strips_empty_lines(self):
        """Test that empty lines are stripped."""
        commit_log = "feat: add feature\n\nfix: resolve bug\n"
        result = format_commit_log_as_summary(commit_log)
        assert result == "Commits:\n- feat: add feature\n- fix: resolve bug"

    def test_strips_whitespace_from_lines(self):
        """Test that whitespace is stripped from each line."""
        commit_log = "  feat: add feature  \n  fix: resolve bug  "
        result = format_commit_log_as_summary(commit_log)
        assert result == "Commits:\n- feat: add feature\n- fix: resolve bug"
