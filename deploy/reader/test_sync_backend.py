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
    def test_date_responses_expose_only_valid_classifications_without_modifying_cache(self):
        calls = []
        rows = [{"id": "7", "mail_classification": "spam"}, {"id": "8"}, {"id": "9"}]

        def lookup(alias, mailbox, envelopes):
            calls.append((alias, mailbox, [row["id"] for row in envelopes]))
            if mailbox == "INBOX":
                return {"7": "urgent", "8": ["important"]}
            return {"7": "important", "8": "invented"}

        backend = sync.Backend({"example": ["primary"]}, classification_lookup=lookup)
        backend.accounts["example"].envelopes = {"envelopes": rows}
        due = time.monotonic() + 60
        for mailbox in ("inbox", "INBOX", "test-folder"):
            backend.date_folders["example", mailbox] = sync.DateFolder(envelopes=rows, due=due)
        inbox = backend.date_snapshot("example")["envelopes"]
        alternate_inbox = backend.list_by_date("example", {"mailbox": "INBOX"})
        folder = backend.list_by_date("example", {"mailbox": "test-folder"})
        self.assertEqual(calls, [("example", "inbox", ["7", "8", "9"]),
                                 ("example", "INBOX", ["7", "8", "9"])])
        self.assertTrue(inbox["classifications_available"])
        self.assertTrue(alternate_inbox["classifications_available"])
        self.assertNotIn("classifications_available", folder)
        self.assertEqual(inbox["envelopes"], [{"id": "7", "mail_classification": "important"},
                                             {"id": "8"}, {"id": "9"}])
        self.assertEqual(alternate_inbox["envelopes"][0]["mail_classification"], "urgent")
        self.assertEqual(alternate_inbox["envelopes"][1:], [{"id": "8"}, {"id": "9"}])
        self.assertEqual(folder["envelopes"], [{"id": "7"}, {"id": "8"}, {"id": "9"}])
        self.assertEqual(rows[0]["mail_classification"], "spam")
        self.assertEqual(backend.snapshot("example")["envelopes"], {"envelopes": rows})

    def test_optional_classification_distinguishes_unavailable_from_no_matches(self):
        rows = [{"id": "1"}]
        backend = sync.Backend({"example": ["primary"]})
        backend.date_folders["example", "inbox"] = sync.DateFolder(
            envelopes=rows, due=time.monotonic() + 60
        )
        self.assertNotIn("classifications_available", backend.list_by_date("example", {}))
        backend.classification_lookup = lambda *_: {}
        self.assertTrue(backend.list_by_date("example", {})["classifications_available"])
        backend.classification_lookup = lambda *_: None
        self.assertFalse(backend.list_by_date("example", {})["classifications_available"])
        with patch.object(backend, "classification_lookup", side_effect=OSError):
            result = backend.list_by_date("example", {})
        self.assertFalse(result["classifications_available"])
        self.assertEqual(result["envelopes"], rows)

    def test_date_listing_and_snapshot_sort_across_backend_pages(self):
        rows = [
            {"id": "6", "date": "2026-11-25T11:00:00+08:00"},
            {"id": "5", "date": None},
            {"id": "4", "date": "2026-11-25T10:00:00-05:00"},
            {"id": "3", "date": "2026-11-25T12:30:00+08:00"},
            {"id": "2", "date": "2026-11-24T22:00:00-05:00"},
            {"id": "1", "date": "2026-11-25T23:00:00"},
        ]

        def execute(_command, calls):
            return [listed("mailboxes", [{"id": "inbox", "name": "Inbox"}]),
                    listed("envelopes", rows[:2], nextPage="2")]

        def execute_date(_command, calls):
            (name, args), = calls
            self.assertEqual(name, "envelope_list")
            self.assertEqual(args["mailbox"], "inbox")
            self.assertEqual(args["page-size"], 2)
            page = int(args.get("page-token", args.get("page", 1)))
            return [listed("envelopes", rows[(page - 1) * 2:page * 2], total=6,
                           nextPage=str(page + 1) if page < 3 else None)]

        def ids(page):
            return [row["id"] for row in page["envelopes"]]

        with patch.object(sync, "DATE_BATCH_SIZE", 2), patch.object(sync, "PAGE_SIZE", 2):
            with sync.Backend({"example": ["synthetic"]}, executor=execute,
                              date_executor=execute_date) as backend:
                first = sync.structured(backend.call("example", "reader_date_list", {}))
                self.assertEqual(ids(first), ["4", "3"])
                deadline = time.monotonic() + 2
                legacy = backend.snapshot("example")
                while legacy["last_success"] is None and time.monotonic() < deadline:
                    time.sleep(0.005)
                    legacy = backend.snapshot("example")
                self.assertIsNotNone(legacy["last_success"])
                self.assertEqual(ids(legacy["envelopes"]), ["6", "5"])
                snapshot = sync.structured(backend.call("example", "reader_date_snapshot", {}))
                self.assertEqual(first, snapshot["envelopes"])
                second = backend.list_by_date("example", {"page-token": first["nextPage"]})
                self.assertEqual(ids(second), ["6", "2"])
                third = backend.list_by_date("example", {"page": 3})
                self.assertEqual(ids(third), ["5", "1"])
                self.assertIsNone(third["nextPage"])

    def test_date_listing_retains_persisted_complete_cache_when_source_changes(self):
        def execute_date(_command, _calls):
            return next(responses)

        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            commands = {"example": ["synthetic"]}
            backend = sync.Backend(commands, date_executor=execute_date, date_directory=directory)
            key = ("example", "inbox")
            folder = backend.date_folders[key]
            initial = [{"id": "100", "date": "2026-11-25T01:00:00Z"}]
            responses = iter([[listed("envelopes", initial, total=1)]])
            backend.load_date_folder(key, folder)
            self.assertEqual(folder.envelopes, initial)
            path = backend.date_path(key)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            restored = sync.Backend(commands, date_executor=execute_date, date_directory=directory)
            restored_folder = restored.date_folders[key]
            self.assertEqual(restored_folder.last_success, folder.last_success)
            self.assertEqual(restored_folder.envelopes, initial)
            changed = sync.Backend({"example": ["different"]}, date_directory=directory)
            self.assertIsNone(changed.date_folders[key].envelopes)
            responses = iter([
                [listed("envelopes", [{"id": "101"}], total=2, nextPage="2")],
                [listed("envelopes", [{"id": "100"}], total=3)],
            ])
            restored.load_date_folder(key, restored_folder)
            self.assertTrue(restored_folder.failed)
            self.assertEqual(restored_folder.envelopes, initial)
            self.assertEqual(restored_folder.last_success, folder.last_success)

    def test_date_adapter_is_used_only_for_date_scan(self):
        aliases = {"example": ["primary"]}

        def execute(command, calls):
            self.assertEqual(command, ["primary"])
            self.assertEqual(calls, [("envelope_list", {"mailbox": "test-folder"})])
            return [listed("envelopes", [{"id": "primary-row"}])]

        def execute_date(command, calls):
            self.assertEqual(command, ["date-adapter"])
            self.assertEqual(calls, [("envelope_list", {"mailbox": "test-folder", "page": 1,
                                                       "page-size": sync.DATE_OVERRIDE_BATCH_SIZE})])
            return [listed("envelopes", [{"id": "adapter-row"}], total=1)]

        backend = sync.Backend(aliases, executor=execute, date_executor=execute_date,
                               date_commands={"example": ["date-adapter"]})
        self.assertEqual(backend.collect_date_folder("example", "test-folder")[0],
                         [{"id": "adapter-row"}])
        legacy = backend.call("example", "envelope_list", {"mailbox": "test-folder"})
        self.assertEqual(sync.structured(legacy)["envelopes"], [{"id": "primary-row"}])

    def test_manual_date_refresh_queues_during_cooldown(self):
        backend = sync.Backend({"example": ["synthetic"]})
        folder = backend.date_folders["example", "inbox"]
        folder.envelopes = []
        folder.manual_after = time.monotonic() + 5
        folder.due = folder.manual_after + 50
        first = backend.date_snapshot("example", schedule=True)
        self.assertTrue(first["refreshing"])
        self.assertEqual(folder.due, folder.manual_after)
        second = backend.date_snapshot("example", schedule=True)
        self.assertTrue(second["refreshing"])
        self.assertEqual(folder.due, folder.manual_after)

    def test_scheduled_snapshot_and_failed_refresh_preserves_complete_state(self):
        calls = []
        partial = False

        def execute(command, tools):
            calls.append((command, tools))
            return [
                listed("mailboxes", [{"id": command[0], "name": command[0]}]),
                listed("envelopes", [], errors=["item unavailable"] if partial else []),
            ]

        backend = sync.Backend({"first": ["first"], "second": ["second"]}, executor=execute,
                               date_executor=lambda *_: [listed("envelopes", [])])
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

        backend = sync.Backend({"example": ["synthetic"]}, executor=execute,
                               date_executor=lambda *_: [listed("envelopes", [])])
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
