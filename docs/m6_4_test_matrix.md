# M6.4 Test Matrix

The M6.4 regression slice focuses on dynamic attention semantics rather than scheduler or notification behavior.

Expected gates:

1. routine admitted work can produce `KEEP_MOVING` with no human response request;
2. Codexia may dynamically choose `ASK_HUMAN` for a material contextual decision even when no hard rule forces it;
3. every explicit HUMAN attention constraint must be evaluated exactly once;
4. a triggered explicit attention constraint overrides cognitive `KEEP_MOVING`;
5. an uncertain explicit attention constraint fails closed to human attention;
6. an M6.2 `ASK_HUMAN` cannot be suppressed by M6.4 cognition;
7. a routine M6.2 `REVISE` does not automatically interrupt the human;
8. a foreign attention-constraint check cannot be substituted for the exact handoff rule;
9. the exact continuation proposal must be present in the attention evidence basis;
10. context construction and final decision must preserve CODEXIA authorship;
11. a no-human outcome cannot carry non-none urgency or a human response request;
12. context and decision canonical round-trip must preserve exact digests;
13. post-capture context/decision mutation must fail digest validation;
14. attention context from delegated work A cannot be rebound to work B;
15. M6.4 records must not expose execution, merge, process, filesystem, Git, or network authority.
