---
cairn: change
id: hermes-mail-triage
status: active
created: 2026-11-25
---
# Classify incoming mail and notify the owner

Run a digest-pinned Hermes Agent container. Expose owner chat through Lody ACP over stdio and a local Hermes CLI on the agent host. Both interfaces use owner-scoped persistent conversation memory. Run unattended classification separately from interactive chat. Reuse the existing four-minute inbox refresh and read-only mail access. Establish a baseline on first start so historical mail does not trigger a burst of notifications.

Send a bounded, sanitized plain-text excerpt to the selected model provider. Include text recognized locally from embedded or attached raster images after bounded offline decoding and OCR. Treat recognized text as untrusted mail. Keep raw images, other attachments, links, credentials and unnecessary identifiers out of model requests. Accept only an enumerated classification. A separate notifier can deliver a fixed notification to the owner's configured Bark device. Model output and incoming mail cannot select tools, commands, URLs, recipients or notification text.

Keep operational endpoints, keys and device identifiers outside Git. Use non-root containers with restricted network access and no host Docker socket or SSH credentials. Keep important-mail facts in a separate searchable record with message provenance. Chat can retrieve them as untrusted data. Only owner conversations populate conversation history.

Run a watchdog on the notification host, independent of the mail host. Track authenticated host heartbeats and each mailbox’s last successful sync. Send an urgent Bark notification after 15 minutes without the relevant signal, including when the mail host is down.
