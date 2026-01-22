# Clover, the Claude Overseer

A local Python daemon that watches GitHub issues and pull requests, automatically launching Claude Code to implement features and review code.

## Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     GitHub Repository                           │
│  ┌──────────────┐    ┌──────────────┐                          │
│  │ Issue tagged │    │ PR tagged    │                          │
│  │   "clover"   │    │  "clover"    │                          │
│  └──────┬───────┘    └──────┬───────┘                          │
└─────────┼───────────────────┼──────────────────────────────────┘
          │                   │
          ▼                   ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Clover Daemon                            │
│  ┌──────────────┐    ┌──────────────┐                          │
│  │  Implement   │    │    Review    │                          │
│  │   Feature    │    │      PR      │                          │
│  └──────┬───────┘    └──────┬───────┘                          │
└─────────┼───────────────────┼──────────────────────────────────┘
          │                   │
          ▼                   ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Git Worktrees                               │
│  ┌──────────────┐    ┌──────────────┐                          │
│  │ worktrees/   │    │ worktrees/   │    (Isolated environments│
│  │ clover-      │    │ clover-      │     for parallel work)   │
│  │ issue-42/    │    │ issue-7/     │                          │
│  └──────────────┘    └──────────────┘                          │
└─────────────────────────────────────────────────────────────────┘
```

## Features

- **Automatic Issue Implementation**: Tag an issue with `clover` and Claude will implement it, create a branch, and open a PR
- **Automated Code Review**: Tag a PR with `clover` and Claude will review it with detailed feedback
- **Parallel Processing**: Multiple issues/PRs can be processed simultaneously using git worktrees
- **State Persistence**: Survives restarts without re-processing completed work
- **Manual Testing**: Use `clover test` to interactively test PRs with Claude's help

## Prerequisites

- **Python 3.10+**
- **Git** with worktree support
- **Claude Code CLI** installed and authenticated (`claude` command available)
- **GitHub CLI** (optional, for automatic token detection) or a GitHub Personal Access Token

### Installing Claude Code

```bash
# Install Claude Code
npm install -g @anthropic-ai/claude-code

# Authenticate (follow the prompts)
claude
```

## Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/harry-m/clover.git
   cd clover
   ```

2. **Install dependencies**:
   ```bash
   pip install -e ".[dev]"
   ```

3. **Create configuration** in your target repository:
   ```bash
   cp clover.example.yaml /path/to/your/repo/clover.yaml
   # Edit clover.yaml with your settings
   ```

## Configuration

Create a `clover.yaml` file in your repository root:

```yaml
# GitHub settings
github:
  repo: owner/repo-name
  token: ${GITHUB_TOKEN}  # Can use environment variable
  label: clover           # Label that triggers Clover (default: clover)
  # base_branch: main     # Target branch for PRs (default: auto-detect)

# Daemon settings
daemon:
  poll_interval: 60       # Seconds between polls (default: 60)
  max_concurrent: 2       # Max parallel Claude instances (default: 2)
  max_turns: 50           # Max turns per Claude conversation (default: 50)
  # worktree_base: ../my-worktrees  # Worktree location (default: ./worktrees)
  # setup_script: scripts/setup-worktree.sh  # Script to run after worktree creation

# Review settings - commands to run during PR review
review:
  commands:
    - pytest
    - ruff check .

# Test session settings (for `clover test` command)
test:
  compose_file: docker-compose.yml  # Docker compose file (default: docker-compose.yml)
```

**Note**: If you've authenticated with `gh auth login`, Clover will automatically use that token and `github.token` is optional.

## Usage

### CLI Commands

```bash
# Show available commands
clover --help

# Start the daemon (automated background processing)
clover run

# Start with verbose logging
clover run --verbose

# Run with terminal UI
clover run --tui

# Single poll cycle (useful for testing)
clover run --once

# Show current state and in-progress work
clover status

# Show configuration
clover config

# Initialize a new clover.yaml
clover init

# Clear state for an issue (allows re-processing)
clover clear issue 42

# Clear state for a PR review
clover clear review 7

# Clear all state
clover clear --all
```

### Workflow: Implementing Issues

1. **Create an issue** on GitHub with a clear description of what needs to be implemented

2. **Add the `clover` label** (or your configured label) to the issue

3. **Clover will**:
   - Detect the labeled issue
   - Create a new git worktree and branch (`clover/issue-{number}`)
   - Launch Claude Code to implement the feature
   - Commit changes and push the branch
   - Create a pull request linked to the issue
   - Remove the `clover` label

4. **Review the PR** created by Claude and provide feedback or approve

### Workflow: Automated Code Review

1. **Add the `clover` label** to a pull request

2. **Clover will**:
   - Detect the labeled PR
   - Check out the PR branch in a worktree
   - Run any configured review commands
   - Run Claude Code to review the changes
   - Post a detailed review comment on the PR

3. **Review Claude's feedback** and address any concerns

### Workflow: Manual Testing with `clover test`

While `clover run` handles automated work in the background, `clover test` is for **you** to manually test PRs with Claude's help.

#### Mental Model

```
┌─────────────────────────────────────────────────────────────────┐
│  clover run (automated)          clover test (manual)           │
│  ─────────────────────           ────────────────────           │
│  • Runs in background            • Interactive, one at a time   │
│  • Multiple parallel worktrees   • Direct checkout in main repo │
│  • Claude works autonomously     • Claude assists YOU           │
│  • Implements & reviews PRs      • Helps test & verify PRs      │
└─────────────────────────────────────────────────────────────────┘
```

#### Commands

```bash
# Start testing a PR - checks out branch, starts Docker, launches Claude
clover test start <PR_NUMBER>

# Show what you're currently testing
clover test status

# Re-launch Claude if you exit
clover test resume

# View Docker container logs
clover test logs
clover test logs -f  # follow mode

# Stop testing - shuts down Docker, returns to original branch
clover test stop
```

#### Example Session

```bash
$ clover test start 184
Testing PR #184: Add user authentication
Checking out feature/auth...
Starting Docker containers in background...
Launching Claude for PR #184...

# Claude launches with full context:
# - PR title and description
# - Linked issue details
# - Your role: help test and verify the changes

# ... work with Claude to test the PR ...
# ... exit Claude when done ...

$ clover test stop
Stopping Docker containers...
Switching back to main...
Stopped testing PR #184
```

#### Options

```bash
# Start without launching Claude (just setup)
clover test start 184 --no-claude

# Start without Docker (if you don't need containers)
clover test start 184 --no-docker

# Stop but stay on the PR branch
clover test stop --keep-branch
```

## Customizing Claude's Behavior

### System Prompts

The orchestrator uses system prompts to guide Claude's behavior. You can customize these:

- `scripts/orchestrator/prompts/implement.md` - Guidelines for implementing issues
- `scripts/orchestrator/prompts/review.md` - Guidelines for reviewing PRs

## State Management

The orchestrator tracks work in progress to:
- Prevent duplicate processing of the same issue/PR
- Survive daemon restarts
- Clean up stale work items

State is stored in `.orchestrator-state.json` (configurable in clover.yaml).

### Clearing State

```bash
# Clear state for a specific item
clover clear issue 42
clover clear review 7

# Clear all state
clover clear --all
```

## Troubleshooting

### "github.token is required"

Either:
- Set `github.token` in clover.yaml, or
- Set the `GITHUB_TOKEN` environment variable, or
- Authenticate with GitHub CLI: `gh auth login`

### Claude process times out

Increase the timeout or reduce complexity:
- Set `daemon.max_turns` to a higher value in clover.yaml
- Break large issues into smaller ones

### Worktree conflicts

If worktrees get stuck:

```bash
# List worktrees
git worktree list

# Remove a stuck worktree
git worktree remove ./worktrees/clover-issue-42 --force

# Prune stale worktree references
git worktree prune
```

## Development

### Running Tests

```bash
pytest
pytest -v  # verbose
```

### Project Structure

```
scripts/orchestrator/
├── __init__.py
├── main.py              # Daemon loop and orchestration
├── cli.py               # Command-line interface
├── config.py            # Configuration loading (clover.yaml)
├── github_watcher.py    # GitHub API integration
├── worktree_manager.py  # Git worktree operations
├── claude_runner.py     # Claude Code process management
├── state.py             # State persistence
├── test_session.py      # Manual test session management
├── docker_utils.py      # Docker Compose utilities
├── tui.py               # Terminal UI
├── agent_context.py     # Agent tracking for TUI
├── prompts/
│   ├── implement.md     # Implementation guidelines
│   └── review.md        # Review guidelines
└── tests/
    └── ...
```

## Security Considerations

- **GitHub Token**: Store securely, use environment variables
- **Code Review**: Claude's implementations should still be human-reviewed
- **Worktrees**: Are created locally; ensure your machine is secure

## License

MIT License - see LICENSE file for details.

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Submit a pull request
