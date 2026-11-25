"""Serve inbox snapshots and date-ordered folders behind a private read-only MCP socket."""

from __future__ import annotations

import argparse
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import socketserver
import stat
from subprocess import TimeoutExpired
from tempfile import NamedTemporaryFile
from threading import BoundedSemaphore, Condition, Event, Thread
import time

from reader import BackendError, PAGE_SIZE, TIMEOUT, call_mcp, structured, unique_aliases, valid_command

BODY_TTL = 300
BODIES_PER_ACCOUNT = 10
MAX_BODY = 512 * 1024
MAX_CACHE = 8 * 1024 * 1024
MAX_SNAPSHOT = 2 * 1024 * 1024
MAX_ACCOUNTS = 16
MAX_LINE = 32 * 1024
MAX_CONNECTIONS = 12
CONNECTION_TIMEOUT = 5
MAX_REQUESTS = 32
RETRY_DELAY = 10
MIN_REFRESH_GAP = 5
DATE_BATCH_SIZE = 100
DATE_OVERRIDE_BATCH_SIZE = 1000
DATE_LOAD_TIMEOUT = 60
DATE_LIST_WAIT = 4
DATE_FOLDERS = 24
MAX_DATE_CACHE = 24 * 1024 * 1024

LIST_PARAMETERS = {"mailbox": str, "page": int, "page-size": int, "page-token": str}

PARAMETERS = {
    "mailbox_list": ({"counts": bool}, set()),
    "envelope_list": ({
        **LIST_PARAMETERS, "has-attachment": bool, "recipient": str,
    }, set()),
    "message_read": ({"mailbox": str, "message-id": str}, {"message-id"}),
    "attachment_download": (
        {"mailbox": str, "message-id": str, "attachment-id": str},
        {"message-id", "attachment-id"},
    ),
    "reader_snapshot": ({}, set()),
    "reader_refresh": ({}, set()),
    "reader_date_list": (LIST_PARAMETERS, set()),
    "reader_date_snapshot": ({}, set()),
    "reader_date_refresh": ({}, set()),
}


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def reject_constant(_value):
    raise ValueError("invalid JSON constant")


def decode(data):
    return json.loads(data, object_pairs_hook=unique_aliases, parse_constant=reject_constant)


def tool_result(data):
    return {"content": [], "structuredContent": data}


def checked_list(result, key):
    data = structured(result, key)
    if result.get("isError") or data.get("errors"):
        raise BackendError("incomplete snapshot")
    if key == "mailboxes" and (data.get("nextPage") or data.get("more")):
        raise BackendError("incomplete folder list")
    for item in data[key]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise BackendError("invalid snapshot item")
        if key == "mailboxes" and not isinstance(item.get("name"), str):
            raise BackendError("invalid folder")
    return data


def envelope_date(item):
    try:
        instant = datetime.fromisoformat(item["date"])
        if instant.utcoffset() is not None:
            return instant.timestamp()
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        pass
    return -math.inf


@dataclass
class DateFolder:
    envelopes: list | None = None
    size: int = 0
    last_success: int | None = None
    due: float = 0
    manual_after: float = 0
    refreshing: bool = False
    failed: bool = False


@dataclass
class Account:
    command: list
    due: float
    folders: list | None = None
    envelopes: dict | None = None
    last_success: int | None = None
    running: bool = False
    queued: bool = True
    failed: bool = False
    manual_after: float = 0


class Backend:
    def __init__(self, commands, interval=60, executor=None, date_executor=None, date_directory=None,
                 date_commands=None):
        self.interval = interval
        self.executor = executor if executor is not None else call_mcp
        self.date_executor = date_executor if date_executor is not None else self.execute_date
        self.date_directory = date_directory
        self.date_commands = date_commands if date_commands is not None else {}
        start = time.monotonic()
        spacing = min(5, interval / len(commands))
        self.accounts = {alias: Account(command, start + index * spacing)
                         for index, (alias, command) in enumerate(commands.items())}
        self.condition = Condition()
        self.foreground = BoundedSemaphore(2)
        self.inflight = {}
        self.bodies = OrderedDict()
        self.cache_size = 0
        self.date_folders = OrderedDict(
            ((alias, "inbox"), DateFolder(due=account.due)) for alias, account in self.accounts.items()
        )
        self.date_size = 0
        self.date_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="date-refresh")
        self.stopping = False
        self.worker = Thread(target=self.run_refreshes, name="inbox-refresh")
        for key, folder in self.date_folders.items():
            self.restore_date_folder(key, folder)

    def __enter__(self):
        self.worker.start()
        return self

    def __exit__(self, *_):
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        self.worker.join()
        self.date_workers.shutdown(cancel_futures=True)

    def run_refreshes(self):
        while True:
            with self.condition:
                if self.stopping:
                    return
                state = min(self.accounts.values(), key=lambda account: account.due)
                now = time.monotonic()
                self.expire_bodies(now)
                for alias in self.accounts:
                    self.date_folder(alias, "inbox")
                if state.due > now:
                    body_due = min((entry[0] for entry in self.bodies.values()), default=state.due)
                    date_due = min((folder.due for key, folder in self.date_folders.items()
                                    if key[1] == "inbox" and not folder.refreshing), default=state.due)
                    self.condition.wait(max(0, min(state.due, body_due, date_due) - now))
                    continue
                state.running = True
                state.queued = False
            self.refresh(state)

    def refresh(self, state):
        updated = None
        try:
            folders, envelopes = self.executor(state.command, [
                ("mailbox_list", {}),
                ("envelope_list", {"mailbox": "inbox", "page": 1, "page-size": PAGE_SIZE}),
            ])
            folders = checked_list(folders, "mailboxes")["mailboxes"]
            envelopes = checked_list(envelopes, "envelopes")
            if len(encode([folders, envelopes])) <= MAX_SNAPSHOT:
                updated = folders, envelopes
        except (BackendError, OSError, TimeoutExpired, TypeError, AttributeError, ValueError, RecursionError):
            pass
        with self.condition:
            now = time.monotonic()
            state.running = False
            state.failed = updated is None
            if updated is not None:
                state.folders, state.envelopes = updated
                state.last_success = int(time.time())
            wait = RETRY_DELAY if state.failed else MIN_REFRESH_GAP
            state.manual_after = now + wait
            state.due = now + (min(self.interval, RETRY_DELAY) if state.failed else self.interval)

    def snapshot(self, alias, schedule=False):
        with self.condition:
            state = self.accounts[alias]
            now = time.monotonic()
            if schedule and not (state.running or state.queued):
                state.due = max(now, state.manual_after)
                state.queued = True
                self.condition.notify()
            return {
                "folders": state.folders if state.folders is not None else [],
                "envelopes": state.envelopes,
                "last_success": state.last_success,
                "refresh_interval_seconds": self.interval,
                "refreshing": state.running or state.queued or state.due <= now,
                "failed": state.failed,
            }

    def discard(self, key):
        self.cache_size -= self.bodies.pop(key)[2]

    def discard_date_folder(self, *, skip=None):
        for key, folder in self.date_folders.items():
            if key != skip and key[1] != "inbox" and not folder.refreshing:
                self.date_size -= self.date_folders.pop(key).size
                return True
        return False

    def date_folder(self, alias, mailbox, *, schedule=False, requested=False):
        # Callers hold the condition while accessing cache state.
        key = alias, mailbox
        folder = self.date_folders.get(key)
        if folder is None:
            if len(self.date_folders) >= DATE_FOLDERS and not self.discard_date_folder():
                raise BackendError("date listings are busy")
            folder = self.date_folders[key] = DateFolder()
        self.date_folders.move_to_end(key)
        now = time.monotonic()
        start = requested and folder.envelopes is None and not folder.failed
        due = folder.manual_after if schedule or start else folder.due
        if not self.stopping and not folder.refreshing and now >= due:
            folder.refreshing = True
            self.date_workers.submit(self.load_date_folder, key, folder)
        return folder

    def load_date_folder(self, key, folder):
        envelopes = None
        size = 0
        try:
            envelopes, size = self.collect_date_folder(*key)
        except (BackendError, OSError, TimeoutExpired, TypeError, AttributeError, ValueError, RecursionError):
            envelopes = None
        with self.condition:
            now = time.monotonic()
            if envelopes is not None:
                while self.date_size - folder.size + size > MAX_DATE_CACHE:
                    if not self.discard_date_folder(skip=key):
                        envelopes = None
                        break
            folder.refreshing = False
            folder.failed = envelopes is None
            if envelopes is not None:
                self.date_size += size - folder.size
                folder.envelopes = envelopes
                folder.size = size
                last_success = folder.last_success = int(time.time())
            folder.manual_after = now + (RETRY_DELAY if folder.failed else MIN_REFRESH_GAP)
            folder.due = now + (min(self.interval, RETRY_DELAY) if folder.failed else self.interval)
            self.condition.notify_all()
        if envelopes is not None and key[1] == "inbox":
            self.persist_date_folder(key, envelopes, last_success)

    def date_path(self, key):
        if self.date_directory is not None:
            alias, _mailbox = key
            digest = hashlib.sha256(encode(
                [key, self.accounts[alias].command, self.date_commands.get(alias)]
            )).hexdigest()
            return self.date_directory / f"date-{digest}.json"
        return None

    def restore_date_folder(self, key, folder):
        path = self.date_path(key)
        if path is None:
            return
        try:
            with path.open("rb") as persisted:
                raw = persisted.read(MAX_DATE_CACHE + 1)
            if len(raw) > MAX_DATE_CACHE:
                return
            data = checked_list(tool_result(decode(raw)), "envelopes")
            envelopes = data["envelopes"]
            last_success = data.get("last_success")
            if not (type(last_success) is int and 0 < last_success <= time.time()
                    and len({row["id"] for row in envelopes}) == len(envelopes)
                    and self.date_size + len(raw) <= MAX_DATE_CACHE):
                return
            folder.size = len(raw)
            self.date_size += folder.size
            folder.envelopes = sorted(envelopes, key=envelope_date, reverse=True)
            folder.last_success = last_success
        except (BackendError, OSError, TypeError, AttributeError, ValueError, RecursionError):
            return

    def persist_date_folder(self, key, envelopes, last_success):
        path = self.date_path(key)
        if path is None:
            return
        pending = None
        try:
            with NamedTemporaryFile(dir=path.parent, delete=False) as output:
                pending = Path(output.name)
                output.write(encode({"envelopes": envelopes, "last_success": last_success}))
            pending.replace(path)
        except OSError:
            if pending is not None:
                try:
                    pending.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def execute_date(command, calls):
        return call_mcp(command, calls, timeout=DATE_LOAD_TIMEOUT)

    def collect_date_folder(self, alias, mailbox):
        command = self.date_commands.get(alias)
        batch_size = DATE_OVERRIDE_BATCH_SIZE if command is not None else DATE_BATCH_SIZE
        command = command if command is not None else self.accounts[alias].command
        envelopes = {}
        size = 0
        cursor = None
        total = None
        seen_cursors = set()
        while True:
            with self.condition:
                if self.stopping:
                    raise BackendError("mail backend is stopping")
            args = {"mailbox": mailbox, "page-size": batch_size}
            args.update({"page-token": cursor} if cursor else {"page": 1})
            result, = self.date_executor(command, [("envelope_list", args)])
            data = checked_list(result, "envelopes")
            rows = data["envelopes"]
            count = data.get("total")
            if type(count) is int:
                if total is not None and total != count:
                    raise BackendError("folder changed during date listing")
                total = count
            size += len(encode(rows))
            if size > MAX_DATE_CACHE:
                raise BackendError("date listing exceeds cache limit")
            for row in rows:
                if row["id"] in envelopes:
                    raise BackendError("overlapping date listing pages")
                envelopes[row["id"]] = row
            cursor = data.get("nextPage")
            if not cursor:
                if data.get("more") or (total is not None and len(envelopes) != total):
                    raise BackendError("incomplete date listing")
                return sorted(envelopes.values(), key=envelope_date, reverse=True), size
            if not rows or not isinstance(cursor, str) or cursor in seen_cursors:
                raise BackendError("invalid date listing cursor")
            seen_cursors.add(cursor)

    @staticmethod
    def date_page(folder, page, size):
        rows = folder.envelopes
        if rows is None:
            return {"envelopes": [], "errors": [], "refreshing": folder.refreshing,
                    "failed": folder.failed}
        stop = page * size
        return {"envelopes": rows[stop - size:stop], "errors": [],
                "nextPage": str(page + 1) if stop < len(rows) else None, "total": len(rows)}

    def date_snapshot(self, alias, schedule=False):
        with self.condition:
            snapshot = self.snapshot(alias, schedule=schedule)
            folder = self.date_folder(alias, "inbox", schedule=schedule, requested=True)
            snapshot["envelopes"] = (
                self.date_page(folder, 1, PAGE_SIZE) if folder.envelopes is not None else None
            )
            snapshot["last_success"] = folder.last_success
            snapshot["refreshing"] = folder.refreshing
            snapshot["failed"] = folder.failed
            return snapshot

    def list_by_date(self, alias, args):
        page = args.get("page", 1)
        if "page-token" in args:
            if "page" in args:
                raise BackendError("date listings take one page argument")
            page = int(args["page-token"])
            if not 1 <= page <= 1_000_000:
                raise BackendError("invalid date page")
        with self.condition:
            folder = self.date_folder(alias, args.get("mailbox", "inbox"), requested=True)
            if folder.envelopes is None:
                self.condition.wait_for(
                    lambda: not folder.refreshing or folder.envelopes is not None,
                    timeout=DATE_LIST_WAIT,
                )
            return self.date_page(folder, page, args.get("page-size", PAGE_SIZE))

    def expire_bodies(self, now):
        for item, (expiry, _result, _size) in list(self.bodies.items()):
            if expiry <= now:
                self.discard(item)

    def store(self, key, result, size):
        now = time.monotonic()
        self.expire_bodies(now)
        for_account = [item for item in self.bodies if item[0] == key[0]]
        if len(for_account) >= BODIES_PER_ACCOUNT:
            self.discard(for_account[0])
        while self.cache_size + size > MAX_CACHE:
            self.discard(next(iter(self.bodies)))
        self.bodies[key] = now + BODY_TTL, result, size
        self.cache_size += size
        self.condition.notify()

    def call(self, alias, name, args):
        if name in {"reader_date_snapshot", "reader_date_refresh"}:
            return tool_result(self.date_snapshot(alias, schedule=name == "reader_date_refresh"))
        if name == "reader_date_list":
            return tool_result(self.list_by_date(alias, args))
        if name in {"reader_snapshot", "reader_refresh"}:
            return tool_result(self.snapshot(alias, schedule=name == "reader_refresh"))
        state = self.accounts[alias]
        body = None
        if name == "message_read":
            body = alias, args.get("mailbox", "inbox"), args["message-id"]
        key = body if body is not None else (alias, name, tuple(sorted(args.items())))
        with self.condition:
            if body in self.bodies:
                expiry, result, _size = self.bodies[body]
                if expiry > time.monotonic():
                    self.bodies.move_to_end(body)
                    return result
                self.discard(body)
            future = self.inflight.get(key)
            lead = future is None
            if lead:
                if not self.foreground.acquire(blocking=False):
                    raise BackendError("mail backend is busy")
                future = self.inflight[key] = Future()
        if not lead:
            return future.result(timeout=TIMEOUT + 1)
        try:
            result, = self.executor(state.command, [(name, args)])
            if body is not None and not result.get("isError") and isinstance(
                    result.get("structuredContent"), dict):
                size = len(encode(result))
                if size <= MAX_BODY:
                    with self.condition:
                        self.store(body, result, size)
            future.set_result(result)
            return result
        except (BackendError, OSError, TimeoutExpired, TypeError, AttributeError, ValueError, RecursionError) as error:
            future.set_exception(error)
            raise
        finally:
            with self.condition:
                del self.inflight[key]
                self.foreground.release()


def tool_definitions():
    schema_types = {int: "integer", str: "string", bool: "boolean"}
    return [
        {"name": name, "annotations": {"readOnlyHint": True}, "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {argument: {"type": schema_types[kind]}
                           for argument, kind in properties.items()},
            "required": sorted(required),
        }}
        for name, (properties, required) in PARAMETERS.items()
    ]


def valid_arguments(name, args):
    if not isinstance(args, dict):
        return False
    properties, required = PARAMETERS[name]
    if not required <= args.keys() <= properties.keys():
        return False
    for key, value in args.items():
        if type(value) is not properties[key]:
            return False
        if isinstance(value, str) and (not value or len(value) > 8192 or "\0" in value):
            return False
        if type(value) is int and not 1 <= value <= 1_000_000:
            return False
    return True


def rpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def dispatch(backend, alias, request):
    if not isinstance(request, dict):
        return rpc_error(None, -32600, "Invalid Request")
    request_id = request.get("id")
    if request_id is not None and (type(request_id) not in {int, float, str}
                                   or (type(request_id) is float and not math.isfinite(request_id))):
        return rpc_error(None, -32600, "Invalid Request")
    method = request.get("method")
    if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return rpc_error(request_id, -32600, "Invalid Request")
    if "id" not in request:
        return None
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "himalaya", "version": "1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_definitions()}
    elif method == "tools/call":
        params = request.get("params")
        if not isinstance(params, dict) or not params.keys() <= {"name", "arguments"}:
            return rpc_error(request_id, -32602, "Invalid params")
        name = params.get("name")
        args = params.get("arguments", {})
        if not isinstance(name, str) or name not in PARAMETERS or not valid_arguments(name, args):
            return rpc_error(request_id, -32602, "Invalid or disabled tool")
        try:
            result = backend.call(alias, name, args)
        except (BackendError, OSError, TimeoutExpired, TypeError, AttributeError, ValueError, RecursionError):
            return rpc_error(request_id, -32000, "Mail is temporarily unavailable")
    else:
        return rpc_error(request_id, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class Handler(socketserver.BaseRequestHandler):
    def read_line(self):
        deadline = time.monotonic() + CONNECTION_TIMEOUT
        while True:
            newline = self.buffer.find(b"\n")
            if 0 <= newline < MAX_LINE:
                line = self.buffer[:newline]
                del self.buffer[:newline + 1]
                return bytes(line)
            remaining = deadline - time.monotonic()
            if len(self.buffer) >= MAX_LINE or remaining <= 0:
                raise ValueError("request limit exceeded")
            self.request.settimeout(remaining)
            chunk = self.request.recv(min(4096, MAX_LINE - len(self.buffer)))
            if not chunk:
                return None
            self.buffer.extend(chunk)

    def handle(self):
        self.buffer = bytearray()
        try:
            first = self.read_line()
            if first is None:
                return
            hello = decode(first)
            if not isinstance(hello, dict) or hello.keys() != {"account"}:
                return
            alias = hello["account"]
            if not isinstance(alias, str) or alias not in self.server.backend.accounts:
                return
            for _ in range(MAX_REQUESTS):
                line = self.read_line()
                if line is None:
                    return
                try:
                    request = decode(line)
                except (ValueError, RecursionError):
                    response = rpc_error(None, -32700, "Parse error")
                else:
                    response = dispatch(self.server.backend, alias, request)
                if response is not None:
                    self.request.settimeout(CONNECTION_TIMEOUT)
                    self.request.sendall(encode(response) + b"\n")
        except (OSError, ValueError, RecursionError):
            return


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    request_queue_size = MAX_CONNECTIONS
    timeout = 0.25

    def __init__(self, path, backend):
        self.backend = backend
        self.connections = BoundedSemaphore(MAX_CONNECTIONS)
        previous_umask = os.umask(0o077)
        try:
            super().__init__(str(path), Handler)
        finally:
            os.umask(previous_umask)

    def process_request(self, request, address):
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.connections.release()


def load_commands(path):
    commands = decode(path.read_text())
    if not isinstance(commands, dict) or not 1 <= len(commands) <= MAX_ACCOUNTS or not all(
        alias and alias.isprintable() and alias.strip() and valid_command(command)
        for alias, command in commands.items()
    ):
        raise ValueError("expected account aliases mapped to non-empty argv arrays")
    return commands


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--accounts", required=True, type=Path)
    parser.add_argument("--date-accounts", type=Path,
                        help="optional read-only listing commands for configured account aliases")
    parser.add_argument("--interval", default=60, type=int)
    args = parser.parse_args()
    if args.interval < RETRY_DELAY:
        parser.error(f"--interval must be at least {RETRY_DELAY} seconds")
    try:
        commands = load_commands(args.accounts)
        date_commands = load_commands(args.date_accounts) if args.date_accounts is not None else {}
        if not date_commands.keys() <= commands.keys():
            raise ValueError("date listing commands must use configured account aliases")
        parent = args.socket.parent.stat()
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise ValueError("the socket directory must be private and owned by the broker")
        # An exclusive lock lets the next invocation recover a socket left by a crash.
        with open(args.socket.with_name(args.socket.name + ".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                previous = args.socket.lstat()
            except FileNotFoundError:
                pass
            else:
                if previous.st_uid != os.geteuid() or not stat.S_ISSOCK(previous.st_mode):
                    raise ValueError("the socket path exists and is not an owned socket")
                args.socket.unlink()
            backend = Backend(commands, args.interval, date_directory=args.socket.parent,
                              date_commands=date_commands)
            with Server(args.socket, backend) as server:
                try:
                    stopping = Event()

                    def stop(_signum, _frame):
                        stopping.set()

                    signal.signal(signal.SIGTERM, stop)
                    signal.signal(signal.SIGINT, stop)
                    with backend:
                        while not stopping.is_set():
                            server.handle_request()
                finally:
                    args.socket.unlink(missing_ok=True)
    except (ValueError, OSError, RecursionError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")


if __name__ == "__main__":
    main()
