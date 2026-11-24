"""Exercise the Seoul auth stack with disposable accounts and a loopback reader."""

import base64
from email.utils import parsedate_to_datetime
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time

HOST = "reader.example.com"
ORIGIN = f"https://{HOST}"
COOKIE_NAME = "himalaya_reader_session"


class Reader(BaseHTTPRequestHandler):
    def do_GET(self):
        assert self.headers["Host"] == f"127.0.0.1:{self.server.server_port}"
        assert "Cookie" not in self.headers and "Authorization" not in self.headers
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"synthetic reader")

    def log_message(self, *_):
        pass


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return str(sock.getsockname()[1])


def totp(secret):
    step = int(time.time()) // 30
    digest = hmac.digest(secret, step.to_bytes(8, "big"), hashlib.sha1)
    offset = digest[-1] & 15
    number = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def exercise(directory, reader_port):
    project = f"himalaya-auth-check-{secrets.token_hex(4)}"
    proxy_port, auth_port = free_port(), free_port()
    env = dict(
        os.environ,
        READER_HOST=HOST,
        READER_PORT=str(reader_port),
        READER_PROXY_PORT=proxy_port,
        READER_AUTH_PORT=auth_port,
        READER_AUTH_DIR=str(directory),
    )
    manifest = Path(__file__).with_name("compose.yaml")
    compose_command = ["docker", "compose", "-f", str(manifest), "-p", project]

    def compose(*args, output=False):
        result = subprocess.run(
            [*compose_command, *args],
            env=env,
            check=True,
            capture_output=output,
            text=True,
        )
        return result.stdout

    def request(path, *, cookie=None, data=None, headers=None, status=200):
        headers = dict(headers or {})
        headers.setdefault("Host", HOST)
        headers.setdefault("Accept", "text/html")
        if cookie:
            headers["Cookie"] = cookie
        if data is not None:
            headers.update({"Origin": ORIGIN, "Content-Type": "application/json"})
        connection = http.client.HTTPConnection("127.0.0.1", int(proxy_port), timeout=5)
        try:
            connection.request(
                "GET" if data is None else "POST",
                path,
                None if data is None else json.dumps(data),
                headers,
            )
            result = connection.getresponse()
            body = result.read()
            assert result.status == status, (path, status, result.status, body[:250])
            return result.headers, body
        finally:
            connection.close()

    def ready():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                request("/authelia/")
                return
            except (OSError, AssertionError):
                time.sleep(0.25)
        raise RuntimeError("Authelia and the loopback proxy did not become ready")

    def cookie_header(headers):
        cookie = next(
            value
            for value in headers.get_all("Set-Cookie", [])
            if value.startswith(f"{COOKIE_NAME}=")
        )
        lower = cookie.lower()
        assert all(flag in lower for flag in ("; secure", "; httponly", "samesite=lax"))
        return cookie

    def cookie_expiry(cookie):
        parsed = re.search(r"(?:^|;\s*)expires=([^;]+)", cookie, re.I)
        return parsedate_to_datetime(parsed.group(1)).timestamp()

    def redis(*args):
        return compose("exec", "-T", "redis", "redis-cli", *args, output=True).strip()

    def sign_in(password, *, remember):
        headers, body = request(
            "/authelia/api/firstfactor",
            data={
                "username": "reader",
                "password": password,
                "keepMeLoggedIn": remember,
                "targetURL": ORIGIN + "/",
            },
        )
        assert json.loads(body)["status"] == "OK"
        return cookie_header(headers)

    def complete_login(secret, cookie):
        headers, body = request(
            "/authelia/api/secondfactor/totp",
            cookie=cookie,
            data={"token": totp(secret), "targetURL": ORIGIN + "/"},
        )
        assert json.loads(body)["status"] == "OK"
        return cookie_header(headers)

    authelia_flags = [
        "--config",
        "/etc/authelia/reader.yml",
        "--config.experimental.filters=template",
    ]
    for name in ("session", "storage"):
        path = directory / name
        path.write_text(secrets.token_hex(32) + "\n")
        path.chmod(0o600)
    password = secrets.token_urlsafe(32)
    seed = secrets.token_bytes(32)
    users = directory / "users.yml"
    users.touch(mode=0o600)
    try:
        digest_output = compose(
            "run", "--rm", "--no-deps", "authelia", "authelia",
            "crypto", "hash", "generate", "argon2", "--password", password,
            output=True,
        )
        digest = re.search(r"\$argon2id\$\S+", digest_output).group(0)
        users.write_text(
            "users:\n"
            "  reader:\n"
            "    displayname: Synthetic Reader\n"
            "    email: reader@example.com\n"
            f"    password: '{digest}'\n"
        )
        compose("config", "--quiet")
        compose(
            "run", "--rm", "--no-deps", "authelia", "authelia",
            "config", "validate", *authelia_flags,
        )
        compose("up", "-d")
        ready()
        compose(
            "exec", "-T", "authelia", "authelia",
            "storage", "user", "totp", "generate", "reader",
            "--secret", base64.b32encode(seed).decode().rstrip("="), *authelia_flags,
            output=True,
        )
        protected = ("/", "/message", "/attachment", "/reader.css")
        for path in protected:
            headers, _ = request(path, status=302)
            assert headers["Location"].startswith(ORIGIN + "/authelia/?")
        request("/", headers={"Host": "wrong.example.com"}, status=421)
        request(
            "/attachment",
            headers={
                "Remote-User": "reader",
                "X-Forwarded-User": "reader",
                "X-Forwarded-Host": "wrong.example.com",
                "X-Forwarded-Proto": "http",
                "X-Forwarded-Uri": "/authelia/",
                "Authorization": "Bearer forged",
            },
            status=302,
        )
        for path in ("/mcp", "/approval", "/authelia/api/authz/forward-auth"):
            request(path, status=404)

        ordinary = sign_in(password, remember=False)
        assert abs(cookie_expiry(ordinary) - time.time() - 3600) < 10
        request("/", cookie=ordinary.split(";", 1)[0], status=302)

        pending = sign_in(password, remember=True).split(";", 1)[0]
        for path in protected:
            request(path, cookie=pending, status=302)
        authenticated = complete_login(seed, pending)
        assert abs(cookie_expiry(authenticated) - time.time() - 30 * 86400) < 10
        cookie = authenticated.split(";", 1)[0]
        session_id = cookie.removeprefix(f"{COOKIE_NAME}=")
        keys = redis("--scan").splitlines()
        key = next(candidate for candidate in keys if candidate.endswith(session_id))
        lifetime = int(redis("PTTL", key))
        assert 30 * 86400_000 - 10_000 < lifetime <= 30 * 86400_000
        time.sleep(1)
        for path in protected:
            headers, body = request(path, cookie=cookie)
            assert headers["Cache-Control"] == "no-store"
            assert not headers.get_all("Set-Cookie"), "access updated the browser cookie"
            assert body == b"synthetic reader"
        assert 0 < int(redis("PTTL", key)) < lifetime - 500
        request("/attachment", status=302)
        compose("restart", "redis", "authelia")
        ready()
        request("/attachment", cookie=cookie)
        # Put a duplicate session close to its deadline without waiting 30 days.
        expiring_id = secrets.token_hex(16)
        expiring_key = key.removesuffix(session_id) + expiring_id
        assert redis("COPY", key, expiring_key) == "1"
        assert redis("PEXPIRE", expiring_key, "3000") == "1"
        expiring_cookie = f"{COOKIE_NAME}={expiring_id}"
        request("/attachment", cookie=expiring_cookie)
        time.sleep(3)
        for path in protected:
            request(path, cookie=expiring_cookie, status=302)
        request("/authelia/api/logout", cookie=cookie, data={"targetURL": ORIGIN + "/"})
        for path in protected:
            request(path, cookie=cookie, status=302)
        compose("stop", "authelia")
        request("/attachment", cookie=cookie, status=502)
        print("Seoul loopback authentication checks passed")
    finally:
        compose("down", "--volumes")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="himalaya-auth-") as directory:
        with ThreadingHTTPServer(("127.0.0.1", 0), Reader) as reader:
            thread = threading.Thread(target=reader.serve_forever)
            thread.start()
            try:
                exercise(Path(directory), reader.server_port)
            finally:
                reader.shutdown()
                thread.join()
