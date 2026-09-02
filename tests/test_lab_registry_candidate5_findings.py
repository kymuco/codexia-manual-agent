from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.lab import (
    ExperimentManifest,
    Hypothesis,
    LabPersistenceIntegrityError,
    SqliteLabRegistry,
)


_FIRST_EXPERIMENT_ID = "11111111-1111-4111-8111-111111111111"
_SECOND_EXPERIMENT_ID = "22222222-2222-4222-8222-222222222222"
_THIRD_EXPERIMENT_ID = "abcdef33-3333-4333-8333-333333333333"
_REBOUND_HYPOTHESIS_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class DurableLabRegistryCandidate5FindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "hypothesis-owner-chronology.sqlite3"
        self.store = SqliteLabRegistry(self.db_path)
        self.hypothesis = Hypothesis.create(
            statement="Authoritative chronology owns durable hypothesis identity.",
            falsification_criterion=(
                "A root metadata rebind can hide an authoritative hypothesis owner."
            ),
        )

    def _execute(self, sql: str, parameters: tuple[object, ...]) -> None:
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(sql, parameters)
            connection.commit()
        finally:
            connection.close()

    def _root_count(self, experiment_id: str) -> int:
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM lab_registry_experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            assert row is not None
            return int(row[0])
        finally:
            connection.close()

    def _register_two_valid_owners(self) -> tuple[ExperimentManifest, ExperimentManifest]:
        first = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="First valid owner.",
            experiment_id=_FIRST_EXPERIMENT_ID,
        )
        second = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Second owner whose root metadata will be rebound.",
            experiment_id=_SECOND_EXPERIMENT_ID,
        )
        self.store.register_experiment(self.hypothesis, first)
        self.store.register_experiment(self.hypothesis, second)
        return first, second

    def test_rebound_root_hypothesis_id_cannot_hide_authoritative_owner(self) -> None:
        _, second = self._register_two_valid_owners()
        third = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Third owner must audit authoritative chronology.",
            experiment_id=_THIRD_EXPERIMENT_ID,
        )

        # Out-of-band corruption changes only the derived root metadata. The
        # authoritative experiment_registered event still claims the original
        # hypothesis identity.
        self._execute(
            """
            UPDATE lab_registry_experiments
            SET hypothesis_id = ?
            WHERE experiment_id = ?
            """,
            (_REBOUND_HYPOTHESIS_ID, second.experiment_id),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_experiment(self.hypothesis, third)

        self.assertEqual(self._root_count(third.experiment_id), 0)

    def test_corrupt_later_owner_precedes_deferred_hypothesis_digest_conflict(self) -> None:
        _, second = self._register_two_valid_owners()
        conflicting_hypothesis = Hypothesis.create(
            statement="Same durable identity with a conflicting hypothesis payload.",
            falsification_criterion="Any valid prior owner binds another digest.",
            hypothesis_id=self.hypothesis.hypothesis_id,
        )
        third = ExperimentManifest.create(
            hypothesis=conflicting_hypothesis,
            procedure="Conflict must not mask corruption in a later owner.",
            experiment_id=_THIRD_EXPERIMENT_ID,
        )

        # The first sorted authoritative owner is valid and has a different digest
        # from the candidate. The second authoritative owner is corrupted. The
        # corruption must be observed before the ordinary digest conflict is raised.
        self._execute(
            """
            UPDATE lab_registry_experiments
            SET hypothesis_id = ?
            WHERE experiment_id = ?
            """,
            (_REBOUND_HYPOTHESIS_ID, second.experiment_id),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_experiment(conflicting_hypothesis, third)

        self.assertEqual(self._root_count(third.experiment_id), 0)


if __name__ == "__main__":
    unittest.main()
