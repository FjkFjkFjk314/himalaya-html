---
cairn: log
change: harden-mail-reader
landed: 2026-11-25
---

# Restrict internal access to the hosted reader

Updated the mail-web capability. The site proxy uses Unix sockets for the reader and authentication service. Both reader listener modes cap concurrent requests at three. The proxy runs as the reader UID, project containers drop capabilities and rotate their logs, and the session store uses Redis 8.4.8. Verified anonymous and password-only denial, full two-factor login, both account listings, message details, static assets, the remembered cookie, and logout.
