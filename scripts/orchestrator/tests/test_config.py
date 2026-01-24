"""Tests for configuration loading."""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts.orchestrator import config as config_module
from scripts.orchestrator.config import Config, load_config, find_config_file


class TestConfig:
    """Tests for Config class."""

    def test_from_yaml_minimal(self, tmp_path):
        """Test loading config with minimal required settings."""
        yaml_content = """
github:
  repo: owner/repo
"""
        config_file = tmp_path / "clover.yaml"
        config_file.write_text(yaml_content)

        with patch.object(config_module, "_get_gh_token", return_value="ghp_test123"):
            config = Config.from_yaml(config_file)

        assert config.github_token == "ghp_test123"
        assert config.github_repo == "owner/repo"
        assert config.repo_owner == "owner"
        assert config.repo_name == "repo"

    def test_from_yaml_all_settings(self, tmp_path):
        """Test loading config with all settings."""
        yaml_content = """
github:
  repo: owner/repo
  token: ghp_explicit_token
  label: do-it
  base_branch: develop

daemon:
  poll_interval: 120
  worktree_base: /tmp/worktrees
  max_concurrent: 5
  state_file: /tmp/state.json
  max_turns: 100

review:
  commands:
    - pytest
    - ruff check .

test:
  compose_file: docker-compose.dev.yml
  container: app
"""
        config_file = tmp_path / "clover.yaml"
        config_file.write_text(yaml_content)

        config = Config.from_yaml(config_file)

        assert config.github_token == "ghp_explicit_token"
        assert config.poll_interval == 120
        assert config.worktree_base == Path("/tmp/worktrees")
        assert config.clover_label == "do-it"
        assert config.base_branch == "develop"
        assert config.max_concurrent == 5
        assert config.state_file == Path("/tmp/state.json")
        assert config.max_turns == 100
        assert config.review_commands == ["pytest", "ruff check ."]
        assert config.test.compose_file == "docker-compose.dev.yml"
        assert config.test.container == "app"

    def test_from_yaml_missing_token(self, tmp_path):
        """Test that missing token raises error when gh CLI not available."""
        yaml_content = """
github:
  repo: owner/repo
"""
        config_file = tmp_path / "clover.yaml"
        config_file.write_text(yaml_content)

        with patch.object(config_module, "_get_gh_token", return_value=None):
            with pytest.raises(ValueError, match="github.token"):
                Config.from_yaml(config_file)

    def test_from_yaml_missing_repo(self, tmp_path):
        """Test that missing repo raises error."""
        yaml_content = """
github:
  token: ghp_test123
"""
        config_file = tmp_path / "clover.yaml"
        config_file.write_text(yaml_content)

        with pytest.raises(ValueError, match="github.repo"):
            Config.from_yaml(config_file)

    def test_from_yaml_invalid_repo_format(self, tmp_path):
        """Test that invalid repo format raises error."""
        yaml_content = """
github:
  repo: just-repo-name
  token: ghp_test123
"""
        config_file = tmp_path / "clover.yaml"
        config_file.write_text(yaml_content)

        with pytest.raises(ValueError, match="owner/repo"):
            Config.from_yaml(config_file)

    def test_from_yaml_review_commands_not_list(self, tmp_path):
        """Test that non-list review commands raises error."""
        yaml_content = """
github:
  repo: owner/repo
  token: ghp_test123

review:
  commands: "pytest"
"""
        config_file = tmp_path / "clover.yaml"
        config_file.write_text(yaml_content)

        with pytest.raises(ValueError, match="review.commands must be a list"):
            Config.from_yaml(config_file)

    def test_from_yaml_env_var_interpolation(self, tmp_path):
        """Test environment variable interpolation in YAML values."""
        yaml_content = """
github:
  repo: owner/repo
  token: ${TEST_GITHUB_TOKEN}
"""
        config_file = tmp_path / "clover.yaml"
        config_file.write_text(yaml_content)

        with patch.dict("os.environ", {"TEST_GITHUB_TOKEN": "ghp_from_env"}):
            config = Config.from_yaml(config_file)

        assert config.github_token == "ghp_from_env"

    def test_defaults(self, tmp_path):
        """Test that defaults are set correctly."""
        yaml_content = """
github:
  repo: owner/repo
  token: ghp_test123
"""
        config_file = tmp_path / "clover.yaml"
        config_file.write_text(yaml_content)

        config = Config.from_yaml(config_file)

        assert config.poll_interval == 60
        assert config.clover_label == "clover"
        assert config.max_concurrent == 2
        assert config.max_turns == 50
        assert config.review_commands == []
        assert config.base_branch is None  # Auto-detect


class TestFindConfigFile:
    """Tests for find_config_file function."""

    def test_find_in_current_dir(self, tmp_path):
        """Test finding config in current directory."""
        config_file = tmp_path / "clover.yaml"
        config_file.write_text("github:\n  repo: test/repo")

        result = find_config_file(tmp_path)
        assert result == config_file

    def test_find_in_parent_dir(self, tmp_path):
        """Test finding config in parent directory."""
        config_file = tmp_path / "clover.yaml"
        config_file.write_text("github:\n  repo: test/repo")

        subdir = tmp_path / "subdir" / "nested"
        subdir.mkdir(parents=True)

        result = find_config_file(subdir)
        assert result == config_file

    def test_not_found(self, tmp_path):
        """Test when config file is not found."""
        subdir = tmp_path / "empty"
        subdir.mkdir()

        result = find_config_file(subdir)
        assert result is None


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_success(self, tmp_path):
        """Test loading config successfully."""
        yaml_content = """
github:
  repo: owner/repo
  token: ghp_test123
"""
        config_file = tmp_path / "clover.yaml"
        config_file.write_text(yaml_content)

        config = load_config(tmp_path)

        assert config.github_repo == "owner/repo"

    def test_load_not_found(self, tmp_path):
        """Test error when config not found."""
        subdir = tmp_path / "empty"
        subdir.mkdir()

        with pytest.raises(ValueError, match="clover.yaml not found"):
            load_config(subdir)
