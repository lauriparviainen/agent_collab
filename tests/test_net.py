import unittest

from agent_collab.net import is_loopback_host, is_loopback_url


class LoopbackHostTests(unittest.TestCase):
    def test_localhost_and_literal_loopback_ips_are_loopback(self):
        for host in ("localhost", "127.0.0.1", "127.255.0.3", "::1"):
            with self.subTest(host=host):
                self.assertTrue(is_loopback_host(host))

    def test_missing_or_non_loopback_hosts_are_not_trusted(self):
        for host in (None, "", "192.168.1.10", "10.0.0.1", "8.8.8.8", "fe80::1"):
            with self.subTest(host=host):
                self.assertFalse(is_loopback_host(host))

    def test_dns_names_other_than_localhost_are_never_trusted(self):
        # Even a name that would resolve to 127.0.0.1 must not pass: the trust
        # decision cannot depend on an attacker-influencable resolver.
        for host in ("localhost.evil.example", "loopback.example", "127.0.0.1.example"):
            with self.subTest(host=host):
                self.assertFalse(is_loopback_host(host))


class LoopbackUrlTests(unittest.TestCase):
    def test_loopback_urls(self):
        for url in ("http://127.0.0.1:8765", "http://localhost:8765/path", "http://[::1]:8765"):
            with self.subTest(url=url):
                self.assertTrue(is_loopback_url(url))

    def test_non_loopback_urls(self):
        for url in ("http://example.com:8765", "http://192.168.1.10:8765", "not a url", ""):
            with self.subTest(url=url):
                self.assertFalse(is_loopback_url(url))


if __name__ == "__main__":
    unittest.main()
