# Design choices and assumptions

**Outcome ownership.** Procurement provides measurable success, company policy, consequential writes and useful evidence. The scope stays narrow so execution and failure behavior can be demonstrated completely.

**Alternative plans instead of more unrelated features.** Internal redistribution and external purchase accomplish the same stock outcome through different actions. Discovery determines which path is available. Explicit purchase requests preserve the user's intent and do not silently turn into transfers.

**Symbolic planning baseline.** Declared predicates, uniform-cost search and visible observations make decisions reproducible. The limitation is authored domain knowledge and bounded language. The optional Ollama adapter interprets complete goals only. It does not invent workflows, control permissions or produce executable code.

**Separate supplier ledger.** A single database cannot demonstrate the uncertainty of a remote side effect. The supplier API owns its own transaction and is called over actual HTTP. Both services start in one process for beginner-friendly setup, while persistent ledgers remain separate. This deliberately exercises transport and transactional separation without requiring live company credentials.

**Persist intent before network I/O.** A pending intent is enough to require reconciliation even when the executor never observed a timeout. Network checkout happens outside the company write transaction. Unknown outcomes require investigation rather than blind retry. This sacrifices some automatic liveness to prevent duplicate spending.

**Approval at the write boundary.** A human approves a fingerprinted purchase, not a vague “continue.” Live price, policy, availability, canonical intent and task-scoped authority are rechecked before a new write. Supplier fallback needs fresh authority.

**Independent verification.** A successful connector response is insufficient. The final verifier compares company and supplier records. A purchase may exist while the task still needs investigation; that partial success must remain explicit.

**Conservative memory.** Verified prior outcomes can influence price ties but cannot override fresh data or spending rules. Retrieval includes provenance. The system does not pretend to learn policies from unsupervised outcomes.

**No runtime dependencies.** Standard-library Python, SQLite and browser APIs keep setup inspectable. Production serving, identity, tenant isolation, queues, migration management and stronger lease coordination are future work, not requirements hidden behind the prototype.

Assumptions: one trusted local reviewer, three fictional SKUs, whole additional units, USD cents, instant sandbox fulfillment, no tax/shipping, one surplus source, no split fulfillment or cancellation.

References used for component contracts: [Python sqlite3](https://docs.python.org/3/library/sqlite3.html) and [Ollama chat API](https://docs.ollama.com/api/chat). Live Ollama inference was not exercised. The demo video uses actual browser screenshots, assembled with Pillow and FFmpeg via imageio-ffmpeg; these are artifact-generation tools, not app dependencies. Project implementation used OpenAI Codex assistance.
