"""Worktree-based test session management for Clover.

Each `clover test` creates an isolated worktree, launches Claude in it,
runs post-exit checks, and leaves the worktree for the user to clean up.
No state file, no session limit - multiple tests can run concurrently.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .github_watcher import GitHubWatcher
from .worktree_manager import WorktreeManager

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)

# Prefix used for test worktree directory names
TEST_WORKTREE_PREFIX = "test-"


class TestSessionManager:
    """Stateless test session manager using git worktrees.

    Each test creates an isolated worktree. No state file is used.
    Multiple tests can run concurrently in separate terminals.
    """

    def __init__(self, config: "Config"):
        self.config = config
        self.repo_path = config.repo_path
        self.github = GitHubWatcher(config)
        self.worktree_manager = WorktreeManager(config, config.repo_path)

    async def _run_git(
        self, *args: str, cwd: Optional[Path] = None
    ) -> tuple[int, str, str]:
        """Run a git command."""
        cmd = ["git"] + list(args)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd or self.repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip()

    def _worktree_name(self, target: str, pr_number: Optional[int]) -> str:
        """Get the worktree directory name for a target.

        PR tests: test-{pr_number} (e.g., test-42)
        Branch tests: test-{safe_branch_name} (e.g., test-feature-auth)
        """
        if pr_number is not None:
            return f"{TEST_WORKTREE_PREFIX}{pr_number}"
        safe_name = target.replace("/", "-")
        return f"{TEST_WORKTREE_PREFIX}{safe_name}"

    def _worktree_path(self, name: str) -> Path:
        """Get the full path for a worktree by name."""
        return self.worktree_manager.worktree_base / name

    async def _find_pr_for_branch(self, branch_name: str) -> Optional[int]:
        """Find an open PR for the given branch."""
        prs = await self.github.get_open_prs()
        for pr in prs:
            if pr.branch == branch_name:
                return pr.number
        return None

    async def _create_test_worktree(
        self, worktree_path: Path, branch_name: str
    ) -> Path:
        """Create a test worktree with a writable local tracking branch.

        Fetches the branch from origin and creates a worktree with a local
        branch that tracks the remote, allowing commits and pushes.
        """
        base = self.worktree_manager.worktree_base
        base.mkdir(parents=True, exist_ok=True)

        # Prune stale worktree references
        await self._run_git("worktree", "prune")

        # If worktree already exists and is valid, reuse it
        if worktree_path.exists():
            returncode, _, _ = await self._run_git(
                "rev-parse", "HEAD", cwd=worktree_path
            )
            if returncode == 0:
                logger.info(f"Reusing existing worktree at {worktree_path}")
                # Make sure we're on the right branch and up to date
                await self._run_git(
                    "checkout", branch_name, cwd=worktree_path
                )
                await self._run_git(
                    "pull", "origin", branch_name, cwd=worktree_path
                )
                return worktree_path
            # Corrupted - remove and recreate
            logger.warning(f"Worktree at {worktree_path} is corrupted, recreating...")
            await self.worktree_manager.cleanup_worktree(worktree_path)

        # Fetch the branch
        await self._run_git("fetch", "origin", branch_name)

        # Check if branch is checked out in main repo (git won't allow same
        # branch in multiple worktrees)
        _, current_branch, _ = await self._run_git(
            "rev-parse", "--abbrev-ref", "HEAD"
        )
        if current_branch.strip() == branch_name:
            # Detach HEAD so the branch can be used in the worktree
            await self._run_git("checkout", "--detach")

        # Create worktree from the remote branch
        returncode, _, stderr = await self._run_git(
            "worktree", "add", str(worktree_path), f"origin/{branch_name}"
        )
        if returncode != 0:
            raise ValueError(f"Failed to create worktree: {stderr}")

        # Create local tracking branch in the worktree
        await self._run_git(
            "checkout", "-B", branch_name, f"origin/{branch_name}",
            cwd=worktree_path,
        )

        return worktree_path

    async def _run_setup_script(
        self,
        worktree_path: Path,
        branch_name: str,
        pr_number: Optional[int],
    ) -> None:
        """Run setup script if configured.

        Args:
            worktree_path: Path to the worktree directory.
            branch_name: Name of the branch.
            pr_number: PR number if testing a PR, None for branch testing.

        Raises:
            FileNotFoundError: If setup script doesn't exist.
            RuntimeError: If setup script fails.
        """
        if not self.config.setup_script:
            return

        script_path = self.config.repo_path / self.config.setup_script
        if not script_path.exists():
            raise FileNotFoundError(f"Setup script not found: {script_path}")

        env = {
            **os.environ,
            "CLOVER_PARENT_REPO": str(self.config.repo_path),
            "CLOVER_WORKTREE": str(worktree_path),
            "CLOVER_BRANCH": branch_name,
            "CLOVER_WORK_TYPE": "test",
        }
        if pr_number is not None:
            env["CLOVER_PR_NUMBER"] = str(pr_number)

        logger.info(f"Running setup script: {script_path}")

        process = await asyncio.create_subprocess_exec(
            "sh",
            str(script_path),
            cwd=worktree_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(
                f"Setup script failed (exit {process.returncode}): {stdout.decode()}"
            )

        logger.info("Setup script completed successfully")

    async def start(self, target: str) -> None:
        """Start testing a PR or branch in an isolated worktree.

        Creates a worktree, launches Claude in it, and runs post-exit checks.

        Args:
            target: PR number (as string) or branch name.

        Raises:
            ValueError: If PR not found or worktree creation fails.
        """
        # Parse target - determine if PR number or branch name
        pr_number = None
        branch_name = None
        linked_issue = None

        if target.isdigit():
            pr_number = int(target)
            pr = await self.github.get_pr(pr_number)
            if pr is None:
                raise ValueError(f"PR #{pr_number} not found")
            branch_name = pr.branch
            linked_issue = pr.linked_issue
            logger.info(f"Testing PR #{pr_number}: {pr.title}")
        else:
            branch_name = target
            pr_number = await self._find_pr_for_branch(branch_name)
            if pr_number:
                pr = await self.github.get_pr(pr_number)
                if pr:
                    linked_issue = pr.linked_issue
                logger.info(f"Testing branch {branch_name} (PR #{pr_number})")
            else:
                logger.info(f"Testing branch {branch_name} (no associated PR)")

        # Determine worktree name and path
        wt_name = self._worktree_name(target, pr_number if target.isdigit() else None)
        wt_path = self._worktree_path(wt_name)

        # Create the worktree
        logger.info(f"Creating worktree at {wt_path}...")
        await self._create_test_worktree(wt_path, branch_name)

        # Run setup script if configured
        await self._run_setup_script(wt_path, branch_name, pr_number)

        # Launch Claude
        await self._launch_claude(
            cwd=wt_path,
            branch_name=branch_name,
            pr_number=pr_number,
            linked_issue=linked_issue,
        )

        # Post-exit checks
        await self._post_exit_checks(
            wt_path=wt_path,
            wt_name=wt_name,
            branch_name=branch_name,
        )

    async def _launch_claude(
        self,
        cwd: Path,
        branch_name: str,
        pr_number: Optional[int],
        linked_issue: Optional[int],
    ) -> None:
        """Launch Claude with PR/branch context in the given directory."""
        claude_path = shutil.which("claude")
        if not claude_path:
            for name in ["claude.cmd", "claude.exe", "claude.bat"]:
                claude_path = shutil.which(name)
                if claude_path:
                    break

        if not claude_path:
            logger.error(
                "Claude not found in PATH. Install with: "
                "npm install -g @anthropic-ai/claude-code"
            )
            return

        # Build prompt
        if pr_number and linked_issue:
            prompt = (
                f"We are testing PR #{pr_number} which implements issue #{linked_issue}. "
                f"Have a look at the changes and the PR description, then let's talk about them."
            )
        elif pr_number:
            prompt = (
                f"We are testing PR #{pr_number}. "
                f"Have a look at the changes and the PR description, then let's talk about them."
            )
        else:
            prompt = (
                f"We are testing branch {branch_name}. "
                f"Have a look at the changes, then let's talk about them."
            )

        label = f"PR #{pr_number}" if pr_number else branch_name
        logger.info(f"Launching Claude for {label} in {cwd}...")

        subprocess.run(
            [claude_path, prompt],
            cwd=cwd,
            shell=(sys.platform == "win32"),
        )

    async def _post_exit_checks(
        self,
        wt_path: Path,
        wt_name: str,
        branch_name: str,
    ) -> None:
        """Run checks after Claude exits.

        Checks for uncommitted changes and unpushed commits in the worktree.
        Prints warnings and cleanup instructions.
        """
        has_uncommitted = await self.worktree_manager.has_uncommitted_changes(wt_path)

        # Check for unpushed commits
        returncode, output, _ = await self._run_git(
            "rev-list", f"origin/{branch_name}..HEAD", "--count",
            cwd=wt_path,
        )
        has_unpushed = False
        unpushed_count = 0
        if returncode == 0:
            try:
                unpushed_count = int(output.strip())
                has_unpushed = unpushed_count > 0
            except ValueError:
                pass

        if has_uncommitted:
            status = await self.worktree_manager.get_uncommitted_status(wt_path)
            print()
            print(f"Warning: uncommitted changes in worktree:")
            print(status)
            print()
            print(f"To commit:  cd {wt_path} && git add . && git commit -m 'your message'")

        if has_unpushed:
            print()
            print(f"Warning: {unpushed_count} unpushed commit(s).")
            print(f"To push:   cd {wt_path} && git push origin {branch_name}")

        print()
        print(f"Worktree: {wt_path}")
        if has_uncommitted or has_unpushed:
            print(f"Clean up when done: clover test clean {wt_name.removeprefix(TEST_WORKTREE_PREFIX)}")
        else:
            print(f"All clean. Remove worktree with: clover test clean {wt_name.removeprefix(TEST_WORKTREE_PREFIX)}")

    async def list(self) -> None:
        """List active test worktrees."""
        worktrees = await self.worktree_manager.list_worktrees()
        test_worktrees = [
            wt for wt in worktrees
            if wt.path.name.startswith(TEST_WORKTREE_PREFIX)
        ]

        if not test_worktrees:
            print("No active test worktrees.")
            return

        print(f"{'Name':<25} {'Branch':<35} {'Path'}")
        print(f"{'-'*25} {'-'*35} {'-'*40}")
        for wt in test_worktrees:
            name = wt.path.name
            print(f"{name:<25} {wt.branch:<35} {wt.path}")

    async def clean(self, target: Optional[str] = None) -> None:
        """Clean up test worktrees.

        Args:
            target: If given, clean the specific test worktree (PR number or branch name).
                    If None, clean all test worktrees.
        """
        worktrees = await self.worktree_manager.list_worktrees()
        test_worktrees = [
            wt for wt in worktrees
            if wt.path.name.startswith(TEST_WORKTREE_PREFIX)
        ]

        if target:
            # Find the specific worktree
            target_name = target.lstrip("#")
            # Try matching as "test-{target}" directly
            match = None
            for wt in test_worktrees:
                name_suffix = wt.path.name.removeprefix(TEST_WORKTREE_PREFIX)
                if name_suffix == target_name or name_suffix == target_name.replace("/", "-"):
                    match = wt
                    break

            if not match:
                print(f"No test worktree found for '{target}'.")
                if test_worktrees:
                    print("Active test worktrees:")
                    for wt in test_worktrees:
                        print(f"  {wt.path.name}")
                return

            await self._clean_one(match)
        else:
            if not test_worktrees:
                print("No test worktrees to clean.")
                return

            for wt in test_worktrees:
                await self._clean_one(wt)

    async def _clean_one(self, wt) -> None:
        """Clean up a single test worktree, warning if dirty."""
        has_changes = await self.worktree_manager.has_uncommitted_changes(wt.path)
        if has_changes:
            status = await self.worktree_manager.get_uncommitted_status(wt.path)
            print(f"Warning: {wt.path.name} has uncommitted changes:")
            print(status)
            try:
                response = input("Remove anyway? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nSkipped.")
                return
            if response not in ("y", "yes"):
                print("Skipped.")
                return

        print(f"Removing {wt.path.name}...")
        await self.worktree_manager.cleanup_worktree(wt.path)
        print(f"Removed {wt.path.name}")
