"""GitHub API integration for watching issues and PRs."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx

from .config import Config

logger = logging.getLogger(__name__)

# GitHub API base URL
GITHUB_API_URL = "https://api.github.com"

# Header that identifies Clover's review comments
REVIEW_COMMENT_HEADER = "## 🤖 Automated Code Review"


@dataclass
class Issue:
    """Represents a GitHub issue."""

    number: int
    title: str
    body: str
    labels: list[str]
    state: str
    created_at: datetime
    user: str

    @classmethod
    def from_api(cls, data: dict) -> Issue:
        """Create from GitHub API response."""
        return cls(
            number=data["number"],
            title=data["title"],
            body=data.get("body") or "",
            labels=[label["name"] for label in data.get("labels", [])],
            state=data["state"],
            created_at=datetime.fromisoformat(
                data["created_at"].replace("Z", "+00:00")
            ),
            user=data["user"]["login"],
        )


@dataclass
class PullRequest:
    """Represents a GitHub pull request."""

    number: int
    title: str
    body: str
    branch: str
    base_branch: str
    state: str
    draft: bool
    mergeable: Optional[bool]
    created_at: datetime
    user: str
    labels: list[str] = field(default_factory=list)
    linked_issue: Optional[int] = None

    @classmethod
    def from_api(cls, data: dict) -> PullRequest:
        """Create from GitHub API response."""
        # Try to extract linked issue from body
        linked_issue = None
        body = data.get("body") or ""
        # Look for "Closes #123" or "Fixes #123" patterns
        import re

        match = re.search(r"(?:closes|fixes|resolves)\s+#(\d+)", body, re.IGNORECASE)
        if match:
            linked_issue = int(match.group(1))

        return cls(
            number=data["number"],
            title=data["title"],
            body=body,
            branch=data["head"]["ref"],
            base_branch=data["base"]["ref"],
            state=data["state"],
            draft=data.get("draft", False),
            mergeable=data.get("mergeable"),
            created_at=datetime.fromisoformat(
                data["created_at"].replace("Z", "+00:00")
            ),
            user=data["user"]["login"],
            labels=[label["name"] for label in data.get("labels", [])],
            linked_issue=linked_issue,
        )


@dataclass
class Comment:
    """Represents a GitHub comment."""

    id: int
    body: str
    user: str
    created_at: datetime

    @classmethod
    def from_api(cls, data: dict) -> Comment:
        """Create from GitHub API response."""
        return cls(
            id=data["id"],
            body=data["body"],
            user=data["user"]["login"],
            created_at=datetime.fromisoformat(
                data["created_at"].replace("Z", "+00:00")
            ),
        )


class GitHubError(Exception):
    """Error from GitHub API."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class RateLimitError(GitHubError):
    """Rate limit exceeded."""

    def __init__(self, reset_at: datetime):
        super().__init__(f"Rate limit exceeded, resets at {reset_at}")
        self.reset_at = reset_at


class GitHubWatcher:
    """Watches GitHub for issues and PRs to process."""

    def __init__(self, config: Config):
        """Initialize the GitHub watcher.

        Args:
            config: Orchestrator configuration.
        """
        self.config = config
        self.repo = config.github_repo
        self._client: Optional[httpx.AsyncClient] = None
        self._rate_limit_remaining: Optional[int] = None
        self._rate_limit_reset: Optional[datetime] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=GITHUB_API_URL,
                headers={
                    "Authorization": f"token {self.config.github_token}",
                    "Accept": "application/vnd.github.v3+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self, method: str, path: str, **kwargs
    ) -> dict | list:
        """Make a request to the GitHub API.

        Args:
            method: HTTP method.
            path: API path (without base URL).
            **kwargs: Additional arguments for httpx.

        Returns:
            JSON response.

        Raises:
            GitHubError: If the request fails.
            RateLimitError: If rate limit is exceeded.
        """
        client = await self._get_client()

        # Check if we should wait for rate limit reset
        if (
            self._rate_limit_remaining is not None
            and self._rate_limit_remaining <= 1
            and self._rate_limit_reset is not None
        ):
            wait_seconds = (self._rate_limit_reset - datetime.utcnow()).total_seconds()
            if wait_seconds > 0:
                logger.warning(f"Rate limit low, waiting {wait_seconds:.0f}s")
                await asyncio.sleep(wait_seconds)

        response = await client.request(method, path, **kwargs)

        # Update rate limit info
        if "X-RateLimit-Remaining" in response.headers:
            self._rate_limit_remaining = int(response.headers["X-RateLimit-Remaining"])
        if "X-RateLimit-Reset" in response.headers:
            self._rate_limit_reset = datetime.fromtimestamp(
                int(response.headers["X-RateLimit-Reset"])
            )

        if response.status_code == 403 and "rate limit" in response.text.lower():
            raise RateLimitError(self._rate_limit_reset or datetime.utcnow())

        if response.status_code >= 400:
            raise GitHubError(
                f"GitHub API error: {response.status_code} - {response.text}",
                status_code=response.status_code,
            )

        return response.json()

    async def get_clover_issues(self) -> list[Issue]:
        """Get issues with the clover label.

        Returns:
            List of issues for Clover to implement.
        """
        label = self.config.clover_label
        path = f"/repos/{self.repo}/issues"
        params = {
            "labels": label,
            "state": "open",
            "sort": "created",
            "direction": "asc",
        }

        try:
            data = await self._request("GET", path, params=params)
            # Filter out pull requests (they show up in issues endpoint too)
            issues = [
                Issue.from_api(item)
                for item in data
                if "pull_request" not in item
            ]
            logger.debug(f"Found {len(issues)} issues with label '{label}'")
            return issues
        except GitHubError as e:
            logger.error(f"Failed to get ready issues: {e}")
            return []

    async def get_open_prs(self) -> list[PullRequest]:
        """Get open pull requests.

        Returns:
            List of open PRs.
        """
        path = f"/repos/{self.repo}/pulls"
        params = {
            "state": "open",
            "sort": "created",
            "direction": "asc",
        }

        try:
            data = await self._request("GET", path, params=params)
            prs = [PullRequest.from_api(item) for item in data]
            # Filter out drafts
            prs = [pr for pr in prs if not pr.draft]
            logger.debug(f"Found {len(prs)} open non-draft PRs")
            return prs
        except GitHubError as e:
            logger.error(f"Failed to get open PRs: {e}")
            return []

    async def get_pr(self, pr_number: int) -> Optional[PullRequest]:
        """Get a specific pull request by number.

        Args:
            pr_number: PR number.

        Returns:
            PullRequest if found, None otherwise.
        """
        path = f"/repos/{self.repo}/pulls/{pr_number}"

        try:
            data = await self._request("GET", path)
            return PullRequest.from_api(data)
        except GitHubError as e:
            logger.error(f"Failed to get PR #{pr_number}: {e}")
            return None

    async def get_issue(self, issue_number: int) -> Optional[Issue]:
        """Get a specific issue by number.

        Args:
            issue_number: Issue number.

        Returns:
            Issue if found, None otherwise.
        """
        path = f"/repos/{self.repo}/issues/{issue_number}"

        try:
            data = await self._request("GET", path)
            return Issue.from_api(data)
        except GitHubError as e:
            logger.error(f"Failed to get issue #{issue_number}: {e}")
            return None

    async def get_pr_comments(self, pr_number: int) -> list[Comment]:
        """Get comments on a pull request.

        Args:
            pr_number: PR number.

        Returns:
            List of comments.
        """
        path = f"/repos/{self.repo}/issues/{pr_number}/comments"

        try:
            data = await self._request("GET", path)
            return [Comment.from_api(item) for item in data]
        except GitHubError as e:
            logger.error(f"Failed to get PR #{pr_number} comments: {e}")
            return []

    async def get_clover_review_comment(self, pr_number: int) -> Optional[Comment]:
        """Get Clover's most recent review comment from a PR.

        Args:
            pr_number: PR number.

        Returns:
            Most recent review comment from Clover, or None if not found.
        """
        comments = await self.get_pr_comments(pr_number)
        review_comments = [
            c for c in comments
            if c.body.strip().startswith(REVIEW_COMMENT_HEADER)
        ]
        if not review_comments:
            return None
        # Return the most recent review comment
        return max(review_comments, key=lambda c: c.created_at)

    async def get_pr_reviews(self, pr_number: int) -> list[dict]:
        """Get reviews on a pull request.

        Args:
            pr_number: PR number.

        Returns:
            List of review data.
        """
        path = f"/repos/{self.repo}/pulls/{pr_number}/reviews"

        try:
            return await self._request("GET", path)
        except GitHubError as e:
            logger.error(f"Failed to get PR #{pr_number} reviews: {e}")
            return []

    async def get_pr_check_status(self, pr_number: int) -> tuple[bool, str]:
        """Get the combined check status for a PR.

        Args:
            pr_number: PR number.

        Returns:
            Tuple of (all_passed, status_description).
        """
        # First get the PR to get the head SHA
        path = f"/repos/{self.repo}/pulls/{pr_number}"

        try:
            pr_data = await self._request("GET", path)
            sha = pr_data["head"]["sha"]
        except GitHubError as e:
            logger.error(f"Failed to get PR #{pr_number}: {e}")
            return False, "Failed to get PR"

        # Get combined status
        path = f"/repos/{self.repo}/commits/{sha}/status"

        try:
            status_data = await self._request("GET", path)
            state = status_data["state"]  # success, failure, pending

            if state == "success":
                return True, "All checks passed"
            elif state == "pending":
                return False, "Checks still running"
            else:
                return False, f"Checks failed: {state}"
        except GitHubError as e:
            logger.error(f"Failed to get check status: {e}")
            return False, f"Failed to get status: {e}"

    async def post_comment(self, issue_or_pr_number: int, body: str) -> None:
        """Post a comment on an issue or PR.

        Args:
            issue_or_pr_number: Issue or PR number.
            body: Comment body.
        """
        path = f"/repos/{self.repo}/issues/{issue_or_pr_number}/comments"

        try:
            await self._request("POST", path, json={"body": body})
            logger.info(f"Posted comment on #{issue_or_pr_number}")
        except GitHubError as e:
            logger.error(f"Failed to post comment: {e}")
            raise

    async def create_pr(
        self,
        branch: str,
        title: str,
        body: str,
        base_branch: Optional[str] = None,
    ) -> PullRequest:
        """Create a pull request.

        Args:
            branch: Head branch name.
            title: PR title.
            body: PR body.
            base_branch: Base branch. Defaults to repo default.

        Returns:
            Created PullRequest.
        """
        if base_branch is None:
            # Get default branch from repo
            repo_data = await self._request("GET", f"/repos/{self.repo}")
            base_branch = repo_data["default_branch"]

        path = f"/repos/{self.repo}/pulls"
        data = {
            "title": title,
            "body": body,
            "head": branch,
            "base": base_branch,
        }

        try:
            result = await self._request("POST", path, json=data)
            pr = PullRequest.from_api(result)
            logger.info(f"Created PR #{pr.number}: {title}")
            return pr
        except GitHubError as e:
            logger.error(f"Failed to create PR: {e}")
            raise

    async def remove_label(self, issue_number: int, label: str) -> None:
        """Remove a label from an issue.

        Args:
            issue_number: Issue number.
            label: Label to remove.
        """
        path = f"/repos/{self.repo}/issues/{issue_number}/labels/{label}"

        try:
            await self._request("DELETE", path)
            logger.info(f"Removed label '{label}' from #{issue_number}")
        except GitHubError as e:
            # 404 is ok, label might not exist
            if e.status_code != 404:
                logger.error(f"Failed to remove label: {e}")
                raise

    async def add_label(self, issue_number: int, label: str) -> None:
        """Add a label to an issue.

        Args:
            issue_number: Issue number.
            label: Label to add.
        """
        path = f"/repos/{self.repo}/issues/{issue_number}/labels"

        try:
            await self._request("POST", path, json={"labels": [label]})
            logger.info(f"Added label '{label}' to #{issue_number}")
        except GitHubError as e:
            logger.error(f"Failed to add label: {e}")
            raise
