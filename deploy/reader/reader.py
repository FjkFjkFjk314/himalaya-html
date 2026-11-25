"""Serve mail from trusted, fixed-account, read-only Himalaya MCP commands."""

import argparse
import base64
import html
import json
import os
import re
import selectors
import stat
import subprocess
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer
from threading import BoundedSemaphore, Lock
from urllib.parse import parse_qs, quote, urlencode, urlsplit

PAGE_SIZE = 25
TIMEOUT = 30
MAX_CONCURRENT_REQUESTS = 3
FOLDER_CACHE_TTL = 60
MAX_RESPONSE = 20 * 1024 * 1024
MAX_REFRESH_BODY = 4096
CSS = Path(__file__).with_name("reader.css")
TOOLS = {"mailbox_list", "envelope_list", "message_read", "attachment_download",
         "reader_snapshot", "reader_refresh"}


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


class FolderCache:
    def __init__(self):
        self.entries = {}
        self.lock = Lock()

    def fetch(self, account, command, tool, arguments):
        command_key = tuple(command)
        started = time.monotonic()
        with self.lock:
            cached = self.entries.get(account)
        if cached and cached[0] == command_key and started - cached[1] < FOLDER_CACHE_TTL:
            result, = call_mcp(command, [(tool, arguments)])
            return cached[2], result

        reply, result = call_mcp(command, [("mailbox_list", {}), (tool, arguments)])
        data = structured(reply, "mailboxes")
        folders = data["mailboxes"]
        if not reply.get("isError") and not data.get("errors") and not data.get("nextPage") and all(
            isinstance(folder, dict) and isinstance(folder.get("id"), str)
            and isinstance(folder.get("name"), str) for folder in folders
        ):
            with self.lock:
                cached = self.entries.get(account)
                if cached is None or cached[1] <= started:
                    self.entries[account] = (command_key, started, folders)
        return folders, result


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


def render_page(folders, mailbox, title, content, account, accounts, *, synced=False, poll=False):
    nav = []
    for folder in folders:
        is_current = (folder["id"] == mailbox or
                      (mailbox == "inbox" and folder.get("role") == "inbox"))
        selected = ' aria-current="page"' if is_current else ""
        inbox = synced and (folder.get("role") == "inbox" or folder["id"] == "inbox")
        link = href(account=account) if inbox else href(account=account, mailbox=folder["id"])
        nav.append(f'<a href="{link}"{selected}>{esc(folder["name"])}</a>')
    switcher = ""
    if account is not None:
        links = []
        for alias in accounts:
            selected = ' aria-current="true"' if alias == account else ""
            links.append(f'<a href="{href(account=alias)}"{selected}>{esc(alias)}</a>')
        switcher = f'<nav class="accounts" aria-label="Accounts">{"".join(links)}</nav>'
    refresh = f'<meta http-equiv="refresh" content="3;url={href(account=account)}">' if poll else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} | Himalaya</title><link rel="stylesheet" href="/reader.css">{refresh}</head>
<body><header><a href="{href(account=account)}">Himalaya</a>{switcher}</header><div class="layout">
<nav aria-label="Folders">{"".join(nav)}</nav><main>{content}</main></div></body></html>"""


def render_envelopes(data, mailbox, page, folder_name, account, *, controls=""):
    rows = []
    for row in data["envelopes"]:
        link = href("/message", account=account, mailbox=mailbox, message=row["id"])
        subject = esc(row.get("subject") or "(No subject)")
        sender = esc(addresses(row.get("from", [])))
        sent = esc((row.get("date") or "")[:10])
        rows.append(f'<li><a href="{link}"><span>{subject}</span>'
                    f'<small>{sender}</small><time>{sent}</time></a></li>')
    content = f'<h1>{esc(folder_name)}</h1>{controls}'
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


def render_snapshot(snapshot, account, accounts):
    folders, data, last_success, refreshing, failed = (
        snapshot[key] for key in ("folders", "envelopes", "last_success", "refreshing", "failed")
    )
    if (not isinstance(folders, list) or type(refreshing) is not bool or type(failed) is not bool
            or (data is not None and
                (not isinstance(data, dict) or not isinstance(data.get("envelopes"), list)))
            or (last_success is not None and type(last_success) not in (int, float))):
        raise BackendError("invalid inbox snapshot")
    stamp = "上次同步 暂无成功记录"
    if last_success is not None:
        try:
            instant = datetime.fromtimestamp(last_success, timezone.utc)
        except (ValueError, OverflowError, OSError) as error:
            raise BackendError("invalid sync time") from error
        stamp = (f'上次同步 <time datetime="{instant.isoformat()}">'
                 f'{instant:%Y-%m-%d %H:%M:%S} UTC</time>')
    if refreshing:
        status = "正在刷新收件箱…"
    elif failed:
        status = "刷新失败，请重试。"
    elif data is None:
        status = "正在等待首次同步…"
    else:
        status = ""
    if status:
        stamp += f'<br><span>{status}</span>'
    controls = (
        f'<div class="sync-controls" lang="zh-CN"><p role="status">{stamp}</p>'
        f'<form action="/refresh" method="post"><input type="hidden" name="account" '
        f'value="{esc(account)}"><button type="submit">立即刷新</button></form></div>'
    )
    if data is None:
        content = f"<h1>Inbox</h1>{controls}"
    else:
        content = render_envelopes(data, "inbox", 1, "Inbox", account, controls=controls)
    poll = refreshing or (data is None and not failed)
    return render_page(folders, "inbox", "Inbox", content, account, accounts, synced=True, poll=poll)


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
                     disposition=None, *, location=None, refresh_form=False):
        if isinstance(content, str):
            content = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Browsers can send Origin: null on a POST from a no-referrer document.
        self.send_header("Referrer-Policy", "same-origin" if refresh_form else "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        form_source = "'self'" if refresh_form else "'none'"
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'self'; base-uri 'none'; "
                         f"frame-ancestors 'none'; form-action {form_source}")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        if location:
            self.send_header("Location", location)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def do_HEAD(self):
        self.do_GET()

    def allowed_host(self):
        hosts = self.headers.get_all("Host", [])
        return len(hosts) == 1 and hosts[0] in self.server.hosts

    def do_POST(self):
        if not self.server.sync_origin:
            self.send_error(501)
            return
        if not self.allowed_host() or self.headers.get_all("Origin") != [self.server.sync_origin]:
            self.send_payload(403, "Forbidden")
            return
        if self.path != "/refresh":
            self.send_payload(404, "Page not found")
            return
        try:
            if self.headers.get_all("Transfer-Encoding"):
                raise ValueError("unsupported transfer encoding")
            if self.headers.get_all("Content-Type") != ["application/x-www-form-urlencoded"]:
                self.send_payload(415, "Unsupported form type")
                return
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                raise ValueError("invalid body length")
            length = int(lengths[0])
            if length > MAX_REFRESH_BODY:
                self.send_payload(413, "Form is too large")
                return
            body = self.rfile.read(length)
            if len(body) != length or re.search(rb"%(?![0-9A-Fa-f]{2})", body):
                raise ValueError("invalid form body")
            form = parse_qs(body.decode("ascii"), keep_blank_values=True,
                            strict_parsing=True, errors="strict", max_num_fields=1)
            if set(form) != {"account"} or len(form["account"]) != 1:
                raise ValueError("invalid form")
            account = form["account"][0] or None
            if account not in self.server.accounts:
                raise ValueError("invalid account")
            command = self.server.accounts[account]
            result, = call_mcp(command, [("reader_refresh", {})])
            structured(result)
            self.send_payload(303, "", location=href(account=account))
        except ValueError:
            self.send_payload(400, "Invalid request")
        except (BackendError, AttributeError, KeyError, TypeError):
            self.send_payload(502, "Mail is temporarily unavailable.")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def do_GET(self):
        if not self.allowed_host():
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
                if self.server.sync_origin and mailbox == "inbox" and page == 1 and not query.get("cursor"):
                    result, = call_mcp(command, [("reader_snapshot", {})])
                    content = render_snapshot(structured(result), account, accounts)
                    self.send_payload(200, content, "text/html; charset=utf-8", refresh_form=True)
                    return
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
            folders, result = self.server.folder_cache.fetch(account, command, tool, arguments)
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
            content = render_page(folders, mailbox, title, content, account, accounts,
                                  synced=bool(self.server.sync_origin))
            self.send_payload(200, content, "text/html; charset=utf-8")
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


def load_sync_origin(environ):
    sync = environ.get("HIMALAYA_READER_SYNC", "0")
    if sync == "0":
        return None
    if sync != "1":
        raise ValueError("HIMALAYA_READER_SYNC must be 0 or 1")
    origin = environ.get("HIMALAYA_READER_ORIGIN", "")
    message = "HIMALAYA_READER_ORIGIN must be an HTTP or HTTPS origin without a path"
    try:
        parsed = urlsplit(origin)
        parsed.port
        if (parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None
                and origin == f"{parsed.scheme}://{parsed.netloc}"
                and re.fullmatch(r"[!-~]+", origin)):
            return origin
    except ValueError:
        raise ValueError(message) from None
    raise ValueError(message)


class BoundedThreadingMixIn(ThreadingMixIn):
    daemon_threads = True

    def __init__(self, *args):
        self.folder_cache = FolderCache()
        self.sync_origin = None
        self.request_slots = BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        super().__init__(*args)

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


class LoopbackHTTPServer(BoundedThreadingMixIn, HTTPServer):
    pass


class UnixHTTPServer(BoundedThreadingMixIn, UnixStreamServer):
    def __init__(self, path, handler):
        if not path.is_absolute():
            raise ValueError("the reader socket path must be absolute")
        directory = path.parent.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or stat.S_IMODE(directory.st_mode) != 0o700
                or directory.st_uid != os.geteuid()):
            raise ValueError("the socket directory must be private (mode 0700) and owned by the reader")
        self.socket_path = path
        self.bound = False
        super().__init__(str(path), handler)

    def server_bind(self):
        previous_umask = os.umask(0o177)
        try:
            super().server_bind()
            self.bound = True
        finally:
            os.umask(previous_umask)

    def server_close(self):
        super().server_close()
        if self.bound:
            self.socket_path.unlink(missing_ok=True)
            self.bound = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    listener = parser.add_mutually_exclusive_group()
    listener.add_argument("--port", type=int, default=8080, help="TCP loopback port")
    listener.add_argument("--unix-socket", type=Path, help="Unix socket in a private directory")
    parser.add_argument("--host-header", action="append", default=[],
                        help="add an exact Host value allowed from an authenticated proxy")
    args = parser.parse_args()
    try:
        accounts, legacy_account = load_accounts(os.environ)
        sync_origin = load_sync_origin(os.environ)
        if args.unix_socket:
            server = UnixHTTPServer(args.unix_socket, ReaderHandler)
            hosts = {"localhost"}
        else:
            server = LoopbackHTTPServer(("127.0.0.1", args.port), ReaderHandler)
            hosts = {"127.0.0.1", f"127.0.0.1:{server.server_port}", "localhost",
                     f"localhost:{server.server_port}"}
    except OSError as error:
        parser.error(f"cannot open reader listener: {error}")
    except ValueError as error:
        parser.error(str(error))
    with server:
        server.accounts = accounts
        server.legacy_account = legacy_account
        server.sync_origin = sync_origin
        server.hosts = hosts.union(args.host_header)
        server.serve_forever()


if __name__ == "__main__":
    main()
