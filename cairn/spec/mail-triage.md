---
cairn: spec
capability: mail-triage
status: current
---

# Mail triage

Each configured inbox has a persistent cursor. The first successful poll establishes a baseline. The feeder processes newer messages in order and preserves mailbox flags. An independent watchdog samples host reachability and each inbox’s last successful sync once per minute. It submits a critical Bark alert after a signal has been stale for more than fifteen minutes, suppresses per-mailbox alerts during host outages, sends an ordinary recovery notification, and rearms after recovery.

For messages requiring model classification the classifier tries the primary model first. On provider unavailability it sends the same bounded, locally redacted excerpt to the configured fallback model. If neither provider is available the cursor stays on the pending message for retry. Offline OCR can supply additional text from size-limited raster images. The pipeline excludes remote images and links from retrieval. Classification allows important, routine, promotional, spam, or review. Only important messages enter owner mail memory and trigger immediate encrypted mail notifications. Each summary contains a topic, action, and available date or validated excerpt. The sender encrypts the notification with AES-256-CBC, PKCS7 padding and a fresh random IV before forwarding it to the push service. The classifier rates important mail as normal or urgent using its content and the consequences of delay. Normal mail uses active notifications and urgent mail uses time-sensitive notifications. Existing pending messages without an urgency rating use active notifications. The owner can retrieve records as untrusted data. Delivery state survives restarts and distinguishes confirmed acceptance from an uncertain outcome.

Owner chat is available over local CLI and Lody ACP. It persists session history. An owner can explicitly save a fact for recall from another session or interface. The model may read important-mail records and owner facts. Only owner input can write owner facts. The owner agent uses a private network with HTTPS egress restricted to the configured primary and fallback inference providers. Owner chat uses the fallback during primary-provider outages and automatically returns to the primary when available.

Routine GitHub Actions test-run failures are identified from their notification metadata. They appear as 普通 in the inbox and are collected in a single encrypted daily Bark digest at ordinary priority. Repeated failures are counted. Empty digests are skipped. Deployment, production and security incidents remain eligible for individual triage.
