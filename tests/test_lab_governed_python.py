from __future__ import annotations

import json
import os
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5

from codexia_manual_agent.authority import ApprovalMode, LocalApprovalAuthority
from codexia_manual_agent.lab import (
    ArtifactRecord,
    EvidenceBindingError,
    ExperimentManifest,
    ExperimentRun,
    GovernedPythonJsonRunner,
    Hypothesis,
    InvalidLabRecordError,
    LabPersistenceIntegrityError,
    MetricRecord,
    PhysicalEvidenceReceipt,
    PYTHON_JSON_PROFILE,
    RunExecutionPhase,
    SqliteLabRegistry,
    SqlitePhysicalEvidenceRegistry,
    SqliteRunExecutionRegistry,
)
from codexia_manual_agent.lab.governed_python import PhysicalOutputSnapshot
from codexia_manual_agent.session_events import (
    ActionRecoveryState,
    SqliteSessionEventStore,
)


GOOD_SOURCE = r'''
from pathlib import Path
import json
import sys

run_id, output_path, input_json = sys.argv[1:4]
value = json.loads(input_json)["x"] * 2
result = {
    "schema": "codexia.python-json-result.v1",
    "run_id": run_id,
    "metric": {"name": "score", "value": value, "unit": "points"},
}
encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
target = Path(output_path)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(encoded, encoding="utf-8")
sys.stdout.write(encoded)
'''.strip()

MISSING_OUTPUT_SOURCE = "import sys; _ = sys.argv[1:4]"

BOOL_METRIC_SOURCE = r'''
from pathlib import Path
import json
import sys
run_id, output_path, _ = sys.argv[1:4]
result = {
    "schema": "codexia.python-json-result.v1",
    "run_id": run_id,
    "metric": {"name": "score", "value": True, "unit": "points"},
}
encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
target = Path(output_path)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(encoded, encoding="utf-8")
sys.stdout.write(encoded)
'''.strip()

MISMATCHED_STDOUT_SOURCE = r'''
from pathlib import Path
import json
import sys
run_id, output_path, _ = sys.argv[1:4]
file_result = {
    "schema": "codexia.python-json-result.v1",
    "run_id": run_id,
    "metric": {"name": "score", "value": 6, "unit": "points"},
}
stdout_result = {
    "schema": "codexia.python-json-result.v1",
    "run_id": run_id,
    "metric": {"name": "score", "value": 7, "unit": "points"},
}
file_bytes = json.dumps(file_result, sort_keys=True, separators=(",", ":"))
stdout_bytes = json.dumps(stdout_result, sort_keys=True, separators=(",", ":"))
target = Path(output_path)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(file_bytes, encoding="utf-8")
sys.stdout.write(stdout_bytes)
'''.strip()


class GovernedPythonRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name).resolve()
        self.db = self.root / "lab.sqlite3"
        self.lab = SqliteLabRegistry(self.db)
        self.m3 = SqliteSessionEventStore(self.db)
        self.executions = SqliteRunExecutionRegistry(self.lab, self.m3)
        self.physical = SqlitePhysicalEvidenceRegistry(self.lab, self.executions)
        self.runner = GovernedPythonJsonRunner(
            self.lab,
            self.m3,
            self.executions,
            self.physical,
        )

    def _register(self, source: str = GOOD_SOURCE) -> ExperimentRun:
        hypothesis = Hypothesis.create(
            statement="The deterministic fixture returns twice its input.",
            falsification_criterion="The score is not exactly twice x.",
        )
        manifest = ExperimentManifest.create(
            hypothesis=hypothesis,
            procedure="Execute one governed inline Python JSON experiment.",
            parameters={
                "profile": PYTHON_JSON_PROFILE,
                "source": source,
                "input": {"x": 3},
                "metric": {"name": "score", "unit": "points"},
            },
        )
        run = ExperimentRun.create(manifest=manifest, ordinal=0, seed=42)
        self.lab.register_experiment(hypothesis, manifest)
        self.lab.register_run(run)
        return run

    def _session(self) -> str:
        session_id = str(uuid4())
        self.m3.start_session(
            session_id=session_id,
            payload={
                "workspace": str(self.root),
                "prompt_version": "m4.3.2-test",
                "mode": "governed-experiment",
                "capabilities": ["execute_process"],
                "provider": "local-test",
                "title": None,
                "model": None,
                "reasoning_effort": None,
            },
        )
        return session_id

    @staticmethod
    def _approve(prepared):
        return LocalApprovalAuthority().decide(
            prepared.proposal,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="m4.3.2-test-human",
        )

    def _prepare(self, run: ExperimentRun):
        return self.runner.prepare(
            run_id=run.run_id,
            m3_session_id=self._session(),
            workspace=self.root,
        )

    def _fresh_physical(self) -> SqlitePhysicalEvidenceRegistry:
        lab = SqliteLabRegistry(self.db)
        m3 = SqliteSessionEventStore(self.db)
        executions = SqliteRunExecutionRegistry(lab, m3)
        return SqlitePhysicalEvidenceRegistry(lab, executions)

    def test_governed_run_registers_exact_observed_physical_artifact_and_metric(self) -> None:
        run = self._register()
        prepared = self._prepare(run)
        result = self.runner.execute_authorized(
            prepared,
            receipt=self._approve(prepared),
        )

        self.assertEqual(result.execution.phase, RunExecutionPhase.OBSERVED)
        self.assertTrue(result.execution.execution_succeeded)
        self.assertIsNotNone(result.physical)
        assert result.physical is not None
        self.assertEqual(result.physical.metric.name, "score")
        self.assertEqual(result.physical.metric.value, 6)
        self.assertEqual(result.physical.metric.unit, "points")
        output = self.root / result.physical.artifact.logical_path
        data = output.read_bytes()
        self.assertEqual(result.physical.artifact.size_bytes, len(data))
        self.assertEqual(
            result.physical.receipt.stdout_sha256,
            result.physical.artifact.sha256,
        )
        self.assertEqual(
            result.execution.evidence.observation.stdout.sha256,
            result.physical.artifact.sha256,
        )

        before = output.stat().st_mtime_ns
        recovered = self._fresh_physical().recover(run.run_id)
        self.assertEqual(recovered.metric.value, 6)
        self.assertEqual(output.stat().st_mtime_ns, before)

    def test_python_profile_rejects_alternate_executable_before_proposal(self) -> None:
        run = self._register()
        session_id = self._session()
        alternate = self.root / "not-the-running-python"
        alternate.write_text("not an interpreter", encoding="utf-8")

        with self.assertRaises(InvalidLabRecordError):
            self.runner.prepare(
                run_id=run.run_id,
                m3_session_id=session_id,
                workspace=self.root,
                python_executable=alternate,
            )

        self.assertFalse(self.m3.recover(session_id).actions)

    def test_preexisting_output_blocks_before_receipt_consumption(self) -> None:
        run = self._register()
        session_id = self._session()
        prepared = self.runner.prepare(
            run_id=run.run_id,
            m3_session_id=session_id,
            workspace=self.root,
        )
        output = self.root / prepared.output_logical_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("stale", encoding="utf-8")

        with self.assertRaises(EvidenceBindingError):
            self.runner.execute_authorized(prepared, receipt=self._approve(prepared))

        action = next(
            item
            for item in self.m3.recover(session_id).actions
            if item.proposal.proposal_id == prepared.proposal.proposal_id
        )
        self.assertEqual(action.state, ActionRecoveryState.AUTHORIZED_UNCONSUMED)
        self.assertEqual(
            self.executions.recover(run.run_id).phase,
            RunExecutionPhase.AUTHORIZED,
        )

    def test_successful_process_without_required_output_is_not_physical_evidence(self) -> None:
        run = self._register(MISSING_OUTPUT_SOURCE)
        prepared = self._prepare(run)
        with self.assertRaises(EvidenceBindingError):
            self.runner.execute_authorized(prepared, receipt=self._approve(prepared))

        recovered = self.executions.recover(run.run_id)
        self.assertEqual(recovered.phase, RunExecutionPhase.OBSERVED)
        self.assertTrue(recovered.execution_succeeded)
        snapshot = self.lab.recover_for_run(run.run_id).run(run.run_id)
        self.assertFalse(snapshot.metrics)
        self.assertFalse(snapshot.artifacts)

    def test_boolean_metric_is_rejected_even_when_process_exits_zero(self) -> None:
        run = self._register(BOOL_METRIC_SOURCE)
        prepared = self._prepare(run)
        with self.assertRaises(EvidenceBindingError):
            self.runner.execute_authorized(prepared, receipt=self._approve(prepared))
        self.assertEqual(
            self.executions.recover(run.run_id).phase,
            RunExecutionPhase.OBSERVED,
        )
        snapshot = self.lab.recover_for_run(run.run_id).run(run.run_id)
        self.assertFalse(snapshot.metrics)
        self.assertFalse(snapshot.artifacts)

    def test_file_bytes_must_equal_exact_execution_stdout_bytes(self) -> None:
        run = self._register(MISMATCHED_STDOUT_SOURCE)
        prepared = self._prepare(run)
        with self.assertRaises(EvidenceBindingError):
            self.runner.execute_authorized(prepared, receipt=self._approve(prepared))

        recovered = self.executions.recover(run.run_id)
        self.assertEqual(recovered.phase, RunExecutionPhase.OBSERVED)
        self.assertTrue(recovered.execution_succeeded)
        snapshot = self.lab.recover_for_run(run.run_id).run(run.run_id)
        self.assertFalse(snapshot.metrics)
        self.assertFalse(snapshot.artifacts)

    def test_equal_numeric_value_with_different_json_type_cannot_be_published(self) -> None:
        run = self._register()
        prepared = self._prepare(run)
        with patch.object(
            self.physical,
            "finalize",
            side_effect=RuntimeError("leave exact observed result unfinalized"),
        ):
            with self.assertRaises(RuntimeError):
                self.runner.execute_authorized(
                    prepared,
                    receipt=self._approve(prepared),
                )

        execution = self.executions.recover(run.run_id)
        assert execution.evidence is not None
        output = self.root / prepared.output_logical_path
        data = output.read_bytes()
        output_digest = sha256(data).hexdigest()
        created_at = execution.evidence.created_at
        artifact = ArtifactRecord.create(
            run=run,
            logical_path=prepared.output_logical_path,
            size_bytes=len(data),
            sha256_digest=output_digest,
            media_type="application/json",
            artifact_id=str(
                uuid5(UUID(run.run_id), "codexia:m4.3.2:artifact:result.json")
            ),
            created_at=created_at,
        )
        rebound_metric = MetricRecord.create(
            run=run,
            name="score",
            value=6.0,
            unit="points",
            metric_id=str(uuid5(UUID(run.run_id), "codexia:m4.3.2:metric:score")),
            created_at=created_at,
        )
        self.lab.register_artifact(artifact)
        self.lab.register_metric(rebound_metric)
        forged = PhysicalEvidenceReceipt.create(
            run=run,
            execution_evidence=execution.evidence,
            snapshot=PhysicalOutputSnapshot(
                logical_path=prepared.output_logical_path,
                size_bytes=len(data),
                sha256=output_digest,
                data=data,
            ),
            artifact=artifact,
            metric=rebound_metric,
        )

        with self.assertRaises(EvidenceBindingError):
            self.physical.publish(forged)

    def test_crash_after_observation_can_finalize_without_reexecution(self) -> None:
        run = self._register()
        prepared = self._prepare(run)
        with patch.object(
            self.physical,
            "finalize",
            side_effect=RuntimeError("simulated crash before physical publication"),
        ):
            with self.assertRaises(RuntimeError):
                self.runner.execute_authorized(
                    prepared,
                    receipt=self._approve(prepared),
                )

        execution = self.executions.recover(run.run_id)
        self.assertEqual(execution.phase, RunExecutionPhase.OBSERVED)
        self.assertTrue(execution.execution_succeeded)
        output = self.root / prepared.output_logical_path
        before_bytes = output.read_bytes()
        before_mtime = output.stat().st_mtime_ns

        recovered = self._fresh_physical().finalize(run.run_id)
        self.assertEqual(recovered.metric.value, 6)
        self.assertEqual(output.read_bytes(), before_bytes)
        self.assertEqual(output.stat().st_mtime_ns, before_mtime)

    def test_substituted_file_after_crash_cannot_finalize_observed_run(self) -> None:
        run = self._register()
        prepared = self._prepare(run)
        with patch.object(
            self.physical,
            "finalize",
            side_effect=RuntimeError("simulated crash before physical publication"),
        ):
            with self.assertRaises(RuntimeError):
                self.runner.execute_authorized(
                    prepared,
                    receipt=self._approve(prepared),
                )

        output = self.root / prepared.output_logical_path
        substituted = {
            "schema": "codexia.python-json-result.v1",
            "run_id": run.run_id,
            "metric": {"name": "score", "value": 999, "unit": "points"},
        }
        output.write_text(
            json.dumps(substituted, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

        with self.assertRaises(EvidenceBindingError):
            self._fresh_physical().finalize(run.run_id)

    def test_physical_mutation_after_publication_fails_recovery(self) -> None:
        run = self._register()
        prepared = self._prepare(run)
        result = self.runner.execute_authorized(
            prepared,
            receipt=self._approve(prepared),
        )
        assert result.physical is not None
        output = self.root / result.physical.artifact.logical_path
        output.write_text(
            json.dumps(
                {
                    "schema": "codexia.python-json-result.v1",
                    "run_id": run.run_id,
                    "metric": {"name": "score", "value": 999, "unit": "points"},
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.physical.recover(run.run_id)

    @unittest.skipIf(os.name == "nt", "Windows symlink creation is privilege-dependent")
    def test_symlink_result_is_rejected_as_physical_evidence(self) -> None:
        source = r'''
from pathlib import Path
import json
import sys
run_id, output_path, _ = sys.argv[1:4]
result = {
    "schema": "codexia.python-json-result.v1",
    "run_id": run_id,
    "metric": {"name": "score", "value": 6, "unit": "points"},
}
encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
target = Path(output_path)
target.parent.mkdir(parents=True, exist_ok=True)
real = target.parent / "real.json"
real.write_text(encoded, encoding="utf-8")
target.symlink_to(real.name)
sys.stdout.write(encoded)
'''.strip()
        run = self._register(source)
        prepared = self._prepare(run)
        with self.assertRaises(EvidenceBindingError):
            self.runner.execute_authorized(prepared, receipt=self._approve(prepared))
        self.assertEqual(
            self.executions.recover(run.run_id).phase,
            RunExecutionPhase.OBSERVED,
        )


if __name__ == "__main__":
    unittest.main()
