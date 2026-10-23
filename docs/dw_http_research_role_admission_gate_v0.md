# DW HTTP → Gen2 Research Role cognition: admission gate v0

**Scope:** one offline application-level integration test. No new transport,
WorkStore tables, Role model, provider-result recovery, scheduler, or autonomous
action authority.

## Why this gate

PR #96 established one real CWA Direct HTTP bootstrap, with durable
`codexia.chat.bootstrap.claimed.v0` → `captured.v0`. That event pair is
**transport evidence** only. The bootstrap pilot Pack has a Workflow member
but **no RoleBinding**, CognitionRequest, or CognitionHandoff. It must never
be retroactively promoted to an admitted Gen2 `CognitionOutcome`.

The existing product path for **new** delegated cognition is different:

`Research Work Pack v1` (pinned Workflow + RoleBinding) →
`RoleCognitionProgressionService` → durable CognitionRequest and
CognitionHandoff → `ModelProviderCognitionPort(ChatGPTHttpProvider)` →
`CognitionOutcome` → `RoleAdmission`.

All durable authority and Work chronology remain in Gen2. CWA provides
a response, not permission or WorkCompletion.

## Test-first acceptance

`tests/test_dw2_research_work_pack_v1.py::
test_direct_http_provider_admits_research_role_cognition_not_work_completion`

The fixture uses an actual temporary `SqliteWorkStore`, the existing
`ResearchWorkflowImplementationV1` and Research Pack, and the real Codexia
HTTP ModelProvider adapter with a **fake in-memory CWA send()**. It:

1. Starts a fresh Research Work with pinned Role/Workflow/Pack bindings.
2. Runs only the initial Research Role; the mocked HTTP reply is one
   schema-valid `ResearchRoleOutput` asking a material human question.
3. Requires exactly one provider call, the model/effort forwarding, a
   durable `role.cognition-handoff-admitted` and a completed RoleRun with
   exact output text.
4. Expects **AttentionNeed** and Work still active, never WorkCompletion.
5. Reopens the same SQLite database and verifies an unanswered attention
   boundary does not resend or append a duplicate result.

The fake SDK is deliberate: this is a deterministic integration gate, **not**
another live ChatGPT call, a proof of remote attachment bytes, or a provider
receipt/correlation system.

## Boundaries and next step

The real HTTP bootstrap Work
`a82e15fd-b7b8-42ad-b7bf-2e15050c7c00` remains revision 4,
`CAPTURED_UNADMITTED`. No replay and no implicit conversion to a Role outcome.

This test does **not** solve the crash window between provider completion and
role outcome admission. Provider-neutral reconciliation and global deduplication
remain governed by Issue #88. A future operator-authorized **new Work** may
exercise the same Research Role route live after separate preclaim readiness,
one-shot provider dispatch, and explicit review of the semantic result.


## New HTTP pre-handoff readiness gate (follow-up)

For a **new** `CognitionHandoff`, `CognitionTransportBridge.dispatch_once()`
now calls the selected cognition port's *optional*
`preflight_read_only()` hook immediately before constructing and persisting
that handoff. The existing `ModelProviderCognitionPort` delegates the hook
to the selected provider only if supported; other providers retain their
previous behavior. For `ChatGPTHttpProvider`, the existing readiness check
requires local Node/curl and CWA's authenticated
`refresh_auth(persist=False)` session GET.

If this check fails, it cannot create the handoff or send to the model.
A `CognitionRequest` may already be durably admitted; that is **not** a
provider dispatch receipt. Retrying the same REQUESTED Role is safe only
while the exact Work chronology proves **no handoff exists**.

An existing handoff is checked **before** the readiness hook, so restart
cannot regenerate authorization or dispatch authority. After readiness,
the bridge compares the exact Work revision and last-event digest to its
precheck chronology; concurrent Work advancement fails closed without a
handoff or POST. A successful preflight is not proof of eventual POST success. An ambiguous error after
the handoff still forbids replay, including across process restarts.

The gate does not add a provider-result claim index, retroactively admit
bootstrap capture, guarantee session longevity, or solve Issue #88.
