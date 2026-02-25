"""Tests for state tracking."""

import json
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from ..state import State, WorkItem, WorkItemType, WorkItemStatus


class TestWorkItem:
    """Tests for WorkItem class."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        item = WorkItem(
            item_type=WorkItemType.ISSUE,
            number=42,
            status=WorkItemStatus.IN_PROGRESS,
            worktree_path="/tmp/issue-42",
            branch_name="clover/issue-42",
            started_at="2024-01-01T00:00:00",
        )

        d = item.to_dict()

        assert d["item_type"] == "issue"
        assert d["number"] == 42
        assert d["status"] == "in_progress"
        assert d["worktree_path"] == "/tmp/issue-42"
        assert d["branch_name"] == "clover/issue-42"
        assert d["started_at"] == "2024-01-01T00:00:00"

    def test_from_dict(self):
        """Test creation from dictionary."""
        d = {
            "item_type": "pr_review",
            "number": 7,
            "status": "completed",
            "worktree_path": None,
            "completed_at": "2024-01-01T01:00:00",
        }

        item = WorkItem.from_dict(d)

        assert item.item_type == WorkItemType.PR_REVIEW
        assert item.number == 7
        assert item.status == WorkItemStatus.COMPLETED
        assert item.completed_at == "2024-01-01T01:00:00"


class TestState:
    """Tests for State class."""

    def test_fresh_state(self):
        """Test creating state with no existing file."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state = State(state_file)

            assert len(state.work_items) == 0
            assert state.get_in_progress_count() == 0

    def test_mark_in_progress(self):
        """Test marking an item as in progress."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state = State(state_file)

            item = state.mark_in_progress(
                WorkItemType.ISSUE,
                42,
                worktree_path="/tmp/issue-42",
                branch_name="clover/issue-42",
            )

            assert item.status == WorkItemStatus.IN_PROGRESS
            assert item.started_at is not None
            assert state.is_in_progress(WorkItemType.ISSUE, 42)
            assert state.is_processing(WorkItemType.ISSUE, 42)
            assert state.get_in_progress_count() == 1

    def test_mark_completed(self):
        """Test marking an item as completed."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state = State(state_file)

            state.mark_in_progress(WorkItemType.ISSUE, 42)
            state.mark_completed(WorkItemType.ISSUE, 42)

            item = state.get_item(WorkItemType.ISSUE, 42)
            assert item.status == WorkItemStatus.COMPLETED
            assert item.completed_at is not None
            assert not state.is_in_progress(WorkItemType.ISSUE, 42)
            # Still "processing" to prevent re-processing
            assert state.is_processing(WorkItemType.ISSUE, 42)

    def test_mark_failed(self):
        """Test marking an item as failed."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state = State(state_file)

            state.mark_in_progress(WorkItemType.PR_REVIEW, 7)
            state.mark_failed(WorkItemType.PR_REVIEW, 7, "Something went wrong")

            item = state.get_item(WorkItemType.PR_REVIEW, 7)
            assert item.status == WorkItemStatus.FAILED
            assert item.error_message == "Something went wrong"

    def test_clear_item(self):
        """Test clearing an item to allow re-processing."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state = State(state_file)

            state.mark_in_progress(WorkItemType.ISSUE, 42)
            state.mark_completed(WorkItemType.ISSUE, 42)
            state.clear_item(WorkItemType.ISSUE, 42)

            assert not state.is_processing(WorkItemType.ISSUE, 42)
            assert state.get_item(WorkItemType.ISSUE, 42) is None

    def test_persistence(self):
        """Test that state persists across instances."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"

            # Create and modify state
            state1 = State(state_file)
            state1.mark_in_progress(WorkItemType.ISSUE, 42)
            state1.mark_completed(WorkItemType.ISSUE, 42)

            # Create new instance from same file
            state2 = State(state_file)

            assert state2.is_processing(WorkItemType.ISSUE, 42)
            item = state2.get_item(WorkItemType.ISSUE, 42)
            assert item.status == WorkItemStatus.COMPLETED

    def test_cleanup_stale_items(self):
        """Test cleaning up stale in-progress items."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state = State(state_file)

            # Create an item with old started_at
            old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
            state.work_items["issue:42"] = WorkItem(
                item_type=WorkItemType.ISSUE,
                number=42,
                status=WorkItemStatus.IN_PROGRESS,
                started_at=old_time,
            )

            # Create a recent item
            state.mark_in_progress(WorkItemType.ISSUE, 43)

            # Cleanup with 24 hour max age
            cleaned = state.cleanup_stale_items(max_age_hours=24)

            assert cleaned == 1
            assert state.get_item(WorkItemType.ISSUE, 42) is None
            assert state.is_in_progress(WorkItemType.ISSUE, 43)

    def test_different_item_types_independent(self):
        """Test that different item types are tracked independently."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state = State(state_file)

            # Same number but different types
            state.mark_in_progress(WorkItemType.ISSUE, 42)
            state.mark_in_progress(WorkItemType.PR_REVIEW, 42)
            state.mark_in_progress(WorkItemType.PR_MERGE, 42)

            assert state.get_in_progress_count() == 3
            assert state.is_in_progress(WorkItemType.ISSUE, 42)
            assert state.is_in_progress(WorkItemType.PR_REVIEW, 42)
            assert state.is_in_progress(WorkItemType.PR_MERGE, 42)

            # Complete one
            state.mark_completed(WorkItemType.ISSUE, 42)

            assert state.get_in_progress_count() == 2
            assert not state.is_in_progress(WorkItemType.ISSUE, 42)
            assert state.is_in_progress(WorkItemType.PR_REVIEW, 42)


class TestPipelineState:
    """Tests for pipeline tracking fields on WorkItem."""

    def test_pipeline_fields_default(self):
        """Test that pipeline fields default to None/0."""
        item = WorkItem(
            item_type=WorkItemType.ISSUE,
            number=42,
            status=WorkItemStatus.IN_PROGRESS,
        )
        assert item.pipeline_step is None
        assert item.pipeline_step_cycle == 0

    def test_pipeline_fields_to_dict(self):
        """Test pipeline fields are serialized."""
        item = WorkItem(
            item_type=WorkItemType.ISSUE,
            number=42,
            status=WorkItemStatus.IN_PROGRESS,
            pipeline_step="code_review",
            pipeline_step_cycle=2,
        )
        d = item.to_dict()
        assert d["pipeline_step"] == "code_review"
        assert d["pipeline_step_cycle"] == 2

    def test_pipeline_fields_from_dict(self):
        """Test pipeline fields are deserialized."""
        d = {
            "item_type": "issue",
            "number": 42,
            "status": "in_progress",
            "pipeline_step": "security_review",
            "pipeline_step_cycle": 1,
        }
        item = WorkItem.from_dict(d)
        assert item.pipeline_step == "security_review"
        assert item.pipeline_step_cycle == 1

    def test_pipeline_fields_from_dict_missing(self):
        """Test backward compat: missing pipeline fields default properly."""
        d = {
            "item_type": "issue",
            "number": 42,
            "status": "in_progress",
        }
        item = WorkItem.from_dict(d)
        assert item.pipeline_step is None
        assert item.pipeline_step_cycle == 0

    def test_pipeline_fields_persist(self):
        """Test pipeline fields survive save/load cycle."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"

            state1 = State(state_file)
            item = state1.mark_in_progress(WorkItemType.ISSUE, 42)
            item.pipeline_step = "code_review"
            item.pipeline_step_cycle = 1
            state1._dirty = True
            state1._save()

            state2 = State(state_file)
            loaded = state2.get_item(WorkItemType.ISSUE, 42)
            assert loaded is not None
            assert loaded.pipeline_step == "code_review"
            assert loaded.pipeline_step_cycle == 1


class TestPausedState:
    """Tests for PAUSED status and retry logic."""

    def test_mark_paused(self):
        """Test basic pause with retry metadata."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state = State(state_file)

            state.mark_in_progress(WorkItemType.ISSUE, 42)
            item = state.mark_paused(
                WorkItemType.ISSUE, 42, "Usage limit hit"
            )

            assert item is not None
            assert item.status == WorkItemStatus.PAUSED
            assert item.retry_count == 1
            assert item.next_retry_at is not None
            assert item.error_message == "Usage limit hit"

    def test_mark_paused_max_retries(self):
        """Test that mark_paused returns None after max retries."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state = State(state_file)

            # Use a short backoff schedule
            backoff = [1, 2]

            state.mark_in_progress(WorkItemType.ISSUE, 42)

            # First pause: retry_count -> 1
            item = state.mark_paused(
                WorkItemType.ISSUE, 42, "error 1", backoff_schedule=backoff
            )
            assert item is not None
            assert item.retry_count == 1

            # Second pause: retry_count -> 2
            item = state.mark_paused(
                WorkItemType.ISSUE, 42, "error 2", backoff_schedule=backoff
            )
            assert item is not None
            assert item.retry_count == 2

            # Third pause: exceeds max (2 entries in backoff)
            item = state.mark_paused(
                WorkItemType.ISSUE, 42, "error 3", backoff_schedule=backoff
            )
            assert item is None

    def test_paused_is_processing(self):
        """Test that paused items block normal re-processing."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state = State(state_file)

            state.mark_in_progress(WorkItemType.ISSUE, 42)
            state.mark_paused(WorkItemType.ISSUE, 42, "Usage limit hit")

            # Should be considered "processing" so the poll doesn't re-pick it
            assert state.is_processing(WorkItemType.ISSUE, 42)
            # But not "in progress"
            assert not state.is_in_progress(WorkItemType.ISSUE, 42)

    def test_get_ready_paused_items(self):
        """Test that only paused items past their next_retry_at are returned."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state = State(state_file)

            state.mark_in_progress(WorkItemType.ISSUE, 42)
            # Use very short backoff so next_retry_at is in the past
            state.mark_paused(
                WorkItemType.ISSUE, 42, "error", backoff_schedule=[0]
            )

            ready = state.get_ready_paused_items()
            assert len(ready) == 1
            assert ready[0].number == 42

    def test_get_ready_paused_items_not_ready(self):
        """Test that items with future next_retry_at are excluded."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state = State(state_file)

            state.mark_in_progress(WorkItemType.ISSUE, 42)
            # Use long backoff so next_retry_at is far in the future
            state.mark_paused(
                WorkItemType.ISSUE, 42, "error", backoff_schedule=[86400]
            )

            ready = state.get_ready_paused_items()
            assert len(ready) == 0

    def test_paused_persistence(self):
        """Test that retry metadata survives save/load cycle."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"

            # Create and pause
            state1 = State(state_file)
            state1.mark_in_progress(WorkItemType.ISSUE, 42)
            state1.mark_paused(WorkItemType.ISSUE, 42, "Usage limit")

            # Reload from disk
            state2 = State(state_file)
            item = state2.get_item(WorkItemType.ISSUE, 42)

            assert item is not None
            assert item.status == WorkItemStatus.PAUSED
            assert item.retry_count == 1
            assert item.next_retry_at is not None
            assert item.error_message == "Usage limit"

    def test_paused_survives_reset_in_progress(self):
        """Test that reset_in_progress_items does NOT clear paused items."""
        with TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state = State(state_file)

            # Create one in-progress and one paused item
            state.mark_in_progress(WorkItemType.ISSUE, 42)
            state.mark_in_progress(WorkItemType.ISSUE, 43)
            state.mark_paused(WorkItemType.ISSUE, 43, "Usage limit")

            # Reset in-progress items (simulates daemon restart)
            reset_count = state.reset_in_progress_items()

            assert reset_count == 1  # Only #42 was reset
            assert state.get_item(WorkItemType.ISSUE, 42) is None  # Cleared
            item = state.get_item(WorkItemType.ISSUE, 43)
            assert item is not None
            assert item.status == WorkItemStatus.PAUSED  # Preserved
