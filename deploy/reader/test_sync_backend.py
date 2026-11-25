"""Offline coverage for the reader's private background MCP broker."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

import sync_backend as sync


def listed(key, items, **extra):
    return sync.tool_result({key: items, **extra})


class SyncTest(unittest.TestCase):
    def test_scheduled_snapshot_and_failed_refresh_preserves_complete_state(self):
        calls = []
        partial = False

        def execute(command, tools):
            calls.append((command, tools))
            return [
                listed("mailboxes", [{"id": command[0], "name": command[0]}]),
                listed("envelopes", [], errors=["item unavailable"] if partial else []),
            ]

        backend = sync.Backend({"first": ["first"], "second": ["second"]}, executor=execute)
        self.assertLess(backend.accounts["first"].due, backend.accounts["second"].due)
        with backend:
            deadline = time.monotonic() + 2
            while backend.snapshot("first")["last_success"] is None and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(backend.snapshot("first")["folders"], [{"id": "first", "name": "first"}])
            self.assertIsNone(backend.snapshot("second")["envelopes"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1][1][1]["page-size"], 25)
        original = backend.snapshot("first")
        partial = True
        state = backend.accounts["first"]
        backend.refresh(state)
        failed = backend.snapshot("first")
        self.assertTrue(failed["failed"])
        for key in ("folders", "envelopes", "last_success"):
            self.assertEqual(original[key], failed[key])
        self.assertTrue(backend.snapshot("first", schedule=True)["refreshing"])
        self.assertEqual(state.due, state.manual_after)
        self.assertTrue(backend.snapshot("first", schedule=True)["refreshing"])
        self.assertEqual(state.due, state.manual_after)

    def test_manual_requests_coalesce_and_snapshot_never_creates_work(self):
        entered = Event()
        release = Event()
        queries = []

        def execute(command, tools):
            queries.append(tools)
            entered.set()
            if not release.wait(2):
                raise sync.BackendError("test deadline")
            raise sync.BackendError("test offline")

        backend = sync.Backend({"example": ["synthetic"]}, executor=execute)
        with backend:
            try:
                self.assertTrue(entered.wait(1))
                for _ in range(15):
                    self.assertTrue(backend.snapshot("example")["refreshing"])
                    self.assertTrue(backend.snapshot("example", schedule=True)["refreshing"])
                self.assertEqual(len(queries), 1)
            finally:
                release.set()
        self.assertEqual(len(queries), 1)
        self.assertFalse(backend.snapshot("example")["refreshing"])
        self.assertTrue(backend.snapshot("example")["failed"])
        self.assertIsNone(backend.snapshot("example")["last_success"])

    def test_invalid_snapshots_and_executor_os_errors_are_retryable(self):
        def execute(_command, _tools):
            return next(responses)()

        responses = iter([
            lambda: [listed("mailboxes", [{"id": "inbox", "name": "Inbox"}], nextPage="token"),
                     listed("envelopes", [])],
            lambda: [listed("mailboxes", [{"id": "inbox", "name": "Inbox"}]),
                     listed("envelopes", [{"subject": "missing id"}])],
            lambda: [listed("mailboxes", [{"id": "inbox"}]), listed("envelopes", [])],
        ])
        backend = sync.Backend({"example": ["synthetic"]}, executor=execute)
        state = backend.accounts["example"]
        for _ in range(3):
            backend.refresh(state)
            self.assertTrue(backend.snapshot("example")["failed"])
            self.assertIsNone(backend.snapshot("example")["envelopes"])
        for error in (FileNotFoundError("not present"), TimeoutError("deadline")):
            with patch.object(backend, "executor", side_effect=error):
                backend.refresh(state)
                self.assertTrue(backend.snapshot("example")["failed"])
        with patch.object(backend, "executor", return_value=[
                listed("mailboxes", [{"id": "inbox", "name": "Inbox"}]),
                listed("envelopes", [{"id": "m"}], nextPage="valid-page-cursor")]):
            backend.refresh(state)
            self.assertFalse(backend.snapshot("example")["failed"])

    def test_body_cache_isolation_lru_ttl_and_attachment_passthrough(self):
        queries = []

        def execute(command, tools):
            queries.append((command, tools))
            return [sync.tool_result({"text": str(len(queries))})]

        backend = sync.Backend({"a": ["a"], "b": ["b"]}, executor=execute)
        first = backend.call("a", "message_read", {"message-id": "m"})
        self.assertEqual(first, backend.call("a", "message_read", {"mailbox": "inbox", "message-id": "m"}))
        self.assertNotEqual(first, backend.call("a", "message_read", {"mailbox": "junk", "message-id": "m"}))
        self.assertNotEqual(first, backend.call("b", "message_read", {"message-id": "m"}))
        for index in range(sync.BODIES_PER_ACCOUNT):
            backend.call("a", "message_read", {"message-id": str(index)})
        self.assertEqual(sum(item[0] == "a" for item in backend.bodies), sync.BODIES_PER_ACCOUNT)
        self.assertNotEqual(first, backend.call("a", "message_read", {"message-id": "m"}))
        clock = time.monotonic()
        with patch.object(sync.time, "monotonic", return_value=clock + sync.BODY_TTL + 1):
            backend.expire_bodies(time.monotonic())
            self.assertEqual(backend.cache_size, 0)
        with patch.object(sync, "MAX_BODY", 1):
            backend.call("a", "message_read", {"message-id": "m"})
            self.assertFalse(backend.bodies)
        args = {"message-id": "m", "attachment-id": "p"}
        self.assertNotEqual(backend.call("a", "attachment_download", args),
                            backend.call("a", "attachment_download", args))

    def test_parallel_duplicate_body_reads_share_one_execution(self):
        started = Event()
        release = Event()
        queries = []

        def execute(_command, tools):
            queries.append(tools)
            started.set()
            if not release.wait(2):
                raise sync.BackendError("test deadline")
            return [sync.tool_result({"text": "synthetic"})]

        backend = sync.Backend({"example": ["synthetic"]}, executor=execute)
        with ThreadPoolExecutor(2) as pool:
            try:
                first = pool.submit(backend.call, "example", "message_read", {"message-id": "m"})
                self.assertTrue(started.wait(1))
                second = pool.submit(backend.call, "example", "message_read", {"message-id": "m"})
            finally:
                release.set()
            self.assertEqual(first.result(timeout=2), second.result(timeout=2))
        self.assertEqual(len(queries), 1)

    def test_wire_preamble_and_strict_parameters(self):
        commands = {"example": ["synthetic"], "other": ["different"]}
        backend = sync.Backend(commands, executor=lambda command, _: [sync.tool_result({"who": command[0]})])
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "broker.sock"
            with sync.Server(path, backend) as server:
                worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
                worker.start()
                try:
                    self.assertEqual(path.stat().st_mode & 0o777, 0o700)
                    with socket.socket(socket.AF_UNIX) as client:
                        client.settimeout(2)
                        client.connect(str(path))
                        client.sendall(b'{"account":"unknown"}\n')
                        self.assertEqual(client.recv(1), b"")
                    with socket.socket(socket.AF_UNIX) as client:
                        client.settimeout(2)
                        client.connect(str(path))
                        with client.makefile("rb") as reader:
                            client.sendall(b'{"account":"example"}\n')

                            def rpc(method, params=None):
                                request = {"jsonrpc": "2.0", "id": 1, "method": method}
                                if params is not None:
                                    request["params"] = params
                                client.sendall(sync.encode(request) + b"\n")
                                return json.loads(reader.readline())

                            self.assertEqual(rpc("initialize")["result"]["serverInfo"]["name"], "himalaya")
                            client.sendall(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                            for params in (
                                {"name": "flag_add"},
                                {"name": "message_read", "arguments": {"message-id": "m", "seen": True}},
                                {"name": "envelope_list", "arguments": {"page": False}},
                                {"name": "reader_snapshot", "arguments": {"account": "other"}},
                                {"name": "mailbox_list", "account": "other"},
                            ):
                                self.assertEqual(rpc("tools/call", params)["error"]["code"], -32602)
                            self.assertEqual(rpc("tools/call", {"name": "mailbox_list"})["result"]
                                             ["structuredContent"], {"who": "synthetic"})
                            snapshot = rpc("tools/call", {"name": "reader_snapshot"})["result"]["structuredContent"]
                            self.assertIsNone(snapshot["envelopes"])
                finally:
                    server.shutdown()
                    worker.join()


if __name__ == "__main__":
    unittest.main()
