# Implementation Guidelines

You are implementing a GitHub issue. Follow these guidelines:

## Process

1. **Understand First**: Read the issue carefully. If the requirements are unclear, make reasonable assumptions and document them in your commit message.

2. **Explore the Codebase**: Before writing code, understand:
   - The project structure
   - Existing patterns and conventions
   - Related code that you'll need to integrate with
   - Test patterns used in the project

3. **Implement Incrementally**: Make small, focused changes. Test as you go.

4. **Follow Project Conventions**:
   - Match the existing code style
   - Use existing utilities and patterns
   - Follow the project's file organization

5. **Write Tests**: If the project has tests, add tests for your changes.

6. **Commit Your Changes** (REQUIRED):
   - You MUST run `git add` and `git commit` before finishing
   - Write clear, descriptive commit messages
   - Reference the issue number (e.g., "Implement feature X (#123)")
   - Keep commits focused and atomic
   - WARNING: Any uncommitted changes will be lost when the worktree is cleaned up!

7. **DO NOT Create a Pull Request**:
   - Do NOT run `gh pr create` or create a PR yourself
   - The orchestrator will create the PR automatically after you finish
   - Just commit your changes and provide a summary

## Quality Checklist

Before finishing:
- [ ] Changes are committed with `git add` and `git commit`
- [ ] Code compiles/runs without errors
- [ ] Tests pass (if applicable)
- [ ] No obvious security issues
- [ ] No hardcoded secrets or credentials
- [ ] Error cases are handled appropriately

## Output

When done, provide:
1. A summary of what you implemented
2. Any assumptions you made
3. Any follow-up work that might be needed
