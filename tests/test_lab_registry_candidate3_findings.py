from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.lab import (
    ArtifactRecord,
    ExperimentManifest,
    ExperimentRun,
    Hypothesis,
    LabPersistenceIntegrityError,
    MetricRecord,
    SqliteLabRegistry,
)


_FIRST_EXPERIMENT_ID = "11111111-1111-4111-8111-111111111111"
_SECOND_EXPERIMENT_ID = "22222222-2222-4222-8222-222222222222"
_THIRD_EXPERIMENT_ID = "abcdef33-3333-4333-8333-333333333333"
_ORPHAN_RUN_ID = "44444444-4444-4444-8444-444444444444"
_ORPHAN_METRIC_ID = "55555555-5555-4555-8555-555555555555"
_ORPHAN_ARTIFACT_ID = "66666666-6666-4666-8666-666666666666"
_ORPHAN_EVENT_ID = "77777777-7777-4777-8777-777777777777"
_CANONICAL_RECORD_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"


class DurableLabRegistryCandidate3FindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.hypothesis = Hypothesis.create(
            statement="Candidate 3 admission rejects orphan durable ownership.",
            falsification_criterion="A new experiment can adopt pre-existing orphan rows.",
        )

    @staticmethod
    def _execute(
        db_path: Path,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> None:
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(sql, parameters)
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _root_count(db_path: Path, experiment_id: str) -> int:
        connection = sqlite3.connect(db_path)
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM lab_registry_experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            assert row is not None
            return int(row[0])
        finally:
            connection.close()

    def test_new_experiment_cannot_adopt_orphan_namespace_rows(self) -> None:
        cases = (
            (
                "event",
                """
                INSERT INTO lab_registry_events(
                    experiment_id, sequence, event_id, created_at, kind,
                    payload_json, previous_event_digest, event_digest
                ) VALUES (?, 0, ?, ?, 'run_registered', '{}', NULL, ?)
                """,
                (
                    _ORPHAN_EVENT_ID,
                    "2026-08-28T00:00:00+00:00",
                    "a" * 64,
                ),
            ),
            (
                "run",
                """
                INSERT INTO lab_registry_runs(
                    run_id, experiment_id, run_digest, manifest_digest, ordinal
                ) VALUES (?, ?, ?, ?, 0)
                """,
                (_ORPHAN_RUN_ID, "b" * 64, "c" * 64),
            ),
            (
                "metric",
                """
                INSERT INTO lab_registry_metrics(
                    metric_id, experiment_id, run_id, run_digest,
                    manifest_digest, metric_digest, name
                ) VALUES (?, ?, ?, ?, ?, ?, 'orphan_metric')
                """,
                (
                    _ORPHAN_METRIC_ID,
                    _ORPHAN_RUN_ID,
                    "d" * 64,
                    "e" * 64,
                    "f" * 64,
                ),
            ),
            (
                "artifact",
                """
                INSERT INTO lab_registry_artifacts(
                    artifact_id, experiment_id, run_id, run_digest,
                    manifest_digest, artifact_digest, logical_path
                ) VALUES (?, ?, ?, ?, ?, ?, 'results/orphan.json')
                """,
                (
                    _ORPHAN_ARTIFACT_ID,
                    _ORPHAN_RUN_ID,
                    "1" * 64,
                    "2" * 64,
                    "3" * 64,
                ),
            ),
        )

        for label, sql, suffix in cases:
            with self.subTest(label=label):
                db_path = Path(self.tempdir.name) / f"orphan-namespace-{label}.sqlite3"
                store = SqliteLabRegistry(db_path)
                manifest = ExperimentManifest.create(
                    hypothesis=self.hypothesis,
                    procedure=f"Reject orphan {label} ownership.",
                    experiment_id=_THIRD_EXPERIMENT_ID,
                )
                if label == "event":
                    parameters = (
                        manifest.experiment_id,
                        suffix[0],
                        suffix[1],
                        suffix[2],
                    )
                else:
                    parameters = (suffix[0], manifest.experiment_id, *suffix[1:])
                self._execute(db_path, sql, parameters)

                with self.assertRaises(LabPersistenceIntegrityError):
                    store.register_experiment(self.hypothesis, manifest)

                self.assertEqual(
                    self._root_count(db_path, manifest.experiment_id),
                    0,
                )

    def test_register_experiment_audits_every_existing_hypothesis_owner(self) -> None:
        db_path = Path(self.tempdir.name) / "hypothesis-owner-audit.sqlite3"
        store = SqliteLabRegistry(db_path)
        first = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="First valid hypothesis owner.",
            experiment_id=_FIRST_EXPERIMENT_ID,
        )
        second = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Second owner becomes corrupt.",
            experiment_id=_SECOND_EXPERIMENT_ID,
        )
        third = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Third owner must not bypass the corrupt second owner.",
            experiment_id=_THIRD_EXPERIMENT_ID,
        )
        store.register_experiment(self.hypothesis, first)
        store.register_experiment(self.hypothesis, second)
        self._execute(
            db_path,
            """
            UPDATE lab_registry_experiments
            SET head_event_digest = ?
            WHERE experiment_id = ?
            """,
            ("0" * 64, second.experiment_id),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            store.register_experiment(self.hypothesis, third)

        self.assertEqual(self._root_count(db_path, third.experiment_id), 0)

    def test_semantic_aliases_in_derived_global_ids_fail_closed(self) -> None:
        aliases = (
            ("uppercase", _CANONICAL_RECORD_ID.upper()),
            ("unhyphenated", _CANONICAL_RECORD_ID.replace("-", "")),
        )
        for record_type in ("run", "metric", "artifact"):
            for alias_label, alias_id in aliases:
                with self.subTest(record_type=record_type, alias=alias_label):
                    db_path = Path(self.tempdir.name) / (
                        f"derived-alias-{record_type}-{alias_label}.sqlite3"
                    )
                    store = SqliteLabRegistry(db_path)
                    target = ExperimentManifest.create(
                        hypothesis=self.hypothesis,
                        procedure=f"Target canonical {record_type} registration.",
                        experiment_id=_FIRST_EXPERIMENT_ID,
                    )
                    owner = ExperimentManifest.create(
                        hypothesis=self.hypothesis,
                        procedure=f"Own stale aliased {record_type} index only.",
                        experiment_id=_SECOND_EXPERIMENT_ID,
                    )
                    store.register_experiment(self.hypothesis, target)
                    store.register_experiment(self.hypothesis, owner)
                    base_run = ExperimentRun.create(
                        manifest=target,
                        ordinal=0,
                        seed=50,
                    )
                    store.register_run(base_run)

                    if record_type == "run":
                        candidate = ExperimentRun.create(
                            manifest=target,
                            ordinal=1,
                            seed=51,
                            run_id=_CANONICAL_RECORD_ID,
                        )
                        self._execute(
                            db_path,
                            """
                            INSERT INTO lab_registry_runs(
                                run_id, experiment_id, run_digest,
                                manifest_digest, ordinal
                            ) VALUES (?, ?, ?, ?, 0)
                            """,
                            (
                                alias_id,
                                owner.experiment_id,
                                candidate.run_digest,
                                candidate.manifest_digest,
                            ),
                        )
                        register = lambda: store.register_run(candidate)
                        recover = lambda: store.recover_for_run(candidate.run_id)
                    elif record_type == "metric":
                        candidate = MetricRecord.create(
                            run=base_run,
                            name="candidate_metric",
                            value=0.75,
                            metric_id=_CANONICAL_RECORD_ID,
                        )
                        self._execute(
                            db_path,
                            """
                            INSERT INTO lab_registry_metrics(
                                metric_id, experiment_id, run_id, run_digest,
                                manifest_digest, metric_digest, name
                            ) VALUES (?, ?, ?, ?, ?, ?, 'stale_alias_metric')
                            """,
                            (
                                alias_id,
                                owner.experiment_id,
                                base_run.run_id,
                                candidate.run_digest,
                                candidate.manifest_digest,
                                candidate.metric_digest,
                            ),
                        )
                        register = lambda: store.register_metric(candidate)
                        recover = lambda: store.recover_for_metric(candidate.metric_id)
                    else:
                        candidate = ArtifactRecord.create(
                            run=base_run,
                            logical_path="results/candidate.json",
                            size_bytes=11,
                            sha256_digest="9" * 64,
                            media_type="application/json",
                            artifact_id=_CANONICAL_RECORD_ID,
                        )
                        self._execute(
                            db_path,
                            """
                            INSERT INTO lab_registry_artifacts(
                                artifact_id, experiment_id, run_id, run_digest,
                                manifest_digest, artifact_digest, logical_path
                            ) VALUES (?, ?, ?, ?, ?, ?, 'results/stale-alias.json')
                            """,
                            (
                                alias_id,
                                owner.experiment_id,
                                base_run.run_id,
                                candidate.run_digest,
                                candidate.manifest_digest,
                                candidate.artifact_digest,
                            ),
                        )
                        register = lambda: store.register_artifact(candidate)
                        recover = lambda: store.recover_for_artifact(candidate.artifact_id)

                    before = store.recover_experiment(target.experiment_id)
                    with self.assertRaises(LabPersistenceIntegrityError):
                        register()
                    after = store.recover_experiment(target.experiment_id)
                    self.assertEqual(after.events, before.events)
                    with self.assertRaises(LabPersistenceIntegrityError):
                        recover()

    def test_semantic_aliases_in_experiment_namespace_fail_closed(self) -> None:
        aliases = (
            ("uppercase", _THIRD_EXPERIMENT_ID.upper()),
            ("unhyphenated", _THIRD_EXPERIMENT_ID.replace("-", "")),
        )
        for row_kind in ("root", "event", "run", "metric", "artifact"):
            for alias_label, alias_id in aliases:
                with self.subTest(row_kind=row_kind, alias=alias_label):
                    db_path = Path(self.tempdir.name) / (
                        f"experiment-alias-{row_kind}-{alias_label}.sqlite3"
                    )
                    store = SqliteLabRegistry(db_path)
                    manifest = ExperimentManifest.create(
                        hypothesis=self.hypothesis,
                        procedure="Reject semantic experiment namespace alias.",
                        experiment_id=_THIRD_EXPERIMENT_ID,
                    )
                    if row_kind == "root":
                        self._execute(
                            db_path,
                            """
                            INSERT INTO lab_registry_experiments(
                                experiment_id, hypothesis_id, hypothesis_digest,
                                manifest_digest, head_sequence, head_event_digest,
                                registered_at
                            ) VALUES (?, ?, ?, ?, 0, ?, ?)
                            """,
                            (
                                alias_id,
                                self.hypothesis.hypothesis_id,
                                self.hypothesis.hypothesis_digest,
                                manifest.manifest_digest,
                                "a" * 64,
                                "2026-08-28T00:00:00+00:00",
                            ),
                        )
                    elif row_kind == "event":
                        self._execute(
                            db_path,
                            """
                            INSERT INTO lab_registry_events(
                                experiment_id, sequence, event_id, created_at,
                                kind, payload_json, previous_event_digest, event_digest
                            ) VALUES (?, 0, ?, ?, 'run_registered', '{}', NULL, ?)
                            """,
                            (
                                alias_id,
                                _ORPHAN_EVENT_ID,
                                "2026-08-28T00:00:00+00:00",
                                "b" * 64,
                            ),
                        )
                    elif row_kind == "run":
                        self._execute(
                            db_path,
                            """
                            INSERT INTO lab_registry_runs(
                                run_id, experiment_id, run_digest,
                                manifest_digest, ordinal
                            ) VALUES (?, ?, ?, ?, 0)
                            """,
                            (_ORPHAN_RUN_ID, alias_id, "c" * 64, "d" * 64),
                        )
                    elif row_kind == "metric":
                        self._execute(
                            db_path,
                            """
                            INSERT INTO lab_registry_metrics(
                                metric_id, experiment_id, run_id, run_digest,
                                manifest_digest, metric_digest, name
                            ) VALUES (?, ?, ?, ?, ?, ?, 'experiment_alias_metric')
                            """,
                            (
                                _ORPHAN_METRIC_ID,
                                alias_id,
                                _ORPHAN_RUN_ID,
                                "e" * 64,
                                "f" * 64,
                                "1" * 64,
                            ),
                        )
                    else:
                        self._execute(
                            db_path,
                            """
                            INSERT INTO lab_registry_artifacts(
                                artifact_id, experiment_id, run_id, run_digest,
                                manifest_digest, artifact_digest, logical_path
                            ) VALUES (?, ?, ?, ?, ?, ?, 'results/experiment-alias.json')
                            """,
                            (
                                _ORPHAN_ARTIFACT_ID,
                                alias_id,
                                _ORPHAN_RUN_ID,
                                "2" * 64,
                                "3" * 64,
                                "4" * 64,
                            ),
                        )

                    with self.assertRaises(LabPersistenceIntegrityError):
                        store.register_experiment(self.hypothesis, manifest)
                    self.assertEqual(self._root_count(db_path, manifest.experiment_id), 0)

    def test_semantic_aliases_in_hypothesis_identity_fail_closed(self) -> None:
        aliases = (
            ("uppercase", self.hypothesis.hypothesis_id.upper()),
            ("unhyphenated", self.hypothesis.hypothesis_id.replace("-", "")),
        )
        for location in ("index", "root"):
            for alias_label, alias_id in aliases:
                with self.subTest(location=location, alias=alias_label):
                    db_path = Path(self.tempdir.name) / (
                        f"hypothesis-alias-{location}-{alias_label}.sqlite3"
                    )
                    store = SqliteLabRegistry(db_path)
                    candidate = ExperimentManifest.create(
                        hypothesis=self.hypothesis,
                        procedure="Reject semantic hypothesis identity alias.",
                        experiment_id=_SECOND_EXPERIMENT_ID,
                    )
                    if location == "index":
                        self._execute(
                            db_path,
                            """
                            INSERT INTO lab_registry_hypotheses(
                                hypothesis_id, hypothesis_digest
                            ) VALUES (?, ?)
                            """,
                            (alias_id, self.hypothesis.hypothesis_digest),
                        )
                    else:
                        existing = ExperimentManifest.create(
                            hypothesis=self.hypothesis,
                            procedure="Create root whose hypothesis id becomes aliased.",
                            experiment_id=_FIRST_EXPERIMENT_ID,
                        )
                        store.register_experiment(self.hypothesis, existing)
                        self._execute(
                            db_path,
                            """
                            UPDATE lab_registry_experiments
                            SET hypothesis_id = ?
                            WHERE experiment_id = ?
                            """,
                            (alias_id, existing.experiment_id),
                        )

                    with self.assertRaises(LabPersistenceIntegrityError):
                        store.register_experiment(self.hypothesis, candidate)
                    self.assertEqual(self._root_count(db_path, candidate.experiment_id), 0)

    def test_semantic_aliases_in_scoped_run_identity_fail_closed(self) -> None:
        aliases = (
            ("uppercase", lambda value: value.upper()),
            ("unhyphenated", lambda value: value.replace("-", "")),
        )
        for evidence_type in ("metric", "artifact"):
            for alias_label, alias_fn in aliases:
                with self.subTest(evidence_type=evidence_type, alias=alias_label):
                    db_path = Path(self.tempdir.name) / (
                        f"scoped-run-alias-{evidence_type}-{alias_label}.sqlite3"
                    )
                    store = SqliteLabRegistry(db_path)
                    target = ExperimentManifest.create(
                        hypothesis=self.hypothesis,
                        procedure="Own the canonical target run.",
                        experiment_id=_FIRST_EXPERIMENT_ID,
                    )
                    foreign = ExperimentManifest.create(
                        hypothesis=self.hypothesis,
                        procedure="Own only the stale scoped alias row.",
                        experiment_id=_SECOND_EXPERIMENT_ID,
                    )
                    store.register_experiment(self.hypothesis, target)
                    store.register_experiment(self.hypothesis, foreign)
                    run = ExperimentRun.create(manifest=target, ordinal=0, seed=60)
                    store.register_run(run)
                    aliased_run_id = alias_fn(run.run_id)

                    if evidence_type == "metric":
                        candidate = MetricRecord.create(
                            run=run,
                            name="scoped_alias_metric",
                            value=0.5,
                        )
                        self._execute(
                            db_path,
                            """
                            INSERT INTO lab_registry_metrics(
                                metric_id, experiment_id, run_id, run_digest,
                                manifest_digest, metric_digest, name
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                _ORPHAN_METRIC_ID,
                                foreign.experiment_id,
                                aliased_run_id,
                                candidate.run_digest,
                                candidate.manifest_digest,
                                "5" * 64,
                                candidate.name,
                            ),
                        )
                        register = lambda: store.register_metric(candidate)
                    else:
                        candidate = ArtifactRecord.create(
                            run=run,
                            logical_path="results/scoped-alias.json",
                            size_bytes=12,
                            sha256_digest="6" * 64,
                            media_type="application/json",
                        )
                        self._execute(
                            db_path,
                            """
                            INSERT INTO lab_registry_artifacts(
                                artifact_id, experiment_id, run_id, run_digest,
                                manifest_digest, artifact_digest, logical_path
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                _ORPHAN_ARTIFACT_ID,
                                foreign.experiment_id,
                                aliased_run_id,
                                candidate.run_digest,
                                candidate.manifest_digest,
                                "7" * 64,
                                candidate.logical_path,
                            ),
                        )
                        register = lambda: store.register_artifact(candidate)

                    before = store.recover_experiment(target.experiment_id)
                    with self.assertRaises(LabPersistenceIntegrityError):
                        register()
                    after = store.recover_experiment(target.experiment_id)
                    self.assertEqual(after.events, before.events)


if __name__ == "__main__":
    unittest.main()
