from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.lab import (
    ExperimentManifest,
    ExperimentRun,
    Hypothesis,
    InvalidLabRecordError,
    LabPersistenceIntegrityError,
    LabRegistryEventReceipt,
    MetricRecord,
    SqliteLabRegistry,
)
from codexia_manual_agent.lab.registry import MAX_LAB_REGISTRY_RAW_JSON_CHARS


class DurableLabRegistryHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "lab-registry.sqlite3"
        self.hypothesis = Hypothesis.create(
            statement="Registry hardening preserves bounded durable replay.",
            falsification_criterion="Malformed persisted metadata is accepted.",
        )
        self.manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Exercise bounded registry recovery.",
        )
        self.run = ExperimentRun.create(manifest=self.manifest, ordinal=0, seed=11)
        self.store = SqliteLabRegistry(self.db_path)
        self.store.register_experiment(self.hypothesis, self.manifest)
        self.store.register_run(self.run)

    def _execute(self, sql: str, parameters: tuple[object, ...] = ()) -> None:
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(sql, parameters)
            connection.commit()
        finally:
            connection.close()

    def test_raw_json_is_bounded_before_json_parsing(self) -> None:
        oversized = (" " * (MAX_LAB_REGISTRY_RAW_JSON_CHARS + 1)) + "{}"
        self._execute(
            """
            UPDATE lab_registry_events
            SET payload_json = ?
            WHERE experiment_id = ? AND sequence = 1
            """,
            (oversized, self.manifest.experiment_id),
        )

        with self.assertRaisesRegex(
            LabPersistenceIntegrityError,
            "pre-parse character budget",
        ):
            SqliteLabRegistry(self.db_path).recover_experiment(self.manifest.experiment_id)

    def test_event_factory_rejects_oversized_timestamp_before_digest_construction(self) -> None:
        with self.assertRaisesRegex(InvalidLabRecordError, "bounded canonical ISO-8601"):
            LabRegistryEventReceipt.create(
                experiment_id=self.manifest.experiment_id,
                sequence=0,
                kind="experiment_registered",
                payload={
                    "hypothesis": self.hypothesis.to_dict(),
                    "manifest": self.manifest.to_dict(),
                },
                previous_event_digest=None,
                created_at="2026-08-27T00:00:00." + ("1" * 1_000_000) + "+00:00",
            )

    def test_public_event_factory_normalizes_unknown_kind_to_lab_error(self) -> None:
        with self.assertRaisesRegex(InvalidLabRecordError, "Unknown M4.2"):
            LabRegistryEventReceipt.create(
                experiment_id=self.manifest.experiment_id,
                sequence=0,
                kind="future-event-kind",
                payload={},
                previous_event_digest=None,
            )

    def test_registered_at_metadata_tamper_fails_closed(self) -> None:
        self._execute(
            """
            UPDATE lab_registry_experiments
            SET registered_at = '2026-08-27T00:00:00+00:00'
            WHERE experiment_id = ?
            """,
            (self.manifest.experiment_id,),
        )

        with self.assertRaisesRegex(
            LabPersistenceIntegrityError,
            "registration metadata",
        ):
            SqliteLabRegistry(self.db_path).recover_experiment(self.manifest.experiment_id)

    def test_corrupt_run_index_is_integrity_failure_not_duplicate_rejection(self) -> None:
        # Rebind the derived ordinal without changing the authoritative event.
        self._execute(
            "UPDATE lab_registry_runs SET ordinal = 1 WHERE run_id = ?",
            (self.run.run_id,),
        )
        competing = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=1,
            seed=12,
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).register_run(competing)

    def test_registry_exposes_no_execution_or_authority_methods(self) -> None:
        forbidden = {
            "execute",
            "execute_process",
            "authorize",
            "issue_receipt",
            "consume_receipt",
            "write_workspace",
            "git_commit",
            "git_push",
            "launch_delegation",
        }
        self.assertTrue(forbidden.isdisjoint(dir(SqliteLabRegistry)))

    def test_metric_with_rebound_run_digest_never_appends(self) -> None:
        metric = MetricRecord.create(run=self.run, name="score", value=1.0)
        payload = metric.to_dict()
        payload["run_digest"] = "0" * 64
        # M4.1 direct reconstruction itself refuses the stale digest, so the
        # registry cannot even receive a valid typed MetricRecord with this
        # rebound lineage.
        from codexia_manual_agent.lab import metric_record_from_dict

        with self.assertRaises(InvalidLabRecordError):
            metric_record_from_dict(payload)
        recovered = self.store.recover_experiment(self.manifest.experiment_id)
        self.assertEqual(len(recovered.events), 2)


if __name__ == "__main__":
    unittest.main()
