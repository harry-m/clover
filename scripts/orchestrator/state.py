"""State tracking for in-progress work.

Persists state to a JSON file to survive restarts and prevent
duplicate processing of the same issue/PR.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class WorkItemType(str, Enum):
    """Type of work item being processed."""

    ISSUE = "issue"
    PR_REVIEW = "pr_review"
    PR_MERGE = "pr_merge"


class WorkItemStatus(str, Enum):
    """Status of a work item."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


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
            "last_updated": datetime.utcnow().isoformat(),
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
        """Check if an item is currently being processed or was recently completed."""
        key = self._make_key(item_type, number)
        item = self.work_items.get(key)

        if item is None:
            return False

        # Consider it "processing" if in progress or completed
        # This prevents re-processing completed items
        return item.status in (WorkItemStatus.IN_PROGRESS, WorkItemStatus.COMPLETED)

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
            started_at=datetime.utcnow().isoformat(),
        )

        self.work_items[key] = item
        self._dirty = True
        self._save()

        logger.info(f"Marked {item_type.value} #{number} as in progress")
        return item

    def mark_completed(self, item_type: WorkItemType, number: int) -> None:
        """Mark an item as completed."""
        key = self._make_key(item_type, number)
        item = self.work_items.get(key)

        if item is None:
            logger.warning(f"Cannot mark unknown item {key} as completed")
            return

        item.status = WorkItemStatus.COMPLETED
        item.completed_at = datetime.utcnow().isoformat()
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
        item.completed_at = datetime.utcnow().isoformat()
        item.error_message = error_message
        self._dirty = True
        self._save()

        logger.warning(f"Marked {item_type.value} #{number} as failed: {error_message}")

    def clear_item(self, item_type: WorkItemType, number: int) -> None:
        """Remove an item from state (allows re-processing)."""
        key = self._make_key(item_type, number)
        if key in self.work_items:
            del self.work_items[key]
            self._dirty = True
            self._save()
            logger.info(f"Cleared {item_type.value} #{number} from state")

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

    def cleanup_stale_items(self, max_age_hours: int = 24) -> int:
        """Remove items that have been in progress for too long.

        This handles cases where the daemon crashed while processing.

        Args:
            max_age_hours: Maximum hours an item can be in progress.

        Returns:
            Number of items cleaned up.
        """
        now = datetime.utcnow()
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
