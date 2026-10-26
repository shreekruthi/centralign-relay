"""A separate, stateful supplier HTTP system and its client connector.

The supplier owns its own SQLite ledger. It cannot join Relay's transactions.
Demo failure: commit a purchase, then close the socket without sending a response.
"""

import hashlib
import json
import secrets
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, Request, build_opener


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class SupplierError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SupplierLedger:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS stock(
                    id TEXT, sku TEXT, name TEXT, unit_cents INTEGER, stock INTEGER,
                    version INTEGER, PRIMARY KEY(id,sku));
                CREATE TABLE IF NOT EXISTS purchases(
                    key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, body TEXT NOT NULL);
            """)
            for sku, price in [("KEYBOARD", 5000), ("MOUSE", 2500), ("MONITOR", 18000)]:
                for vid, name, amount in [("atlas", "Atlas Supply", price), ("beacon", "Beacon Office", price+1000),
                                          ("unknown", "Unvetted Bargains", 100)]:
                    db.execute("INSERT OR IGNORE INTO stock VALUES(?,?,?,?,100,1)", (vid, sku, name, amount))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def catalog(self, sku):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM stock WHERE sku=? ORDER BY unit_cents,id", (sku,))]

    def lookup(self, key):
        with self.db() as db:
            row = db.execute("SELECT body FROM purchases WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def purchase(self, key, quote):
        if not isinstance(key, str) or not 1 <= len(key) <= 100 or not isinstance(quote, dict):
            raise SupplierError("invalid", "Invalid purchase contract.")
        fingerprint = hashlib.sha256(canonical(quote).encode()).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute("SELECT * FROM purchases WHERE key=?", (key,)).fetchone()
                if existing:
                    if existing["request_hash"] != fingerprint:
                        raise SupplierError("conflict", "Idempotency key was already used for a different purchase.")
                    db.commit()
                    return json.loads(existing["body"]), False
                vendor = db.execute("SELECT * FROM stock WHERE id=? AND sku=?", (quote.get("vendor"), quote.get("sku"))).fetchone()
                qty = quote.get("quantity")
                if not vendor or type(qty) is not int or not 1 <= qty <= 100:
                    raise SupplierError("invalid", "Unknown SKU/vendor or invalid quantity.")
                if vendor["stock"] < qty:
                    raise SupplierError("supplier", "Supplier has insufficient stock.")
                if (quote.get("expires_at", 0) <= time.time() or quote.get("vendor_version") != vendor["version"]
                        or quote.get("unit_cents") != vendor["unit_cents"]
                        or quote.get("total_cents") != qty * vendor["unit_cents"]):
                    raise SupplierError("stale", "Supplier rejected an expired or changed quote.")
                body = {"external_order_id": "SUP-"+key, "idempotency_key": key, "fingerprint": fingerprint,
                        "vendor": vendor["id"], "sku": quote["sku"], "quantity": qty,
                        "total_cents": quote["total_cents"], "status": "fulfilled", "sandbox": True}
                db.execute("UPDATE stock SET stock=stock-?,version=version+1 WHERE id=? AND sku=?", (qty,vendor["id"],quote["sku"]))
                db.execute("INSERT INTO purchases VALUES(?,?,?)", (key, fingerprint, canonical(body)))
                db.commit()
                return body, True
            except BaseException:
                db.rollback()
                raise


class SupplierService:
    def __init__(self, path, port=0):
        self.ledger = SupplierLedger(path)
        self.token = secrets.token_urlsafe(32)
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, status, value):
                body = canonical(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def allowed(self):
                return (self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
                        and secrets.compare_digest(self.headers.get("X-Sandbox-Token", ""), service.token))

            def do_GET(self):
                if not self.allowed():
                    self.send(403, {"error": "Sandbox capability required."})
                    return
                parsed = urlparse(self.path)
                if parsed.path == "/catalog":
                    self.send(200, service.ledger.catalog(parse_qs(parsed.query).get("sku", [""])[0]))
                elif parsed.path.startswith("/orders/"):
                    self.send(200, {"order": service.ledger.lookup(parsed.path.split("/")[-1])})
                else:
                    self.send(404, {"error": "Unknown route."})

            def do_POST(self):
                if not self.allowed():
                    self.send(403, {"error": "Sandbox capability required."})
                    return
                if self.path != "/orders":
                    self.send(404, {"error": "Unknown route."})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16384:
                        raise ValueError("Invalid body size.")
                    data = json.loads(self.rfile.read(length))
                    body, created = service.ledger.purchase(data["idempotency_key"], data["quote"])
                    if created and data.get("drop_response") is True:
                        # Real transport failure AFTER the independent supplier committed.
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    self.send(201 if created else 200, body)
                except SupplierError as exc:
                    self.send(409, {"code": exc.code, "error": str(exc)})
                except (ValueError, TypeError, KeyError):
                    self.send(400, {"code": "invalid", "error": "Invalid JSON purchase contract."})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.thread = None
        self.client = SupplierClient(f"http://127.0.0.1:{self.server.server_port}", self.token)

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="sandbox-supplier")
        self.thread.start()
        return self

    def stop(self):
        if self.thread:
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()


class SupplierClient:
    def __init__(self, base, token):
        parsed = urlparse(base)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path:
            raise ValueError("Only a loopback sandbox endpoint is supported.")
        self.base, self.token = base, token
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path, body=None):
        req = Request(self.base+path, canonical(body).encode() if body is not None else None,
                      {"Content-Type":"application/json", "X-Sandbox-Token":self.token})
        try:
            with self.opener.open(req, timeout=3) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                error = json.load(exc)
            finally:
                exc.close()
            raise SupplierError(error.get("code", "unavailable"), error.get("error", "Supplier rejected request.")) from exc
        except (OSError, HTTPException, ValueError) as exc:
            raise SupplierError("transport", "Supplier response was unavailable; reconcile before any new purchase.") from exc

    def catalog(self, sku):
        return self.request("/catalog?sku="+sku)

    def lookup(self, key):
        return self.request("/orders/"+key)["order"]

    def purchase(self, key, quote, drop_response=False):
        return self.request("/orders", {"idempotency_key":key,"quote":quote,"drop_response":drop_response})
