# Read-only mail reader

The reader serves one fixed account using Python 3.9+ and the Himalaya stdio
MCP protocol. Run it on the proxy host. It listens only on IPv4 loopback and
relies on an authenticating reverse proxy to protect all routes, including
direct message and attachment URLs. Keep the loopback listener private to the
reader and trusted proxy operators. For the two-factor deployment, see
[Seoul ingress](../seoul/).

## Start the reader

Configure the following environment variable on the service. The value is a
JSON array of arguments.

```sh
export HIMALAYA_READER_MCP_COMMAND='["ssh","-T","-o","BatchMode=yes","-o","StrictHostKeyChecking=yes","himalaya-reader-executor"]'
python3 deploy/reader/reader.py --port 8080 --host-header mail.example.com
```

Configure the SSH host alias to use the dedicated private executor address,
an independently verified host key, and a dedicated key kept outside the
repository. On the executor, restrict that key to the fixed-account read-only
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

The reader allows loopback Host headers by default. Set `--host-header` to
the exact value sent by the proxy, or have the proxy send
`Host: 127.0.0.1:8080`. Configure the proxy to authenticate every route and
avoid logging query strings, which contain mailbox and message identifiers.

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
