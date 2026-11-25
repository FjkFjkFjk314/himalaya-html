# Read-only mail reader

The reader serves configured accounts using Python 3.9+ and the Himalaya stdio
MCP protocol. Run it on the proxy host. Its authenticating reverse proxy
protects every route, including direct message and attachment URLs. Use a
private Unix socket for deployment on a shared host. The reader also supports
an IPv4 loopback listener for standalone use. [Seoul ingress](../seoul/)

## Start the reader

For a single account, set `HIMALAYA_READER_MCP_COMMAND` to a JSON array of
arguments.

```sh
export HIMALAYA_READER_MCP_COMMAND='["ssh","-T","-o","BatchMode=yes","-o","StrictHostKeyChecking=yes","himalaya-reader-executor"]'
python3 deploy/reader/reader.py --port 8080 --host-header mail.example.com
```

For named accounts, use `HIMALAYA_READER_ACCOUNTS` as the service's sole
command configuration. Each key appears as a label in the account switcher.
Each value is the argument array for that account's fixed read-only command.
The first alias opens on the bare home page.

```sh
export HIMALAYA_READER_ACCOUNTS='{"school":["ssh","-T","-o","BatchMode=yes","-o","StrictHostKeyChecking=yes","school-reader-executor"],"163":["ssh","-T","-o","BatchMode=yes","-o","StrictHostKeyChecking=yes","163-reader-executor"]}'
export HIMALAYA_READER_LEGACY_ACCOUNT=school
```

Set the optional `HIMALAYA_READER_LEGACY_ACCOUNT` when migrating bookmarks
from a single-account installation. It maps URLs without an account parameter
to that alias, except for the bare home page. New mailbox, message and download
links always carry the selected alias in named mode. Every authenticated user
of the reader can switch to any configured account.

Configure the SSH host alias to use the dedicated private executor address,
an independently verified host key, and a dedicated key kept outside the
repository. Set up a distinct SSH key and forced command for each account.
On the executor, restrict each key to the matching fixed-account read-only
MCP command. A typical native executor command looks like this.

```sh
himalaya -c /path/to/private/config.toml -a reader --log-level off mcp
```

The executor should apply the same read-only restriction whether the reader
connects over SSH or through a local subprocess. The reader sends only
`mailbox_list`, `envelope_list`, `message_read` and `attachment_download`.
Keep write and send flags disabled on that executor. The SSH client needs
stdin enabled and a raw stdout channel for the MCP messages. A Docker-based
forced command should use `docker compose run --rm -T` with the same fixed
arguments, including the account name.

Use `--unix-socket /absolute/private/directory/reader.sock` to select the
Unix socket listener. Its immediate directory must already exist, have mode
`0700`, and belong to the reader process user. The reader creates the socket
with mode `0600`. Run the proxy with the reader UID and have it send
`Host: localhost`. The standalone IPv4 loopback listener accepts loopback
Host headers. Use `--host-header` to allow another exact Host value as
needed. Configure the proxy to authenticate every route and avoid logging
query strings, which contain mailbox and message identifiers.

Each request reads data on demand. HTML-only mail is converted to escaped
text, with remote assets and clickable links omitted. Attachments are served
as opaque downloads and responses ask the browser to avoid caching. Reads
keep message flags unchanged. The reader gives the executor 30 seconds and
limits its response to 20 MiB, including MCP framing and attachment encoding.
Himalaya's own attachment limit also applies.

## Synthetic smoke test

Build Himalaya with Maildir enabled, then run the reader tests from the
repository root.

```sh
cargo build --locked --bin himalaya
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s deploy/reader -v
```

The tests exercise HTTP through the read-only MCP gateway and use
`target/debug/himalaya` by default. Set
`HIMALAYA_READER_TEST_BINARY` to test another build.

## Background inbox synchronization

Synchronized mode serves the first inbox page from a backend memory cache. A
worker refreshes each account on startup and then on a staggered schedule
(default interval 60 seconds, page size 25). The displayed time is the last
successful inbox refresh. After a failed update, the reader keeps the previous
complete page. The refresh button queues work for the selected account. Opening
a message reads it on demand and may reuse that account's recent preview for
five minutes. Attachments and subsequent folder pages remain on demand.

1. On the executor host, create a private directory owned by the worker and a
   private JSON file mapping each account alias to its fixed **read-only** MCP
   command (an argv array). Start the worker under a service manager. For
   example, with operator-supplied paths:

   ```sh
   python3 -B sync_backend.py --socket "$SYNC_SOCKET" --accounts "$ACCOUNT_MAP" --interval 60
   ```

2. For each account, configure the trusted transport to run
   `sync_bridge.py --socket "$SYNC_SOCKET" --account "$FIXED_ALIAS"` on the
   backend. Choose the alias from the server-side allowlist. Each reader command
   must select the same account as the frontend selector. Keep the broker
   socket accessible only to its service identity.
3. Enable the reader with `HIMALAYA_READER_SYNC=1` and set
   `HIMALAYA_READER_ORIGIN` to the external HTTPS origin, for example
   `https://reader.example.invalid`. Protect the dedicated refresh POST with
   the same authentication as the inbox and forward its Origin header and
   form body. The HTTP response to a refresh redirects back to the account
   inbox, where completion or failure is displayed.
