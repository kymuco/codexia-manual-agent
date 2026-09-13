# M6.5 Explicit Non-Claims

M6.5 proves durable delegated-work supervision and a no-replay provider-dispatch boundary. It does not claim:

- a general autonomous-agent mode;
- time-of-day or "night mode" scheduling semantics;
- that `READY`, `PREPARED`, or `KEEP_MOVING` grants execution authority;
- local process, filesystem, Git mutation, network, merge, or arbitrary tool authority;
- notification delivery, companion UI, mobile surfaces, or user-attention profiles;
- automatic retry of an ambiguous provider side effect after process failure;
- that an `IN_FLIGHT` dispatch with no visible provider delta is safe to resend;
- that worker output is sufficient to declare delegated work complete;
- perfect semantic interpretation of human intent or completion quality;
- a general distributed queue, cluster scheduler, or multi-host lease protocol;
- a transport-independent worker bus: the first proven background dispatch/reconciliation adapter is the existing M6.3 ChatGPT peer loop;
- durable reinterpretation of new human evidence beyond the existing M6.1 interpretation contract;
- the M6.6 daily-use product proof.

The supervisor is deliberately conservative around ambiguous effects: rare uncertainty may stall until exact reconciliation rather than risking duplicate external action. Live human/worker evidence is accepted only through the M6.3 capture boundary (or exact internal crash reconciliation), not merely because a caller can construct a structurally valid record.
