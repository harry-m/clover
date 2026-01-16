"""Claude Code process spawning and management."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Config

logger = logging.getLogger(__name__)


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
    ) -> ClaudeResult:
        """Run Claude Code with a prompt.

        Args:
            prompt: The prompt to send to Claude.
            cwd: Working directory for Claude.
            system_prompt_file: Optional path to system prompt file.
            allowed_tools: List of allowed tools. Defaults to safe set.
            timeout_seconds: Maximum execution time.

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

        # Build command
        cmd = [
            "claude",
            "-p",  # Print mode (non-interactive)
            "--output-format", "json",
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
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
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

            stdout_str = stdout.decode()
            stderr_str = stderr.decode()

            # Try to parse JSON output
            result_data = None
            cost_usd = None
            session_id = None

            if stdout_str:
                try:
                    # Claude outputs JSON when --output-format json is used
                    result_data = json.loads(stdout_str)
                    cost_usd = result_data.get("cost_usd")
                    session_id = result_data.get("session_id")
                except json.JSONDecodeError:
                    # Not JSON, just use raw output
                    pass

            # Determine output text
            if result_data and "result" in result_data:
                output = result_data["result"]
            elif result_data and "error" in result_data:
                output = result_data["error"]
            else:
                output = stdout_str or stderr_str

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
    ) -> ClaudeResult:
        """Run Claude to implement an issue.

        Args:
            issue_number: GitHub issue number.
            issue_title: Issue title.
            issue_body: Issue body/description.
            cwd: Worktree path to work in.

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
5. Commit your changes with a clear commit message that references the issue

When done, provide a summary of what you implemented.
"""

        system_prompt_file = self.config.prompts_dir / "implement.md"

        return await self.run(
            prompt=prompt,
            cwd=cwd,
            system_prompt_file=system_prompt_file,
        )

    async def review_pr(
        self,
        pr_number: int,
        pr_title: str,
        pr_body: str,
        cwd: Path,
    ) -> ClaudeResult:
        """Run Claude to review a PR.

        Args:
            pr_number: GitHub PR number.
            pr_title: PR title.
            pr_body: PR body/description.
            cwd: Worktree path with PR code.

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
            # Review doesn't need write access
            allowed_tools=["Bash", "Read", "Glob", "Grep"],
        )
