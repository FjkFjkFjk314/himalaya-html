"""Run against a built Himalaya binary with a disposable synthetic account."""

import html
import http.client
import os
import re
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

from reader import BackendError, ReaderHandler, call_mcp

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
        inbox = root / "mail" / "inbox"
        for sub in ("cur", "new", "tmp"):
            (inbox / sub).mkdir(parents=True)
        self.message = inbox / "cur" / "m1:2,"
        self.message.write_bytes(MESSAGE.replace("\n", "\r\n").encode())
        config = root / "config.toml"
        config.write_text('[accounts.reader]\nemail = "reader@example.org"\n'
                          f'maildir.root = "{root / "mail"}"\n')
        server = HTTPServer(("127.0.0.1", 0), ReaderHandler)
        server.hosts = {f"127.0.0.1:{server.server_port}"}
        server.command = [
            sys.executable, str(GATEWAY), "--read-only", "--", str(BINARY),
            "-c", str(config), "-a", "reader", "--log-level", "off", "mcp",
        ]
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
            call_mcp(self.server.command, [("mailbox_list", {})])


if __name__ == "__main__":
    unittest.main()
