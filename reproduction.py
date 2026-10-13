#!/usr/bin/env python3
"""A real scrape endpoint with stable label sets and changing gauge values."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import random
from urllib.parse import parse_qs, urlparse


class Handler(BaseHTTPRequestHandler):
    series = 1000

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/healthz":
            self.respond(200, b"ok\n", "text/plain")
            return
        if url.path != "/metrics":
            self.respond(404, b"not found\n", "text/plain")
            return
        ids = parse_qs(url.query).get("id", [])
        try:
            if len(ids) != 1:
                raise ValueError
            identity = int(ids[0])
            if not 1 <= identity <= 10000:
                raise ValueError
        except ValueError:
            self.respond(400, b"id must be a single integer in 1..10000\n", "text/plain")
            return
        lines = ["# TYPE reproduction_value gauge"]
        for i in range(self.series):
            lines.append(f'reproduction_value{{workload_id="{identity}",series="{i}"}} {random.random():.9f}')
        self.respond(200, ("\n".join(lines) + "\n").encode(), "text/plain; version=0.0.4")

    def respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass



if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
