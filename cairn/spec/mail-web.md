---
cairn: spec
capability: mail-web
status: current
---

# Read-only mail reader

The standalone reader SHALL default to loopback TCP. The hosted reader SHALL use a Unix socket in a service-owned directory with mode 0700 and restrict the socket to its owner. Both listeners SHALL bound concurrent request handling. The reader SHALL use a fixed, configured read-only MCP command for each account. It SHALL serve a paginated mailbox listing, message text, downloadable attachments and a stylesheet. It SHALL escape untrusted text, reduce HTML mail to text, return private mail responses with no-store, and exclude message bodies and query strings from its logs. The hosted deployment SHALL place its pages, downloads and stylesheet behind password and TOTP verification on a dedicated HTTPS site.

In named mode, the reader SHALL display an account switcher, open the first configured account on the bare home page and include the selected alias in folder, pagination, message and attachment links. It SHALL reject unknown, blank and repeated aliases and require an alias on queries and deep links unless a configured legacy alias handles missing account parameters. The single-command configuration SHALL continue accepting its existing URLs.

The hosted site proxy SHALL reach the authentication service through a private Unix socket. The project proxy, authentication service and session store SHALL use read-only container root filesystems, drop Linux capabilities, disable privilege escalation and bound container log retention.

In live mode, the reader MAY reuse a complete, successful folder list in memory for up to one minute for the same configured account and fixed MCP command. In live mode, message lists, messages, and attachments SHALL be fetched on each request.

In synchronized mode, the backend SHALL periodically refresh the folder list and first inbox page for each configured account. The reader SHALL serve the latest complete snapshot and MAY briefly reuse previously opened messages within account-scoped time, size, and count limits. Other folder pages and attachments SHALL remain available on demand. Standalone live mode SHALL remain compatible.



The inbox SHALL display its account's last successful synchronization time with an explicit timezone and offer an authenticated, same-origin manual refresh action. A pending or failed refresh SHALL preserve any previous complete snapshot and its success timestamp. Successful refreshes SHALL update the displayed list and timestamp. Concurrent refresh requests for the same account SHALL share a single backend job. Reading and refreshing the inbox SHALL preserve mail flags.
