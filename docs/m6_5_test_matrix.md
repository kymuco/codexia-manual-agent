# M6.5 Test Matrix

The M6.5 regression slice targets durable work continuity, bounded background progression, and no-replay behavior rather than UI or local-machine execution.

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
10. a fresh live M6.3 external-human observation resumes `WAITING_HUMAN` work;
11. external human activity invalidates an unclaimed `PREPARED` continuation;
12. a structurally valid but synthetic external-human observation cannot advance supervisor state;
13. an unbound live observation cannot arbitrarily choose between two active works at the same chat cursor;
14. event payload tamper fails recovery;
15. strict supervisor-dispatch decoding rejects authority-shaped extra fields;
16. WORKER output cannot directly declare supervisor completion;
17. multiple delegated works recover independently from the same durable supervisor database;
18. one bounded driver call can execute multiple admitted worker turns without a human `continue` between them;
19. the driver synchronizes live external activity before claiming a `PREPARED` dispatch, so ordinary human intervention wins before provider send;
20. the driver stops at durable `WAITING_HUMAN` rather than converting an attention request into background progress;
21. the driver can reconcile an already-visible `IN_FLIGHT` peer turn after restart without a second provider send;
22. repeated `REVISE`/replanning remains bounded by an explicit driver step budget and cannot loop indefinitely;
23. the checkpoint source remains cognition-only input: every returned proposal/admission/attention tuple is revalidated by the existing M6.2/M6.4 supervisor boundary before it can prepare a dispatch.
