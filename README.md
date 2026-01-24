# Clover, the Claude Overseer

A local Python daemon that watches GitHub issues and pull requests, automatically launching Claude Code to implement features and review code.


## Features

- **Automatic Issue Implementation**: Tag an issue → Claude implements → PR created
- **Automated Code Review**: PRs are reviewed with configurable tests, linters, and Claude analysis
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
3. Create a PR and automatically label it `clover`
4. Review its own PR (running your configured tests and linters)
5. Post Claude's review comments

You can then review the PR, make any changes (with Claude's assistance, if you want). When you're happy, commit and push as normal, then merge the PR.

For manual testing, run `clover test <PR>` to checkout a PR branch and launch Claude with full context. When you're done, Clover checks for uncommitted or unpushed changes and returns you to your original branch.


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

# Edit the generated clover.yaml
```

This creates a `clover.yaml` with sensible defaults. At minimum, set your repository:

```yaml
github:
  repo: your-username/your-repo
```

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
# CLOVER_PR_NUMBER - PR number (if applicable)

cp "$CLOVER_PARENT_REPO/.env" "$CLOVER_WORKTREE/.env"
cd "$CLOVER_WORKTREE" && npm install
```

Reference it in clover.yaml:
```yaml
daemon:
  setup_script: scripts/setup-worktree.sh
```

## Configuration

Full `clover.yaml` reference:

```yaml
github:
  repo: owner/repo-name           # Required
  token: ${GITHUB_TOKEN}          # Optional if using gh CLI
  label: clover                   # Label that triggers Clover (default: clover)
  base_branch: main               # PR target branch (default: auto-detect)

daemon:
  poll_interval: 60               # Seconds between polls (default: 60)
  max_concurrent: 2               # Parallel Claude instances (default: 2)
  max_turns: 50                   # Max turns per conversation (default: 50)
  worktree_base: ./worktrees      # Worktree directory (default: ./worktrees)
  state_file: ./.clover-state.json
  setup_script: scripts/setup.sh  # Run after worktree creation

# Commands to run during PR review (before Claude reviews)
review:
  commands:
    - pytest
    - ruff check .
    - mypy src/
```

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
   - Creates a PR linking to the issue
   - Labels the PR `clover` (triggering review)
   - Runs your review commands (tests, linters)
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

These run in the PR's worktree before Claude reviews. Results are included in the review comment.

### Manual Testing with `clover test`

For hands-on testing of PRs:

```bash
# Start testing - checks out PR branch, launches Claude with context
clover test 184          # By PR number
clover test #184         # With hash prefix
clover test feature/foo  # By branch name

# Claude knows the PR/issue context and can look them up

# If you exit Claude, resume the session
clover test resume
```

When you exit Claude, Clover checks for uncommitted or unpushed changes:
- If clean: returns you to your original branch
- If dirty: stays on branch and shows what needs attention

### Other Commands

```bash
clover status              # Show in-progress work
clover config              # Show current configuration
clover clear issue 42      # Allow re-processing an issue
clover clear review 7      # Allow re-reviewing a PR
clover clear --all         # Reset all state
```

## Customizing Claude

Edit the prompt files to customize Claude's behavior:

- `scripts/orchestrator/prompts/implement.md` - Implementation guidelines
- `scripts/orchestrator/prompts/review.md` - Review guidelines

## Troubleshooting

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

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
