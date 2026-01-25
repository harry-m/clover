"""Simplified test session management for Clover.

This module provides a simple workflow for testing PRs:
1. Checkout the PR branch in the main repo
2. Launch local Claude with PR context
3. Warn on uncommitted/unpushed changes on exit

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

from .github_watcher import GitHubWatcher
from .worktree_manager import WorktreeManager

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class TestState:
    """Current test session state."""

    original_branch: str
    branch_name: str
    pr_number: Optional[int] = None
    linked_issue: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "original_branch": self.original_branch,
            "branch_name": self.branch_name,
            "pr_number": self.pr_number,
            "linked_issue": self.linked_issue,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TestState:
        return cls(
            original_branch=data["original_branch"],
            branch_name=data["branch_name"],
            pr_number=data.get("pr_number"),
            linked_issue=data.get("linked_issue"),
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
        self.worktree_manager = WorktreeManager(config, config.repo_path)
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
        _, output, _ = await self._run_git("status", "--porcelain")
        return bool(output.strip())

    async def _has_unpushed_commits(self, branch_name: str) -> tuple[bool, int]:
        """Check for commits ahead of remote.

        Args:
            branch_name: The branch to check against origin.

        Returns:
            Tuple of (has_unpushed, count).
        """
        # Use explicit origin/branch instead of @{upstream} which may not be set
        returncode, output, _ = await self._run_git(
            "rev-list", f"origin/{branch_name}..HEAD", "--count"
        )
        if returncode != 0:
            # Branch might not exist on remote yet, or fetch needed
            logger.debug(f"rev-list failed for origin/{branch_name}, assuming no unpushed")
            return False, 0
        try:
            count = int(output.strip())
            return count > 0, count
        except ValueError:
            return False, 0

    async def _find_pr_for_branch(self, branch_name: str) -> Optional[int]:
        """Find an open PR for the given branch.

        Returns:
            PR number if found, None otherwise.
        """
        prs = await self.github.get_open_prs()
        for pr in prs:
            if pr.branch == branch_name:
                return pr.number
        return None

    async def _check_stale_worktrees(self, target_branch: str) -> None:
        """Check for and clean up stale worktrees with the target branch.

        Raises:
            ValueError: If a worktree has uncommitted changes.
        """
        worktrees = await self.worktree_manager.list_worktrees()
        for wt in worktrees:
            if wt.branch == target_branch:
                # Check if worktree has changes
                has_changes = await self.worktree_manager.has_uncommitted_changes(wt.path)
                if has_changes:
                    raise ValueError(
                        f"Worktree at {wt.path} has uncommitted changes on branch {target_branch}.\n"
                        f"Please commit or discard changes, then remove the worktree:\n"
                        f"  cd {wt.path} && git status\n"
                        f"  git worktree remove {wt.path}"
                    )
                # Auto-cleanup stale worktree
                logger.info(f"Cleaning up stale worktree at {wt.path}")
                await self.worktree_manager.cleanup_worktree(wt.path)

    async def start(self, target: str) -> TestState:
        """Start testing a PR or branch.

        Args:
            target: PR number (as string) or branch name.

        Returns:
            The test state.

        Raises:
            ValueError: If PR not found, uncommitted changes exist, or other errors.
        """
        # Check for existing test session
        existing = self._load_state()
        if existing:
            raise ValueError(
                f"Already testing {'PR #' + str(existing.pr_number) if existing.pr_number else existing.branch_name}.\n"
                f"Run 'clover test resume' to continue, or exit Claude and run again."
            )

        # Check for uncommitted changes
        if await self._has_uncommitted_changes():
            raise ValueError(
                "You have uncommitted changes. Commit or stash them first."
            )

        # Parse target - determine if PR number or branch name
        pr_number = None
        branch_name = None
        linked_issue = None

        if target.isdigit():
            # Treat as PR number
            pr_number = int(target)
            pr = await self.github.get_pr(pr_number)
            if pr is None:
                raise ValueError(f"PR #{pr_number} not found")
            branch_name = pr.branch
            linked_issue = pr.linked_issue
            logger.info(f"Testing PR #{pr_number}: {pr.title}")
        else:
            # Treat as branch name
            branch_name = target
            # Try to find associated PR
            pr_number = await self._find_pr_for_branch(branch_name)
            if pr_number:
                pr = await self.github.get_pr(pr_number)
                if pr:
                    linked_issue = pr.linked_issue
                logger.info(f"Testing branch {branch_name} (PR #{pr_number})")
            else:
                logger.info(f"Testing branch {branch_name} (no associated PR)")

        # Check for stale worktrees
        await self._check_stale_worktrees(branch_name)

        # Save current branch
        original_branch = await self._get_current_branch()

        # Fetch and checkout target branch
        logger.info(f"Checking out {branch_name}...")
        await self._run_git("fetch", "origin", branch_name)
        returncode, _, stderr = await self._run_git("checkout", branch_name)
        if returncode != 0:
            # Branch might not exist locally, try tracking remote
            returncode, _, stderr = await self._run_git(
                "checkout", "-b", branch_name, f"origin/{branch_name}"
            )
            if returncode != 0:
                raise ValueError(f"Failed to checkout {branch_name}: {stderr}")

        # Pull latest
        await self._run_git("pull", "origin", branch_name)

        # Save state
        state = TestState(
            original_branch=original_branch,
            branch_name=branch_name,
            pr_number=pr_number,
            linked_issue=linked_issue,
        )
        self._save_state(state)

        # Launch Claude
        await self._launch_claude(state, resume=False)

        # Run post-exit checks
        await self._post_exit_checks(state)

        return state

    async def _launch_claude(self, state: TestState, resume: bool = False) -> None:
        """Launch Claude with PR context.

        Args:
            state: Current test state.
            resume: If True, use --resume flag.
        """
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

        if resume:
            logger.info("Resuming previous Claude session...")
            subprocess.run(
                [claude_path, "--resume"],
                cwd=self.repo_path,
                shell=(sys.platform == "win32"),
            )
        else:
            # Build simple prompt
            if state.pr_number and state.linked_issue:
                prompt = (
                    f"We are testing PR #{state.pr_number} which implements issue #{state.linked_issue}. "
                    f"Have a look at the changes and the PR description, then let's talk about them."
                )
            elif state.pr_number:
                prompt = (
                    f"We are testing PR #{state.pr_number}. "
                    f"Have a look at the changes and the PR description, then let's talk about them."
                )
            else:
                prompt = (
                    f"We are testing branch {state.branch_name}. "
                    f"Have a look at the changes, then let's talk about them."
                )

            logger.info(f"Launching Claude for {'PR #' + str(state.pr_number) if state.pr_number else state.branch_name}...")

            subprocess.run(
                [claude_path, prompt],
                cwd=self.repo_path,
                shell=(sys.platform == "win32"),
            )

    async def _post_exit_checks(self, state: TestState) -> None:
        """Run checks after Claude exits.

        Checks for uncommitted changes and unpushed commits, warns if found,
        otherwise returns to original branch and clears state.
        """
        has_uncommitted = await self._has_uncommitted_changes()
        has_unpushed, unpushed_count = await self._has_unpushed_commits(state.branch_name)

        if has_uncommitted:
            _, status_output, _ = await self._run_git("status", "--short")
            print()
            print("⚠️  You have uncommitted changes:")
            print(status_output)
            print()
            print("Commit them with: git add . && git commit -m 'your message'")

        if has_unpushed:
            print()
            print(f"⚠️  You have {unpushed_count} unpushed commit(s).")
            print(f"Push them with: git push origin {state.branch_name}")

        if has_uncommitted or has_unpushed:
            print()
            print(f"Staying on branch {state.branch_name}.")
            print("Run 'clover test resume' to continue testing.")
            # Keep state file so resume works
        else:
            # All clean - return to original branch
            logger.info(f"Returning to {state.original_branch}...")
            await self._run_git("checkout", state.original_branch)
            self._save_state(None)
            print()
            print(f"✓ Returned to {state.original_branch}")

    async def resume(self) -> None:
        """Resume previous Claude session.

        Raises:
            ValueError: If no test session is active.
        """
        state = self._load_state()
        if not state:
            raise ValueError("No active test session. Run 'clover test <PR>' first.")

        # Make sure we're on the right branch
        current = await self._get_current_branch()
        if current != state.branch_name:
            logger.info(f"Switching to {state.branch_name}...")
            await self._run_git("checkout", state.branch_name)

        await self._launch_claude(state, resume=True)
        await self._post_exit_checks(state)

    async def clear(self) -> None:
        """Clear test session state.

        Use this to manually clear a stuck test session.
        """
        state = self._load_state()
        if not state:
            print("No active test session to clear.")
            return

        print(f"Clearing test session for {'PR #' + str(state.pr_number) if state.pr_number else state.branch_name}")
        self._save_state(None)
        print("✓ Test session cleared. You can now start a new test.")
