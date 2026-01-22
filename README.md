# Clover, the Claude Overseer

A local Python daemon that watches GitHub issues and pull requests, automatically launching Claude Code to implement features and review code.

## Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     GitHub Repository                           │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │ Issue tagged │    │   New PR     │    │  Comment     │      │
│  │   "ready"    │    │   opened     │    │  "/merge"    │      │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘      │
└─────────┼───────────────────┼───────────────────┼──────────────┘
          │                   │                   │
          ▼                   ▼                   ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Clover Daemon                            │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │  Implement   │    │    Review    │    │ Run Checks   │      │
│  │   Feature    │    │      PR      │    │  & Merge     │      │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘      │
└─────────┼───────────────────┼───────────────────┼──────────────┘
          │                   │                   │
          ▼                   ▼                   ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Git Worktrees                               │
│  ┌──────────────┐    ┌──────────────┐                          │
│  │ worktrees/   │    │ worktrees/   │    (Isolated environments│
│  │ feature-42/  │    │ pr-review-7/ │     for parallel work)   │
│  └──────────────┘    └──────────────┘                          │
└─────────────────────────────────────────────────────────────────┘
```

## Features

- **Automatic Issue Implementation**: Tag an issue with `ready` and Claude will implement it, create a branch, and open a PR
- **Automated Code Review**: New PRs are automatically reviewed by Claude with detailed feedback
- **Controlled Merging**: Comment `/merge` on a PR to trigger pre-merge checks and automatic merging
- **Configurable Pre-merge Checks**: Run tests, linters, and security scans before merging
- **Parallel Processing**: Multiple issues/PRs can be processed simultaneously using git worktrees
- **State Persistence**: Survives restarts without re-processing completed work

## Prerequisites

- **Python 3.10+**
- **Git** with worktree support
- **Claude Code CLI** installed and authenticated (`claude` command available)
- **GitHub CLI** (optional, for automatic token detection) or a GitHub Personal Access Token

### Installing Claude Code

If you haven't installed Claude Code yet:

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

3. **Configure the environment**:
   ```bash
   cp .env.example .env
   # Edit .env with your settings
   ```

## Configuration

Create a `.env` file in the project root (or set environment variables):

### Required Settings

| Variable | Description | Example |
|----------|-------------|---------|
| `GITHUB_REPO` | Repository to watch (owner/repo format) | `harry-m/dashai` |
| `GITHUB_TOKEN` | GitHub Personal Access Token | `ghp_xxxx...` |

**Note**: If you've authenticated with `gh auth login`, Clover will automatically use that token and `GITHUB_TOKEN` is optional.

### Optional Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `POLL_INTERVAL` | `60` | Seconds between GitHub API polls |
| `WORKTREE_BASE` | `./worktrees` | Directory for git worktrees |
| `READY_LABEL` | `ready` | Issue label that triggers implementation |
| `MAX_CONCURRENT` | `2` | Maximum parallel Claude processes |
| `STATE_FILE` | `./.orchestrator-state.json` | State persistence file |
| `MAX_TURNS` | `50` | Maximum Claude conversation turns |
| `AUTO_MERGE_ENABLED` | `true` | Enable `/merge` comment handling |
| `MERGE_COMMENT_TRIGGER` | `/merge` | Comment text that triggers merge |
| `PRE_MERGE_COMMANDS` | `[]` | JSON array of commands to run before merge |

### Example Configuration

```bash
# .env
GITHUB_REPO=harry-m/dashai
GITHUB_TOKEN=ghp_your_token_here

# Polling
POLL_INTERVAL=60
MAX_CONCURRENT=2

# Labels
READY_LABEL=ready

# Merging
AUTO_MERGE_ENABLED=true
MERGE_COMMENT_TRIGGER=/merge
PRE_MERGE_COMMANDS=["pytest", "ruff check .", "bandit -r src/"]
```

## Usage

### CLI Commands

```bash
# Show available commands
clover --help

# Start the daemon (automated background processing)
clover run

# Start with verbose logging
clover run --verbose

# Single poll cycle (useful for testing)
clover run --once

# Show current state and in-progress work
clover status

# Show configuration
clover config

# Clear state for an issue (allows re-processing)
clover clear issue 42

# Clear state for a PR review
clover clear review 7

# Clear state for a merge
clover clear merge 7

# Manual testing commands (see "Manual Testing" section below)
clover test start <PR>   # Start testing a PR
clover test status       # Show current test session
clover test resume       # Re-launch Claude
clover test logs         # View Docker logs
clover test stop         # Stop testing
```

### Workflow: Implementing Issues

1. **Create an issue** on GitHub with a clear description of what needs to be implemented. If you want to, Claude can help write it in Plan mode, and you can post the plan as an issue.

2. **Add the `ready` label** (or your configured label) to the issue

3. **The orchestrator will**:
   - Detect the labeled issue
   - Create a new git worktree and branch (`feature/issue-{number}`)
   - Launch Claude Code to implement the feature
   - Commit changes and push the branch
   - Create a pull request linked to the issue
   - Remove the `ready` label

4. **Review the PR** created by Claude and provide feedback or approve

### Workflow: Automated Code Review

1. **Open a pull request** against the repository

2. **The orchestrator will**:
   - Detect the new PR
   - Check out the PR branch in a worktree
   - Run Claude Code to review the changes
   - Post a detailed review comment on the PR

3. **Review Claude's feedback** and address any concerns

### Workflow: Merging with Checks

1. **Comment `/merge`** (or your configured trigger) on a PR

2. **The orchestrator will**:
   - Run all configured pre-merge commands (tests, linting, etc.)
   - Check that GitHub CI checks pass
   - If all checks pass, squash-merge the PR
   - Delete the feature branch
   - Close any linked issues

3. **If checks fail**, Clover posts a comment explaining what failed

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
# Start testing a PR - checks out the branch, starts Docker, launches Claude
clover test start <PR_NUMBER>

# Show what you're currently testing
clover test status

# Re-launch Claude if you exit (session persists)
clover test resume

# View Docker container logs
clover test logs
clover test logs -f  # follow mode

# Stop testing - shuts down Docker, returns to your original branch
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

#### Docker Setup

Your `docker-compose.yml` only needs your app services. Example:

```yaml
services:
  postgres:
    image: postgres:15
    # ...

  backend:
    build: ./backend
    ports:
      - "8000:8000"
    # ...

  frontend:
    build: ./frontend
    ports:
      - "3000:3000"
    # ...
```

Docker containers start in the background so you're not waiting - Claude launches immediately while services spin up.

## Pre-merge Checks

Configure commands that must pass before a PR can be merged:

```bash
PRE_MERGE_COMMANDS=["pytest", "ruff check .", "bandit -r src/", "mypy src/"]
```

Each command:
- Runs in the PR's worktree
- Must exit with code 0 to pass
- Has a 10-minute timeout
- Output is posted to the PR on failure

### Example Check Configurations

**Python project**:
```bash
PRE_MERGE_COMMANDS=["pytest", "ruff check .", "mypy src/"]
```

**Node.js project**:
```bash
PRE_MERGE_COMMANDS=["npm test", "npm run lint", "npm run build"]
```

**Go project**:
```bash
PRE_MERGE_COMMANDS=["go test ./...", "golangci-lint run"]
```

## Customizing Claude's Behavior

### System Prompts

The orchestrator uses system prompts to guide Claude's behavior. You can customize these:

- `scripts/orchestrator/prompts/implement.md` - Guidelines for implementing issues
- `scripts/orchestrator/prompts/review.md` - Guidelines for reviewing PRs

### Example: Custom Implementation Prompt

Edit `prompts/implement.md` to add project-specific instructions:

```markdown
# Implementation Guidelines

## Project-Specific Rules

- Use TypeScript for all new files
- Follow the existing pattern in `src/components/`
- Always add tests in `__tests__/` directories
- Use the `logger` utility instead of `console.log`

## Process

1. Read and understand the issue requirements
2. Explore the codebase...
```

## State Management

The orchestrator tracks work in progress to:
- Prevent duplicate processing of the same issue/PR
- Survive daemon restarts
- Clean up stale work items

State is stored in `.orchestrator-state.json` (configurable via `STATE_FILE`).

### Clearing State

To re-process an issue or PR, you can clear its state:

```python
from scripts.orchestrator.state import State, WorkItemType
from pathlib import Path

state = State(Path("./.orchestrator-state.json"))
state.clear_item(WorkItemType.ISSUE, 42)  # Allow issue #42 to be re-processed
```

Or simply delete the state file to reset everything:

```bash
rm .orchestrator-state.json
```

## Troubleshooting

### "GITHUB_TOKEN environment variable is required"

Either:
- Set the `GITHUB_TOKEN` environment variable, or
- Authenticate with GitHub CLI: `gh auth login`

### Claude process times out

Increase the timeout or reduce complexity:
- Set `MAX_TURNS` to a higher value
- Break large issues into smaller ones

### "Rate limit exceeded"

The orchestrator respects GitHub's rate limits automatically. If you hit limits frequently:
- Increase `POLL_INTERVAL`
- Use a GitHub App token (higher rate limits)

### Worktree conflicts

If worktrees get stuck:

```bash
# List worktrees
git worktree list

# Remove a stuck worktree
git worktree remove ./worktrees/feature-issue-42 --force

# Prune stale worktree references
git worktree prune
```

### State gets corrupted

Delete the state file and restart:

```bash
rm .orchestrator-state.json
python -m scripts.orchestrator.main
```

## Development

### Running Tests

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run specific test file
pytest scripts/orchestrator/tests/test_config.py
```

### Project Structure

```
scripts/orchestrator/
├── __init__.py
├── main.py              # Entry point and daemon loop
├── config.py            # Configuration loading
├── github_watcher.py    # GitHub API integration
├── worktree_manager.py  # Git worktree operations
├── claude_runner.py     # Claude Code process spawning
├── state.py             # State persistence
├── prompts/
│   ├── implement.md     # Implementation guidelines
│   └── review.md        # Review guidelines
└── tests/
    ├── test_config.py
    ├── test_state.py
    ├── test_github_watcher.py
    ├── test_worktree_manager.py
    └── test_claude_runner.py
```

### Adding New Features

1. Create a feature branch
2. Add tests for new functionality
3. Implement the feature
4. Run the test suite: `pytest`
5. Submit a PR

## Security Considerations

- **GitHub Token**: Store securely, never commit to version control
- **Pre-merge Checks**: Always include security scanning (e.g., `bandit`, `npm audit`)
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

## Support

- **Issues**: Report bugs and request features on GitHub Issues
- **Discussions**: Ask questions in GitHub Discussions
