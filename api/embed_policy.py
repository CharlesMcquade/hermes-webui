"""Embed-ancestor policy (Phase 1 spike).

`HERMES_WEBUI_EMBED_FRAME_ANCESTORS` configures which origins may frame the
dedicated `/embed` route. Comma-separated `chrome-extension://<id>` or
`https://` origins. Validated; invalid entries are rejected wholesale (fail
closed). DEFAULT (unset/empty) = embed route fully disabled.

Normative constraints (HOST-CONTRACT §11 rules 7-8):
- Only the embed route's response policy changes. Every other response keeps
  `X-Frame-Options: DENY` + `frame-ancestors 'none'`.
- The embed route's enforced CSP `frame-ancestors` is exactly the configured
  ancestor set plus `'self'`; `X-Frame-Options` is omitted on that route only
  when ancestors are configured (XFO cannot express an allowlist, and sending
  DENY would break the embed).
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

ENV_VAR = "HERMES_WEBUI_EMBED_FRAME_ANCESTORS"

# chrome-extension://<id>  — extension IDs are 32 chars of a-p (Chrome)
_EXT_ID_RE = re.compile(r"^[a-p]{32}$")
# https://host[:port] — no path, no wildcard, no userinfo
_HTTPS_RE = re.compile(r"^https://[A-Za-z0-9._~-]+(?::(\d{1,5}))?$")


def validate_ancestor(origin: str) -> bool:
    """Return True if `origin` is an acceptable embed-ancestor entry."""
    if not isinstance(origin, str):
        return False
    origin = origin.strip()
    if origin.startswith("chrome-extension://"):
        return bool(_EXT_ID_RE.fullmatch(origin[len("chrome-extension://"):]))
    match = _HTTPS_RE.fullmatch(origin)
    if not match:
        return False
    port = match.group(1)
    if port is not None:
        try:
            return 1 <= int(port) <= 65535
        except ValueError:
            return False
    return True


def configured_ancestors(env: dict | None = None) -> tuple[str, ...]:
    """Parse + validate the embed ancestor allowlist from the environment.

    Returns a tuple of validated origins (order preserved, duplicates removed).
    Empty tuple = embed disabled. Any invalid entry rejects the WHOLE value
    (fail closed) with a logged warning — mirroring the existing CSP-knob
    behavior in api/helpers.py.
    """
    source = os.environ if env is None else env
    raw = (source.get(ENV_VAR) or "").strip()
    if not raw:
        return ()
    seen: list[str] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or not validate_ancestor(entry):
            logger.warning("Ignoring invalid %s value: %r", ENV_VAR, raw)
            return ()
        if entry not in seen:
            seen.append(entry)
    return tuple(seen)


def embed_enabled(env: dict | None = None) -> bool:
    """True when at least one valid ancestor is configured."""
    return bool(configured_ancestors(env))


def embed_frame_ancestors_csp_value(env: dict | None = None) -> str:
    """CSP frame-ancestors value for the embed route.

    Always includes 'self' plus the configured ancestors. When nothing is
    configured this is 'none' — the embed route fails closed (and 404s).
    """
    ancestors = configured_ancestors(env)
    if not ancestors:
        return "'none'"
    return "'self' " + " ".join(ancestors)


def embed_route_headers(env: dict | None = None) -> dict:
    """Response header overrides for the dedicated embed route only.

    - When embed is disabled: empty dict — the route denies itself (404) and
      any response it does emit keeps the global XFO DENY + 'none' policy.
    - When enabled: X-Frame-Options is OMITTED (it cannot express an
      allowlist) and the enforced CSP carries the exact allowlist.
    """
    if not embed_enabled(env):
        return {}
    return {
        "Content-Security-Policy": _build_embed_csp(env),
    }


_EMBED_CSP_TEMPLATE = (
    "default-src 'self' https://*.cloudflareaccess.com; "
    "object-src 'none'; "
    "frame-ancestors {frame_ancestors}; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://static.cloudflareinsights.com blob:; "
    "worker-src blob: 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "img-src 'self' data: https: blob:; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "media-src 'self' data: blob:; "
    "connect-src {connect_src}; "
    "frame-src {frame_src}; "
    "manifest-src 'self' https://*.cloudflareaccess.com; "
    "base-uri 'self'; form-action 'self'"
)


def _build_embed_csp(env: dict | None = None) -> str:
    """Embed-route enforced CSP: identical to the shared policy except
    frame-ancestors, which is the exact configured allowlist + 'self'."""
    from api.helpers import _csp_connect_src, _csp_extra_connect_src, _csp_extra_frame_src, _csp_frame_src

    return _EMBED_CSP_TEMPLATE.format(
        frame_ancestors=embed_frame_ancestors_csp_value(env),
        connect_src=_csp_connect_src(_csp_extra_connect_src()),
        frame_src=_csp_frame_src(_csp_extra_frame_src()),
    )
