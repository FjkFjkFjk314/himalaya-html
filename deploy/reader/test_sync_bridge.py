"""Exercise the private bridge with disposable Unix sockets."""

import json
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import unittest

from sync_bridge import frames


class FrameTests(unittest.TestCase):
    def test_complete_frames_with_fragmented_input(self):
        chunks = iter((b"one\nt", b"wo\nthree", b"\n", b""))
        self.assertEqual(
            list(frames(lambda _: next(chunks), 6)),
            [b"one\n", b"two\n", b"three\n"],
        )

    def test_overlong_or_unterminated_frame(self):
        for chunks in [(b"longer\n", b""), (b"unfinished", b""), (b"short", b"")]:
            with self.subTest(chunks=chunks):
                source = iter(chunks)
                with self.assertRaises(ValueError):
                    list(frames(lambda _: next(source), 6))


class BridgeTests(unittest.TestCase):
    def test_streams_both_directions_and_drains_after_stdin_eof(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as listener:
            path = str(Path(directory) / "bridge.sock")
            listener.settimeout(5)
            listener.bind(path)
            listener.listen()
            with subprocess.Popen(
                [
                    sys.executable, str(Path(__file__).with_name("sync_bridge.py")),
                    "--socket", path, "--account", "163-example",
                ],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ) as process:
                try:
                    connection, _ = listener.accept()
                    with connection, connection.makefile("rb") as requests:
                        connection.settimeout(5)
                        self.assertEqual(json.loads(requests.readline()), {"account": "163-example"})
                        process.stdin.write(b'{"id":1}\n')
                        process.stdin.flush()
                        self.assertEqual(requests.readline(), b'{"id":1}\n')
                        connection.sendall(b'{"id":1}\n')
                        self.assertTrue(select.select([process.stdout], [], [], 5)[0])
                        self.assertEqual(process.stdout.readline(), b'{"id":1}\n')
                        process.stdin.close()
                        self.assertEqual(requests.readline(), b"")
                        connection.sendall(b'{"id":2}\n')
                    self.assertEqual(process.wait(timeout=5), 0)
                    self.assertEqual(process.stdout.read(), b'{"id":2}\n')
                    self.assertEqual(process.stderr.read(), b"")
                finally:
                    if process.poll() is None:
                        process.kill()


if __name__ == "__main__":
    unittest.main()
