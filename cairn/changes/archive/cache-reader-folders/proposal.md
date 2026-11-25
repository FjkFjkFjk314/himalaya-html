---
cairn: change
id: cache-reader-folders
status: landed
created: 2026-11-25
---
# Reuse the folder list during navigation

Keep the successful folder list for each configured mailbox account in memory for one minute. Fetch message lists and content from the provider on every request. Preserve the existing combined call when the folder list expires. Operator-managed transports can independently reuse their authenticated connections.
