---
cairn: tasks
change: hermes-mail-triage
---
- [x] Confirm the user's provider selection, permitted mail excerpts and notification destination.
- [ ] Verify the image digest, ACP support, effective container privileges and permitted egress.
- [x] Connect the read-only synchronized inboxes, persist per-mailbox cursors and classify new messages without changing read flags.
- [x] Validate classifications and exercise Bark delivery, duplicate suppression, bounded retries and restart recovery.
- [ ] Test promotional, actionable, ambiguous and malicious synthetic mail, including embedded instructions, remote images, attachments and oversized inputs. Check actual outbound data and logs.
- [ ] Register a restricted Lody ACP agent, verify an owner chat round trip, and test stopping and rolling back the project independently.
- [ ] Test offline OCR with embedded and attached images, misleading filenames, oversized images, tracking URLs and instructions visible only inside an image.
- [ ] Verify owner memory recall through both Lody and the local CLI after restart. Verify only important mail creates factual records and retrieved mail cannot grant tools.
- [x] Exercise the watchdog threshold, individual mailbox and whole-host outages, recovery, and submission of a time-sensitive test alert.
- [ ] Confirm the time-sensitive alert appeared on the owner’s phone.
