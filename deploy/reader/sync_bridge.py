"""Relay newline-delimited MCP between stdio and a private Unix socket."""

import argparse
import json
import os
import re
import socket
import sys
from threading import Event, Thread


MAX_REQUEST_FRAME = 1024 * 1024
MAX_RESPONSE_FRAME = 20 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
ACCOUNT_ALIAS = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")


def frames(read, limit):
    """Yield complete frames, including their newline, within the byte limit."""
    pending = bytearray()
    while chunk := read(CHUNK_SIZE):
        start = 0
        while start < len(chunk):
            newline = chunk.find(b"\n", start)
            end = len(chunk) if newline < 0 else newline + 1
            if len(pending) + end - start > limit:
                raise ValueError("MCP frame exceeds size limit")
            pending.extend(chunk[start:end])
            if newline >= 0:
                yield pending
                pending = bytearray()
            start = end
    if pending:
        raise ValueError("Incomplete MCP frame")


def relay(connection, account, input_fd, output):
    connection.sendall(json.dumps({"account": account}).encode() + b"\n")
    send_failed = Event()

    def send_requests():
        try:
            for frame in frames(lambda size: os.read(input_fd, size), MAX_REQUEST_FRAME):
                connection.sendall(frame)
            connection.shutdown(socket.SHUT_WR)
        except (OSError, ValueError):
            send_failed.set()
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # The backend can finish first. A blocked read on the raw input descriptor
    # can then be left to process exit without holding Python's stdin lock.
    Thread(target=send_requests, daemon=True).start()
    for frame in frames(connection.recv, MAX_RESPONSE_FRAME):
        output.write(frame)
        output.flush()
    if send_failed.is_set():
        raise ValueError("MCP request forwarding failed")


class ArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, "Invalid synchronization bridge arguments.\n")


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, metavar="PATH")
    parser.add_argument("--account", required=True, metavar="ALIAS")
    args = parser.parse_args()
    if not ACCOUNT_ALIAS.fullmatch(args.account):
        parser.error("invalid account alias")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(args.socket)
            relay(connection, args.account, sys.stdin.fileno(), sys.stdout.buffer)
    except (OSError, ValueError):
        print("Synchronization bridge failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
