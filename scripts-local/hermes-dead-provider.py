#!/usr/bin/env python3
"""Dead-end OpenAI-compatible provider for testing fallback notices.

Listens on 127.0.0.1:18082 and answers every request with a 503, mimicking
the GLM "backend unavailable" shape that triggers provider fallback.
Usage: python3 hermes-dead-provider.py [port]
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18082


class Handler(BaseHTTPRequestHandler):
    def _reject(self):
        body = json.dumps({
            "error": {
                "message": "dead-model-test backend unavailable: "
                           "simulated outage (fallback test provider)",
                "type": "server_error",
                "code": 503,
            }
        }).encode()
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _reject

    def log_message(self, fmt, *args):
        print("[dead-provider] %s" % (fmt % args), flush=True)


if __name__ == "__main__":
    print(f"dead-provider: listening on 127.0.0.1:{PORT}, always 503", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
