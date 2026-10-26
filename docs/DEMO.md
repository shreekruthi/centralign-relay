# Five-minute live demonstration script

Start fresh without deleting data:

```sh
python run.py --db data/my-recording.sqlite3 serve
```

Open http://127.0.0.1:8765. Use your screen recorder to capture the uninterrupted interaction. The supplied `demo.mp4` is a captioned assembly of real screenshots, not an uninterrupted recording. Use this script to record your own presentation and demonstrate understanding.

## 0:00–1:15 — Clarify and choose a plan

Say: “Relay owns an inventory outcome. Its first decision is whether it has enough context to act.”

Enter **Restock mice** and choose **Normal operation**. Start. Show **Needs input**, with no company tool execution. Enter **3** units and a **$1** budget, then continue the same task.

Show the verified result: ready stock 5→8, source surplus 6→3, zero spend and no supplier order. Expand the plan selecting `transfer_surplus`.

Say: “The discovered company state enabled an internal fulfillment path. The operator chose it instead of following a fixed purchase pipeline. ‘Buy’ would explicitly require purchasing.”

## 1:15–3:00 — A real uncertain write

Enter **Restock 12 keyboards within $900**. Choose **Purchase commits → response is lost**. Start and show the **$600 Atlas** approval card. Approve it.

Expand **Purchase intent**, **Adapt**, and **Reconcile** events. Show the stable task UUID and external supplier order ID.

Say: “The supplier owns a separate database. It committed the purchase and stock decrement, then actually closed its HTTP socket. Relay cannot safely assume the write failed. It persisted intent before the call, disabled another checkout and queried the supplier ledger. There is one external order and one checkout call in the integration test.”

Show completion with **10 checks**, including `independent_supplier_matches`. Download the receipt and evidence.

## 3:00–4:00 — Refuse a false success

Enter **Buy 2 monitors within $500** and select **Wrong receipt → refuse completion**. Start; this purchase is below the approval threshold. Show **Needs attention** and expand **Verification failed**.

Say: “A purchase can exist while the requested outcome is not verified. The system preserves partial success and does not buy again to repair a receipt.”

## 4:00–5:00 — Explain reliability and limits

Show the test command and architecture diagram.

Say: “The suite tests response loss, an actual separate process exiting with a pending remote purchase, supplier idempotency conflicts, unknown outcomes, quote changes, internal stock races, clarification, memory retrieval and verification failures. No real company account or credential is used.”

“This is an authored symbolic domain. Optional local-model interpretation is available but live inference was not tested. I would next add an authenticated vendor sandbox connector, reviewer identity, lease renewal/fencing and model-planner evaluations. It is a local prototype, not a production service.”

Optional extra: demonstrate **Supplier fails** and explain why Atlas's $600 approval cannot authorize Beacon's $720 replacement. Use a fresh database if exact starting stock matters.
