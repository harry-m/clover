"""Configuration loading for the orchestrator."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .pipeline import GateConfig


# Bundled prompts directory (shipped with Clover)
BUNDLED_PROMPTS_DIR = Path(__file__).parent / "prompts"


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
    state_file: Path = field(default_factory=lambda: Path(".clover-state.json"))

    # Claude settings
    max_turns: int = 50

    # Review checks (list of commands to run during PR review)
    review_commands: list[str] = field(default_factory=list)

    # Bundled prompts directory (shipped with Clover)
    prompts_dir: Path = field(default_factory=lambda: BUNDLED_PROMPTS_DIR)

    # User config directory (~/.clover/<owner>/<repo>/)
    # Used for user prompt overrides and state
    user_config_dir: Optional[Path] = None

    # Test session settings
    test: TestConfig = field(default_factory=TestConfig)

    # Optional setup script to run after worktree creation
    # Path relative to repo root, receives CLOVER_* env vars
    setup_script: Optional[str] = None

    # Custom command to invoke Claude (e.g., "docker exec -it dev claude")
    # If not set, finds "claude" in PATH
    claude_command: Optional[str] = None

    # Pre-PR review-fix cycles (0 to disable)
    max_review_fix_cycles: int = 2

    # Retry backoff schedule for transient failures (list of seconds)
    # Default: 5min, 30min, 2hr, 8hr, 24hr
    retry_backoff: list[int] = field(
        default_factory=lambda: [300, 1800, 7200, 28800, 86400]
    )

    # Pipeline settings
    pipeline_gates: list[GateConfig] = field(default_factory=list)
    pipeline_gate_max_retries: int = 2

    @property
    def repo_owner(self) -> str:
        """Extract owner from github_repo."""
        return self.github_repo.split("/")[0]

    @property
    def repo_name(self) -> str:
        """Extract repo name from github_repo."""
        return self.github_repo.split("/")[1]

    def get_prompt_file(self, name: str) -> Path:
        """Get the path to a prompt file, checking user overrides first.

        User prompts in ~/.clover/<owner>/<repo>/prompts/ take precedence
        over the bundled defaults.

        Args:
            name: Prompt filename (e.g., "implement.md").

        Returns:
            Path to the prompt file.
        """
        if self.user_config_dir:
            user_prompt = self.user_config_dir / "prompts" / name
            if user_prompt.exists():
                return user_prompt
        return self.prompts_dir / name

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

        # Determine repo path (where the git repo lives)
        if repo_path is None:
            repo_path = Path.cwd().resolve()
        else:
            repo_path = repo_path.resolve()

        # User config directory (where the yaml lives)
        user_config_dir = yaml_path.parent.resolve()

        # Optional settings with defaults
        poll_interval = daemon.get("poll_interval", 60)
        worktree_base_str = daemon.get("worktree_base", "./worktrees")
        worktree_base = Path(worktree_base_str)
        clover_label = github.get("label", "clover")
        base_branch = github.get("base_branch")  # None means auto-detect
        max_concurrent = daemon.get("max_concurrent", 2)
        max_turns = daemon.get("max_turns", 50)

        # State file defaults to user config dir
        state_file_str = daemon.get("state_file")
        if state_file_str:
            state_file = Path(state_file_str)
        else:
            state_file = user_config_dir / "state.json"

        # Review commands
        review_commands = review.get("commands", [])
        max_review_fix_cycles = review.get("max_review_fix_cycles", 2)
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

        # Retry backoff schedule
        retry_config = daemon.get("retry", {})
        retry_backoff = retry_config.get("backoff", [300, 1800, 7200, 28800, 86400])
        if not isinstance(retry_backoff, list):
            raise ValueError("daemon.retry.backoff must be a list of integers")

        # Pipeline settings
        pipeline_config = data.get("pipeline", {})
        pipeline_gates_raw = pipeline_config.get("gates", [])
        if not isinstance(pipeline_gates_raw, list):
            raise ValueError("pipeline.gates must be a list")

        pipeline_gates = []
        for gate_data in pipeline_gates_raw:
            if isinstance(gate_data, dict):
                command = gate_data.get("command", "")
                name = gate_data.get("name", command)
                if command:
                    pipeline_gates.append(GateConfig(command=command, name=name))
            elif isinstance(gate_data, str):
                pipeline_gates.append(GateConfig(command=gate_data, name=gate_data))

        # Backward compat: if no pipeline.gates configured, use review.commands
        if not pipeline_gates and review_commands:
            pipeline_gates = [
                GateConfig(command=cmd, name=cmd)
                for cmd in review_commands
            ]

        pipeline_gate_max_retries = pipeline_config.get("gate_max_retries", 2)

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
            user_config_dir=user_config_dir,
            test=test,
            setup_script=setup_script,
            claude_command=claude_command,
            max_review_fix_cycles=max_review_fix_cycles,
            retry_backoff=retry_backoff,
            pipeline_gates=pipeline_gates,
            pipeline_gate_max_retries=pipeline_gate_max_retries,
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


def detect_github_repo(repo_path: Optional[Path] = None) -> Optional[str]:
    """Detect the GitHub owner/repo from a git remote URL.

    Parses the origin remote URL to extract the owner/repo slug.

    Args:
        repo_path: Path to the git repository. Defaults to cwd.

    Returns:
        "owner/repo" string, or None if detection fails.
    """
    if repo_path is None:
        repo_path = Path.cwd()

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            cwd=repo_path,
            timeout=10,
        )
        if result.returncode == 0:
            remote_url = result.stdout.strip()
            match = re.search(
                r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", remote_url
            )
            if match:
                return f"{match.group(1)}/{match.group(2)}"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def get_clover_dir(github_repo: str) -> Path:
    """Get the Clover config directory for a given repo.

    Returns:
        Path like ~/.clover/<owner>/<repo>/
    """
    owner, repo = github_repo.split("/", 1)
    return Path.home() / ".clover" / owner / repo


def find_config_file(repo_path: Optional[Path] = None) -> Optional[Path]:
    """Find clover.yaml for the current repository.

    Detects the GitHub repo from the git remote in repo_path (or cwd),
    then looks for config at ~/.clover/<owner>/<repo>/clover.yaml.

    Args:
        repo_path: Path to the git repository. Defaults to cwd.

    Returns:
        Path to clover.yaml if found, None otherwise.
    """
    if repo_path is None:
        repo_path = Path.cwd()

    github_repo = detect_github_repo(repo_path)
    if not github_repo:
        return None

    config_dir = get_clover_dir(github_repo)
    config_path = config_dir / "clover.yaml"
    if config_path.exists():
        return config_path
    return None


def load_config(repo_path: Optional[Path] = None) -> Config:
    """Load configuration from clover.yaml.

    Detects the GitHub repo from the git remote, then loads config
    from ~/.clover/<owner>/<repo>/clover.yaml.

    Args:
        repo_path: Optional path to the repository root.

    Returns:
        Config instance.

    Raises:
        ValueError: If config not found, repo not detected, or config invalid.
    """
    repo_path = (repo_path or Path.cwd()).resolve()

    github_repo = detect_github_repo(repo_path)
    if not github_repo:
        raise ValueError(
            f"Could not detect GitHub repository from git remote in {repo_path}. "
            "Make sure you're in a git repository with an 'origin' remote "
            "pointing to GitHub."
        )

    config_dir = get_clover_dir(github_repo)
    config_path = config_dir / "clover.yaml"

    if not config_path.exists():
        raise ValueError(
            f"No Clover configuration found at {config_path}. "
            f"Run 'clover init' in your repository to set up Clover."
        )

    return Config.from_yaml(config_path, repo_path=repo_path)
