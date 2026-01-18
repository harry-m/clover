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

from .config import load_config
from .docker_utils import DockerError
from .main import async_main
from .state import State, WorkItemStatus, WorkItemType
from .test_session import TestSessionManager


def get_repo_path(args: argparse.Namespace) -> Optional[Path]:
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
        config = load_config(get_repo_path(args))
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
        config = load_config(get_repo_path(args))
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
        config = load_config(get_repo_path(args))
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
    """Initialize a new Clover project."""
    import re
    import subprocess

    target_dir = get_repo_path(args) or Path.cwd()
    config_path = target_dir / "clover.yaml"
    gitignore_path = target_dir / ".gitignore"

    # Check if config already exists
    if config_path.exists() and not args.force:
        print(f"clover.yaml already exists in {target_dir}")
        print("Use --force to overwrite.")
        return 1

    # Try to detect GitHub repo from git remote
    github_repo = None
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            cwd=target_dir,
            timeout=10,
        )
        if result.returncode == 0:
            remote_url = result.stdout.strip()
            # Parse GitHub URL (SSH or HTTPS)
            # git@github.com:owner/repo.git
            # https://github.com/owner/repo.git
            match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", remote_url)
            if match:
                github_repo = f"{match.group(1)}/{match.group(2)}"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    if not github_repo:
        github_repo = "owner/repo-name  # TODO: Update with your repo"

    # Generate clover.yaml content
    config_content = f"""# Clover configuration
# Documentation: https://github.com/anthropics/claude-code

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

# Review settings - commands to run during PR review
review:
  commands: []
    # Examples (uncomment for your project):
    # - npm test
    # - npm run lint
    # - pytest
    # - ruff check .

# Test session settings - for `clover test` command
test:
  # Path to docker-compose file (default: docker-compose.yml)
  compose_file: docker-compose.yml

  # Container for interactive Claude sessions (default: first container)
  # container: develop
"""

    # Write config file
    config_path.write_text(config_content)
    print(f"Created {config_path}")

    # Update .gitignore
    gitignore_entries = [
        "# Clover state and working files",
        ".orchestrator-state.json",
        ".clover-test-sessions.json",
        ".clover-compose-override.yml",
        "worktrees/",
    ]

    existing_gitignore = ""
    if gitignore_path.exists():
        existing_gitignore = gitignore_path.read_text()

    # Check which entries are missing
    missing_entries = []
    for entry in gitignore_entries:
        # Skip comment lines when checking
        if entry.startswith("#"):
            continue
        if entry not in existing_gitignore:
            missing_entries.append(entry)

    if missing_entries:
        # Add missing entries
        with open(gitignore_path, "a") as f:
            if existing_gitignore and not existing_gitignore.endswith("\n"):
                f.write("\n")
            if existing_gitignore:
                f.write("\n")
            f.write("# Clover state and working files\n")
            for entry in missing_entries:
                f.write(f"{entry}\n")
        print(f"Updated {gitignore_path}")
    else:
        print(".gitignore already has Clover entries")

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

    if "TODO" in github_repo:
        print(f"  {step}. Edit clover.yaml and set your GitHub repository")
        step += 1

    if not gh_authenticated:
        print(f"  {step}. Authenticate with GitHub:")
        print("       gh auth login")
        step += 1

    print(f"  {step}. Add the 'clover' label to issues you want Clover to work on")
    step += 1

    print(f"  {step}. Start Clover:")
    print("       clover run")

    return 0


# Test command handlers

def cmd_test(args: argparse.Namespace) -> int:
    """Start a test session."""
    try:
        config = load_config(get_repo_path(args))
    except ValueError as e:
        print(f"Configuration error: {e}")
        return 1

    manager = TestSessionManager(config)

    try:
        session = _run_async(manager.start(args.target))
    except DockerError as e:
        print(f"Docker error: {e}")
        return 1
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1

    print(f"Test session started: {session.session_id}")
    print(f"  Branch: {session.branch_name}")
    print(f"  Worktree: {session.worktree_path}")
    print(f"  Container: {session.container_name}")
    print()

    if session.ports:
        print("Ports:")
        for port_key, host_port in session.ports.items():
            service, container_port = port_key.split(":")
            print(f"  {service} port {container_port} -> http://localhost:{host_port}")
        print()

    print("To attach to this session:")
    print(f"  clover test attach {session.session_id}")
    print()
    print("To see logs:")
    print(f"  clover test logs {session.session_id}")
    print()
    print("To stop:")
    print(f"  clover test stop {session.session_id}")

    return 0


def cmd_test_attach(args: argparse.Namespace) -> int:
    """Attach to a test session."""
    try:
        config = load_config(get_repo_path(args))
    except ValueError as e:
        print(f"Configuration error: {e}")
        return 1

    manager = TestSessionManager(config)
    session_id = getattr(args, "session_id", None)

    try:
        # This replaces the current process
        _run_async(manager.attach(session_id))
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    return 0


def cmd_test_list(args: argparse.Namespace) -> int:
    """List test sessions."""
    try:
        config = load_config(get_repo_path(args))
    except ValueError as e:
        print(f"Configuration error: {e}")
        return 1

    manager = TestSessionManager(config)
    sessions = _run_async(manager.list_sessions())

    if not sessions:
        print("No test sessions found.")
        return 0

    print("Test Sessions")
    print("=" * 60)

    for session in sessions:
        status_icon = "●" if session.status == "running" else "○"
        print(f"\n{status_icon} {session.session_id}")
        print(f"  Status: {session.status}")
        print(f"  Branch: {session.branch_name}")
        print(f"  Worktree: {session.worktree_path}")
        if session.container_name:
            print(f"  Container: {session.container_name}")
        if session.ports:
            print("  Ports:")
            for port_key, host_port in session.ports.items():
                service, container_port = port_key.split(":")
                print(f"    {service}:{container_port} -> localhost:{host_port}")
        print(f"  Started: {session.started_at.strftime('%Y-%m-%d %H:%M:%S')}")

    return 0


def cmd_test_stop(args: argparse.Namespace) -> int:
    """Stop a test session."""
    try:
        config = load_config(get_repo_path(args))
    except ValueError as e:
        print(f"Configuration error: {e}")
        return 1

    manager = TestSessionManager(config)
    session_id = args.session_id

    # If no session_id, show list and prompt
    if not session_id:
        sessions = _run_async(manager.list_sessions())
        running = [s for s in sessions if s.status == "running"]

        if not running:
            print("No running test sessions found.")
            return 0

        if len(running) == 1:
            session_id = running[0].session_id
            print(f"Stopping session: {session_id}")
        else:
            print("Multiple running sessions. Please specify which to stop:")
            for s in running:
                print(f"  - {s.session_id}")
            return 1

    try:
        _run_async(manager.stop(session_id))
        print(f"Stopped session: {session_id}")
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    return 0


def cmd_test_logs(args: argparse.Namespace) -> int:
    """Show logs from a test session."""
    try:
        config = load_config(get_repo_path(args))
    except ValueError as e:
        print(f"Configuration error: {e}")
        return 1

    manager = TestSessionManager(config)
    session_id = args.session_id

    # If no session_id, use most recent
    if not session_id:
        sessions = _run_async(manager.list_sessions())
        running = [s for s in sessions if s.status == "running"]

        if not running:
            print("No running test sessions found.")
            return 1

        session_id = max(running, key=lambda s: s.started_at).session_id

    async def stream_logs():
        try:
            process = await manager.get_logs(session_id, follow=args.follow, tail=args.tail)
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                print(line.decode().rstrip())
        except ValueError as e:
            print(f"Error: {e}")
            return 1
        return 0

    try:
        return _run_async(stream_logs())
    except KeyboardInterrupt:
        print("\nStopped following logs.")
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

    # Init command
    init_parser = subparsers.add_parser("init", help="Initialize a new Clover project")
    init_parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Overwrite existing clover.yaml",
    )

    # Test command with subcommands
    test_parser = subparsers.add_parser("test", help="Manage test sessions")
    test_subparsers = test_parser.add_subparsers(dest="test_command", help="Test commands")

    # test <target> - start a test session (default action)
    test_start_parser = test_subparsers.add_parser("start", help="Start a test session")
    test_start_parser.add_argument(
        "target",
        help="Issue number or branch name to test",
    )

    # test attach [session_id]
    test_attach_parser = test_subparsers.add_parser("attach", help="Attach to a test session")
    test_attach_parser.add_argument(
        "session_id",
        nargs="?",
        help="Session ID to attach to (default: most recent)",
    )

    # test list
    test_subparsers.add_parser("list", help="List test sessions")

    # test stop [session_id]
    test_stop_parser = test_subparsers.add_parser("stop", help="Stop a test session")
    test_stop_parser.add_argument(
        "session_id",
        nargs="?",
        help="Session ID to stop",
    )

    # test logs [session_id]
    test_logs_parser = test_subparsers.add_parser("logs", help="Show logs from a test session")
    test_logs_parser.add_argument(
        "session_id",
        nargs="?",
        help="Session ID to get logs from (default: most recent)",
    )
    test_logs_parser.add_argument(
        "-f", "--follow",
        action="store_true",
        help="Follow log output",
    )
    test_logs_parser.add_argument(
        "-n", "--tail",
        type=int,
        default=100,
        help="Number of lines to show (default: 100)",
    )

    # Also allow `clover test <target>` as shorthand for `clover test start <target>`
    test_parser.add_argument(
        "target",
        nargs="?",
        help="Issue number or branch name to test (shorthand for 'test start')",
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
        # Handle test subcommands
        if args.test_command == "start":
            return cmd_test(args)
        elif args.test_command == "attach":
            return cmd_test_attach(args)
        elif args.test_command == "list":
            return cmd_test_list(args)
        elif args.test_command == "stop":
            return cmd_test_stop(args)
        elif args.test_command == "logs":
            return cmd_test_logs(args)
        elif args.target:
            # Shorthand: `clover test <target>` -> `clover test start <target>`
            return cmd_test(args)
        else:
            test_parser.print_help()
            return 0
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
