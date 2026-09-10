# M6.3 Test Matrix

The M6.3 regression slice is intentionally centered on provenance and stale-state behavior rather than autonomous scheduling.

Expected gates:

1. attaching to an existing conversation creates an exact cursor without retroactively attributing historical messages;
2. a new manual user/assistant pair after attachment is observed as HUMAN/WORKER semantic delta;
3. an exact M6.2 ADMIT can produce one Codexia-authored user-role transport message and one captured WORKER response;
4. the captured WORKER response can become a new M6.2 proposal candidate bound to the new chat checkpoint;
5. human activity after admission makes the old continuation stale before send;
6. current-branch rewrite/edit breaks prefix binding and fails closed;
7. REJECT/REVISE/ASK_HUMAN admissions cannot enter the peer send path;
8. an admission from a different chat checkpoint cannot be reused;
9. concurrent account-side activity during the Codexia send fails exact delta reconciliation;
10. provider response text/message identity must match the post-send current-branch observation;
11. the pinned web adapter contract must expose current-branch history reads on both Ubuntu and Windows.
