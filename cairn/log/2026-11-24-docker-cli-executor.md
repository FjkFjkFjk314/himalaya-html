---
cairn: log
change: docker-mail-mcp-web
landed: 2026-11-24
---

# Docker CLI executor and synthetic mail tests

The first packaging increment adds a multi-stage image for the locked CLI and a Compose executor with a persistent state volume. The deployment example mounts an external account configuration and reads IMAP and SMTP passwords through Compose secrets. CI scans incoming commits with redacted output and exercises the CLI with synthetic mail, a local Maildir and an isolated SMTP receiver.

Capability: packaging. The multi-account gateway, approval service and public reader remain in the active proposal.
