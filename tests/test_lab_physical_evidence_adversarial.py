from __future__ import annotations

import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from codexia_manual_agent.authority import ApprovalMode, LocalApprovalAuthority
from codexia_manual_agent.lab import (
    ArtifactRecord,
    EvidenceBindingError,
    ExperimentManifest,
    ExperimentRun,
    GovernedPythonJsonRunner,
    Hypothesis,
    MetricRecord,
    PYTHON_JSON_PROFILE,
    PhysicalEvidenceReceipt,
    SqliteLabRegistry,
    SqlitePhysicalEvidenceRegistry,
    SqliteRunExecutionRegistry,
)
from codexia_manual_agent.lab.governed_python import (
    PhysicalOutputSnapshot,
    _artifact_id,
    _metric_id,
)
from codexia_manual_agent.session_events import SqliteSessionEventStore


SOURCE = r'''
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
target.write_text(encoded, encoding="utf-8")
sys.stdout.write(encoded)
'''.strip()


class PhysicalEvidenceAdversarialTests(unittest.TestCase):
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

        hypothesis = Hypothesis.create(
            statement="The fixture emits score 6.",
            falsification_criterion="The score is not 6.",
        )
        manifest = ExperimentManifest.create(
            hypothesis=hypothesis,
            procedure="Execute one governed result fixture.",
            parameters={
                "profile": PYTHON_JSON_PROFILE,
                "source": SOURCE,
                "input": {},
                "metric": {"name": "score", "unit": "points"},
            },
        )
        self.run = ExperimentRun.create(manifest=manifest, ordinal=0, seed=1)
        self.lab.register_experiment(hypothesis, manifest)
        self.lab.register_run(self.run)

        self.session_id = str(uuid4())
        self.m3.start_session(
            session_id=self.session_id,
            payload={
                "workspace": str(self.root),
                "prompt_version": "m4.3.2-adversarial",
                "mode": "governed-experiment",
                "capabilities": ["execute_process"],
                "provider": "local-test",
                "title": None,
                "model": None,
                "reasoning_effort": None,
            },
        )

    def test_publish_rejects_metric_name_rebinding_even_with_valid_records(self) -> None:
        prepared = self.runner.prepare(
            run_id=self.run.run_id,
            m3_session_id=self.session_id,
            workspace=self.root,
        )
        receipt = LocalApprovalAuthority().decide(
            prepared.proposal,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="m4.3.2-test-human",
        )
        with patch.object(
            self.physical,
            "finalize",
            side_effect=RuntimeError("stop after M4.3.1 observation"),
        ):
            with self.assertRaises(RuntimeError):
                self.runner.execute_authorized(prepared, receipt=receipt)

        execution = self.executions.recover(self.run.run_id)
        assert execution.evidence is not None
        output = self.root / prepared.output_logical_path
        data = output.read_bytes()
        snapshot = PhysicalOutputSnapshot(
            logical_path=prepared.output_logical_path,
            size_bytes=len(data),
            sha256=sha256(data).hexdigest(),
            data=data,
        )
        created_at = execution.evidence.created_at
        artifact = ArtifactRecord.create(
            run=self.run,
            logical_path=prepared.output_logical_path,
            size_bytes=len(data),
            sha256_digest=snapshot.sha256,
            media_type="application/json",
            artifact_id=_artifact_id(self.run.run_id),
            created_at=created_at,
        )
        rebound_metric = MetricRecord.create(
            run=self.run,
            name="renamed_score",
            value=6,
            unit="points",
            metric_id=_metric_id(self.run.run_id, "renamed_score"),
            created_at=created_at,
        )
        self.lab.register_artifact(artifact)
        self.lab.register_metric(rebound_metric)
        forged = PhysicalEvidenceReceipt.create(
            run=self.run,
            execution_evidence=execution.evidence,
            snapshot=snapshot,
            artifact=artifact,
            metric=rebound_metric,
        )

        with self.assertRaises(EvidenceBindingError):
            self.physical.publish(forged)


if __name__ == "__main__":
    unittest.main()
