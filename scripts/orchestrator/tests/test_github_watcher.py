"""Tests for GitHub watcher."""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from ..config import Config
from ..github_watcher import (
    GitHubWatcher,
    Issue,
    PullRequest,
    Comment,
    GitHubError,
    RateLimitError,
)


@pytest.fixture
def config():
    """Create a test config."""
    return Config(
        github_token="ghp_test123",
        github_repo="owner/repo",
    )


@pytest.fixture
def watcher(config):
    """Create a test watcher."""
    return GitHubWatcher(config)


class TestIssue:
    """Tests for Issue class."""

    def test_from_api(self):
        """Test creating Issue from API response."""
        data = {
            "number": 42,
            "title": "Test Issue",
            "body": "Issue body",
            "labels": [{"name": "bug"}, {"name": "ready"}],
            "state": "open",
            "created_at": "2024-01-01T00:00:00Z",
            "user": {"login": "testuser"},
        }

        issue = Issue.from_api(data)

        assert issue.number == 42
        assert issue.title == "Test Issue"
        assert issue.body == "Issue body"
        assert issue.labels == ["bug", "ready"]
        assert issue.state == "open"
        assert issue.user == "testuser"

    def test_from_api_no_body(self):
        """Test handling None body."""
        data = {
            "number": 42,
            "title": "Test Issue",
            "body": None,
            "labels": [],
            "state": "open",
            "created_at": "2024-01-01T00:00:00Z",
            "user": {"login": "testuser"},
        }

        issue = Issue.from_api(data)
        assert issue.body == ""


class TestPullRequest:
    """Tests for PullRequest class."""

    def test_from_api(self):
        """Test creating PullRequest from API response."""
        data = {
            "number": 7,
            "title": "Test PR",
            "body": "PR body\n\nCloses #42",
            "head": {"ref": "feature-branch"},
            "base": {"ref": "main"},
            "state": "open",
            "draft": False,
            "mergeable": True,
            "created_at": "2024-01-01T00:00:00Z",
            "user": {"login": "testuser"},
            "labels": [{"name": "enhancement"}],
        }

        pr = PullRequest.from_api(data)

        assert pr.number == 7
        assert pr.title == "Test PR"
        assert pr.branch == "feature-branch"
        assert pr.base_branch == "main"
        assert pr.linked_issue == 42
        assert pr.labels == ["enhancement"]

    def test_from_api_no_linked_issue(self):
        """Test PR without linked issue."""
        data = {
            "number": 7,
            "title": "Test PR",
            "body": "Just a PR",
            "head": {"ref": "feature-branch"},
            "base": {"ref": "main"},
            "state": "open",
            "draft": False,
            "created_at": "2024-01-01T00:00:00Z",
            "user": {"login": "testuser"},
        }

        pr = PullRequest.from_api(data)
        assert pr.linked_issue is None

    def test_from_api_fixes_pattern(self):
        """Test detecting 'Fixes #X' pattern."""
        data = {
            "number": 7,
            "title": "Test PR",
            "body": "This fixes #123",
            "head": {"ref": "feature-branch"},
            "base": {"ref": "main"},
            "state": "open",
            "draft": False,
            "created_at": "2024-01-01T00:00:00Z",
            "user": {"login": "testuser"},
        }

        pr = PullRequest.from_api(data)
        assert pr.linked_issue == 123


class TestGitHubWatcher:
    """Tests for GitHubWatcher class."""

    @pytest.mark.asyncio
    async def test_get_ready_issues(self, watcher):
        """Test fetching ready issues."""
        mock_response = [
            {
                "number": 42,
                "title": "Test Issue",
                "body": "Body",
                "labels": [{"name": "ready"}],
                "state": "open",
                "created_at": "2024-01-01T00:00:00Z",
                "user": {"login": "testuser"},
            },
            {
                # This should be filtered out (it's a PR)
                "number": 7,
                "title": "Test PR",
                "body": "Body",
                "labels": [{"name": "ready"}],
                "state": "open",
                "created_at": "2024-01-01T00:00:00Z",
                "user": {"login": "testuser"},
                "pull_request": {"url": "..."},
            },
        ]

        with patch.object(watcher, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_response

            issues = await watcher.get_ready_issues()

            assert len(issues) == 1
            assert issues[0].number == 42

    @pytest.mark.asyncio
    async def test_get_open_prs(self, watcher):
        """Test fetching open PRs."""
        mock_response = [
            {
                "number": 7,
                "title": "Test PR",
                "body": "Body",
                "head": {"ref": "feature"},
                "base": {"ref": "main"},
                "state": "open",
                "draft": False,
                "created_at": "2024-01-01T00:00:00Z",
                "user": {"login": "testuser"},
            },
            {
                # This should be filtered out (it's a draft)
                "number": 8,
                "title": "Draft PR",
                "body": "Body",
                "head": {"ref": "wip"},
                "base": {"ref": "main"},
                "state": "open",
                "draft": True,
                "created_at": "2024-01-01T00:00:00Z",
                "user": {"login": "testuser"},
            },
        ]

        with patch.object(watcher, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_response

            prs = await watcher.get_open_prs()

            assert len(prs) == 1
            assert prs[0].number == 7

    @pytest.mark.asyncio
    async def test_has_merge_comment(self, watcher):
        """Test checking for merge comment."""
        mock_comments = [
            {
                "id": 1,
                "body": "Looks good!",
                "user": {"login": "reviewer"},
                "created_at": "2024-01-01T00:00:00Z",
            },
            {
                "id": 2,
                "body": "/merge",
                "user": {"login": "owner"},
                "created_at": "2024-01-01T01:00:00Z",
            },
        ]

        with patch.object(watcher, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_comments

            has_trigger = await watcher.has_merge_comment(7)

            assert has_trigger is True

    @pytest.mark.asyncio
    async def test_has_merge_comment_not_found(self, watcher):
        """Test when merge comment is not present."""
        mock_comments = [
            {
                "id": 1,
                "body": "Looks good!",
                "user": {"login": "reviewer"},
                "created_at": "2024-01-01T00:00:00Z",
            },
        ]

        with patch.object(watcher, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_comments

            has_trigger = await watcher.has_merge_comment(7)

            assert has_trigger is False

    @pytest.mark.asyncio
    async def test_post_comment(self, watcher):
        """Test posting a comment."""
        with patch.object(watcher, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"id": 123}

            await watcher.post_comment(42, "Test comment")

            mock_req.assert_called_once_with(
                "POST",
                "/repos/owner/repo/issues/42/comments",
                json={"body": "Test comment"},
            )

    @pytest.mark.asyncio
    async def test_create_pr(self, watcher):
        """Test creating a PR."""
        mock_repo = {"default_branch": "main"}
        mock_pr = {
            "number": 7,
            "title": "Test PR",
            "body": "Body",
            "head": {"ref": "feature"},
            "base": {"ref": "main"},
            "state": "open",
            "draft": False,
            "created_at": "2024-01-01T00:00:00Z",
            "user": {"login": "testuser"},
        }

        with patch.object(watcher, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = [mock_repo, mock_pr]

            pr = await watcher.create_pr(
                branch="feature",
                title="Test PR",
                body="Body",
            )

            assert pr.number == 7
            assert mock_req.call_count == 2

    @pytest.mark.asyncio
    async def test_merge_pr(self, watcher):
        """Test merging a PR."""
        with patch.object(watcher, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"merged": True}

            success = await watcher.merge_pr(7)

            assert success is True
            mock_req.assert_called_once()

    @pytest.mark.asyncio
    async def test_merge_pr_failure(self, watcher):
        """Test merge failure."""
        with patch.object(watcher, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = GitHubError("Merge failed", status_code=405)

            success = await watcher.merge_pr(7)

            assert success is False
