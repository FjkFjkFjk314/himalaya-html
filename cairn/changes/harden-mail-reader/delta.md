---
cairn: delta
change: harden-mail-reader
---
## MODIFIED Requirements

The production web reader accepts requests from its dedicated site proxy through a filesystem-protected Unix socket. The standalone reader supports loopback TCP for existing clients. The project containers use bounded logs and disable privilege escalation.

The reader SHALL bound concurrent request handling. The hosted authentication service SHALL communicate with the proxy through a dedicated Unix socket.
