# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately through the repository's security advisory form
("Report a vulnerability") rather than a public issue. Include steps to reproduce and the
affected versions. We aim to acknowledge reports within 5 business days.

Do **not** include real session cookies, tokens or workspace data in reports; redact them
(`xoxd-…[REDACTED, len=N]`).

## Supported versions

Only the latest released minor version of each SDK receives security fixes during the 0.x series.

## Handling credentials safely

* The `d` session cookie grants full access to the signed-in account. Store it in an OS keychain
  or secret manager; if it must live in a file, restrict it to mode `0600` (the `gabriel` CLI does
  this).
* Prefer a dedicated service account for long-running automation, and alert on `auth.dead`.
* Restrict workspaces with `allowedTeams`, and give LLM tools a `client.scope()` view rather than
  the full client.
* The SDK only sends credentials to `https` Slack hosts and never logs or emits them. If you find a
  path that leaks credentials, report it as a vulnerability.
