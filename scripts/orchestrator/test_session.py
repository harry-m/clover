"""Test session management for Clover."""

from __future__ import annotations

import asyncio
import json
import logging
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
        """
        # If it looks like a PR number, look it up
        if pr_or_branch.isdigit():
            pr_number = int(pr_or_branch)
            pr = await self.github.get_pr(pr_number)
            if pr is None:
                raise ValueError(f"PR #{pr_number} not found")
            return (pr.branch, pr.number, pr.title)

        # Otherwise treat as branch name
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

    async def start(self, pr_or_branch: str) -> TestSession:
        """Start a new test session.

        Args:
            pr_or_branch: PR number or branch name.

        Returns:
            The created test session.

        Raises:
            DockerError: If Docker operations fail.
            FileNotFoundError: If compose file not found.
            ValueError: If PR number not found.
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

        # Check if branch exists
        branch_exists = await self.worktrees.branch_exists(branch_name)

        # Create worktree
        if pr_number:
            logger.info(f"Creating worktree for PR #{pr_number} ({branch_name})...")
        else:
            logger.info(f"Creating worktree for {branch_name}...")
        worktree = await self.worktrees.create_worktree(
            branch_name,
            base_branch=default_branch,
            checkout_existing=branch_exists,
        )

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

    async def stop(self, session_id: str, cleanup_worktree: bool = True) -> None:
        """Stop a test session.

        Args:
            session_id: Session ID to stop.
            cleanup_worktree: Whether to remove the worktree.

        Raises:
            ValueError: If session not found.
        """
        sessions = self._load_sessions()
        if session_id not in sessions:
            raise ValueError(f"Session not found: {session_id}")

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

    async def get_session(self, session_id: str) -> Optional[TestSession]:
        """Get a specific session.

        Args:
            session_id: Session ID.

        Returns:
            Session if found, None otherwise.
        """
        sessions = self._load_sessions()
        return sessions.get(session_id)

    async def attach(self, session_id: Optional[str] = None) -> None:
        """Attach to a test session for interactive Claude.

        This will exec into the container with an interactive shell.

        Args:
            session_id: Session ID to attach to. If None, uses most recent running session.

        Raises:
            ValueError: If session not found or not running.
        """
        from .docker_utils import exec_interactive

        sessions = self._load_sessions()

        if session_id is None:
            # Find most recent running session
            running = [s for s in sessions.values() if s.status == "running"]
            if not running:
                raise ValueError("No running test sessions found")
            session = max(running, key=lambda s: s.started_at)
        else:
            if session_id not in sessions:
                raise ValueError(f"Session not found: {session_id}")
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
        session_id: str,
        follow: bool = True,
        tail: int = 100,
    ) -> asyncio.subprocess.Process:
        """Get logs from a test session.

        Args:
            session_id: Session ID.
            follow: Follow log output.
            tail: Number of lines to show from end.

        Returns:
            Subprocess for reading logs.

        Raises:
            ValueError: If session not found.
        """
        sessions = self._load_sessions()
        if session_id not in sessions:
            raise ValueError(f"Session not found: {session_id}")

        session = sessions[session_id]
        compose = DockerCompose(session.compose_file, session_id)
        return await compose.logs(follow=follow, tail=tail)
