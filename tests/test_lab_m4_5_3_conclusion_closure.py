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
    ComparisonDirection,
    ComparisonMissingPolicy,
    ComparisonOutcome,
    ComparisonPolicy,
    ConclusionScope,
    ConclusionVerdict,
    ExperimentManifest,
    ExperimentRun,
    GovernedPythonJsonRunner,
    Hypothesis,
    PYTHON_JSON_PROFILE,
    SqliteAdjudicatedConclusionRegistry,
    SqliteComparisonRegistry,
    SqliteComparisonResultRegistry,
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
        SqliteAdjudicatedConclusionRegistry,
        SqliteComparisonRegistry,
        SqliteComparisonResultRegistry,
        SqliteLabRegistry,
        SqlitePhysicalEvidenceRegistry,
        SqliteRunExecutionRegistry,
    )
    from codexia_manual_agent.session_events import SqliteSessionEventStore

    db = Path(sys.argv[1]).resolve()
    policy_id = sys.argv[2]

    lab = SqliteLabRegistry(db)
    m3 = SqliteSessionEventStore(db)
    executions = SqliteRunExecutionRegistry(lab, m3)
    physical = SqlitePhysicalEvidenceRegistry(lab, executions)
    comparisons = SqliteComparisonRegistry(lab)
    results = SqliteComparisonResultRegistry(comparisons, lab, physical)
    conclusions = SqliteAdjudicatedConclusionRegistry(comparisons, results, lab)

    frozen = comparisons.recover_policy(policy_id)
    result = results.recover_result(policy_id)
    conclusion = conclusions.recover(policy_id)

    print(json.dumps({
        "policy_id": frozen.policy.policy_id,
        "policy_digest": frozen.policy.policy_digest,
        "freeze_digest": frozen.freeze_digest,
        "result_id": result.result_id,
        "result_digest": result.result_digest,
        "outcome": result.outcome.value,
        "conclusion_id": conclusion.conclusion_id,
        "conclusion_digest": conclusion.conclusion_digest,
        "scope": conclusion.scope.value,
        "verdict": conclusion.verdict.value,
        "summary": conclusion.summary,
        "baseline_manifest_digest": conclusion.baseline_manifest_digest,
        "candidate_manifest_digest": conclusion.candidate_manifest_digest,
    }, sort_keys=True, separators=(",", ":")))
    """
).strip()


REFUTED_SUMMARY = (
    "The verified evidence does not satisfy the exact frozen comparison policy "
    "for this hypothesis; refutation is limited to that declared policy and evidence."
)


class M453FirstRealConclusionClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.db = self.root / "m4-5-3.sqlite3"

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
        self.comparisons = SqliteComparisonRegistry(self.lab)
        self.results = SqliteComparisonResultRegistry(
            self.comparisons,
            self.lab,
            self.physical,
        )
        self.conclusions = SqliteAdjudicatedConclusionRegistry(
            self.comparisons,
            self.results,
            self.lab,
        )

    @staticmethod
    def _fixture_source() -> str:
        path = (
            Path(__file__).with_name("fixtures")
            / "m4_4_3_integration_error_experiment.py"
        )
        return path.read_text(encoding="utf-8").strip()

    def _session(self) -> str:
        session_id = str(uuid4())
        self.m3.start_session(
            session_id=session_id,
            payload={
                "workspace": str(self.workspace),
                "prompt_version": "m4.5.3-conclusion-closure",
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
            actor="m4.5.3-test-human",
        )

    def _manifest(
        self,
        hypothesis: Hypothesis,
        *,
        method: str,
    ) -> ExperimentManifest:
        return ExperimentManifest.create(
            hypothesis=hypothesis,
            procedure=(
                "Approximate integral_0^1 x^2 dx with n=8 and emit the absolute "
                "error numerator over the common denominator 6*n^3."
            ),
            parameters={
                "profile": PYTHON_JSON_PROFILE,
                "source": self._fixture_source(),
                "input": {"method": method, "n": 8},
                "metric": {
                    "name": "integration_error_numerator",
                    "unit": "over_6n_cubed",
                },
            },
        )

    def _execute_and_seal_run(
        self,
        manifest: ExperimentManifest,
        *,
        ordinal: int,
        seed: int,
    ) -> ExperimentRun:
        run = ExperimentRun.create(manifest=manifest, ordinal=ordinal, seed=seed)
        self.lab.register_run(run)
        prepared = self.runner.prepare(
            run_id=run.run_id,
            m3_session_id=self._session(),
            workspace=self.workspace,
        )
        execution = self.runner.execute_authorized(
            prepared,
            receipt=self._approve(prepared),
        )
        self.assertIsNotNone(execution.physical)
        self.lab.seal_run(run.run_id, run.run_digest)
        return run

    def test_real_refuted_comparison_becomes_bounded_conclusion_and_recovers_without_replay(self) -> None:
        hypothesis = Hypothesis.create(
            statement=(
                "For n=8, trapezoid integration reduces the exact common-denominator "
                "error numerator by at least 180 relative to left Riemann."
            ),
            falsification_criterion=(
                "The frozen lower-is-better comparison effect is less than 180."
            ),
        )
        baseline = self._manifest(hypothesis, method="left")
        candidate = self._manifest(hypothesis, method="trapezoid")
        self.lab.register_experiment(hypothesis, baseline)
        self.lab.register_experiment(hypothesis, candidate)

        seeds = (101, 202)
        policy = ComparisonPolicy.create(
            hypothesis=hypothesis,
            baseline_manifest=baseline,
            candidate_manifest=candidate,
            metric_name="integration_error_numerator",
            metric_unit="over_6n_cubed",
            direction=ComparisonDirection.LOWER_IS_BETTER,
            minimum_effect=180,
            seeds=seeds,
            missing_run_policy=ComparisonMissingPolicy.ERROR,
        )
        frozen = self.comparisons.register_policy(policy)

        baseline_runs = tuple(
            self._execute_and_seal_run(baseline, ordinal=index, seed=seed)
            for index, seed in enumerate(seeds)
        )
        candidate_runs = tuple(
            self._execute_and_seal_run(candidate, ordinal=index, seed=seed)
            for index, seed in enumerate(seeds)
        )
        self.lab.seal_experiment(baseline.experiment_id, baseline.manifest_digest)
        self.lab.seal_experiment(candidate.experiment_id, candidate.manifest_digest)

        result = self.results.evaluate(frozen.policy.policy_id)
        self.assertEqual(result.outcome, ComparisonOutcome.REFUTED)
        self.assertEqual(result.baseline_mean, "184")
        self.assertEqual(result.candidate_mean, "8")
        self.assertEqual(result.effect, "176")

        conclusion = self.conclusions.publish(frozen.policy.policy_id)
        self.assertEqual(conclusion.scope, ConclusionScope.FROZEN_COMPARISON_POLICY_V1)
        self.assertEqual(conclusion.comparison_outcome, ComparisonOutcome.REFUTED)
        self.assertEqual(conclusion.verdict, ConclusionVerdict.REFUTED)
        self.assertEqual(conclusion.summary, REFUTED_SUMMARY)
        self.assertEqual(conclusion.result_id, result.result_id)
        self.assertEqual(conclusion.result_digest, result.result_digest)
        self.assertEqual(conclusion.policy_id, frozen.policy.policy_id)
        self.assertEqual(conclusion.policy_digest, frozen.policy.policy_digest)
        self.assertEqual(conclusion.freeze_digest, frozen.freeze_digest)
        self.assertEqual(conclusion.baseline_manifest_digest, baseline.manifest_digest)
        self.assertEqual(conclusion.candidate_manifest_digest, candidate.manifest_digest)

        all_runs = baseline_runs + candidate_runs
        physical_before = {}
        for run in all_runs:
            evidence = self.physical.recover(run.run_id)
            path = self.workspace / evidence.artifact.logical_path
            physical_before[run.run_id] = (
                path,
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                RECOVERY_PROGRAM,
                str(self.db),
                frozen.policy.policy_id,
            ],
            cwd=self.workspace,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        recovered = json.loads(completed.stdout)

        self.assertEqual(recovered["policy_id"], frozen.policy.policy_id)
        self.assertEqual(recovered["policy_digest"], frozen.policy.policy_digest)
        self.assertEqual(recovered["freeze_digest"], frozen.freeze_digest)
        self.assertEqual(recovered["result_id"], result.result_id)
        self.assertEqual(recovered["result_digest"], result.result_digest)
        self.assertEqual(recovered["outcome"], ComparisonOutcome.REFUTED.value)
        self.assertEqual(recovered["conclusion_id"], conclusion.conclusion_id)
        self.assertEqual(recovered["conclusion_digest"], conclusion.conclusion_digest)
        self.assertEqual(
            recovered["scope"],
            ConclusionScope.FROZEN_COMPARISON_POLICY_V1.value,
        )
        self.assertEqual(recovered["verdict"], ConclusionVerdict.REFUTED.value)
        self.assertEqual(recovered["summary"], REFUTED_SUMMARY)
        self.assertEqual(
            recovered["baseline_manifest_digest"],
            baseline.manifest_digest,
        )
        self.assertEqual(
            recovered["candidate_manifest_digest"],
            candidate.manifest_digest,
        )

        for path, before_bytes, before_mtime in physical_before.values():
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(path.stat().st_mtime_ns, before_mtime)


if __name__ == "__main__":
    unittest.main()
