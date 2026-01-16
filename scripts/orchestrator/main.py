#!/usr/bin/env python3
"""Main entry point for the Clover daemon."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

from .config import load_config, Config
from .state import State, WorkItemType
from .github_watcher import GitHubWatcher, Issue, PullRequest
from .worktree_manager import WorktreeManager, WorktreeError
from .claude_runner import ClaudeRunner, ClaudeRunnerError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class Orchestrator:
    """Main orchestrator daemon that coordinates all components."""

    def __init__(self, config: Config):
        """Initialize the orchestrator.

        Args:
            config: Orchestrator configuration.
        """
        self.config = config
        self.state = State(config.state_file)
        self.github = GitHubWatcher(config)
        self.worktrees = WorktreeManager(config)
        self.claude = ClaudeRunner(config)
        self._shutdown = False
        self._active_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Start the orchestrator daemon."""
        logger.info(f"Starting Clover for {self.config.github_repo}")
        logger.info(f"Watching for issues with label: {self.config.ready_label}")
        logger.info(f"Poll interval: {self.config.poll_interval}s")
        logger.info(f"Max concurrent: {self.config.max_concurrent}")

        if self.config.auto_merge_enabled:
            logger.info(f"Auto-merge enabled, trigger: {self.config.merge_comment_trigger}")
            if self.config.pre_merge_commands:
                logger.info(f"Pre-merge commands: {self.config.pre_merge_commands}")

        # Clean up any stale items from previous runs
        cleaned = self.state.cleanup_stale_items()
        if cleaned:
            logger.info(f"Cleaned up {cleaned} stale work items")

        # Get default branch for creating feature branches
        self._default_branch = await self.worktrees.get_default_branch()
        logger.info(f"Default branch: {self._default_branch}")

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

        # Check for ready issues
        issues = await self.github.get_ready_issues()
        for issue in issues:
            if available_slots <= 0:
                break

            if not self.state.is_processing(WorkItemType.ISSUE, issue.number):
                logger.info(f"Found ready issue #{issue.number}: {issue.title}")
                task = asyncio.create_task(self._process_issue(issue))
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)
                available_slots -= 1

        # Check for PRs needing review
        prs = await self.github.get_open_prs()
        for pr in prs:
            if available_slots <= 0:
                break

            if not self.state.is_processing(WorkItemType.PR_REVIEW, pr.number):
                logger.info(f"Found PR needing review #{pr.number}: {pr.title}")
                task = asyncio.create_task(self._process_pr_review(pr))
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)
                available_slots -= 1

        # Check for PRs with merge trigger
        if self.config.auto_merge_enabled:
            for pr in prs:
                if self.state.is_processing(WorkItemType.PR_MERGE, pr.number):
                    continue

                if await self.github.has_merge_comment(pr.number):
                    logger.info(f"Found PR with merge trigger #{pr.number}")
                    task = asyncio.create_task(self._process_pr_merge(pr))
                    self._active_tasks.add(task)
                    task.add_done_callback(self._active_tasks.discard)

    async def _process_issue(self, issue: Issue) -> None:
        """Process an issue by implementing it.

        Args:
            issue: Issue to implement.
        """
        branch_name = f"feature/issue-{issue.number}"
        worktree = None

        try:
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
            )

            self.state.mark_in_progress(
                WorkItemType.ISSUE,
                issue.number,
                worktree_path=str(worktree.path),
                branch_name=branch_name,
            )

            # Run Claude to implement
            result = await self.claude.implement_issue(
                issue_number=issue.number,
                issue_title=issue.title,
                issue_body=issue.body,
                cwd=worktree.path,
            )

            if not result.success:
                raise ClaudeRunnerError(f"Implementation failed: {result.output[:500]}")

            # Push branch
            await self.worktrees.push_branch(worktree.path, branch_name)

            # Create PR
            pr_body = f"""Implements #{issue.number}

## Changes

{result.output[:2000]}

---
*Automatically implemented by Claude Orchestrator*
"""
            pr = await self.github.create_pr(
                branch=branch_name,
                title=f"Implement #{issue.number}: {issue.title}",
                body=pr_body,
                base_branch=self._default_branch,
            )

            # Remove ready label from issue
            await self.github.remove_label(issue.number, self.config.ready_label)

            # Mark completed
            self.state.mark_completed(WorkItemType.ISSUE, issue.number)
            logger.info(f"Created PR #{pr.number} for issue #{issue.number}")

        except Exception as e:
            logger.error(f"Failed to process issue #{issue.number}: {e}")
            self.state.mark_failed(WorkItemType.ISSUE, issue.number, str(e))

            # Post error comment on issue
            try:
                await self.github.post_comment(
                    issue.number,
                    f"❌ Failed to implement this issue automatically.\n\n"
                    f"Error: {str(e)[:500]}\n\n"
                    f"*Claude Orchestrator*",
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

    async def _process_pr_review(self, pr: PullRequest) -> None:
        """Process a PR by reviewing it.

        Args:
            pr: PR to review.
        """
        worktree = None

        try:
            # Mark as in progress
            self.state.mark_in_progress(WorkItemType.PR_REVIEW, pr.number)

            # Create worktree at PR branch
            worktree = await self.worktrees.checkout_pr_branch(pr.number, pr.branch)

            # Run Claude review
            result = await self.claude.review_pr(
                pr_number=pr.number,
                pr_title=pr.title,
                pr_body=pr.body,
                cwd=worktree.path,
            )

            # Post review as comment
            review_comment = f"""## 🤖 Automated Code Review

{result.output[:4000]}

---
*Reviewed by Claude Orchestrator*
"""
            await self.github.post_comment(pr.number, review_comment)

            # Mark completed
            self.state.mark_completed(WorkItemType.PR_REVIEW, pr.number)
            logger.info(f"Posted review for PR #{pr.number}")

        except Exception as e:
            logger.error(f"Failed to review PR #{pr.number}: {e}")
            self.state.mark_failed(WorkItemType.PR_REVIEW, pr.number, str(e))

        finally:
            # Cleanup worktree
            if worktree:
                try:
                    await self.worktrees.cleanup_worktree(worktree.path)
                except Exception as e:
                    logger.warning(f"Failed to cleanup worktree: {e}")

    async def _process_pr_merge(self, pr: PullRequest) -> None:
        """Process a PR merge request.

        Args:
            pr: PR to merge.
        """
        worktree = None

        try:
            # Mark as in progress
            self.state.mark_in_progress(WorkItemType.PR_MERGE, pr.number)

            # Run pre-merge checks if configured
            if self.config.pre_merge_commands:
                # Create worktree to run checks
                worktree = await self.worktrees.checkout_pr_branch(pr.number, pr.branch)

                checks_passed, check_output = await self.claude.run_checks(
                    commands=self.config.pre_merge_commands,
                    cwd=worktree.path,
                )

                if not checks_passed:
                    await self.github.post_comment(
                        pr.number,
                        f"❌ Pre-merge checks failed. Cannot merge.\n\n"
                        f"## Check Results\n\n{check_output}\n\n"
                        f"*Claude Orchestrator*",
                    )
                    self.state.mark_failed(
                        WorkItemType.PR_MERGE, pr.number, "Pre-merge checks failed"
                    )
                    return

                # Post success message
                await self.github.post_comment(
                    pr.number,
                    f"✅ Pre-merge checks passed.\n\n"
                    f"## Check Results\n\n{check_output}\n\n"
                    f"Proceeding with merge...\n\n"
                    f"*Claude Orchestrator*",
                )

            # Check GitHub CI status
            ci_passed, ci_status = await self.github.get_pr_check_status(pr.number)
            if not ci_passed:
                await self.github.post_comment(
                    pr.number,
                    f"❌ GitHub checks not passing: {ci_status}\n\n"
                    f"Please fix the failing checks before merging.\n\n"
                    f"*Claude Orchestrator*",
                )
                self.state.mark_failed(
                    WorkItemType.PR_MERGE, pr.number, f"CI checks failed: {ci_status}"
                )
                return

            # Merge the PR
            merged = await self.github.merge_pr(pr.number)

            if merged:
                # Delete the branch
                await self.github.delete_branch(pr.branch)

                # Close linked issue if any
                if pr.linked_issue:
                    await self.github.close_issue(pr.linked_issue)

                self.state.mark_completed(WorkItemType.PR_MERGE, pr.number)
                logger.info(f"Merged PR #{pr.number}")
            else:
                self.state.mark_failed(
                    WorkItemType.PR_MERGE, pr.number, "Merge failed"
                )

        except Exception as e:
            logger.error(f"Failed to merge PR #{pr.number}: {e}")
            self.state.mark_failed(WorkItemType.PR_MERGE, pr.number, str(e))

            try:
                await self.github.post_comment(
                    pr.number,
                    f"❌ Failed to merge.\n\nError: {str(e)[:500]}\n\n"
                    f"*Claude Orchestrator*",
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


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Clover - Watch GitHub and launch Claude Code "
        "to implement features and review code"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to .env config file",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one poll cycle and exit (useful for testing)",
    )
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    """Async main entry point."""
    # Load config
    try:
        config = load_config()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return 1

    # Create orchestrator
    orchestrator = Orchestrator(config)

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
    if args.once:
        logger.info("Running single poll cycle")
        await orchestrator._poll_cycle()
        await orchestrator._cleanup()
    else:
        await orchestrator.start()

    return 0


def main() -> int:
    """Main entry point."""
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        logger.info("Interrupted")
        return 0


if __name__ == "__main__":
    sys.exit(main())
