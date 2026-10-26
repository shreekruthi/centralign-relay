"""Loopback-only HTTP review UI. No frameworks, build step or external assets."""

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .goals import parse_with_ollama
from .tools import SPECS

STATIC = Path(__file__).resolve().parent.parent / "static"


def make_server(runtime, port=8765, model=None):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send(self, status, value, content_type="application/json", filename=None):
            body = json.dumps(value).encode() if content_type == "application/json" else value
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def do_GET(self):
            if not self.valid_host():
                self.send(403, {"error": "Loopback host required."})
                return
            path = urlparse(self.path).path
            try:
                if path == "/api/state":
                    self.send(200, {"tasks": runtime.store.tasks(), "company": runtime.store.company(),
                                    "tools": [s.__dict__ for s in SPECS], "token": token,
                                    "connector": "Independent supplier HTTP API" if runtime.tools.supplier else "Local reference connector",
                                    "interpreter": f"Ollama / {model}" if model else "Offline goal grammar + symbolic planner"})
                elif path.startswith("/api/tasks/"):
                    parts = path.strip("/").split("/")
                    task = runtime.store.task(parts[2])
                    if len(parts) == 3:
                        self.send(200, task)
                    elif len(parts) == 4 and parts[3] == "evidence":
                        self.send(200, task, filename=f"relay-{task['id'][:8]}-evidence.json")
                    elif len(parts) == 4 and parts[3] == "receipt":
                        with runtime.store.connect() as db:
                            receipt = db.execute("SELECT r.body FROM receipts r JOIN orders o ON o.id=r.order_id WHERE o.task_id=?", (task["id"],)).fetchone()
                            if not receipt:
                                receipt = db.execute("SELECT body FROM transfer_receipts WHERE task_id=?", (task["id"],)).fetchone()
                        if not receipt:
                            raise KeyError("Receipt not found")
                        self.send(200, json.loads(receipt["body"]), filename=f"relay-{task['id'][:8]}-receipt.json")
                    else:
                        raise KeyError("Route not found")
                elif path in {"/", "/app.js", "/style.css"}:
                    filename = "index.html" if path == "/" else path[1:]
                    content_type = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}[filename]
                    self.send(200, (STATIC / filename).read_bytes(), content_type)
                else:
                    raise KeyError("Route not found")
            except KeyError as exc:
                self.send(404, {"error": str(exc)})

        def do_POST(self):
            if not self.valid_host() or not secrets.compare_digest(self.headers.get("X-Relay-Token", ""), token):
                self.send(403, {"error": "Reload the local UI to obtain a valid request token."})
                return
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                self.send(403, {"error": "Cross-origin writes are forbidden."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    self.send(413, {"error": "JSON body must be 1–16384 bytes."})
                    return
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("JSON object required.")
                path = urlparse(self.path).path.strip("/").split("/")
                if path == ["api", "tasks"]:
                    text = body.get("goal", "")
                    intent = parse_with_ollama(text, model) if model else None
                    task_id = runtime.create(text, body.get("scenario", "normal"), intent=intent,
                                             interpreter=f"Ollama / {model}" if model else "offline grammar")
                    self.send(201, {"id": task_id})
                elif len(path) == 4 and path[:2] == ["api", "tasks"] and path[3] == "decision":
                    self.send(200, runtime.decide(path[2], body.get("approval_id"), body.get("approved"), body.get("reason", "")))
                elif len(path) == 4 and path[:2] == ["api", "tasks"] and path[3] == "clarify":
                    self.send(200, runtime.clarify(path[2], body))
                else:
                    raise KeyError("Route not found")
            except KeyError as exc:
                self.send(404, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self.send(400, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server
