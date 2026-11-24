"""Accept a synthetic SMTP message and record the envelope and DATA locally."""

import json
import socket
import socketserver
import sys

PORT = 2525
CAPTURE = "/tmp/capture.jsonl"


class SmtpSession(socketserver.StreamRequestHandler):
    def reply(self, response):
        self.wfile.write(response + b"\r\n")

    def handle(self):
        self.request.settimeout(10)
        sender = None
        recipients = []
        self.reply(b"220 synthetic ESMTP")
        for line in self.rfile:
            command, _, argument = line.rstrip(b"\r\n").partition(b" ")
            command = command.upper()
            if command in (b"EHLO", b"HELO"):
                self.reply(b"250 synthetic")
            elif command == b"MAIL" and argument.upper().startswith(b"FROM:"):
                sender = argument[5:].decode()
                recipients = []
                self.reply(b"250 Sender accepted")
            elif (
                command == b"RCPT"
                and sender is not None
                and argument.upper().startswith(b"TO:")
            ):
                recipients.append(argument[3:].decode())
                self.reply(b"250 Recipient accepted")
            elif command == b"DATA" and recipients:
                self.reply(b"354 End with <CRLF>.<CRLF>")
                data = bytearray()
                for line in self.rfile:
                    if line == b".\r\n":
                        with open(CAPTURE, "a", encoding="utf-8") as capture:
                            print(json.dumps({
                                "sender": sender,
                                "recipients": recipients,
                                "message": data.decode(),
                            }), file=capture)
                        self.reply(b"250 Message accepted")
                        sender = None
                        recipients = []
                        break
                    data.extend(line[1:] if line.startswith(b"..") else line)
            elif command == b"QUIT":
                self.reply(b"221 Goodbye")
                return
            else:
                self.reply(b"503 Unexpected SMTP command")


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1) as connection:
            if not connection.recv(128).startswith(b"220 "):
                sys.exit("SMTP sink did not respond")
    else:
        with socketserver.TCPServer(("0.0.0.0", PORT), SmtpSession) as server:
            server.serve_forever()
