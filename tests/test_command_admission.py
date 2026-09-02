from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codexia_manual_agent.admission import (
    CommandAdmissionPolicy,
    CommandAdmissionVerdict,
    CommandFamily,
    CommandRisk,
    ModelProcessRequest,
    build_process_proposal,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import CommandAdmissionError
from codexia_manual_agent.execution import PROCESS_ACTION


def request(family: CommandFamily, *, arguments=None) -> ModelProcessRequest:
    return ModelProcessRequest(
        request_id=f"req-{family.value}",
        family=family,
        arguments={} if arguments is None else arguments,
    )


class CommandAdmissionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = CommandAdmissionPolicy()

    def test_python_version_is_locally_constructed_and_admitted(self) -> None:
        admission = self.policy.evaluate(request(CommandFamily.PYTHON_VERSION))
        self.assertTrue(admission.admitted)
        self.assertIs(
            admission.verdict,
            CommandAdmissionVerdict.ADMIT_REQUIRES_HUMAN,
        )
        self.assertIs(admission.command.risk, CommandRisk.DIAGNOSTIC)
        self.assertEqual(
            admission.command.envelope.required_capabilities,
            (Capability.EXECUTE_PROCESS,),
        )
        self.assertTrue(admission.command.envelope.bounded)
        self.assertEqual(
            admission.command.argv,
            (str(Path(sys.executable).resolve(strict=True)), "--version"),
        )

    def test_git_version_defers_executable_identity_to_m21_workspace_filter(self) -> None:
        admission = self.policy.evaluate(request(CommandFamily.GIT_VERSION))
        self.assertTrue(admission.admitted)
        self.assertEqual(admission.command.argv, ("git", "--version"))

    def test_git_version_cannot_bind_workspace_controlled_git_from_path(self) -> None:
        found = shutil.which("git")
        if found is None:
            self.skipTest("git is unavailable on this test host")
        real_git = Path(found).resolve(strict=True)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve(strict=True)
            fake_git = workspace / real_git.name
            fake_git.write_bytes(b"repository-controlled fake git")
            if os.name != "nt":
                fake_git.chmod(0o755)

            poisoned_path = os.pathsep.join((str(workspace), str(real_git.parent)))
            with patch.dict(os.environ, {"PATH": poisoned_path}, clear=False):
                # An unfiltered resolver sees the repository-controlled executable first.
                unfiltered = shutil.which("git")
                self.assertIsNotNone(unfiltered)
                self.assertEqual(Path(unfiltered).resolve(strict=True), fake_git)

                admission = self.policy.evaluate(request(CommandFamily.GIT_VERSION))
                proposal = build_process_proposal(admission, workspace=workspace)

            params = proposal.to_dict()["parameters"]
            resolved = Path(params["resolved_executable"]).resolve(strict=True)
            self.assertEqual(resolved, real_git)
            self.assertNotEqual(resolved, fake_git)
            self.assertEqual(params["argv"], ["git", "--version"])

    def test_compileall_is_rejected_for_write_capability(self) -> None:
        admission = self.policy.evaluate(request(CommandFamily.PYTHON_COMPILEALL))
        self.assertFalse(admission.admitted)
        self.assertIs(
            admission.verdict,
            CommandAdmissionVerdict.REJECT_CAPABILITY_ENVELOPE,
        )
        self.assertEqual(
            admission.command.envelope.required_capabilities,
            (Capability.EXECUTE_PROCESS, Capability.WRITE_WORKSPACE),
        )
        self.assertIs(admission.command.risk, CommandRisk.WORKSPACE_MUTATION)

    def test_unittest_is_rejected_as_unbounded_child_code(self) -> None:
        admission = self.policy.evaluate(
            request(CommandFamily.PYTHON_UNITTEST_DISCOVER)
        )
        self.assertFalse(admission.admitted)
        self.assertIs(
            admission.verdict,
            CommandAdmissionVerdict.REJECT_UNBOUNDED_CHILD_CODE,
        )
        self.assertFalse(admission.command.envelope.bounded)
        self.assertIs(admission.command.risk, CommandRisk.UNBOUNDED_CHILD_CODE)

    def test_unbounded_child_code_stays_rejected_even_with_all_capabilities(self) -> None:
        policy = CommandAdmissionPolicy(available_capabilities=tuple(Capability))
        admission = policy.evaluate(request(CommandFamily.PYTHON_UNITTEST_DISCOVER))
        self.assertIs(
            admission.verdict,
            CommandAdmissionVerdict.REJECT_UNBOUNDED_CHILD_CODE,
        )

    def test_command_families_reject_all_model_supplied_arguments(self) -> None:
        for family in CommandFamily:
            with self.subTest(family=family):
                with self.assertRaises(CommandAdmissionError):
                    self.policy.evaluate(
                        request(family, arguments={"argv": ["unexpected"]})
                    )

    def test_admitted_command_can_be_bound_into_m21_proposal(self) -> None:
        admission = self.policy.evaluate(request(CommandFamily.PYTHON_VERSION))
        with tempfile.TemporaryDirectory() as tmp:
            proposal = build_process_proposal(admission, workspace=tmp)
        self.assertIs(proposal.capability, Capability.EXECUTE_PROCESS)
        self.assertEqual(proposal.action, PROCESS_ACTION)
        params = proposal.to_dict()["parameters"]
        self.assertEqual(params["argv"], list(admission.command.argv))
        self.assertEqual(params["cwd"], ".")

    def test_rejected_command_cannot_become_process_proposal(self) -> None:
        admission = self.policy.evaluate(request(CommandFamily.PYTHON_COMPILEALL))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CommandAdmissionError):
                build_process_proposal(admission, workspace=tmp)

    def test_expanded_policy_does_not_expand_proposal_bridge(self) -> None:
        policy = CommandAdmissionPolicy(
            available_capabilities=(
                Capability.EXECUTE_PROCESS,
                Capability.WRITE_WORKSPACE,
            )
        )
        admission = policy.evaluate(request(CommandFamily.PYTHON_COMPILEALL))
        self.assertTrue(admission.admitted)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CommandAdmissionError):
                build_process_proposal(admission, workspace=tmp)


if __name__ == "__main__":
    unittest.main()
