# M6.5 Test Matrix

The M6.5 regression slice targets durable work continuity and no-replay behavior rather than UI or local-machine execution.

Expected gates:

1. registration and fresh-process recovery preserve the exact handoff, interpretation and chat cursor;
2. `ADMIT + KEEP_MOVING` becomes `PREPARED` without sending to the provider;
3. claiming a dispatch durably enters `IN_FLIGHT` before any provider side effect;
4. one live dispatch lease can drive exactly one peer send and then returns the work to `READY` at the new cursor;
5. a restarted supervisor cannot reconstruct or reuse the ephemeral dispatch lease;
6. a restarted supervisor cannot claim the same `IN_FLIGHT` dispatch again;
7. exact post-crash Codexia/assistant history can be reconciled without a second send;
8. no visible post-crash provider effect remains `IN_FLIGHT` and does not manufacture retry permission;
9. M6.4 `ASK_HUMAN` state becomes durable `WAITING_HUMAN` with no pending dispatch;
10. an exact external human observation resumes `WAITING_HUMAN` work;
11. external human activity invalidates an unclaimed `PREPARED` continuation;
12. event payload tamper fails recovery;
13. strict supervisor-dispatch decoding rejects authority-shaped extra fields;
14. WORKER output cannot directly declare supervisor completion;
15. multiple delegated works recover independently from the same durable supervisor database.
