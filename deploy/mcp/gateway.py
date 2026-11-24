"""Apply the deployment tool policy to a native Himalaya MCP process."""

import argparse
import json
import math
import subprocess
import sys

ENVELOPE_ARGUMENTS = {
    "has-attachment", "mailbox", "page", "page-size", "page-token", "recipient",
}

READ_TOOLS = {
    "account_capability": set(),
    "mailbox_list": {"counts"},
    "envelope_list": ENVELOPE_ARGUMENTS,
    "envelope_search": ENVELOPE_ARGUMENTS | {"query", "sort"},
    "message_read": {"mailbox", "message-id"},
    "attachment_list": {"inline", "mailbox", "message-id"},
    "attachment_download": {"attachment-id", "mailbox", "message-id"},
}

FLAG_ARGUMENTS = {"flag", "mailbox", "message-ids"}
FLAG_TOOLS = {"flag_add": FLAG_ARGUMENTS, "flag_remove": FLAG_ARGUMENTS}
FLAGS = ("seen", "flagged")


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate object key")
        obj[key] = value
    return obj


def invalid_constant(_):
    raise ValueError("Invalid JSON constant")


def decode(line):
    return json.loads(
        line, object_pairs_hook=unique_object, parse_constant=invalid_constant,
    )


def encode(message):
    return json.dumps(message, allow_nan=False).encode("ascii") + b"\n"


def error(request_id, code, message):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


class Gateway:
    """One synchronous request at a time over a trusted child's stdio."""

    def __init__(self, argv, read_only=False):
        self.tools = READ_TOOLS if read_only else READ_TOOLS | FLAG_TOOLS
        self.child = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.child.stdin.close()
        self.child.stdout.close()
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait()

    def handle(self, request):
        if not isinstance(request, dict):
            return error(None, -32600, "Expected one JSON-RPC request object")
        request_id = request.get("id")
        if "id" in request and not (
            type(request_id) in (str, int)
            or (type(request_id) is float and math.isfinite(request_id))
        ):
            return error(None, -32600, "Invalid request id")

        method = request.get("method")
        if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return error(request_id, -32600, "Invalid JSON-RPC request")
        if "id" not in request:
            return None

        params = request.get("params", {})
        if method == "tools/call":
            refusal = self.check_call(params)
            if refusal is not None:
                return error(request_id, -32602, refusal)
            # Reconstruct the forwarded params using only the checked fields.
            params = {"name": params["name"], "arguments": params.get("arguments", {})}
        elif method not in ("initialize", "ping", "tools/list"):
            return error(request_id, -32601, "Method not found")

        forwarded = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            native_line = json.dumps(forwarded, ensure_ascii=False, allow_nan=False)
            self.child.stdin.write(native_line.encode("utf-8") + b"\n")
            self.child.stdin.flush()
        except (ValueError, OverflowError, RecursionError):
            return error(request_id, -32602, "Invalid request parameters")

        line = self.child.stdout.readline()
        if not line:
            raise RuntimeError("Native MCP process closed stdout")

        response = decode(line)
        if not isinstance(response, dict) or response.get("id") != 1:
            raise RuntimeError("Native MCP response id did not match")
        response["id"] = request_id
        if "result" in response:
            result = response["result"]
            if method == "tools/list":
                result["tools"] = self.filter_tools(result["tools"])
            if method == "initialize":
                result["instructions"] = (
                    "List the available tools before calling them. Use mailbox_list "
                    "to find mailboxes, envelope_list or envelope_search for message "
                    "identifiers, and message_read or attachment_list for parts. "
                    "attachment_download returns the part as an embedded resource."
                )
        return response

    def check_call(self, params):
        if not isinstance(params, dict):
            return "Invalid tool call parameters"
        name = params.get("name")
        if not isinstance(name, str) or name not in self.tools:
            return "Unknown or disabled tool"

        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict) or arguments.keys() - self.tools[name]:
            return "Invalid or disabled tool arguments"

        if name in FLAG_TOOLS:
            flags = arguments.get("flag")
            flags = flags if isinstance(flags, list) else [flags]
            if not flags or any(flag not in FLAGS for flag in flags):
                return "Only seen and flagged may be changed"
        return None

    def filter_tools(self, definitions):
        tools = []
        for tool in definitions:
            name = tool["name"]
            if name not in self.tools:
                continue
            schema = tool["inputSchema"]
            properties = schema["properties"]
            if set(schema.get("required", [])) - self.tools[name]:
                continue
            schema["properties"] = {
                key: value for key, value in properties.items() if key in self.tools[name]
            }
            schema["additionalProperties"] = False
            if name in FLAG_TOOLS:
                flag_schema = schema["properties"]["flag"]
                flag_schema["items"]["enum"] = [
                    value for value in FLAGS if value in flag_schema["items"]["enum"]
                ]
                flag_schema["minItems"] = 1
            tools.append(tool)
        return tools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- NATIVE_MCP [ARG ...]")
    args = parser.parse_args()
    if not args.command or args.command[0] != "--" or len(args.command) == 1:
        parser.error("pass the native MCP command after --")

    try:
        with Gateway(args.command[1:], args.read_only) as gateway:
            for line in sys.stdin.buffer:
                if not line.strip():
                    continue
                try:
                    request = decode(line.decode("utf-8"))
                except (UnicodeError, ValueError, RecursionError):
                    response = error(None, -32700, "Invalid JSON")
                else:
                    response = gateway.handle(request)
                if response is not None:
                    sys.stdout.buffer.write(encode(response))
                    sys.stdout.buffer.flush()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"MCP gateway: {exc}", file=sys.stderr)
        return 1
    return gateway.child.returncode


if __name__ == "__main__":
    sys.exit(main())
