from __future__ import annotations

import unittest
from unittest import mock

from codexia_manual_agent.domain.errors import InvalidGitMutationError
from codexia_manual_agent.git_mutation import push_router


class GovernedGitPushRouterTests(unittest.TestCase):
    def test_file_is_default_and_rejects_network_trust_inputs(self) -> None:
        sentinel = object()
        with mock.patch.object(
            push_router,
            "prepare_git_push_proposal",
            return_value=sentinel,
        ) as prepare:
            result = push_router.prepare_governed_git_push_proposal(
                workspace="workspace",
                remote="origin",
                destination_ref="refs/heads/main",
            )
        self.assertIs(result, sentinel)
        prepare.assert_called_once_with(
            workspace="workspace",
            remote="origin",
            destination_ref="refs/heads/main",
        )

        with self.assertRaisesRegex(InvalidGitMutationError, "does not accept"):
            push_router.prepare_governed_git_push_proposal(
                workspace="workspace",
                remote="origin",
                destination_ref="refs/heads/main",
                transport="file",
                ssh_identity_file="C:/id",
            )

    def test_ssh_requires_exact_pair_and_rejects_https_inputs(self) -> None:
        for kwargs in (
            {"ssh_identity_file": "C:/id"},
            {"ssh_host_key_file": "C:/hosts"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(InvalidGitMutationError, "requires both"):
                    push_router.prepare_governed_git_push_proposal(
                        workspace="workspace",
                        remote="origin",
                        destination_ref="refs/heads/main",
                        transport="ssh",
                        **kwargs,
                    )
        with self.assertRaisesRegex(InvalidGitMutationError, "cannot accept HTTPS"):
            push_router.prepare_governed_git_push_proposal(
                workspace="workspace",
                remote="origin",
                destination_ref="refs/heads/main",
                transport="ssh",
                ssh_identity_file="C:/id",
                ssh_host_key_file="C:/hosts",
                https_ca_bundle="C:/ca.pem",
            )

        sentinel = object()
        with mock.patch.object(
            push_router,
            "prepare_network_git_push_proposal",
            return_value=sentinel,
        ) as prepare:
            result = push_router.prepare_governed_git_push_proposal(
                workspace="workspace",
                remote="origin",
                destination_ref="refs/heads/main",
                transport="ssh",
                ssh_identity_file="C:/id",
                ssh_host_key_file="C:/hosts",
            )
        self.assertIs(result, sentinel)
        prepare.assert_called_once_with(
            workspace="workspace",
            remote="origin",
            destination_ref="refs/heads/main",
            identity_file="C:/id",
            host_key_file="C:/hosts",
        )

    def test_https_requires_credential_source_and_ca_and_rejects_ssh_inputs(self) -> None:
        for kwargs in (
            {"https_credential_file": "C:/credentials"},
            {"https_ca_bundle": "C:/ca.pem"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(InvalidGitMutationError, "requires both"):
                    push_router.prepare_governed_git_push_proposal(
                        workspace="workspace",
                        remote="origin",
                        destination_ref="refs/heads/main",
                        transport="https",
                        **kwargs,
                    )
        with self.assertRaisesRegex(InvalidGitMutationError, "cannot accept SSH"):
            push_router.prepare_governed_git_push_proposal(
                workspace="workspace",
                remote="origin",
                destination_ref="refs/heads/main",
                transport="https",
                https_credential_file="C:/credentials",
                https_ca_bundle="C:/ca.pem",
                ssh_identity_file="C:/id",
            )

        sentinel = object()
        with mock.patch.object(
            push_router,
            "prepare_https_network_git_push_proposal",
            return_value=sentinel,
        ) as prepare:
            result = push_router.prepare_governed_git_push_proposal(
                workspace="workspace",
                remote="origin",
                destination_ref="refs/heads/main",
                transport="https",
                https_credential_file="C:/credentials",
                https_ca_bundle="C:/ca.pem",
            )
        self.assertIs(result, sentinel)
        prepare.assert_called_once_with(
            workspace="workspace",
            remote="origin",
            destination_ref="refs/heads/main",
            credential_file="C:/credentials",
            ca_bundle_file="C:/ca.pem",
        )

    def test_unknown_transport_fails_closed_before_any_backend(self) -> None:
        with self.assertRaisesRegex(InvalidGitMutationError, "file, ssh, or https"):
            push_router.prepare_governed_git_push_proposal(
                workspace="workspace",
                remote="origin",
                destination_ref="refs/heads/main",
                transport="auto",
            )


if __name__ == "__main__":
    unittest.main()
