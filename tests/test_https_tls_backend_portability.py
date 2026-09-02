from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.git_mutation.https_push import GIT_HTTPS_NETWORK_PUSH_BACKEND
from codexia_manual_agent.git_mutation.https_transport import (
    close_https_transport,
    https_git_config_args,
    materialize_https_credentials,
)
import test_git_mutation_https_transport as transport


class HttpsTlsBackendPortabilityTests(unittest.TestCase):
    def test_backend_identity_does_not_claim_openssl_when_git_selects_native_tls(self) -> None:
        self.assertEqual(
            GIT_HTTPS_NETWORK_PUSH_BACKEND,
            "network-https-bound-git-default.v1",
        )

    def test_binding_uses_bound_git_default_tls_backend_policy(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            transport._init_repo(root)
            _snapshot, binding, _ca, _credential = transport._binding(root, trust)
            self.addCleanup(close_https_transport, binding)

            self.assertEqual(binding.tls_backend, "bound-git-default")

    def test_config_does_not_inject_empty_tls_paths_or_override_tls_backend(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            transport._init_repo(root)
            _snapshot, binding, _ca, _credential = transport._binding(root, trust)
            self.addCleanup(close_https_transport, binding)
            materialize_https_credentials(binding)

            args = https_git_config_args(binding)
            self.assertNotIn("http.sslCert=", args)
            self.assertNotIn("http.sslKey=", args)
            self.assertNotIn("http.pinnedPubkey=", args)
            self.assertFalse(any(arg.startswith("http.sslBackend=") for arg in args))

    def test_normative_https_docs_match_portable_backend_contract(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        transport_doc = (repo_root / "docs" / "m2_5_1_network_git_transport.md").read_text(
            encoding="utf-8"
        )
        source_audit = (repo_root / "docs" / "m2_5_1_source_audit.md").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            f"HTTPS backend — `{GIT_HTTPS_NETWORK_PUSH_BACKEND}`",
            transport_doc,
        )
        for document in (transport_doc, source_audit):
            self.assertNotIn("network-https-openssl.v1", document)
            self.assertNotIn("http.sslBackend=openssl", document)
            self.assertIn("bound-git-default", document)


if __name__ == "__main__":
    unittest.main()
