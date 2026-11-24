---
cairn: change
id: harden-mail-reader
status: landed
created: 2026-11-25
---
# Restrict access to the deployed mail reader

The production security review found that other processes on the shared frontend host could reach the reader over loopback. Serve the production reader over a filesystem-protected Unix socket accessible to the site proxy. Preserve the standalone loopback mode and the existing authenticated web routes. Reduce project container privileges and bound container logs. Track platform package updates and credential replacement separately because they involve shared host maintenance and account recovery.
