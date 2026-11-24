---
cairn: log
change: reader-account-switching
landed: 2026-11-24
---

# Account switching for the read-only mail reader

The reader can select from named fixed-account executors. Account links preserve scope across folders, pagination, messages and attachment downloads. Existing single-command deployments continue to work, and an optional named fallback preserves legacy bookmarks during migration. Synthetic Maildir tests exercised separate executors with overlapping message IDs.

Capability: mail-web.
