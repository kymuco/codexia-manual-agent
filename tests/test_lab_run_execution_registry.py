from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

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
from codexia_manual_agent.session_events import (
    DurableAuthorizationConsumptionRegistry,
    SqliteSessionEventStore,
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
        self.m3 = SqliteSessionEventStore(self.db_path)
        self.m3_session_id = str(uuid4())
        self.m3.start_session(
            session_id=self.m3_session_id,
            payload={
                "workspace": str(self.root),
                "prompt_version": "m4.3.1-test",
                "mode": "governed-experiment",
                "capabilities": ["execute_process"],
                "provider": "local-test",
                "title": None,
                "model": None,
                "reasoning_effort": None,
            },
        )
        self.executions = SqliteRunExecutionRegistry(self.lab, self.m3)

    def _proposal(self, text: str = "ok") -> ActionProposal:
        return prepare_process_proposal(
            workspace=self.root,
            argv=[sys.executable, "-c", f"print({text!r})"],
        )

    def _registered_binding(self, text: str = "ok") -> RunExecutionBinding:
        binding = RunExecutionBinding.create(run=self.run, proposal=self._proposal(text))
        self.m3.record_proposal(self.m3_session_id, binding.proposal)
        self.executions.register_binding(
            binding,
            m3_session_id=self.m3_session_id,
        )
        return binding

    def _authorized(
        self,
        binding: RunExecutionBinding,
    ) -> tuple[
        RunExecutionAuthorization,
        ActionLifecycle,
        LocalApprovalAuthority,
    ]:
        authority = LocalApprovalAuthority(
            consumption_registry=DurableAuthorizationConsumptionRegistry(
                self.m3,
                session_id=self.m3_session_id,
            )
        )
        receipt = authority.decide(
            binding.proposal,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="m4.3.1-test-human",
        )
        self.m3.record_authorization(self.m3_session_id, receipt)
        authorization = RunExecutionAuthorization.create(
            binding=binding,
            receipt=receipt,
        )
        lifecycle = ActionLifecycle(binding.proposal, ApprovalMode.RISKY)
        lifecycle.apply_receipt(receipt, authority=authority)
        return authorization, lifecycle, authority

    def _execute_and_record_m3(
        self,
        *,
        binding: RunExecutionBinding,
        authorization: RunExecutionAuthorization,
        lifecycle: ActionLifecycle,
        authority: LocalApprovalAuthority,
        digest_bound: bool = True,
    ) -> ProcessExecutionObservation:
        observation = ProcessExecutor().execute(lifecycle, authority=authority)
        self.m3.record_execution(
            self.m3_session_id,
            proposal=binding.proposal,
            receipt=authorization.receipt,
            execution_id=observation.execution_id,
        )
        kwargs: dict[str, object] = {
            "proposal": binding.proposal,
            "execution_id": observation.execution_id,
            "observation_id": observation.observation_id,
        }
        if digest_bound:
            kwargs["observation_digest"] = observation.observation_digest
        self.m3.record_observation(self.m3_session_id, **kwargs)
        return observation

    def _observed_chain(
        self,
    ) -> tuple[
        RunExecutionBinding,
        RunExecutionAuthorization,
        RunExecutionEvidence,
    ]:
        binding = self._registered_binding()
        authorization, lifecycle, authority = self._authorized(binding)
        self.executions.register_authorization(authorization)
        observation = self._execute_and_record_m3(
            binding=binding,
            authorization=authorization,
            lifecycle=lifecycle,
            authority=authority,
        )
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

    def test_binding_requires_exact_proposed_m3_action(self) -> None:
        binding = RunExecutionBinding.create(run=self.run, proposal=self._proposal("not-recorded"))
        with self.assertRaises(EvidenceBindingError):
            self.executions.register_binding(
                binding,
                m3_session_id=self.m3_session_id,
            )

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

    def test_already_authorized_m3_action_cannot_be_bound_post_hoc(self) -> None:
        binding = RunExecutionBinding.create(run=self.run, proposal=self._proposal("pre-authorized"))
        self.m3.record_proposal(self.m3_session_id, binding.proposal)
        authority = LocalApprovalAuthority()
        receipt = authority.decide(
            binding.proposal,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="test-human",
        )
        self.m3.record_authorization(self.m3_session_id, receipt)

        with self.assertRaises(EvidenceBindingError):
            self.executions.register_binding(
                binding,
                m3_session_id=self.m3_session_id,
            )

    def test_observed_m3_action_cannot_receive_m4_authorization_post_hoc(self) -> None:
        binding = self._registered_binding("post-hoc")
        authorization, lifecycle, authority = self._authorized(binding)
        self._execute_and_record_m3(
            binding=binding,
            authorization=authorization,
            lifecycle=lifecycle,
            authority=authority,
        )

        with self.assertRaises(EvidenceBindingError):
            self.executions.register_authorization(authorization)

        self.assertEqual(
            self.executions.recover(self.run.run_id).phase,
            RunExecutionPhase.BOUND,
        )

    def test_observation_with_forged_process_identity_cannot_back_run(self) -> None:
        binding = RunExecutionBinding.create(run=self.run, proposal=self._proposal("bound"))
        authority = LocalApprovalAuthority()
        receipt = authority.decide(
            binding.proposal,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="test-human",
        )
        authorization = RunExecutionAuthorization.create(binding=binding, receipt=receipt)
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

    def test_fully_fabricated_observation_with_real_ids_cannot_back_run(self) -> None:
        binding = self._registered_binding("real")
        authorization, lifecycle, authority = self._authorized(binding)
        self.executions.register_authorization(authorization)
        actual = self._execute_and_record_m3(
            binding=binding,
            authorization=authorization,
            lifecycle=lifecycle,
            authority=authority,
        )
        forged_stdout = StreamObservation.from_bytes(
            byte_count=len(b"forged\n"),
            digest="4a247e0e7f94d88a089169fbdc9ab21df93e873b617bcb2dd2b08d6d6e35d56c",
            stored=b"forged\n",
        )
        forged = ProcessExecutionObservation.create(
            proposal_id=actual.proposal_id,
            proposal_digest=actual.proposal_digest,
            receipt_id=actual.receipt_id,
            receipt_digest=actual.receipt_digest,
            execution_id=actual.execution_id,
            started=actual.started,
            pid=actual.pid,
            cwd=actual.cwd,
            resolved_executable=actual.resolved_executable,
            argv=actual.argv,
            exit_code=actual.exit_code,
            termination_reason=actual.termination_reason,
            duration_ms=actual.duration_ms,
            stdout=forged_stdout,
            stderr=actual.stderr,
            error=actual.error,
            observation_id=actual.observation_id,
            created_at=actual.created_at,
        )
        self.assertNotEqual(forged.observation_digest, actual.observation_digest)
        forged_evidence = RunExecutionEvidence.create(
            binding=binding,
            authorization=authorization,
            observation=forged,
        )

        with self.assertRaises(EvidenceBindingError):
            self.executions.register_evidence(forged_evidence)
        self.assertEqual(
            self.executions.recover(self.run.run_id).phase,
            RunExecutionPhase.AUTHORIZED,
        )

        actual_evidence = RunExecutionEvidence.create(
            binding=binding,
            authorization=authorization,
            observation=actual,
        )
        recovered = self.executions.register_evidence(actual_evidence)
        self.assertEqual(recovered.phase, RunExecutionPhase.OBSERVED)

    def test_legacy_m3_observation_without_digest_cannot_back_m4_execution(self) -> None:
        binding = self._registered_binding("legacy")
        authorization, lifecycle, authority = self._authorized(binding)
        self.executions.register_authorization(authorization)
        observation = self._execute_and_record_m3(
            binding=binding,
            authorization=authorization,
            lifecycle=lifecycle,
            authority=authority,
            digest_bound=False,
        )
        evidence = RunExecutionEvidence.create(
            binding=binding,
            authorization=authorization,
            observation=observation,
        )

        with self.assertRaises(EvidenceBindingError):
            self.executions.register_evidence(evidence)
        self.assertEqual(
            self.executions.recover(self.run.run_id).phase,
            RunExecutionPhase.AUTHORIZED,
        )

    def test_durable_binding_authorization_and_observation_survive_restart(self) -> None:
        binding, authorization, evidence = self._observed_chain()

        recovered = SqliteRunExecutionRegistry(
            SqliteLabRegistry(self.db_path),
            SqliteSessionEventStore(self.db_path),
        ).recover(self.run.run_id)

        self.assertEqual(recovered.phase, RunExecutionPhase.OBSERVED)
        self.assertTrue(recovered.execution_succeeded)
        self.assertEqual(recovered.binding, binding)
        self.assertEqual(recovered.authorization, authorization)
        self.assertEqual(recovered.evidence, evidence)
        self.assertEqual(recovered.m3_session_id, self.m3_session_id)
        self.assertIsNotNone(recovered.m3_observation_event_id)
        self.assertIsNotNone(recovered.m3_observation_event_digest)
        self.assertEqual(len(recovered.events), 3)

    def test_execution_evidence_requires_durable_authorization_event(self) -> None:
        binding = self._registered_binding()
        authorization, lifecycle, authority = self._authorized(binding)
        observation = self._execute_and_record_m3(
            binding=binding,
            authorization=authorization,
            lifecycle=lifecycle,
            authority=authority,
        )
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
        authority = LocalApprovalAuthority()
        receipt = authority.decide(
            binding.proposal,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="test-human",
        )
        authorization = RunExecutionAuthorization.create(binding=binding, receipt=receipt)

        with self.assertRaises(InvalidLabRecordError):
            self.executions.register_authorization(authorization)

    def test_sealed_m4_run_cannot_gain_execution_binding(self) -> None:
        self.lab.seal_run(self.run.run_id, self.run.run_digest)
        binding = RunExecutionBinding.create(run=self.run, proposal=self._proposal())
        self.m3.record_proposal(self.m3_session_id, binding.proposal)

        with self.assertRaises(LabRegistryStateError):
            self.executions.register_binding(
                binding,
                m3_session_id=self.m3_session_id,
            )

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
            SqliteRunExecutionRegistry(
                SqliteLabRegistry(self.db_path),
                SqliteSessionEventStore(self.db_path),
            ).recover(self.run.run_id)

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
            SqliteRunExecutionRegistry(
                SqliteLabRegistry(self.db_path),
                SqliteSessionEventStore(self.db_path),
            ).recover(self.run.run_id)

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
        self.m3.record_proposal(self.m3_session_id, binding.proposal)
        with self.assertRaises(InvalidLabRecordError):
            self.executions.register_binding(
                binding,
                m3_session_id=self.m3_session_id,
            )

    def test_m3_and_m4_must_share_one_sqlite_trust_domain(self) -> None:
        other = SqliteSessionEventStore(self.root / "other-m3.sqlite3")
        with self.assertRaises(ValueError):
            SqliteRunExecutionRegistry(self.lab, other)


if __name__ == "__main__":
    unittest.main()
