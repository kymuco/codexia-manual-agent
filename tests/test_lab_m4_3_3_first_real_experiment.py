from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from uuid import uuid4

from codexia_manual_agent.authority import ApprovalMode, LocalApprovalAuthority
from codexia_manual_agent.lab import (
    ExperimentManifest,
    ExperimentRun,
    GovernedPythonJsonRunner,
    Hypothesis,
    LabRegistryStateError,
    PYTHON_JSON_PROFILE,
    SqliteLabRegistry,
    SqlitePhysicalEvidenceRegistry,
    SqliteRunExecutionRegistry,
)
from codexia_manual_agent.session_events import SqliteSessionEventStore


RECOVERY_PROGRAM = textwrap.dedent(
    r"""
    import json
    import sys
    from pathlib import Path

    from codexia_manual_agent.lab import (
        SqliteLabRegistry,
        SqlitePhysicalEvidenceRegistry,
        SqliteRunExecutionRegistry,
    )
    from codexia_manual_agent.session_events import SqliteSessionEventStore

    db = Path(sys.argv[1]).resolve()
    run_id = sys.argv[2]

    lab = SqliteLabRegistry(db)
    m3 = SqliteSessionEventStore(db)
    executions = SqliteRunExecutionRegistry(lab, m3)
    physical = SqlitePhysicalEvidenceRegistry(lab, executions)

    recovered = lab.recover_for_run(run_id)
    run = recovered.run(run_id)
    execution = executions.recover(run_id)
    evidence = physical.recover(run_id)
    if execution.evidence is None:
        raise RuntimeError("missing execution evidence")

    print(json.dumps({
        "experiment_id": recovered.experiment_id,
        "hypothesis_digest": recovered.hypothesis.hypothesis_digest,
        "manifest_digest": recovered.manifest.manifest_digest,
        "run_digest": run.run.run_digest,
        "execution_evidence_digest": execution.evidence.evidence_digest,
        "physical_receipt_digest": evidence.receipt.receipt_digest,
        "artifact_sha256": evidence.artifact.sha256,
        "metric_name": evidence.metric.name,
        "metric_value": evidence.metric.value,
        "run_sealed": run.evidence_sealed,
        "experiment_sealed": recovered.experiment_sealed,
    }, sort_keys=True, separators=(",", ":")))
    """
).strip()


class M433FirstRealExperimentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.db = self.root / "m4-3-3.sqlite3"

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

    @staticmethod
    def _fixture_source() -> str:
        path = Path(__file__).with_name("fixtures") / "m4_3_3_sum_odds_experiment.py"
        return path.read_text(encoding="utf-8").strip()

    def _register_real_experiment(self) -> tuple[Hypothesis, ExperimentManifest, ExperimentRun]:
        hypothesis = Hypothesis.create(
            statement=(
                "For n=37, the sum of the first 37 positive odd integers equals 37 squared."
            ),
            falsification_criterion=(
                "The governed run yields absolute_error != 0 for the declared input n=37."
            ),
        )
        manifest = ExperimentManifest.create(
            hypothesis=hypothesis,
            procedure=(
                "Compute the first n positive odd integers and n squared in one governed "
                "Python run, then emit the absolute difference as canonical JSON."
            ),
            parameters={
                "profile": PYTHON_JSON_PROFILE,
                "source": self._fixture_source(),
                "input": {"n": 37},
                "metric": {"name": "absolute_error", "unit": "integer"},
            },
        )
        run = ExperimentRun.create(manifest=manifest, ordinal=0, seed=20260907)
        self.lab.register_experiment(hypothesis, manifest)
        self.lab.register_run(run)
        return hypothesis, manifest, run

    def _start_session(self, prompt_version: str) -> str:
        session_id = str(uuid4())
        self.m3.start_session(
            session_id=session_id,
            payload={
                "workspace": str(self.workspace),
                "prompt_version": prompt_version,
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
            actor="m4.3.3-test-human",
        )

    def _execute_and_seal(self):
        hypothesis, manifest, run = self._register_real_experiment()
        prepared = self.runner.prepare(
            run_id=run.run_id,
            m3_session_id=self._start_session("m4.3.3-first-real-experiment"),
            workspace=self.workspace,
        )
        result = self.runner.execute_authorized(
            prepared,
            receipt=self._approve(prepared),
        )
        self.assertIsNotNone(result.physical)
        assert result.physical is not None
        self.assertEqual(result.physical.metric.name, "absolute_error")
        self.assertEqual(result.physical.metric.value, 0)
        self.assertEqual(result.physical.metric.unit, "integer")

        self.lab.seal_run(run.run_id, run.run_digest)
        self.lab.seal_experiment(manifest.experiment_id, manifest.manifest_digest)
        return hypothesis, manifest, run, prepared, result

    def test_first_real_experiment_recovers_sealed_chain_in_fresh_process_without_replay(self) -> None:
        hypothesis, manifest, run, _prepared, result = self._execute_and_seal()
        assert result.physical is not None
        output = self.workspace / result.physical.artifact.logical_path
        before_bytes = output.read_bytes()
        before_mtime = output.stat().st_mtime_ns

        completed = subprocess.run(
            [sys.executable, "-c", RECOVERY_PROGRAM, str(self.db), run.run_id],
            cwd=self.workspace,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        summary = json.loads(completed.stdout)

        self.assertEqual(summary["experiment_id"], manifest.experiment_id)
        self.assertEqual(summary["hypothesis_digest"], hypothesis.hypothesis_digest)
        self.assertEqual(summary["manifest_digest"], manifest.manifest_digest)
        self.assertEqual(summary["run_digest"], run.run_digest)
        self.assertEqual(
            summary["execution_evidence_digest"],
            result.execution.evidence.evidence_digest,
        )
        self.assertEqual(
            summary["physical_receipt_digest"],
            result.physical.receipt.receipt_digest,
        )
        self.assertEqual(summary["artifact_sha256"], result.physical.artifact.sha256)
        self.assertEqual(summary["metric_name"], "absolute_error")
        self.assertEqual(summary["metric_value"], 0)
        self.assertTrue(summary["run_sealed"])
        self.assertTrue(summary["experiment_sealed"])
        self.assertEqual(output.read_bytes(), before_bytes)
        self.assertEqual(output.stat().st_mtime_ns, before_mtime)

    def test_sealed_real_run_cannot_be_reprepared_or_replayed(self) -> None:
        _hypothesis, _manifest, run, _prepared, _result = self._execute_and_seal()
        replay_session = self._start_session("m4.3.3-replay-attempt")

        fresh_lab = SqliteLabRegistry(self.db)
        fresh_m3 = SqliteSessionEventStore(self.db)
        fresh_executions = SqliteRunExecutionRegistry(fresh_lab, fresh_m3)
        fresh_physical = SqlitePhysicalEvidenceRegistry(fresh_lab, fresh_executions)
        fresh_runner = GovernedPythonJsonRunner(
            fresh_lab,
            fresh_m3,
            fresh_executions,
            fresh_physical,
        )

        with self.assertRaises(LabRegistryStateError):
            fresh_runner.prepare(
                run_id=run.run_id,
                m3_session_id=replay_session,
                workspace=self.workspace,
            )

        self.assertFalse(fresh_m3.recover(replay_session).actions)


if __name__ == "__main__":
    unittest.main()
