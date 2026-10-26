# Version 2 validation report

Validated on **26 October 2026**, Windows, **Python 3.14.4**.

**62 tests passed; 0 failures, 0 errors, 0 skipped.** Final run: **69.651 seconds**. This is functional validation, not a throughput benchmark.

- [Exact test output](test-output.txt)
- [Machine-readable result](test-results.json)

Reproduce from the repository root:

```sh
python -m unittest discover -s tests -v
```

## Automated coverage

| Area | Exercised behavior |
|---|---|
| Actual supplier HTTP boundary | Separate SQLite ledger, persisted intent before network I/O, real connection close after supplier commit, independent lookup and order adoption |
| Uncertain outcome | One checkout call and one external order after response loss; missing lookup never blindly reposts; bounded uncertainty requires investigation |
| Process recovery | A separate process exits after remote purchase commit before runtime observation; a new runtime recovers by lookup with zero additional checkout calls |
| Supplier contract | Same idempotency key and contract replays; changed contract conflicts; request capability required; loopback-only endpoint |
| Goal and clarification | Strict complete contract, missing quantity/budget, no tool before answer, restart persistence, invalid answers, preventing edits to supplied fields |
| Alternative plans | Internal surplus yields a shorter zero-spend path; explicit Buy still purchases; insufficient surplus purchases; a source race invalidates context and changes the plan |
| Internal effects | Atomic transfer, both inventory locations checked, duplicate transfer prevention and transfer receipt mismatch refusal |
| Company memory | Verified past outcomes retrieved with task provenance; supplier successes break equal-price ties |
| Authority | Genuine approval pause, rejection, price/policy drift, expiry, cross-task misuse, tampered authority and replacement supplier approval |
| Reliability | Durable retries/backoff, retry/step budgets, restart, concurrency and unique order/movement constraints |
| Verification | Independent supplier mismatch, wrong receipt, altered ready stock, reproducible receipt digest, no false completion |
| UI HTTP API | End-to-end approval, receipt/evidence downloads, background completion, clarification and internal receipt download |
| Local web boundaries | Request token, Origin and Host checks, static-file allowlist, invalid payload/decision and security headers |
| Optional model | Fake-transport structured interpretation contract and malformed response rejection; no live model inference |

The original local reference-connector tests remain, with additional real supplier HTTP integration tests. Python source compilation and JavaScript syntax checks passed. GitHub Actions is configured for Python 3.11/3.14 on Windows, Ubuntu and macOS; **remote CI and other local interpreter/OS combinations were not run during this delivery**.

## Browser demonstration

Used actual controls against the standalone app:

1. Submitted **Restock mice**, answered 3 units and $1 after `needs_input`, and observed the same task complete by internal transfer: ready stock 5→8, surplus 6→3, $0 spend, no supplier purchase.
2. Submitted **Restock 12 keyboards within $900** with response loss. Approved the exact $600 quote, observed reconciliation, and inspected all 10 successful purchase checks including the independent supplier record.
3. Captured the goal, clarification, approval, transfer, verified purchase and reconciliation events. See [screenshots](screenshots/), [HTTP task trace](sample-evidence.json), [transfer trace](transfer-evidence.json) and [separate-ledger proof](supplier-proof.json).

The captioned demo video assembles actual browser screenshots. It does not claim to be a continuous recording or latency benchmark. The continuous presentation script is supplied for recording your own walkthrough.

## Limits

No real vendor/account, real payment, live model, browser-automation connector, penetration test, load/soak test, full accessibility audit or physical fulfillment was exercised. Local prototype checks do not establish production security or global exactly-once execution.
