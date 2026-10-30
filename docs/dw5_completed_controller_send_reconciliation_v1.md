# DW5: completed Controller send observed after transport timeout

## Observed 2026-10-30

The DW6 governed --interpret invocation was explicitly human-authorized for one send.
A streaming POST timed out after ~60s having received 21,009 bytes.
The transport outcome was ambiguous, so the durable Controller row remained
send_admitted without a Controller message ID. NEVER send again for this predecessor.

A separate canonical GET established the existing Controller conversation contains, in order:

- prior assistant: 938a6213-2a6a-4657-b886-3fee5aba2bea
- new user request: 30bc9d10-6ea9-47b1-a4d1-f13b6a6d86e4
  referencing Worker a57787c5-91b3-4fa8-814d-c81fda54a8e4
- new completed assistant: 18fcb124-bec1-473f-b79b-5a7fdcdbf278
  with finish_reason=stop and observed SHA-256 prefix cd922c88e95dc80b

This is not a third presend recovery. The server accepted and completed a Controller turn.

## Operator scope

tools.dw5_observed_controller_send_reconcile_v1 has two actions:

- inspect: SQLite opened read-only; canonical GET of Worker and latest three Controller
  messages; no network POST, model send, shell or Gen2 Work effect.
- reconcile: repeat identical inspection around explicit human approval; in one
  SQLite transaction archive exact request/response IDs and SHA-256 digests,
  CommandPlan digest and links to unchanged v1/v2 presend archives; advance
  exact active Controller row send_admitted -> controller_known -> ready.
  Roll back everything if any consistency check fails.

The operator requires a completed assistant with finish_reason=stop, exact
predecessor/request/response IDs, exact reconstructed Controller user prompt
bound to the Worker source, and strict ControllerCommandDecision.from_messages
validation. It rejects incomplete, different, ambiguous or duplicate responses.
Controller output remains a proposal, not execution authority.

The operator is deliberately narrow and single-use. It does not relax the
pre-send recovery cap, automate reconciliation, or retry a partial model generation.

## Production preflight

Use both Codexia and the isolated clean CWA source roots on PYTHONPATH.
Supply the known IDs above and --response-sha256-prefix cd922c88e95dc80b.
Run inspect and examine exact response SHA / plan digest before reconcile.
Human approval is RECONCILE-OBSERVED followed by the displayed digest prefix.
Verify archives and phase after reconciliation; never modify the Gen2 store.

## Offline test

    python -m pytest -q tests/test_dw5_observed_controller_send_reconcile_v1.py

Tests use fake canonical chats and disposable local SQLite only.
