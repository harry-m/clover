"""State tracking for in-progress work.

Persists state to a JSON file to survive restarts and prevent
duplicate processing of the same issue/PR.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class WorkItemType(str, Enum):
    """Type of work item being processed."""

    ISSUE = "issue"
    PR_REVIEW = "pr_review"
    PR_FIX = "pr_fix"
    PR_MERGE = "pr_merge"


class WorkItemStatus(str, Enum):
    """Status of a work item."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


# Default backoff schedule for transient failures (in seconds):
# 5min, 30min, 2hr, 8hr, 24hr
DEFAULT_RETRY_BACKOFF = [300, 1800, 7200, 28800, 86400]


@dataclass
class WorkItem:
    """Represents a unit of work being tracked."""

    item_type: WorkItemType
    number: int  # Issue or PR number
    status: WorkItemStatus
    worktree_path: Optional[str] = None
    branch_name: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    # For issues: the PR number that was created
    # For PR reviews: the issue number this PR addresses
    related_number: Optional[int] = None
    # Retry tracking for paused items
    retry_count: int = 0
    next_retry_at: Optional[str] = None
    pause_comment_posted: bool = False

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "item_type": self.item_type.value,
            "number": self.number,
            "status": self.status.value,
            "worktree_path": self.worktree_path,
            "branch_name": self.branch_name,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error_message": self.error_message,
            "related_number": self.related_number,
            "retry_count": self.retry_count,
            "next_retry_at": self.next_retry_at,
            "pause_comment_posted": self.pause_comment_posted,
        }

    @classmethod
    def from_dict(cls, data: dict) -> WorkItem:
        """Create from dictionary."""
        return cls(
            item_type=WorkItemType(data["item_type"]),
            number=data["number"],
            status=WorkItemStatus(data["status"]),
            worktree_path=data.get("worktree_path"),
            branch_name=data.get("branch_name"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            error_message=data.get("error_message"),
            related_number=data.get("related_number"),
            retry_count=data.get("retry_count", 0),
            next_retry_at=data.get("next_retry_at"),
            pause_comment_posted=data.get("pause_comment_posted", False),
        )


@dataclass
class State:
    """Manages persistent state for the orchestrator.

    State is stored as a JSON file and includes:
    - In-progress work items
    - Completed work items (for deduplication)
    - Merged PRs (to avoid re-processing)
    """

    state_file: Path
    work_items: dict[str, WorkItem] = field(default_factory=dict)
    _dirty: bool = field(default=False, repr=False)

    def __post_init__(self):
        """Load existing state from file if it exists."""
        self._load()

    def _make_key(self, item_type: WorkItemType, number: int) -> str:
        """Create a unique key for a work item."""
        return f"{item_type.value}:{number}"

    def _load(self) -> None:
        """Load state from file."""
        if not self.state_file.exists():
            logger.debug(f"No state file found at {self.state_file}, starting fresh")
            return

        try:
            with open(self.state_file) as f:
                data = json.load(f)

            for key, item_data in data.get("work_items", {}).items():
                self.work_items[key] = WorkItem.from_dict(item_data)

            logger.info(f"Loaded {len(self.work_items)} work items from state file")
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load state file: {e}, starting fresh")
            self.work_items = {}

    def _save(self) -> None:
        """Save state to file."""
        if not self._dirty:
            return

        data = {
            "work_items": {
                key: item.to_dict() for key, item in self.work_items.items()
            },
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

        # Ensure parent directory exists
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        # Write atomically
        temp_file = self.state_file.with_suffix(".tmp")
        with open(temp_file, "w") as f:
            json.dump(data, f, indent=2)
        temp_file.replace(self.state_file)

        self._dirty = False
        logger.debug(f"Saved state to {self.state_file}")

    def is_processing(self, item_type: WorkItemType, number: int) -> bool:
        """Check if an item is currently being processed, completed, or failed."""
        key = self._make_key(item_type, number)
        item = self.work_items.get(key)

        if item is None:
            return False

        # Consider it "processing" if in progress, paused, completed, or failed
        # This prevents re-processing items (failed/paused items need manual clearing or retry)
        return item.status in (
            WorkItemStatus.IN_PROGRESS,
            WorkItemStatus.PAUSED,
            WorkItemStatus.COMPLETED,
            WorkItemStatus.FAILED,
        )

    def is_in_progress(self, item_type: WorkItemType, number: int) -> bool:
        """Check if an item is currently in progress (not completed)."""
        key = self._make_key(item_type, number)
        item = self.work_items.get(key)

        if item is None:
            return False

        return item.status == WorkItemStatus.IN_PROGRESS

    def mark_in_progress(
        self,
        item_type: WorkItemType,
        number: int,
        worktree_path: Optional[str] = None,
        branch_name: Optional[str] = None,
    ) -> WorkItem:
        """Mark an item as in progress."""
        key = self._make_key(item_type, number)

        item = WorkItem(
            item_type=item_type,
            number=number,
            status=WorkItemStatus.IN_PROGRESS,
            worktree_path=worktree_path,
            branch_name=branch_name,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        self.work_items[key] = item
        self._dirty = True
        self._save()

        logger.info(f"Marked {item_type.value} #{number} as in progress")
        return item

    def mark_completed(
        self,
        item_type: WorkItemType,
        number: int,
        related_number: Optional[int] = None,
    ) -> None:
        """Mark an item as completed.

        Args:
            item_type: Type of work item.
            number: Issue or PR number.
            related_number: For issues, the PR number created. For PR reviews, the issue number.
        """
        key = self._make_key(item_type, number)
        item = self.work_items.get(key)

        if item is None:
            logger.warning(f"Cannot mark unknown item {key} as completed")
            return

        item.status = WorkItemStatus.COMPLETED
        item.completed_at = datetime.now(timezone.utc).isoformat()
        if related_number is not None:
            item.related_number = related_number
        self._dirty = True
        self._save()

        logger.info(f"Marked {item_type.value} #{number} as completed")

    def mark_failed(
        self, item_type: WorkItemType, number: int, error_message: str
    ) -> None:
        """Mark an item as failed."""
        key = self._make_key(item_type, number)
        item = self.work_items.get(key)

        if item is None:
            logger.warning(f"Cannot mark unknown item {key} as failed")
            return

        item.status = WorkItemStatus.FAILED
        item.completed_at = datetime.now(timezone.utc).isoformat()
        item.error_message = error_message
        self._dirty = True
        self._save()

        logger.warning(f"Marked {item_type.value} #{number} as failed: {error_message}")

    def mark_paused(
        self,
        item_type: WorkItemType,
        number: int,
        error_message: str,
        backoff_schedule: Optional[list[int]] = None,
    ) -> Optional[WorkItem]:
        """Mark an item as paused for retry later.

        Args:
            item_type: Type of work item.
            number: Issue or PR number.
            error_message: Description of the transient error.
            backoff_schedule: List of backoff delays in seconds.
                Defaults to DEFAULT_RETRY_BACKOFF.

        Returns:
            The updated WorkItem if paused successfully, or None if
            max retries have been exceeded.
        """
        if backoff_schedule is None:
            backoff_schedule = DEFAULT_RETRY_BACKOFF

        key = self._make_key(item_type, number)
        item = self.work_items.get(key)

        if item is None:
            logger.warning(f"Cannot mark unknown item {key} as paused")
            return None

        retry_count = item.retry_count + 1

        if retry_count > len(backoff_schedule):
            logger.warning(
                f"Max retries ({len(backoff_schedule)}) exceeded for "
                f"{item_type.value} #{number}"
            )
            return None

        delay_seconds = backoff_schedule[retry_count - 1]
        next_retry = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)

        item.status = WorkItemStatus.PAUSED
        item.error_message = error_message
        item.retry_count = retry_count
        item.next_retry_at = next_retry.isoformat()
        self._dirty = True
        self._save()

        logger.info(
            f"Paused {item_type.value} #{number} (retry {retry_count}/"
            f"{len(backoff_schedule)}, next retry at {item.next_retry_at})"
        )
        return item

    def get_ready_paused_items(self) -> list[WorkItem]:
        """Get paused items that are ready for retry.

        Returns:
            List of paused items whose next_retry_at has passed.
        """
        now = datetime.now(timezone.utc)
        ready = []

        for item in self.work_items.values():
            if item.status != WorkItemStatus.PAUSED:
                continue
            if item.next_retry_at is None:
                continue
            retry_at = datetime.fromisoformat(item.next_retry_at)
            if retry_at <= now:
                ready.append(item)

        return ready

    def clear_item(self, item_type: WorkItemType, number: int) -> None:
        """Remove an item from state (allows re-processing)."""
        key = self._make_key(item_type, number)
        if key in self.work_items:
            del self.work_items[key]
            self._dirty = True
            self._save()
            logger.info(f"Cleared {item_type.value} #{number} from state")

    def clear_all(self) -> int:
        """Remove all items from state (blank slate).

        Returns:
            Number of items cleared.
        """
        count = len(self.work_items)
        if count > 0:
            self.work_items.clear()
            self._dirty = True
            self._save()
            logger.info(f"Cleared all {count} items from state")
        return count

    def get_in_progress_count(self) -> int:
        """Get count of items currently in progress."""
        return sum(
            1
            for item in self.work_items.values()
            if item.status == WorkItemStatus.IN_PROGRESS
        )

    def get_item(self, item_type: WorkItemType, number: int) -> Optional[WorkItem]:
        """Get a work item by type and number."""
        key = self._make_key(item_type, number)
        return self.work_items.get(key)

    def reset_in_progress_items(self) -> int:
        """Reset all in-progress items so they can be resumed.

        This should be called on daemon startup to allow resuming
        work that was interrupted when the daemon was killed.

        Returns:
            Number of items reset.
        """
        keys_to_remove = []

        for key, item in self.work_items.items():
            if item.status == WorkItemStatus.IN_PROGRESS:
                keys_to_remove.append(key)
                logger.info(
                    f"Resetting in-progress item {key} for resumption"
                )

        for key in keys_to_remove:
            del self.work_items[key]

        if keys_to_remove:
            self._dirty = True
            self._save()

        return len(keys_to_remove)

    def cleanup_stale_items(self, max_age_hours: int = 24) -> int:
        """Remove items that have been in progress for too long.

        This handles cases where the daemon crashed while processing.

        Args:
            max_age_hours: Maximum hours an item can be in progress.

        Returns:
            Number of items cleaned up.
        """
        now = datetime.now(timezone.utc)
        stale_keys = []

        for key, item in self.work_items.items():
            if item.status != WorkItemStatus.IN_PROGRESS:
                continue

            if item.started_at:
                started = datetime.fromisoformat(item.started_at)
                age_hours = (now - started).total_seconds() / 3600

                if age_hours > max_age_hours:
                    stale_keys.append(key)
                    logger.warning(
                        f"Cleaning up stale item {key} "
                        f"(in progress for {age_hours:.1f} hours)"
                    )

        for key in stale_keys:
            del self.work_items[key]

        if stale_keys:
            self._dirty = True
            self._save()

        return len(stale_keys)
