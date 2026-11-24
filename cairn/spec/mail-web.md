---
cairn: spec
capability: mail-web
status: current
---

# Read-only mail reader

The reader SHALL bind to loopback and use a fixed, configured read-only MCP command for each account. It SHALL serve a paginated mailbox listing, message text, downloadable attachments and a stylesheet. It SHALL escape untrusted text, reduce HTML mail to text, return private mail responses with no-store, and exclude message bodies and query strings from its logs. The hosted deployment SHALL place its pages, downloads and stylesheet behind password and TOTP verification on a dedicated HTTPS site.

In named mode, the reader SHALL display an account switcher, open the first configured account on the bare home page and include the selected alias in folder, pagination, message and attachment links. It SHALL reject unknown, blank and repeated aliases and require an alias on queries and deep links unless a configured legacy alias handles missing account parameters. The single-command configuration SHALL continue accepting its existing URLs.
