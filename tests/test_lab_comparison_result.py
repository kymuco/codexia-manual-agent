from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from codexia_manual_agent.authority import ApprovalMode, LocalApprovalAuthority
from codexia_manual_agent.lab import (
    ComparisonDirection,
    ComparisonMissingPolicy,
    ComparisonOutcome,
    ComparisonPolicy,
    EvidenceBindingError,
    ExperimentManifest,
    ExperimentRun,
    GovernedPythonJsonRunner,
    Hypothesis,
    InvalidLabRecordError,
    LabPersistenceIntegrityError,
    LabRegistryStateError,
    PYTHON_JSON_PROFILE,
    SqliteComparisonRegistry,
    SqliteComparisonResultRegistry,
    SqliteLabRegistry,
    SqlitePhysicalEvidenceRegistry,
    SqliteRunExecutionRegistry,
)
from codexia_manual_agent.session_events import SqliteSessionEventStore


RESULT_SOURCE = r'''
from pathlib import Path
import json
import sys

run_id, output_path, input_json = sys.argv[1:4]
value = json.loads(input_json)["value"]
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


class VerifiedComparisonResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.db = self.root / "comparison.sqlite3"

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

    def _session(self) -> str:
        session_id = str(uuid4())
        self.m3.start_session(
            session_id=session_id,
            payload={
                "workspace": str(self.workspace),
                "prompt_version": "m4.4.2-test",
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
            actor="m4.4.2-test-human",
        )

    @staticmethod
    def _manifest(hypothesis: Hypothesis, *, value: int | float) -> ExperimentManifest:
        return ExperimentManifest.create(
            hypothesis=hypothesis,
            procedure="Emit one deterministic governed score for comparison.",
            parameters={
                "profile": PYTHON_JSON_PROFILE,
                "source": RESULT_SOURCE,
                "input": {"value": value},
                "metric": {"name": "score", "unit": "points"},
            },
        )

    def _freeze(
        self,
        *,
        seeds: tuple[int, ...] = (11,),
        baseline_value: int | float = 10,
        candidate_value: int | float = 7,
        direction: ComparisonDirection = ComparisonDirection.LOWER_IS_BETTER,
        minimum_effect: int | float = 2,
        missing_policy: ComparisonMissingPolicy = ComparisonMissingPolicy.INCONCLUSIVE,
        metric_name: str = "score",
        metric_unit: str | None = "points",
    ):
        hypothesis = Hypothesis.create(
            statement="The candidate improves the frozen score by the declared threshold.",
            falsification_criterion="The frozen comparison effect is below its threshold.",
        )
        baseline = self._manifest(hypothesis, value=baseline_value)
        candidate = self._manifest(hypothesis, value=candidate_value)
        self.lab.register_experiment(hypothesis, baseline)
        self.lab.register_experiment(hypothesis, candidate)
        policy = ComparisonPolicy.create(
            hypothesis=hypothesis,
            baseline_manifest=baseline,
            candidate_manifest=candidate,
            metric_name=metric_name,
            metric_unit=metric_unit,
            direction=direction,
            minimum_effect=minimum_effect,
            seeds=seeds,
            missing_run_policy=missing_policy,
        )
        frozen = self.comparisons.register_policy(policy)
        return hypothesis, baseline, candidate, frozen

    def _run(
        self,
        manifest: ExperimentManifest,
        *,
        ordinal: int,
        seed: int,
        execute: bool,
    ) -> ExperimentRun:
        run = ExperimentRun.create(manifest=manifest, ordinal=ordinal, seed=seed)
        self.lab.register_run(run)
        if execute:
            prepared = self.runner.prepare(
                run_id=run.run_id,
                m3_session_id=self._session(),
                workspace=self.workspace,
            )
            result = self.runner.execute_authorized(
                prepared,
                receipt=self._approve(prepared),
            )
            self.assertIsNotNone(result.physical)
        self.lab.seal_run(run.run_id, run.run_digest)
        return run

    def _seal_experiments(
        self,
        baseline: ExperimentManifest,
        candidate: ExperimentManifest,
    ) -> None:
        self.lab.seal_experiment(baseline.experiment_id, baseline.manifest_digest)
        self.lab.seal_experiment(candidate.experiment_id, candidate.manifest_digest)

    def _complete(
        self,
        *,
        seeds: tuple[int, ...] = (11,),
        baseline_value: int | float = 10,
        candidate_value: int | float = 7,
        direction: ComparisonDirection = ComparisonDirection.LOWER_IS_BETTER,
        minimum_effect: int | float = 2,
    ):
        _hypothesis, baseline, candidate, frozen = self._freeze(
            seeds=seeds,
            baseline_value=baseline_value,
            candidate_value=candidate_value,
            direction=direction,
            minimum_effect=minimum_effect,
        )
        baseline_runs = []
        candidate_runs = []
        for ordinal, seed in enumerate(seeds):
            baseline_runs.append(
                self._run(baseline, ordinal=ordinal, seed=seed, execute=True)
            )
            candidate_runs.append(
                self._run(candidate, ordinal=ordinal, seed=seed, execute=True)
            )
        self._seal_experiments(baseline, candidate)
        result = self.results.evaluate(frozen.policy.policy_id)
        return baseline, candidate, frozen, tuple(baseline_runs), tuple(candidate_runs), result

    def _fresh_results(self) -> SqliteComparisonResultRegistry:
        lab = SqliteLabRegistry(self.db)
        m3 = SqliteSessionEventStore(self.db)
        executions = SqliteRunExecutionRegistry(lab, m3)
        physical = SqlitePhysicalEvidenceRegistry(lab, executions)
        comparisons = SqliteComparisonRegistry(lab)
        return SqliteComparisonResultRegistry(comparisons, lab, physical)

    def test_complete_sealed_physical_evidence_is_compared_and_recomputed_after_restart(self) -> None:
        _baseline, _candidate, frozen, _baseline_runs, _candidate_runs, result = self._complete(
            seeds=(11, 22),
            baseline_value=10,
            candidate_value=7,
        )

        self.assertEqual(result.outcome, ComparisonOutcome.SUPPORTED)
        self.assertEqual(result.baseline_mean, "10")
        self.assertEqual(result.candidate_mean, "7")
        self.assertEqual(result.effect, "3")
        self.assertEqual(tuple(item.seed for item in result.baseline_evidence), (11, 22))
        self.assertEqual(tuple(item.seed for item in result.candidate_evidence), (11, 22))
        self.assertFalse(result.missing_baseline_seeds)
        self.assertFalse(result.missing_candidate_seeds)

        recovered = self._fresh_results().recover_result(frozen.policy.policy_id)
        self.assertEqual(recovered.to_dict(), result.to_dict())

    def test_higher_is_better_uses_frozen_direction_and_can_refute_threshold(self) -> None:
        _baseline, _candidate, _frozen, _br, _cr, result = self._complete(
            baseline_value=8,
            candidate_value=9,
            direction=ComparisonDirection.HIGHER_IS_BETTER,
            minimum_effect=2,
        )
        self.assertEqual(result.baseline_mean, "8")
        self.assertEqual(result.candidate_mean, "9")
        self.assertEqual(result.effect, "1")
        self.assertEqual(result.outcome, ComparisonOutcome.REFUTED)

    def test_open_experiments_cannot_be_compared(self) -> None:
        _hypothesis, _baseline, _candidate, frozen = self._freeze()
        with self.assertRaises(LabRegistryStateError):
            self.results.evaluate(frozen.policy.policy_id)

    def test_extra_undeclared_run_is_not_silently_ignored(self) -> None:
        _hypothesis, baseline, candidate, frozen = self._freeze(seeds=(11,))
        self._run(baseline, ordinal=0, seed=11, execute=False)
        self._run(baseline, ordinal=1, seed=99, execute=False)
        self._seal_experiments(baseline, candidate)

        with self.assertRaises(EvidenceBindingError):
            self.results.evaluate(frozen.policy.policy_id)

    def test_seed_ordinal_substitution_is_rejected(self) -> None:
        _hypothesis, baseline, candidate, frozen = self._freeze(seeds=(11, 22))
        self._run(baseline, ordinal=0, seed=22, execute=False)
        self._seal_experiments(baseline, candidate)

        with self.assertRaises(EvidenceBindingError):
            self.results.evaluate(frozen.policy.policy_id)

    def test_inconclusive_missing_policy_never_computes_partial_mean(self) -> None:
        _hypothesis, baseline, candidate, frozen = self._freeze(
            seeds=(11, 22),
            missing_policy=ComparisonMissingPolicy.INCONCLUSIVE,
        )
        self._run(baseline, ordinal=0, seed=11, execute=True)
        self._run(candidate, ordinal=0, seed=11, execute=True)
        self._seal_experiments(baseline, candidate)

        result = self.results.evaluate(frozen.policy.policy_id)
        self.assertEqual(result.outcome, ComparisonOutcome.INCONCLUSIVE)
        self.assertEqual(result.missing_baseline_seeds, (22,))
        self.assertEqual(result.missing_candidate_seeds, (22,))
        self.assertEqual(len(result.baseline_evidence), 1)
        self.assertEqual(len(result.candidate_evidence), 1)
        self.assertIsNone(result.baseline_mean)
        self.assertIsNone(result.candidate_mean)
        self.assertIsNone(result.effect)

    def test_error_missing_policy_refuses_to_publish_partial_result(self) -> None:
        _hypothesis, baseline, candidate, frozen = self._freeze(
            missing_policy=ComparisonMissingPolicy.ERROR,
        )
        self._seal_experiments(baseline, candidate)

        with self.assertRaises(LabRegistryStateError):
            self.results.evaluate(frozen.policy.policy_id)
        with self.assertRaises(InvalidLabRecordError):
            self.results.recover_result(frozen.policy.policy_id)

    def test_physical_metric_cannot_be_reinterpreted_under_another_policy_metric(self) -> None:
        _hypothesis, baseline, candidate, frozen = self._freeze(
            metric_name="other_score",
            metric_unit="points",
        )
        self._run(baseline, ordinal=0, seed=11, execute=True)
        self._run(candidate, ordinal=0, seed=11, execute=True)
        self._seal_experiments(baseline, candidate)

        with self.assertRaises(EvidenceBindingError):
            self.results.evaluate(frozen.policy.policy_id)

    def test_physical_mutation_after_publication_invalidates_result_recovery(self) -> None:
        _baseline, _candidate, frozen, baseline_runs, _candidate_runs, _result = self._complete()
        physical = self.physical.recover(baseline_runs[0].run_id)
        output = self.workspace / physical.artifact.logical_path
        output.write_bytes(b"tampered")

        with self.assertRaises(LabPersistenceIntegrityError):
            self._fresh_results().recover_result(frozen.policy.policy_id)

    def test_persisted_result_tamper_is_detected_before_recomputation(self) -> None:
        _baseline, _candidate, frozen, _br, _cr, result = self._complete()
        with closing(sqlite3.connect(self.db)) as connection:
            row = connection.execute(
                "SELECT payload_json FROM lab_comparison_results WHERE policy_id = ?",
                (frozen.policy.policy_id,),
            ).fetchone()
            assert row is not None
            payload = json.loads(row[0])
            payload["effect"] = "999"
            connection.execute(
                "UPDATE lab_comparison_results SET payload_json = ? WHERE policy_id = ?",
                (
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    frozen.policy.policy_id,
                ),
            )
            connection.commit()

        self.assertEqual(result.effect, "3")
        with self.assertRaises(LabPersistenceIntegrityError):
            self._fresh_results().recover_result(frozen.policy.policy_id)

    def test_metric_numeric_representation_remains_bound_even_when_fraction_is_equal(self) -> None:
        _baseline, _candidate, _frozen, _br, _cr, result = self._complete(
            baseline_value=1.0,
            candidate_value=0,
            minimum_effect=1,
        )
        self.assertIs(type(result.baseline_evidence[0].metric_value), float)
        self.assertIs(type(result.candidate_evidence[0].metric_value), int)
        self.assertEqual(result.baseline_mean, "1")
        self.assertEqual(result.effect, "1")
        self.assertEqual(result.outcome, ComparisonOutcome.SUPPORTED)
        self.assertNotEqual(
            result.baseline_evidence[0].metric_digest,
            result.candidate_evidence[0].metric_digest,
        )


if __name__ == "__main__":
    unittest.main()
