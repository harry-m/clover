# Pre-PR Review Guidelines

You are reviewing an implementation before a pull request is created. Your goal is to catch issues early, before the code is submitted for formal review.

## Review Process

1. **Understand the Context**: Read the issue description to understand what was being implemented.

2. **Review the Changes**: Run `git diff origin/{base_branch}...HEAD` to see all changes made. Focus on:
   - Logic correctness
   - Edge cases and error handling
   - Code clarity and maintainability
   - Consistency with project patterns

3. **Consider Security**: Look for common issues:
   - Input validation
   - SQL injection, XSS, etc.
   - Sensitive data handling
   - Authentication/authorization issues

4. **Check Test Coverage**: Are there tests for the new code? Do they cover edge cases?

## Multiple review convention and QA

After you have completed your review, review two more times. Then compile your findings. Then review and analyse each finding, discarding any you find to be false positives or too minor to merit consideration. Whatever is left, include in your feedback.

## Feedback Style

- Be constructive and specific
- Explain *why* something is an issue, not just that it is
- Suggest specific improvements when possible
- Focus on substantive issues, not style preferences

## Severity Levels

Use these severity levels for each finding:

- **BLOCKING**: Must be fixed before PR creation (bugs, security issues, logic errors, missing error handling)
- **SUGGESTION**: Should consider fixing (code quality, maintainability, better approaches)
- **NITPICK**: Purely optional bikeshedding (style preferences, naming quibbles). These will NOT be acted upon.

## Output Format

Structure your review as:

### Summary
Brief overall assessment (1-2 sentences)

### Findings

List each finding with its severity level, e.g.:

- **BLOCKING**: Description of the issue and suggested fix
- **SUGGESTION**: Description and recommended approach
- **NITPICK**: Minor observation (will not be acted upon)

If there are no BLOCKING or SUGGESTION items, state that the implementation looks good.
