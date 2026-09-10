"""Narrow preference-sync scopes for extension device grants.

Adds `preferences:read` and `preferences:write`, strictly narrower than
`control`: they grant exactly the `/api/client/preferences` and
`/api/client/capabilities` routes and no other control surface. Existing
grants never gain the scopes implicitly — a device must be re-paired with an
explicit scope request (pairing requests are validated against SCOPES here, so
a stale client sending a new scope name without server support fails closed).
"""
from api.extension_auth import SCOPES  # re-exported for pairing validation

PREFERENCES_SCOPES = frozenset(('preferences:read', 'preferences:write'))

PREFERENCES_GET = frozenset(('client/preferences', 'client/capabilities'))
PREFERENCES_PATCH = frozenset(('client/preferences',))

# Method the preference contract freezes for conditional writes. PATCH is
# deliberately routed in server.py/api/routes.py; the auth table keys on the
# same (method, path) pairs so an unreviewed new method stays denied.
PREFERENCES_WRITE_METHOD = 'PATCH'


def preferences_allowed(method, path, scopes):
    """Allow only the preference routes, only under the narrow scopes.

    `control` must NOT imply these routes (and these scopes must not imply any
    control route) — the surface stays bidirectionally narrow.
    """
    route = path.removeprefix('/api/')
    if 'preferences:read' in scopes and method == 'GET' and route in PREFERENCES_GET:
        return True
    if ('preferences:write' in scopes and method == PREFERENCES_WRITE_METHOD
            and route in PREFERENCES_PATCH):
        return True
    return False
