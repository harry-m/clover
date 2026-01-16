"""Tests for Claude runner."""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import json

from ..config import Config
from ..claude_runner import ClaudeRunner, ClaudeResult, ClaudeRunnerError


@pytest.fixture
def config():
    """Create a test config."""
    return Config(
        github_token="ghp_test123",
        github_repo="owner/repo",
        max_turns=50,
    )


@pytest.fixture
def runner(config):
    """Create a test runner."""
    return ClaudeRunner(config)


class TestClaudeResult:
    """Tests for ClaudeResult dataclass."""

    def test_create(self):
        """Test creating a ClaudeResult."""
        result = ClaudeResult(
            success=True,
            output="Done!",
            exit_code=0,
            cost_usd=0.05,
            session_id="session-123",
            duration_seconds=60.0,
        )

        assert result.success is True
        assert result.output == "Done!"
        assert result.cost_usd == 0.05


class TestClaudeRunner:
    """Tests for ClaudeRunner class."""

    @pytest.mark.asyncio
    async def test_run_success(self, runner):
        """Test successful Claude run."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(
                json.dumps({"result": "Task completed!"}).encode(),
                b"",
            )
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
                mock_wait.return_value = (
                    json.dumps({"result": "Task completed!"}).encode(),
                    b"",
                )

                result = await runner.run(
                    prompt="Test prompt",
                    cwd=Path("/tmp/test"),
                )

                assert result.success is True
                assert "completed" in result.output

    @pytest.mark.asyncio
    async def test_run_failure(self, runner):
        """Test failed Claude run."""
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"Error output", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
                mock_wait.return_value = (b"Error output", b"")

                result = await runner.run(
                    prompt="Test prompt",
                    cwd=Path("/tmp/test"),
                )

                assert result.success is False
                assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_run_timeout(self, runner):
        """Test Claude run timeout."""
        import asyncio

        mock_proc = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch(
                "asyncio.wait_for",
                side_effect=asyncio.TimeoutError(),
            ):
                result = await runner.run(
                    prompt="Test prompt",
                    cwd=Path("/tmp/test"),
                    timeout_seconds=1,
                )

                assert result.success is False
                assert "timed out" in result.output.lower()
                mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_not_found(self, runner):
        """Test Claude CLI not found."""
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError(),
        ):
            with pytest.raises(ClaudeRunnerError, match="not found"):
                await runner.run(
                    prompt="Test prompt",
                    cwd=Path("/tmp/test"),
                )

    @pytest.mark.asyncio
    async def test_run_with_system_prompt(self, runner):
        """Test running with system prompt file."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"Done", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
                mock_wait.return_value = (b"Done", b"")

                with patch.object(Path, "exists", return_value=True):
                    result = await runner.run(
                        prompt="Test prompt",
                        cwd=Path("/tmp/test"),
                        system_prompt_file=Path("/tmp/system.md"),
                    )

                    # Verify --append-system-prompt was included
                    call_args = mock_exec.call_args[0]
                    assert "--append-system-prompt" in call_args

    @pytest.mark.asyncio
    async def test_run_checks_all_pass(self, runner):
        """Test running pre-merge checks that all pass."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))

        with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
            with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
                mock_wait.return_value = (b"OK", b"")

                passed, output = await runner.run_checks(
                    commands=["pytest", "ruff check ."],
                    cwd=Path("/tmp/test"),
                )

                assert passed is True
                assert "pytest" in output
                assert "ruff check" in output

    @pytest.mark.asyncio
    async def test_run_checks_one_fails(self, runner):
        """Test running pre-merge checks where one fails."""
        call_count = 0

        async def mock_communicate():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (b"OK", b"")
            else:
                return (b"Error: lint failed", b"")

        def create_mock_proc(*args, **kwargs):
            nonlocal call_count
            mock_proc = MagicMock()
            # First call succeeds, second fails
            if "pytest" in args[0]:
                mock_proc.returncode = 0
                mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))
            else:
                mock_proc.returncode = 1
                mock_proc.communicate = AsyncMock(
                    return_value=(b"Error: lint failed", b"")
                )
            return mock_proc

        with patch("asyncio.create_subprocess_shell", side_effect=create_mock_proc):
            with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
                # Make wait_for just call and return the result
                async def wait_for_side_effect(coro, timeout):
                    return await coro

                mock_wait.side_effect = wait_for_side_effect

                passed, output = await runner.run_checks(
                    commands=["pytest", "ruff check ."],
                    cwd=Path("/tmp/test"),
                )

                assert passed is False
                assert "✅" in output  # pytest passed
                assert "❌" in output  # ruff failed

    @pytest.mark.asyncio
    async def test_implement_issue(self, runner):
        """Test implementing an issue."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(b"Implemented the feature", b"")
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
                mock_wait.return_value = (b"Implemented the feature", b"")

                result = await runner.implement_issue(
                    issue_number=42,
                    issue_title="Add feature X",
                    issue_body="Please add feature X",
                    cwd=Path("/tmp/test"),
                )

                assert result.success is True

    @pytest.mark.asyncio
    async def test_review_pr(self, runner):
        """Test reviewing a PR."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(b"## Summary\nLooks good!", b"")
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
                mock_wait.return_value = (b"## Summary\nLooks good!", b"")

                result = await runner.review_pr(
                    pr_number=7,
                    pr_title="Add feature X",
                    pr_body="Implements feature X",
                    cwd=Path("/tmp/test"),
                )

                assert result.success is True
                assert "Summary" in result.output or "Looks good" in result.output
