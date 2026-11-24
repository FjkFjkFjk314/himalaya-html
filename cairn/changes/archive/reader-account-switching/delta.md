---
cairn: delta
change: reader-account-switching
---

## MODIFIED Requirements

### Account scope

The loopback reader SHALL use a configured read-only MCP command per named account and offer an account switcher. Named mode SHALL use the first configured account on the bare home page and require a single known account alias for URLs that identify mailbox content. An optional legacy alias SHALL apply to existing bookmarks that omit the account parameter. The alias SHALL carry through folder, pagination, message and attachment navigation. The original single-command configuration SHALL remain compatible.
