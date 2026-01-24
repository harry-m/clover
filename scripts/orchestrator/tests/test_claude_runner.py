"""Tests for Claude runner."""

import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


def create_mock_process(stdout_lines: list[bytes], stderr_data: bytes = b"", returncode: int = 0):
    """Create a properly mocked async subprocess.

    Args:
        stdout_lines: List of lines to return from stdout.readline()
        stderr_data: Data to return from stderr.read()
        returncode: Process return code
    """
    mock_proc = MagicMock()
    mock_proc.returncode = returncode

    # Create async iterators for stdout lines
    stdout_iter = iter(stdout_lines + [b""])  # Add empty bytes to signal EOF

    async def mock_readline():
        try:
            return next(stdout_iter)
        except StopIteration:
            return b""

    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline = mock_readline

    # Mock stderr.read() to return data once, then empty
    stderr_returned = False

    async def mock_stderr_read(size):
        nonlocal stderr_returned
        if not stderr_returned:
            stderr_returned = True
            return stderr_data
        return b""

    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read = mock_stderr_read

    # Mock wait() as async
    mock_proc.wait = AsyncMock(return_value=returncode)
    mock_proc.kill = MagicMock()

    return mock_proc


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
        result_json = json.dumps({
            "type": "result",
            "result": "Task completed!",
            "total_cost_usd": 0.05,
            "session_id": "test-session"
        })

        mock_proc = create_mock_process(
            stdout_lines=[result_json.encode() + b"\n"],
            returncode=0
        )

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            result = await runner.run(
                prompt="Test prompt",
                cwd=Path("/tmp/test"),
            )

            assert result.success is True
            assert "completed" in result.output

    @pytest.mark.asyncio
    async def test_run_failure(self, runner):
        """Test failed Claude run."""
        mock_proc = create_mock_process(
            stdout_lines=[b"Error output\n"],
            stderr_data=b"Some error",
            returncode=1
        )

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            result = await runner.run(
                prompt="Test prompt",
                cwd=Path("/tmp/test"),
            )

            assert result.success is False
            assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_run_timeout(self, runner):
        """Test Claude run timeout."""
        mock_proc = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        mock_proc.returncode = -1

        # Make stdout.readline hang forever
        async def hanging_readline():
            await asyncio.sleep(100)
            return b""

        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline = hanging_readline

        async def mock_stderr_read(size):
            await asyncio.sleep(100)
            return b""

        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = mock_stderr_read

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
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
        result_json = json.dumps({
            "type": "result",
            "result": "Done",
        })

        mock_proc = create_mock_process(
            stdout_lines=[result_json.encode() + b"\n"],
            returncode=0
        )

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)) as mock_exec:
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

        with patch("asyncio.create_subprocess_shell", AsyncMock(return_value=mock_proc)):
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

        def create_mock_proc(cmd, **kwargs):
            mock_proc = MagicMock()
            # First call succeeds, second fails
            if "pytest" in cmd:
                mock_proc.returncode = 0
                mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))
            else:
                mock_proc.returncode = 1
                mock_proc.communicate = AsyncMock(
                    return_value=(b"Error: lint failed", b"")
                )
            return mock_proc

        with patch("asyncio.create_subprocess_shell", AsyncMock(side_effect=create_mock_proc)):
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
        result_json = json.dumps({
            "type": "result",
            "result": "Implemented the feature",
        })

        mock_proc = create_mock_process(
            stdout_lines=[result_json.encode() + b"\n"],
            returncode=0
        )

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
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
        result_json = json.dumps({
            "type": "result",
            "result": "## Summary\nLooks good!",
        })

        mock_proc = create_mock_process(
            stdout_lines=[result_json.encode() + b"\n"],
            returncode=0
        )

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            result = await runner.review_pr(
                pr_number=7,
                pr_title="Add feature X",
                pr_body="Implements feature X",
                cwd=Path("/tmp/test"),
            )

            assert result.success is True
            assert "Summary" in result.output or "Looks good" in result.output
