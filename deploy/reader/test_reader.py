"""Run against a built Himalaya binary with a disposable synthetic account."""

import html
import http.client
import os
import re
import socket
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import patch

from reader import (
    BackendError, LoopbackHTTPServer, MAX_CONCURRENT_REQUESTS, ReaderHandler,
    UnixHTTPServer, call_mcp, load_accounts,
)

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "deploy/mcp/gateway.py"
DEFAULT_BINARY = ROOT / "target/debug/himalaya"
BINARY = Path(os.environ.get("HIMALAYA_READER_TEST_BINARY", DEFAULT_BINARY)).resolve()
MESSAGE = """From: sender@example.org
To: receiver@example.org
Subject: Safety <check>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary=test

--test
Content-Type: text/html

<p>Visible &lt;em&gt; text</p><script>alert('unsafe')</script>
<img src="https://example.org/pixel"><a href="https://example.org">Link label</a>
--test
Content-Type: text/html; name="unsafe.html"
Content-Disposition: attachment; filename="unsafe.html"

<script>alert('attachment')</script>
--test--
"""


@unittest.skipUnless(BINARY.is_file(), "build Himalaya with Maildir support to run the reader smoke test")
class ReaderTest(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        root = Path(fixture.name)
        config = root / "config.toml"
        self.commands = {}
        self.messages = {}
        with config.open("w") as stream:
            for account in ("school", "163"):
                inbox = root / account / "inbox"
                for sub in ("cur", "new", "tmp"):
                    (inbox / sub).mkdir(parents=True)
                message = inbox / "cur" / "m1:2,"
                self.messages[account] = message
                if account == "school":
                    self.message = message
                    fixture_content = MESSAGE
                else:
                    fixture_content = MESSAGE.replace("Safety", "Other account").replace(
                        "alert('attachment')", "alert('163')")
                message.write_bytes(fixture_content.replace("\n", "\r\n").encode())
                stream.write(f'[accounts."{account}"]\nemail = "reader@example.org"\n'
                             f'maildir.root = "{inbox.parent}"\n')
                self.commands[account] = [
                    sys.executable, str(GATEWAY), "--read-only", "--", str(BINARY),
                    "-c", str(config), "-a", account, "--log-level", "off", "mcp",
                ]
        server = LoopbackHTTPServer(("127.0.0.1", 0), ReaderHandler)
        server.hosts = {f"127.0.0.1:{server.server_port}"}
        server.accounts = {None: self.commands["school"]}
        server.legacy_account = None
        self.server = server
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def request(self, target, method="GET", headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            client.request(method, target, headers=headers or {})
            response = client.getresponse()
            return response.status, response.headers, response.read()
        finally:
            client.close()

    def test_list_and_read_keep_flags_and_render_inert_mail(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200, body)
        self.assertIn(b"Safety &lt;check&gt;", body)
        self.assertIn(b"/message?mailbox=inbox&amp;message=m1", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])

        status, _, body = self.request("/message?mailbox=inbox&message=m1&seen=true")
        self.assertEqual(status, 200, body)
        self.assertIn(b"Visible &lt;em&gt; text", body)
        self.assertIn(b"Link label", body)
        self.assertIn(b"unsafe.html", body)
        self.assertNotIn(b"https://example.org", body)
        self.assertNotIn(b"alert(", body)
        self.assertEqual(list(self.message.parent.glob("m1:2,*")), [self.message])

    def test_attachment_is_downloaded_as_opaque_bytes(self):
        status, headers, body = self.request("/attachment?mailbox=inbox&message=m1&part=3")
        self.assertEqual(status, 200, body)
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("filename*=UTF-8''unsafe.html", headers["Content-Disposition"])
        self.assertIn(b"<script>alert('attachment')</script>", body)

    def test_rejects_unknown_host_invalid_query_and_write_request(self):
        self.assertEqual(self.request("/", headers={"Host": "unexpected.invalid"})[0], 403)
        self.assertEqual(self.request("/message")[0], 400)
        self.assertEqual(self.request("/", method="POST")[0], 501)
        status, headers, body = self.request("/", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body, b"")
        self.assertEqual(self.request("/private-mcp")[0], 404)

    def test_open_folder_and_continue_listing(self):
        for index in range(2, 28):
            self.message.with_name(f"m{index}:2,").write_bytes(self.message.read_bytes())
        status, _, body = self.request("/")
        self.assertEqual(status, 200, body)
        self.assertEqual(body.count(b"/message?"), 25)
        folder_link = html.unescape(re.search(rb'<nav[^>]*><a href="([^"]+)', body)[1].decode())

        status, _, folder = self.request(folder_link)
        self.assertEqual(status, 200, folder)
        older_link = html.unescape(re.search(rb'<a href="([^"]+)">Older</a>', folder)[1].decode())
        status, _, older = self.request(older_link)
        self.assertEqual(status, 200, older)
        self.assertEqual(older.count(b"/message?"), 2)
        self.assertIn(b">Newer</a>", older)
        self.assertNotIn(b">Older</a>", older)

    def test_limit_rejects_large_executor_output(self):
        with patch("reader.MAX_RESPONSE", 5), self.assertRaises(BackendError):
            call_mcp(self.commands["school"], [("mailbox_list", {})])

    def test_named_accounts_keep_navigation_and_attachments_scoped(self):
        self.server.accounts = self.commands
        status, _, body = self.request("/")
        self.assertEqual(status, 200, body)
        self.assertIn(b'>Safety &lt;check&gt;<', body)
        self.assertIn(b'href="/?account=school" aria-current="true"', body)
        self.assertIn(b'href="/?account=163"', body)

        second = self.messages["163"]
        for index in range(2, 28):
            second.with_name(f"m{index}:2,").write_bytes(second.read_bytes())
        status, _, body = self.request("/?account=163")
        self.assertEqual(status, 200, body)
        self.assertIn(b'>Other account &lt;check&gt;<', body)
        folder_link = html.unescape(re.search(rb'<nav aria-label="Folders"><a href="([^"]+)',
                                             body)[1].decode())
        self.assertIn("account=163&mailbox=", folder_link)
        status, _, folder = self.request(folder_link)
        self.assertEqual(status, 200, folder)
        self.assertIn(b'>Other account &lt;check&gt;<', folder)
        older_link = html.unescape(re.search(rb'<a href="([^"]+)">Older</a>', folder)[1].decode())
        self.assertIn("account=163", older_link)
        status, _, body = self.request(older_link)
        self.assertEqual(status, 200, body)
        self.assertRegex(body, rb'href="/\?account=163&amp;mailbox=[^"]+&amp;page=1">Newer')
        self.assertRegex(body, rb'/message\?account=163&amp;mailbox=[^"]+&amp;message=')

        status, _, body = self.request("/message?account=163&mailbox=inbox&message=m1")
        self.assertEqual(status, 200, body)
        self.assertIn(b'Other account &lt;check&gt;', body)
        self.assertIn(b'href="/?account=163&amp;mailbox=inbox">Back to folder', body)
        attachment = html.unescape(re.search(rb'href="(/attachment[^"]+)', body)[1].decode())
        self.assertIn("account=163", attachment)
        status, _, body = self.request(attachment)
        self.assertEqual(status, 200, body)
        self.assertIn(b"alert('163')", body)
        status, _, body = self.request(attachment.replace("account=163", "account=school"))
        self.assertEqual(status, 200, body)
        self.assertIn(b"alert('attachment')", body)

    def test_named_accounts_reject_missing_unknown_and_ambiguous_aliases(self):
        self.server.accounts = self.commands
        with patch("reader.call_mcp") as call:
            for url in ("/?mailbox=inbox", "/?account=", "/?account=unknown",
                        "/?account=school&account=school", "/?account=school&account=163",
                        "/message?message=m1", "/attachment?message=m1&part=3"):
                with self.subTest(url=url):
                    self.assertEqual(self.request(url)[0], 400)
            call.assert_not_called()

    def test_legacy_bookmarks_use_only_the_configured_alias(self):
        self.server.accounts = {"163": self.commands["163"], "school": self.commands["school"]}
        self.server.legacy_account = "school"
        status, _, body = self.request("/")
        self.assertEqual(status, 200, body)
        self.assertIn(b"Other account &lt;check&gt;", body)
        status, _, body = self.request("/message?mailbox=inbox&message=m1")
        self.assertEqual(status, 200, body)
        self.assertIn(b"Safety &lt;check&gt;", body)
        self.assertIn(b"account=school&amp;mailbox=inbox", body)
        self.assertEqual(self.request("/message?account=&message=m1")[0], 400)


class ReaderConcurrencyTest(unittest.TestCase):
    def test_slow_connections_leave_other_slots_available_and_slots_are_bounded(self):
        started = Queue()

        class ObservedHandler(ReaderHandler):
            def setup(self):
                super().setup()
                started.put(self.request)

        with LoopbackHTTPServer(("127.0.0.1", 0), ObservedHandler) as server:
            server.hosts = {"localhost"}
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            slow_clients = []
            try:
                # Idle clients each occupy one slot while waiting to send a request.
                for _ in range(MAX_CONCURRENT_REQUESTS - 1):
                    client = socket.create_connection(server.server_address, timeout=2)
                    slow_clients.append(client)
                    started.get(timeout=2)

                with socket.create_connection(server.server_address, timeout=2) as client:
                    client.sendall(b"GET /reader.css HTTP/1.1\r\nHost: localhost\r\n")
                    started.get(timeout=2)
                    with socket.create_connection(server.server_address, timeout=2) as excess:
                        self.assertEqual(excess.recv(1), b"")
                    client.sendall(b"\r\n")
                    response = http.client.HTTPResponse(client)
                    response.begin()
                    self.assertEqual(response.status, 200)
                    response.read()
                    response.close()
            finally:
                for client in slow_clients:
                    client.close()
                server.shutdown()
                thread.join()


class UnixReaderTest(unittest.TestCase):
    def test_private_socket_accepts_proxy_requests_and_is_cleaned_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reader.sock"
            with UnixHTTPServer(path, ReaderHandler) as server:
                server.hosts = {"localhost"}
                self.assertTrue(path.is_socket())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                with self.assertRaises(OSError):
                    UnixHTTPServer(path, ReaderHandler)
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    for host, expected in (("localhost", 200), ("invalid", 403)):
                        with socket.socket(socket.AF_UNIX) as connection:
                            connection.settimeout(5)
                            connection.connect(str(path))
                            connection.sendall(
                                f"GET /reader.css HTTP/1.1\r\nHost: {host}\r\n\r\n".encode()
                            )
                            response = http.client.HTTPResponse(connection)
                            response.begin()
                            self.assertEqual(response.status, expected)
                            response.read()
                            response.close()
                finally:
                    server.shutdown()
                    thread.join()
            self.assertFalse(path.exists())

    def test_refuses_public_directory_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public"
            public.mkdir(mode=0o755)
            symlink = root / "link"
            symlink.symlink_to(root, target_is_directory=True)
            for parent in (public, symlink):
                with self.subTest(directory=parent), self.assertRaises(ValueError):
                    UnixHTTPServer(parent / "reader.sock", ReaderHandler)


class ReaderConfigTest(unittest.TestCase):
    def test_loads_named_and_legacy_accounts(self):
        self.assertEqual(load_accounts({"HIMALAYA_READER_MCP_COMMAND": '["himalaya","mcp"]'}),
                         ({None: ["himalaya", "mcp"]}, None))
        accounts, legacy = load_accounts({
            "HIMALAYA_READER_ACCOUNTS": '{"school":["a"],"163":["b"]}',
            "HIMALAYA_READER_LEGACY_ACCOUNT": "school",
        })
        self.assertEqual(list(accounts.items()), [("school", ["a"]), ("163", ["b"])])
        self.assertEqual(legacy, "school")

    def test_rejects_invalid_or_ambiguous_configuration(self):
        for raw in ('{}', '[]', '{"school":[]}', '{"school":"ssh"}', '{"":["ssh"]}',
                    '{"school":["a"],"school":["b"]}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "HIMALAYA_READER_ACCOUNTS"):
                load_accounts({"HIMALAYA_READER_ACCOUNTS": raw})
        with self.assertRaisesRegex(ValueError, "only one"):
            load_accounts({"HIMALAYA_READER_ACCOUNTS": '{"school":["a"]}',
                           "HIMALAYA_READER_MCP_COMMAND": '["b"]'})
        with self.assertRaisesRegex(ValueError, "HIMALAYA_READER_LEGACY_ACCOUNT"):
            load_accounts({"HIMALAYA_READER_ACCOUNTS": '{"school":["a"]}',
                           "HIMALAYA_READER_LEGACY_ACCOUNT": "unknown"})


if __name__ == "__main__":
    unittest.main()
