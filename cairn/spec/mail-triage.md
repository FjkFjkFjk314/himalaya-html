---
cairn: spec
capability: mail-triage
status: current
---

# Mail triage

Each configured inbox has a persistent cursor. The first successful poll establishes a baseline. The feeder processes newer messages in order and preserves mailbox flags. An independent watchdog samples host reachability and each inbox’s last successful sync once per minute. It submits a time-sensitive Bark alert after a signal has been stale for more than fifteen minutes, suppresses per-mailbox alerts during host outages, and rearms after recovery.

The classifier receives bounded, locally redacted text. Offline OCR can supply additional text from size-limited raster images. The pipeline excludes remote images and links from retrieval. Classification allows only important, promotional, spam, or review. Only important messages generate persistent mail records and fixed-content notifications. The owner can retrieve records as untrusted data. Delivery state survives restarts and distinguishes confirmed acceptance from an uncertain outcome.

Owner chat is available over local CLI and Lody ACP. It persists session history. An owner can explicitly save a fact for recall from another session or interface. The model may read important-mail records and owner facts. Only owner input can write owner facts. The owner agent uses a private network with HTTPS egress restricted to the configured inference provider.
