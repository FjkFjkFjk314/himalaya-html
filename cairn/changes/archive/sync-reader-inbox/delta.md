---
cairn: delta
change: sync-reader-inbox
---

## MODIFIED Requirements

In synchronized mode, the backend SHALL periodically refresh the folder list and first inbox page for each configured account. The reader SHALL serve the latest complete snapshot and MAY briefly reuse previously opened messages within account-scoped time, size, and count limits. Other folder pages and attachments SHALL remain available on demand. Standalone live mode SHALL remain compatible.

## ADDED Requirements

The inbox SHALL display its account's last successful synchronization time with an explicit timezone and offer an authenticated, same-origin manual refresh action. A pending or failed refresh SHALL preserve any previous complete snapshot and its success timestamp. Successful refreshes SHALL update the displayed list and timestamp. Concurrent refresh requests for the same account SHALL share a single backend job. Reading and refreshing the inbox SHALL preserve mail flags.
