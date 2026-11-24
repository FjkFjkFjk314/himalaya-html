---
cairn: spec
capability: mail-web
status: current
---

# Read-only mail reader

The single-account reader SHALL bind to loopback and use a configured read-only MCP process. It SHALL serve a paginated mailbox listing, message text, downloadable attachments and a stylesheet. It SHALL escape untrusted text, reduce HTML mail to text, return private mail responses with no-store, and exclude message bodies and query strings from its logs. The hosted deployment SHALL place its pages, downloads and stylesheet behind password and TOTP verification on a dedicated HTTPS site.
