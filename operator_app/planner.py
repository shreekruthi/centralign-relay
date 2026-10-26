"""Domain-independent uniform-cost planning over symbolic predicates.

The runtime executes ONLY the first action, observes the world, and plans again.
This search is intentionally inspectable and bounded, not an LLM pretending to use tools.
"""

from dataclasses import dataclass, field
from heapq import heappop, heappush
from itertools import count


@dataclass(frozen=True)
class Action:
    tool: str
    requires: frozenset = field(default_factory=frozenset)
    adds: frozenset = field(default_factory=frozenset)
    cost: int = 1
    reason: str = ""


def plan(facts, actions, target="verified", limit=512):
    serial = count()
    queue = [(0, next(serial), frozenset(facts), [])]
    best = {}
    expanded = 0
    while queue and expanded < limit:
        cost, _, state, path = heappop(queue)
        if target in state:
            return path
        if best.get(state, float("inf")) <= cost:
            continue
        best[state] = cost
        expanded += 1
        for action in actions:
            if action.requires <= state and not action.adds <= state:
                heappush(queue, (cost + action.cost, next(serial), state | action.adds, path + [action]))
    return []


def procurement_actions(memory=None):
    def a(tool, requires, adds, reason):
        return Action(tool, frozenset(requires.split()), frozenset(adds.split()), reason=reason)
    actions = [
        a("read_policy", "", "policy", "Load the company's spending and supplier rules."),
        a("read_inventory", "", "inventory", "Establish the stock baseline for this request."),
        a("search_suppliers", "policy inventory", "suppliers", "Discover approved suppliers with sufficient stock."),
        a("choose_quote", "suppliers", "quote", "Choose the least expensive feasible current quote."),
        a("authorize", "quote policy", "authorized", "Check spending authority; pause for a bound human decision if needed."),
        a("place_order", "authorized", "ordered", "Commit one purchase with an idempotency key."),
        a("receive_order", "ordered", "received", "Record sandbox fulfillment and inventory movement."),
        a("write_receipt", "received", "receipt", "Create a persisted receipt and a content digest."),
        a("verify_outcome", "receipt", "verified", "Independently compare goal, order, inventory movement and receipt."),
    ]
    memory = memory or {}
    inventory = memory.get("outputs", {}).get("read_inventory", {})
    if memory.get("allow_transfer") and inventory.get("surplus_quantity", 0) >= memory.get("requested_quantity", 101):
        actions.extend([
            a("transfer_surplus", "policy inventory", "transferred", "Internal surplus can meet the goal without spending; choose the shorter valid plan."),
            a("write_transfer_receipt", "transferred", "transfer_receipt", "Record evidence of internal redistribution."),
            a("verify_transfer", "transfer_receipt", "verified", "Verify both stock locations and the transfer receipt; no purchase should exist."),
        ])
    if "purchase_uncertain" in memory.get("facts", []):
        actions = [action for action in actions if action.tool not in {"place_order", "transfer_surplus"}]
        actions.append(a("reconcile_purchase", "authorized purchase_uncertain", "ordered",
                         "Purchase outcome is uncertain. Query the supplier ledger before considering another write."))
    return actions
