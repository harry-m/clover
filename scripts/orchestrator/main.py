#!/usr/bin/env python3
"""Main entry point for the Clover daemon."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Optional

from .agent_context import AgentContext
from .claude_runner import ClaudeRunner, ClaudeRunnerError
from .config import Config, load_config
from .github_watcher import GitHubWatcher, Issue, PullRequest
from .state import State, WorkItemType
from .tui import CloverDisplay, is_tty
from .worktree_manager import WorktreeManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


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
        self._active_tasks: set[asyncio.Task] = set()

    def _log(self, message: str) -> None:
        """Log a message to both logger and display.

        Args:
            message: Message to log.
        """
        logger.info(message)
        if self.display:
            self.display.log(message)
            self.display.refresh()

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

            # Wait for next poll interval
            await asyncio.sleep(self.config.poll_interval)

        # Cleanup
        logger.info("Shutting down...")
        await self._cleanup()

    async def stop(self) -> None:
        """Signal the orchestrator to stop."""
        logger.info("Stop requested")
        self._shutdown = True

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

        if available_slots <= 0:
            logger.debug(
                f"At concurrency limit ({in_progress}/{self.config.max_concurrent})"
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

    def _should_review_pr(self, pr: PullRequest) -> bool:
        """Check if Clover should review this PR.

        Args:
            pr: Pull request to check.

        Returns:
            True if PR has the clover label.
        """
        return self.config.clover_label in pr.labels

    async def _process_issue(self, issue: Issue) -> None:
        """Process an issue by implementing it.

        Args:
            issue: Issue to implement.
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
            self.display.refresh()

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

            # Run Claude to implement
            on_output = self.display.get_output_callback(agent) if agent else None
            result = await self.claude.implement_issue(
                issue_number=issue.number,
                issue_title=issue.title,
                issue_body=issue.body,
                cwd=worktree.path,
                on_output=on_output,
            )

            if not result.success:
                raise ClaudeRunnerError(f"Implementation failed: {result.output[:500]}")

            # Check if there are any commits to push
            has_commits = await self.worktrees.has_commits_ahead(
                worktree.path, self._default_branch
            )

            if not has_commits:
                # Check if there are uncommitted changes (Claude made changes but didn't commit)
                has_uncommitted = await self.worktrees.has_uncommitted_changes(worktree.path)

                if has_uncommitted:
                    # Claude made changes but didn't commit them - this is an error
                    uncommitted_status = await self.worktrees.get_uncommitted_status(worktree.path)
                    logger.error(
                        f"Issue #{issue.number}: Claude made changes but didn't commit them!\n"
                        f"Uncommitted changes:\n{uncommitted_status}"
                    )
                    # Don't clean up the worktree so user can inspect/recover
                    worktree = None  # Prevent cleanup in finally block
                    raise ClaudeRunnerError(
                        f"Claude made file changes but didn't commit them. "
                        f"Worktree preserved for inspection. "
                        f"Uncommitted files:\n{uncommitted_status[:500]}"
                    )

                logger.info(f"No commits made for issue #{issue.number}, nothing to push")
                await self.github.post_comment(
                    issue.number,
                    f"I looked at this issue but didn't find any changes to make.\n\n"
                    f"Claude's response:\n\n{result.output[:1000]}\n\n"
                    f"*— Clover, the Claude Overseer*",
                )
                # Remove clover label and add clover-complete
                await self.github.remove_label(issue.number, self.config.clover_label)
                await self.github.add_label(issue.number, "clover-complete")
                self.state.mark_completed(WorkItemType.ISSUE, issue.number)
                return

            # Push branch
            await self.worktrees.push_branch(worktree.path, branch_name)

            # Create PR
            pr_body = f"""Implements #{issue.number}

## Changes

{result.output[:2000]}

---
*— Clover, the Claude Overseer*
"""
            pr = await self.github.create_pr(
                branch=branch_name,
                title=f"Implement #{issue.number}: {issue.title}",
                body=pr_body,
                base_branch=self._default_branch,
            )

            # Add clover label to PR so Clover will review it
            await self.github.add_label(pr.number, self.config.clover_label)

            # Remove clover label from issue and add clover-complete
            await self.github.remove_label(issue.number, self.config.clover_label)
            await self.github.add_label(issue.number, "clover-complete")

            # Post completion comment on issue with PR link
            pr_url = f"https://github.com/{self.config.github_repo}/pull/{pr.number}"
            await self.github.post_comment(
                issue.number,
                f"✅ Finished working on this issue.\n\n"
                f"**Summary:** {result.output[:500]}\n\n"
                f"**Pull Request:** {pr_url}\n\n"
                f"*— Clover, the Claude Overseer*",
            )

            # Mark completed with link to the created PR
            self.state.mark_completed(WorkItemType.ISSUE, issue.number, related_number=pr.number)
            self._log(f"Created PR #{pr.number} for issue #{issue.number}")
            if agent:
                agent.mark_completed()

        except Exception as e:
            logger.error(f"Failed to process issue #{issue.number}: {e}")
            self.state.mark_failed(WorkItemType.ISSUE, issue.number, str(e))
            if agent:
                agent.mark_failed()
                agent.add_output(f"Error: {str(e)[:100]}")

            # Post error comment on issue
            try:
                await self.github.post_comment(
                    issue.number,
                    f"❌ Failed to implement this issue automatically.\n\n"
                    f"Error: {str(e)[:500]}\n\n"
                    f"*— Clover, the Claude Overseer*",
                )
            except Exception:
                pass

        finally:
            # Cleanup worktree
            if worktree:
                try:
                    await self.worktrees.cleanup_worktree(worktree.path)
                except Exception as e:
                    logger.warning(f"Failed to cleanup worktree: {e}")
            # Refresh display
            if self.display:
                self.display.refresh()

    async def _process_pr_review(self, pr: PullRequest) -> None:
        """Process a PR by reviewing it.

        Args:
            pr: PR to review.
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
            self.display.refresh()

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

            # Post review as comment
            review_comment = f"""## 🤖 Automated Code Review

{checks_output}{result.output[:60000]}

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
            self.state.mark_failed(WorkItemType.PR_REVIEW, pr.number, str(e))
            if agent:
                agent.mark_failed()
                agent.add_output(f"Error: {str(e)[:100]}")

        finally:
            # Cleanup worktree
            if worktree:
                try:
                    await self.worktrees.cleanup_worktree(worktree.path)
                except Exception as e:
                    logger.warning(f"Failed to cleanup worktree: {e}")
            # Refresh display
            if self.display:
                self.display.refresh()


def get_repo_path(args: argparse.Namespace) -> Optional[Path]:
    """Get repo path from args, if specified.

    Args:
        args: Parsed command line arguments.

    Returns:
        Path to repository, or None for current directory.
    """
    if hasattr(args, "repo") and args.repo:
        return Path(args.repo)
    return None


async def async_main(args: argparse.Namespace) -> int:
    """Async main entry point."""
    # Load config
    try:
        config = load_config(get_repo_path(args))
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return 1

    # Determine if TUI should be enabled
    use_tui = False
    if hasattr(args, "no_tui") and args.no_tui:
        use_tui = False
    elif hasattr(args, "tui") and args.tui:
        use_tui = True
    elif is_tty() and not getattr(args, "once", False):
        # Auto-enable TUI for interactive sessions (but not --once mode)
        use_tui = True

    # Create display if TUI is enabled
    display: Optional[CloverDisplay] = None
    if use_tui:
        display = CloverDisplay(config)

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
    finally:
        # Stop TUI display
        if display:
            display.stop()

    return 0
