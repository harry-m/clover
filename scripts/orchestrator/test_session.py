"""Test session management for Clover."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .docker_utils import (
    DockerCompose,
    DockerError,
    PortManager,
    get_claude_cli_path,
    inject_claude_into_container,
)
from .github_watcher import GitHubWatcher
from .worktree_manager import WorktreeManager

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class TestSession:
    """A test session for manual testing."""

    session_id: str
    branch_name: str
    worktree_path: Path
    compose_file: Path
    container_name: Optional[str] = None
    status: str = "starting"  # starting, running, stopped
    started_at: datetime = field(default_factory=datetime.now)
    ports: dict[str, int] = field(default_factory=dict)
    pr_number: Optional[int] = None
    pr_title: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "session_id": self.session_id,
            "branch_name": self.branch_name,
            "worktree_path": str(self.worktree_path),
            "compose_file": str(self.compose_file),
            "container_name": self.container_name,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "ports": self.ports,
            "pr_number": self.pr_number,
            "pr_title": self.pr_title,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TestSession:
        """Create from dictionary."""
        return cls(
            session_id=data["session_id"],
            branch_name=data["branch_name"],
            worktree_path=Path(data["worktree_path"]),
            compose_file=Path(data["compose_file"]),
            container_name=data.get("container_name"),
            status=data.get("status", "unknown"),
            started_at=datetime.fromisoformat(data.get("started_at", datetime.now().isoformat())),
            ports=data.get("ports", {}),
            pr_number=data.get("pr_number"),
            pr_title=data.get("pr_title"),
        )


class TestSessionManager:
    """Manages test sessions for Clover."""

    def __init__(self, config: "Config"):
        """Initialize the session manager.

        Args:
            config: Clover configuration.
        """
        self.config = config
        self.worktrees = WorktreeManager(config, repo_path=config.repo_path)
        self.github = GitHubWatcher(config)
        self._sessions_file = config.state_file.parent / ".clover-test-sessions.json"

    def _load_sessions(self) -> dict[str, TestSession]:
        """Load sessions from disk."""
        if not self._sessions_file.exists():
            return {}

        try:
            with open(self._sessions_file) as f:
                data = json.load(f)
            return {k: TestSession.from_dict(v) for k, v in data.items()}
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load sessions file: {e}")
            return {}

    def _save_sessions(self, sessions: dict[str, TestSession]) -> None:
        """Save sessions to disk."""
        self._sessions_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._sessions_file, "w") as f:
            json.dump({k: v.to_dict() for k, v in sessions.items()}, f, indent=2)

    def _generate_session_id(self, branch_name: str) -> str:
        """Generate a session ID from branch name.

        Args:
            branch_name: Branch name or issue number.

        Returns:
            Session ID.
        """
        # Normalize branch name for use as project name
        safe_name = re.sub(r"[^a-zA-Z0-9-]", "-", branch_name)
        safe_name = re.sub(r"-+", "-", safe_name).strip("-").lower()
        return f"clover-test-{safe_name}"

    async def _resolve_pr(self, pr_or_branch: str) -> tuple[str, Optional[int], Optional[str]]:
        """Resolve PR info from input.

        Args:
            pr_or_branch: PR number or branch name.

        Returns:
            Tuple of (branch_name, pr_number, pr_title).
            If input is a branch name, pr_number and pr_title are None.

        Raises:
            ValueError: If PR not found or branch doesn't exist.
        """
        # If it looks like a PR number, look it up
        if pr_or_branch.isdigit():
            pr_number = int(pr_or_branch)
            pr = await self.github.get_pr(pr_number)
            if pr is None:
                raise ValueError(f"PR #{pr_number} not found")
            return (pr.branch, pr.number, pr.title)

        # Otherwise treat as branch name - verify it exists
        branch_exists = await self.worktrees.branch_exists(pr_or_branch)
        if not branch_exists:
            raise ValueError(
                f"Branch '{pr_or_branch}' not found. "
                f"Use a PR number or an existing branch name."
            )
        return (pr_or_branch, None, None)

    async def _get_default_branch(self) -> str:
        """Get the base branch name (configured or auto-detected)."""
        if self.config.base_branch:
            return self.config.base_branch
        return await self.worktrees.get_default_branch()

    async def _find_compose_file(self, worktree_path: Path) -> Path:
        """Find the docker-compose file.

        Args:
            worktree_path: Path to worktree.

        Returns:
            Path to compose file.

        Raises:
            FileNotFoundError: If compose file not found.
        """
        compose_path = worktree_path / self.config.test.compose_file
        if not compose_path.exists():
            raise FileNotFoundError(
                f"Docker Compose file not found: {compose_path}\n"
                f"Configure 'test.compose_file' in clover.yaml"
            )
        return compose_path

    async def _run_setup_script(
        self,
        worktree_path: Path,
        branch_name: str,
        pr_number: Optional[int] = None,
    ) -> None:
        """Run setup script if configured.

        Args:
            worktree_path: Path to the worktree directory.
            branch_name: Name of the branch.
            pr_number: PR number if starting from a PR.

        Raises:
            FileNotFoundError: If setup script doesn't exist.
            RuntimeError: If setup script fails.
        """
        if not self.config.setup_script:
            return

        script_path = self.config.repo_path / self.config.setup_script
        if not script_path.exists():
            raise FileNotFoundError(f"Setup script not found: {script_path}")

        default_branch = await self._get_default_branch()

        env = {
            **os.environ,
            "CLOVER_PARENT_REPO": str(self.config.repo_path),
            "CLOVER_WORKTREE": str(worktree_path),
            "CLOVER_BRANCH": branch_name,
            "CLOVER_BASE_BRANCH": default_branch,
            "CLOVER_WORK_TYPE": "test_session",
        }
        if pr_number:
            env["CLOVER_PR_NUMBER"] = str(pr_number)

        logger.info(f"Running setup script: {script_path}")

        # Run through sh for cross-platform compatibility (works with Git Bash on Windows)
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
            output = stdout.decode() if stdout else ""
            raise RuntimeError(f"Setup script failed (exit {process.returncode}):\n{output}")

    async def _get_target_service(self, compose: DockerCompose) -> str:
        """Get the target service for Claude sessions.

        Args:
            compose: Docker Compose wrapper.

        Returns:
            Service name.

        Raises:
            DockerError: If no services found.
        """
        services = await compose.get_services()
        if not services:
            raise DockerError("No services defined in docker-compose.yml")

        # Use configured container if specified
        if self.config.test.container:
            if self.config.test.container in services:
                return self.config.test.container
            logger.warning(
                f"Configured container '{self.config.test.container}' not found, "
                f"using first service"
            )

        # Look for 'develop' service
        if "develop" in services:
            return "develop"

        # Use first service
        return services[0]

    async def start(self, pr_or_branch: str, force: bool = False) -> TestSession:
        """Start a new test session.

        Args:
            pr_or_branch: PR number or branch name.
            force: If True, remove existing worktree even if it has uncommitted changes.

        Returns:
            The created test session.

        Raises:
            DockerError: If Docker operations fail.
            FileNotFoundError: If compose file not found.
            ValueError: If PR number not found.
            WorktreeError: If existing worktree has uncommitted changes (unless force=True).
        """
        branch_name, pr_number, pr_title = await self._resolve_pr(pr_or_branch)
        session_id = self._generate_session_id(branch_name)
        default_branch = await self._get_default_branch()

        # Check if session already exists
        sessions = self._load_sessions()
        if session_id in sessions:
            existing = sessions[session_id]
            if existing.status == "running":
                logger.info(f"Session {session_id} already running")
                return existing

        # Create worktree (branch already validated to exist in _resolve_pr)
        if pr_number:
            logger.info(f"Creating worktree for PR #{pr_number} ({branch_name})...")
        else:
            logger.info(f"Creating worktree for {branch_name}...")
        worktree = await self.worktrees.create_worktree(
            branch_name,
            base_branch=default_branch,
            checkout_existing=True,  # Branch must exist
            force=force,
        )

        # Run setup script if configured (e.g., to copy .env files)
        try:
            await self._run_setup_script(worktree.path, branch_name, pr_number)
        except (FileNotFoundError, RuntimeError) as e:
            # Cleanup worktree on setup failure
            await self.worktrees.cleanup_worktree(worktree.path)
            raise DockerError(f"Setup script failed:\n{e}")

        # Find compose file
        compose_file = await self._find_compose_file(worktree.path)

        # Create session
        session = TestSession(
            session_id=session_id,
            branch_name=branch_name,
            worktree_path=worktree.path,
            compose_file=compose_file,
            pr_number=pr_number,
            pr_title=pr_title,
        )

        # Start Docker Compose
        logger.info("Starting Docker containers...")
        compose = DockerCompose(compose_file, session_id)

        returncode, stdout, stderr = await compose.up(detach=True)
        if returncode != 0:
            # Cleanup worktree on failure
            await self.worktrees.cleanup_worktree(worktree.path)
            raise DockerError(f"Failed to start containers:\n{stderr or stdout}")

        # Get target service and container name
        target_service = await self._get_target_service(compose)
        container_name = await compose.get_container_name(target_service)

        session.container_name = container_name
        session.status = "running"

        # Inject Claude CLI into the container
        if container_name:
            claude_injected = await inject_claude_into_container(container_name)
            if claude_injected:
                logger.info("Claude CLI injected into container")
            else:
                logger.info("Claude CLI not injected (install manually if needed)")

        # Query assigned ports
        port_manager = PortManager(compose_file)
        expected_ports = port_manager.get_expected_ports()
        assigned_ports = {}

        for service, container_ports in expected_ports.items():
            for container_port in container_ports:
                host_port = await compose.port(service, container_port)
                if host_port:
                    key = f"{service}:{container_port}"
                    assigned_ports[key] = host_port
                    logger.info(f"Port {container_port} -> localhost:{host_port}")

        session.ports = assigned_ports

        # Save session
        sessions[session_id] = session
        self._save_sessions(sessions)

        logger.info(f"Test session {session_id} started")
        return session

    async def stop(self, identifier: str, cleanup_worktree: bool = True) -> str:
        """Stop a test session.

        Args:
            identifier: Session ID, PR number, or branch name.
            cleanup_worktree: Whether to remove the worktree.

        Returns:
            The resolved session_id that was stopped.

        Raises:
            ValueError: If session not found.
        """
        session_id = self._resolve_session_identifier(identifier)
        sessions = self._load_sessions()

        session = sessions[session_id]

        # Stop Docker containers
        logger.info("Stopping Docker containers...")
        compose = DockerCompose(session.compose_file, session_id)
        await compose.down(volumes=True)

        # Cleanup worktree
        if cleanup_worktree and session.worktree_path.exists():
            logger.info("Cleaning up worktree...")
            await self.worktrees.cleanup_worktree(session.worktree_path)

        # Update session status
        session.status = "stopped"
        sessions[session_id] = session
        self._save_sessions(sessions)

        logger.info(f"Test session {session_id} stopped")
        return session_id

    async def list_sessions(self) -> list[TestSession]:
        """List all test sessions.

        Returns:
            List of test sessions.
        """
        sessions = self._load_sessions()

        # Update status for each session
        for session in sessions.values():
            if session.status == "running":
                # Check if containers are actually running
                compose = DockerCompose(session.compose_file, session.session_id)
                containers = await compose.ps()
                if not any(c["status"] == "running" for c in containers):
                    session.status = "stopped"

        self._save_sessions(sessions)
        return list(sessions.values())

    def _resolve_session_identifier(self, identifier: str) -> str:
        """Resolve a user-friendly identifier to a session_id.

        Accepts:
        - Full session_id (e.g., "clover-test-feature-foo")
        - PR number (e.g., "123")
        - Branch name (e.g., "feature/foo")

        Args:
            identifier: Session identifier in any of the above formats.

        Returns:
            The resolved session_id.

        Raises:
            ValueError: If no matching session is found.
        """
        sessions = self._load_sessions()

        # 1. Exact session_id match
        if identifier in sessions:
            return identifier

        # 2. PR number match
        if identifier.isdigit():
            pr_number = int(identifier)
            for session_id, session in sessions.items():
                if session.pr_number == pr_number:
                    return session_id

        # 3. Branch name match
        for session_id, session in sessions.items():
            if session.branch_name == identifier:
                return session_id

        # 4. Try generating session_id from identifier (in case it's a branch name)
        generated_id = self._generate_session_id(identifier)
        if generated_id in sessions:
            return generated_id

        raise ValueError(
            f"No session found matching '{identifier}'. "
            f"Use 'clover test list' to see available sessions."
        )

    async def get_session(self, identifier: str) -> Optional[TestSession]:
        """Get a specific session.

        Args:
            identifier: Session ID, PR number, or branch name.

        Returns:
            Session if found, None otherwise.
        """
        sessions = self._load_sessions()
        try:
            session_id = self._resolve_session_identifier(identifier)
            return sessions.get(session_id)
        except ValueError:
            return None

    async def attach(self, identifier: Optional[str] = None) -> None:
        """Attach to a test session for interactive Claude.

        This will exec into the container with an interactive shell.

        Args:
            identifier: Session ID, PR number, or branch name. If None, uses most recent running session.

        Raises:
            ValueError: If session not found or not running.
        """
        from .docker_utils import exec_interactive

        sessions = self._load_sessions()

        if identifier is None:
            # Find most recent running session
            running = [s for s in sessions.values() if s.status == "running"]
            if not running:
                raise ValueError("No running test sessions found")
            session = max(running, key=lambda s: s.started_at)
        else:
            session_id = self._resolve_session_identifier(identifier)
            session = sessions[session_id]

        if session.status != "running":
            raise ValueError(f"Session {session.session_id} is not running")

        if not session.container_name:
            raise ValueError(f"No container found for session {session.session_id}")

        # Check if Claude CLI is available
        claude_path = get_claude_cli_path()
        if claude_path:
            logger.info(f"Claude CLI available at: {claude_path}")
        else:
            logger.warning("Claude CLI not found - you can install it in the container")

        logger.info(f"Attaching to container {session.container_name}...")
        logger.info("Run 'claude' to start an interactive Claude session")

        # This replaces the current process
        await exec_interactive(
            session.container_name,
            command=["bash"],
            workdir="/app",
        )

    async def get_logs(
        self,
        identifier: str,
        follow: bool = True,
        tail: int = 100,
    ) -> asyncio.subprocess.Process:
        """Get logs from a test session.

        Args:
            identifier: Session ID, PR number, or branch name.
            follow: Follow log output.
            tail: Number of lines to show from end.

        Returns:
            Subprocess for reading logs.

        Raises:
            ValueError: If session not found.
        """
        session_id = self._resolve_session_identifier(identifier)
        sessions = self._load_sessions()
        session = sessions[session_id]
        compose = DockerCompose(session.compose_file, session.session_id)
        return await compose.logs(follow=follow, tail=tail)

    async def cleanup_worktree(self, identifier: str, force: bool = False) -> bool:
        """Safely clean up a test session's worktree.

        Performs safety checks before deleting:
        - Checks for uncommitted changes
        - Checks if branch has been pushed to remote
        - Prompts user for confirmation if issues found

        Args:
            identifier: Session ID, PR number, or branch name.
            force: Skip safety checks and delete anyway.

        Returns:
            True if worktree was cleaned up, False if user declined.

        Raises:
            ValueError: If session not found or still running.
        """
        session_id = self._resolve_session_identifier(identifier)
        sessions = self._load_sessions()
        session = sessions[session_id]

        if session.status == "running":
            raise ValueError(
                f"Session is still running. Stop it first with: clover test stop {identifier}"
            )

        worktree_path = session.worktree_path
        if not worktree_path.exists():
            # Already cleaned up, just remove from sessions
            del sessions[session_id]
            self._save_sessions(sessions)
            logger.info(f"Session {session_id} removed (worktree already gone)")
            return True

        issues = []

        if not force:
            # Check for uncommitted changes
            has_changes = await self._check_uncommitted_changes(worktree_path)
            if has_changes:
                issues.append("Has uncommitted changes")

            # Check if branch is pushed to remote
            is_pushed = await self._check_branch_pushed(worktree_path, session.branch_name)
            if not is_pushed:
                issues.append("Branch not pushed to remote")

        if issues and not force:
            print(f"Warning: Worktree at {worktree_path} has issues:")
            for issue in issues:
                print(f"  - {issue}")
            print()
            try:
                response = input("Delete anyway? (yes/no): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return False

            if response not in ("yes", "y"):
                print("Aborted. Worktree preserved.")
                return False

        # Clean up the worktree
        await self.worktrees.cleanup_worktree(worktree_path)

        # Remove session from state
        del sessions[session_id]
        self._save_sessions(sessions)

        logger.info(f"Cleaned up worktree for session {session_id}")
        return True

    async def _check_uncommitted_changes(self, worktree_path: Path) -> bool:
        """Check if worktree has uncommitted changes.

        Args:
            worktree_path: Path to the worktree.

        Returns:
            True if there are uncommitted changes.
        """
        process = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain",
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        return bool(stdout.strip())

    async def _check_branch_pushed(self, worktree_path: Path, branch_name: str) -> bool:
        """Check if the branch has been pushed to remote.

        Args:
            worktree_path: Path to the worktree.
            branch_name: Name of the branch.

        Returns:
            True if branch exists on remote and is up to date.
        """
        # Check if remote tracking branch exists
        process = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--verify", f"origin/{branch_name}",
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        if process.returncode != 0:
            return False  # No remote tracking branch

        # Check if local is ahead of remote
        process = await asyncio.create_subprocess_exec(
            "git", "rev-list", "--count", f"origin/{branch_name}..HEAD",
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            return False

        ahead_count = int(stdout.strip() or 0)
        return ahead_count == 0  # True if not ahead (i.e., pushed)
