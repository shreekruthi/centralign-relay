"""Cross-system and alternative-plan integration tests. No live company or model."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from operator_app.runtime import Runtime
from operator_app.store import Store, encode
from operator_app.supplier import SupplierError, SupplierService
from operator_app.tools import ToolError


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent.parent / "data" / "test-tmp"
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(Path(self.tmp.name) / "company.sqlite3")
        self.supplier = SupplierService(Path(self.tmp.name) / "supplier.sqlite3").start()
        self.runtime = Runtime(self.store, self.supplier.client)

    def tearDown(self):
        self.runtime.stop()
        self.supplier.stop()
        self.tmp.cleanup()

    def complete(self, task_id):
        for _ in range(6):
            task = self.runtime.run_until_pause(task_id)
            if task["status"] != "awaiting_approval":
                return task
            approval = next(a for a in task["approvals"] if a["status"] == "pending")
            self.runtime.decide(task_id, approval["id"], True)
        self.fail("Repeated approval loop")

    def until_fact(self, task_id, fact):
        for _ in range(20):
            if fact in self.store.task(task_id)["memory"]["facts"]:
                return
            self.runtime.step(task_id)
        self.fail("Expected fact: " + fact)

    def external_count(self):
        with self.supplier.ledger.db() as db:
            return db.execute("SELECT count(*) FROM purchases").fetchone()[0]

    def test_actual_response_loss_reconciles_without_second_post(self):
        task_id = self.runtime.create("Buy 3 mice within $150", "timeout_after_commit")
        with patch.object(self.supplier.client, "purchase", wraps=self.supplier.client.purchase) as post:
            task = self.complete(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.external_count(), 1)
        self.assertTrue(any(e["kind"] == "reconcile" for e in task["events"]))
        self.assertNotIn("purchase_uncertain", task["memory"]["facts"])
        self.assertTrue(task["memory"]["outputs"]["verify_outcome"]["checks"]["independent_supplier_matches"])

    def test_process_exit_with_pending_remote_intent_recovers_without_post(self):
        task_id = self.runtime.create("Buy 3 mice within $150", "timeout_after_commit")
        self.until_fact(task_id, "authorized")
        script = (
            "import os; from operator_app.store import Store; from operator_app.tools import ToolRegistry; "
            "from operator_app.supplier import SupplierClient; "
            f"tools=ToolRegistry(Store({self.store.path!r}),SupplierClient({self.supplier.client.base!r},{self.supplier.token!r})); "
            "\ntry:\n tools.execute(" + repr(task_id) + ", 'place_order')\nfinally:\n os._exit(23)"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.runtime = Runtime(Store(self.store.path), self.supplier.client)
        with patch.object(self.supplier.client, "purchase", wraps=self.supplier.client.purchase) as post:
            task = self.complete(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(post.call_count, 0)
        self.assertEqual(self.external_count(), 1)

    def test_missing_remote_outcome_never_blindly_reposts(self):
        task_id = self.runtime.create("Buy 3 mice within $150", "timeout_after_commit")
        self.until_fact(task_id, "authorized")
        self.runtime.step(task_id)
        with patch.object(self.supplier.client, "lookup", return_value=None), patch.object(self.supplier.client, "purchase") as post:
            task = self.complete(task_id)
        self.assertEqual(task["status"], "needs_attention")
        post.assert_not_called()
        self.assertEqual(self.external_count(), 1)

    def test_supplier_idempotency_replays_same_contract_and_rejects_changed_contract(self):
        task_id = self.runtime.create("Buy 3 mice within $150")
        self.until_fact(task_id, "authorized")
        quote = self.store.task(task_id)["memory"]["outputs"]["choose_quote"]
        first = self.supplier.client.purchase(task_id, quote)
        self.assertEqual(self.supplier.client.purchase(task_id, quote), first)
        with self.assertRaises(SupplierError) as exc:
            self.supplier.client.purchase(task_id, quote | {"quantity":2})
        self.assertEqual(exc.exception.code, "conflict")
        self.assertEqual(self.external_count(), 1)

    def test_remote_price_change_requires_new_approval(self):
        task_id = self.runtime.create("Buy 12 keyboards within $900")
        task = self.runtime.run_until_pause(task_id)
        self.runtime.decide(task_id, task["approvals"][0]["id"], True)
        with self.supplier.ledger.db() as db:
            db.execute("UPDATE stock SET unit_cents=5500,version=version+1 WHERE id='atlas' AND sku='KEYBOARD'")
        task = self.runtime.run_until_pause(task_id)
        self.assertEqual(task["status"], "awaiting_approval")
        self.assertEqual(task["approvals"][-1]["payload"]["total_cents"], 66000)
        self.assertEqual(self.external_count(), 0)

    def test_supplier_fallback_still_obeys_changed_authority(self):
        task_id = self.runtime.create("Buy 12 keyboards within $900", "supplier_outage")
        task = self.complete(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["memory"]["outputs"]["verify_outcome"]["total_cents"], 72000)
        self.assertEqual(self.external_count(), 1)
        self.assertEqual(len(task["approvals"]), 2)

    def test_separate_database_mismatch_is_detected(self):
        task_id = self.runtime.create("Buy 3 mice within $150")
        self.until_fact(task_id, "receipt")
        with self.supplier.ledger.db() as db:
            body = self.supplier.ledger.lookup(task_id)
            body["quantity"] = 2
            db.execute("UPDATE purchases SET body=? WHERE key=?", (encode(body), task_id))
        task = self.complete(task_id)
        self.assertEqual(task["status"], "needs_attention")
        failure = next(e for e in task["events"] if e["kind"] == "verification_failed")
        self.assertFalse(failure["detail"]["checks"]["independent_supplier_matches"])

    def test_internal_surplus_selects_shorter_zero_spend_plan(self):
        task_id = self.runtime.create("Restock 3 mice within $1")
        task = self.complete(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertIn("verify_transfer", task["memory"]["outputs"])
        self.assertNotIn("choose_quote", task["memory"]["outputs"])
        self.assertEqual(self.external_count(), 0)
        self.assertEqual(task["approvals"], [])
        company = self.store.company()
        self.assertEqual(next(x["quantity"] for x in company["inventory"] if x["sku"] == "MOUSE"), 8)
        self.assertEqual(next(x["quantity"] for x in company["surplus"] if x["sku"] == "MOUSE"), 3)

    def test_explicit_buy_does_not_silently_become_transfer(self):
        task = self.complete(self.runtime.create("Buy 3 mice within $150"))
        self.assertEqual(task["status"], "completed")
        self.assertIn("verify_outcome", task["memory"]["outputs"])
        self.assertEqual(self.external_count(), 1)

    def test_insufficient_surplus_chooses_purchase(self):
        task = self.complete(self.runtime.create("Restock 7 mice within $300"))
        self.assertEqual(task["status"], "completed")
        self.assertEqual(self.external_count(), 1)

    def test_surplus_race_replans_to_purchase(self):
        task_id = self.runtime.create("Restock 3 mice within $150")
        self.until_fact(task_id, "policy")
        with self.store.transaction() as db:
            db.execute("UPDATE surplus SET quantity=0,version=version+1 WHERE sku='MOUSE'")
        task = self.complete(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(self.external_count(), 1)
        self.assertTrue(any(e["kind"] == "adapt" and e["detail"]["code"] == "inventory" for e in task["events"]))

    def test_replayed_transfer_moves_stock_once(self):
        task_id = self.runtime.create("Restock 3 mice within $150")
        self.complete(task_id)
        self.runtime.tools.execute(task_id, "transfer_surplus")
        self.assertEqual(next(x["quantity"] for x in self.store.company()["inventory"] if x["sku"] == "MOUSE"), 8)

    def test_transfer_receipt_mismatch_refuses_completion(self):
        task = self.complete(self.runtime.create("Restock 3 mice within $150", "verification_mismatch"))
        self.assertEqual(task["status"], "needs_attention")
        self.assertEqual(self.external_count(), 0)

    def test_partial_goal_pauses_before_any_tool_and_survives_restart(self):
        task_id = self.runtime.create("Restock mice")
        task = self.runtime.run_until_pause(task_id)
        self.assertEqual(task["status"], "needs_input")
        self.assertFalse(any(e["kind"] == "execute" for e in task["events"]))
        self.runtime = Runtime(Store(self.store.path), self.supplier.client)
        self.runtime.clarify(task_id, {"quantity":3,"budget_cents":100})
        task = self.complete(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["id"], task_id)
        self.assertEqual(self.external_count(), 0)

    def test_only_missing_fields_can_be_supplied(self):
        task_id = self.runtime.create("Buy 3 mice")
        with self.assertRaises(ValueError):
            self.runtime.clarify(task_id, {"quantity":4,"budget_cents":10000})
        self.runtime.clarify(task_id, {"budget_cents":15000})
        self.assertEqual(self.complete(task_id)["status"], "completed")

    def test_invalid_clarification_does_not_resume_task(self):
        task_id = self.runtime.create("Restock mice")
        for answers in [{"quantity":0,"budget_cents":100}, {"quantity":True,"budget_cents":100},
                        {"quantity":2,"budget_cents":-1}]:
            with self.subTest(answers=answers), self.assertRaises(ValueError):
                self.runtime.clarify(task_id, answers)
        self.assertEqual(self.store.task(task_id)["status"], "needs_input")

    def test_verified_history_is_retrieved_and_breaks_price_ties(self):
        first = self.complete(self.runtime.create("Buy 3 mice within $150", "supplier_outage"))
        self.assertEqual(first["memory"]["outputs"]["choose_quote"]["vendor"], "beacon")
        with self.supplier.ledger.db() as db:
            db.execute("UPDATE stock SET unit_cents=3500 WHERE sku='MOUSE' AND id='atlas'")
        second = self.complete(self.runtime.create("Buy 2 mice within $150"))
        self.assertEqual(second["memory"]["outputs"]["choose_quote"]["vendor"], "beacon")
        self.assertEqual(second["memory"]["outputs"]["read_policy"]["prior_outcomes"][0]["task_id"], first["id"])

    def test_connector_only_accepts_loopback_endpoint(self):
        from operator_app.supplier import SupplierClient
        for endpoint in ["https://real-vendor.example", "http://evil.example", "http://127.0.0.1:80/path"]:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                SupplierClient(endpoint, "fake")

    def test_supplier_capability_is_required(self):
        from urllib.error import HTTPError
        from urllib.request import urlopen
        with self.assertRaises(HTTPError) as exc:
            urlopen(self.supplier.client.base + "/catalog?sku=MOUSE")
        self.assertEqual(exc.exception.code, 403)
        exc.exception.close()

    def test_prepared_intent_is_committed_before_http_call(self):
        task_id = self.runtime.create("Buy 3 mice within $150")
        original = self.supplier.client.purchase
        def check_then_purchase(*args):
            with self.store.connect() as db:
                self.assertEqual(db.execute("SELECT status FROM purchase_intents WHERE task_id=?", (task_id,)).fetchone()[0], "pending")
            return original(*args)
        with patch.object(self.supplier.client, "purchase", side_effect=check_then_purchase):
            self.assertEqual(self.complete(task_id)["status"], "completed")

    def test_conflicting_supplier_order_requires_investigation(self):
        task_id = self.runtime.create("Buy 3 mice within $150")
        self.until_fact(task_id, "authorized")
        quote = self.store.task(task_id)["memory"]["outputs"]["choose_quote"]
        self.supplier.client.purchase(task_id, quote | {"quantity":2,"total_cents":5000})
        task = self.complete(task_id)
        self.assertEqual(task["status"], "needs_attention")
        self.assertEqual(self.external_count(), 1)
        self.assertEqual(self.store.company()["orders"], [])


if __name__ == "__main__":
    unittest.main()
