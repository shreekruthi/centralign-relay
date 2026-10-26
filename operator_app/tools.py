"""Reusable registry with a narrow capability boundary over real sandbox records.

Tool effects + successful call receipts commit together. Runtime observations commit
separately: a crash between them is recovered by replaying the tool receipt.
"""

import hashlib
import json
import time
import uuid
from dataclasses import dataclass

from .store import Store, encode
from .supplier import SupplierError


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class ToolError(Exception):
    def __init__(self, code, message, vendor=None):
        super().__init__(message)
        self.code, self.vendor = code, vendor


@dataclass(frozen=True)
class ToolSpec:
    name: str
    capability: str
    description: str


SPECS = [
    ToolSpec("read_policy", "company.read", "Read versioned company spending policy"),
    ToolSpec("read_inventory", "inventory.read", "Read SKU baseline"),
    ToolSpec("search_suppliers", "suppliers.read", "Discover approved, stocked suppliers"),
    ToolSpec("choose_quote", "quotes.read", "Rank budget-feasible quotes"),
    ToolSpec("authorize", "approval.request", "Request approval for exact purchase fingerprint"),
    ToolSpec("place_order", "orders.write", "Purchase after independent policy enforcement"),
    ToolSpec("receive_order", "inventory.write", "Fulfill a sandbox order exactly once"),
    ToolSpec("write_receipt", "receipts.write", "Persist a downloadable receipt with SHA-256"),
    ToolSpec("verify_outcome", "company.read", "Read back and verify all outcome predicates"),
    ToolSpec("reconcile_purchase", "suppliers.read", "Reconcile an uncertain purchase against the independent supplier ledger"),
    ToolSpec("transfer_surplus", "inventory.write", "Move internal surplus to the requested stock location"),
    ToolSpec("write_transfer_receipt", "receipts.write", "Persist internal transfer evidence"),
    ToolSpec("verify_transfer", "company.read", "Independently verify both stock locations and transfer evidence"),
]


class ToolRegistry:
    def __init__(self, store, supplier=None):
        self.store, self.supplier = store, supplier
        self.specs = {spec.name: spec for spec in SPECS}

    def execute(self, task_id, name):
        if name not in self.specs:
            raise ToolError("permission", "Unknown tool; capabilities are deny-by-default.")
        if self.supplier and name in {"place_order", "reconcile_purchase"}:
            return self._remote_purchase(task_id, name)
        error = None
        with self.store.transaction() as db:
            task = dict(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
            goal, memory = json.loads(task["intent"]), json.loads(task["memory"])
            key = digest([task_id, name, memory.get("epoch", 0)])
            cached = db.execute("SELECT result FROM tool_calls WHERE key=?", (key,)).fetchone()
            if cached:
                Store.event(db, task_id, "replay", "Recovered a committed tool result; no effect repeated.", {"tool": name})
                return json.loads(cached["result"])
            try:
                result = getattr(self, name)(db, task, goal, memory)
                if "waiting" not in result:
                    db.execute("INSERT INTO tool_calls VALUES(?,?,?,?)", (key, task_id, name, encode(result)))
                Store.event(db, task_id, "execute", self.specs[name].description,
                            {"tool": name, "capability": self.specs[name].capability, "result": result})
            except ToolError as exc:
                # Fault counters/world changes must persist even if the attempted operation fails.
                error = exc
        if error:
            raise error
        return result

    @staticmethod
    def fault(db, task, name):
        row = db.execute("SELECT remaining FROM faults WHERE task_id=? AND name=?", (task["id"], name)).fetchone()
        if row and row[0] > 0:
            db.execute("UPDATE faults SET remaining=remaining-1 WHERE task_id=? AND name=?", (task["id"], name))
            return True
        return False

    @staticmethod
    def output(memory, name):
        try:
            return memory["outputs"][name]
        except KeyError as exc:
            raise ToolError("precondition", f"Missing observed prerequisite: {name}") from exc

    def read_policy(self, db, task, goal, memory):
        policy = dict(db.execute("SELECT * FROM policies WHERE id=1").fetchone())
        policy["prior_outcomes"] = []
        for row in db.execute("SELECT * FROM memories ORDER BY at DESC LIMIT 20"):
            outcome = json.loads(row["summary"])
            if outcome["sku"] == goal["sku"]:
                policy["prior_outcomes"].append({"task_id":row["task_id"], "vendor":outcome["vendor"], "verified_at":row["at"]})
        return {"fact": "policy", "data": policy}

    def read_inventory(self, db, task, goal, memory):
        row = db.execute("SELECT * FROM inventory WHERE sku=?", (goal["sku"],)).fetchone()
        data = dict(row)
        surplus = db.execute("SELECT * FROM surplus WHERE sku=?", (goal["sku"],)).fetchone()
        data.update(surplus_quantity=surplus["quantity"], surplus_version=surplus["version"])
        return {"fact": "inventory", "data": data}

    def search_suppliers(self, db, task, goal, memory):
        self.output(memory, "read_policy")
        self.output(memory, "read_inventory")
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM vendors WHERE sku=? AND approved=1 AND stock>=? ORDER BY unit_cents,id",
            (goal["sku"], goal["quantity"])) if row["id"] not in memory.get("excluded", [])]
        if self.supplier:
            approved = {row[0] for row in db.execute("SELECT id FROM vendors WHERE sku=? AND approved=1", (goal["sku"],))}
            rows = [row for row in self._catalog(goal["sku"]) if row["id"] in approved
                    and row["stock"] >= goal["quantity"] and row["id"] not in memory.get("excluded", [])]
        history = self.output(memory, "read_policy").get("prior_outcomes", [])
        for row in rows:
            row["prior_verified_successes"] = sum(past["vendor"] == row["id"] for past in history)
        return {"fact": "suppliers", "data": rows}

    def choose_quote(self, db, task, goal, memory):
        policy = self.output(memory, "read_policy")
        candidates = self.output(memory, "search_suppliers")
        feasible = [v for v in candidates if v["unit_cents"] * goal["quantity"] <= min(
            goal["budget_cents"], policy["max_order_cents"])]
        if not feasible:
            raise ToolError("infeasible", "No approved supplier can satisfy quantity and budget. No purchase made.")
        vendor = min(feasible, key=lambda v: (v["unit_cents"], -v.get("prior_verified_successes", 0), v["id"]))
        quote = {"vendor": vendor["id"], "vendor_name": vendor["name"], "sku": goal["sku"],
                 "quantity": goal["quantity"], "unit_cents": vendor["unit_cents"],
                 "total_cents": vendor["unit_cents"] * goal["quantity"],
                 "vendor_version": vendor["version"], "policy_version": policy["version"],
                 "expires_at": time.time() + 300}
        quote["selection_basis"] = "Lowest feasible price; verified prior successes break price ties."
        return {"fact": "quote", "data": quote}

    def check_quote(self, db, quote, goal):
        policy = dict(db.execute("SELECT * FROM policies WHERE id=1").fetchone())
        vendor = db.execute("SELECT * FROM vendors WHERE id=? AND sku=?", (quote["vendor"], goal["sku"])).fetchone()
        if self.supplier and vendor and vendor["approved"]:
            vendor = next((row | {"approved":1} for row in self._catalog(goal["sku"]) if row["id"] == quote["vendor"]), None)
        if quote["expires_at"] <= time.time() or quote["policy_version"] != policy["version"]:
            raise ToolError("stale", "Quote expired or company policy changed; obtain a fresh quote and approval.")
        if not vendor or not vendor["approved"] or vendor["stock"] < goal["quantity"]:
            raise ToolError("supplier", "Supplier no longer approved or sufficiently stocked.", quote["vendor"])
        if vendor["version"] != quote["vendor_version"] or vendor["unit_cents"] != quote["unit_cents"]:
            raise ToolError("stale", "Supplier price/version changed; the old approval cannot authorize a new purchase.")
        if (quote["sku"] != goal["sku"] or quote["quantity"] != goal["quantity"]
                or quote["total_cents"] != vendor["unit_cents"] * goal["quantity"]
                or quote["total_cents"] > min(goal["budget_cents"], policy["max_order_cents"])):
            raise ToolError("permission", "Purchase exceeds the canonical goal or live spending policy.")
        return policy

    def authorize(self, db, task, goal, memory):
        quote = self.output(memory, "choose_quote")
        policy = self.check_quote(db, quote, goal)
        fingerprint = digest(quote)
        if quote["total_cents"] <= policy["approval_cents"]:
            return {"fact": "authorized", "data": {"fingerprint": fingerprint, "mode": "company_policy"}}
        approval = db.execute("SELECT * FROM approvals WHERE task_id=? AND fingerprint=? ORDER BY rowid DESC LIMIT 1",
                              (task["id"], fingerprint)).fetchone()
        if approval and approval["status"] == "approved":
            return {"fact": "authorized", "data": {"fingerprint": fingerprint, "mode": "human", "approval_id": approval["id"]}}
        if approval and approval["status"] == "rejected":
            raise ToolError("rejected", "Human rejected this purchase. Task stopped without placing an order.")
        if not approval:
            approval_id = str(uuid.uuid4())
            db.execute("INSERT INTO approvals(id,task_id,fingerprint,payload,status) VALUES(?,?,?,?, 'pending')",
                       (approval_id, task["id"], fingerprint, encode(quote)))
        else:
            approval_id = approval["id"]
        return {"waiting": approval_id, "data": quote}

    def place_order(self, db, task, goal, memory):
        existing = db.execute("SELECT * FROM orders WHERE task_id=?", (task["id"],)).fetchone()
        if existing:
            return {"fact": "ordered", "data": dict(existing)}
        quote = self.output(memory, "choose_quote")
        policy = self.check_quote(db, quote, goal)
        fingerprint = digest(quote)
        authorization = self.output(memory, "authorize")
        if authorization["fingerprint"] != fingerprint:
            raise ToolError("permission", "Authorization does not match the exact quote.")
        if quote["total_cents"] > policy["approval_cents"]:
            approved = db.execute("SELECT id FROM approvals WHERE task_id=? AND fingerprint=? AND status='approved'",
                                  (task["id"], fingerprint)).fetchone()
            if not approved:
                raise ToolError("permission", "A matching human approval is required at the write boundary.")
        if self.fault(db, task, "supplier_outage"):
            raise ToolError("supplier", "Atlas declined at checkout; choose another feasible supplier.", quote["vendor"])
        if self.fault(db, task, "transient"):
            raise ToolError("transient", "Sandbox supplier temporarily unavailable.")
        order_id = "PO-" + task["id"][:8].upper()
        db.execute("INSERT INTO orders(id,task_id,vendor,sku,quantity,total_cents,fingerprint,quote,status) VALUES(?,?,?,?,?,?,?,?,?)",
                   (order_id, task["id"], quote["vendor"], goal["sku"], goal["quantity"],
                    quote["total_cents"], fingerprint, encode(quote), "placed"))
        db.execute("UPDATE vendors SET stock=stock-?, version=version+1 WHERE id=? AND sku=?",
                   (goal["quantity"], quote["vendor"], goal["sku"]))
        return {"fact": "ordered", "data": dict(db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())}

    def receive_order(self, db, task, goal, memory):
        order = db.execute("SELECT * FROM orders WHERE task_id=?", (task["id"],)).fetchone()
        if not order:
            raise ToolError("precondition", "There is no committed purchase order.")
        if self.supplier:
            remote = self._lookup(task["id"])
            if not remote or remote["status"] != "fulfilled" or remote["external_order_id"] != order["external_order_id"]:
                raise ToolError("verification", "Supplier has not confirmed fulfillment of the committed order.")
        if not db.execute("SELECT 1 FROM movements WHERE order_id=?", (order["id"],)).fetchone():
            db.execute("INSERT INTO movements VALUES(?,?,?)", (order["id"], order["sku"], order["quantity"]))
            db.execute("UPDATE inventory SET quantity=quantity+? WHERE sku=?", (order["quantity"], order["sku"]))
            db.execute("UPDATE orders SET status='received' WHERE id=?", (order["id"],))
        return {"fact": "received", "data": {"order_id": order["id"], "quantity": order["quantity"]}}

    def write_receipt(self, db, task, goal, memory):
        order = db.execute("SELECT * FROM orders WHERE task_id=?", (task["id"],)).fetchone()
        if not order or order["status"] != "received":
            raise ToolError("precondition", "Order must be received before a receipt is issued.")
        receipt = db.execute("SELECT * FROM receipts WHERE order_id=?", (order["id"],)).fetchone()
        if not receipt:
            receipt_id = "RCPT-" + task["id"][:8].upper()
            body = {"receipt_id": receipt_id, "order_id": order["id"], "task_id": task["id"],
                    "vendor": order["vendor"], "sku": order["sku"], "quantity": order["quantity"],
                    "total_cents": order["total_cents"], "currency": "USD", "sandbox": True,
                    "purchase_fingerprint": order["fingerprint"]}
            if self.fault(db, task, "verification_mismatch"):
                body["quantity"] -= 1
            db.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?)",
                       (receipt_id, order["id"], body["quantity"], body["total_cents"], encode(body), digest(body)))
            receipt = db.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        return {"fact": "receipt", "data": dict(receipt)}

    def verify_outcome(self, db, task, goal, memory):
        # Never infer success from a tool's claimed success or a model's narrative.
        order = db.execute("SELECT * FROM orders WHERE task_id=?", (task["id"],)).fetchone()
        receipt = db.execute("SELECT * FROM receipts WHERE order_id=?", (order["id"],)).fetchone() if order else None
        movement = db.execute("SELECT * FROM movements WHERE order_id=?", (order["id"],)).fetchone() if order else None
        stock = db.execute("SELECT quantity FROM inventory WHERE sku=?", (goal["sku"],)).fetchone()[0]
        body = json.loads(receipt["body"]) if receipt else {}
        quote = json.loads(order["quote"]) if order else {}
        baseline = self.output(memory, "read_inventory")["quantity"]
        policy_at_purchase = self.output(memory, "read_policy")
        approval = db.execute("SELECT id FROM approvals WHERE task_id=? AND fingerprint=? AND status='approved'",
                              (task["id"], order["fingerprint"])).fetchone() if order else None
        checks = {
            "order_matches_goal": bool(order and order["sku"] == goal["sku"] and order["quantity"] == goal["quantity"]),
            "within_budget": bool(order and order["total_cents"] <= goal["budget_cents"]),
            "purchase_matches_quote": bool(order and order["fingerprint"] == digest(quote)
                                            and order["total_cents"] == quote.get("total_cents")),
            "authorization_recorded": bool(order and (order["total_cents"] <= policy_at_purchase["approval_cents"] or approval)),
            "order_received": bool(order and order["status"] == "received"),
            "inventory_movement_matches": bool(movement and movement["sku"] == goal["sku"] and movement["quantity"] == goal["quantity"]),
            "stock_increased": stock >= baseline + goal["quantity"],
            "receipt_matches_order": bool(receipt and body.get("order_id") == order["id"]
                                          and body.get("task_id") == task["id"] and body.get("sku") == goal["sku"]
                                          and body.get("vendor") == order["vendor"]
                                          and body.get("quantity") == order["quantity"] == receipt["quantity"]
                                          and body.get("total_cents") == order["total_cents"] == receipt["total_cents"]
                                          and body.get("purchase_fingerprint") == order["fingerprint"]),
            "receipt_digest_matches": bool(receipt and receipt["sha256"] == digest(body)),
        }
        if self.supplier:
            remote = self._lookup(task["id"])
            checks["independent_supplier_matches"] = bool(remote and order and
                all(remote.get(key) == order[key] for key in ("sku", "quantity", "vendor", "total_cents", "fingerprint", "external_order_id")))
        evidence = {"checks": checks, "order_id": order["id"] if order else None,
                    "receipt_id": receipt["id"] if receipt else None, "stock_before": baseline,
                    "stock_after": stock, "total_cents": order["total_cents"] if order else None,
                    "receipt_sha256": receipt["sha256"] if receipt else None}
        if not all(checks.values()):
            Store.event(db, task["id"], "verification_failed", "Read-back verification found a mismatch.", evidence)
            raise ToolError("verification", "Outcome is not complete: " + ", ".join(k for k, v in checks.items() if not v))
        return {"fact": "verified", "data": evidence}

    def _catalog(self, sku):
        try:
            return self.supplier.catalog(sku)
        except SupplierError as exc:
            raise ToolError("transient", str(exc)) from exc

    def _lookup(self, task_id):
        try:
            return self.supplier.lookup(task_id)
        except SupplierError as exc:
            raise ToolError("uncertain", str(exc)) from exc

    def _remote_purchase(self, task_id, name):
        """Prepare intent -> HTTP outside transaction -> record confirmed effect.

        A previously pending intent ALWAYS reconciles, even if the executor never
        observed its failure. An absent/unknown outcome never triggers a blind POST.
        """
        task = self.store.task(task_id)
        goal, memory = task["intent"], task["memory"]
        key = digest([task_id, name, memory.get("epoch", 0)])
        error = None
        with self.store.transaction() as db:
            cached = db.execute("SELECT result FROM tool_calls WHERE key=?", (key,)).fetchone()
            if cached:
                Store.event(db, task_id, "replay", "Replayed confirmed purchase result without contacting checkout.", {"tool":name})
                return json.loads(cached[0])
            intent = db.execute("SELECT * FROM purchase_intents WHERE task_id=?", (task_id,)).fetchone()
            pending = bool(intent and intent["status"] in {"pending", "confirmed"})
            quote = json.loads(intent["quote"]) if pending else self.output(memory, "choose_quote")
            try:
                if not pending:
                    policy = self.check_quote(db, quote, goal)
                    fingerprint = digest(quote)
                    if self.output(memory, "authorize")["fingerprint"] != fingerprint:
                        raise ToolError("permission", "Authorization does not match this purchase.")
                    if quote["total_cents"] > policy["approval_cents"] and not db.execute(
                        "SELECT 1 FROM approvals WHERE task_id=? AND fingerprint=? AND status='approved'", (task_id, fingerprint)).fetchone():
                        raise ToolError("permission", "Exact purchase requires a matching human approval.")
                    if self.fault(db, task, "supplier_outage"):
                        raise ToolError("supplier", "Supplier declined before commit; select another feasible quote.", quote["vendor"])
                    if self.fault(db, task, "transient"):
                        raise ToolError("transient", "Supplier temporarily unavailable before checkout.")
                    db.execute("INSERT OR REPLACE INTO purchase_intents VALUES(?,?,?,'pending')", (task_id, fingerprint, encode(quote)))
                    Store.event(db, task_id, "purchase_intent", "Persisted exact purchase intent before crossing the HTTP boundary.", {"idempotency_key":task_id,"fingerprint":fingerprint})
            except ToolError as exc:
                error = exc
        if error:
            raise error
        if pending or name == "reconcile_purchase":
            remote = self._lookup(task_id)
            if remote is None:
                raise ToolError("uncertain", "No supplier record is visible for the pending intent. No second purchase will be sent; investigate if reconciliation remains unresolved.")
            operation = "reconcile"
        else:
            try:
                remote = self.supplier.purchase(task_id, quote, task["scenario"] == "timeout_after_commit")
            except SupplierError as exc:
                if exc.code == "transport":
                    raise ToolError("uncertain", "Checkout response lost. The supplier may have committed; query its ledger before another write.") from exc
                with self.store.transaction() as db:
                    db.execute("UPDATE purchase_intents SET status='rejected' WHERE task_id=?", (task_id,))
                code = "verification" if exc.code == "conflict" else (exc.code if exc.code in {"stale","supplier"} else "permission")
                raise ToolError(code, str(exc),
                                quote["vendor"] if exc.code == "supplier" else None) from exc
            operation = "connector"
        expected = {"idempotency_key":task_id, "fingerprint":digest(quote), "sku":goal["sku"],
                    "quantity":goal["quantity"], "vendor":quote["vendor"], "total_cents":quote["total_cents"]}
        if any(remote.get(field) != value for field, value in expected.items()):
            raise ToolError("verification", "Supplier ledger disagrees with the durable authorized intent.")
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO orders(id,task_id,vendor,sku,quantity,total_cents,fingerprint,quote,status,external_order_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       ("PO-"+task_id.replace("-", "").upper(), task_id, quote["vendor"], goal["sku"], goal["quantity"],
                        quote["total_cents"], digest(quote), encode(quote), "placed", remote["external_order_id"]))
            db.execute("UPDATE purchase_intents SET status='confirmed' WHERE task_id=?", (task_id,))
            order = dict(db.execute("SELECT * FROM orders WHERE task_id=?", (task_id,)).fetchone())
            result = {"fact":"ordered", "data":order}
            db.execute("INSERT OR IGNORE INTO tool_calls VALUES(?,?,?,?)", (key, task_id, name, encode(result)))
            Store.event(db, task_id, operation, "Confirmed the purchase in the independent supplier ledger. No second purchase was needed." if pending else "Supplier HTTP API confirmed the committed order.",
                        {"tool":name,"external_order_id":remote["external_order_id"],"idempotency_key":task_id})
        return result

    def reconcile_purchase(self, db, task, goal, memory):
        raise ToolError("precondition", "Reconciliation requires the HTTP supplier connector.")

    def transfer_surplus(self, db, task, goal, memory):
        existing = db.execute("SELECT * FROM transfers WHERE task_id=?", (task["id"],)).fetchone()
        if existing:
            return {"fact":"transferred", "data":dict(existing)}
        if not memory.get("allow_transfer") or db.execute("SELECT 1 FROM purchase_intents WHERE task_id=? AND status IN ('pending','confirmed')", (task["id"],)).fetchone():
            raise ToolError("permission", "Explicit purchase or uncertain checkout cannot become an internal transfer.")
        observed = self.output(memory, "read_inventory")
        source = db.execute("SELECT * FROM surplus WHERE sku=?", (goal["sku"],)).fetchone()
        current = db.execute("SELECT quantity FROM inventory WHERE sku=?", (goal["sku"],)).fetchone()[0]
        if source["version"] != observed["surplus_version"] or source["quantity"] < goal["quantity"]:
            raise ToolError("inventory", "Internal surplus changed; refresh inventory and choose another valid plan.")
        if db.execute("SELECT 1 FROM orders WHERE task_id=?", (task["id"],)).fetchone():
            raise ToolError("permission", "A committed purchase cannot also be fulfilled by transfer.")
        transfer_id = "MOVE-"+task["id"].replace("-", "")
        db.execute("INSERT INTO transfers VALUES(?,?,?,?,?,?)", (transfer_id, task["id"], goal["sku"], goal["quantity"], source["quantity"], current))
        db.execute("UPDATE surplus SET quantity=quantity-?,version=version+1 WHERE sku=?", (goal["quantity"], goal["sku"]))
        db.execute("UPDATE inventory SET quantity=quantity+? WHERE sku=?", (goal["quantity"], goal["sku"]))
        return {"fact":"transferred", "data":dict(db.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone())}

    def write_transfer_receipt(self, db, task, goal, memory):
        transfer = db.execute("SELECT * FROM transfers WHERE task_id=?", (task["id"],)).fetchone()
        if not transfer:
            raise ToolError("precondition", "No committed transfer exists.")
        body = {"receipt_id":"TR-"+task["id"], "transfer_id":transfer["id"], "task_id":task["id"],
                "sku":transfer["sku"], "quantity":transfer["quantity"], "total_cents":0,"route":"internal_transfer","sandbox":True}
        if self.fault(db, task, "verification_mismatch"):
            body["quantity"] -= 1
        db.execute("INSERT OR IGNORE INTO transfer_receipts VALUES(?,?,?)", (task["id"], encode(body), digest(body)))
        return {"fact":"transfer_receipt", "data":body}

    def verify_transfer(self, db, task, goal, memory):
        transfer = db.execute("SELECT * FROM transfers WHERE task_id=?", (task["id"],)).fetchone()
        receipt = db.execute("SELECT * FROM transfer_receipts WHERE task_id=?", (task["id"],)).fetchone()
        stock = db.execute("SELECT quantity FROM inventory WHERE sku=?", (goal["sku"],)).fetchone()[0]
        surplus = db.execute("SELECT quantity FROM surplus WHERE sku=?", (goal["sku"],)).fetchone()[0]
        body = json.loads(receipt["body"]) if receipt else {}
        checks = {"transfer_matches_goal":bool(transfer and transfer["sku"] == goal["sku"] and transfer["quantity"] == goal["quantity"]),
                  "stock_increased":bool(transfer and stock >= transfer["stock_before"]+goal["quantity"]),
                  "source_decreased":bool(transfer and surplus <= transfer["source_before"]-goal["quantity"]),
                  "receipt_matches_transfer":bool(transfer and body.get("transfer_id") == transfer["id"] and body.get("quantity") == goal["quantity"]
                                                  and body.get("sku") == goal["sku"] and body.get("task_id") == task["id"]),
                  "receipt_digest_matches":bool(receipt and digest(body) == receipt["sha256"]),
                  "zero_spend":body.get("total_cents") == 0,
                  "no_external_purchase":not bool(db.execute("SELECT 1 FROM orders WHERE task_id=?", (task["id"],)).fetchone())}
        evidence = {"route":"internal_transfer","transfer_id":transfer["id"] if transfer else None,"receipt_id":body.get("receipt_id"),
                    "total_cents":0,"stock_before":transfer["stock_before"] if transfer else None,"stock_after":stock,
                    "surplus_after":surplus,"checks":checks}
        if not all(checks.values()):
            Store.event(db, task["id"], "verification_failed", "Internal transfer read-back failed.", evidence)
            raise ToolError("verification", "Transfer evidence failed verification.")
        return {"fact":"verified", "data":evidence}
