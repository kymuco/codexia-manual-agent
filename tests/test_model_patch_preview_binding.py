from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codexia_manual_agent.agent.patch_protocol import (
    ModelPatchChangeRequest,
    ModelPatchRequest,
)
from codexia_manual_agent.domain.errors import InvalidWorkspaceMutationError
from codexia_manual_agent.mutation.model_patch import (
    MODEL_PATCH_PREPARATION_SCHEMA_VERSION,
    ModelPatchPreparation,
    prepare_model_patch_proposal,
)


class ModelPatchPreviewBindingTests(unittest.TestCase):
    def test_tampered_displayed_diff_is_rejected_even_with_real_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = ModelPatchRequest.create(
                request_id="preview-binding",
                changes=(
                    ModelPatchChangeRequest(
                        operation="create",
                        target="a.txt",
                        content="real content\n",
                    ),
                ),
            )
            prepared = prepare_model_patch_proposal(request, workspace=root)

            canonical_patch = prepared.approval_preview.patch
            first = canonical_patch.changes[0]
            tampered_change = replace(
                first,
                unified_diff="--- harmless\n+++ harmless\n",
            )
            tampered_patch = replace(
                canonical_patch,
                changes=(tampered_change,),
            )
            tampered_preview = replace(
                prepared.approval_preview,
                patch=tampered_patch,
            )

            self.assertEqual(
                tampered_patch.change_set_digest,
                canonical_patch.change_set_digest,
            )
            with self.assertRaisesRegex(
                InvalidWorkspaceMutationError,
                "displayed preview does not match exact proposal",
            ):
                ModelPatchPreparation(
                    schema_version=MODEL_PATCH_PREPARATION_SCHEMA_VERSION,
                    request=prepared.request,
                    proposal=prepared.proposal,
                    approval_preview=tampered_preview,
                )


if __name__ == "__main__":
    unittest.main()
