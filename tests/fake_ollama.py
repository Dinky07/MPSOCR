"""A tiny stand-in for Ollama's HTTP API, for tests: records every request and answers with canned JSON."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeOllama:
    def __init__(self, reply=None, status=200):
        self.requests = []                     # (method, path, parsed body or None)
        self.reply = reply if reply is not None else {"message": {"content": "{}"}, "done_reason": "stop"}
        self.status = status
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                b = json.dumps(fake.reply).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                self._answer()

            def do_POST(self):
                n = int(self.headers["Content-Length"])
                fake.requests.append(("POST", self.path, json.loads(self.rfile.read(n))))
                self._answer()

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()
