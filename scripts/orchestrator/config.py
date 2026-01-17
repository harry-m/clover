"""Configuration loading for the orchestrator."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Config:
    """Orchestrator configuration loaded from environment variables."""

    # GitHub settings
    github_token: str
    github_repo: str  # format: owner/repo

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
    state_file: Path = field(default_factory=lambda: Path("./.orchestrator-state.json"))

    # Claude settings
    max_turns: int = 50

    # Merge settings
    auto_merge_enabled: bool = True
    merge_comment_trigger: str = "/merge"

    # Pre-merge checks (list of commands that must pass)
    pre_merge_commands: list[str] = field(default_factory=list)

    # Prompts directory
    prompts_dir: Path = field(
        default_factory=lambda: Path(__file__).parent / "prompts"
    )

    @property
    def repo_owner(self) -> str:
        """Extract owner from github_repo."""
        return self.github_repo.split("/")[0]

    @property
    def repo_name(self) -> str:
        """Extract repo name from github_repo."""
        return self.github_repo.split("/")[1]

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> Config:
        """Load configuration from environment variables.

        Args:
            env: Optional dict of environment variables. Defaults to os.environ.

        Returns:
            Config instance.

        Raises:
            ValueError: If required environment variables are missing.
        """
        if env is None:
            env = dict(os.environ)

        # Required settings
        github_token = env.get("GITHUB_TOKEN")
        if not github_token:
            # Try to get token from gh CLI
            github_token = _get_gh_token()

        if not github_token:
            raise ValueError(
                "GITHUB_TOKEN environment variable is required, "
                "or authenticate with `gh auth login`"
            )

        github_repo = env.get("GITHUB_REPO")
        if not github_repo:
            raise ValueError("GITHUB_REPO environment variable is required")

        if "/" not in github_repo:
            raise ValueError("GITHUB_REPO must be in format 'owner/repo'")

        # Optional settings with defaults
        poll_interval = int(env.get("POLL_INTERVAL", "60"))
        worktree_base = Path(env.get("WORKTREE_BASE", "./worktrees"))
        repo_path = Path(env.get("REPO_PATH", ".")).resolve()
        clover_label = env.get("CLOVER_LABEL", "clover")
        max_concurrent = int(env.get("MAX_CONCURRENT", "2"))
        state_file = Path(env.get("STATE_FILE", "./.orchestrator-state.json"))
        max_turns = int(env.get("MAX_TURNS", "50"))

        # Merge settings
        auto_merge_enabled = env.get("AUTO_MERGE_ENABLED", "true").lower() == "true"
        merge_comment_trigger = env.get("MERGE_COMMENT_TRIGGER", "/merge")

        # Pre-merge commands (JSON array)
        pre_merge_commands_raw = env.get("PRE_MERGE_COMMANDS", "[]")
        try:
            pre_merge_commands = json.loads(pre_merge_commands_raw)
            if not isinstance(pre_merge_commands, list):
                raise ValueError("PRE_MERGE_COMMANDS must be a JSON array")
        except json.JSONDecodeError as e:
            raise ValueError(f"PRE_MERGE_COMMANDS is not valid JSON: {e}")

        return cls(
            github_token=github_token,
            github_repo=github_repo,
            poll_interval=poll_interval,
            worktree_base=worktree_base,
            repo_path=repo_path,
            clover_label=clover_label,
            max_concurrent=max_concurrent,
            state_file=state_file,
            max_turns=max_turns,
            auto_merge_enabled=auto_merge_enabled,
            merge_comment_trigger=merge_comment_trigger,
            pre_merge_commands=pre_merge_commands,
        )


def _get_gh_token() -> Optional[str]:
    """Try to get GitHub token from gh CLI."""
    import subprocess

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


def load_config() -> Config:
    """Load configuration from .env file and environment variables.

    .env file values are loaded first, then environment variables override them.

    Returns:
        Config instance.
    """
    # Try to load .env file
    env_file = Path(".env")
    env = dict(os.environ)

    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    # Remove quotes if present
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]
                    # Environment variables take precedence
                    if key not in os.environ:
                        env[key] = value

    return Config.from_env(env)
