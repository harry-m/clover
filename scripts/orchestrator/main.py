#!/usr/bin/env python3
"""Main entry point for the Clover daemon."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from .agent_context import AgentContext
from .claude_runner import (
    ClaudeRunner,
    ClaudeRunnerError,
    TransientClaudeError,
    check_playwright_available,
    get_playwright_mcp_config,
)
from .config import Config, load_config
from .github_watcher import GitHubWatcher, Issue, PullRequest
from .output_utils import format_output, format_commit_log_as_summary
from .pipeline import (
    GateConfig,
    IssueContext,
    PipelineConfig,
    StepConfig,
    StepResult,
    StepType,
    get_default_pipeline,
    has_blocking_findings,
)
from .state import State, WorkItemStatus, WorkItemType
from .tui import CloverDisplay, is_tty
from .worktree_manager import WorktreeManager

# GitHub API limits - comments can be up to 65,536 characters
# We use 64,000 to leave room for headers/footers added around dynamic content
GITHUB_COMMENT_MAX_BODY = 64000

# Configure logging - log to both console and file
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Add file handler so errors are preserved even when TUI clears the terminal
_log_file = Path.home() / ".clover" / "clover.log"
_log_file.parent.mkdir(parents=True, exist_ok=True)
_file_handler = logging.FileHandler(_log_file, mode="a")
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)
logging.getLogger().addHandler(_file_handler)


class Orchestrator:
    """Main orchestrator daemon that coordinates all components."""

    def __init__(self, config: Config, display: Optional[CloverDisplay] = None):
        """Initialize the orchestrator.

        Args:
            config: Orchestrator configuration.
            display: Optional TUI display for rich output.
        """
        self.config = config
        self.display = display
        self.state = State(config.state_file)
        self.github = GitHubWatcher(config)
        self.worktrees = WorktreeManager(config, repo_path=config.repo_path)
        self.claude = ClaudeRunner(config)
        self._shutdown = False
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._active_tasks: set[asyncio.Task] = set()

    def _log(self, message: str) -> None:
        """Log a message to both logger and display.

        Args:
            message: Message to log.
        """
        logger.info(message)
        if self.display:
            self.display.log(message)
            # Note: Don't call refresh() - let Rich's automatic timer handle it

    async def _run_setup_script(
        self,
        worktree_path: Path,
        branch_name: str,
        work_type: str,
        number: int,
    ) -> None:
        """Run setup script if configured.

        Args:
            worktree_path: Path to the worktree directory.
            branch_name: Name of the branch.
            work_type: Either "issue" or "pr_review".
            number: Issue or PR number.

        Raises:
            FileNotFoundError: If setup script doesn't exist.
            RuntimeError: If setup script fails.
        """
        if not self.config.setup_script:
            return

        script_path = self.config.repo_path / self.config.setup_script
        if not script_path.exists():
            raise FileNotFoundError(f"Setup script not found: {script_path}")

        env = {
            **os.environ,
            "CLOVER_PARENT_REPO": str(self.config.repo_path),
            "CLOVER_WORKTREE": str(worktree_path),
            "CLOVER_BRANCH": branch_name,
            "CLOVER_BASE_BRANCH": self._default_branch,
            "CLOVER_WORK_TYPE": work_type,
        }
        if work_type == "issue":
            env["CLOVER_ISSUE_NUMBER"] = str(number)
        else:
            env["CLOVER_PR_NUMBER"] = str(number)

        logger.info(f"Running setup script: {script_path}")

        # Run through sh for cross-platform compatibility (works with Git Bash on Windows)
        process = await asyncio.create_subprocess_exec(
            "sh",
            str(script_path),
            cwd=worktree_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(
                f"Setup script failed (exit {process.returncode}): {stdout.decode()}"
            )

        logger.info("Setup script completed successfully")

    async def start(self) -> None:
        """Start the orchestrator daemon."""
        self._log(f"Starting Clover for {self.config.github_repo}")
        self._log(f"Watching for label: {self.config.clover_label}")
        logger.info(f"Poll interval: {self.config.poll_interval}s")
        logger.info(f"Max concurrent: {self.config.max_concurrent}")

        # Reset any in-progress items from previous runs so they can be resumed
        # (the branch detection logic will handle resuming work properly)
        reset = self.state.reset_in_progress_items()
        if reset:
            logger.info(f"Reset {reset} in-progress items for resumption")

        # Get base branch for creating feature branches and PR targets
        if self.config.base_branch:
            self._default_branch = self.config.base_branch
            logger.info(f"Base branch (configured): {self._default_branch}")
        else:
            self._default_branch = await self.worktrees.get_default_branch()
            logger.info(f"Base branch (auto-detected): {self._default_branch}")

        # Main loop
        while not self._shutdown:
            try:
                await self._poll_cycle()
            except Exception as e:
                logger.error(f"Error in poll cycle: {e}", exc_info=True)

            # Wait for next poll interval (interruptible by shutdown event)
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.config.poll_interval,
                )
            except asyncio.TimeoutError:
                pass  # Normal timeout, continue polling

        # Cleanup
        logger.info("Shutting down...")
        await self._cleanup()

    async def stop(self) -> None:
        """Signal the orchestrator to stop."""
        logger.info("Stop requested")
        self._shutdown = True
        self._shutdown_event.set()

        # Cancel active tasks
        for task in self._active_tasks:
            task.cancel()

    async def _cleanup(self) -> None:
        """Clean up resources."""
        await self.github.close()

    async def _poll_cycle(self) -> None:
        """Execute one poll cycle."""
        # Check concurrency limit
        in_progress = self.state.get_in_progress_count()
        available_slots = self.config.max_concurrent - in_progress

        # Retry paused items that are ready before picking up new work
        ready_paused = self.state.get_ready_paused_items()
        for item in ready_paused:
            if available_slots <= 0:
                break
            # Transition to IN_PROGRESS so it holds a concurrency slot
            self.state.mark_in_progress(
                item.item_type,
                item.number,
                worktree_path=item.worktree_path,
                branch_name=item.branch_name,
            )
            # Preserve retry metadata on the newly in-progress item
            current = self.state.get_item(item.item_type, item.number)
            if current:
                current.retry_count = item.retry_count
                current.pause_comment_posted = item.pause_comment_posted
                self.state._dirty = True
                self.state._save()

            self._log(
                f"Retrying paused {item.item_type.value} #{item.number} "
                f"(attempt {item.retry_count})"
            )
            task = asyncio.create_task(self._retry_paused_item(item))
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)
            available_slots -= 1

        if available_slots <= 0:
            logger.debug(
                f"At concurrency limit ({self.config.max_concurrent}/{self.config.max_concurrent})"
            )
            return

        # Check for issues with clover label
        issues = await self.github.get_clover_issues()
        for issue in issues:
            if available_slots <= 0:
                break

            if not self.state.is_processing(WorkItemType.ISSUE, issue.number):
                self._log(f"Found issue #{issue.number}: {issue.title}")
                task = asyncio.create_task(self._process_issue(issue))
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)
                available_slots -= 1

        # Check for PRs needing review (only Clover's PRs or PRs with clover label)
        prs = await self.github.get_open_prs()
        for pr in prs:
            if available_slots <= 0:
                break

            if not self._should_review_pr(pr):
                continue

            if not self.state.is_processing(WorkItemType.PR_REVIEW, pr.number):
                self._log(f"Found PR #{pr.number}: {pr.title}")
                task = asyncio.create_task(self._process_pr_review(pr))
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)
                available_slots -= 1

        # Check for PRs needing fix implementation (have clover-fix and clover-reviewed)
        for pr in prs:
            if available_slots <= 0:
                break

            if not self._should_fix_pr(pr):
                continue

            if not self.state.is_processing(WorkItemType.PR_FIX, pr.number):
                self._log(f"Found PR #{pr.number} for fix: {pr.title}")
                task = asyncio.create_task(self._process_pr_fix(pr))
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)
                available_slots -= 1

    def _should_review_pr(self, pr: PullRequest) -> bool:
        """Check if Clover should review this PR.

        Args:
            pr: Pull request to check.

        Returns:
            True if PR has the clover label.
        """
        return self.config.clover_label in pr.labels

    def _should_fix_pr(self, pr: PullRequest) -> bool:
        """Check if PR needs fix implementation.

        Args:
            pr: Pull request to check.

        Returns:
            True if PR has clover-fix label and has been reviewed.
        """
        has_fix_label = "clover-fix" in pr.labels
        has_reviewed_label = "clover-reviewed" in pr.labels

        if has_fix_label and not has_reviewed_label:
            logger.warning(
                f"PR #{pr.number} has clover-fix label but hasn't been reviewed yet"
            )
            return False

        return has_fix_label and has_reviewed_label

    def _is_transient_error(self, error: Exception) -> bool:
        """Classify whether an error is transient (retryable).

        Returns True for errors that happen during or after Claude
        invocation (usage limits, crashes, timeouts). Returns False
        for setup errors (worktree, git, missing files).

        Args:
            error: The exception to classify.

        Returns:
            True if the error is transient and retryable.
        """
        if isinstance(error, TransientClaudeError):
            return True

        if isinstance(error, ClaudeRunnerError):
            msg = str(error).lower()
            transient_patterns = [
                "timed out",
                "implementation failed",
                "review implementation failed",
                "failed to run claude",
            ]
            return any(pattern in msg for pattern in transient_patterns)

        return False

    async def _handle_failure(
        self,
        item_type: WorkItemType,
        number: int,
        error: Exception,
        agent: Optional[AgentContext],
        retry_count: int = 0,
        pause_comment_posted: bool = False,
    ) -> None:
        """Handle a work item failure, pausing if transient or failing permanently.

        Args:
            item_type: Type of work item.
            number: Issue or PR number.
            error: The exception that caused the failure.
            agent: Optional TUI agent context.
            retry_count: Current retry count (from previous pauses).
            pause_comment_posted: Whether a pause comment was already posted.
        """
        if self._is_transient_error(error):
            paused_item = self.state.mark_paused(
                item_type,
                number,
                str(error),
                backoff_schedule=self.config.retry_backoff,
            )

            if paused_item is not None:
                # Successfully paused for retry
                if agent:
                    agent.mark_paused(str(error)[:100])

                if not pause_comment_posted:
                    paused_item.pause_comment_posted = True
                    self.state._dirty = True
                    self.state._save()
                    try:
                        await self.github.post_comment(
                            number,
                            f"⏸️ Work paused due to a transient error. "
                            f"Will retry automatically at "
                            f"`{paused_item.next_retry_at}`.\n\n"
                            f"Error: {str(error)[:GITHUB_COMMENT_MAX_BODY]}\n\n"
                            f"*— Clover, the Claude Overseer*",
                        )
                    except Exception:
                        pass

                self._log(
                    f"Paused {item_type.value} #{number} "
                    f"(retry {paused_item.retry_count})"
                )
                return

            # max retries exceeded — fall through to permanent failure
            logger.warning(
                f"Max retries exceeded for {item_type.value} #{number}, "
                "marking as permanently failed"
            )

        # Permanent failure
        self.state.mark_failed(item_type, number, str(error))
        if agent:
            agent.mark_failed()
            agent.add_output(f"Error: {str(error)[:100]}")

        try:
            await self.github.post_comment(
                number,
                f"❌ Failed to process this item.\n\n"
                f"Error: {str(error)[:GITHUB_COMMENT_MAX_BODY]}\n\n"
                f"*— Clover, the Claude Overseer*",
            )
        except Exception:
            pass

    async def _ensure_committed(
        self,
        worktree_path: Path,
        context: str,
        agent: Optional[AgentContext] = None,
        fatal: bool = False,
    ) -> bool:
        """Ensure all changes in a worktree are committed.

        Checks for uncommitted changes and asks Claude to commit them.
        If fatal=True and changes remain after retry, raises ClaudeRunnerError.

        Args:
            worktree_path: Path to the worktree.
            context: Human-readable context for log messages.
            agent: Optional TUI agent for output tracking.
            fatal: If True, raise on failure instead of just warning.

        Returns:
            True if worktree should be preserved for inspection
            (fatal failure occurred).

        Raises:
            ClaudeRunnerError: If fatal=True and commit retry fails.
        """
        has_uncommitted = await self.worktrees.has_uncommitted_changes(worktree_path)
        if not has_uncommitted:
            return False

        uncommitted_status = await self.worktrees.get_uncommitted_status(worktree_path)
        logger.warning(
            f"{context}: Claude left uncommitted changes, "
            f"retrying with commit instructions. Files:\n{uncommitted_status}"
        )

        on_output = self.display.get_output_callback(agent) if agent else None
        commit_result = await self.claude.commit_uncommitted_changes(
            uncommitted_status=uncommitted_status,
            context=context,
            cwd=worktree_path,
            on_output=on_output,
        )

        logger.info(
            f"{context}: Commit retry completed. "
            f"Success={commit_result.success}, exit_code={commit_result.exit_code}, "
            f"duration={commit_result.duration_seconds:.1f}s"
        )
        if not commit_result.success:
            logger.warning(
                f"{context}: Commit retry output: {commit_result.output[:500]}"
            )

        still_uncommitted = await self.worktrees.has_uncommitted_changes(worktree_path)
        if still_uncommitted and fatal:
            final_status = await self.worktrees.get_uncommitted_status(worktree_path)
            logger.error(
                f"{context}: Still has uncommitted changes after retry! "
                f"Files:\n{final_status}"
            )
            raise ClaudeRunnerError(
                f"Claude failed to commit changes after retry. "
                f"Worktree preserved for inspection. "
                f"Uncommitted files:\n{final_status[:GITHUB_COMMENT_MAX_BODY]}"
            )

        return False

    async def _run_tests_with_retry(
        self,
        worktree_path: Path,
        context: str,
        agent: Optional[AgentContext] = None,
    ) -> None:
        """Run review commands with retry on failure.

        Runs configured review commands. If they fail, asks Claude to fix
        and retries up to 2 more times.

        Args:
            worktree_path: Path to the worktree.
            context: Human-readable context for log messages.
            agent: Optional TUI agent for output tracking.
        """
        if not self.config.review_commands:
            return

        max_test_retries = 2
        for attempt in range(max_test_retries + 1):
            tests_passed, test_output = await self.claude.run_checks(
                commands=self.config.review_commands,
                cwd=worktree_path,
            )

            if tests_passed:
                logger.info("All tests passed")
                break

            if attempt < max_test_retries:
                logger.warning(
                    f"Tests failed (attempt {attempt + 1}/{max_test_retries + 1}), "
                    "asking Claude to fix..."
                )
                on_output = self.display.get_output_callback(agent) if agent else None
                await self.claude.fix_failing_tests(
                    test_output=test_output,
                    context=context,
                    cwd=worktree_path,
                    on_output=on_output,
                )
                await self._ensure_committed(
                    worktree_path,
                    f"{context} (test fix)",
                    agent=agent,
                )
            else:
                logger.error("Tests still failing after retries")

    async def _retry_paused_item(self, item) -> None:
        """Retry a previously paused work item.

        Args:
            item: The paused WorkItem to retry.
        """
        retry_count = item.retry_count
        pause_comment_posted = item.pause_comment_posted

        try:
            if item.item_type == WorkItemType.ISSUE:
                issue = await self.github.get_issue(item.number)
                if issue is None or self.config.clover_label not in issue.labels:
                    logger.info(
                        f"Issue #{item.number} no longer eligible, "
                        "clearing from state"
                    )
                    self.state.clear_item(item.item_type, item.number)
                    return
                await self._process_issue(
                    issue,
                    retry_count=retry_count,
                    pause_comment_posted=pause_comment_posted,
                )
            elif item.item_type == WorkItemType.PR_REVIEW:
                pr = await self.github.get_pr(item.number)
                if pr is None or not self._should_review_pr(pr):
                    logger.info(
                        f"PR #{item.number} no longer eligible for review, "
                        "clearing from state"
                    )
                    self.state.clear_item(item.item_type, item.number)
                    return
                await self._process_pr_review(
                    pr,
                    retry_count=retry_count,
                    pause_comment_posted=pause_comment_posted,
                )
            elif item.item_type == WorkItemType.PR_FIX:
                pr = await self.github.get_pr(item.number)
                if pr is None or not self._should_fix_pr(pr):
                    logger.info(
                        f"PR #{item.number} no longer eligible for fix, "
                        "clearing from state"
                    )
                    self.state.clear_item(item.item_type, item.number)
                    return
                await self._process_pr_fix(
                    pr,
                    retry_count=retry_count,
                    pause_comment_posted=pause_comment_posted,
                )
            else:
                logger.warning(
                    f"Unknown item type for retry: {item.item_type}"
                )
                self.state.clear_item(item.item_type, item.number)
        except Exception as e:
            logger.error(
                f"Error retrying {item.item_type.value} #{item.number}: {e}"
            )

    async def _process_issue(
        self,
        issue: Issue,
        retry_count: int = 0,
        pause_comment_posted: bool = False,
    ) -> None:
        """Process an issue by implementing it.

        Args:
            issue: Issue to implement.
            retry_count: Current retry count (for pause/resume tracking).
            pause_comment_posted: Whether a pause comment was already posted.
        """
        branch_name = f"clover/issue-{issue.number}"
        worktree = None
        agent: Optional[AgentContext] = None

        # Create agent for TUI tracking
        if self.display:
            agent = self.display.create_agent(
                work_type="issue",
                number=issue.number,
                title=issue.title,
                branch_name=branch_name,
            )

        try:
            # Check if branch already exists (locally or on remote)
            branch_exists = await self.worktrees.branch_exists(branch_name)

            if branch_exists:
                # Branch exists - assume we were working on it, resume
                logger.info(
                    f"Found existing branch {branch_name}, resuming work on issue #{issue.number}"
                )
                checkout_existing = True
            else:
                checkout_existing = False

            # Mark as in progress
            self.state.mark_in_progress(
                WorkItemType.ISSUE,
                issue.number,
                branch_name=branch_name,
            )

            # Create worktree
            worktree = await self.worktrees.create_worktree(
                branch_name,
                base_branch=self._default_branch,
                checkout_existing=checkout_existing,
            )

            self.state.mark_in_progress(
                WorkItemType.ISSUE,
                issue.number,
                worktree_path=str(worktree.path),
                branch_name=branch_name,
            )

            # Run setup script if configured
            await self._run_setup_script(
                worktree.path, branch_name, "issue", issue.number
            )

            # Post start/resume comment
            if checkout_existing:
                await self.github.post_comment(
                    issue.number,
                    "🔄 Resuming work on this issue...\n\n"
                    "*— Clover, the Claude Overseer*",
                )
            else:
                await self.github.post_comment(
                    issue.number,
                    "🚀 Starting work on this issue...\n\n"
                    "*— Clover, the Claude Overseer*",
                )

            # Post resume comment if this was a retry
            if retry_count > 0:
                try:
                    await self.github.post_comment(
                        issue.number,
                        "▶️ Work has resumed after transient failure.\n\n"
                        "*— Clover, the Claude Overseer*",
                    )
                except Exception:
                    pass

            # Build issue context for the pipeline
            dev_server_url = os.environ.get("CLOVER_DEV_URL")
            context = IssueContext(
                issue_number=issue.number,
                issue_title=issue.title,
                issue_body=issue.body,
                base_branch=self._default_branch,
                worktree_path=worktree.path,
                branch_name=branch_name,
                dev_server_url=dev_server_url,
            )

            # Run the pipeline
            try:
                result = await self._run_pipeline(context, agent)
            except ClaudeRunnerError:
                worktree = None  # Prevent cleanup so user can inspect
                raise

            if result:
                self._log(f"Created PR #{result} for issue #{issue.number}")
                if agent:
                    agent.mark_completed()

        except Exception as e:
            logger.error(f"Failed to process issue #{issue.number}: {e}")
            await self._handle_failure(
                WorkItemType.ISSUE,
                issue.number,
                e,
                agent,
                retry_count=retry_count,
                pause_comment_posted=pause_comment_posted,
            )

        finally:
            # Cleanup worktree (but preserve if paused or has uncommitted changes)
            if worktree:
                try:
                    item = self.state.get_item(WorkItemType.ISSUE, issue.number)
                    if item and item.status == WorkItemStatus.PAUSED:
                        logger.info(
                            f"Preserving worktree at {worktree.path} "
                            f"(item is paused for retry)"
                        )
                    else:
                        has_uncommitted = await self.worktrees.has_uncommitted_changes(worktree.path)
                        if has_uncommitted:
                            logger.warning(
                                f"Preserving worktree at {worktree.path} for inspection "
                                f"(has uncommitted changes)"
                            )
                        else:
                            await self.worktrees.cleanup_worktree(worktree.path)
                except Exception as e:
                    logger.warning(f"Failed to cleanup worktree: {e}")
            # Refresh display
            if self.display:
                self.display.refresh()

    async def _run_pipeline(
        self,
        context: IssueContext,
        agent: Optional[AgentContext] = None,
    ) -> Optional[int]:
        """Run the full processing pipeline for an issue.

        Iterates through pipeline steps (implement, code review, security
        review, browser testing), running gates between each step. Creates
        a PR at the end if there are commits.

        Args:
            context: Issue context with all relevant metadata.
            agent: Optional TUI agent for output tracking.

        Returns:
            PR number if a PR was created, None otherwise.
        """
        # Check Playwright availability once at pipeline start
        browser_available = await check_playwright_available()
        if not browser_available:
            logger.info(
                "Playwright MCP not found. Browser testing disabled. "
                "Install with: npm install -g @playwright/mcp"
            )

        # Build pipeline config
        pipeline = get_default_pipeline(
            browser_available=browser_available,
            dev_server_url=context.dev_server_url,
            max_review_fix_cycles=self.config.max_review_fix_cycles,
        )
        # Apply configured gates
        if self.config.pipeline_gates:
            pipeline.gates = self.config.pipeline_gates
        pipeline.gate_max_retries = self.config.pipeline_gate_max_retries

        # Determine MCP config for browser-enabled steps
        mcp_config = get_playwright_mcp_config() if browser_available else None

        # Check pipeline state for resume
        item = self.state.get_item(WorkItemType.ISSUE, context.issue_number)
        resume_from_step = item.pipeline_step if item else None

        total_steps = len(pipeline.steps)
        skipping = resume_from_step is not None
        impl_output = ""

        for step_idx, step in enumerate(pipeline.steps):
            # Handle resume: skip completed steps
            if skipping:
                if step.step_type.value == resume_from_step:
                    skipping = False
                    logger.info(
                        f"Issue #{context.issue_number}: Resuming from step "
                        f"{step.name}"
                    )
                else:
                    logger.info(
                        f"Issue #{context.issue_number}: Skipping completed "
                        f"step {step.name}"
                    )
                    continue

            # Update TUI
            if agent:
                agent.current_step = step.name
                agent.step_index = (step_idx + 1, total_steps)
                agent.step_cycle = None  # Reset between steps

            # Update state for resume tracking
            if item:
                item.pipeline_step = step.step_type.value
                item.pipeline_step_cycle = 0
                self.state._dirty = True
                self.state._save()

            logger.info(
                f"Issue #{context.issue_number}: Pipeline step {step_idx + 1}/"
                f"{total_steps}: {step.name}"
            )

            # Run the step
            step_result = await self._run_step(
                step, context, agent, mcp_config=mcp_config,
            )

            if step.step_type == StepType.IMPLEMENT:
                impl_output = step_result.output

            if not step_result.success:
                logger.warning(
                    f"Issue #{context.issue_number}: Step {step.name} failed, "
                    "continuing to next step"
                )

            # Run gates after each step
            if pipeline.gates:
                await self._run_gates(
                    step, pipeline, context, agent, mcp_config=mcp_config,
                )

        # Create PR
        return await self._run_create_pr(context, impl_output, agent)

    async def _run_step(
        self,
        step: StepConfig,
        context: IssueContext,
        agent: Optional[AgentContext] = None,
        mcp_config: Optional[dict] = None,
    ) -> StepResult:
        """Run a single pipeline step.

        For IMPLEMENT: calls implement_issue then ensures committed.
        For review steps: runs the review-fix loop.

        Args:
            step: Step configuration.
            context: Issue context.
            agent: Optional TUI agent.
            mcp_config: Optional MCP config for Playwright.

        Returns:
            StepResult with success status and output.
        """
        log_context = f"issue #{context.issue_number}: {context.issue_title}"

        if step.step_type == StepType.IMPLEMENT:
            # Implementation step uses the existing implement_issue method
            on_output = self.display.get_output_callback(agent) if agent else None
            result = await self.claude.implement_issue(
                issue_number=context.issue_number,
                issue_title=context.issue_title,
                issue_body=context.issue_body,
                cwd=context.worktree_path,
                on_output=on_output,
            )

            if not result.success:
                raise ClaudeRunnerError(
                    f"Implementation failed: {result.output[:GITHUB_COMMENT_MAX_BODY]}"
                )

            # Ensure all changes are committed
            await self._ensure_committed(
                context.worktree_path, log_context, agent=agent, fatal=True,
            )

            return StepResult(
                step_type=step.step_type,
                success=True,
                output=result.output,
            )
        else:
            # Review steps use the fix loop
            return await self._run_fix_loop(
                step, context, agent, mcp_config=mcp_config,
            )

    async def _run_fix_loop(
        self,
        step: StepConfig,
        context: IssueContext,
        agent: Optional[AgentContext] = None,
        mcp_config: Optional[dict] = None,
    ) -> StepResult:
        """Run the generalized review-fix loop for a pipeline step.

        Each cycle: review (read-only) -> check for blocking findings ->
        fix (write) -> ensure committed -> check if changes were made.

        Args:
            step: Step configuration.
            context: Issue context.
            agent: Optional TUI agent.
            mcp_config: Optional MCP config for Playwright.

        Returns:
            StepResult with cycle count and final output.
        """
        log_context = f"issue #{context.issue_number}: {context.issue_title}"

        # Determine MCP config for this step's tools
        step_mcp = None
        if mcp_config and any("mcp__" in t for t in step.review_tools):
            step_mcp = mcp_config

        # Build the browser context string if dev URL is available
        browser_context = ""
        if context.dev_server_url and step.step_type == StepType.BROWSER_TESTING:
            browser_context = (
                f"\n\nA dev server is running at {context.dev_server_url}. "
                "You can use the Playwright browser tools to navigate and test."
            )

        for cycle in range(step.max_fix_cycles):
            logger.info(
                f"Issue #{context.issue_number}: {step.name} cycle "
                f"{cycle + 1}/{step.max_fix_cycles}"
            )

            # Update TUI with cycle progress
            if agent:
                agent.step_cycle = (cycle + 1, step.max_fix_cycles)

            # Update state for resume
            item = self.state.get_item(WorkItemType.ISSUE, context.issue_number)
            if item:
                item.pipeline_step_cycle = cycle
                self.state._dirty = True
                self.state._save()

            commits_before = await self.worktrees.get_commit_count(
                context.worktree_path, context.base_branch
            )

            # Review phase (read-only, fresh session)
            review_prompt = (
                f"Review the implementation for issue "
                f"#{context.issue_number}: {context.issue_title}\n\n"
                f"{context.issue_body}\n\n"
                f"Run `git diff origin/{context.base_branch}...HEAD` to see "
                f"all changes.{browser_context}"
            )

            system_prompt_file = (
                self.config.get_prompt_file(step.review_prompt_file)
                if step.review_prompt_file
                else None
            )

            on_output = self.display.get_output_callback(agent) if agent else None
            review_result = await self.claude.run(
                prompt=review_prompt,
                cwd=context.worktree_path,
                system_prompt_file=system_prompt_file,
                allowed_tools=step.review_tools,
                on_output=on_output,
                mcp_config=step_mcp,
            )

            if not review_result.success:
                logger.warning(
                    f"Issue #{context.issue_number}: {step.name} review "
                    f"failed, stopping fix loop"
                )
                return StepResult(
                    step_type=step.step_type,
                    success=False,
                    cycles_completed=cycle + 1,
                    output=review_result.output,
                )

            # Check for blocking findings
            if not has_blocking_findings(review_result.output):
                logger.info(
                    f"Issue #{context.issue_number}: {step.name} found no "
                    f"blocking issues after cycle {cycle + 1}"
                )
                return StepResult(
                    step_type=step.step_type,
                    success=True,
                    cycles_completed=cycle + 1,
                    output=review_result.output,
                )

            # Fix phase (write-enabled, fresh session)
            fix_prompt = (
                f"Address the review feedback for issue "
                f"#{context.issue_number}: {context.issue_title}\n\n"
                f"## Review Feedback\n\n{review_result.output}\n\n"
                f"---\n\nInstructions:\n"
                f"1. Address all BLOCKING items — these must be fixed\n"
                f"2. Address SUGGESTION items where you agree they improve the code\n"
                f"3. Skip NITPICK items — do not act on them\n"
                f"4. If you make changes, you MUST commit them with git\n"
                f"5. If no changes are needed, state that explicitly"
            )

            fix_system_prompt = (
                self.config.get_prompt_file(step.fix_prompt_file)
                if step.fix_prompt_file
                else None
            )

            # Determine MCP for fix tools
            fix_mcp = None
            if mcp_config and any("mcp__" in t for t in step.fix_tools):
                fix_mcp = mcp_config

            on_output = self.display.get_output_callback(agent) if agent else None
            fix_result = await self.claude.run(
                prompt=fix_prompt,
                cwd=context.worktree_path,
                system_prompt_file=fix_system_prompt,
                allowed_tools=step.fix_tools,
                on_output=on_output,
                mcp_config=fix_mcp,
            )

            if not fix_result.success:
                logger.warning(
                    f"Issue #{context.issue_number}: {step.name} fix failed, "
                    f"stopping fix loop"
                )
                return StepResult(
                    step_type=step.step_type,
                    success=False,
                    cycles_completed=cycle + 1,
                    output=fix_result.output,
                )

            # Ensure changes are committed
            await self._ensure_committed(
                context.worktree_path,
                f"{log_context} ({step.name} fix)",
                agent=agent,
            )

            # Check if fix made any new commits
            commits_after = await self.worktrees.get_commit_count(
                context.worktree_path, context.base_branch
            )
            if commits_after == commits_before:
                logger.info(
                    f"Issue #{context.issue_number}: {step.name} fix cycle "
                    f"{cycle + 1} made no changes, stopping"
                )
                return StepResult(
                    step_type=step.step_type,
                    success=True,
                    cycles_completed=cycle + 1,
                    output=review_result.output,
                )

        # Exhausted max cycles
        logger.info(
            f"Issue #{context.issue_number}: {step.name} completed "
            f"{step.max_fix_cycles} cycles"
        )
        return StepResult(
            step_type=step.step_type,
            success=True,
            cycles_completed=step.max_fix_cycles,
        )

    async def _run_gates(
        self,
        step_just_completed: StepConfig,
        pipeline: PipelineConfig,
        context: IssueContext,
        agent: Optional[AgentContext] = None,
        mcp_config: Optional[dict] = None,
    ) -> None:
        """Run all configured gate commands after a pipeline step.

        On failure, runs a contextual fix session that knows what step
        just completed and retries the gate.

        Args:
            step_just_completed: The step that just finished.
            pipeline: Pipeline configuration with gate definitions.
            context: Issue context.
            agent: Optional TUI agent.
            mcp_config: Optional MCP config for Playwright.
        """
        log_context = f"issue #{context.issue_number}: {context.issue_title}"

        for gate in pipeline.gates:
            logger.info(
                f"Issue #{context.issue_number}: Running gate: {gate.name}"
            )

            passed, output = await self.claude.run_checks(
                commands=[gate.command],
                cwd=context.worktree_path,
            )

            if passed:
                logger.info(f"Gate passed: {gate.name}")
                continue

            # Gate failed — try to fix
            for retry in range(pipeline.gate_max_retries):
                logger.warning(
                    f"Issue #{context.issue_number}: Gate '{gate.name}' "
                    f"failed (attempt {retry + 1}/{pipeline.gate_max_retries})"
                )

                fix_prompt = (
                    f"You just completed the {step_just_completed.name} step "
                    f"for issue #{context.issue_number}: "
                    f"{context.issue_title}.\n\n"
                    f"The following check failed: {gate.name}\n"
                    f"Command: `{gate.command}`\n\n"
                    f"## Failure Output\n\n```\n{output}\n```\n\n"
                    f"Fix the issue while keeping the broader implementation "
                    f"goals in mind. Do not disable or skip the check."
                )

                gate_fix_prompt = self.config.get_prompt_file("gate_fix.md")

                on_output = (
                    self.display.get_output_callback(agent) if agent else None
                )
                await self.claude.run(
                    prompt=fix_prompt,
                    cwd=context.worktree_path,
                    system_prompt_file=gate_fix_prompt,
                    on_output=on_output,
                )

                await self._ensure_committed(
                    context.worktree_path,
                    f"{log_context} (gate fix: {gate.name})",
                    agent=agent,
                )

                # Re-run the gate
                passed, output = await self.claude.run_checks(
                    commands=[gate.command],
                    cwd=context.worktree_path,
                )

                if passed:
                    logger.info(
                        f"Gate '{gate.name}' passed after fix attempt "
                        f"{retry + 1}"
                    )
                    break
            else:
                logger.error(
                    f"Issue #{context.issue_number}: Gate '{gate.name}' "
                    f"still failing after {pipeline.gate_max_retries} retries"
                )

    async def _run_create_pr(
        self,
        context: IssueContext,
        impl_output: str,
        agent: Optional[AgentContext] = None,
    ) -> Optional[int]:
        """Create a PR from the pipeline results.

        Handles rebase, push, PR creation, label management, and
        completion comments.

        Args:
            context: Issue context.
            impl_output: Output from the implementation step (for summary).
            agent: Optional TUI agent.

        Returns:
            PR number if created, None if no commits to push.
        """
        # Check if there are any commits to push
        has_commits = await self.worktrees.has_commits_ahead(
            context.worktree_path, context.base_branch
        )

        if not has_commits:
            logger.info(
                f"No commits made for issue #{context.issue_number}, "
                "nothing to push"
            )
            no_changes_explanation = format_output(
                impl_output,
                context="explanation",
                work_type="issue",
                number=context.issue_number,
            )
            await self.github.post_comment(
                context.issue_number,
                f"I looked at this issue but didn't find any changes to make.\n\n"
                f"**Claude's response:**\n\n"
                f"{no_changes_explanation[:GITHUB_COMMENT_MAX_BODY]}\n\n"
                f"*— Clover, the Claude Overseer*",
            )
            await self.github.remove_label(
                context.issue_number, self.config.clover_label
            )
            await self.github.add_label(context.issue_number, "clover-complete")
            self.state.mark_completed(WorkItemType.ISSUE, context.issue_number)
            return None

        # Rebase on base branch if needed
        is_behind = await self.worktrees.is_behind_base(
            context.worktree_path, context.base_branch
        )
        if is_behind:
            logger.info(
                f"Branch is behind {context.base_branch}, rebasing..."
            )
            success, error_msg = await self.worktrees.rebase_on_base(
                context.worktree_path, context.base_branch
            )
            if not success:
                logger.warning(
                    f"Issue #{context.issue_number}: Could not update branch "
                    f"to match {context.base_branch}: {error_msg}. "
                    "Continuing with branch as-is."
                )

        # Push branch (force needed after rebase)
        await self.worktrees.push_branch(
            context.worktree_path, context.branch_name, force=True
        )

        # Build summary
        commit_log = await self.worktrees.get_commit_log(
            context.worktree_path, context.base_branch
        )
        commit_fallback = format_commit_log_as_summary(commit_log)
        summary = format_output(
            impl_output,
            fallback_generator=lambda: commit_fallback,
            context="summary",
            work_type="issue",
            number=context.issue_number,
        )

        # Create PR
        pr_body = (
            f"Implements #{context.issue_number}\n\n"
            f"## Changes\n\n"
            f"{summary[:GITHUB_COMMENT_MAX_BODY]}\n\n"
            f"---\n*— Clover, the Claude Overseer*\n"
        )
        pr = await self.github.create_pr(
            branch=context.branch_name,
            title=f"Implement #{context.issue_number}: {context.issue_title}",
            body=pr_body,
            base_branch=context.base_branch,
        )

        # Label management
        await self.github.add_label(pr.number, self.config.clover_label)
        await self.github.remove_label(
            context.issue_number, self.config.clover_label
        )
        await self.github.add_label(context.issue_number, "clover-complete")

        # Post completion comment
        pr_url = (
            f"https://github.com/{self.config.github_repo}/pull/{pr.number}"
        )
        await self.github.post_comment(
            context.issue_number,
            f"✅ Finished working on this issue.\n\n"
            f"**Summary:** {summary[:GITHUB_COMMENT_MAX_BODY]}\n\n"
            f"**Pull Request:** {pr_url}\n\n"
            f"*— Clover, the Claude Overseer*",
        )

        self.state.mark_completed(
            WorkItemType.ISSUE,
            context.issue_number,
            related_number=pr.number,
        )
        return pr.number

    async def _process_pr_review(
        self,
        pr: PullRequest,
        retry_count: int = 0,
        pause_comment_posted: bool = False,
    ) -> None:
        """Process a PR by reviewing it.

        Args:
            pr: PR to review.
            retry_count: Current retry count (for pause/resume tracking).
            pause_comment_posted: Whether a pause comment was already posted.
        """
        worktree = None
        agent: Optional[AgentContext] = None

        # Create agent for TUI tracking
        if self.display:
            agent = self.display.create_agent(
                work_type="pr_review",
                number=pr.number,
                title=pr.title,
                branch_name=pr.branch,
            )

        try:
            # Mark as in progress
            self.state.mark_in_progress(WorkItemType.PR_REVIEW, pr.number)

            # Post start comment
            await self.github.post_comment(
                pr.number,
                "🔍 Starting code review...\n\n"
                "*— Clover, the Claude Overseer*",
            )

            # Create worktree at PR branch
            worktree = await self.worktrees.checkout_pr_branch(pr.number, pr.branch)

            # Run setup script if configured
            await self._run_setup_script(
                worktree.path, pr.branch, "pr_review", pr.number
            )

            # Run review checks if configured
            checks_output = ""
            if self.config.review_commands:
                checks_passed, check_output = await self.claude.run_checks(
                    commands=self.config.review_commands,
                    cwd=worktree.path,
                )
                checks_output = f"## 🔧 Review Checks\n\n{check_output}\n\n"

            # Run Claude review
            on_output = self.display.get_output_callback(agent) if agent else None
            result = await self.claude.review_pr(
                pr_number=pr.number,
                pr_title=pr.title,
                pr_body=pr.body,
                cwd=worktree.path,
                on_output=on_output,
            )

            # Post resume comment if this was a retry
            if retry_count > 0:
                try:
                    await self.github.post_comment(
                        pr.number,
                        "▶️ Work has resumed after transient failure.\n\n"
                        "*— Clover, the Claude Overseer*",
                    )
                except Exception:
                    pass

            # Format review output with fallback
            review_output = format_output(
                result.output,
                fallback_generator=lambda: (
                    "## Summary\n\n"
                    "Code review completed. Please see the diff for details.\n\n"
                    "## Suggestions\n\nNone\n\n"
                    "## Blocking Issues\n\nNone"
                ),
                context="review",
                work_type="pr_review",
                number=pr.number,
            )

            # Post review as comment
            review_comment = f"""## 🤖 Automated Code Review

{checks_output}{review_output[:GITHUB_COMMENT_MAX_BODY]}

---
*— Clover, the Claude Overseer*
"""
            await self.github.post_comment(pr.number, review_comment)

            # Remove clover label and add clover-reviewed
            await self.github.remove_label(pr.number, self.config.clover_label)
            await self.github.add_label(pr.number, "clover-reviewed")

            # Mark completed
            self.state.mark_completed(WorkItemType.PR_REVIEW, pr.number)
            self._log(f"Posted review for PR #{pr.number}")
            if agent:
                agent.mark_completed()

        except Exception as e:
            logger.error(f"Failed to review PR #{pr.number}: {e}")
            await self._handle_failure(
                WorkItemType.PR_REVIEW,
                pr.number,
                e,
                agent,
                retry_count=retry_count,
                pause_comment_posted=pause_comment_posted,
            )

        finally:
            # Cleanup worktree
            if worktree:
                try:
                    item = self.state.get_item(WorkItemType.PR_REVIEW, pr.number)
                    if item and item.status == WorkItemStatus.PAUSED:
                        logger.info(
                            f"Preserving worktree at {worktree.path} "
                            f"(item is paused for retry)"
                        )
                    else:
                        await self.worktrees.cleanup_worktree(worktree.path)
                except Exception as e:
                    logger.warning(f"Failed to cleanup worktree: {e}")
            # Refresh display
            if self.display:
                self.display.refresh()

    async def _process_pr_fix(
        self,
        pr: PullRequest,
        retry_count: int = 0,
        pause_comment_posted: bool = False,
    ) -> None:
        """Implement review suggestions for a PR.

        Args:
            pr: PR to fix.
            retry_count: Current retry count (for pause/resume tracking).
            pause_comment_posted: Whether a pause comment was already posted.
        """
        worktree = None
        agent: Optional[AgentContext] = None

        # Create agent for TUI tracking
        if self.display:
            agent = self.display.create_agent(
                work_type="pr_fix",
                number=pr.number,
                title=pr.title,
                branch_name=pr.branch,
            )

        try:
            # Get Clover's review comment
            review_comment = await self.github.get_clover_review_comment(pr.number)
            if not review_comment:
                raise ClaudeRunnerError(
                    f"No review comment found for PR #{pr.number}. "
                    "Cannot implement fixes without review feedback."
                )

            # Mark as in progress
            self.state.mark_in_progress(WorkItemType.PR_FIX, pr.number)

            # Post start comment
            await self.github.post_comment(
                pr.number,
                "🔧 Implementing review suggestions...\n\n"
                "*— Clover, the Claude Overseer*",
            )

            # Create writable worktree at PR branch
            worktree = await self.worktrees.checkout_pr_branch_writable(
                pr.number, pr.branch
            )

            # Run setup script if configured
            await self._run_setup_script(
                worktree.path, pr.branch, "pr_fix", pr.number
            )

            # Rebase on base branch before starting work to avoid push failures
            rebase_context = ""
            is_behind = await self.worktrees.is_behind_base(
                worktree.path, self._default_branch
            )
            if is_behind:
                logger.info(
                    f"PR #{pr.number}: Branch is behind {self._default_branch}, "
                    "rebasing before starting work..."
                )
                success, error_msg = await self.worktrees.rebase_on_base(
                    worktree.path, self._default_branch
                )
                if not success:
                    logger.warning(
                        f"PR #{pr.number}: Automatic rebase failed, "
                        "delegating conflict resolution to Claude."
                    )
                    rebase_context = (
                        f"IMPORTANT: This branch is behind `{self._default_branch}` "
                        f"and an automatic rebase failed due to conflicts. "
                        f"Before implementing review suggestions, you must:\n"
                        f"1. Run `git rebase origin/{self._default_branch}`\n"
                        f"2. Resolve any conflicts (edit conflicting files, "
                        f"`git add` them, then `git rebase --continue`)\n"
                        f"3. Then proceed with the review feedback\n"
                    )

            # Run Claude to implement review suggestions
            on_output = self.display.get_output_callback(agent) if agent else None
            result = await self.claude.implement_review(
                pr_number=pr.number,
                pr_title=pr.title,
                pr_body=pr.body,
                review_comment=review_comment.body,
                cwd=worktree.path,
                on_output=on_output,
                rebase_context=rebase_context,
            )

            if not result.success:
                raise ClaudeRunnerError(
                    f"Review implementation failed: {result.output[:GITHUB_COMMENT_MAX_BODY]}"
                )

            # Post resume comment if this was a retry
            if retry_count > 0:
                try:
                    await self.github.post_comment(
                        pr.number,
                        "▶️ Work has resumed after transient failure.\n\n"
                        "*— Clover, the Claude Overseer*",
                    )
                except Exception:
                    pass

            # Check if there are uncommitted changes (Claude made changes but didn't commit)
            context = f"PR #{pr.number} review fixes: {pr.title}"
            try:
                await self._ensure_committed(
                    worktree.path, context, agent=agent, fatal=True,
                )
            except ClaudeRunnerError:
                worktree = None  # Prevent cleanup so user can inspect
                raise

            # Run tests if configured
            await self._run_tests_with_retry(worktree.path, context, agent=agent)

            # Check if there are any commits to push
            has_commits = await self.worktrees.has_commits_ahead(
                worktree.path, pr.branch
            )

            if not has_commits:
                # No changes made
                logger.info(f"No commits made for PR #{pr.number}, nothing to push")
                no_changes_details = format_output(
                    result.output,
                    fallback_generator=lambda: (
                        "Reviewed all suggestions and determined the current "
                        "implementation already addresses the feedback."
                    ),
                    context="details",
                    work_type="pr_fix",
                    number=pr.number,
                )
                await self.github.post_comment(
                    pr.number,
                    f"I reviewed the suggestions and determined no code changes were needed.\n\n"
                    f"**Details:**\n\n{no_changes_details[:GITHUB_COMMENT_MAX_BODY]}\n\n"
                    f"*— Clover, the Claude Overseer*",
                )
            else:
                # Push commits to the existing PR branch (force needed after rebase)
                await self.worktrees.push_branch(worktree.path, pr.branch, force=True)

                # Build summary with commit log fallback
                commit_log = await self.worktrees.get_commit_log(worktree.path, pr.branch)
                commit_fallback = format_commit_log_as_summary(commit_log)
                fix_summary = format_output(
                    result.output,
                    fallback_generator=lambda: commit_fallback,
                    context="summary",
                    work_type="pr_fix",
                    number=pr.number,
                )

                # Post completion comment
                await self.github.post_comment(
                    pr.number,
                    f"✅ Implemented review suggestions and pushed changes.\n\n"
                    f"**Summary:** {fix_summary[:GITHUB_COMMENT_MAX_BODY]}\n\n"
                    f"*— Clover, the Claude Overseer*",
                )

            # Update labels: remove clover-fix, add clover-fixed
            await self.github.remove_label(pr.number, "clover-fix")
            await self.github.add_label(pr.number, "clover-fixed")

            # Mark completed
            self.state.mark_completed(WorkItemType.PR_FIX, pr.number)
            self._log(f"Implemented review fixes for PR #{pr.number}")
            if agent:
                agent.mark_completed()

        except Exception as e:
            logger.error(f"Failed to implement fixes for PR #{pr.number}: {e}")
            await self._handle_failure(
                WorkItemType.PR_FIX,
                pr.number,
                e,
                agent,
                retry_count=retry_count,
                pause_comment_posted=pause_comment_posted,
            )

        finally:
            # Cleanup worktree (but preserve if paused or has uncommitted changes)
            if worktree:
                try:
                    item = self.state.get_item(WorkItemType.PR_FIX, pr.number)
                    if item and item.status == WorkItemStatus.PAUSED:
                        logger.info(
                            f"Preserving worktree at {worktree.path} "
                            f"(item is paused for retry)"
                        )
                    else:
                        has_uncommitted = await self.worktrees.has_uncommitted_changes(worktree.path)
                        if has_uncommitted:
                            logger.warning(
                                f"Preserving worktree at {worktree.path} for inspection "
                                f"(has uncommitted changes)"
                            )
                        else:
                            await self.worktrees.cleanup_worktree(worktree.path)
                except Exception as e:
                    logger.warning(f"Failed to cleanup worktree: {e}")
            # Refresh display
            if self.display:
                self.display.refresh()


async def async_main(args: argparse.Namespace) -> int:
    """Async main entry point."""
    # Load config
    try:
        repo_path = Path(args.repo) if hasattr(args, "repo") and args.repo else None
        config = load_config(repo_path)
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return 1

    # Determine if TUI should be enabled
    use_tui = False
    if hasattr(args, "no_tui") and args.no_tui:
        logger.info("TUI disabled via --no-tui flag")
        use_tui = False
    elif hasattr(args, "tui") and args.tui:
        logger.info("TUI enabled via --tui flag")
        use_tui = True
    elif is_tty() and not getattr(args, "once", False):
        # Auto-enable TUI for interactive sessions (but not --once mode)
        logger.info("TUI auto-enabled (interactive TTY session)")
        use_tui = True
    else:
        logger.info(f"TUI disabled (is_tty={is_tty()}, once={getattr(args, 'once', False)})")

    # Create display if TUI is enabled
    display: Optional[CloverDisplay] = None
    if use_tui:
        logger.info("Creating TUI display...")
        display = CloverDisplay(config)
        logger.info("TUI display created")

    # Create orchestrator
    orchestrator = Orchestrator(config, display=display)

    # Set up signal handlers
    loop = asyncio.get_event_loop()

    def signal_handler():
        asyncio.create_task(orchestrator.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    # Run
    error_to_display = None
    try:
        # Start TUI display if enabled
        if display:
            display.start()

        if args.once:
            logger.info("Running single poll cycle")
            # Reset in-progress items so they can be resumed
            reset = orchestrator.state.reset_in_progress_items()
            if reset:
                logger.info(f"Reset {reset} in-progress items for resumption")
            if config.base_branch:
                orchestrator._default_branch = config.base_branch
            else:
                orchestrator._default_branch = await orchestrator.worktrees.get_default_branch()
            await orchestrator._poll_cycle()
            # Wait for all tasks to complete
            if orchestrator._active_tasks:
                logger.info(f"Waiting for {len(orchestrator._active_tasks)} task(s) to complete...")
                await asyncio.gather(*orchestrator._active_tasks, return_exceptions=True)
            await orchestrator._cleanup()
        else:
            await orchestrator.start()
    except Exception as e:
        # Capture error to display after TUI stops
        error_to_display = e
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        # Stop TUI display
        if display:
            display.stop()

    # Display error after TUI is stopped so user can see it
    if error_to_display:
        import traceback
        print(f"\n\033[91mError:\033[0m {error_to_display}", file=sys.stderr)
        print(f"\nFull traceback logged to: {_log_file}", file=sys.stderr)
        return 1

    return 0
