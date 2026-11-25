"""Keep per-account inbox snapshots behind a private read-only MCP socket."""

from __future__ import annotations

import argparse
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import socketserver
import stat
from subprocess import TimeoutExpired
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

PARAMETERS = {
    "mailbox_list": ({"counts": bool}, set()),
    "envelope_list": ({
        "has-attachment": bool, "mailbox": str, "page": int, "page-size": int,
        "page-token": str, "recipient": str,
    }, set()),
    "message_read": ({"mailbox": str, "message-id": str}, {"message-id"}),
    "attachment_download": (
        {"mailbox": str, "message-id": str, "attachment-id": str},
        {"message-id", "attachment-id"},
    ),
    "reader_snapshot": ({}, set()),
    "reader_refresh": ({}, set()),
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
    def __init__(self, commands, interval=60, executor=None):
        self.interval = interval
        self.executor = executor if executor is not None else call_mcp
        start = time.monotonic()
        spacing = min(5, interval / len(commands))
        self.accounts = {alias: Account(command, start + index * spacing)
                         for index, (alias, command) in enumerate(commands.items())}
        self.condition = Condition()
        self.foreground = BoundedSemaphore(2)
        self.inflight = {}
        self.bodies = OrderedDict()
        self.cache_size = 0
        self.stopping = False
        self.worker = Thread(target=self.run_refreshes, name="inbox-refresh")

    def __enter__(self):
        self.worker.start()
        return self

    def __exit__(self, *_):
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        self.worker.join()

    def run_refreshes(self):
        while True:
            with self.condition:
                if self.stopping:
                    return
                state = min(self.accounts.values(), key=lambda account: account.due)
                now = time.monotonic()
                self.expire_bodies(now)
                if state.due > now:
                    wake_at = min((entry[0] for entry in self.bodies.values()), default=state.due)
                    self.condition.wait(min(state.due, wake_at) - now)
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
                "refreshing": state.running or state.queued or state.due <= now,
                "failed": state.failed,
            }

    def discard(self, key):
        self.cache_size -= self.bodies.pop(key)[2]

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
    parser.add_argument("--interval", default=60, type=int)
    args = parser.parse_args()
    if args.interval < RETRY_DELAY:
        parser.error(f"--interval must be at least {RETRY_DELAY} seconds")
    try:
        commands = load_commands(args.accounts)
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
            backend = Backend(commands, args.interval)
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
