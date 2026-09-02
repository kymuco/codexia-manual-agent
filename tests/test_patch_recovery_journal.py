from patch_recovery_test_support import *
from patch_recovery_test_support import (
    _authorized_patch, _executed_patch, _committed_result, _observations_for,
)

class PatchRecoveryJournalContractTests(unittest.TestCase):
    def test_journal_requires_absolute_path(self) -> None:
        with self.assertRaisesRegex(InvalidWorkspaceMutationError, "absolute"):
            PatchRecoveryJournal(Path("relative/recovery.jsonl"))
    def test_journal_must_live_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            journal = PatchRecoveryJournal((root / "recovery.jsonl").resolve())
            with self.assertRaisesRegex(WorkspaceMutationBoundaryError, "outside"):
                journal.assert_fresh(workspace_root=root)
    def test_nonempty_journal_blocks_new_execution_before_authority_use(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            _, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            journal_path = Path(state) / "recovery.jsonl"
            journal_path.write_text("stale\n", encoding="utf-8")
            executor = RecoverablePatchApplicationExecutor(
                PatchRecoveryJournal(journal_path.resolve())
            )
            with self.assertRaisesRegex(WorkspaceMutationBoundaryError, "already exists"):
                executor.execute(lifecycle, plan, authority=authority)
            self.assertFalse(authority.is_consumed(receipt))
            self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)
    def test_bounded_journal_read_rejects_oversize_before_parse(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            path = Path(state) / "recovery.jsonl"
            with path.open("wb") as stream:
                stream.seek(MAX_RECOVERY_JOURNAL_BYTES)
                stream.write(b"x")
            journal = PatchRecoveryJournal(path.resolve())
            with self.assertRaisesRegex(WorkspaceMutationBoundaryError, "bounded read"):
                journal.read(workspace_root=root)
    def test_journal_hash_chain_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            _, _, _, lifecycle, plan = _executed_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            journal = PatchRecoveryJournal((Path(state) / "recovery.jsonl").resolve())
            journal.append_phase(
                lifecycle=lifecycle,
                plan=plan,
                phase=PatchRecoveryJournalPhase.EXECUTION_STARTED,
            )
            rows = journal.path.read_text(encoding="utf-8").splitlines()
            value = json.loads(rows[0])
            value["plan_digest"] = "0" * 64
            journal.path.write_text(json.dumps(value) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(InvalidWorkspaceMutationError, "digest"):
                journal.read(workspace_root=root)
    def test_torn_tail_is_ignored_but_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            _, _, _, lifecycle, plan = _executed_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            journal = PatchRecoveryJournal((Path(state) / "recovery.jsonl").resolve())
            first = journal.append_phase(
                lifecycle=lifecycle,
                plan=plan,
                phase=PatchRecoveryJournalPhase.EXECUTION_STARTED,
            )
            with journal.path.open("ab") as stream:
                stream.write(b'{"partial":')
            read = journal.read(workspace_root=root)
            self.assertTrue(read.torn_tail)
            self.assertEqual(len(read.records), 1)
            self.assertEqual(read.records[0].record_digest, first.record_digest)
    def test_different_process_cannot_append_existing_journal(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            _, _, _, lifecycle, plan = _executed_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            journal = PatchRecoveryJournal((Path(state) / "recovery.jsonl").resolve())
            journal.append_phase(
                lifecycle=lifecycle,
                plan=plan,
                phase=PatchRecoveryJournalPhase.EXECUTION_STARTED,
            )
            with patch.object(journal_module.os, "getpid", return_value=os.getpid() + 1000):
                with self.assertRaisesRegex(WorkspaceMutationBoundaryError, "different process"):
                    journal.append_phase(
                        lifecycle=lifecycle,
                        plan=plan,
                        phase=PatchRecoveryJournalPhase.COMMIT_INTENT,
                    )
    def test_journal_cannot_start_at_commit_intent(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            _, _, _, lifecycle, plan = _executed_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            journal = PatchRecoveryJournal((Path(state) / "recovery.jsonl").resolve())
            with self.assertRaisesRegex(WorkspaceMutationBoundaryError, "begin"):
                journal.append_phase(
                    lifecycle=lifecycle,
                    plan=plan,
                    phase=PatchRecoveryJournalPhase.COMMIT_INTENT,
                )

class PatchRecoveryJournalWindowsNamespacePinTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows journal namespace pin required")
    def test_parent_pin_blocks_direct_parent_rename(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            parent = Path(state) / "journal-parent"
            parent.mkdir()
            journal_path = parent / "recovery.jsonl"
            moved = parent.with_name("journal-parent-moved")
            with parent_module.PinnedRecoveryJournalParent(
                journal_path=journal_path.resolve(),
                workspace_root=root,
            ):
                with self.assertRaises(OSError):
                    os.replace(parent, moved)
            self.assertTrue(parent.is_dir())
            self.assertFalse(moved.exists())

    @unittest.skipUnless(os.name == "nt", "Windows journal namespace pin required")
    def test_parent_pin_blocks_ancestor_rename(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            ancestor = Path(state) / "ancestor"
            parent = ancestor / "journal-parent"
            parent.mkdir(parents=True)
            journal_path = parent / "recovery.jsonl"
            moved = ancestor.with_name("ancestor-moved")
            with parent_module.PinnedRecoveryJournalParent(
                journal_path=journal_path.resolve(),
                workspace_root=root,
            ):
                with self.assertRaises(OSError):
                    os.replace(ancestor, moved)
            self.assertTrue(parent.is_dir())
            self.assertFalse(moved.exists())

    @unittest.skipUnless(os.name == "nt", "Windows journal namespace pin required")
    def test_parent_pin_blocks_redirect_into_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            parent = Path(state) / "journal-parent"
            parent.mkdir()
            journal_path = parent / "recovery.jsonl"
            redirected = root / "redirected-journal-parent"
            with parent_module.PinnedRecoveryJournalParent(
                journal_path=journal_path.resolve(),
                workspace_root=root,
            ):
                with self.assertRaises(OSError):
                    os.replace(parent, redirected)
            self.assertTrue(parent.is_dir())
            self.assertFalse(redirected.exists())

    @unittest.skipUnless(os.name == "nt", "Windows journal namespace pin required")
    def test_first_create_holds_parent_pin_at_secure_open(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            parent = Path(state) / "journal-parent"
            parent.mkdir()
            journal = PatchRecoveryJournal((parent / "recovery.jsonl").resolve())
            _, _, _, lifecycle, plan = _executed_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            moved = parent.with_name("journal-parent-moved")
            original = parent_module._win_open_journal_file

            def attacked_open(path, *, create, writable):
                with self.assertRaises(OSError):
                    os.replace(parent, moved)
                return original(path, create=create, writable=writable)

            with patch.object(parent_module, "_win_open_journal_file", side_effect=attacked_open):
                journal.append_phase(
                    lifecycle=lifecycle,
                    plan=plan,
                    phase=PatchRecoveryJournalPhase.EXECUTION_STARTED,
                )
            self.assertTrue(journal.path.is_file())
            self.assertFalse(moved.exists())

    @unittest.skipUnless(os.name == "nt", "Windows journal namespace pin required")
    def test_existing_append_holds_same_file_against_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            journal = PatchRecoveryJournal((Path(state) / "recovery.jsonl").resolve())
            _, _, _, lifecycle, plan = _executed_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            journal.append_phase(
                lifecycle=lifecycle,
                plan=plan,
                phase=PatchRecoveryJournalPhase.EXECUTION_STARTED,
            )
            moved = journal.path.with_name("recovery-moved.jsonl")
            original = PatchRecoveryJournal._read_open_fd

            def attacked_read(fd):
                with self.assertRaises(OSError):
                    os.replace(journal.path, moved)
                return original(fd)

            with patch.object(
                PatchRecoveryJournal,
                "_read_open_fd",
                side_effect=attacked_read,
            ):
                journal.append_phase(
                    lifecycle=lifecycle,
                    plan=plan,
                    phase=PatchRecoveryJournalPhase.COMMIT_INTENT,
                )
            read = journal.read(workspace_root=root)
            self.assertEqual(len(read.records), 2)
            self.assertFalse(moved.exists())

