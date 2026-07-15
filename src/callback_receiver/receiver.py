from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("content-length") or "0")
        body = self.rfile.read(length).decode("utf-8", "replace")
        print(
            f"[{datetime.now(timezone.utc).isoformat()}] "
            f"callback path={self.path} status=received body={body}",
            flush=True,
        )
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, fmt, *args):
        print(
            f"[{datetime.now(timezone.utc).isoformat()}] http {fmt % args}",
            flush=True,
        )


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
