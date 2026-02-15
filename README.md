# Clover, the Claude Overseer

A local Python daemon that watches GitHub issues and pull requests, automatically launching Claude Code to implement features and review code.

Clover runs unsupervised Claude sessions by design, you should run it in a VM. Please think about what else is on the VM, the credentials your development environment uses, etc -- Clover get things done quickly, but it raises all the usual risks of AIs going rogue.

## Features

- **Automatic Issue Implementation**: Tag an issue → Claude implements → PR created
- **Automated Code Review**: PRs are reviewed with configurable tests, linters, and Claude analysis
- **Self-Review Cycles**: Claude reviews its own work and fixes issues before creating a PR
- **Continuous Pipeline**: Implementation flows directly into review
- **Parallel Processing**: Multiple issues processed simultaneously using git worktrees
- **Manual Testing**: Use `clover test` to interactively test PRs with Claude's help


## How It Works

1. You label an issue `clover`
2. Clover implements it, creates PR, labels PR `clover`
3. Clover reviews the PR (runs tests, lints, Claude review)
4. You review and merge

You can also label your own PR with `clover` if you want Claude to review it.


When you label an issue with `clover`, Clover will:

1. Create a git worktree and branch
2. Launch Claude to implement the feature
3. Self-review the changes and fix issues (configurable cycles)
4. Run your configured tests/linters and fix failures
5. Create a PR and automatically label it `clover`
6. Review its own PR (running your configured tests and linters)
7. Post Claude's review comments

You can then review the PR, make any changes (with Claude's assistance, if you want). When you're happy, commit and push as normal, then merge the PR.

For manual testing, run `clover test <PR>` to create an isolated worktree and launch Claude with full context. Your main working directory stays untouched, and you can run multiple tests concurrently.


## Installation

### Prerequisites

- **Python 3.10+**
- **Git** with worktree support
- **Claude Code CLI** installed and authenticated
- **GitHub CLI** (recommended) or a GitHub Personal Access Token

### Install Clover

```bash
# Clone the repository
git clone https://github.com/harry-m/clover.git
cd clover

# Option A: pipx (recommended for CLI tools)
pipx install -e .

# Option B: Virtual environment (for development)
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Verify installation
clover --help
```

### Install Claude Code

```bash
npm install -g @anthropic-ai/claude-code
claude  # Follow prompts to authenticate
```

### Set Up Your Repository

Navigate to the repository you want Clover to manage:

```bash
cd /path/to/your/repo

# Initialize Clover configuration
clover init
```

This detects your GitHub repository from the git remote and creates a configuration file at `~/.clover/<owner>/<repo>/clover.yaml`. The repo field is auto-filled from your git remote.

### GitHub Authentication

Clover needs a GitHub token. Choose one:

**Option A: GitHub CLI (recommended)**
```bash
gh auth login
# Clover automatically uses this token
```

**Option B: Environment variable**
```bash
export GITHUB_TOKEN=ghp_your_token_here
```

**Option C: In clover.yaml**
```yaml
github:
  token: ${GITHUB_TOKEN}  # References env var
```

### Optional: Setup Script

If your worktrees need initialization (copying `.env` files, installing dependencies), create a setup script:

```bash
#!/bin/bash
# scripts/setup-worktree.sh

# Available environment variables:
# CLOVER_WORKTREE - path to the worktree
# CLOVER_BRANCH - branch name
# CLOVER_BASE_BRANCH - base branch (main/master)
# CLOVER_PARENT_REPO - path to the main repo
# CLOVER_ISSUE_NUMBER - issue number (if processing an issue)
# CLOVER_PR_NUMBER - PR number (if processing a PR)

cp "$CLOVER_PARENT_REPO/.env" "$CLOVER_WORKTREE/.env"
cd "$CLOVER_WORKTREE" && npm install
```

Reference it in clover.yaml:
```yaml
daemon:
  setup_script: scripts/setup-worktree.sh
```

## Configuration

Configuration lives at `~/.clover/<owner>/<repo>/clover.yaml`. Run `clover init` from your repository to generate it.

Full reference:

```yaml
github:
  repo: owner/repo-name           # Required (auto-detected by clover init)
  token: ${GITHUB_TOKEN}          # Optional if using gh CLI
  label: clover                   # Label that triggers Clover (default: clover)
  base_branch: main               # PR target branch (default: auto-detect)

daemon:
  poll_interval: 60               # Seconds between polls (default: 60)
  max_concurrent: 2               # Parallel Claude instances (default: 2)
  max_turns: 50                   # Max turns per conversation (default: 50)
  worktree_base: ./worktrees      # Worktree directory (default: ./worktrees)
  setup_script: scripts/setup.sh  # Run after worktree creation (optional)
  claude_command: claude           # Custom Claude CLI path (optional, auto-detected)

review:
  commands:                        # Commands to run during review
    - pytest
    - ruff check .
    - mypy src/
  max_review_fix_cycles: 2        # Self-review cycles before PR (default: 2, 0 to disable)
```

> **Security note:** Review commands are executed as shell commands. Only use trusted commands in your configuration.

## Usage

### Running the Daemon

```bash
# Start watching for issues and PRs
clover run

# With terminal UI
clover run --tui

# Verbose logging
clover run --verbose

# Single poll cycle (for testing)
clover run --once
```

### The Automated Workflow

1. **Create an issue** with a clear description of what to implement

2. **Add the `clover` label** to the issue

3. **Clover automatically**:
   - Creates branch `clover/issue-{number}`
   - Launches Claude to implement
   - Self-reviews and fixes issues (configurable cycles)
   - Runs your configured review commands
   - Creates a PR linking to the issue
   - Labels the PR `clover` (triggering review)
   - Posts Claude's code review
   - Labels issue `clover-complete`, PR `clover-reviewed`

4. **You review** the PR and merge when ready

### Review Commands

Configure tests and linters to run automatically during review:

```yaml
review:
  commands:
    - pytest                    # Run tests
    - ruff check .              # Lint
    - mypy src/                 # Type check
    - npm run build             # Build check
```

These run in the PR's worktree before Claude reviews. Results are included in the review comment. If tests fail, Claude will attempt to fix them (up to 2 retries).

### Manual Testing with `clover test`

For hands-on testing of PRs, `clover test` creates an isolated git worktree so your main working directory stays untouched. Multiple tests can run concurrently in separate terminals.

```bash
# Start testing - creates a worktree, launches Claude with context
clover test 184          # By PR number
clover test #184         # With hash prefix
clover test feature/foo  # By branch name

# Claude runs in the isolated worktree with full PR/issue context.
# Your main repo is untouched - you can keep working in another terminal.

# When Claude exits, Clover checks for uncommitted/unpushed changes
# and prints the worktree path and cleanup command.
```

Managing test worktrees:

```bash
# List active test worktrees
clover test list

# Clean up a specific test worktree
clover test clean 184          # By PR number
clover test clean feature-foo  # By branch name

# Clean up all test worktrees
clover test clean
```

If a worktree has uncommitted changes, `clean` will warn you and ask for confirmation before removing it. You can also `cd` into any worktree to inspect or continue working manually.

### Other Commands

```bash
clover status              # Show in-progress work
clover config              # Show current configuration
clover clear issue 42      # Allow re-processing an issue
clover clear review 7      # Allow re-reviewing a PR
clover clear fix 7          # Allow re-fixing a PR
clover clear --all         # Reset all state
```

## Customizing Claude's Prompts

Clover ships with default prompts for implementation and review. You can override any prompt by placing a file with the same name in `~/.clover/<owner>/<repo>/prompts/`.

Available prompt files:

| Prompt | Purpose |
|--------|---------|
| `implement.md` | Instructions for implementing issues |
| `review.md` | Instructions for reviewing PRs |
| `pre_pr_review.md` | Instructions for self-review before creating a PR |
| `implement_review.md` | Instructions for fixing issues found during self-review |

For example, to customize the implementation prompt for `myorg/myrepo`:

```bash
mkdir -p ~/.clover/myorg/myrepo/prompts
cp scripts/orchestrator/prompts/implement.md ~/.clover/myorg/myrepo/prompts/implement.md
# Edit the file to customize
```

## Troubleshooting

### "Could not detect GitHub repository"
Make sure you're in a git repository with an `origin` remote pointing to GitHub:
```bash
git remote -v  # Should show a github.com URL
```

### "No Clover configuration found"
```bash
clover init  # Run from your repository directory
```

### "github.token is required"
```bash
gh auth login              # Recommended
# Or set GITHUB_TOKEN environment variable
```

### Worktree issues
```bash
git worktree list          # See all worktrees
git worktree remove ./worktrees/clover-issue-42 --force
git worktree prune         # Clean up stale references
```

### Re-process an item
```bash
clover clear issue 42      # Then re-label the issue
```

### Logs
Clover logs to `~/.clover/clover.log`. Check this file for detailed error information.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
