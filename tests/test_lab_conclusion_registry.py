from __future__ import annotations

import hashlib
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
    ConclusionVerdict,
    ExperimentManifest,
    ExperimentRun,
    GovernedPythonJsonRunner,
    Hypothesis,
    InvalidLabRecordError,
    LabPersistenceIntegrityError,
    PYTHON_JSON_PROFILE,
    SqliteAdjudicatedConclusionRegistry,
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


class DurableAdjudicatedConclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.db = self.root / "conclusion.sqlite3"
        self._build_runtime()

    def _build_runtime(self) -> None:
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

    def _fresh_conclusions(self) -> SqliteAdjudicatedConclusionRegistry:
        lab = SqliteLabRegistry(self.db)
        m3 = SqliteSessionEventStore(self.db)
        executions = SqliteRunExecutionRegistry(lab, m3)
        physical = SqlitePhysicalEvidenceRegistry(lab, executions)
        comparisons = SqliteComparisonRegistry(lab)
        results = SqliteComparisonResultRegistry(comparisons, lab, physical)
        return SqliteAdjudicatedConclusionRegistry(comparisons, results, lab)

    def _session(self) -> str:
        session_id = str(uuid4())
        self.m3.start_session(
            session_id=session_id,
            payload={
                "workspace": str(self.workspace),
                "prompt_version": "m4.5.2-test",
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
            actor="m4.5.2-test-human",
        )

    @staticmethod
    def _manifest(hypothesis: Hypothesis, value: int) -> ExperimentManifest:
        return ExperimentManifest.create(
            hypothesis=hypothesis,
            procedure="Emit one deterministic governed score for conclusion recovery.",
            parameters={
                "profile": PYTHON_JSON_PROFILE,
                "source": RESULT_SOURCE,
                "input": {"value": value},
                "metric": {"name": "score", "unit": "points"},
            },
        )

    def _execute(self, manifest: ExperimentManifest, seed: int) -> ExperimentRun:
        run = ExperimentRun.create(manifest=manifest, ordinal=0, seed=seed)
        self.lab.register_run(run)
        prepared = self.runner.prepare(
            run_id=run.run_id,
            m3_session_id=self._session(),
            workspace=self.workspace,
        )
        executed = self.runner.execute_authorized(
            prepared,
            receipt=self._approve(prepared),
        )
        self.assertIsNotNone(executed.physical)
        self.lab.seal_run(run.run_id, run.run_digest)
        return run

    def _complete_refuted_comparison(self):
        hypothesis = Hypothesis.create(
            statement="The candidate improves score by at least two points.",
            falsification_criterion="The frozen comparison effect is below two points.",
        )
        baseline = self._manifest(hypothesis, 10)
        candidate = self._manifest(hypothesis, 9)
        self.lab.register_experiment(hypothesis, baseline)
        self.lab.register_experiment(hypothesis, candidate)
        policy = ComparisonPolicy.create(
            hypothesis=hypothesis,
            baseline_manifest=baseline,
            candidate_manifest=candidate,
            metric_name="score",
            metric_unit="points",
            direction=ComparisonDirection.LOWER_IS_BETTER,
            minimum_effect=2,
            seeds=(17,),
            missing_run_policy=ComparisonMissingPolicy.INCONCLUSIVE,
        )
        frozen = self.comparisons.register_policy(policy)
        self._execute(baseline, 17)
        self._execute(candidate, 17)
        self.lab.seal_experiment(baseline.experiment_id, baseline.manifest_digest)
        self.lab.seal_experiment(candidate.experiment_id, candidate.manifest_digest)
        result = self.results.evaluate(policy.policy_id)
        self.assertEqual(result.outcome, ComparisonOutcome.REFUTED)
        return frozen, result

    def test_publish_and_fresh_restart_recover_exact_authoritative_conclusion(self) -> None:
        frozen, result = self._complete_refuted_comparison()
        published = self.conclusions.publish(frozen.policy.policy_id)

        self.assertEqual(published.result_digest, result.result_digest)
        self.assertEqual(published.verdict, ConclusionVerdict.REFUTED)
        recovered = self._fresh_conclusions().recover(frozen.policy.policy_id)
        self.assertEqual(recovered.to_dict(), published.to_dict())

    def test_publish_requires_a_durable_comparison_result(self) -> None:
        hypothesis = Hypothesis.create(
            statement="A comparison exists.",
            falsification_criterion="No durable comparison result exists.",
        )
        baseline = self._manifest(hypothesis, 10)
        candidate = self._manifest(hypothesis, 9)
        self.lab.register_experiment(hypothesis, baseline)
        self.lab.register_experiment(hypothesis, candidate)
        policy = ComparisonPolicy.create(
            hypothesis=hypothesis,
            baseline_manifest=baseline,
            candidate_manifest=candidate,
            metric_name="score",
            metric_unit="points",
            direction=ComparisonDirection.LOWER_IS_BETTER,
            minimum_effect=2,
            seeds=(17,),
        )
        self.comparisons.register_policy(policy)

        with self.assertRaises(InvalidLabRecordError):
            self.conclusions.publish(policy.policy_id)

    def test_persisted_summary_tamper_fails_closed(self) -> None:
        frozen, _result = self._complete_refuted_comparison()
        self.conclusions.publish(frozen.policy.policy_id)
        with closing(sqlite3.connect(self.db)) as connection:
            raw = connection.execute(
                "SELECT payload_json FROM lab_adjudicated_conclusions WHERE policy_id = ?",
                (frozen.policy.policy_id,),
            ).fetchone()[0]
            payload = json.loads(raw)
            payload["summary"] = "The hypothesis is universally false."
            connection.execute(
                "UPDATE lab_adjudicated_conclusions SET payload_json = ? WHERE policy_id = ?",
                (
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    frozen.policy.policy_id,
                ),
            )
            connection.commit()

        with self.assertRaises(LabPersistenceIntegrityError):
            self._fresh_conclusions().recover(frozen.policy.policy_id)

    def test_index_tamper_fails_closed(self) -> None:
        frozen, _result = self._complete_refuted_comparison()
        self.conclusions.publish(frozen.policy.policy_id)
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(
                "UPDATE lab_adjudicated_conclusions SET conclusion_digest = ? WHERE policy_id = ?",
                ("0" * 64, frozen.policy.policy_id),
            )
            connection.commit()

        with self.assertRaises(LabPersistenceIntegrityError):
            self._fresh_conclusions().recover(frozen.policy.policy_id)

    def test_self_consistent_forged_verdict_is_rejected_by_authoritative_recomputation(self) -> None:
        frozen, result = self._complete_refuted_comparison()
        original = self.conclusions.publish(frozen.policy.policy_id)
        payload = original.to_dict()
        payload["comparison_outcome"] = ComparisonOutcome.SUPPORTED.value
        payload["verdict"] = ConclusionVerdict.SUPPORTED.value
        payload["summary"] = (
            "The verified evidence satisfies the exact frozen comparison policy for "
            "this hypothesis; support is limited to that declared policy and evidence."
        )
        unsigned = dict(payload)
        unsigned.pop("conclusion_digest")
        encoded_unsigned = json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        forged_digest = hashlib.sha256(encoded_unsigned.encode("utf-8")).hexdigest()
        payload["conclusion_digest"] = forged_digest
        forged_raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

        self.assertEqual(original.result_digest, result.result_digest)
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(
                """
                UPDATE lab_adjudicated_conclusions
                SET conclusion_digest = ?, payload_json = ?
                WHERE policy_id = ?
                """,
                (forged_digest, forged_raw, frozen.policy.policy_id),
            )
            connection.commit()

        with self.assertRaises(LabPersistenceIntegrityError):
            self._fresh_conclusions().recover(frozen.policy.policy_id)

    def test_physical_evidence_mutation_invalidates_conclusion_recovery_transitively(self) -> None:
        frozen, result = self._complete_refuted_comparison()
        self.conclusions.publish(frozen.policy.policy_id)
        entry = result.baseline_evidence[0]
        physical = self.physical.recover(entry.run_id)
        output = self.workspace / physical.artifact.logical_path
        output.write_bytes(b"tampered")

        with self.assertRaises(LabPersistenceIntegrityError):
            self._fresh_conclusions().recover(frozen.policy.policy_id)


if __name__ == "__main__":
    unittest.main()
