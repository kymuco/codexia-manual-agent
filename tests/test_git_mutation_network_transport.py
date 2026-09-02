from __future__ import annotations

import unittest

from codexia_manual_agent.domain.errors import InvalidGitMutationError
from codexia_manual_agent.git_mutation.network_transport import (
    GitNetworkTransport,
    GitSshUrlForm,
    parse_network_git_endpoint,
)


class GitNetworkEndpointAdmissionTests(unittest.TestCase):
    def test_ssh_uri_binds_explicit_user_host_port_and_absolute_path(self) -> None:
        endpoint = parse_network_git_endpoint(
            "ssh://git@GitHub.COM:2222/kymuco/codexia-manual-agent.git"
        )
        self.assertIs(endpoint.transport, GitNetworkTransport.SSH)
        self.assertIs(endpoint.ssh_url_form, GitSshUrlForm.URI)
        self.assertEqual(endpoint.ssh_user, "git")
        self.assertEqual(endpoint.host, "github.com")
        self.assertEqual(endpoint.port, 2222)
        self.assertEqual(endpoint.repository_path, "kymuco/codexia-manual-agent.git")
        self.assertTrue(endpoint.ssh_path_is_absolute)
        self.assertEqual(
            endpoint.review_destination,
            "ssh://git@github.com:2222/kymuco/codexia-manual-agent.git",
        )

    def test_scp_like_ssh_preserves_home_relative_path_semantics(self) -> None:
        endpoint = parse_network_git_endpoint(
            "git@github.com:kymuco/codexia-manual-agent.git"
        )
        self.assertIs(endpoint.transport, GitNetworkTransport.SSH)
        self.assertIs(endpoint.ssh_url_form, GitSshUrlForm.SCP)
        self.assertEqual(endpoint.port, 22)
        self.assertFalse(endpoint.ssh_path_is_absolute)
        self.assertEqual(
            endpoint.review_destination,
            "ssh://git@github.com:22/~/kymuco/codexia-manual-agent.git",
        )

    def test_scp_like_ssh_can_bind_explicit_absolute_path(self) -> None:
        endpoint = parse_network_git_endpoint("deploy@example.com:/srv/git/project.git")
        self.assertTrue(endpoint.ssh_path_is_absolute)
        self.assertEqual(endpoint.repository_path, "srv/git/project.git")
        self.assertEqual(endpoint.review_destination, "ssh://deploy@example.com:22/srv/git/project.git")

    def test_ssh_uri_accepts_canonical_ipv6(self) -> None:
        endpoint = parse_network_git_endpoint(
            "ssh://git@[2001:0db8:0:0:0:0:0:1]:22/repos/project.git"
        )
        self.assertEqual(endpoint.host, "2001:db8::1")
        self.assertEqual(
            endpoint.review_destination,
            "ssh://git@[2001:db8::1]:22/repos/project.git",
        )

    def test_https_binds_host_default_port_and_repository_path_without_credentials(self) -> None:
        endpoint = parse_network_git_endpoint(
            "https://GitHub.COM/kymuco/codexia-manual-agent.git"
        )
        self.assertIs(endpoint.transport, GitNetworkTransport.HTTPS)
        self.assertEqual(endpoint.host, "github.com")
        self.assertEqual(endpoint.port, 443)
        self.assertEqual(endpoint.repository_path, "kymuco/codexia-manual-agent.git")
        self.assertEqual(
            endpoint.review_destination,
            "https://github.com:443/kymuco/codexia-manual-agent.git",
        )

    def test_https_custom_port_is_explicit_in_review_identity(self) -> None:
        endpoint = parse_network_git_endpoint("https://git.example.test:8443/team/repo.git")
        self.assertEqual(endpoint.port, 8443)
        self.assertEqual(endpoint.review_destination, "https://git.example.test:8443/team/repo.git")

    def test_network_endpoint_rejects_ambient_or_ambiguous_transport_forms(self) -> None:
        invalid = (
            "http://example.com/team/repo.git",
            "git://example.com/team/repo.git",
            "ext::sh -c something",
            "file:///tmp/repo.git",
            "github.com:team/repo.git",
            "git@example.com",
            "ssh://example.com/team/repo.git",
            "ssh://git:secret@example.com/team/repo.git",
            "https://user@example.com/team/repo.git",
            "https://user:secret@example.com/team/repo.git",
            "https://example.com/team/repo.git?x=1",
            "ssh://git@example.com/team/repo.git#fragment",
            "https://example.com/team/../repo.git",
            "https://example.com/team/%2Frepo.git",
            "git@example.com:team/../repo.git",
            "git@example.com:team/repo with space.git",
            "ssh://git@example.com:0/team/repo.git",
            "https://example.com:65536/team/repo.git",
            "https://example.com./team/repo.git",
            "https://exa_mple.com/team/repo.git",
            "https://127.01/team/repo.git",
            "https://exämple.com/team/repo.git",
            "https://example.com/team/repo.git\n",
        )
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(InvalidGitMutationError):
                    parse_network_git_endpoint(url)

    def test_network_endpoint_serialization_is_stable_and_secret_free(self) -> None:
        endpoint = parse_network_git_endpoint("ssh://git@example.com/team/repo.git")
        self.assertEqual(
            endpoint.to_dict(),
            {
                "transport": "ssh",
                "original_url": "ssh://git@example.com/team/repo.git",
                "host": "example.com",
                "port": 22,
                "repository_path": "team/repo.git",
                "ssh_user": "git",
                "ssh_url_form": "uri",
                "ssh_path_is_absolute": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
