"""SQLite persistence and a fictional company. All money uses integer cents."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(self, path="data/relay.sqlite3"):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def initialize(self):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY, goal TEXT NOT NULL, intent TEXT NOT NULL,
                    status TEXT NOT NULL, memory TEXT NOT NULL DEFAULT '{}',
                    scenario TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    steps INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT);
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
                    at REAL NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS policies(
                    id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
                    approval_cents INTEGER NOT NULL, max_order_cents INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS inventory(sku TEXT PRIMARY KEY, quantity INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS vendors(
                    id TEXT NOT NULL, sku TEXT NOT NULL, name TEXT NOT NULL, unit_cents INTEGER NOT NULL,
                    stock INTEGER NOT NULL, approved INTEGER NOT NULL, version INTEGER NOT NULL,
                    PRIMARY KEY(id,sku));
                CREATE TABLE IF NOT EXISTS approvals(
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    fingerprint TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                    decided_at REAL, reason TEXT);
                CREATE TABLE IF NOT EXISTS orders(
                    id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL REFERENCES tasks(id),
                    vendor TEXT NOT NULL, sku TEXT NOT NULL, quantity INTEGER NOT NULL,
                    total_cents INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                    quote TEXT NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(
                    id TEXT PRIMARY KEY, order_id TEXT UNIQUE NOT NULL REFERENCES orders(id),
                    quantity INTEGER NOT NULL, total_cents INTEGER NOT NULL, body TEXT NOT NULL,
                    sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS movements(
                    order_id TEXT PRIMARY KEY REFERENCES orders(id), sku TEXT NOT NULL, quantity INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS tool_calls(
                    key TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    tool TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS faults(
                    task_id TEXT NOT NULL REFERENCES tasks(id), name TEXT NOT NULL,
                    remaining INTEGER NOT NULL, PRIMARY KEY(task_id,name));
                CREATE TABLE IF NOT EXISTS memories(
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id), summary TEXT NOT NULL, at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS surplus(sku TEXT PRIMARY KEY, quantity INTEGER NOT NULL, version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS transfers(
                    id TEXT PRIMARY KEY, task_id TEXT UNIQUE REFERENCES tasks(id), sku TEXT NOT NULL,
                    quantity INTEGER NOT NULL, source_before INTEGER NOT NULL, stock_before INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS transfer_receipts(
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id), body TEXT NOT NULL, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS purchase_intents(
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id), fingerprint TEXT NOT NULL,
                    quote TEXT NOT NULL, status TEXT NOT NULL);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(orders)")}
            if "external_order_id" not in columns:
                db.execute("ALTER TABLE orders ADD COLUMN external_order_id TEXT")
            db.execute("INSERT OR IGNORE INTO policies VALUES(1,1,50000,200000)")
            for sku, base in [("KEYBOARD", 5000), ("MOUSE", 2500), ("MONITOR", 18000)]:
                db.execute("INSERT OR IGNORE INTO inventory VALUES(?,?)", (sku, 5))
                db.execute("INSERT OR IGNORE INTO surplus VALUES(?,?,1)", (sku, 6 if sku == "MOUSE" else 0))
                for vid, name, price, approved in [
                    ("atlas", "Atlas Supply", base, 1),
                    ("beacon", "Beacon Office", base + 1000, 1),
                    ("unknown", "Unvetted Bargains", 100, 0),
                ]:
                    db.execute("INSERT OR IGNORE INTO vendors VALUES(?,?,?,?,?,?,1)",
                               (vid, sku, name, price, 100, approved))

    @staticmethod
    def event(db, task_id, kind, message, detail=None):
        db.execute("INSERT INTO events(task_id,at,kind,message,detail) VALUES(?,?,?,?,?)",
                   (task_id, time.time(), kind, message, encode(detail or {})))

    def task(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError("Task not found")
            task = dict(row)
            for name in ["intent", "memory"]:
                task[name] = json.loads(task[name])
            task["events"] = [dict(r) | {"detail": json.loads(r["detail"])} for r in db.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY seq", (task_id,))]
            task["approvals"] = [dict(r) | {"payload": json.loads(r["payload"])} for r in db.execute(
                "SELECT * FROM approvals WHERE task_id=? ORDER BY rowid", (task_id,))]
            return task

    def tasks(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT id,goal,status,created,updated,steps FROM tasks ORDER BY created DESC")]

    def company(self):
        with self.connect() as db:
            return {"inventory": [dict(r) for r in db.execute("SELECT * FROM inventory")],
                    "vendors": [dict(r) for r in db.execute("SELECT * FROM vendors")],
                    "orders": [dict(r) for r in db.execute("SELECT * FROM orders")],
                    "surplus": [dict(r) for r in db.execute("SELECT * FROM surplus")],
                    "transfers": [dict(r) for r in db.execute("SELECT * FROM transfers")],
                    "policy": dict(db.execute("SELECT * FROM policies WHERE id=1").fetchone()),
                    "memories": [dict(r) for r in db.execute("SELECT * FROM memories ORDER BY at DESC LIMIT 10")]}
