"""Run with python3 -m unittest discover -s deploy/mcp."""

import json
from pathlib import Path
import subprocess
import sys
import unittest


# Stand-in for the native server. Echo every call's arguments so the tests can
# tell which requests reached the child and how they were forwarded.
NATIVE = r"""
import json
import sys

def tool(name, properties):
    return {
        "name": name,
        "inputSchema": {"properties": properties, "additionalProperties": False},
    }

tools = [
    tool("message_read", {"seen": {}, "message-id": {}}),
    tool("message_delete", {}),
    tool("message_send", {}),
    tool("flag_add", {
        "flag": {"type": "array", "items": {"enum": ["seen", "junk", "flagged"]}}
    }),
]
for line in sys.stdin:
    req = json.loads(line)
    result = {"params": req["params"]}
    if req["method"] == "tools/list":
        result = {"tools": tools}
    elif req["method"] == "initialize":
        result = {"instructions": "Use message_send"}
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}), flush=True)
"""


def request(request_id, method, params=None):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def call(request_id, name, arguments):
    return request(request_id, "tools/call", {"name": name, "arguments": arguments})


class GatewayTest(unittest.TestCase):
    def run_gateway(self, *requests, read_only=False):
        argv = [sys.executable, str(Path(__file__).with_name("gateway.py"))]
        if read_only:
            argv.append("--read-only")
        argv.extend(["--", sys.executable, "-c", NATIVE])
        lines = [req if isinstance(req, str) else json.dumps(req) for req in requests]
        result = subprocess.run(
            argv, text=True, input="\n".join(lines) + "\n",
            capture_output=True, timeout=10, check=True,
        )
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_discovery_matches_policy(self):
        initialize, listing = self.run_gateway(
            request("setup", "initialize"), request(2, "tools/list"),
        )
        self.assertEqual(initialize["id"], "setup")
        self.assertNotIn("message_send", initialize["result"]["instructions"])
        tools = {tool["name"]: tool["inputSchema"] for tool in listing["result"]["tools"]}
        self.assertEqual(set(tools), {"flag_add", "message_read"})
        self.assertEqual(set(tools["message_read"]["properties"]), {"message-id"})
        self.assertEqual(
            tools["flag_add"]["properties"]["flag"]["items"]["enum"], ["seen", "flagged"],
        )

    def test_blocks_unsafe_calls_even_without_discovery(self):
        failures = self.run_gateway(
            call(1, "message_send", {}),
            call(2, "message_delete", {}),
            call(3, "flag_set", {"flag": "seen"}),
            call(4, "flag_add", {"flag": ["seen", "junk"]}),
            call(5, "message_read", {"message-id": "m1", "seen": True}),
            call(6, "attachment_download", {"message-id": "m1", "dir": "/tmp"}),
            call(7, "flag_remove", {"flag": {"value": "seen"}}),
            call(8, "flag_remove", {"flag": []}),
            call(9, "message_read", {"message-id": "m1", "account": "other"}),
        )
        self.assertEqual([r["id"] for r in failures], list(range(1, 10)))
        self.assertTrue(all(r["error"]["code"] == -32602 for r in failures))

    def test_allows_reads_and_selected_flag_changes(self):
        replies = self.run_gateway(
            call(1, "message_read", {"message-id": "m1"}),
            call(2, "flag_remove", {"flag": "seen", "message-ids": ["m1"]}),
            call(3, "flag_add", {"flag": ["seen", "flagged"], "message-ids": ["m1"]}),
        )
        self.assertEqual(replies[0]["result"]["params"]["arguments"], {"message-id": "m1"})
        self.assertEqual(replies[1]["result"]["params"]["arguments"]["flag"], "seen")
        self.assertEqual(replies[2]["result"]["params"]["arguments"]["flag"], ["seen", "flagged"])

    def test_read_only_denies_any_flag_change(self):
        listing, denied, read = self.run_gateway(
            request(1, "tools/list"),
            call(2, "flag_add", {"flag": "seen"}),
            call(3, "message_read", {"message-id": "m1"}),
            read_only=True,
        )
        self.assertEqual([t["name"] for t in listing["result"]["tools"]], ["message_read"])
        self.assertEqual(denied["error"]["code"], -32602)
        self.assertIn("result", read)

    def test_rejects_batches_and_duplicate_keys_then_reads_on(self):
        batch, duplicate, ping = self.run_gateway(
            [call(1, "message_delete", {})],
            '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":'
            '{"name":"message_delete","name":"message_read"}}',
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            request(3, "ping"),
        )
        self.assertEqual(batch["error"]["code"], -32600)
        self.assertEqual(duplicate["error"]["code"], -32700)
        self.assertIn("result", ping)
