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
        # Resolve worktree_base - default to sibling of repo, not inside it
        if config.worktree_base.is_absolute():
            self.worktree_base = config.worktree_base
        else:
            # Put worktrees as sibling to repo (e.g., ../dashai-worktrees)
            self.worktree_base = self.repo_path.parent / f"{self.repo_path.name}-worktrees"
        # Lock to serialize worktree operations (git config can't handle concurrent writes)
        self._worktree_lock = asyncio.Lock()

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
        """Create a new worktree for a branch, or reuse existing one.

        Args:
            branch_name: Name of the branch to create.
            base_branch: Branch to base the new branch on.
            checkout_existing: If True, checkout existing branch instead of creating new.

        Returns:
            Worktree instance.

        Raises:
            WorktreeError: If worktree creation fails.
        """
        # Serialize worktree operations to avoid git config lock contention
        async with self._worktree_lock:
            return await self._create_worktree_impl(
                branch_name, base_branch, checkout_existing
            )

    async def _create_worktree_impl(
        self,
        branch_name: str,
        base_branch: str,
        checkout_existing: bool,
    ) -> Worktree:
        """Internal implementation of create_worktree (called with lock held)."""
        # Ensure base directory exists
        self.worktree_base.mkdir(parents=True, exist_ok=True)

        # Prune stale worktree references
        await self._run_git("worktree", "prune", check=False)

        # Create worktree path from branch name (replace / with -)
        safe_name = branch_name.replace("/", "-")
        worktree_path = self.worktree_base / safe_name

        # Reuse existing worktree if it exists and is valid
        if worktree_path.exists():
            logger.info(f"Reusing existing worktree at {worktree_path}")
            returncode, commit, _ = await self._run_git(
                "rev-parse", "HEAD", cwd=worktree_path, check=False
            )
            if returncode == 0:
                return Worktree(path=worktree_path, branch=branch_name, commit=commit)
            # Worktree is corrupted - remove and recreate
            logger.warning(f"Worktree at {worktree_path} is corrupted, recreating...")
            shutil.rmtree(worktree_path)
            await self._run_git("worktree", "prune", check=False)

        # Fetch latest from remote to ensure we have the base branch
        await self._run_git("fetch", "origin", base_branch, check=False)

        # Check if the branch is currently checked out in the main repo
        # (git won't allow the same branch in multiple worktrees)
        _, current_branch, _ = await self._run_git(
            "rev-parse", "--abbrev-ref", "HEAD", check=False
        )
        if current_branch.strip() == branch_name:
            logger.info(
                f"Branch {branch_name} is checked out in main repo, "
                f"switching to {base_branch} first"
            )
            await self._run_git("checkout", base_branch, check=False)

        if checkout_existing:
            # Try to fetch from remote first
            await self._run_git("fetch", "origin", branch_name, check=False)

            # Check if branch exists on remote
            _, remote_check, _ = await self._run_git(
                "ls-remote", "--heads", "origin", branch_name, check=False
            )

            if remote_check.strip():
                # Branch exists on remote - checkout from there
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
                # Branch only exists locally - checkout local branch
                await self._run_git(
                    "worktree",
                    "add",
                    str(worktree_path),
                    branch_name,
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
        # Serialize worktree operations to avoid git config lock contention
        async with self._worktree_lock:
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

    async def checkout_pr_branch_writable(
        self, pr_number: int, branch_name: str
    ) -> Worktree:
        """Create a worktree with a local tracking branch that can receive commits.

        Unlike checkout_pr_branch which creates a detached HEAD, this creates
        a proper local branch that tracks the remote branch, allowing commits
        to be made and pushed.

        Args:
            pr_number: PR number (for naming the worktree).
            branch_name: The PR's branch name.

        Returns:
            Worktree instance with a writable local branch.
        """
        # Serialize worktree operations to avoid git config lock contention
        async with self._worktree_lock:
            worktree_name = f"pr-fix-{pr_number}"
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

            # Create worktree with a local tracking branch
            # First create the worktree at the remote branch
            await self._run_git(
                "worktree",
                "add",
                str(worktree_path),
                f"origin/{branch_name}",
            )

            # Create a local tracking branch in the worktree
            await self._run_git(
                "checkout",
                "-B",
                branch_name,
                f"origin/{branch_name}",
                cwd=worktree_path,
            )

            # Get the current commit
            _, commit, _ = await self._run_git("rev-parse", "HEAD", cwd=worktree_path)

            logger.info(
                f"Created writable worktree for PR #{pr_number} at {worktree_path} "
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

    async def has_commits_ahead(self, worktree_path: Path, base_branch: str) -> bool:
        """Check if the current branch has commits ahead of base branch.

        Args:
            worktree_path: Path to the worktree.
            base_branch: Base branch to compare against.

        Returns:
            True if there are commits ahead of base branch.
        """
        _, output, _ = await self._run_git(
            "rev-list", f"origin/{base_branch}..HEAD", "--count",
            cwd=worktree_path, check=False
        )
        try:
            count = int(output.strip())
            return count > 0
        except ValueError:
            return False

    async def has_uncommitted_changes(self, worktree_path: Path) -> bool:
        """Check if there are uncommitted changes in the worktree.

        Args:
            worktree_path: Path to the worktree.

        Returns:
            True if there are uncommitted changes (staged or unstaged).
        """
        # Check for any changes (staged or unstaged)
        _, output, _ = await self._run_git(
            "status", "--porcelain",
            cwd=worktree_path, check=False
        )
        return bool(output.strip())

    async def get_uncommitted_status(self, worktree_path: Path) -> str:
        """Get a summary of uncommitted changes.

        Args:
            worktree_path: Path to the worktree.

        Returns:
            Git status output showing uncommitted changes.
        """
        _, output, _ = await self._run_git(
            "status", "--short",
            cwd=worktree_path, check=False
        )
        return output

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

    async def branch_exists(self, branch_name: str) -> bool:
        """Check if a branch exists locally or on remote.

        Args:
            branch_name: Name of the branch to check.

        Returns:
            True if branch exists locally or on origin.
        """
        # Check local branches
        returncode, output, _ = await self._run_git(
            "branch", "--list", branch_name, check=False
        )
        if returncode == 0 and output.strip():
            return True

        # Check remote branches
        returncode, _, _ = await self._run_git(
            "ls-remote", "--heads", "origin", branch_name, check=False
        )
        if returncode != 0:
            return False

        _, output, _ = await self._run_git(
            "ls-remote", "--heads", "origin", branch_name
        )
        return bool(output.strip())

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
