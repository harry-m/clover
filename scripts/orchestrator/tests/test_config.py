"""Tests for configuration loading."""

import pytest
from pathlib import Path
from unittest.mock import patch

from scripts.orchestrator import config as config_module
from scripts.orchestrator.config import Config


class TestConfig:
    """Tests for Config class."""

    def test_from_env_minimal(self):
        """Test loading config with minimal required settings."""
        env = {
            "GITHUB_TOKEN": "ghp_test123",
            "GITHUB_REPO": "owner/repo",
        }

        config = Config.from_env(env)

        assert config.github_token == "ghp_test123"
        assert config.github_repo == "owner/repo"
        assert config.repo_owner == "owner"
        assert config.repo_name == "repo"

    def test_from_env_all_settings(self):
        """Test loading config with all settings."""
        env = {
            "GITHUB_TOKEN": "ghp_test123",
            "GITHUB_REPO": "owner/repo",
            "POLL_INTERVAL": "120",
            "WORKTREE_BASE": "/tmp/worktrees",
            "CLOVER_LABEL": "do-it",
            "MAX_CONCURRENT": "5",
            "STATE_FILE": "/tmp/state.json",
            "MAX_TURNS": "100",
            "AUTO_MERGE_ENABLED": "false",
            "MERGE_COMMENT_TRIGGER": "/ship-it",
            "PRE_MERGE_COMMANDS": '["pytest", "ruff check ."]',
        }

        config = Config.from_env(env)

        assert config.poll_interval == 120
        assert config.worktree_base == Path("/tmp/worktrees")
        assert config.clover_label == "do-it"
        assert config.max_concurrent == 5
        assert config.state_file == Path("/tmp/state.json")
        assert config.max_turns == 100
        assert config.auto_merge_enabled is False
        assert config.merge_comment_trigger == "/ship-it"
        assert config.pre_merge_commands == ["pytest", "ruff check ."]

    def test_from_env_missing_token(self):
        """Test that missing token raises error."""
        env = {
            "GITHUB_REPO": "owner/repo",
        }

        # Mock _get_gh_token to return None (no gh CLI auth)
        with patch.object(config_module, "_get_gh_token", return_value=None):
            with pytest.raises(ValueError, match="GITHUB_TOKEN"):
                Config.from_env(env)

    def test_from_env_missing_repo(self):
        """Test that missing repo raises error."""
        env = {
            "GITHUB_TOKEN": "ghp_test123",
        }

        with pytest.raises(ValueError, match="GITHUB_REPO"):
            Config.from_env(env)

    def test_from_env_invalid_repo_format(self):
        """Test that invalid repo format raises error."""
        env = {
            "GITHUB_TOKEN": "ghp_test123",
            "GITHUB_REPO": "just-repo-name",
        }

        with pytest.raises(ValueError, match="owner/repo"):
            Config.from_env(env)

    def test_from_env_invalid_json(self):
        """Test that invalid JSON in PRE_MERGE_COMMANDS raises error."""
        env = {
            "GITHUB_TOKEN": "ghp_test123",
            "GITHUB_REPO": "owner/repo",
            "PRE_MERGE_COMMANDS": "not valid json",
        }

        with pytest.raises(ValueError, match="PRE_MERGE_COMMANDS"):
            Config.from_env(env)

    def test_from_env_pre_merge_not_array(self):
        """Test that non-array PRE_MERGE_COMMANDS raises error."""
        env = {
            "GITHUB_TOKEN": "ghp_test123",
            "GITHUB_REPO": "owner/repo",
            "PRE_MERGE_COMMANDS": '{"command": "pytest"}',
        }

        with pytest.raises(ValueError, match="JSON array"):
            Config.from_env(env)

    def test_defaults(self):
        """Test that defaults are set correctly."""
        env = {
            "GITHUB_TOKEN": "ghp_test123",
            "GITHUB_REPO": "owner/repo",
        }

        config = Config.from_env(env)

        assert config.poll_interval == 60
        assert config.clover_label == "clover"
        assert config.max_concurrent == 2
        assert config.max_turns == 50
        assert config.auto_merge_enabled is True
        assert config.merge_comment_trigger == "/merge"
        assert config.pre_merge_commands == []
