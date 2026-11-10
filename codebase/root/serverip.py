"""
serverip.py
===========
Helpers for the Minecraft server IP allow-list that gates the redemption API.

An admin must register the Minecraft server's public IP in Discord with
``/server setip`` before the Forge mod can redeem shop keys. The API compares
the connecting address against the stored value, so the two sides must agree on
the same canonical form. Everything here is pure (no I/O) and shared by
``cogs/server.py`` and ``api.py``.
"""

from __future__ import annotations

import ipaddress
import re

# A conservative hostname pattern. Only used to *reject* hostnames with a clear
# message: the API compares against a numeric socket address, so a hostname
# would never match and would silently lock the server out.
_HOSTNAME_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*$")


def _strip_port(text: str) -> str:
    """Drop a ``:port`` suffix and ``[]`` brackets from ``ip[:port]`` forms."""
    if text.startswith("["):
        end = text.find("]")
        if end != -1:
            return text[1:end]
        return text[1:]
    # A single colon means ``host:port`` for IPv4; IPv6 has several colons and is
    # left untouched. ``request.remote`` already omits the source port.
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        if port.isdigit():
            return host
    return text


def normalise(value) -> str:
    """Return ``value`` as a canonical, lowercase IP string (or ``""``).

    Handles ``ip:port``, ``[ipv6]:port``, zone ids (``fe80::1%eth0``) and
    IPv4-mapped IPv6 addresses (``::ffff:203.0.113.7`` -> ``203.0.113.7``), so a
    value typed in Discord and a socket address from aiohttp compare equal.
    """
    text = _strip_port(str(value or "").strip())
    if not text:
        return ""
    text = text.split("%", 1)[0]  # strip an IPv6 zone id
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return text.lower()
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return str(addr)


def is_valid(value) -> bool:
    """True only for a genuine IPv4/IPv6 address (hostnames are rejected)."""
    text = _strip_port(str(value or "").strip())
    if not text:
        return False
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def looks_like_hostname(value) -> bool:
    """True when ``value`` looks like a DNS name rather than a numeric IP."""
    text = _strip_port(str(value or "").strip())
    if not text or is_valid(text):
        return False
    return bool(_HOSTNAME_RE.match(text)) and "." in text
