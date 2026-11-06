# Security policy

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** on this
repository. Do not open a public issue for anything that could help someone take over
an account, such as a reset link, code, or token getting past the sanitizer in a
realistic email, a way to make the server change a mailbox, or a sandbox escape.

Include a **de-personalized** sample: replace every real address, name, link, code, and
token with placeholders, and check the sample still reproduces the problem. Never send
real email.

You should get a reply within 7 days. Fixes for confirmed issues are released as soon
as practical, and reporters are credited unless they ask not to be.

## Scope

In scope: the read-only guarantee, sanitizer bypasses, the approval flow, the attachment
sandbox, credential handling, and index file permissions.

Out of scope: the residual risks listed in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)
(privacy from your AI provider, unredacted images you approved, same-user shell agents,
desktop-automation tools that click the approval dialog).

Low-impact filter misses, such as a harmless link format, can go in a public
**Filter bypass** issue.
