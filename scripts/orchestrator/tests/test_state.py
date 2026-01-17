"""Tests for state tracking."""

import json
import pytest
from datetime import datetime, timedelta
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
            old_time = (datetime.utcnow() - timedelta(hours=25)).isoformat()
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
