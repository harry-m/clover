"""Tests for configuration loading."""

import pytest
from pathlib import Path
from unittest.mock import patch

from scripts.orchestrator import config as config_module
from scripts.orchestrator.config import (
    Config,
    detect_github_repo,
    find_config_file,
    get_clover_dir,
    load_config,
)


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
  claude_command: /custom/claude
  setup_script: scripts/setup.sh

review:
  commands:
    - pytest
    - ruff check .
  max_review_fix_cycles: 3

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
        assert config.claude_command == "/custom/claude"
        assert config.setup_script == "scripts/setup.sh"
        assert config.max_review_fix_cycles == 3
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
        assert config.max_review_fix_cycles == 2
        assert config.claude_command is None
        assert config.setup_script is None

    def test_user_config_dir_set_from_yaml_parent(self, tmp_path):
        """Test that user_config_dir is set to the yaml file's parent."""
        yaml_content = """
github:
  repo: owner/repo
  token: ghp_test123
"""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        config_file = config_dir / "clover.yaml"
        config_file.write_text(yaml_content)

        config = Config.from_yaml(config_file)

        assert config.user_config_dir == config_dir.resolve()

    def test_state_file_defaults_to_config_dir(self, tmp_path):
        """Test that state_file defaults to user_config_dir/state.json."""
        yaml_content = """
github:
  repo: owner/repo
  token: ghp_test123
"""
        config_file = tmp_path / "clover.yaml"
        config_file.write_text(yaml_content)

        config = Config.from_yaml(config_file)

        assert config.state_file == tmp_path.resolve() / "state.json"

    def test_get_prompt_file_bundled(self, tmp_path):
        """Test that get_prompt_file returns bundled prompt by default."""
        yaml_content = """
github:
  repo: owner/repo
  token: ghp_test123
"""
        config_file = tmp_path / "clover.yaml"
        config_file.write_text(yaml_content)

        config = Config.from_yaml(config_file)
        result = config.get_prompt_file("implement.md")

        assert result == config.prompts_dir / "implement.md"

    def test_get_prompt_file_user_override(self, tmp_path):
        """Test that get_prompt_file returns user prompt when it exists."""
        yaml_content = """
github:
  repo: owner/repo
  token: ghp_test123
"""
        config_file = tmp_path / "clover.yaml"
        config_file.write_text(yaml_content)

        # Create user prompt override
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        user_prompt = prompts_dir / "implement.md"
        user_prompt.write_text("Custom prompt")

        config = Config.from_yaml(config_file)
        result = config.get_prompt_file("implement.md")

        assert result == user_prompt


class TestDetectGithubRepo:
    """Tests for detect_github_repo function."""

    def test_https_remote(self, tmp_path):
        """Test detecting repo from HTTPS remote URL."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "https://github.com/owner/repo.git\n"

            result = detect_github_repo(tmp_path)

        assert result == "owner/repo"

    def test_ssh_remote(self, tmp_path):
        """Test detecting repo from SSH remote URL."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "git@github.com:owner/repo.git\n"

            result = detect_github_repo(tmp_path)

        assert result == "owner/repo"

    def test_https_without_git_suffix(self, tmp_path):
        """Test detecting repo from HTTPS URL without .git suffix."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "https://github.com/owner/repo\n"

            result = detect_github_repo(tmp_path)

        assert result == "owner/repo"

    def test_no_remote(self, tmp_path):
        """Test None returned when no origin remote."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""

            result = detect_github_repo(tmp_path)

        assert result is None

    def test_non_github_remote(self, tmp_path):
        """Test None returned for non-GitHub remotes."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "https://gitlab.com/owner/repo.git\n"

            result = detect_github_repo(tmp_path)

        assert result is None


class TestGetCloverDir:
    """Tests for get_clover_dir function."""

    def test_returns_correct_path(self):
        """Test that get_clover_dir returns ~/.clover/<owner>/<repo>/."""
        result = get_clover_dir("owner/repo")
        assert result == Path.home() / ".clover" / "owner" / "repo"


class TestFindConfigFile:
    """Tests for find_config_file function."""

    def test_find_config(self, tmp_path):
        """Test finding config via git remote detection."""
        # Set up ~/.clover/owner/repo/clover.yaml
        config_dir = tmp_path / ".clover" / "owner" / "repo"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "clover.yaml"
        config_file.write_text("github:\n  repo: owner/repo")

        with (
            patch.object(
                config_module, "detect_github_repo", return_value="owner/repo"
            ),
            patch.object(config_module, "get_clover_dir", return_value=config_dir),
        ):
            result = find_config_file(tmp_path)

        assert result == config_file

    def test_not_found_no_repo(self, tmp_path):
        """Test None returned when git repo not detected."""
        with patch.object(
            config_module, "detect_github_repo", return_value=None
        ):
            result = find_config_file(tmp_path)

        assert result is None

    def test_not_found_no_config_file(self, tmp_path):
        """Test None returned when config file doesn't exist."""
        config_dir = tmp_path / ".clover" / "owner" / "repo"

        with (
            patch.object(
                config_module, "detect_github_repo", return_value="owner/repo"
            ),
            patch.object(config_module, "get_clover_dir", return_value=config_dir),
        ):
            result = find_config_file(tmp_path)

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
        config_dir = tmp_path / ".clover" / "owner" / "repo"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "clover.yaml"
        config_file.write_text(yaml_content)

        with (
            patch.object(
                config_module, "detect_github_repo", return_value="owner/repo"
            ),
            patch.object(config_module, "get_clover_dir", return_value=config_dir),
        ):
            config = load_config(tmp_path)

        assert config.github_repo == "owner/repo"

    def test_load_no_repo_detected(self, tmp_path):
        """Test error when git repo cannot be detected."""
        with patch.object(
            config_module, "detect_github_repo", return_value=None
        ):
            with pytest.raises(ValueError, match="Could not detect GitHub repository"):
                load_config(tmp_path)

    def test_load_no_config_file(self, tmp_path):
        """Test error when config file not found."""
        config_dir = tmp_path / ".clover" / "owner" / "repo"

        with (
            patch.object(
                config_module, "detect_github_repo", return_value="owner/repo"
            ),
            patch.object(config_module, "get_clover_dir", return_value=config_dir),
        ):
            with pytest.raises(ValueError, match="No Clover configuration found"):
                load_config(tmp_path)
