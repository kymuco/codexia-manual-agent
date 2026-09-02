from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.application.mutate_workspace import MutateWorkspaceService
from codexia_manual_agent.mutation import MutationOperation, MutationTerminationReason
from codexia_manual_agent.mutation import windows_txf as windows_txf_module


@unittest.skipUnless(os.name == "nt", "Windows TxF strict-replace regressions")
class WindowsTxFWorkspaceMutationTests(unittest.TestCase):
    def test_exact_target_pin_no_longer_self_blocks_authorized_replace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "file.txt"
            target.write_bytes(b"old")

            result = MutateWorkspaceService().run(
                workspace=root,
                operation=MutationOperation.REPLACE,
                target="file.txt",
                content=b"new",
                approved=True,
            )

            self.assertTrue(result.observation.applied)
            self.assertEqual(
                result.observation.termination_reason,
                MutationTerminationReason.APPLIED,
            )
            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(list(root.glob(".codexia-txf-stage-*")), [])

    def test_postmove_transaction_lock_survives_file_handle_close_until_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "file.txt"
            moved = root / "moved.txt"
            target.write_bytes(b"old")
            original_commit = windows_txf_module.WindowsTxFTransaction.commit
            commit_probe_ran = False

            def commit_with_external_probe(transaction) -> None:
                nonlocal commit_probe_ran
                commit_probe_ran = True
                # metadata_executor closes both transacted file handles before this
                # method is called.  These failures therefore come from TxF's pending
                # namespace reservation, not from the old share=0 target handle.
                with self.assertRaises(OSError):
                    target.rename(moved)
                with self.assertRaises(OSError):
                    target.write_bytes(b"intruder")
                original_commit(transaction)

            with mock.patch.object(
                windows_txf_module.WindowsTxFTransaction,
                "commit",
                new=commit_with_external_probe,
            ):
                result = MutateWorkspaceService().run(
                    workspace=root,
                    operation=MutationOperation.REPLACE,
                    target="file.txt",
                    content=b"new",
                    approved=True,
                )

            self.assertTrue(commit_probe_ran)
            self.assertTrue(result.observation.applied)
            self.assertEqual(target.read_bytes(), b"new")
            self.assertFalse(moved.exists())

    def test_commit_failure_rolls_back_old_target_and_cleans_transacted_stage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "file.txt"
            target.write_bytes(b"old")

            with mock.patch.object(
                windows_txf_module.WindowsTxFTransaction,
                "commit",
                side_effect=OSError(5, "simulated TxF commit failure"),
            ):
                result = MutateWorkspaceService().run(
                    workspace=root,
                    operation=MutationOperation.REPLACE,
                    target="file.txt",
                    content=b"new",
                    approved=True,
                )

            self.assertFalse(result.observation.applied)
            self.assertEqual(
                result.observation.termination_reason,
                MutationTerminationReason.WRITE_ERROR,
            )
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(list(root.glob(".codexia-txf-stage-*")), [])

    def test_process_death_before_commit_rolls_back_target_and_stage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "file.txt"
            target.write_bytes(b"old")

            child_code = r'''
import os
import sys
import time
from pathlib import Path

from codexia_manual_agent.mutation.parent_anchor import PinnedMutationTarget
from codexia_manual_agent.mutation.windows_txf import (
    create_metadata_stage,
    create_transaction,
    move_replace_staged,
    pin_exact_replace_target,
)

root = Path(sys.argv[1])
target = root / "file.txt"
tx = create_transaction()
with PinnedMutationTarget(root=root, parent=root, target_name=target.name) as pinned:
    exact = pin_exact_replace_target(tx, target, max_bytes=16_777_216)
    if exact is None:
        raise SystemExit(2)
    stage = create_metadata_stage(tx, pinned, b"new", mode=0o644)
    pinned.verify_staged_identity(stage)
    move_replace_staged(tx, stage, target)
    pinned.close_staged(stage)
    exact.close()
    print("READY", flush=True)
    while True:
        time.sleep(60)
'''
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, str(root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert child.stdout is not None
                assert child.stderr is not None
                line = child.stdout.readline().strip()
                if line != "READY":
                    stderr = child.stderr.read()
                    self.fail(f"TxF crash worker failed before crash point: {line!r} {stderr}")

                child.kill()
                child.wait(timeout=5)

                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    try:
                        if target.read_bytes() == b"old":
                            break
                    except OSError:
                        pass
                    time.sleep(0.05)
                else:
                    self.fail("TxF rollback did not become visible after process death")

                self.assertEqual(target.read_bytes(), b"old")
                self.assertEqual(list(root.glob(".codexia-txf-stage-*")), [])
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
