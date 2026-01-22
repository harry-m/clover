"""Simplified test session management for Clover.

This module provides a simple workflow for testing PRs:
1. Checkout the PR branch in the main repo
2. Start docker compose
3. Launch local Claude with PR context

Only one test session at a time - no worktrees, no complexity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .docker_utils import DockerCompose, DockerError
from .github_watcher import GitHubWatcher

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class TestState:
    """Current test session state."""

    branch_name: str
    pr_number: Optional[int] = None
    pr_title: Optional[str] = None
    pr_body: Optional[str] = None
    linked_issue: Optional[int] = None
    issue_title: Optional[str] = None
    issue_body: Optional[str] = None
    original_branch: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "branch_name": self.branch_name,
            "pr_number": self.pr_number,
            "pr_title": self.pr_title,
            "pr_body": self.pr_body,
            "linked_issue": self.linked_issue,
            "issue_title": self.issue_title,
            "issue_body": self.issue_body,
            "original_branch": self.original_branch,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TestState:
        return cls(
            branch_name=data["branch_name"],
            pr_number=data.get("pr_number"),
            pr_title=data.get("pr_title"),
            pr_body=data.get("pr_body"),
            linked_issue=data.get("linked_issue"),
            issue_title=data.get("issue_title"),
            issue_body=data.get("issue_body"),
            original_branch=data.get("original_branch"),
        )


class TestSessionManager:
    """Simplified test session manager.

    Manages a single test session at a time using direct checkout
    in the main repository.
    """

    def __init__(self, config: "Config"):
        self.config = config
        self.repo_path = config.repo_path
        self.github = GitHubWatcher(config)
        self._state_file = config.state_file.parent / ".clover-test-state.json"

    def _load_state(self) -> Optional[TestState]:
        """Load current test state."""
        if not self._state_file.exists():
            return None
        try:
            with open(self._state_file) as f:
                return TestState.from_dict(json.load(f))
        except (json.JSONDecodeError, KeyError):
            return None

    def _save_state(self, state: Optional[TestState]) -> None:
        """Save test state."""
        if state is None:
            if self._state_file.exists():
                self._state_file.unlink()
            return
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._state_file, "w") as f:
            json.dump(state.to_dict(), f, indent=2)

    async def _run_git(self, *args: str) -> tuple[int, str, str]:
        """Run a git command."""
        cmd = ["git"] + list(args)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip()

    async def _get_current_branch(self) -> str:
        """Get current git branch."""
        _, branch, _ = await self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        return branch

    async def _has_uncommitted_changes(self) -> bool:
        """Check for uncommitted changes."""
        returncode, _, _ = await self._run_git("diff", "--quiet", "HEAD")
        return returncode != 0

    async def _resolve_pr(self, pr_number: int) -> dict:
        """Resolve PR to full details including linked issue."""
        pr = await self.github.get_pr(pr_number)
        if pr is None:
            raise ValueError(f"PR #{pr_number} not found")

        result = {
            "branch": pr.branch,
            "title": pr.title,
            "body": pr.body,
            "linked_issue": pr.linked_issue,
            "issue_title": None,
            "issue_body": None,
        }

        # Fetch linked issue details if available
        if pr.linked_issue:
            issue = await self.github.get_issue(pr.linked_issue)
            if issue:
                result["issue_title"] = issue.title
                result["issue_body"] = issue.body

        return result

    def _get_compose_file(self) -> Path:
        """Get docker-compose file path."""
        compose_path = self.repo_path / self.config.test.compose_file
        if not compose_path.exists():
            raise FileNotFoundError(
                f"Docker Compose file not found: {compose_path}\n"
                f"Configure 'test.compose_file' in clover.yaml"
            )
        return compose_path

    async def start(
        self,
        pr_number: int,
        skip_docker: bool = False,
        no_claude: bool = False,
    ) -> TestState:
        """Start testing a PR.

        Args:
            pr_number: The PR number to test.
            skip_docker: Skip starting docker containers.
            no_claude: Don't launch Claude after setup.

        Returns:
            The test state.

        Raises:
            ValueError: If PR not found or uncommitted changes exist.
            DockerError: If docker fails to start.
        """
        # Check for existing test session
        existing = self._load_state()
        if existing:
            raise ValueError(
                f"Already testing PR #{existing.pr_number or existing.branch_name}.\n"
                f"Run 'clover test stop' first."
            )

        # Check for uncommitted changes
        if await self._has_uncommitted_changes():
            raise ValueError(
                "You have uncommitted changes. Commit or stash them first."
            )

        # Resolve PR with full details
        pr_info = await self._resolve_pr(pr_number)
        logger.info(f"Testing PR #{pr_number}: {pr_info['title']}")

        # Save current branch
        original_branch = await self._get_current_branch()

        # Fetch and checkout PR branch
        branch = pr_info['branch']
        logger.info(f"Checking out {branch}...")
        await self._run_git("fetch", "origin", branch)
        returncode, _, stderr = await self._run_git("checkout", branch)
        if returncode != 0:
            raise ValueError(f"Failed to checkout {branch}: {stderr}")

        # Pull latest
        await self._run_git("pull", "origin", branch)

        # Save state with full PR context
        state = TestState(
            branch_name=branch,
            pr_number=pr_number,
            pr_title=pr_info['title'],
            pr_body=pr_info['body'],
            linked_issue=pr_info['linked_issue'],
            issue_title=pr_info['issue_title'],
            issue_body=pr_info['issue_body'],
            original_branch=original_branch,
        )
        self._save_state(state)

        # Start docker in background
        if not skip_docker:
            compose_file = self._get_compose_file()
            logger.info("Starting Docker containers in background...")
            # Start docker compose without waiting
            subprocess.Popen(
                ["docker", "compose", "-f", str(compose_file), "-p", "clover-test", "up", "-d"],
                cwd=self.repo_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        # Launch Claude
        if not no_claude:
            self._launch_claude(state)

        return state

    def _launch_claude(self, state: TestState) -> None:
        """Launch Claude with PR context."""
        # Find claude executable
        claude_path = shutil.which("claude")
        if not claude_path:
            # Try common Windows locations
            for name in ["claude.cmd", "claude.exe", "claude.bat"]:
                claude_path = shutil.which(name)
                if claude_path:
                    break

        if not claude_path:
            logger.error("Claude not found in PATH. Install with: npm install -g @anthropic-ai/claude-code")
            return

        # Build rich context with PR and issue details
        context_lines = ["# Test Session Context", ""]

        if state.pr_number:
            context_lines.append(f"## PR #{state.pr_number}: {state.pr_title}")
            context_lines.append("")
            if state.pr_body:
                context_lines.append("### PR Description")
                context_lines.append(state.pr_body)
                context_lines.append("")
        else:
            context_lines.append(f"## Branch: {state.branch_name}")
            context_lines.append("")

        if state.linked_issue:
            context_lines.append(f"## Linked Issue #{state.linked_issue}: {state.issue_title}")
            context_lines.append("")
            if state.issue_body:
                context_lines.append("### Issue Description")
                context_lines.append(state.issue_body)
                context_lines.append("")

        context_lines.extend([
            "## Environment",
            "- The PR code is checked out in this directory",
            "- Docker containers are starting in the background",
            "",
            "## Your Role",
            "Help the user test and verify the changes in this PR.",
            "Focus on the requirements described in the issue and PR description above.",
        ])

        context = "\n".join(context_lines)

        # Write context to file for Claude to read
        context_file = self.repo_path / ".clover-test-context.md"
        context_file.write_text(context)

        logger.info(f"Launching Claude for PR #{state.pr_number}..." if state.pr_number
                    else f"Launching Claude for {state.branch_name}...")

        # Launch Claude with initial prompt to read context
        initial_prompt = "Read .clover-test-context.md for your task context, then greet me."
        subprocess.run(
            [claude_path, initial_prompt],
            cwd=self.repo_path,
            shell=(sys.platform == "win32"),
        )

    async def stop(self, keep_branch: bool = False) -> None:
        """Stop the current test session.

        Args:
            keep_branch: Don't switch back to original branch.

        Raises:
            ValueError: If no test session is active.
        """
        state = self._load_state()
        if not state:
            raise ValueError("No active test session. Run 'clover test start <PR>' first.")

        # Stop docker
        try:
            compose_file = self._get_compose_file()
            compose = DockerCompose(compose_file, project_name="clover-test")
            logger.info("Stopping Docker containers...")
            await compose.down(volumes=False)
        except FileNotFoundError:
            logger.warning("Docker compose file not found, skipping container cleanup")

        # Switch back to original branch
        if not keep_branch and state.original_branch:
            logger.info(f"Switching back to {state.original_branch}...")
            await self._run_git("checkout", state.original_branch)

        # Clear state
        self._save_state(None)

        if state.pr_number:
            logger.info(f"Stopped testing PR #{state.pr_number}")
        else:
            logger.info(f"Stopped testing {state.branch_name}")

    async def status(self) -> Optional[TestState]:
        """Get current test status."""
        return self._load_state()

    def resume(self) -> None:
        """Re-launch Claude for the current test session.

        Raises:
            ValueError: If no test session is active.
        """
        state = self._load_state()
        if not state:
            raise ValueError("No active test session. Run 'clover test start <PR>' first.")
        self._launch_claude(state)
