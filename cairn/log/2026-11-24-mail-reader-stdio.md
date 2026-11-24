---
cairn: log
change: docker-mail-mcp-web
landed: 2026-11-24
---

# Single-account deployment gateway and reader

The stdio wrapper restricts the native Docker executor to mailbox reads and two flag changes. Its read-only mode supplies the loopback HTML reader. The Seoul deployment templates provide a dedicated reader service, private SSH transport and a site authenticated by Authelia.

Capabilities: mcp, mail-web. Public rollout and provider acceptance are tracked in the active change.
