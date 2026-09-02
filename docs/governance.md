# Governance

## Version policy

Create a new released prompt or runtime version only for a meaningful capability,
trust-contract, schema, or real-workflow change. Accumulate cosmetic wording
changes instead of micro-versioning.

## Evidence policy

Codexia distinguishes current supplied evidence, user confirmation,
summary-derived history, model interpretation, proposal, and unknown state.

## Runtime policy

The local runtime may execute tools only through explicit capabilities,
workspace boundaries, approval policy, and persisted receipts.

A remote model response is never itself an authorization.

Every future side effect must be represented by a local digest-bound proposal.
Risk classification is local. When policy requires a human decision, a
policy-sourced allow receipt is invalid even if all other proposal fields match.

Authorization is single-use and is consumed before the side-effect executor is
invoked. `never` mode cannot be overridden by approving one proposal; changing
the mode is a separate control-plane decision.

M2.0 receipt consumption is process-local. Durable authorization chronology and
crash/replay governance are required before M3 closes.

## Promotion policy

Experimental components require repeated real-workflow evidence before promotion
into the stable runtime.

## Project boundary

Codexia is standalone. Integration with HDE or another system may be implemented
later, but does not redefine this repository's scope.
