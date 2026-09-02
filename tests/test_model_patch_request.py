from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


from codexia_manual_agent.agent.patch_protocol import (
    MAX_MODEL_PATCH_FILE_BYTES,
    MAX_MODEL_PATCH_FILES,
    MAX_MODEL_PATCH_TOTAL_CONTENT_BYTES,
    ModelPatchChangeRequest,
    ModelPatchOperation,
    ModelPatchRequest,
    parse_model_patch_request,
)
from codexia_manual_agent.agent.protocol import parse_model_reply
from codexia_manual_agent.authority import (
    ApprovalMode,
    ApprovalPolicy,
    ApprovalRequirement,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    ProtocolError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationTargetExistsError,
    WorkspaceMutationTargetMissingError,
)
from codexia_manual_agent.mutation.model_patch import (
    MODEL_PATCH_PREPARATION_SCHEMA_VERSION,
    ModelPatchPreparation,
    prepare_model_patch_proposal,
)
from codexia_manual_agent.mutation.patches import PATCH_ACTION


def _request(
    *changes: tuple[str, str, str],
    request_id: str = "patch-1",
) -> ModelPatchRequest:
    return ModelPatchRequest.create(
        request_id=request_id,
        changes=tuple(
            ModelPatchChangeRequest(
                operation=operation,
                target=target,
                content=content,
            )
            for operation, target, content in changes
        ),
    )


class ModelPatchProtocolTests(unittest.TestCase):
    def test_parse_exact_create_request(self) -> None:
        request = parse_model_patch_request(
            json.dumps(
                {
                    "type": "patch_request",
                    "request_id": "patch-1",
                    "changes": [
                        {
                            "operation": "create",
                            "target": "src/new.py",
                            "content": "print('ok')\n",
                        }
                    ],
                }
            )
        )
        self.assertEqual(request.request_id, "patch-1")
        self.assertEqual(request.changes[0].operation, ModelPatchOperation.CREATE)
        self.assertEqual(request.changes[0].content_bytes, b"print('ok')\n")
        self.assertEqual(len(request.request_digest), 64)

    def test_json_fence_and_whitespace_do_not_change_request_digest(self) -> None:
        payload = {
            "type": "patch_request",
            "request_id": "patch-1",
            "changes": [
                {
                    "operation": "replace",
                    "target": "a.txt",
                    "content": "new\n",
                }
            ],
        }
        compact = parse_model_patch_request(json.dumps(payload, separators=(",", ":")))
        fenced = parse_model_patch_request(
            "```json\n" + json.dumps(payload, indent=2) + "\n```"
        )
        self.assertEqual(compact.request_digest, fenced.request_digest)

    def test_duplicate_json_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "duplicate JSON key"):
            parse_model_patch_request(
                '{"type":"patch_request","request_id":"a","request_id":"b",'
                '"changes":[{"operation":"create","target":"a","content":"x"}]}'
            )

    def test_model_cannot_supply_workspace_or_authority_fields(self) -> None:
        for field, value in (
            ("workspace_root", "C:/other"),
            ("capability", "write_workspace"),
            ("approval", True),
            ("proposal_digest", "0" * 64),
        ):
            with self.subTest(field=field):
                payload = {
                    "type": "patch_request",
                    "request_id": "patch-1",
                    "changes": [
                        {
                            "operation": "create",
                            "target": "a.txt",
                            "content": "x\n",
                        }
                    ],
                    field: value,
                }
                with self.assertRaisesRegex(ProtocolError, "keys mismatch"):
                    parse_model_patch_request(json.dumps(payload))

    def test_model_cannot_supply_preimage_or_change_digest(self) -> None:
        for field, value in (
            ("expected_preimage", {"state": "absent"}),
            ("preimage_data_base64", None),
            ("change_digest", "0" * 64),
        ):
            with self.subTest(field=field):
                payload = {
                    "type": "patch_request",
                    "request_id": "patch-1",
                    "changes": [
                        {
                            "operation": "create",
                            "target": "a.txt",
                            "content": "x\n",
                            field: value,
                        }
                    ],
                }
                with self.assertRaisesRegex(ProtocolError, "keys mismatch"):
                    parse_model_patch_request(json.dumps(payload))

    def test_delete_and_rename_are_rejected(self) -> None:
        for operation in ("delete", "rename"):
            with self.subTest(operation=operation):
                payload = {
                    "type": "patch_request",
                    "request_id": "patch-1",
                    "changes": [
                        {
                            "operation": operation,
                            "target": "a.txt",
                            "content": "",
                        }
                    ],
                }
                with self.assertRaisesRegex(ProtocolError, "create.*replace"):
                    parse_model_patch_request(json.dumps(payload))

    def test_request_id_format_is_bounded(self) -> None:
        payload = {
            "type": "patch_request",
            "request_id": "../bad id",
            "changes": [
                {"operation": "create", "target": "a.txt", "content": "x"}
            ],
        }
        with self.assertRaisesRegex(ProtocolError, "request_id"):
            parse_model_patch_request(json.dumps(payload))

    def test_empty_and_too_many_change_sets_are_rejected(self) -> None:
        empty = {"type": "patch_request", "request_id": "a", "changes": []}
        with self.assertRaisesRegex(ProtocolError, "1..32"):
            parse_model_patch_request(json.dumps(empty))

        too_many = {
            "type": "patch_request",
            "request_id": "a",
            "changes": [
                {"operation": "create", "target": f"{index}.txt", "content": "x"}
                for index in range(MAX_MODEL_PATCH_FILES + 1)
            ],
        }
        with self.assertRaisesRegex(ProtocolError, "1..32"):
            parse_model_patch_request(json.dumps(too_many))

    def test_per_file_content_budget_is_enforced_in_bytes(self) -> None:
        payload = {
            "type": "patch_request",
            "request_id": "a",
            "changes": [
                {
                    "operation": "create",
                    "target": "a.txt",
                    "content": "x" * (MAX_MODEL_PATCH_FILE_BYTES + 1),
                }
            ],
        }
        with self.assertRaisesRegex(ProtocolError, "file content exceeds"):
            parse_model_patch_request(json.dumps(payload))

    def test_total_content_budget_is_enforced(self) -> None:
        chunk = "x" * 900_000
        changes = tuple(
            ModelPatchChangeRequest(
                operation=ModelPatchOperation.CREATE,
                target=f"{index}.txt",
                content=chunk,
            )
            for index in range(5)
        )
        self.assertGreater(
            sum(len(change.content_bytes) for change in changes),
            MAX_MODEL_PATCH_TOTAL_CONTENT_BYTES,
        )
        with self.assertRaisesRegex(ProtocolError, "total content exceeds"):
            ModelPatchRequest.create(request_id="large", changes=changes)

    def test_request_digest_tampering_is_rejected(self) -> None:
        request = _request(("create", "a.txt", "x\n"))
        with self.assertRaisesRegex(ProtocolError, "digest does not match"):
            replace(request, request_digest="0" * 64)

    def test_read_only_agent_protocol_still_rejects_patch_request(self) -> None:
        payload = json.dumps(
            {
                "type": "patch_request",
                "request_id": "patch-1",
                "changes": [
                    {"operation": "create", "target": "a.txt", "content": "x\n"}
                ],
            }
        )
        with self.assertRaisesRegex(ProtocolError, "tool_request.*final"):
            parse_model_reply(payload, max_chars=16_384)


class ModelPatchProposalBridgeTests(unittest.TestCase):
    def test_create_intent_becomes_unapproved_exact_patch_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = _request(("create", "new.txt", "hello\n"))
            prepared = prepare_model_patch_proposal(request, workspace=root)

            self.assertEqual(
                prepared.schema_version,
                MODEL_PATCH_PREPARATION_SCHEMA_VERSION,
            )
            self.assertEqual(prepared.proposal.capability, Capability.WRITE_WORKSPACE)
            self.assertEqual(prepared.proposal.action, PATCH_ACTION)
            self.assertIn(request.request_id, prepared.proposal.summary)
            self.assertIn(request.request_digest, prepared.proposal.summary)
            self.assertTrue(prepared.approval_preview.requires_human)
            self.assertFalse((root / "new.txt").exists())

    def test_replace_intent_captures_local_preimage_and_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "old.txt"
            target.write_text("old\n", encoding="utf-8")
            request = _request(("replace", "old.txt", "new\n"))
            prepared = prepare_model_patch_proposal(request, workspace=root)

            change = prepared.approval_preview.patch.changes[0]
            self.assertEqual(change.target, "old.txt")
            self.assertIn("-old", change.unified_diff)
            self.assertIn("+new", change.unified_diff)
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

    def test_existing_create_missing_replace_and_noop_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "present.txt").write_bytes(b"same\n")
            cases = (
                (
                    _request(("create", "present.txt", "new\n")),
                    WorkspaceMutationTargetExistsError,
                ),
                (
                    _request(("replace", "missing.txt", "new\n")),
                    WorkspaceMutationTargetMissingError,
                ),
                (
                    _request(("replace", "present.txt", "same\n")),
                    InvalidWorkspaceMutationError,
                ),
            )
            for request, error in cases:
                with self.subTest(error=error.__name__):
                    with self.assertRaises(error):
                        prepare_model_patch_proposal(request, workspace=root)

    def test_line_ending_change_is_not_noop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "present.txt"
            target.write_bytes(b"same\r\n")

            prepared = prepare_model_patch_proposal(
                _request(("replace", "present.txt", "same\n")),
                workspace=root,
            )

            change = prepared.approval_preview.patch.changes[0]
            self.assertNotEqual(change.preimage_sha256, change.postimage_sha256)
            self.assertEqual(target.read_bytes(), b"same\r\n")

    def test_traversal_and_sensitive_targets_use_existing_patch_guards(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for target in ("../escape.txt", ".git/config", ".codexia/state.json"):
                with self.subTest(target=target):
                    request = _request(("create", target, "x\n"))
                    with self.assertRaises(WorkspaceMutationBoundaryError):
                        prepare_model_patch_proposal(request, workspace=root)

    def test_duplicate_targets_are_rejected_by_local_patch_builder(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = _request(
                ("create", "same.txt", "one\n"),
                ("create", "same.txt", "two\n"),
            )
            with self.assertRaisesRegex(
                InvalidWorkspaceMutationError,
                "duplicate target",
            ):
                prepare_model_patch_proposal(request, workspace=root)

    def test_approval_policy_remains_human_governed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prepared = prepare_model_patch_proposal(
                _request(("create", "a.txt", "x\n")),
                workspace=root,
            )
            policy = ApprovalPolicy()
            self.assertEqual(
                policy.evaluate(prepared.proposal, ApprovalMode.ALWAYS),
                ApprovalRequirement.REQUIRE_HUMAN,
            )
            self.assertEqual(
                policy.evaluate(prepared.proposal, ApprovalMode.RISKY),
                ApprovalRequirement.REQUIRE_HUMAN,
            )
            self.assertEqual(
                policy.evaluate(prepared.proposal, ApprovalMode.NEVER),
                ApprovalRequirement.DENY,
            )

    def test_preview_contains_provenance_but_no_authority_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prepared = prepare_model_patch_proposal(
                _request(("create", "a.txt", "x\n")),
                workspace=Path(raw),
            )
            value = prepared.approval_preview.to_dict()
            rendered = json.dumps(value, sort_keys=True)
            self.assertEqual(
                value["request_digest"],
                prepared.request.request_digest,
            )
            self.assertEqual(
                value["proposal_digest"],
                prepared.proposal.proposal_digest,
            )
            self.assertNotIn("authorization", rendered)
            self.assertNotIn("receipt", rendered)

    def test_pairing_foreign_request_with_proposal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = prepare_model_patch_proposal(
                _request(("create", "a.txt", "a\n"), request_id="a"),
                workspace=root,
            )
            foreign = _request(("create", "b.txt", "b\n"), request_id="b")
            with self.assertRaises(InvalidWorkspaceMutationError):
                ModelPatchPreparation(
                    schema_version=MODEL_PATCH_PREPARATION_SCHEMA_VERSION,
                    request=foreign,
                    proposal=first.proposal,
                    approval_preview=first.approval_preview,
                )
