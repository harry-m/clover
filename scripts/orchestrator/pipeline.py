"""Pipeline data model for multi-step issue processing.

Defines the structure for processing issues through a series of steps
(implement, code review, security review, browser testing), each with
its own review-fix loop and configurable gates between steps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class StepType(str, Enum):
    """Type of pipeline step."""

    IMPLEMENT = "implement"
    CODE_REVIEW = "code_review"
    SECURITY_REVIEW = "security_review"
    BROWSER_TESTING = "browser_testing"


# Read-only tools for review steps
REVIEW_TOOLS = ["Bash", "Read", "Glob", "Grep"]

# Write-enabled tools for fix/implementation steps
FIX_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "TodoWrite"]

# Playwright MCP tool pattern
PLAYWRIGHT_TOOL_PATTERN = "mcp__playwright__*"


@dataclass
class StepConfig:
    """Configuration for a single pipeline step."""

    step_type: StepType
    name: str
    max_fix_cycles: int = 3
    review_prompt_file: str = ""
    fix_prompt_file: str = ""
    review_tools: list[str] = field(default_factory=lambda: list(REVIEW_TOOLS))
    fix_tools: list[str] = field(default_factory=lambda: list(FIX_TOOLS))


@dataclass
class GateConfig:
    """Configuration for a gate command that runs between pipeline steps."""

    command: str
    name: str


@dataclass
class PipelineConfig:
    """Configuration for the full processing pipeline."""

    steps: list[StepConfig]
    gates: list[GateConfig] = field(default_factory=list)
    gate_max_retries: int = 2


@dataclass
class StepResult:
    """Result from executing a pipeline step."""

    step_type: StepType
    success: bool
    cycles_completed: int = 0
    output: str = ""


@dataclass
class IssueContext:
    """Context passed through the pipeline for the issue being processed."""

    issue_number: int
    issue_title: str
    issue_body: str
    base_branch: str
    worktree_path: Path
    branch_name: str
    dev_server_url: Optional[str] = None


# Pattern matching BLOCKING findings in review output.
# Matches lines like "- **BLOCKING**: ..." or "**BLOCKING**:" at line start.
_BLOCKING_PATTERN = re.compile(
    r"^\s*[-*]*\s*\*{0,2}BLOCKING\*{0,2}\s*:", re.MULTILINE
)


def has_blocking_findings(review_output: str) -> bool:
    """Check if review output contains BLOCKING findings.

    Returns False if the output is empty, contains no BLOCKING markers,
    or the only BLOCKING-related text is "None" or similar negation.

    Args:
        review_output: The text output from a review step.

    Returns:
        True if there are actionable BLOCKING findings.
    """
    if not review_output:
        return False

    matches = _BLOCKING_PATTERN.findall(review_output)
    if not matches:
        return False

    # Check if every BLOCKING line is followed by "None" or similar
    for match in _BLOCKING_PATTERN.finditer(review_output):
        # Get the rest of the line after the match
        line_start = match.end()
        line_end = review_output.find("\n", line_start)
        if line_end == -1:
            line_end = len(review_output)
        rest_of_line = review_output[line_start:line_end].strip()
        # If the content after BLOCKING: is not just "None" or empty, it's real
        if rest_of_line.lower() not in ("", "none", "none.", "n/a", "no blocking issues", "no blocking issues."):
            return True

    return False


def get_default_pipeline(
    browser_available: bool,
    dev_server_url: Optional[str] = None,
    max_review_fix_cycles: int = 3,
) -> PipelineConfig:
    """Return the default pipeline configuration.

    Browser testing step is only included if Playwright MCP is available
    AND a dev server URL is configured.

    Args:
        browser_available: Whether Playwright MCP is available.
        dev_server_url: URL of the development server, if running.
        max_review_fix_cycles: Max review-fix cycles per step.

    Returns:
        Default PipelineConfig.
    """
    steps = [
        StepConfig(
            step_type=StepType.IMPLEMENT,
            name="Implement",
            max_fix_cycles=max_review_fix_cycles,
            fix_prompt_file="implement.md",
        ),
        StepConfig(
            step_type=StepType.CODE_REVIEW,
            name="Code Review",
            max_fix_cycles=max_review_fix_cycles,
            review_prompt_file="code_review.md",
            fix_prompt_file="implement_review.md",
        ),
        StepConfig(
            step_type=StepType.SECURITY_REVIEW,
            name="Security Review",
            max_fix_cycles=max_review_fix_cycles,
            review_prompt_file="security_review.md",
            fix_prompt_file="implement_review.md",
        ),
    ]

    if browser_available and dev_server_url:
        browser_review_tools = list(REVIEW_TOOLS) + [PLAYWRIGHT_TOOL_PATTERN]
        browser_fix_tools = list(FIX_TOOLS) + [PLAYWRIGHT_TOOL_PATTERN]
        steps.append(
            StepConfig(
                step_type=StepType.BROWSER_TESTING,
                name="Browser Testing",
                max_fix_cycles=max_review_fix_cycles,
                review_prompt_file="browser_test.md",
                fix_prompt_file="implement_review.md",
                review_tools=browser_review_tools,
                fix_tools=browser_fix_tools,
            )
        )

    return PipelineConfig(steps=steps)
