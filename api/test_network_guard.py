"""Test-mode network isolation for the out-of-process WebUI test server.

Extracted from server.py (which imports this module when
``HERMES_WEBUI_TEST_NETWORK_BLOCK`` is set) so the server entry point stays a
thin launcher; the allow-list policy is shared with the pytest-side block in
``tests/conftest.py`` and must stay behaviorally identical:

Allowed destinations (silent pass-through):
  - 127.0.0.0/8     loopback
  - ::1             IPv6 loopback
  - fc00::/7        IPv6 unique-local
  - fe80::/10       IPv6 link-local
  - 192.168.0.0/16  RFC1918 private
  - 10.0.0.0/8      RFC1918 private
  - 172.16.0.0/12   RFC1918 private (16-31)
  - 169.254.0.0/16  link-local
  - 203.0.113.0/24  RFC5737 TEST-NET-3 (documentation IPs used in tests)
  - hostnames ``localhost``, ``*.local``, ``*.test``, ``*.example``,
    ``*.example.com``, ``*.example.net``, ``*.example.org``, ``*.invalid``
    (RFC2606/6761 reserved)
"""
import os
import re
import socket


def install_test_network_block() -> None:
    """Monkeypatch socket.create_connection / socket.socket.connect for tests.

    Called from server.py at import time when HERMES_WEBUI_TEST_NETWORK_BLOCK
    is enabled (set by tests/conftest.py before booting the subprocess server
    so the subprocess cannot make outbound requests the pytest-side block
    can't see).
    """
    if os.environ.get("HERMES_WEBUI_TEST_NETWORK_BLOCK", "").strip() not in ("1", "true", "yes"):
        return

    real_create_connection = socket.create_connection
    real_socket_connect = socket.socket.connect

    def _re_match_unique_local_ipv6(h):
        """Match IPv6 fc00::/7 without catching similar-looking hostnames."""
        return bool(re.match(r"^f[cd][0-9a-f]{0,2}:", h))

    def _addr_is_local(host):
        if not isinstance(host, str):
            return False
        h = host.strip().lower()
        if not h:
            return False
        if h in ("::1", "0:0:0:0:0:0:0:1") or h.startswith("fe80:") or _re_match_unique_local_ipv6(h):
            return True
        if h == "localhost" or h.endswith(".localhost"):
            return True
        if h.endswith(".local") or h.endswith(".test") or h.endswith(".invalid"):
            return True
        if h == "example.com" or h.endswith(".example.com"):
            return True
        if h == "example.net" or h.endswith(".example.net"):
            return True
        if h == "example.org" or h.endswith(".example.org"):
            return True
        if h.endswith(".example"):
            return True
        if h and h[0].isdigit() and h.count(".") == 3:
            try:
                o1, o2, o3, o4 = [int(p) for p in h.split(".")]
            except ValueError:
                return False
            if o1 == 127:
                return True
            if o1 == 10:
                return True
            if o1 == 192 and o2 == 168:
                return True
            if o1 == 172 and 16 <= o2 <= 31:
                return True
            if o1 == 169 and o2 == 254:
                return True
            if o1 == 203 and o2 == 0 and o3 == 113:
                return True
        return False

    def _blocked_create_connection(address, *a, **kw):
        try:
            host = address[0]
        except (TypeError, IndexError):
            host = ""
        if _addr_is_local(host):
            return real_create_connection(address, *a, **kw)
        raise OSError(
            f"hermes test network isolation (server.py): outbound to {address!r} blocked"
        )

    def _blocked_socket_connect(self, address):
        try:
            host = address[0]
        except (TypeError, IndexError):
            host = ""
        if _addr_is_local(host):
            return real_socket_connect(self, address)
        raise OSError(
            f"hermes test network isolation (server.py): socket.connect to {address!r} blocked"
        )

    socket.create_connection = _blocked_create_connection
    socket.socket.connect = _blocked_socket_connect
