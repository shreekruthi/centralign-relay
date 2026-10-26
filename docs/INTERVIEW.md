# Technical discussion preparation

Run and understand the system before submitting. You should be able to explain an event, change a policy, and debug a stopped task yourself.

## Two-minute explanation

“I focused on one company outcome: get additional equipment into ready inventory under company rules. The runtime can ask for missing limits, discover surplus and choose an internal transfer, or purchase through a separate supplier API. It executes one tool at a time, observes and replans, and completes only after independent read-back checks pass.”

“The reliability centerpiece is uncertainty across two databases. Relay commits purchase intent before HTTP. The supplier can commit and lose its response. Recovery queries the supplier's idempotency ledger and adopts the existing purchase; it does not blindly retry checkout. If lookup remains inconclusive, the task requires investigation.”

“Authority remains outside the planner. Exact quote fingerprints and live policy checks control writes. Historical verified outcomes break equal-price ties only. The default is a small symbolic agent, not a claim of general-purpose LLM autonomy.”

## Read the code in this order

1. `goals.py`: strict complete intent and bounded partial-goal interpretation.
2. `planner.py`: generic search and alternative domain action paths.
3. `runtime.py`: clarification, lease, decision loop, recovery and human authority.
4. `tools.py`: `_remote_purchase`, `transfer_surplus`, verifiers and retrieved context.
5. `supplier.py`: independent HTTP/transaction boundary, idempotency contract and real response loss.
6. `store.py`: persisted task/company state, constraints, migration and history.
7. `tests/test_standalone.py`: cross-system recovery, alternatives, uncertainty and memory tests.

## Questions to practice

**Why is a repeated POST unsafe?** The previous call can commit while its response is lost. A pending intent and authoritative lookup are required before another write. Server-side idempotency is an additional safety barrier.

**What if lookup returns absent?** Absence is not automatically proof that no write is in flight. This prototype refuses blind resubmission and eventually requires investigation. A real connector needs documented consistency/timeout contracts to recover more aggressively.

**What if the executor dies before persisting its uncertainty?** The purchase intent was already committed. On restart, its pending state forces lookup even when agent memory still proposes `place_order`.

**What if the surplus changes?** The transfer checks live quantity and source version. A conflict invalidates inventory observations; the next plan can purchase instead. Transfer writes and stock updates are atomic and task-unique.

**What does memory change?** Past verified supplier success breaks price ties. It does not change budget, authority, current stock, company policy or the action vocabulary. Provenance is visible in policy observations.

**Why no paid LLM by default?** The evaluation can run without credentials and inspect the actual reliability logic. The tradeoff is a bounded grammar and authored domain. Optional local interpretation does not substitute for a tested model planner.

**What remains unchanged for another task?** The runtime, generic search, persistence and approval mechanisms can be reused. Another supported product/budget needs no new runtime. Another business workflow needs new domain contracts/tools and a verifier; that broader generalization has not been demonstrated.

**Is this production-secure or exactly once?** No. It is a trusted local sandbox with two ledgers, idempotency and conservative reconciliation. Production needs identity/tenant boundaries, audit storage, stronger leases/fencing, vendor contracts, queues and monitoring.

## Practice changes

- Change an approval threshold and prove that stale authority cannot purchase.
- Add a SKU without changing the runtime loop.
- Race two restocking tasks for one surplus pool and explain the selected fallback.
- Make the supplier lookup unavailable after a committed purchase; prove there is no second checkout.
- Add tax to a quote and update fingerprint binding and independent verification.

## Before submitting

Publish the source to your GitHub account, confirm relative README/demo links, record your own continuous walkthrough if possible, and disclose Codex assistance honestly. Include your own name/contact through your application, not fabricated authorship metadata. Be ready to explain and modify the implementation live.
