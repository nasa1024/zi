# Security Policy

## Reporting a vulnerability

Please do not disclose a suspected vulnerability or exposed credential in a public issue. Use GitHub's private vulnerability reporting for this repository when available; otherwise contact the repository owner privately with:

- the affected component and version or commit;
- minimal reproduction steps;
- the expected impact;
- any suggested mitigation.

Avoid including live credentials, personal data, or unnecessary production data in a report.

## Credential handling

- Keep provider keys in environment variables or a local `.env` file.
- Never commit real credentials to source, examples, fixtures, logs, screenshots, or commit messages.
- Treat a credential as compromised if it ever entered Git history. Revoke or rotate it first, then remove it from every reachable ref and coordinate a force-push.
- `.env.example` must contain placeholders only.

## Supported version

Security fixes target the latest commit on `main`. Older snapshots are not maintained separately.
