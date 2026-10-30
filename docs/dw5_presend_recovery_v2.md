# DW5: Second verified Sentinel pre-send reconciliation (bounded v2)

DW5 Recovery v2 supports exactly one **second** attested pre-send failure per
project and Controller predecessor. Recovery v1 remains unchanged and keeps its
original one-time limit and durable archive.

The active Controller turn must be in send_admitted with no Controller response
message ID. The immediately previous turn must be ready and point to the same
Controller predecessor. A matching first recovery audit record must exist in
dw5_controller_presend_recovery_v1: source Worker ID/SHA, Controller SHA, error
kind, original phase and original reconciliation digest must agree.

Inspect performs canonical latest Worker/Controller message readbacks and
verifies both exact message IDs and SHA-256 values. It writes nothing, including
no schema migration. Reconciliation requires the human to attest that the
specific SECOND attempt ended with browserless Sentinel prepare rejected
**before** a conversation submission. No timeout, uncertain send, finalize
error or post-submit readback error is eligible. The user must enter
RECONCILE-SECOND plus the exact first 12 characters of the new digest.

After confirmation, canonical readbacks and the immutable audit are checked
again. A single SQLite transaction writes a second immutable audit record in
dw5_controller_presend_recovery_v2, retires exactly the current active fence,
and verifies the previous ready Controller turn. Failures roll back.

The second audit has a unique key (project, predecessor_message_id), so any
THIRD same-predecessor failure is deliberately blocked. Neither reconciliation
nor inspection sends a model message, executes commands or changes Gen2 Work.

## Windows usage

From W:\dev\codexia-daily, with PYTHONPATH including the Codexia source and
W:\dev\chatgpt-web-adapter-codexia-main\src, use the same arguments as Recovery
v1, but with the module tools.dw_codexia_presend_recovery_v2.

First run the action inspect; check the returned project, message IDs, hashes,
prior v1 digest and the second reconciliation digest. Only after explicitly
verifying the recorded pre-send error, run reconcile-presend with
--error-kind sentinel_prepare_rejected and type the requested
RECONCILE-SECOND confirmation. Do not combine recovery and another model send.

Fixture regression:
python -m pytest -q tests/test_dw5_presend_recovery_v1.py tests/test_dw5_presend_recovery_v2.py

## Design limitation

The original DW5 send admission table does not assign a unique persistent
attempt ID. Matching Worker and Controller hashes are not cryptographic proof
that two external send attempts differed. This narrow v2 therefore requires
human attestation and caps recovery at two; a general-purpose design would
require a distinct attempt identity recorded **before** each send.
