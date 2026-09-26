"""Create test TLS servers without the process-wide client truststore shim.

Agent tests can install truststore into ``ssl`` during a shared WebUI run.
Its SSLContext is a client verifier and cannot wrap a listening server socket;
the TLS fixture must continue to exercise the stdlib server implementation.
"""

import ssl


def server_ssl_context() -> ssl.SSLContext:
    context_class = ssl.SSLContext
    if context_class.__module__ != "ssl":
        # truststore saves the original class when injecting into ssl. This is
        # the same escape hatch Agent uses for explicit CA-bundle contexts.
        from truststore._ssl_constants import _original_SSLContext

        context_class = _original_SSLContext
    return context_class(ssl.PROTOCOL_TLS_SERVER)
