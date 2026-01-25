# Review Implementation Guidelines

You are implementing code review suggestions for a pull request. Your goal is to address the feedback from the automated review.

## Process

1. **Read the Review Feedback**: Understand what changes are being requested. Prioritize:
   - **🔴 Blocking**: Must-fix issues (bugs, security problems)
   - **🟡 Suggestions**: Should-fix items (code quality, maintainability)
   - **🟢 Nitpicks**: Optional improvements (style, minor preferences)

2. **Understand the Existing Code**: Before making changes, review:
   - The current implementation and its intent
   - How the suggested changes fit into the existing architecture
   - Any dependencies or related code that might be affected

3. **Make Focused Changes**:
   - Address the specific feedback items
   - Don't make unrelated changes or refactors
   - Keep changes minimal and targeted
   - Run specific tests to verify your fixes (e.g., `pytest path/to/test.py::test_name -v`)
   - Do NOT run the entire test suite - Clover will run it after you commit

4. **Follow Project Conventions**:
   - Match the existing code style
   - Use existing utilities and patterns
   - Maintain consistency with the rest of the codebase

5. **Commit Your Changes** (REQUIRED):
   - You MUST run `git add` and `git commit` before finishing
   - Write clear commit messages describing what was fixed
   - Reference that these are review fixes (e.g., "Address review feedback: fix X")
   - WARNING: Any uncommitted changes will be lost when the worktree is cleaned up!

6. **DO NOT Push or Create PRs**:
   - Do NOT run `git push` or `gh pr create`
   - The orchestrator will push your commits automatically
   - Just commit your changes and provide a summary

## Quality Checklist

Before finishing:
- [ ] Changes are committed with `git add` and `git commit`
- [ ] All blocking issues have been addressed
- [ ] Code compiles/runs without errors
- [ ] You've verified key functionality with targeted tests (Clover will run the full suite)
- [ ] Changes don't introduce new issues

## Output

When done, provide:
1. A summary of what you implemented
2. Which review suggestions were addressed
3. Any suggestions you couldn't implement (and why)
