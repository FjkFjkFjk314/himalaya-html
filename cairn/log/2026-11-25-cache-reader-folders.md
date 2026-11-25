---
cairn: log
change: cache-reader-folders
landed: 2026-11-25
---

# Reuse folder lists during navigation

The reader reuses complete and successful folder lists for up to one minute per account and fixed command. A cache miss still batches the folder and page queries. Message lists, messages, and attachments are fetched for each request.

Capability: mail-web.
