---
cairn: delta
change: hermes-mail-triage
---
## ADDED Requirements

For each configured mailbox the service SHALL classify newly synchronized messages as important, promotional, spam or requiring review. Only important messages SHALL trigger a fixed Bark notification. Initial startup SHALL baseline existing mail. Durable message identities SHALL suppress repeat notifications during normal polling and restarts. Ambiguous delivery outcomes SHALL be recorded for review.

The unattended classifier SHALL have no callable tools or mailbox write capability. It SHALL process bounded text including local OCR results. An isolated offline decoder SHALL validate image type and enforce byte, pixel, frame, memory and time limits before or during decoding. External image URLs and active content SHALL never be loaded. Unreadable image-only messages SHALL enter review. Invalid results SHALL enter review. Credentials, message text and device tokens SHALL be excluded from operational logs. The notification destination SHALL be operator configured.

The owner SHALL be able to chat with Hermes through Lody ACP and a local CLI with persistent owner conversation history. Mail SHALL NOT initiate chat actions, update persistent agent instructions or grant access to host resources.

Only important messages SHALL create retrievable mail memory, as deduplicated, bounded factual records with source identifiers. Retrieved records and OCR text SHALL be treated as untrusted data and SHALL NOT modify agent instructions. Promotional, spam and pending-review classifications SHALL create no mail memory.

An independent watchdog SHALL alert the configured Bark device urgently when host heartbeat age or any individual mailbox’s successful-sync age exceeds 15 minutes. Successful empty syncs SHALL count. Failed attempts and stale reports SHALL NOT reset deadlines. A never-seen signal SHALL use the monitoring start time. The watchdog SHALL persist incident state across restarts, suppress duplicate alerts during an outage, rearm after recovery and retry failed delivery. A host outage SHALL produce one host alert, with individual stale mailboxes reassessed when the host returns.
