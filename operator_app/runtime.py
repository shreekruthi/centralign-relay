"""Durable receding-horizon executor, human checkpoints and bounded recovery."""

import json
import threading
import time
import uuid

from .goals import SCENARIOS, parse_partial, validate_goal
from .planner import plan, procurement_actions
from .store import Store, encode
from .tools import ToolError, ToolRegistry, digest

TERMINAL = {"completed", "blocked", "rejected", "needs_attention", "failed"}
ACTIVE = {"queued", "running", "recovering"}


class Runtime:
    def __init__(self, store, supplier=None):
        self.store, self.tools = store, ToolRegistry(store, supplier)
        self.stop_event = threading.Event()
        self.worker = None

    def create(self, text, scenario="normal", intent=None, interpreter="offline grammar"):
        if scenario not in SCENARIOS:
            raise ValueError("Unknown sandbox scenario.")
        goal = validate_goal(intent) if intent is not None else parse_partial(text)
        if not isinstance(text, str) or not 1 <= len(text) <= 500:
            raise ValueError("Goal must be 1–500 characters.")
        task_id, now = str(uuid.uuid4()), time.time()
        missing = [field for field in ("quantity", "budget_cents") if field not in goal]
        memory = {"facts": [], "outputs": {}, "epoch": 0, "excluded": [], "retries": {},
                  "allow_transfer": text.strip().lower().startswith("restock"),
                  "requested_quantity": goal.get("quantity"), "missing": missing}
        with self.store.transaction() as db:
            db.execute("INSERT INTO tasks(id,goal,intent,status,memory,scenario,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                       (task_id, text, encode(goal), "needs_input" if missing else "queued", encode(memory), scenario, now, now))
            if scenario != "normal":
                db.execute("INSERT INTO faults VALUES(?,?,?)", (task_id, scenario, 2 if scenario == "transient" else 1))
            Store.event(db, task_id, "understand", "Goal translated to an explicit outcome contract.",
                        {"intent": goal, "interpreter": interpreter, "scenario": scenario})
            if missing:
                Store.event(db, task_id, "clarification_required", "Supply the missing quantity and/or USD budget before any work begins.", {"missing":missing})
        return task_id

    def clarify(self, task_id, answers):
        if not isinstance(answers, dict) or not answers:
            raise ValueError("Provide the requested missing fields.")
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError("Task not found")
            memory, intent = json.loads(row["memory"]), json.loads(row["intent"])
            if row["status"] != "needs_input" or set(answers) != set(memory["missing"]):
                raise ValueError("Answer exactly the currently missing fields; existing intent cannot be changed here.")
            intent = validate_goal({**intent, **answers})
            memory["missing"], memory["requested_quantity"] = [], intent["quantity"]
            db.execute("UPDATE tasks SET intent=?,memory=?,status='queued',updated=? WHERE id=?",
                       (encode(intent), encode(memory), time.time(), task_id))
            Store.event(db, task_id, "human_input", "Clarification recorded; resume the same task.", {"answers":answers, "intent":intent})
        return {"id":task_id,"status":"queued"}

    def _claim(self, task_id):
        token, now = str(uuid.uuid4()), time.time()
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError("Task not found")
            if row["status"] not in ACTIVE or row["retry_at"] > now or row["lease_until"] > now:
                return None
            if row["steps"] >= 80:
                db.execute("UPDATE tasks SET status='blocked',updated=? WHERE id=?", (now, task_id))
                Store.event(db, task_id, "blocked", "Execution budget exhausted after 80 decisions.")
                return None
            db.execute("UPDATE tasks SET lease_token=?,lease_until=?,status='running',steps=steps+1,updated=? WHERE id=?",
                       (token, now + 30, now, task_id))
        return token

    def step(self, task_id):
        token = self._claim(task_id)
        if not token:
            return False
        try:
            task = self.store.task(task_id)
            memory = task["memory"]
            # Observe uncertain context first; only then choose among fulfillment plans.
            target = "inventory" if "inventory" not in memory["facts"] else ("policy" if "policy" not in memory["facts"] else "verified")
            path = plan(memory["facts"], procurement_actions(memory), target=target)
            if not path:
                self._finish(task_id, token, "failed", "No valid plan exists for the current predicates.")
                return True
            action = path[0]
            with self.store.transaction() as db:
                Store.event(db, task_id, "plan", action.reason,
                            {"next_tool": action.tool, "remaining_plan": [a.tool for a in path],
                             "facts": memory["facts"], "epoch": memory["epoch"], "horizon_target":target})
            try:
                result = self.tools.execute(task_id, action.tool)
            except ToolError as error:
                self._recover(task_id, token, action.tool, memory, error)
                return True
            # A deliberately injected process-interruption boundary: the tool committed,
            # but the executor has not persisted its observation. The next step replays.
            with self.store.transaction() as db:
                if action.tool == "place_order" and self.tools.fault(db, task, "crash_after_order"):
                    db.execute("UPDATE tasks SET status='recovering',updated=? WHERE id=? AND lease_token=?",
                               (time.time(), task_id, token))
                    Store.event(db, task_id, "interrupted", "Injected interruption after purchase commit, before observation. Resume safely.")
                    return True
            if "waiting" in result:
                with self.store.transaction() as db:
                    db.execute("UPDATE tasks SET status='awaiting_approval',updated=? WHERE id=? AND lease_token=?",
                               (time.time(), task_id, token))
                    Store.event(db, task_id, "approval_required", "Human approval required for this exact vendor, price and quantity.",
                                {"approval_id": result["waiting"], "quote": result["data"]})
                return True
            memory["facts"] = sorted(set(memory["facts"]) | {result["fact"]})
            if result["fact"] == "ordered" and "purchase_uncertain" in memory["facts"]:
                memory["facts"].remove("purchase_uncertain")
            memory["outputs"][action.tool] = result["data"]
            memory["retries"].pop(action.tool, None)
            completed = result["fact"] == "verified"
            with self.store.transaction() as db:
                db.execute("UPDATE tasks SET memory=?,status=?,retry_at=0,updated=? WHERE id=? AND lease_token=?",
                           (encode(memory), "completed" if completed else "queued", time.time(), task_id, token))
                Store.event(db, task_id, "complete" if completed else "observe",
                            "All outcome checks passed; task completed." if completed else f"Observed {result['fact']}; replan from the new state.",
                            result["data"])
                if completed:
                    summary = {"sku": task["intent"]["sku"], "quantity": task["intent"]["quantity"],
                               "vendor": memory["outputs"].get("choose_quote", {}).get("vendor", "internal"),
                               "excluded_suppliers": memory["excluded"], "evidence": result["data"]}
                    db.execute("INSERT OR REPLACE INTO memories VALUES(?,?,?)", (task_id, encode(summary), time.time()))
            return True
        except Exception as exc:
            self._finish(task_id, token, "failed", f"Unexpected {type(exc).__name__}: {exc}")
            return True
        finally:
            with self.store.transaction() as db:
                db.execute("UPDATE tasks SET lease_until=0,lease_token=NULL WHERE id=? AND lease_token=?", (task_id, token))

    def _finish(self, task_id, token, status, message):
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status=?,updated=? WHERE id=? AND lease_token=?",
                       (status, time.time(), task_id, token))
            Store.event(db, task_id, status, message)

    def _recover(self, task_id, token, tool, memory, error):
        status, retry_at = "queued", 0
        if error.code in {"supplier", "stale", "inventory"}:
            if error.vendor:
                memory["excluded"].append(error.vendor)
            invalid = {"policy", "suppliers", "quote", "authorized"}
            if error.code == "inventory":
                invalid.add("inventory")
                memory["outputs"].pop("read_inventory", None)
            memory["facts"] = [f for f in memory["facts"] if f not in invalid]
            for key in ["read_policy", "search_suppliers", "choose_quote", "authorize"]:
                memory["outputs"].pop(key, None)
            memory["epoch"] += 1
        elif error.code in {"transient", "uncertain"}:
            if error.code == "uncertain":
                memory["facts"] = sorted(set(memory["facts"]) | {"purchase_uncertain"})
            count = memory["retries"].get(tool, 0) + 1
            memory["retries"][tool] = count
            if count >= 3:
                status = "needs_attention" if error.code == "uncertain" else "blocked"
            else:
                retry_at = time.time() + 0.25 * (2 ** (count - 1))
        else:
            status = {"verification": "needs_attention", "rejected": "rejected"}.get(error.code, "blocked")
        with self.store.transaction() as db:
            if error.code in {"supplier", "stale", "inventory"}:
                db.execute("UPDATE approvals SET status='invalidated' WHERE task_id=? AND status IN ('pending','approved')", (task_id,))
            db.execute("UPDATE tasks SET memory=?,status=?,retry_at=?,updated=? WHERE id=? AND lease_token=?",
                       (encode(memory), status, retry_at, time.time(), task_id, token))
            Store.event(db, task_id, "adapt" if status == "queued" else status, str(error),
                        {"code": error.code, "tool": tool, "retry_at": retry_at,
                         "epoch": memory["epoch"], "excluded": memory["excluded"]})

    def decide(self, task_id, approval_id, approved, reason=""):
        if type(approved) is not bool or not isinstance(reason, str) or len(reason) > 500:
            raise ValueError("A boolean decision and a reason of at most 500 characters are required.")
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=? AND task_id=?", (approval_id, task_id)).fetchone()
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or not task:
                raise KeyError("Approval not found")
            if row["status"] != "pending" or task["status"] != "awaiting_approval":
                raise ValueError("This approval is no longer pending.")
            memory, goal = json.loads(task["memory"]), json.loads(task["intent"])
            if digest(memory["outputs"].get("choose_quote", {})) != row["fingerprint"]:
                raise ValueError("Approval does not match the task's current purchase.")
            stale = False
            try:
                self.tools.check_quote(db, json.loads(row["payload"]), goal)
            except ToolError:
                stale = True
            decision = "invalidated" if stale else ("approved" if approved else "rejected")
            db.execute("UPDATE approvals SET status=?,decided_at=?,reason=? WHERE id=?",
                       (decision, time.time(), reason, approval_id))
            db.execute("UPDATE tasks SET status=?,retry_at=0,updated=? WHERE id=?",
                       ("queued" if approved or stale else "rejected", time.time(), task_id))
            Store.event(db, task_id, "human_decision", "Quote changed; obtain a new approval." if stale else f"Human {decision} purchase.",
                        {"approval_id": approval_id, "decision": decision, "reason": reason})
        return {"decision": decision}

    def run_until_pause(self, task_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.store.task(task_id)["status"] not in ACTIVE:
                break
            if not self.step(task_id):
                time.sleep(0.02)
        return self.store.task(task_id)

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.stop_event.clear()
        def work():
            while not self.stop_event.is_set():
                for task in self.store.tasks():
                    if self.stop_event.is_set():
                        break
                    if task["status"] in ACTIVE:
                        self.step(task["id"])
                self.stop_event.wait(0.12)
        self.worker = threading.Thread(target=work, daemon=True, name="relay-worker")
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=5)
