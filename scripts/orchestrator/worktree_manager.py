"""Git worktree management for isolated work environments."""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class Worktree:
    """Represents a git worktree."""

    path: Path
    branch: str
    commit: Optional[str] = None


class WorktreeError(Exception):
    """Error during worktree operations."""

    pass


class WorktreeManager:
    """Manages git worktrees for isolated work environments.

    Each issue/PR gets its own worktree, allowing parallel work without
    conflicts. Worktrees share the same git objects but have separate
    working directories.
    """

    def __init__(self, config: Config, repo_path: Optional[Path] = None):
        """Initialize the worktree manager.

        Args:
            config: Orchestrator configuration.
            repo_path: Path to the main repository. Defaults to current directory.
        """
        self.config = config
        self.repo_path = repo_path or Path.cwd()
        self.worktree_base = config.worktree_base

    async def _run_git(
        self, *args: str, cwd: Optional[Path] = None, check: bool = True
    ) -> tuple[int, str, str]:
        """Run a git command asynchronously.

        Args:
            *args: Git command arguments.
            cwd: Working directory. Defaults to repo_path.
            check: Raise exception on non-zero exit code.

        Returns:
            Tuple of (return_code, stdout, stderr).

        Raises:
            WorktreeError: If check=True and command fails.
        """
        cwd = cwd or self.repo_path
        cmd = ["git"] + list(args)

        logger.debug(f"Running: {' '.join(cmd)} in {cwd}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()
        stdout_str = stdout.decode().strip()
        stderr_str = stderr.decode().strip()

        if check and proc.returncode != 0:
            raise WorktreeError(
                f"Git command failed: {' '.join(cmd)}\n"
                f"Exit code: {proc.returncode}\n"
                f"Stderr: {stderr_str}"
            )

        return proc.returncode, stdout_str, stderr_str

    async def create_worktree(
        self,
        branch_name: str,
        base_branch: str = "main",
        checkout_existing: bool = False,
    ) -> Worktree:
        """Create a new worktree for a branch.

        Args:
            branch_name: Name of the branch to create.
            base_branch: Branch to base the new branch on.
            checkout_existing: If True, checkout existing branch instead of creating new.

        Returns:
            Worktree instance.

        Raises:
            WorktreeError: If worktree creation fails.
        """
        # Ensure base directory exists
        self.worktree_base.mkdir(parents=True, exist_ok=True)

        # Create worktree path from branch name (replace / with -)
        safe_name = branch_name.replace("/", "-")
        worktree_path = self.worktree_base / safe_name

        # Check if worktree already exists
        if worktree_path.exists():
            logger.warning(f"Worktree already exists at {worktree_path}, removing")
            await self.cleanup_worktree(worktree_path)

        # Fetch latest from remote to ensure we have the base branch
        await self._run_git("fetch", "origin", base_branch, check=False)

        if checkout_existing:
            # Checkout existing remote branch
            await self._run_git("fetch", "origin", branch_name, check=False)
            await self._run_git(
                "worktree",
                "add",
                str(worktree_path),
                f"origin/{branch_name}",
            )
            # Create local tracking branch
            await self._run_git(
                "checkout",
                "-B",
                branch_name,
                f"origin/{branch_name}",
                cwd=worktree_path,
            )
        else:
            # Create new branch from base
            await self._run_git(
                "worktree",
                "add",
                "-b",
                branch_name,
                str(worktree_path),
                f"origin/{base_branch}",
            )

        # Get the current commit
        _, commit, _ = await self._run_git("rev-parse", "HEAD", cwd=worktree_path)

        logger.info(f"Created worktree at {worktree_path} on branch {branch_name}")

        return Worktree(path=worktree_path, branch=branch_name, commit=commit)

    async def checkout_pr_branch(self, pr_number: int, branch_name: str) -> Worktree:
        """Create a worktree for reviewing a PR.

        Args:
            pr_number: PR number (for naming the worktree).
            branch_name: The PR's branch name.

        Returns:
            Worktree instance.
        """
        worktree_name = f"pr-review-{pr_number}"
        safe_name = worktree_name.replace("/", "-")
        worktree_path = self.worktree_base / safe_name

        # Ensure base directory exists
        self.worktree_base.mkdir(parents=True, exist_ok=True)

        # Check if worktree already exists
        if worktree_path.exists():
            logger.warning(f"Worktree already exists at {worktree_path}, removing")
            await self.cleanup_worktree(worktree_path)

        # Fetch the PR branch
        await self._run_git("fetch", "origin", branch_name)

        # Create worktree at the PR branch
        await self._run_git(
            "worktree",
            "add",
            str(worktree_path),
            f"origin/{branch_name}",
        )

        # Get the current commit
        _, commit, _ = await self._run_git("rev-parse", "HEAD", cwd=worktree_path)

        logger.info(
            f"Created worktree for PR #{pr_number} at {worktree_path} "
            f"on branch {branch_name}"
        )

        return Worktree(path=worktree_path, branch=branch_name, commit=commit)

    async def cleanup_worktree(self, worktree_path: Path) -> None:
        """Remove a worktree and clean up.

        Args:
            worktree_path: Path to the worktree to remove.
        """
        if not worktree_path.exists():
            logger.debug(f"Worktree {worktree_path} does not exist, skipping cleanup")
            return

        try:
            # Remove the worktree from git's tracking
            await self._run_git(
                "worktree", "remove", str(worktree_path), "--force", check=False
            )
        except WorktreeError as e:
            logger.warning(f"Git worktree remove failed: {e}")

        # Force remove the directory if it still exists
        if worktree_path.exists():
            try:
                shutil.rmtree(worktree_path)
                logger.debug(f"Force removed worktree directory {worktree_path}")
            except OSError as e:
                logger.error(f"Failed to remove worktree directory: {e}")

        # Prune worktree references
        await self._run_git("worktree", "prune", check=False)

        logger.info(f"Cleaned up worktree at {worktree_path}")

    async def list_worktrees(self) -> list[Worktree]:
        """List all active worktrees.

        Returns:
            List of Worktree instances.
        """
        _, output, _ = await self._run_git("worktree", "list", "--porcelain")

        worktrees = []
        current_worktree: dict = {}

        for line in output.split("\n"):
            if not line:
                if current_worktree.get("worktree"):
                    path = Path(current_worktree["worktree"])
                    # Skip the main worktree
                    if path != self.repo_path:
                        worktrees.append(
                            Worktree(
                                path=path,
                                branch=current_worktree.get("branch", "").replace(
                                    "refs/heads/", ""
                                ),
                                commit=current_worktree.get("HEAD"),
                            )
                        )
                current_worktree = {}
            elif line.startswith("worktree "):
                current_worktree["worktree"] = line[9:]
            elif line.startswith("HEAD "):
                current_worktree["HEAD"] = line[5:]
            elif line.startswith("branch "):
                current_worktree["branch"] = line[7:]

        # Handle the last worktree if output didn't end with an empty line
        if current_worktree.get("worktree"):
            path = Path(current_worktree["worktree"])
            if path != self.repo_path:
                worktrees.append(
                    Worktree(
                        path=path,
                        branch=current_worktree.get("branch", "").replace(
                            "refs/heads/", ""
                        ),
                        commit=current_worktree.get("HEAD"),
                    )
                )

        return worktrees

    async def push_branch(self, worktree_path: Path, branch_name: str) -> None:
        """Push a branch to origin.

        Args:
            worktree_path: Path to the worktree.
            branch_name: Name of the branch to push.
        """
        await self._run_git(
            "push", "-u", "origin", branch_name, cwd=worktree_path
        )
        logger.info(f"Pushed branch {branch_name} to origin")

    async def delete_remote_branch(self, branch_name: str) -> None:
        """Delete a branch from origin.

        Args:
            branch_name: Name of the branch to delete.
        """
        await self._run_git("push", "origin", "--delete", branch_name, check=False)
        logger.info(f"Deleted remote branch {branch_name}")

    async def get_default_branch(self) -> str:
        """Get the default branch of the repository.

        Returns:
            Name of the default branch (e.g., 'main' or 'master').
        """
        # Try to get from remote
        _, output, _ = await self._run_git(
            "symbolic-ref", "refs/remotes/origin/HEAD", check=False
        )

        if output:
            # Format: refs/remotes/origin/main
            return output.split("/")[-1]

        # Fallback: check if main or master exists
        code, _, _ = await self._run_git(
            "show-ref", "--verify", "refs/remotes/origin/main", check=False
        )
        if code == 0:
            return "main"

        code, _, _ = await self._run_git(
            "show-ref", "--verify", "refs/remotes/origin/master", check=False
        )
        if code == 0:
            return "master"

        # Default to main
        return "main"
