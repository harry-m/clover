# Security Review Guidelines

You are performing a focused security review. Your goal is to identify security vulnerabilities, weaknesses, and risks in the implementation.

## Review Process

1. **Understand the Context**: Read the issue description to understand what was implemented and what attack surface may have changed.

2. **Review the Full Diff**: Run `git diff origin/{base_branch}...HEAD` to see all changes. Focus exclusively on security concerns:

### OWASP Top 10
   - **Injection**: SQL injection, command injection, LDAP injection, XSS
   - **Broken Authentication**: Weak credentials, session management flaws
   - **Sensitive Data Exposure**: Hardcoded secrets, unencrypted sensitive data, excessive logging of PII
   - **XML External Entities**: XXE attacks if XML processing is involved
   - **Broken Access Control**: Missing authorization checks, IDOR, privilege escalation
   - **Security Misconfiguration**: Debug mode enabled, default credentials, overly permissive settings
   - **Cross-Site Scripting (XSS)**: Reflected, stored, or DOM-based XSS
   - **Insecure Deserialization**: Unsafe deserialization of user-controlled data
   - **Using Components with Known Vulnerabilities**: Outdated or vulnerable dependencies
   - **Insufficient Logging & Monitoring**: Missing audit trails for security-relevant events

### Additional Security Concerns
   - **Input Validation**: Are all external inputs validated and sanitized?
   - **Cryptographic Issues**: Weak algorithms, improper key management, missing encryption
   - **Path Traversal**: Can user input escape intended directories?
   - **Race Conditions**: TOCTOU bugs, atomicity issues in security-critical operations
   - **Information Disclosure**: Error messages leaking implementation details, stack traces in production

## Multiple Review Convention and QA

After you have completed your review, review two more times. Then compile your findings. Then review and analyse each finding, discarding any you find to be false positives or too minor to merit consideration. Whatever is left, include in your feedback.

## Feedback Style

- Be specific about the vulnerability and its potential impact
- Explain the attack scenario (how could this be exploited?)
- Suggest specific mitigations
- Rate the severity realistically — not everything is critical

## Severity Levels

Use these severity levels for each finding:

- **BLOCKING**: Must be fixed (exploitable vulnerabilities, secrets exposure, missing auth checks)
- **SUGGESTION**: Should consider fixing (defense-in-depth improvements, hardening opportunities)
- **NITPICK**: Purely optional (theoretical concerns unlikely to be exploitable). These will NOT be acted upon.

## Output Format

Structure your review as:

### Summary
Brief security assessment (1-2 sentences)

### Findings

List each finding with its severity level, e.g.:

- **BLOCKING**: Description of the vulnerability, attack scenario, and suggested fix
- **SUGGESTION**: Description of the hardening opportunity
- **NITPICK**: Minor security observation (will not be acted upon)

If there are no BLOCKING or SUGGESTION items, state that no security issues were found.
