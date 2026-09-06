from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionProposal,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.execution import (
    PROCESS_ACTION,
    ProcessExecutionObservation,
    ProcessExecutor,
    ProcessTerminationReason,
    StreamObservation,
    prepare_process_proposal,
)
from codexia_manual_agent.lab import (
    EvidenceBindingError,
    ExperimentManifest,
    ExperimentRun,
    Hypothesis,
    InvalidLabRecordError,
    LabPersistenceIntegrityError,
    LabRegistryStateError,
    RunExecutionAuthorization,
    RunExecutionBinding,
    RunExecutionEvidence,
    RunExecutionPhase,
    SqliteLabRegistry,
    SqliteRunExecutionRegistry,
)


class RunExecutionRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name).resolve()
        self.db_path = self.root / "lab.sqlite3"
        self.hypothesis = Hypothesis.create(
            statement="The governed fixture exits successfully.",
            falsification_criterion="The governed fixture does not exit successfully.",
        )
        self.manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Execute one exact local Python fixture.",
            parameters={"profile": "m4.3.1-test"},
        )
        self.run = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=0,
            seed=42,
        )
        self.lab = SqliteLabRegistry(self.db_path)
        self.lab.register_experiment(self.hypothesis, self.manifest)
        self.lab.register_run(self.run)
        self.executions = SqliteRunExecutionRegistry(self.lab)

    def _proposal(self, text: str = "ok") -> ActionProposal:
        return prepare_process_proposal(
            workspace=self.root,
            argv=[sys.executable, "-c", f"print({text!r})"],
        )

    def _authorized(
        self,
        binding: RunExecutionBinding,
    ) -> tuple[
        RunExecutionAuthorization,
        ActionLifecycle,
        LocalApprovalAuthority,
    ]:
        authority = LocalApprovalAuthority()
        receipt = authority.decide(
            binding.proposal,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="m4.3.1-test-human",
        )
        authorization = RunExecutionAuthorization.create(
            binding=binding,
            receipt=receipt,
        )
        lifecycle = ActionLifecycle(binding.proposal, ApprovalMode.RISKY)
        lifecycle.apply_receipt(receipt, authority=authority)
        return authorization, lifecycle, authority

    def _observed_chain(
        self,
    ) -> tuple[
        RunExecutionBinding,
        RunExecutionAuthorization,
        RunExecutionEvidence,
    ]:
        binding = RunExecutionBinding.create(run=self.run, proposal=self._proposal())
        self.executions.register_binding(binding)
        authorization, lifecycle, authority = self._authorized(binding)
        self.executions.register_authorization(authorization)
        observation = ProcessExecutor().execute(lifecycle, authority=authority)
        evidence = RunExecutionEvidence.create(
            binding=binding,
            authorization=authorization,
            observation=observation,
        )
        self.executions.register_evidence(evidence)
        return binding, authorization, evidence

    def test_binding_rejects_non_process_authority(self) -> None:
        proposal = ActionProposal.create(
            capability=Capability.READ_WORKSPACE,
            action=PROCESS_ACTION,
            workspace_root=str(self.root),
            parameters={},
        )
        with self.assertRaises(EvidenceBindingError):
            RunExecutionBinding.create(run=self.run, proposal=proposal)

    def test_authorization_from_another_proposal_cannot_bind_run(self) -> None:
        binding = RunExecutionBinding.create(run=self.run, proposal=self._proposal("first"))
        other = self._proposal("second")
        authority = LocalApprovalAuthority()
        receipt = authority.decide(
            other,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="test-human",
        )
        with self.assertRaises(EvidenceBindingError):
            RunExecutionAuthorization.create(binding=binding, receipt=receipt)

    def test_observation_with_forged_process_identity_cannot_back_run(self) -> None:
        binding = RunExecutionBinding.create(run=self.run, proposal=self._proposal("bound"))
        authorization, _lifecycle, _authority = self._authorized(binding)
        empty = StreamObservation.from_bytes(
            byte_count=0,
            digest="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            stored=b"",
        )
        forged = ProcessExecutionObservation.create(
            proposal_id=binding.proposal.proposal_id,
            proposal_digest=binding.proposal.proposal_digest,
            receipt_id=authorization.receipt.receipt_id,
            receipt_digest=authorization.receipt.receipt_digest,
            execution_id="forged-execution",
            started=True,
            pid=None,
            cwd=".",
            resolved_executable=str(self.root / "not-the-bound-python"),
            argv=(str(self.root / "not-the-bound-python"), "-c", "print('bound')"),
            exit_code=0,
            termination_reason=ProcessTerminationReason.EXITED,
            duration_ms=1,
            stdout=empty,
            stderr=empty,
        )
        with self.assertRaises(EvidenceBindingError):
            RunExecutionEvidence.create(
                binding=binding,
                authorization=authorization,
                observation=forged,
            )

    def test_durable_binding_authorization_and_observation_survive_restart(self) -> None:
        binding, authorization, evidence = self._observed_chain()

        recovered = SqliteRunExecutionRegistry(
            SqliteLabRegistry(self.db_path)
        ).recover(self.run.run_id)

        self.assertEqual(recovered.phase, RunExecutionPhase.OBSERVED)
        self.assertTrue(recovered.execution_succeeded)
        self.assertEqual(recovered.binding, binding)
        self.assertEqual(recovered.authorization, authorization)
        self.assertEqual(recovered.evidence, evidence)
        self.assertEqual(len(recovered.events), 3)

    def test_execution_evidence_requires_durable_authorization_event(self) -> None:
        binding = RunExecutionBinding.create(run=self.run, proposal=self._proposal())
        self.executions.register_binding(binding)
        authorization, lifecycle, authority = self._authorized(binding)
        observation = ProcessExecutor().execute(lifecycle, authority=authority)
        evidence = RunExecutionEvidence.create(
            binding=binding,
            authorization=authorization,
            observation=observation,
        )

        with self.assertRaises(LabRegistryStateError):
            self.executions.register_evidence(evidence)

        recovered = self.executions.recover(self.run.run_id)
        self.assertEqual(recovered.phase, RunExecutionPhase.BOUND)
        self.assertIsNone(recovered.authorization)
        self.assertIsNone(recovered.evidence)

    def test_unknown_binding_cannot_receive_authorization(self) -> None:
        binding = RunExecutionBinding.create(run=self.run, proposal=self._proposal())
        authorization, _lifecycle, _authority = self._authorized(binding)

        with self.assertRaises(InvalidLabRecordError):
            self.executions.register_authorization(authorization)

    def test_sealed_m4_run_cannot_gain_execution_binding(self) -> None:
        self.lab.seal_run(self.run.run_id, self.run.run_digest)
        binding = RunExecutionBinding.create(run=self.run, proposal=self._proposal())

        with self.assertRaises(LabRegistryStateError):
            self.executions.register_binding(binding)

    def test_event_payload_tamper_is_detected_after_restart(self) -> None:
        self._observed_chain()
        with closing(sqlite3.connect(self.db_path)) as connection:
            raw = connection.execute(
                """
                SELECT payload_json FROM lab_run_execution_events
                WHERE run_id = ? AND sequence = 2
                """,
                (self.run.run_id,),
            ).fetchone()[0]
            payload = json.loads(raw)
            payload["evidence"]["expected_cwd"] = "tampered"
            connection.execute(
                """
                UPDATE lab_run_execution_events SET payload_json = ?
                WHERE run_id = ? AND sequence = 2
                """,
                (
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    self.run.run_id,
                ),
            )
            connection.commit()

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteRunExecutionRegistry(SqliteLabRegistry(self.db_path)).recover(
                self.run.run_id
            )

    def test_root_head_tamper_is_detected_after_restart(self) -> None:
        self._observed_chain()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """
                UPDATE lab_run_execution_roots SET head_event_digest = ?
                WHERE run_id = ?
                """,
                ("0" * 64, self.run.run_id),
            )
            connection.commit()

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteRunExecutionRegistry(SqliteLabRegistry(self.db_path)).recover(
                self.run.run_id
            )

    def test_binding_requires_durable_registered_run(self) -> None:
        unregistered = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=1,
            seed=43,
        )
        binding = RunExecutionBinding.create(
            run=unregistered,
            proposal=self._proposal(),
        )
        with self.assertRaises(InvalidLabRecordError):
            self.executions.register_binding(binding)


if __name__ == "__main__":
    unittest.main()
