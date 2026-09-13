# M6.5 Test Matrix

The M6.5 regression slice targets durable work continuity, exact worker-evidence flow, bounded background progression, and no-replay behavior rather than UI or local-machine execution.

Expected gates:

1. registration and fresh-process recovery preserve the exact handoff, interpretation and chat cursor;
2. `ADMIT + KEEP_MOVING` becomes a durable `PREPARED` continuation dispatch without sending to the provider;
3. `REVISE + KEEP_MOVING` becomes a durable `PREPARED` worker-revision dispatch carrying the exact M6.2 revision request;
4. `REVISE + ASK_HUMAN` remains `WAITING_HUMAN` with no dispatch;
5. claiming either dispatch durably enters `IN_FLIGHT` before any provider side effect;
6. one live dispatch lease can drive exactly one peer send and then returns the work to `READY` at the new cursor;
7. a restarted supervisor cannot reconstruct or reuse the ephemeral dispatch lease;
8. a restarted supervisor cannot claim the same `IN_FLIGHT` dispatch again;
9. exact post-crash continuation history can be reconciled without a second send;
10. exact post-crash worker-revision history can be reconciled without a second send;
11. no visible post-crash provider effect remains `IN_FLIGHT` and does not manufacture retry permission;
12. M6.4 `ASK_HUMAN` state becomes durable `WAITING_HUMAN` with no pending dispatch;
13. a fresh live M6.3 external-human observation resumes `WAITING_HUMAN` work;
14. external human activity invalidates an unclaimed `PREPARED` continuation or revision;
15. a structurally valid but synthetic external-human observation cannot advance supervisor state;
16. an unbound live observation cannot arbitrarily choose between two active works at the same chat cursor;
17. event payload tamper fails recovery;
18. strict continuation and revision dispatch decoding rejects authority-shaped extra fields;
19. WORKER output cannot directly declare supervisor completion;
20. multiple delegated works recover independently from the same durable supervisor database;
21. one bounded driver call can execute multiple admitted worker turns without a human `continue` between them;
22. one bounded driver call can perform `REVISE → worker revision → exact revised worker turn → ADMIT → continuation` without a human scheduling turn;
23. the revision peer turn preserves CODEXIA semantic authorship over transport `user` and WORKER authorship over transport `assistant`;
24. after each durable peer turn, the checkpoint source receives the exact terminal persisted `ChatPeerTurn`, and can derive the next proposal with `followup_proposal()`;
25. the same exact worker evidence remains available after fresh-process crash recovery;
26. the driver synchronizes work-bound live external activity before claiming a `PREPARED` dispatch, so ordinary human intervention wins before provider send;
27. the driver stops at durable `WAITING_HUMAN` rather than converting an attention request into background progress;
28. repeated worker-side revision remains bounded by an explicit driver step budget and cannot loop indefinitely;
29. every checkpoint returned by the cognition source is revalidated by the existing M6.2/M6.4 supervisor boundary before it can prepare a dispatch.
