"""Serve mail from trusted, fixed-account, read-only Himalaya MCP commands."""

import argparse
import base64
import html
import json
import os
import selectors
import subprocess
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

PAGE_SIZE = 25
TIMEOUT = 30
MAX_RESPONSE = 20 * 1024 * 1024
CSS = Path(__file__).with_name("reader.css")
TOOLS = {"mailbox_list", "envelope_list", "message_read", "attachment_download"}


class BackendError(Exception):
    """The executor returned an unusable response."""


def call_mcp(command, calls):
    """Send one batch of stdio requests with an overall time and size limit."""
    requests = [
        {"id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "himalaya-reader", "version": "1"},
        }},
        {"method": "notifications/initialized"},
    ]
    for request_id, (name, arguments) in enumerate(calls, start=1):
        if name not in TOOLS:
            raise BackendError("unknown tool")
        requests.append({"id": request_id, "method": "tools/call", "params": {
            "name": name, "arguments": arguments,
        }})
    outgoing = "".join(json.dumps({"jsonrpc": "2.0", **request}) + "\n"
                       for request in requests).encode()
    output = bytearray()
    deadline = time.monotonic() + TIMEOUT
    try:
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE)
                    selector.register(process.stdout, selectors.EVENT_READ)
                    sent = 0
                    while selector.get_map():
                        ready = selector.select(max(0, deadline - time.monotonic()))
                        if not ready or time.monotonic() >= deadline:
                            raise BackendError("executor timed out")
                        for key, event in ready:
                            if event == selectors.EVENT_WRITE:
                                try:
                                    sent += os.write(key.fd, outgoing[sent:sent + 4096])
                                except BlockingIOError:
                                    continue
                                if sent == len(outgoing):
                                    selector.unregister(key.fd)
                                    process.stdin.close()
                            else:
                                chunk = os.read(key.fd, 65536)
                                if not chunk:
                                    selector.unregister(key.fd)
                                output.extend(chunk)
                                if len(output) > MAX_RESPONSE:
                                    raise BackendError("response exceeds limit")
                if process.wait(timeout=max(0, deadline - time.monotonic())):
                    raise BackendError("executor exited with an error")
            finally:
                if process.poll() is None:
                    process.kill()
        responses = {reply.get("id"): reply for line in output.splitlines()
                     if (reply := json.loads(line))}
        initialized = responses[0]["result"]
        if initialized["serverInfo"]["name"] != "himalaya":
            raise BackendError("unexpected executor")
        return [responses[i]["result"] for i in range(1, len(calls) + 1)]
    except (OSError, subprocess.SubprocessError, KeyError, ValueError, TypeError,
            AttributeError, RecursionError) as error:
        raise BackendError("invalid executor response") from error


def structured(result, list_key=None):
    data = result.get("structuredContent")
    if not isinstance(data, dict):
        raise BackendError("missing response data")
    # A list can report item-level errors alongside a usable subset.
    if list_key:
        if not isinstance(data.get(list_key), list):
            raise BackendError("could not list items")
    elif result.get("isError"):
        raise BackendError("tool failed")
    return data


class MailText(HTMLParser):
    """Extract text without returning tags, attributes, links or resources."""

    hidden = {"head", "script", "style", "template", "iframe", "object", "svg"}
    blocks = {"p", "div", "section", "br", "li", "tr", "h1", "h2", "h3", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = []
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden:
            self.skip.append(tag)
        elif not self.skip and tag in self.blocks:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
        elif tag in self.blocks:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.chunks.append(data)

    def handle_startendtag(self, tag, attrs):
        if tag not in self.hidden:
            self.handle_starttag(tag, attrs)


def esc(value):
    return html.escape(str(value if value is not None else ""))


def href(path="/", *, account=None, **params):
    if account is not None:
        params = {"account": account, **params}
    return esc(path + ("?" + urlencode(params) if params else ""))


def addresses(items):
    return ", ".join(f'{item["name"]} <{item["email"]}>' if item.get("name")
                     else item["email"] for item in items)


def render_page(folders, mailbox, title, content, account, accounts):
    nav = []
    for folder in folders:
        is_current = (folder["id"] == mailbox or
                      (mailbox == "inbox" and folder.get("role") == "inbox"))
        selected = ' aria-current="page"' if is_current else ""
        link = href(account=account, mailbox=folder["id"])
        nav.append(f'<a href="{link}"{selected}>{esc(folder["name"])}</a>')
    switcher = ""
    if account is not None:
        links = []
        for alias in accounts:
            selected = ' aria-current="true"' if alias == account else ""
            links.append(f'<a href="{href(account=alias)}"{selected}>{esc(alias)}</a>')
        switcher = f'<nav class="accounts" aria-label="Accounts">{"".join(links)}</nav>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} | Himalaya</title><link rel="stylesheet" href="/reader.css"></head>
<body><header><a href="{href(account=account)}">Himalaya</a>{switcher}</header><div class="layout">
<nav aria-label="Folders">{"".join(nav)}</nav><main>{content}</main></div></body></html>"""


def render_envelopes(data, mailbox, page, folder_name, account):
    rows = []
    for row in data["envelopes"]:
        link = href("/message", account=account, mailbox=mailbox, message=row["id"])
        subject = esc(row.get("subject") or "(No subject)")
        sender = esc(addresses(row.get("from", [])))
        sent = esc((row.get("date") or "")[:10])
        rows.append(f'<li><a href="{link}"><span>{subject}</span>'
                    f'<small>{sender}</small><time>{sent}</time></a></li>')
    content = f'<h1>{esc(folder_name)}</h1>'
    if data.get("errors"):
        content += "<p>Some messages could not be listed.</p>"
    content += '<ul class="messages">' + "".join(rows) + "</ul>" if rows else "<p>No messages.</p>"
    links = []
    if page > 1:
        links.append(f'<a href="{href(account=account, mailbox=mailbox, page=page - 1)}">Newer</a>')
    if token := data.get("nextPage"):
        link = href(account=account, mailbox=mailbox, page=page + 1, cursor=token)
        links.append(f'<a href="{link}">Older</a>')
    return content + f'<div class="pagination">{"".join(links)}</div>'


def render_message(message, mailbox, message_id, account):
    headers = message.get("headers", {})
    body = message.get("text")
    if body is None and message.get("html"):
        parser = MailText()
        parser.feed(message["html"])
        parser.close()
        body = "".join(parser.chunks).strip()
    content = f'<a href="{href(account=account, mailbox=mailbox)}">Back to folder</a>'
    content += f'<h1>{esc(message.get("subject") or "(No subject)")}</h1><dl>'
    for label, field in (("From", "from"), ("To", "to"), ("Cc", "cc")):
        if items := headers.get(field):
            content += f"<dt>{label}</dt><dd>{esc(addresses(items))}</dd>"
    if date := headers.get("date"):
        content += f"<dt>Date</dt><dd>{esc(date)}</dd>"
    content += f'</dl><pre>{esc(body or "No readable message body.")}</pre>'
    links = []
    for part in message.get("parts", []):
        if part["role"] in {"attachment", "inline"}:
            link = href("/attachment", account=account, mailbox=mailbox,
                        message=message_id, part=part["id"])
            label = esc(part.get("filename") or f'Part {part["id"]}')
            links.append(f'<li><a href="{link}">{label}</a></li>')
    if links:
        content += "<h2>Attachments</h2><ul>" + "".join(links) + "</ul>"
    return content


class ReaderHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, format, *args):
        # URLs may contain private mailbox, message and pagination identifiers.
        pass

    def send_payload(self, status, content, content_type="text/plain; charset=utf-8",
                     disposition=None):
        if isinstance(content, str):
            content = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'self'; base-uri 'none'; "
                         "frame-ancestors 'none'; form-action 'none'")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in self.server.hosts:
            self.send_payload(403, "Forbidden")
            return
        try:
            target = urlsplit(self.path)
            if target.path == "/reader.css":
                self.send_payload(200, CSS.read_bytes(), "text/css; charset=utf-8")
                return
            if target.path not in {"/", "/message", "/attachment"}:
                self.send_payload(404, "Page not found")
                return
            if len(target.query) > 8192:
                raise ValueError("query exceeds limit")
            fields = parse_qs(target.query, keep_blank_values=True, max_num_fields=8)
            accounts = self.server.accounts
            if "account" in fields:
                aliases = fields.pop("account")
                if len(aliases) != 1 or aliases[0] not in accounts:
                    raise ValueError("invalid account")
                account = aliases[0]
            elif None in accounts:
                account = None
            elif target.path == "/" and not target.query:
                account = next(iter(accounts))
            elif self.server.legacy_account is not None:
                account = self.server.legacy_account
            else:
                raise ValueError("missing account")
            command = accounts[account]
            query = {key: next((value for value in values if value), "")
                     for key, values in fields.items() if any(values)}
            mailbox = query.get("mailbox", "inbox")
            arguments = {"mailbox": mailbox}
            if target.path == "/":
                page = int(query.get("page", "1"))
                if not 1 <= page <= 1_000_000:
                    raise ValueError("invalid page")
                arguments["page-size"] = PAGE_SIZE
                if cursor := query.get("cursor"):
                    arguments["page-token"] = cursor
                else:
                    arguments["page"] = page
                tool = "envelope_list"
            else:
                if not (message_id := query.get("message")):
                    raise ValueError("missing message")
                arguments["message-id"] = message_id
                tool = "message_read"
            if target.path == "/attachment":
                if not (part := query.get("part")):
                    raise ValueError("missing part")
                arguments["attachment-id"] = part
                self.download(command, arguments)
                return
            folders, result = call_mcp(command, [("mailbox_list", {}), (tool, arguments)])
            folders = structured(folders, "mailboxes")["mailboxes"]
            folder_name = next((folder["name"] for folder in folders if folder["id"] == mailbox),
                               "Inbox" if mailbox == "inbox" else mailbox)
            if tool == "envelope_list":
                title = folder_name
                content = render_envelopes(structured(result, "envelopes"),
                                           mailbox, page, folder_name, account)
            else:
                message = structured(result)
                title = message.get("subject") or "(No subject)"
                content = render_message(message, mailbox, message_id, account)
            self.send_payload(200, render_page(folders, mailbox, title, content, account, accounts),
                              "text/html; charset=utf-8")
        except ValueError:
            self.send_payload(400, "Invalid request")
        except (BackendError, AttributeError, KeyError, TypeError):
            self.send_payload(502, "Mail is temporarily unavailable.")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def download(self, command, arguments):
        result, = call_mcp(command, [("attachment_download", arguments)])
        metadata = structured(result)
        try:
            resource, = [item["resource"] for item in result["content"] if item["type"] == "resource"]
            binary = base64.b64decode(resource["blob"], validate=True)
            if len(binary) != metadata["size"]:
                raise ValueError("unexpected size")
        except (KeyError, ValueError, TypeError) as error:
            raise BackendError("invalid attachment") from error
        filename = str(metadata.get("filename") or "attachment")
        filename = "".join(c for c in filename if c.isprintable() and c not in "/\\")[:150]
        if filename in {"", ".", ".."}:
            filename = "attachment"
        disposition = "attachment; filename=\"attachment\"; filename*=UTF-8''" + quote(filename, safe="")
        self.send_payload(200, binary, "application/octet-stream", disposition)


def valid_command(command):
    return isinstance(command, list) and bool(command) and all(
        isinstance(arg, str) and arg and "\0" not in arg for arg in command
    )


def unique_aliases(pairs):
    accounts = dict(pairs)
    if len(accounts) != len(pairs):
        raise ValueError("duplicate account alias")
    return accounts


def load_accounts(environ):
    aliases = environ.get("HIMALAYA_READER_ACCOUNTS")
    legacy = environ.get("HIMALAYA_READER_MCP_COMMAND")
    legacy_account = environ.get("HIMALAYA_READER_LEGACY_ACCOUNT")
    if aliases is not None:
        if legacy is not None:
            raise ValueError("set only one reader account configuration variable")
        try:
            accounts = json.loads(aliases, object_pairs_hook=unique_aliases)
        except (ValueError, RecursionError):
            accounts = None
        if not isinstance(accounts, dict) or not accounts or not all(
            alias.isprintable() and alias.strip() and valid_command(command)
            for alias, command in accounts.items()
        ):
            raise ValueError(
                "HIMALAYA_READER_ACCOUNTS must map unique aliases to non-empty JSON argv arrays"
            )
        if legacy_account is not None and legacy_account not in accounts:
            raise ValueError("HIMALAYA_READER_LEGACY_ACCOUNT must name a configured account")
        return accounts, legacy_account
    if legacy_account is not None:
        raise ValueError("HIMALAYA_READER_LEGACY_ACCOUNT requires HIMALAYA_READER_ACCOUNTS")
    try:
        command = json.loads(legacy if legacy is not None else "null")
        if valid_command(command):
            return {None: command}, None
    except (ValueError, RecursionError):
        pass
    raise ValueError("HIMALAYA_READER_MCP_COMMAND must be a non-empty JSON argv array")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host-header", action="append", default=[],
                        help="add an exact Host value allowed from an authenticated proxy")
    args = parser.parse_args()
    try:
        accounts, legacy_account = load_accounts(os.environ)
    except ValueError as error:
        parser.error(str(error))
    with HTTPServer(("127.0.0.1", args.port), ReaderHandler) as server:
        server.accounts = accounts
        server.legacy_account = legacy_account
        server.hosts = {"127.0.0.1", f"127.0.0.1:{server.server_port}", "localhost",
                        f"localhost:{server.server_port}", *args.host_header}
        server.serve_forever()


if __name__ == "__main__":
    main()
