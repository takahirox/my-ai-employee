"""Model-only CONNECT gateway, shared design with Pocket Agent Bench (no body logs)."""

GATEWAY_SOURCE = (
    '"""CONNECT-only model gateway on a Docker internal network; no credentials logged."""\n'
    "\n"
    "import select\n"
    "import socket\n"
    "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
    "\n"
    'ALLOWED = {"chatgpt.com", "auth.openai.com", "api.openai.com"}\n'
    "\n"
    "\n"
    "class Proxy(BaseHTTPRequestHandler):\n"
    "    def log_message(self, *args):\n"
    "        pass\n"
    "\n"
    "    def do_CONNECT(self):\n"
    '        host, sep, port = self.path.rpartition(":")\n'
    '        if not sep or host.lower() not in ALLOWED or port != "443":\n'
    '            self.send_error(403, "Destination is not a model endpoint")\n'
    "            return\n"
    "        try:\n"
    "            remote = socket.create_connection((host, 443), timeout=15)\n"
    "        except OSError:\n"
    "            self.send_error(502)\n"
    "            return\n"
    '        self.send_response(200, "Connection established")\n'
    "        self.end_headers()\n"
    "        try:\n"
    "            with remote:\n"
    "                peers = [self.connection, remote]\n"
    "                while True:\n"
    "                    ready, _, _ = select.select(peers, [], [], 120)\n"
    "                    if not ready:\n"
    "                        return\n"
    "                    for src in ready:\n"
    "                        buf = src.recv(65536)\n"
    "                        if not buf:\n"
    "                            return\n"
    "                        (remote if src is self.connection else self.connection).sendall(buf)\n"
    "        except OSError:\n"
    "            pass\n"
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    '    ThreadingHTTPServer(("0.0.0.0", 3128), Proxy).serve_forever()\n'
    "\n"
)
