"""CONNECT gateway for delegated model traffic and explicitly granted service hosts."""

GATEWAY_SOURCE = r"""
import fnmatch
import hashlib
import json
import re
import select
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALLOWED = {"chatgpt.com", "auth.openai.com", "api.openai.com"}
ALLOWED.update(json.loads(sys.argv[1]) if len(sys.argv) > 1 else [])
log_lock = threading.Lock()
log_count = 0

class Proxy(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_CONNECT(self):
        global log_count
        host, sep, port = self.path.rpartition(":")
        valid = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host) is not None
        allowed = (valid and bool(sep) and port == "443"
                   and any(fnmatch.fnmatchcase(host.lower(), rule) for rule in ALLOWED))
        with log_lock:
            if log_count < 1000:
                print(json.dumps({
                    "destination": host.lower() if allowed else "denied",
                    "destination_digest": hashlib.sha256(self.path.encode()).hexdigest(),
                    "allowed": allowed,
                }), flush=True)
                log_count += 1
        if not allowed:
            self.send_error(403, "Destination is outside the configured grant")
            return
        try:
            remote = socket.create_connection((host, 443), timeout=15)
        except OSError:
            self.send_error(502)
            return
        self.send_response(200, "Connection established")
        self.end_headers()
        try:
            with remote:
                peers = [self.connection, remote]
                while True:
                    ready, _, _ = select.select(peers, [], [], 120)
                    if not ready:
                        return
                    for src in ready:
                        buf = src.recv(65536)
                        if not buf:
                            return
                        (remote if src is self.connection else self.connection).sendall(buf)
        except OSError:
            pass

if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 3128), Proxy).serve_forever()
"""
