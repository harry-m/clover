#!/usr/bin/env python3
"""Clover CLI - Command-line interface for the Clover daemon."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import load_config
from .main import async_main
from .state import State, WorkItemStatus, WorkItemType


def cmd_run(args: argparse.Namespace) -> int:
    """Run the Clover daemon."""
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Reuse the existing async_main logic
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\nInterrupted")
        return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show current state and in-progress work."""
    try:
        config = load_config()
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
        for item in completed[-10:]:  # Show last 10
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
        config = load_config()
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
    }

    if args.type not in item_type_map:
        print(f"Unknown type: {args.type}")
        print("Valid types: issue (or feature), review (or pr)")
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

    # Build summary
    issues = []
    reviews = []

    for item in state.work_items.values():
        if item.item_type == WorkItemType.ISSUE:
            issues.append(item)
        elif item.item_type == WorkItemType.PR_REVIEW:
            reviews.append(item)

    # Display summary
    print("This will clear ALL state (blank slate):")
    print()
    if issues:
        print(f"  Issues ({len(issues)}):")
        for item in issues:
            print(f"    - #{item.number} ({item.status.value})")
    if reviews:
        print(f"  PR Reviews ({len(reviews)}):")
        for item in reviews:
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
        config = load_config()
    except ValueError as e:
        print(f"Configuration error: {e}")
        return 1

    print("Clover Configuration")
    print("=" * 50)
    print(f"Repository:      {config.github_repo}")
    print(f"Clover label:    {config.clover_label}")
    print(f"Poll interval:   {config.poll_interval}s")
    print(f"Max concurrent:  {config.max_concurrent}")
    print(f"Max turns:       {config.max_turns}")
    print(f"Worktree base:   {config.worktree_base}")
    print(f"State file:      {config.state_file}")
    print()
    print("Review Settings:")
    if config.review_commands:
        print("  Review checks:")
        for cmd in config.review_commands:
            print(f"    - {cmd}")
    else:
        print("  Review checks: none configured")

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
        choices=["issue", "feature", "review", "pr"],
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
    else:
        # No command specified - default to run for backwards compatibility
        # But show help if no args at all
        if len(sys.argv) == 1:
            parser.print_help()
            return 0
        # Otherwise, treat as run command
        args.verbose = "-v" in sys.argv or "--verbose" in sys.argv
        args.once = "--once" in sys.argv
        args.tui = "--tui" in sys.argv
        args.no_tui = "--no-tui" in sys.argv
        return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
