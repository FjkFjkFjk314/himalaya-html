"""Exercise the production Himalaya image with isolated synthetic mail."""

from email import policy
from email.parser import Parser
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
PROJECT = f"himalaya-smoke-{uuid.uuid4().hex[:12]}"
COMPOSE = [
    "docker", "compose", "--project-name", PROJECT, "--env-file", "/dev/null",
    "-f", "compose.yaml", "-f", "tests/docker/compose.yaml",
    "--profile", "executor",
]
RUN = ["run", "--rm", "--no-deps", "-T"]


def compose(*args, capture=False, check=True):
    result = subprocess.run(
        [*COMPOSE, *args],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else None,
        text=True,
        check=check,
    )
    return result.stdout


def cli(*args):
    try:
        output = compose(
            *RUN, "himalaya", "-c", "/fixtures/config.toml", "--json", *args,
            capture=True,
        )
    except subprocess.CalledProcessError as error:
        print(error.stdout, end="", file=sys.stderr)
        raise
    return json.loads(output)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read_message(mailbox, subject, text):
    page = cli("envelope", "list", "--mailbox", mailbox)
    rows = page["envelopes"]
    require(len(rows) == 1 and rows[0]["subject"] == subject and not page["errors"], page)
    message = cli("message", "read", "--mailbox", mailbox, rows[0]["id"])
    require(message["id"] == rows[0]["id"], message)
    require(message["headers"]["subject"] == subject, message)
    require(message["text"].strip() == text, message)


def smoke():
    compose("config", "--quiet")
    compose("build", "himalaya")
    compose("up", "--detach", "--wait", "smtp")
    compose(
        *RUN, "--entrypoint", "/bin/sh", "himalaya", "-ec",
        'test "$(id -u):$(id -g)" = 10001:10001; '
        'test ! -w /fixtures/config.toml; '
        'mkdir -p /var/lib/himalaya/mail',
    )
    cli("mailbox", "create", "inbox")
    cli("mailbox", "create", "Sent")
    added = cli("message", "add", "--mailbox", "inbox", "/fixtures/inbox.eml")
    require(added["saved"] is True and added["errors"] == [], added)

    sent = cli("message", "send", "--save", "Sent", "/fixtures/outbound.eml")
    require(sent["sent"] is True and sent["saved"] is True and sent["errors"] == [], sent)
    capture = compose("exec", "-T", "smtp", "cat", "/tmp/capture.jsonl", capture=True)
    delivery = json.loads(capture)
    require(delivery["sender"] == "<himalaya@example.test>", delivery)
    require(delivery["recipients"] == ["<recipient@example.test>"], delivery)
    message = Parser(policy=policy.default).parsestr(delivery["message"])
    require(message["Subject"] == "Synthetic SMTP delivery", delivery)
    require(message.get_content().strip() ==
            "This message was delivered to the synthetic SMTP sink.", delivery)

    # Recreate the project containers while retaining the production state volume.
    compose("down")
    read_message("inbox", "Synthetic inbox message",
                 "This message lives only in the Compose smoke test.")
    read_message("Sent", "Synthetic SMTP delivery",
                 "This message was delivered to the synthetic SMTP sink.")


if __name__ == "__main__":
    try:
        smoke()
    except Exception:
        compose("logs", "--no-color", "smtp", check=False)
        raise
    finally:
        compose("down", "--volumes", "--remove-orphans", "--rmi", "local")
    print("Compose smoke passed (non-root volume, JSON mail reads, SMTP delivery).")
