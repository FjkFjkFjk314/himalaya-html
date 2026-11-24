---
cairn: spec
capability: mail-web
status: current
---

# Read-only mail reader

The standalone reader SHALL default to loopback TCP. The hosted reader SHALL use a Unix socket in a service-owned directory with mode 0700 and restrict the socket to its owner. Both listeners SHALL bound concurrent request handling. The reader SHALL use a fixed, configured read-only MCP command for each account. It SHALL serve a paginated mailbox listing, message text, downloadable attachments and a stylesheet. It SHALL escape untrusted text, reduce HTML mail to text, return private mail responses with no-store, and exclude message bodies and query strings from its logs. The hosted deployment SHALL place its pages, downloads and stylesheet behind password and TOTP verification on a dedicated HTTPS site.

In named mode, the reader SHALL display an account switcher, open the first configured account on the bare home page and include the selected alias in folder, pagination, message and attachment links. It SHALL reject unknown, blank and repeated aliases and require an alias on queries and deep links unless a configured legacy alias handles missing account parameters. The single-command configuration SHALL continue accepting its existing URLs.

The hosted site proxy SHALL reach the authentication service through a private Unix socket. The project proxy, authentication service and session store SHALL use read-only container root filesystems, drop Linux capabilities, disable privilege escalation and bound container log retention.
