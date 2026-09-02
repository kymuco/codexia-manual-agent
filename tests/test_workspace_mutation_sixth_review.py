from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.errors import WorkspaceMutationBoundaryError
from codexia_manual_agent.mutation import (
    WorkspaceMutationExecutor,
    prepare_create_proposal,
    prepare_replace_proposal,
)
from codexia_manual_agent.mutation import metadata_executor as metadata_executor_module
from codexia_manual_agent.mutation import preflight_executor as preflight_executor_module
from codexia_manual_agent.mutation import secure_executor as secure_executor_module
from codexia_manual_agent.mutation import windows_txf as windows_txf_module
from codexia_manual_agent.mutation import workspace as workspace_module


class SixthReviewWorkspaceMutationTests(unittest.TestCase):
    def test_all_executor_imports_use_capability_preflight(self) -> None:
        self.assertIs(workspace_module.WorkspaceMutationExecutor, WorkspaceMutationExecutor)
        self.assertIs(secure_executor_module.WorkspaceMutationExecutor, WorkspaceMutationExecutor)

    def test_unsupported_windows_replace_fails_before_receipt_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "file.txt"
            target.write_bytes(b"old")
            proposal = prepare_replace_proposal(
                workspace=root,
                target="file.txt",
                content=b"new",
            )
            authority = LocalApprovalAuthority()
            receipt = authority.decide(
                proposal,
                mode=ApprovalMode.RISKY,
                approved=True,
            )
            lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
            lifecycle.apply_receipt(receipt, authority=authority)
            executor = WorkspaceMutationExecutor()

            with (
                mock.patch.object(
                    preflight_executor_module,
                    "_is_windows_host",
                    return_value=True,
                ),
                mock.patch.object(
                    preflight_executor_module,
                    "_require_windows_strict_replace_support",
                    side_effect=WorkspaceMutationBoundaryError(
                        "TxF unavailable; authorization was not consumed"
                    ),
                ) as support,
                mock.patch.object(
                    executor._delegate,
                    "execute",
                    side_effect=AssertionError("delegate must not execute"),
                ) as delegate,
            ):
                with self.assertRaisesRegex(
                    WorkspaceMutationBoundaryError,
                    "TxF unavailable",
                ):
                    executor.execute(lifecycle, authority=authority)

            support.assert_called_once_with(target)
            delegate.assert_not_called()
            self.assertFalse(authority.is_consumed(receipt))
            self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)
            self.assertEqual(target.read_bytes(), b"old")

    def test_supported_windows_replace_delegates_after_txf_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "file.txt"
            target.write_bytes(b"old")
            proposal = prepare_replace_proposal(
                workspace=root,
                target="file.txt",
                content=b"new",
            )
            authority = LocalApprovalAuthority()
            receipt = authority.decide(
                proposal,
                mode=ApprovalMode.RISKY,
                approved=True,
            )
            lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
            lifecycle.apply_receipt(receipt, authority=authority)
            executor = WorkspaceMutationExecutor()
            sentinel = object()

            with (
                mock.patch.object(
                    preflight_executor_module,
                    "_is_windows_host",
                    return_value=True,
                ),
                mock.patch.object(
                    preflight_executor_module,
                    "_require_windows_strict_replace_support",
                    return_value="NTFS",
                ) as support,
                mock.patch.object(
                    executor._delegate,
                    "execute",
                    return_value=sentinel,
                ) as delegate,
            ):
                result = executor.execute(lifecycle, authority=authority)

            self.assertIs(result, sentinel)
            support.assert_called_once_with(target)
            delegate.assert_called_once_with(lifecycle, authority=authority)

    def test_create_does_not_require_strict_replace_capability(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal = prepare_create_proposal(
                workspace=root,
                target="file.txt",
                content=b"new",
            )
            authority = LocalApprovalAuthority()
            receipt = authority.decide(
                proposal,
                mode=ApprovalMode.RISKY,
                approved=True,
            )
            lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
            lifecycle.apply_receipt(receipt, authority=authority)
            executor = WorkspaceMutationExecutor()
            sentinel = object()

            with (
                mock.patch.object(
                    preflight_executor_module,
                    "_is_windows_host",
                    return_value=True,
                ),
                mock.patch.object(
                    preflight_executor_module,
                    "_require_windows_strict_replace_support",
                    side_effect=AssertionError("create must not probe strict replace"),
                ),
                mock.patch.object(
                    executor._delegate,
                    "execute",
                    return_value=sentinel,
                ) as delegate,
            ):
                result = executor.execute(lifecycle, authority=authority)

            self.assertIs(result, sentinel)
            delegate.assert_called_once_with(lifecycle, authority=authority)

    def test_txf_capability_gate_requires_volume_transaction_flag(self) -> None:
        with (
            mock.patch.object(windows_txf_module, "_require_txf_api_surface"),
            mock.patch.object(
                windows_txf_module,
                "_volume_capabilities",
                return_value=("NTFS", 0),
            ),
            mock.patch.object(
                windows_txf_module,
                "create_transaction",
                side_effect=AssertionError("transaction must not be created"),
            ) as create_transaction,
        ):
            with self.assertRaisesRegex(
                WorkspaceMutationBoundaryError,
                "FILE_SUPPORTS_TRANSACTIONS",
            ):
                windows_txf_module.require_windows_txf_support(
                    Path("C:/workspace/file.txt")
                )

        create_transaction.assert_not_called()

    def test_txf_capability_gate_rejects_read_only_volume(self) -> None:
        flags = (
            windows_txf_module._FILE_SUPPORTS_TRANSACTIONS
            | windows_txf_module._FILE_READ_ONLY_VOLUME
        )
        with (
            mock.patch.object(windows_txf_module, "_require_txf_api_surface"),
            mock.patch.object(
                windows_txf_module,
                "_volume_capabilities",
                return_value=("NTFS", flags),
            ),
            mock.patch.object(
                windows_txf_module,
                "create_transaction",
                side_effect=AssertionError("transaction must not be created"),
            ) as create_transaction,
        ):
            with self.assertRaisesRegex(
                WorkspaceMutationBoundaryError,
                "FILE_READ_ONLY_VOLUME",
            ):
                windows_txf_module.require_windows_txf_support(
                    Path("C:/workspace/file.txt")
                )

        create_transaction.assert_not_called()

    def test_transaction_close_keeps_handle_for_retry_after_close_failure(self) -> None:
        transaction = windows_txf_module.WindowsTxFTransaction(handle=123)

        with mock.patch.object(
            windows_txf_module,
            "_close_transaction_handle",
            side_effect=[OSError(6, "simulated close failure"), None],
        ) as close_handle:
            with self.assertRaisesRegex(OSError, "simulated close failure"):
                transaction.close()

            self.assertEqual(transaction.handle, 123)
            transaction.close()

        self.assertEqual(transaction.handle, 0)
        self.assertEqual(close_handle.call_count, 2)

    def test_finish_transaction_retries_transient_handle_cleanup_failure(self) -> None:
        transaction = windows_txf_module.WindowsTxFTransaction(
            handle=123,
            finished=True,
        )

        with mock.patch.object(
            windows_txf_module.WindowsTxFTransaction,
            "close",
            side_effect=[OSError(6, "simulated close failure"), None],
        ) as close_transaction:
            retained, cleanup_error = metadata_executor_module._finish_transaction(
                transaction,
                committed=True,
                cleanup_error=None,
            )

        self.assertIsNone(retained)
        self.assertIsNotNone(cleanup_error)
        self.assertIn("transaction handle cleanup failed", cleanup_error)
        self.assertEqual(close_transaction.call_count, 2)

    def test_finish_transaction_retains_object_after_persistent_close_failure(self) -> None:
        transaction = windows_txf_module.WindowsTxFTransaction(
            handle=123,
            finished=True,
        )

        with mock.patch.object(
            windows_txf_module.WindowsTxFTransaction,
            "close",
            side_effect=OSError(6, "persistent close failure"),
        ) as close_transaction:
            retained, cleanup_error = metadata_executor_module._finish_transaction(
                transaction,
                committed=True,
                cleanup_error=None,
            )

        self.assertIs(retained, transaction)
        self.assertIsNotNone(cleanup_error)
        self.assertIn("transaction handle cleanup retry failed", cleanup_error)
        self.assertEqual(close_transaction.call_count, 2)

    def test_executor_retains_unresolved_transaction_and_fails_closed_on_retry(self) -> None:
        executor = metadata_executor_module.WindowsMetadataReplaceExecutor()
        transaction = windows_txf_module.WindowsTxFTransaction(
            handle=123,
            finished=True,
        )
        executor._retained_transactions.append(transaction)

        with mock.patch.object(
            metadata_executor_module,
            "_finish_transaction",
            return_value=(transaction, "persistent cleanup failure"),
        ) as finish_transaction:
            with self.assertRaisesRegex(
                WorkspaceMutationBoundaryError,
                "cleanup remains unresolved",
            ):
                executor._retry_retained_transaction_cleanup()

        self.assertEqual(executor._retained_transactions, [transaction])
        finish_transaction.assert_called_once_with(
            transaction,
            committed=True,
            cleanup_error=None,
        )


if __name__ == "__main__":
    unittest.main()
