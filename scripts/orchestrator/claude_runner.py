"""Claude Code process spawning and management."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import Config

logger = logging.getLogger(__name__)


def _find_claude_cli() -> str:
    """Find the claude CLI executable.

    Returns:
        Path to claude CLI or just 'claude' if in PATH.
    """
    # First check if it's in PATH
    claude_path = shutil.which("claude")
    if claude_path:
        return claude_path

    # On Windows, check common npm locations
    if os.name == "nt":
        possible_paths = [
            Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
            Path("C:/nvm4w/nodejs/claude.cmd"),
            Path(os.environ.get("ProgramFiles", "")) / "nodejs" / "claude.cmd",
        ]
        for path in possible_paths:
            if path.exists():
                return str(path)

    return "claude"  # Fall back to hoping it's in PATH


@dataclass
class ClaudeResult:
    """Result from a Claude Code execution."""

    success: bool
    output: str
    exit_code: int
    cost_usd: Optional[float] = None
    session_id: Optional[str] = None
    duration_seconds: Optional[float] = None


class ClaudeRunnerError(Exception):
    """Error running Claude Code."""

    pass


class ClaudeRunner:
    """Runs Claude Code processes for implementation and review tasks."""

    def __init__(self, config: Config):
        """Initialize the Claude runner.

        Args:
            config: Orchestrator configuration.
        """
        self.config = config

    async def run(
        self,
        prompt: str,
        cwd: Path,
        system_prompt_file: Optional[Path] = None,
        allowed_tools: Optional[list[str]] = None,
        timeout_seconds: int = 1800,  # 30 minutes default
        on_output: Optional[Callable[[str, Optional[str]], None]] = None,
    ) -> ClaudeResult:
        """Run Claude Code with a prompt.

        Args:
            prompt: The prompt to send to Claude.
            cwd: Working directory for Claude.
            system_prompt_file: Optional path to system prompt file.
            allowed_tools: List of allowed tools. Defaults to safe set.
            timeout_seconds: Maximum execution time.
            on_output: Optional callback for output lines. Called with (line, tool_name).
                       tool_name is set when a tool starts, None otherwise.

        Returns:
            ClaudeResult with output and status.
        """
        if allowed_tools is None:
            allowed_tools = [
                "Bash",
                "Read",
                "Write",
                "Edit",
                "Glob",
                "Grep",
                "TodoWrite",
            ]

        # Find claude CLI
        claude_cli = _find_claude_cli()
        logger.debug(f"Using Claude CLI: {claude_cli}")

        # Build command - use stream-json for real-time visibility
        cmd = [
            claude_cli,
            "-p",  # Print mode (non-interactive)
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", str(self.config.max_turns),
            "--permission-mode", "acceptEdits",
            "--allowedTools", ",".join(allowed_tools),
        ]

        # Add system prompt if provided
        if system_prompt_file and system_prompt_file.exists():
            cmd.extend(["--append-system-prompt", str(system_prompt_file)])

        # Add the prompt
        cmd.append(prompt)

        logger.info(f"Running Claude in {cwd}")
        logger.debug(f"Command: {' '.join(cmd)}")

        try:
            import time
            start_time = time.time()

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Increase buffer limit to 10MB to handle large stream-json lines
                # (default is 64KB which fails on large tool outputs)
                limit=10 * 1024 * 1024,
            )

            # Signal that Claude is starting
            if on_output:
                on_output("Waiting for Claude...", None)

            # Stream stdout to show Claude's activity in real-time
            stdout_chunks = []
            stderr_chunks = []

            first_response = True
            # Buffer for accumulating streaming text
            text_buffer = ""
            last_text_output_time = 0

            async def read_stdout():
                nonlocal first_response, text_buffer, last_text_output_time
                import time

                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    stdout_chunks.append(line)
                    line_str = line.decode().rstrip()
                    if line_str:
                        # Parse stream-json and log meaningful activity
                        try:
                            data = json.loads(line_str)
                            msg_type = data.get("type", "")
                            subtype = data.get("subtype", "")

                            if msg_type == "system" and subtype == "init":
                                # Session initialized
                                if on_output:
                                    on_output("Claude session started", None)
                            elif msg_type == "content_block_start":
                                # New content block starting
                                block = data.get("content_block", {})
                                block_type = block.get("type", "")
                                if block_type == "tool_use":
                                    # Flush any accumulated text before tool use
                                    if text_buffer.strip():
                                        if on_output:
                                            on_output(text_buffer.strip()[:200], None)
                                        text_buffer = ""
                                    tool = block.get("name", "unknown")
                                    if on_output:
                                        on_output(f"Using tool: {tool}", tool)
                                    else:
                                        logger.info(f"[Claude] Using tool: {tool}")
                                elif block_type == "text":
                                    # Text block starting - Claude is thinking/responding
                                    if first_response:
                                        first_response = False
                                        if on_output:
                                            on_output("Claude is thinking...", None)
                            elif msg_type == "content_block_delta":
                                # Streaming delta
                                delta = data.get("delta", {})
                                delta_type = delta.get("type", "")
                                if delta_type == "text_delta":
                                    text = delta.get("text", "")
                                    if text:
                                        text_buffer += text
                                        # Output accumulated text periodically (every ~500ms)
                                        # or when buffer gets long enough
                                        now = time.time()
                                        if (now - last_text_output_time > 0.5 and text_buffer.strip()) or len(text_buffer) > 100:
                                            # Find a good break point (newline or space)
                                            output_text = text_buffer
                                            if "\n" in output_text:
                                                # Output up to last newline
                                                last_nl = output_text.rfind("\n")
                                                output_text = text_buffer[:last_nl]
                                                text_buffer = text_buffer[last_nl + 1:]
                                            else:
                                                text_buffer = ""

                                            if output_text.strip():
                                                # Split into lines and output
                                                for out_line in output_text.split("\n"):
                                                    if out_line.strip():
                                                        if on_output:
                                                            on_output(out_line[:200], None)
                                                        else:
                                                            logger.info(f"[Claude] {out_line[:200]}")
                                            last_text_output_time = now
                            elif msg_type == "content_block_stop":
                                # Content block finished - flush remaining text
                                if text_buffer.strip():
                                    for out_line in text_buffer.split("\n"):
                                        if out_line.strip():
                                            if on_output:
                                                on_output(out_line[:200], None)
                                            else:
                                                logger.info(f"[Claude] {out_line[:200]}")
                                    text_buffer = ""
                            elif msg_type == "assistant":
                                # Full assistant message (non-streaming or final)
                                # Extract text from assistant messages
                                msg = data.get("message", {})
                                content = msg.get("content", [])
                                for item in content:
                                    if item.get("type") == "text":
                                        text = item.get("text", "")
                                        if text:
                                            # Show first line or snippet
                                            first_line = text.split("\n")[0][:200]
                                            if first_line.strip():
                                                if on_output:
                                                    on_output(first_line, None)
                                                else:
                                                    logger.info(f"[Claude] {first_line}")
                                    elif item.get("type") == "tool_use":
                                        tool = item.get("name", "unknown")
                                        if on_output:
                                            on_output(f"Using tool: {tool}", tool)
                                        else:
                                            logger.info(f"[Claude] Using tool: {tool}")
                            elif msg_type == "result":
                                # Final result
                                result_text = data.get("result", "")
                                if on_output:
                                    on_output("Task completed", None)
                                    if result_text:
                                        # Split long results into lines for display
                                        for result_line in result_text.split("\n")[:15]:
                                            if result_line.strip():
                                                on_output(result_line[:200], None)
                                else:
                                    logger.info("[Claude] Task completed")
                        except json.JSONDecodeError:
                            pass  # Ignore non-JSON lines

            async def read_stderr():
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    stderr_chunks.append(chunk)

            try:
                await asyncio.wait_for(
                    asyncio.gather(read_stdout(), read_stderr(), proc.wait()),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ClaudeResult(
                    success=False,
                    output=f"Claude process timed out after {timeout_seconds}s",
                    exit_code=-1,
                )

            duration = time.time() - start_time

            stdout_str = b"".join(stdout_chunks).decode()
            stderr_str = b"".join(stderr_chunks).decode()

            # Parse stream-json to extract final result
            output = ""
            cost_usd = None
            session_id = None

            for line in stdout_str.strip().split("\n"):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("type") == "result":
                        output = data.get("result", "")
                        cost_usd = data.get("total_cost_usd")
                        session_id = data.get("session_id")
                except json.JSONDecodeError:
                    pass

            if not output:
                output = stderr_str or "No output"

            success = proc.returncode == 0

            if not success:
                logger.warning(
                    f"Claude exited with code {proc.returncode}: {output[:200]}"
                )
            else:
                logger.info(f"Claude completed successfully in {duration:.1f}s")
                if cost_usd:
                    logger.info(f"Cost: ${cost_usd:.4f}")

            return ClaudeResult(
                success=success,
                output=output,
                exit_code=proc.returncode or 0,
                cost_usd=cost_usd,
                session_id=session_id,
                duration_seconds=duration,
            )

        except FileNotFoundError:
            raise ClaudeRunnerError(
                "Claude CLI not found. Ensure 'claude' is in your PATH "
                "and you have authenticated with 'claude' first."
            )
        except Exception as e:
            logger.error(f"Error running Claude: {e}")
            raise ClaudeRunnerError(f"Failed to run Claude: {e}")

    async def run_checks(
        self,
        commands: list[str],
        cwd: Path,
        timeout_seconds: int = 600,  # 10 minutes per command
    ) -> tuple[bool, str]:
        """Run pre-merge check commands.

        Args:
            commands: List of commands to run.
            cwd: Working directory.
            timeout_seconds: Timeout per command.

        Returns:
            Tuple of (all_passed, output_summary).
        """
        results = []
        all_passed = True

        for command in commands:
            logger.info(f"Running check: {command}")

            try:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    results.append(f"❌ `{command}` - Timed out after {timeout_seconds}s")
                    all_passed = False
                    continue

                stdout_str = stdout.decode()
                stderr_str = stderr.decode()
                output = stdout_str + stderr_str

                if proc.returncode == 0:
                    results.append(f"✅ `{command}` - Passed")
                    logger.info(f"Check passed: {command}")
                else:
                    results.append(
                        f"❌ `{command}` - Failed (exit code {proc.returncode})\n"
                        f"```\n{output[:1000]}\n```"
                    )
                    all_passed = False
                    logger.warning(f"Check failed: {command}")

            except Exception as e:
                results.append(f"❌ `{command}` - Error: {e}")
                all_passed = False
                logger.error(f"Check error: {command}: {e}")

        summary = "\n\n".join(results)
        return all_passed, summary

    async def implement_issue(
        self,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        cwd: Path,
        on_output: Optional[Callable[[str, Optional[str]], None]] = None,
    ) -> ClaudeResult:
        """Run Claude to implement an issue.

        Args:
            issue_number: GitHub issue number.
            issue_title: Issue title.
            issue_body: Issue body/description.
            cwd: Worktree path to work in.
            on_output: Optional callback for output lines.

        Returns:
            ClaudeResult.
        """
        prompt = f"""Implement this GitHub issue:

# Issue #{issue_number}: {issue_title}

{issue_body}

---

Instructions:
1. Read and understand the issue requirements
2. Explore the codebase to understand the relevant code
3. Implement the feature or fix
4. Write or update tests if appropriate
5. IMPORTANT: You MUST commit your changes using git. Run `git add` and `git commit` with a clear message that references #{issue_number}. Uncommitted changes will be lost!

When done, provide a summary of what you implemented.
"""

        system_prompt_file = self.config.prompts_dir / "implement.md"

        return await self.run(
            prompt=prompt,
            cwd=cwd,
            system_prompt_file=system_prompt_file,
            on_output=on_output,
        )

    async def review_pr(
        self,
        pr_number: int,
        pr_title: str,
        pr_body: str,
        cwd: Path,
        on_output: Optional[Callable[[str, Optional[str]], None]] = None,
    ) -> ClaudeResult:
        """Run Claude to review a PR.

        Args:
            pr_number: GitHub PR number.
            pr_title: PR title.
            pr_body: PR body/description.
            cwd: Worktree path with PR code.
            on_output: Optional callback for output lines.

        Returns:
            ClaudeResult.
        """
        prompt = f"""Review this pull request:

# PR #{pr_number}: {pr_title}

{pr_body}

---

Instructions:
1. Read the PR description to understand what it's trying to accomplish
2. Review the code changes (use git diff to see what changed)
3. Check for:
   - Code correctness and logic errors
   - Edge cases and error handling
   - Code style and consistency
   - Test coverage
   - Security issues
   - Performance concerns
4. Provide constructive feedback

Format your review as markdown with sections for:
- Summary (1-2 sentences)
- What looks good
- Suggestions for improvement
- Any blocking issues
"""

        system_prompt_file = self.config.prompts_dir / "review.md"

        return await self.run(
            prompt=prompt,
            cwd=cwd,
            system_prompt_file=system_prompt_file,
            on_output=on_output,
            # Review doesn't need write access
            allowed_tools=["Bash", "Read", "Glob", "Grep"],
        )

    async def implement_review(
        self,
        pr_number: int,
        pr_title: str,
        pr_body: str,
        review_comment: str,
        cwd: Path,
        on_output: Optional[Callable[[str, Optional[str]], None]] = None,
    ) -> ClaudeResult:
        """Run Claude to implement review suggestions for a PR.

        Args:
            pr_number: GitHub PR number.
            pr_title: PR title.
            pr_body: PR body/description.
            review_comment: The review feedback to implement.
            cwd: Worktree path with PR code.
            on_output: Optional callback for output lines.

        Returns:
            ClaudeResult.
        """
        prompt = f"""Implement the review suggestions for this pull request:

# PR #{pr_number}: {pr_title}

## PR Description

{pr_body}

## Review Feedback to Implement

{review_comment}

---

Instructions:
1. Read the review feedback carefully
2. Address each suggestion, prioritizing blocking issues first
3. Make focused changes that address the specific feedback
4. IMPORTANT: You MUST commit your changes using git. Run `git add` and `git commit` with a clear message. Uncommitted changes will be lost!

When done, provide a summary of what you implemented and which suggestions were addressed.
"""

        system_prompt_file = self.config.prompts_dir / "implement_review.md"

        return await self.run(
            prompt=prompt,
            cwd=cwd,
            system_prompt_file=system_prompt_file,
            on_output=on_output,
        )
