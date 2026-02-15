#!/usr/bin/env python3
"""Clover CLI - Command-line interface for the Clover daemon."""

from __future__ import annotations

import argparse
import asyncio
import gc
import logging
import sys
from pathlib import Path
from typing import Optional

# Suppress the harmless "Event loop is closed" error on Windows during exit
if sys.platform == "win32":
    _original_del = asyncio.proactor_events._ProactorBasePipeTransport.__del__

    def _silenced_del(self):
        try:
            _original_del(self)
        except RuntimeError:
            pass  # Ignore "Event loop is closed" during cleanup

    asyncio.proactor_events._ProactorBasePipeTransport.__del__ = _silenced_del

from .config import load_config, detect_github_repo, get_clover_dir
from .main import async_main
from .state import State, WorkItemStatus, WorkItemType
from .test_session import TestSessionManager


def _get_repo_path(args: argparse.Namespace) -> Optional[Path]:
    """Get repo path from args, if specified."""
    if hasattr(args, "repo") and args.repo:
        return Path(args.repo)
    return None


def _run_async(coro) -> int:
    """Run an async coroutine with proper cleanup on Windows.

    This avoids the 'Event loop is closed' RuntimeError that occurs
    when asyncio transports are garbage collected after the loop closes.
    """
    if sys.platform == "win32":
        # On Windows, we need to be more careful about cleanup
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = 0
        try:
            result = loop.run_until_complete(coro)
        except KeyboardInterrupt:
            # Handle Ctrl+C gracefully
            pass
        finally:
            try:
                # Cancel any pending tasks
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                # Run the loop briefly to let cancellations propagate
                # Use return_exceptions=True to suppress CancelledError
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass  # Ignore errors during cleanup
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            try:
                # Python 3.9+ has shutdown_default_executor
                if hasattr(loop, "shutdown_default_executor"):
                    loop.run_until_complete(loop.shutdown_default_executor())
            except Exception:
                pass
            # Clear the event loop reference before closing
            asyncio.set_event_loop(None)
            loop.close()
            # Force garbage collection after loop is closed and cleared
            gc.collect()
        return result
    else:
        return asyncio.run(coro)


def cmd_run(args: argparse.Namespace) -> int:
    """Run the Clover daemon."""
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Reuse the existing async_main logic
    try:
        return _run_async(async_main(args))
    except KeyboardInterrupt:
        print("\nInterrupted")
        return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show current state and in-progress work."""
    try:
        config = load_config(_get_repo_path(args))
    except ValueError as e:
        print(f"Configuration error: {e}")
        return 1

    state = State(config.state_file)

    print("Clover Status")
    print(f"{'=' * 50}")
    print(f"Repository: {config.github_repo}")
    print(f"State file: {config.state_file}")
    print()

    # Group items by status
    in_progress = []
    completed = []
    failed = []

    for key, item in state.work_items.items():
        if item.status == WorkItemStatus.IN_PROGRESS:
            in_progress.append(item)
        elif item.status == WorkItemStatus.COMPLETED:
            completed.append(item)
        elif item.status == WorkItemStatus.FAILED:
            failed.append(item)

    if in_progress:
        print(f"In Progress ({len(in_progress)}):")
        for item in in_progress:
            print(f"  - {item.item_type.value} #{item.number}")
            if item.branch_name:
                print(f"    Branch: {item.branch_name}")
            if item.started_at:
                print(f"    Started: {item.started_at}")
        print()

    if failed:
        print(f"Failed ({len(failed)}):")
        for item in failed:
            print(f"  - {item.item_type.value} #{item.number}")
            if item.error_message:
                print(f"    Error: {item.error_message[:100]}")
        print()

    if completed:
        print(f"Completed ({len(completed)}):")
        # Build a map of PR numbers that came from issues
        pr_from_issue = {}  # pr_number -> issue_number
        for item in completed:
            if item.item_type == WorkItemType.ISSUE and item.related_number:
                pr_from_issue[item.related_number] = item.number

        # Show items, grouping related ones
        shown_prs = set()
        for item in completed[-10:]:  # Show last 10
            if item.item_type == WorkItemType.ISSUE:
                if item.related_number:
                    # Issue that created a PR
                    print(f"  - issue #{item.number} → PR #{item.related_number}")
                    shown_prs.add(item.related_number)
                else:
                    # Issue with no PR (no changes needed)
                    print(f"  - issue #{item.number} (no changes)")
            elif item.item_type == WorkItemType.PR_REVIEW:
                if item.number in pr_from_issue:
                    # This PR came from an issue we know about
                    if item.number not in shown_prs:
                        issue_num = pr_from_issue[item.number]
                        print(f"  - issue #{issue_num} → PR #{item.number} (reviewed)")
                        shown_prs.add(item.number)
                else:
                    # Standalone PR review
                    print(f"  - pr_review #{item.number}")
            else:
                print(f"  - {item.item_type.value} #{item.number}")
        if len(completed) > 10:
            print(f"  ... and {len(completed) - 10} more")
        print()

    if not (in_progress or completed or failed):
        print("No work items tracked yet.")

    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    """Clear state for an issue or PR to allow re-processing."""
    try:
        config = load_config(_get_repo_path(args))
    except ValueError as e:
        print(f"Configuration error: {e}")
        return 1

    state = State(config.state_file)

    # Handle --all flag
    if args.all:
        return _clear_all(state)

    # Validate that type and number are provided for single-item clear
    if args.type is None or args.number is None:
        print("Error: type and number are required when not using --all")
        return 1

    # Determine item type (with synonyms)
    item_type_map = {
        "issue": WorkItemType.ISSUE,
        "feature": WorkItemType.ISSUE,  # synonym
        "review": WorkItemType.PR_REVIEW,
        "pr": WorkItemType.PR_REVIEW,  # synonym
        "fix": WorkItemType.PR_FIX,
    }

    if args.type not in item_type_map:
        print(f"Unknown type: {args.type}")
        print("Valid types: issue (or feature), review (or pr), fix")
        return 1

    item_type = item_type_map[args.type]
    number = args.number

    item = state.get_item(item_type, number)
    if item is None:
        print(f"No {args.type} #{number} found in state.")
        return 1

    state.clear_item(item_type, number)
    print(f"Cleared {args.type} #{number} from state. It will be re-processed on next poll.")
    return 0


def _clear_all(state: State) -> int:
    """Clear all state with confirmation."""
    if not state.work_items:
        print("State is already empty. Nothing to clear.")
        return 0

    # Build summary by type
    by_type: dict[str, list] = {}

    for item in state.work_items.values():
        type_name = item.item_type.value
        by_type.setdefault(type_name, []).append(item)

    # Display summary
    print("This will clear ALL state (blank slate):")
    print()
    for type_name, items in sorted(by_type.items()):
        label = type_name.replace("_", " ").title()
        print(f"  {label} ({len(items)}):")
        for item in items:
            print(f"    - #{item.number} ({item.status.value})")
    print()
    print(f"Total: {len(state.work_items)} items will be cleared.")
    print()

    # Confirm
    try:
        response = input("Are you sure? (yes/no): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return 1

    if response not in ("yes", "y"):
        print("Aborted.")
        return 1

    count = state.clear_all()
    print(f"Cleared {count} items. State is now empty.")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Show current configuration."""
    try:
        config = load_config(_get_repo_path(args))
    except ValueError as e:
        print(f"Configuration error: {e}")
        return 1

    print("Clover Configuration")
    print("=" * 50)
    print(f"Repository:      {config.github_repo}")
    print(f"Base branch:     {config.base_branch or '(auto-detect)'}")
    print(f"Clover label:    {config.clover_label}")
    print(f"Poll interval:   {config.poll_interval}s")
    print(f"Max concurrent:  {config.max_concurrent}")
    print(f"Max turns:       {config.max_turns}")
    print(f"Worktree base:   {config.worktree_base}")
    print(f"State file:      {config.state_file}")
    print(f"Config dir:      {config.user_config_dir}")
    print(f"Claude command:  {config.claude_command or '(auto-detect)'}")
    print(f"Review-fix cycles: {config.max_review_fix_cycles}")
    print()
    print("Review Settings:")
    if config.review_commands:
        print("  Review checks:")
        for cmd in config.review_commands:
            print(f"    - {cmd}")
    else:
        print("  Review checks: none configured")

    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize Clover for the current repository."""
    import subprocess

    target_dir = _get_repo_path(args) or Path.cwd()

    # Detect GitHub repo from git remote
    github_repo = detect_github_repo(target_dir)
    if not github_repo:
        print("Error: Could not detect GitHub repository from git remote.")
        print("Make sure you're in a git repository with an 'origin' remote")
        print("pointing to GitHub.")
        return 1

    # Determine config directory
    config_dir = get_clover_dir(github_repo)
    config_path = config_dir / "clover.yaml"

    # Check if config already exists
    if config_path.exists() and not args.force:
        print(f"Clover is already configured at {config_path}")
        print("Use --force to overwrite.")
        return 1

    # Generate clover.yaml content
    config_content = f"""# Clover configuration for {github_repo}
# Documentation: https://github.com/harry-m/clover

github:
  # Repository in format: owner/repo
  repo: {github_repo}

  # GitHub token - uses gh CLI by default, or set GITHUB_TOKEN env var
  # token: ${{GITHUB_TOKEN}}

  # Label that triggers Clover (default: clover)
  label: clover

  # Base branch for feature branches and PR targets
  # Leave blank to auto-detect (repo's default branch)
  # base_branch: develop

daemon:
  # Seconds between GitHub polling (default: 60)
  poll_interval: 60

  # Maximum concurrent Claude instances (default: 2)
  max_concurrent: 2

  # Maximum turns per Claude conversation (default: 50)
  max_turns: 50

  # Custom command to invoke Claude (optional)
  # Use this if claude is not in PATH or to run via docker/ssh
  # claude_command: /custom/path/to/claude

  # Setup script to run after worktree creation (optional)
  # Path relative to repo root, receives CLOVER_* env vars
  # setup_script: scripts/setup-worktree.sh

# Review settings - commands to run during PR review
review:
  commands: []
    # Examples (uncomment for your project):
    # - npm test
    # - npm run lint
    # - pytest
    # - ruff check .

  # Number of self-review/fix cycles before creating PR (default: 2, 0 to disable)
  # max_review_fix_cycles: 2
"""

    # Create config directory and write config file
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(config_content)
    print(f"Created {config_path}")

    # Check if gh CLI is authenticated
    gh_authenticated = False
    gh_installed = False
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        gh_installed = True
        gh_authenticated = result.returncode == 0
    except FileNotFoundError:
        gh_installed = False
    except subprocess.TimeoutExpired:
        gh_installed = True  # Assume installed if it timed out

    print()

    # Handle gh authentication
    if not gh_installed:
        print("Warning: GitHub CLI (gh) is not installed.")
        print("Install it from: https://cli.github.com/")
        print()
        print("Alternatively, set GITHUB_TOKEN in your environment and")
        print("uncomment the token line in clover.yaml.")
        print()
    elif not gh_authenticated:
        print("GitHub CLI is not authenticated.")
        print()
        response = ""
        try:
            response = input("Run 'gh auth login' now? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()

        if response in ("", "y", "yes"):
            print()
            # Run gh auth login interactively
            subprocess.run(["gh", "auth", "login"])
            print()
            # Check if it worked
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                print("GitHub authentication successful!")
                gh_authenticated = True
            else:
                print("GitHub authentication was not completed.")
        print()

    # Show next steps
    print("Next steps:")
    step = 1

    if not gh_authenticated:
        print(f"  {step}. Authenticate with GitHub:")
        print("       gh auth login")
        step += 1

    print(f"  {step}. Add the 'clover' label to issues you want Clover to work on")
    step += 1

    print(f"  {step}. Start Clover (from your repo directory):")
    print("       clover run")

    return 0


# Test command handlers

def cmd_test(args: argparse.Namespace) -> int:
    """Start a test session for a PR or branch."""
    try:
        config = load_config(_get_repo_path(args))
    except ValueError as e:
        print(f"Configuration error: {e}")
        return 1

    # Parse target - strip leading # if present
    target = args.targets[0].lstrip("#") if args.targets else None
    if not target:
        print("Usage: clover test <PR-number-or-branch>")
        print("       clover test list")
        print("       clover test clean [target]")
        return 1

    manager = TestSessionManager(config)

    try:
        _run_async(manager.start(target))
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    return 0


def cmd_test_list(args: argparse.Namespace) -> int:
    """List active test worktrees."""
    try:
        config = load_config(_get_repo_path(args))
    except ValueError as e:
        print(f"Configuration error: {e}")
        return 1

    manager = TestSessionManager(config)
    _run_async(manager.list())
    return 0


def cmd_test_clean(args: argparse.Namespace) -> int:
    """Clean up test worktrees."""
    try:
        config = load_config(_get_repo_path(args))
    except ValueError as e:
        print(f"Configuration error: {e}")
        return 1

    # Optional target: clover test clean [42]
    clean_target = args.targets[1].lstrip("#") if len(args.targets) > 1 else None

    manager = TestSessionManager(config)
    _run_async(manager.clean(clean_target))
    return 0


def main() -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="clover",
        description="Clover, the Claude Overseer",
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version="%(prog)s 0.1.0",
    )
    parser.add_argument(
        "--repo", "-r",
        type=str,
        help="Path to repository root (default: current directory)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Run command
    run_parser = subparsers.add_parser("run", help="Start the Clover daemon")
    run_parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    run_parser.add_argument(
        "--once",
        action="store_true",
        help="Run one poll cycle and exit",
    )
    run_parser.add_argument(
        "--tui",
        action="store_true",
        default=None,
        help="Enable rich terminal UI (default when TTY)",
    )
    run_parser.add_argument(
        "--no-tui",
        action="store_true",
        help="Disable rich terminal UI",
    )

    # Status command
    subparsers.add_parser("status", help="Show current state")

    # Clear command
    clear_parser = subparsers.add_parser("clear", help="Clear state for re-processing")
    clear_parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="Clear all state (blank slate)",
    )
    clear_parser.add_argument(
        "type",
        nargs="?",
        choices=["issue", "feature", "review", "pr", "fix"],
        help="Type of item to clear (feature=issue, pr=review)",
    )
    clear_parser.add_argument(
        "number",
        nargs="?",
        type=int,
        help="Issue or PR number",
    )

    # Config command
    subparsers.add_parser("config", help="Show configuration")

    # Init command
    init_parser = subparsers.add_parser("init", help="Initialize Clover for this repository")
    init_parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Overwrite existing clover.yaml",
    )

    # Test command: clover test <target>, clover test list, clover test clean [target]
    test_parser = subparsers.add_parser("test", help="Test a PR or branch locally")
    test_parser.add_argument(
        "targets",
        nargs="*",
        help="PR number, branch name, 'list', or 'clean [target]'",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Dispatch to command handler
    if args.command == "run":
        return cmd_run(args)
    elif args.command == "status":
        return cmd_status(args)
    elif args.command == "clear":
        return cmd_clear(args)
    elif args.command == "config":
        return cmd_config(args)
    elif args.command == "init":
        return cmd_init(args)
    elif args.command == "test":
        first = args.targets[0] if args.targets else None
        if first == "list":
            return cmd_test_list(args)
        elif first in ("clean", "clear"):
            return cmd_test_clean(args)
        elif first:
            return cmd_test(args)
        else:
            test_parser.print_help()
            return 0
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
