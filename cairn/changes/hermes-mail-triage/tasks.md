---
cairn: tasks
change: hermes-mail-triage
---
- [x] Confirm the user's provider selection, permitted mail excerpts and notification destination.
- [x] Verify the image digest, ACP support, effective container privileges and permitted egress.
- [x] Connect the read-only synchronized inboxes, persist per-mailbox cursors and classify new messages without changing read flags.
- [x] Validate classifications and exercise Bark delivery, duplicate suppression, bounded retries and restart recovery.
- [x] Exercise actionable, promotional, empty and malicious synthetic excerpts and the delivery state machine.
- [x] Register a restricted Lody ACP agent and verify a synthetic owner chat round trip. Document project-specific stop and restart commands.
- [x] Exercise isolated image decoding, unsupported images, remote image avoidance, OCR-only instructions and the unreadable-image review path.
- [x] Verify saved owner facts across fresh ACP and CLI sessions, with write access limited to explicit owner input. Verify selective important-mail records and read-only access.
- [x] Exercise the watchdog threshold, individual mailbox and whole-host outages, recovery, and submission of a time-sensitive test alert.
- [ ] Confirm the time-sensitive alert appeared on the owner’s phone.
