"""Loopback trust-boundary detection shared by the HTTP client and daemon.

The daemon's Origin validation and the client's decision to send the local
daemon token are the same trust question ("is this endpoint local?"), so the
answer is defined exactly once here.
"""

from __future__ import annotations

from ipaddress import ip_address
from typing import Optional
from urllib.parse import urlparse


def is_loopback_host(host: Optional[str]) -> bool:
    """True for ``localhost`` or a literal loopback IP (127.0.0.0/8, ``::1``).

    DNS names other than ``localhost`` are never trusted, even if they would
    resolve to a loopback address: resolving would make the trust decision
    depend on a resolver an attacker may influence.
    """

    if host == "localhost":
        return True
    if not host:
        return False
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def is_loopback_url(url: str) -> bool:
    """True when the URL's hostname is a loopback host per ``is_loopback_host``."""

    return is_loopback_host(urlparse(url).hostname)
