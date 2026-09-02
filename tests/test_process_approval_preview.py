from __future__ import annotations

import unittest

from codexia_manual_agent.admission import (
    CommandAdmissionPolicy,
    CommandFamily,
    ModelProcessRequest,
    build_approval_preview,
)


class ProcessApprovalPreviewTests(unittest.TestCase):
    def test_admitted_preview_exposes_local_command_without_authority_tokens(self) -> None:
        request = ModelProcessRequest("preview-1", CommandFamily.PYTHON_VERSION, {})
        admission = CommandAdmissionPolicy().evaluate(request)
        preview = build_approval_preview(admission).to_dict()

        self.assertTrue(preview["requires_human"])
        self.assertEqual(preview["family"], "python_version")
        self.assertEqual(preview["required_capabilities"], ["execute_process"])
        self.assertTrue(preview["bounded"])
        self.assertEqual(preview["argv"][-1], "--version")
        self.assertEqual(
            set(preview),
            {
                "request_id",
                "family",
                "verdict",
                "risk",
                "argv",
                "cwd",
                "required_capabilities",
                "bounded",
                "requires_human",
                "reason",
            },
        )
        for forbidden in (
            "approved",
            "approval_mode",
            "proposal_digest",
            "receipt_id",
            "receipt_digest",
        ):
            self.assertNotIn(forbidden, preview)

    def test_rejected_preview_never_claims_human_approval_is_sufficient(self) -> None:
        request = ModelProcessRequest(
            "preview-2",
            CommandFamily.PYTHON_UNITTEST_DISCOVER,
            {},
        )
        admission = CommandAdmissionPolicy().evaluate(request)
        preview = build_approval_preview(admission).to_dict()
        self.assertFalse(preview["requires_human"])
        self.assertFalse(preview["bounded"])
        self.assertEqual(preview["verdict"], "reject_unbounded_child_code")


if __name__ == "__main__":
    unittest.main()
