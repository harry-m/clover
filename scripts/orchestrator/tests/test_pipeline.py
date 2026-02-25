"""Tests for pipeline data model."""

import pytest
from pathlib import Path

from ..pipeline import (
    GateConfig,
    IssueContext,
    PipelineConfig,
    StepConfig,
    StepResult,
    StepType,
    PLAYWRIGHT_TOOL_PATTERN,
    REVIEW_TOOLS,
    FIX_TOOLS,
    get_default_pipeline,
    has_blocking_findings,
)


class TestStepType:
    """Tests for StepType enum."""

    def test_values(self):
        assert StepType.IMPLEMENT == "implement"
        assert StepType.CODE_REVIEW == "code_review"
        assert StepType.SECURITY_REVIEW == "security_review"
        assert StepType.BROWSER_TESTING == "browser_testing"

    def test_string_comparison(self):
        assert StepType.IMPLEMENT == "implement"
        assert StepType("code_review") == StepType.CODE_REVIEW


class TestStepConfig:
    """Tests for StepConfig dataclass."""

    def test_defaults(self):
        step = StepConfig(
            step_type=StepType.CODE_REVIEW,
            name="Code Review",
        )
        assert step.max_fix_cycles == 3
        assert step.review_prompt_file == ""
        assert step.fix_prompt_file == ""
        assert step.review_tools == list(REVIEW_TOOLS)
        assert step.fix_tools == list(FIX_TOOLS)

    def test_custom_tools(self):
        step = StepConfig(
            step_type=StepType.BROWSER_TESTING,
            name="Browser Testing",
            review_tools=["Bash", "Read", PLAYWRIGHT_TOOL_PATTERN],
            fix_tools=["Bash", "Read", "Write", PLAYWRIGHT_TOOL_PATTERN],
        )
        assert PLAYWRIGHT_TOOL_PATTERN in step.review_tools
        assert PLAYWRIGHT_TOOL_PATTERN in step.fix_tools


class TestGateConfig:
    """Tests for GateConfig dataclass."""

    def test_create(self):
        gate = GateConfig(command="pytest", name="Tests")
        assert gate.command == "pytest"
        assert gate.name == "Tests"


class TestPipelineConfig:
    """Tests for PipelineConfig dataclass."""

    def test_defaults(self):
        pipeline = PipelineConfig(steps=[])
        assert pipeline.gates == []
        assert pipeline.gate_max_retries == 2

    def test_with_steps_and_gates(self):
        steps = [
            StepConfig(step_type=StepType.IMPLEMENT, name="Implement"),
            StepConfig(step_type=StepType.CODE_REVIEW, name="Code Review"),
        ]
        gates = [GateConfig(command="pytest", name="Tests")]

        pipeline = PipelineConfig(steps=steps, gates=gates, gate_max_retries=3)
        assert len(pipeline.steps) == 2
        assert len(pipeline.gates) == 1
        assert pipeline.gate_max_retries == 3


class TestStepResult:
    """Tests for StepResult dataclass."""

    def test_create(self):
        result = StepResult(
            step_type=StepType.CODE_REVIEW,
            success=True,
            cycles_completed=2,
            output="All good",
        )
        assert result.success is True
        assert result.cycles_completed == 2

    def test_defaults(self):
        result = StepResult(step_type=StepType.IMPLEMENT, success=False)
        assert result.cycles_completed == 0
        assert result.output == ""


class TestIssueContext:
    """Tests for IssueContext dataclass."""

    def test_create(self):
        ctx = IssueContext(
            issue_number=42,
            issue_title="Add feature",
            issue_body="Please add this.",
            base_branch="main",
            worktree_path=Path("/tmp/wt"),
            branch_name="clover/issue-42",
        )
        assert ctx.issue_number == 42
        assert ctx.dev_server_url is None

    def test_with_dev_url(self):
        ctx = IssueContext(
            issue_number=42,
            issue_title="Add feature",
            issue_body="",
            base_branch="main",
            worktree_path=Path("/tmp/wt"),
            branch_name="clover/issue-42",
            dev_server_url="http://localhost:3000",
        )
        assert ctx.dev_server_url == "http://localhost:3000"


class TestHasBlockingFindings:
    """Tests for has_blocking_findings helper."""

    def test_empty_string(self):
        assert has_blocking_findings("") is False

    def test_no_blocking(self):
        review = "## Summary\nLooks good.\n\n- **SUGGESTION**: Minor improvement"
        assert has_blocking_findings(review) is False

    def test_blocking_found(self):
        review = "- **BLOCKING**: SQL injection in user input handler"
        assert has_blocking_findings(review) is True

    def test_blocking_none(self):
        review = "- **BLOCKING**: None"
        assert has_blocking_findings(review) is False

    def test_blocking_none_dot(self):
        review = "- **BLOCKING**: None."
        assert has_blocking_findings(review) is False

    def test_blocking_no_blocking_issues(self):
        review = "- **BLOCKING**: No blocking issues"
        assert has_blocking_findings(review) is False

    def test_blocking_na(self):
        review = "- **BLOCKING**: N/A"
        assert has_blocking_findings(review) is False

    def test_blocking_with_real_content(self):
        review = (
            "### Findings\n\n"
            "- **BLOCKING**: Missing input validation on the API endpoint\n"
            "- **SUGGESTION**: Consider caching the result\n"
        )
        assert has_blocking_findings(review) is True

    def test_blocking_without_stars(self):
        review = "- BLOCKING: Missing error handling"
        assert has_blocking_findings(review) is True

    def test_mixed_blocking_and_none(self):
        review = (
            "- **BLOCKING**: None\n"
            "- **BLOCKING**: Actually this is a real issue\n"
        )
        assert has_blocking_findings(review) is True

    def test_only_nitpick_and_suggestion(self):
        review = (
            "- **NITPICK**: Rename variable\n"
            "- **SUGGESTION**: Add docstring\n"
        )
        assert has_blocking_findings(review) is False


class TestGetDefaultPipeline:
    """Tests for get_default_pipeline."""

    def test_without_browser(self):
        pipeline = get_default_pipeline(browser_available=False)
        step_types = [s.step_type for s in pipeline.steps]

        assert StepType.IMPLEMENT in step_types
        assert StepType.CODE_REVIEW in step_types
        assert StepType.SECURITY_REVIEW in step_types
        assert StepType.BROWSER_TESTING not in step_types

    def test_with_browser_and_url(self):
        pipeline = get_default_pipeline(
            browser_available=True,
            dev_server_url="http://localhost:3000",
        )
        step_types = [s.step_type for s in pipeline.steps]

        assert StepType.BROWSER_TESTING in step_types

    def test_with_browser_no_url(self):
        """Browser step skipped if no dev server URL."""
        pipeline = get_default_pipeline(
            browser_available=True,
            dev_server_url=None,
        )
        step_types = [s.step_type for s in pipeline.steps]

        assert StepType.BROWSER_TESTING not in step_types

    def test_max_fix_cycles_passed_through(self):
        pipeline = get_default_pipeline(
            browser_available=False,
            max_review_fix_cycles=5,
        )
        for step in pipeline.steps:
            assert step.max_fix_cycles == 5

    def test_browser_step_has_playwright_tools(self):
        pipeline = get_default_pipeline(
            browser_available=True,
            dev_server_url="http://localhost:3000",
        )
        browser_step = next(
            s for s in pipeline.steps
            if s.step_type == StepType.BROWSER_TESTING
        )
        assert PLAYWRIGHT_TOOL_PATTERN in browser_step.review_tools
        assert PLAYWRIGHT_TOOL_PATTERN in browser_step.fix_tools

    def test_implement_step_has_correct_prompt(self):
        pipeline = get_default_pipeline(browser_available=False)
        impl_step = next(
            s for s in pipeline.steps
            if s.step_type == StepType.IMPLEMENT
        )
        assert impl_step.fix_prompt_file == "implement.md"

    def test_step_order(self):
        pipeline = get_default_pipeline(
            browser_available=True,
            dev_server_url="http://localhost:3000",
        )
        step_types = [s.step_type for s in pipeline.steps]

        assert step_types == [
            StepType.IMPLEMENT,
            StepType.CODE_REVIEW,
            StepType.SECURITY_REVIEW,
            StepType.BROWSER_TESTING,
        ]
