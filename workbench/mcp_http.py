#!/usr/bin/env python3
"""Small authenticated HTTP transport draft for the shared MCP facade.

It intentionally binds to loopback by default.  Put an authenticated reverse
proxy in front before any remote exposure; this file does not alter VPS state.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp_adapter import MemoryService, rpc


MAX_BODY = 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "xiaoxun-ledger-mcp/0.1"

    def log_message(self, fmt, *args):
        # Do not put Authorization headers or tool arguments into access logs.
        return

    def _write(self, status: int, payload: dict):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path.rstrip("/") != "/mcp":
            self._write(404, {"error": "not found"})
            return
        expected = str(getattr(self.server, "token", ""))
        supplied = self.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(supplied, "Bearer " + expected):
            self._write(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._write(413, {"error": "request body too large"})
            return
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            response = rpc(self.server.service, request)
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": str(exc)[:200]}}
        if response is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._write(200, response)


def serve_http(host: str = "127.0.0.1", port: int = 8765, token: str | None = None) -> None:
    secret = token or os.environ.get("XIAOXUN_MCP_TOKEN", "")
    if len(secret) < 24:
        raise SystemExit("XIAOXUN_MCP_TOKEN must be at least 24 characters")
    server = ThreadingHTTPServer((host, int(port)), Handler)
    server.token = secret
    server.service = MemoryService()
    print("xiaoxun-ledger MCP HTTP listening on %s:%d" % (host, port), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=None)
    args = parser.parse_args()
    serve_http(args.host, args.port, args.token)

