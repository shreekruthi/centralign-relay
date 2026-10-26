import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from operator_app.runtime import Runtime
from operator_app.server import make_server
from operator_app.store import Store


class HttpTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parent.parent / "data" / "test-tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=temp_root)
        self.runtime = Runtime(Store(Path(self.tmp.name) / "test.sqlite3"))
        self.server = make_server(self.runtime, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.token = self.get("/api/state")["token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.runtime.stop()
        self.tmp.cleanup()

    def get(self, path):
        with urlopen(self.base + path, timeout=5) as response:
            return json.load(response)

    def post(self, path, body, token=None, extra=None):
        headers = {"Content-Type": "application/json", "X-Relay-Token": self.token if token is None else token}
        headers.update(extra or {})
        req = Request(self.base + path, json.dumps(body).encode(), headers)
        with urlopen(req, timeout=5) as response:
            return json.load(response)

    def test_http_end_to_end_approval_and_downloads(self):
        task_id = self.post("/api/tasks", {"goal": "Restock 12 keyboards within $900"})["id"]
        self.runtime.run_until_pause(task_id)
        task = self.get("/api/tasks/" + task_id)
        self.post(f"/api/tasks/{task_id}/decision", {"approval_id": task["approvals"][0]["id"], "approved": True})
        self.runtime.run_until_pause(task_id)
        self.assertEqual(self.get(f"/api/tasks/{task_id}/evidence")["status"], "completed")
        with urlopen(self.base + f"/api/tasks/{task_id}/receipt") as response:
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertEqual(json.load(response)["quantity"], 12)

    def test_missing_token_and_cross_origin_writes_rejected(self):
        for kwargs in [{"token": "bad"}, {"extra": {"Origin": "https://evil.example"}}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(HTTPError) as error:
                self.post("/api/tasks", {"goal": "Buy 3 mice within $150"}, **kwargs)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()

    def test_dns_rebinding_host_rejected(self):
        req = Request(self.base + "/api/state", headers={"Host": "evil.example"})
        with self.assertRaises(HTTPError) as error:
            urlopen(req)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_bad_goal_and_non_boolean_approval_return_400(self):
        with self.assertRaises(HTTPError) as error:
            self.post("/api/tasks", {"goal": "Do everything"})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        task_id = self.post("/api/tasks", {"goal": "Restock 12 keyboards within $900"})["id"]
        task = self.runtime.run_until_pause(task_id)
        with self.assertRaises(HTTPError) as error:
            self.post(f"/api/tasks/{task_id}/decision", {"approval_id": task["approvals"][0]["id"], "approved": "true"})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_static_headers_and_no_path_traversal(self):
        for path in ["/", "/style.css", "/app.js"]:
            with urlopen(self.base + path) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base + "/../operator_app/store.py")
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_unknown_task_returns_404(self):
        with self.assertRaises(HTTPError) as error:
            self.get("/api/tasks/missing")
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_background_worker_completes_without_manual_step(self):
        self.runtime.start()
        task_id = self.post("/api/tasks", {"goal": "Buy 3 mice within $150"})["id"]
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.get("/api/tasks/" + task_id)["status"] == "completed":
                return
            time.sleep(0.05)
        self.fail("Background worker did not complete the task")

    def test_clarification_endpoint_resumes_same_task_and_downloads_transfer_receipt(self):
        task_id = self.post("/api/tasks", {"goal":"Restock mice"})["id"]
        self.assertEqual(self.get("/api/tasks/"+task_id)["status"], "needs_input")
        self.post(f"/api/tasks/{task_id}/clarify", {"quantity":3,"budget_cents":100})
        task = self.runtime.run_until_pause(task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(self.get(f"/api/tasks/{task_id}/receipt")["route"], "internal_transfer")

    def test_clarification_cannot_change_supplied_goal_fields(self):
        task_id = self.post("/api/tasks", {"goal":"Buy 3 mice"})["id"]
        with self.assertRaises(HTTPError) as error:
            self.post(f"/api/tasks/{task_id}/clarify", {"quantity":4,"budget_cents":10000})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
