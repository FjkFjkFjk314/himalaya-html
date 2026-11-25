---
cairn: log
change: sync-reader-inbox
landed: 2026-11-25
---

# Background inbox synchronization

Updated the mail-web capability with periodic account-scoped inbox snapshots,
bounded message-preview reuse, last-success timestamps and authenticated manual
refresh. Refresh failures preserve the last complete page. Attachments remain
on demand. The standalone live reader and direct MCP clients retain their
existing behavior.
