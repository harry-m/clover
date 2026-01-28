"""Configuration loading for the orchestrator."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class TestConfig:
    """Configuration for test sessions."""

    # Path to docker-compose file (relative to repo root)
    compose_file: str = "docker-compose.yml"

    # Container to attach to for interactive Claude sessions
    # If blank, uses "develop" if it exists, otherwise first container
    container: Optional[str] = None


@dataclass
class Config:
    """Orchestrator configuration loaded from clover.yaml."""

    # GitHub settings
    github_token: str
    github_repo: str  # format: owner/repo

    # Base branch for feature branches and PR targets
    # None means auto-detect (repo's default branch)
    base_branch: Optional[str] = None

    # Polling settings
    poll_interval: int = 60  # seconds between polls

    # Worktree settings
    worktree_base: Path = field(default_factory=lambda: Path("./worktrees"))

    # Repository path (where the git repo is located)
    repo_path: Path = field(default_factory=Path.cwd)

    # Label that triggers Clover to work on issues/PRs
    clover_label: str = "clover"

    # Concurrency limits
    max_concurrent: int = 2

    # State file location
    state_file: Path = field(default_factory=lambda: Path("./.clover-state.json"))

    # Claude settings
    max_turns: int = 50

    # Review checks (list of commands to run during PR review)
    review_commands: list[str] = field(default_factory=list)

    # Prompts directory
    prompts_dir: Path = field(
        default_factory=lambda: Path(__file__).parent / "prompts"
    )

    # Test session settings
    test: TestConfig = field(default_factory=TestConfig)

    # Optional setup script to run after worktree creation
    # Path relative to repo root, receives CLOVER_* env vars
    setup_script: Optional[str] = None

    # Custom command to invoke Claude (e.g., "docker exec -it dev claude")
    # If not set, finds "claude" in PATH
    claude_command: Optional[str] = None

    @property
    def repo_owner(self) -> str:
        """Extract owner from github_repo."""
        return self.github_repo.split("/")[0]

    @property
    def repo_name(self) -> str:
        """Extract repo name from github_repo."""
        return self.github_repo.split("/")[1]

    @classmethod
    def from_yaml(cls, yaml_path: Path, repo_path: Optional[Path] = None) -> Config:
        """Load configuration from a YAML file.

        Args:
            yaml_path: Path to the clover.yaml file.
            repo_path: Optional override for repository path.

        Returns:
            Config instance.

        Raises:
            ValueError: If required settings are missing or invalid.
            FileNotFoundError: If the YAML file doesn't exist.
        """
        with open(yaml_path) as f:
            data = yaml.safe_load(f) or {}

        # Interpolate environment variables in string values
        data = _interpolate_env_vars(data)

        # Extract sections
        github = data.get("github", {})
        daemon = data.get("daemon", {})
        review = data.get("review", {})
        test_config = data.get("test", {})

        # Required settings
        github_token = github.get("token")
        if not github_token:
            github_token = _get_gh_token()

        if not github_token:
            raise ValueError(
                "github.token is required in clover.yaml, "
                "or authenticate with `gh auth login`"
            )

        github_repo = github.get("repo")
        if not github_repo:
            raise ValueError("github.repo is required in clover.yaml")

        if "/" not in github_repo:
            raise ValueError("github.repo must be in format 'owner/repo'")

        # Determine repo path
        if repo_path is None:
            repo_path = yaml_path.parent.resolve()
        else:
            repo_path = repo_path.resolve()

        # Optional settings with defaults
        poll_interval = daemon.get("poll_interval", 60)
        worktree_base_str = daemon.get("worktree_base", "./worktrees")
        worktree_base = Path(worktree_base_str)
        clover_label = github.get("label", "clover")
        base_branch = github.get("base_branch")  # None means auto-detect
        max_concurrent = daemon.get("max_concurrent", 2)
        state_file_str = daemon.get("state_file", "./.orchestrator-state.json")
        state_file = Path(state_file_str)
        max_turns = daemon.get("max_turns", 50)

        # Review commands
        review_commands = review.get("commands", [])
        if not isinstance(review_commands, list):
            raise ValueError("review.commands must be a list")

        # Test config
        test = TestConfig(
            compose_file=test_config.get("compose_file", "docker-compose.yml"),
            container=test_config.get("container"),
        )

        # Setup script (optional)
        setup_script = daemon.get("setup_script")

        # Claude command (optional)
        claude_command = daemon.get("claude_command")

        return cls(
            github_token=github_token,
            github_repo=github_repo,
            base_branch=base_branch,
            poll_interval=poll_interval,
            worktree_base=worktree_base,
            repo_path=repo_path,
            clover_label=clover_label,
            max_concurrent=max_concurrent,
            state_file=state_file,
            max_turns=max_turns,
            review_commands=review_commands,
            test=test,
            setup_script=setup_script,
            claude_command=claude_command,
        )


def _interpolate_env_vars(data: Any) -> Any:
    """Recursively interpolate ${VAR} patterns with environment variables.

    Args:
        data: YAML data structure (dict, list, or scalar).

    Returns:
        Data with environment variables interpolated.
    """
    if isinstance(data, dict):
        return {k: _interpolate_env_vars(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_interpolate_env_vars(item) for item in data]
    elif isinstance(data, str):
        # Replace ${VAR} or $VAR patterns
        def replace_var(match):
            var_name = match.group(1) or match.group(2)
            return os.environ.get(var_name, match.group(0))

        # Match ${VAR} or $VAR (but not $$)
        pattern = r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)"
        return re.sub(pattern, replace_var, data)
    else:
        return data


def _get_gh_token() -> Optional[str]:
    """Try to get GitHub token from gh CLI."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def find_config_file(start_path: Optional[Path] = None) -> Optional[Path]:
    """Find clover.yaml by searching up from start_path.

    Args:
        start_path: Directory to start searching from. Defaults to cwd.

    Returns:
        Path to clover.yaml if found, None otherwise.
    """
    if start_path is None:
        start_path = Path.cwd()

    current = start_path.resolve()

    # Search up to root
    while current != current.parent:
        config_path = current / "clover.yaml"
        if config_path.exists():
            return config_path
        current = current.parent

    return None


def load_config(repo_path: Optional[Path] = None) -> Config:
    """Load configuration from clover.yaml.

    Searches for clover.yaml in the repo_path or current directory,
    then walks up the directory tree.

    Args:
        repo_path: Optional path to the repository root.

    Returns:
        Config instance.

    Raises:
        ValueError: If clover.yaml is not found or invalid.
    """
    search_start = repo_path or Path.cwd()
    config_path = find_config_file(search_start)

    if config_path is None:
        raise ValueError(
            f"clover.yaml not found in {search_start} or any parent directory. "
            "Create a clover.yaml file in your repository root."
        )

    return Config.from_yaml(config_path, repo_path=repo_path)
