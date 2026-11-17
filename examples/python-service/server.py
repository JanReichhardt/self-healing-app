import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(200 if self.path == "/health" else 404, {"status": "UP"})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
            price = body["price_cents"]
            if not 0 <= price <= 1_000_000:
                return self.reply(400, {"error": "price_cents out of range"})
            # Deliberate incident: the optional code is used without a presence check.
            code = body["code"].strip().upper()
            self.reply(200, {"total_cents": price * 90 // 100 if code == "SAVE10" else price})
        except Exception:
            logging.exception("POST %s failed", self.path)
            self.reply(500, {"error": "internal"})


ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
