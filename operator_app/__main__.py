"""One-command composition root: supplier HTTP API + operator + review UI."""
import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from .runtime import Runtime
from .server import make_server
from .store import Store
from .supplier import SupplierService


def main():
    parser = argparse.ArgumentParser(description="Relay — standalone company operator")
    parser.add_argument("--db", default="data/relay.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Start both local services and the UI")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--ollama-model")
    demo = sub.add_parser("demo")
    demo.add_argument("--goal", default="Restock 12 keyboards within $900")
    demo.add_argument("--scenario", default="timeout_after_commit")
    demo.add_argument("--approve", action="store_true", help="Approve fictional demo purchases")
    demo.add_argument("--export")
    for name in ["show", "resume"]:
        command = sub.add_parser(name)
        command.add_argument("task_id")
    decision = sub.add_parser("decide")
    decision.add_argument("task_id")
    decision.add_argument("approval_id")
    decision.add_argument("decision", choices=["approve", "reject"])
    clarify = sub.add_parser("clarify")
    clarify.add_argument("task_id")
    clarify.add_argument("--quantity", type=int)
    clarify.add_argument("--budget", help="USD, for example 900.00")
    args = parser.parse_args()
    store = Store(args.db)
    supplier = SupplierService(str(Path(store.path).with_suffix(".supplier.sqlite3"))).start()
    runtime = Runtime(store, supplier.client)
    try:
        if args.command == "serve":
            server = make_server(runtime, args.port, args.ollama_model)
            runtime.start()
            print(f"Relay: http://127.0.0.1:{server.server_port}\nSupplier API: {supplier.client.base}\nBoth services are local. No real purchases or credentials. Ctrl+C to stop.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        elif args.command == "demo":
            task_id = runtime.create(args.goal, args.scenario)
            for _ in range(10):
                task = runtime.run_until_pause(task_id)
                if task["status"] != "awaiting_approval" or not args.approve:
                    break
                pending = next(a for a in task["approvals"] if a["status"] == "pending")
                runtime.decide(task_id, pending["id"], True, "Explicit CLI --approve for fictional demo")
            for event in task["events"]:
                print(f"{event['seq']:03} {event['kind']:20} {event['message']}")
            print(f"\nTask: {task_id}\nStatus: {task['status']}")
            if args.export:
                destination = Path(args.export)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(json.dumps(task, indent=2), encoding="utf-8")
            if task["status"] not in {"completed", "awaiting_approval", "needs_input"}:
                raise SystemExit(1)
        elif args.command == "decide":
            print(json.dumps(runtime.decide(args.task_id, args.approval_id, args.decision == "approve"), indent=2))
            print(json.dumps(runtime.run_until_pause(args.task_id), indent=2))
        elif args.command == "clarify":
            answers = {}
            if args.quantity is not None:
                answers["quantity"] = args.quantity
            if args.budget is not None:
                try:
                    amount = Decimal(args.budget) * 100
                    if not amount.is_finite() or amount != amount.to_integral_value():
                        raise ValueError("Budget needs at most two decimal places.")
                    answers["budget_cents"] = int(amount)
                except InvalidOperation as exc:
                    raise ValueError("Invalid budget.") from exc
            runtime.clarify(args.task_id, answers)
            print(json.dumps(runtime.run_until_pause(args.task_id), indent=2))
        else:
            task = runtime.run_until_pause(args.task_id) if args.command == "resume" else store.task(args.task_id)
            print(json.dumps(task, indent=2))
    finally:
        runtime.stop()
        supplier.stop()


if __name__ == "__main__":
    main()
