"""Explicit browser device grants. Bearers are independent of WebUI sessions.

State belongs to STATE_DIR, not a selected agent profile. Only hashes of device
secrets and access tokens are persisted. Origin binding is not proof of possession:
a stolen bearer can be replayed by a non-browser client spoofing Origin or the
public client-ID header. Neither header is browser attestation.
"""
import base64
from contextlib import contextmanager
try:
    import fcntl
except ImportError:  # Native Windows
    fcntl = None
    import msvcrt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlparse

PREFIX = '/api/auth/extension/'
PUBLIC = {PREFIX + 'start', PREFIX + 'token'}
ORIGIN_RE = re.compile(r'chrome-extension://([a-p]{32})\Z')
LOCK = threading.RLock()
MAX_PENDING = 256
MAX_PER_IP = 8
MAX_DEVICES = 256
SCOPES = {'chat', 'control', 'cdp'}

# Exact method/path grants: new server routes are denied until reviewed here.
CHAT_GET = set(('session sessions sessions/search media sidecar/identity session/status '
    'session/usage session/export session/draft session/conversation-rounds '
    'session/compress/status session/worktree/status chat/stream/status chat/stream chat/cancel '
    'models models/live models/catalog workspaces commands prompts personalities '
    'approval/pending clarify/pending background/status transcribe/capability '
    'profile/active').split())
CHAT_POST = set(('session/new session/rename session/archive session/pin session/draft '
    'session/update session/delete session/duplicate session/branch session/clear '
    'session/retry session/undo session/truncate session/compress/start '
    'session/title/regenerate chat/start chat/steer chat btw upload '
    'upload/extract approval/respond clarify/respond bg-task-complete-ack '
    'transcribe tts background').split())
CONTROL_GET = set(('settings providers providers/self-hosted provider/quota '
    'provider/cost-history skills skills/content skills/usage memory crons '
    'crons/status crons/recent crons/history crons/output crons/delivery-options '
    'logs system/health health/agent gateway/status dashboard/status dashboard/config '
    'file file/raw file/path list folder/download git-info git/status git/diff '
    'git/branches projects insights plugins extensions/status extensions/registry '
    'mcp/servers mcp/tools terminal/output reasoning goal model/auxiliary '
    'rollback/list rollback/diff notes/search notes/sources notes/item '
    'wiki/status wiki/browse wiki/page voice/live/status voice/live/capability '
    'updates/summary kanban/boards kanban/board').split())
CONTROL_POST = set(('settings default-model model/set model/auxiliary models/refresh '
    'personality/set reasoning goal commands/exec commands/bundles/resolve '
    'commands/moa/resolve session/yolo session/toolsets session/import '
    'session/import_cli session/move session/worktree/remove sessions/cleanup '
    'sessions/cleanup_zero_message memory/write skills/save skills/delete skills/toggle '
    'crons/create crons/update crons/delete crons/pause crons/resume crons/run '
    'workspaces/add workspaces/remove workspaces/rename workspaces/reorder '
    'workspace/upload file/create file/create-dir file/delete file/move '
    'file/rename file/save file/office-save file/open-vscode file/reveal '
    'git/checkout git/commit git/commit-selected git/commit-message '
    'git/commit-message-selected git/discard git/fetch git/pull git/push '
    'git/stage git/unstash git/stash-checkout git/unstage projects/create '
    'projects/delete projects/rename terminal/start terminal/input terminal/resize '
    'terminal/close rollback/restore gateway/start gateway/stop gateway/restart '
    'share/create share/revoke voice/live/connect voice/live/disconnect '
    'voice/live/ask voice/live/sdp voice/live/steer voice/live/stop voice/live/turn '
    'admin/reload updates/check kanban/tasks').split())
CDP_POST = {'sidecar/cdp/' + x for x in ('register', 'poll', 'respond', 'unregister', 'command')}


def allowed(method, path, scopes):
    route = path.removeprefix('/api/')
    if path == PREFIX + 'me' and method == 'GET':
        return True
    if path == PREFIX + 'revoke' and method == 'POST':
        return True
    if not path.startswith('/api/'):
        return False
    # Shared boards are an explicit administrative control, not profile-private
    # chat data. Only the implemented read/create/edit routes are granted;
    # agent dispatch, board administration, deletion and event feeds are not.
    if 'control' in scopes and (
        (method == 'GET' and re.fullmatch(r'kanban/tasks/[A-Za-z0-9_-]{1,128}', route))
        or (method == 'POST' and re.fullmatch(r'kanban/tasks/[A-Za-z0-9_-]{1,128}/patch', route))
    ):
        return True
    return ((('chat' in scopes or 'control' in scopes) and route in
             (CHAT_GET if method == 'GET' else CHAT_POST if method == 'POST' else set()))
            or ('control' in scopes and route in
                (CONTROL_GET if method == 'GET' else CONTROL_POST if method == 'POST' else set()))
            or ('cdp' in scopes and ((method == 'POST' and route in CDP_POST)
                or (method == 'GET' and route == 'sidecar/cdp/relays'))))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _lock_file(fd):
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
    else:
        # Windows locks byte ranges, including beyond EOF. All processes lock
        # the same byte; closing the descriptor releases it on every exit.
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)


def _private_fd(fd):
    if hasattr(os, 'fchmod'):
        os.fchmod(fd, 0o600)


def _sync_directory(root):
    # Windows does not support opening/fsyncing directories through os.open.
    # File flush + atomic replace remain required on both platforms.
    if os.name == 'nt':
        return
    dirfd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


@contextmanager
def store(*, write=True):
    from api.config import STATE_DIR
    root = Path(STATE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    with LOCK:
        fd = os.open(root / 'extension-auth.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _private_fd(fd)
            _lock_file(fd)
            path = root / 'extension-auth.json'
            data = json.loads(path.read_text()) if path.exists() else {'pending': {}, 'devices': {}}
            now = time.time()
            for key in ('pending', 'devices'):
                data[key] = {k: v for k, v in data[key].items() if v['expires_at'] > now}
            yield data
            if not write:
                return
            tmp_fd, tmp = tempfile.mkstemp(prefix='.extension-auth-', dir=root)
            try:
                with os.fdopen(tmp_fd, 'w') as stream:
                    _private_fd(stream.fileno())
                    json.dump(data, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, path)
                _sync_directory(root)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        finally:
            os.close(fd)


def reply(handler, payload, status=200):
    from api.helpers import j
    return j(handler, payload, status=status, extra_headers={'Cache-Control': 'no-store'})


def origin(handler):
    value = handler.headers.get('Origin', '')
    return value if ORIGIN_RE.fullmatch(value) else None


def client_matches(handler, principal):
    """Bind a bearer to its client, not to ambient cookies or browser attestation.

    Chromium may omit Origin on extension GETs. Only an absent Origin permits
    the explicit public client ID; an invalid/present Origin never falls back.
    """
    if handler.headers.get('Origin') is not None:
        value = origin(handler)
        return bool(value and value == principal['origin'])
    client_id = handler.headers.get('X-Hermes-Extension-Id', '')
    return bool(re.fullmatch(r'[a-p]{32}', client_id)
                and client_id == principal['extension_id'])


def public_request(handler):
    return bool(origin(handler) and not handler.headers.get('Cookie')
                and not handler.headers.get('Authorization') and handler.command == 'POST')


def authenticate(handler, parsed):
    """Return None for ordinary cookie auth; False must never fall back."""
    handler._extension_principal = None
    auth = handler.headers.get('Authorization')
    if auth is None:
        if parsed.path in PUBLIC:
            if public_request(handler):
                return True
            reply(handler, {'error': 'invalid_request'}, 403)
            return False
        # Extension origins must never ride ambient cookies, even with auth off.
        if (handler.headers.get('Origin', '').startswith('chrome-extension:')
                or handler.headers.get('X-Hermes-Extension-Id') is not None):
            reply(handler, {'error': 'bearer_required'}, 401)
            return False
        return None
    if not auth.startswith('Bearer ') or len(auth) > 256 or handler.headers.get('Cookie'):
        reply(handler, {'error': 'invalid_token'}, 401)
        return False
    with store(write=False) as data:
        principal = data['devices'].get(digest(auth[7:]))
        if principal:
            principal = dict(principal)
    if not principal or not client_matches(handler, principal):
        reply(handler, {'error': 'invalid_token'}, 401)
        return False
    selected = handler.headers.get('X-Hermes-Profile', principal['profile'])
    query = parse_qs(parsed.query)
    if (selected != principal['profile'] or any(k in query for k in ('all_profiles', 'profile', 'profiles'))
            or not allowed(handler.command, parsed.path, principal['scopes'])):
        reply(handler, {'error': 'insufficient_scope'}, 403)
        return False
    from api.profiles import set_request_profile, get_active_profile_name
    set_request_profile(selected)
    if get_active_profile_name() != selected:
        reply(handler, {'error': 'profile_forbidden'}, 403)
        return False
    handler._extension_principal = principal
    return True


def csrf_allowed(handler):
    p = getattr(handler, '_extension_principal', None)
    return bool(p and client_matches(handler, p) and not handler.headers.get('Cookie')
                and allowed(handler.command, urlparse(handler.path).path, p['scopes']))


class AuthorizedStreamWriter:
    """Recheck revocation/expiry at every SSE event and heartbeat boundary."""
    def __init__(self, raw, handler):
        self._raw = raw
        self._handler = handler

    def write(self, data):
        handler = self._handler
        old = handler._extension_principal
        try:
            with store(write=False) as state:
                current = state['devices'].get(digest(handler.headers['Authorization'][7:]))
                valid = (current and current['device_id'] == old['device_id']
                         and current['origin'] == old['origin']
                         and current['profile'] == old['profile']
                         and allowed(handler.command, urlparse(handler.path).path, current['scopes']))
        except Exception:
            valid = False
        if not valid:
            handler.close_connection = True
            # Existing disconnect handlers unsubscribe and close the stream.
            raise BrokenPipeError('Device authorization ended')
        return self._raw.write(data)

    def __getattr__(self, name):
        return getattr(self._raw, name)


def media_root(handler, target, qs):
    """Authorize only this profile's session uploads or explicit workspace.

    Path-only upload URLs infer session_id from the inbox; workspace media
    requires session_id. The caller also applies the shared secret denylist.
    """
    from api.upload import _attachment_root, _session_attachment_dir
    from api.models import get_session, is_safe_session_id
    from api.profiles import _profiles_match
    from api.config import STATE_DIR
    attachment_root = _attachment_root().resolve()
    sid = qs.get('session_id', [''])[0]
    if not sid and target.is_relative_to(attachment_root):
        parts = target.relative_to(attachment_root).parts
        if len(parts) >= 2:
            sid = parts[0]
    if not sid or not is_safe_session_id(sid):
        return None
    try:
        session = get_session(sid, metadata_only=True)
    except (KeyError, ValueError):
        return None
    if not _profiles_match(getattr(session, 'profile', None), handler._extension_principal['profile']):
        return None
    uploads = _session_attachment_dir(sid)
    if target.is_relative_to(uploads):
        return uploads
    protected = [Path(STATE_DIR).resolve(), Path.home().joinpath('.hermes').resolve(),
                 Path(os.environ.get('HERMES_HOME', '~/.hermes')).expanduser().resolve()]
    if any(target.is_relative_to(root) for root in protected):
        return None
    workspace = getattr(session, 'workspace', None)
    if workspace:
        root = Path(workspace).expanduser().resolve()
        if root != Path(root.anchor) and target.is_relative_to(root):
            return root
    return None


def cors(handler):
    """CORS is not authorization. Actual requests still validate the bearer."""
    o = origin(handler)
    if not o or handler.headers.get('Cookie'):
        return
    path = urlparse(handler.path).path
    method = handler.headers.get('Access-Control-Request-Method') if handler.command == 'OPTIONS' else handler.command
    if not ((path in PUBLIC and method == 'POST') or allowed(method, path, SCOPES)):
        return
    handler.send_header('Access-Control-Allow-Origin', o)
    handler.send_header('Vary', 'Origin')
    if handler.command == 'OPTIONS':
        handler.send_header('Access-Control-Allow-Methods', 'GET, POST')
        handler.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type, X-Hermes-Profile, X-Hermes-Extension-Id')


def cookie_session(handler):
    from api.auth import get_session_info, parse_cookie
    return get_session_info(parse_cookie(handler) or '')


def handle_get(handler, parsed):
    if parsed.path == PREFIX + 'me':
        p = getattr(handler, '_extension_principal', None)
        if not p:
            return reply(handler, {'error': 'bearer_required'}, 401)
        return reply(handler, {k: p[k] for k in ('device_id', 'expires_at', 'scopes', 'profile', 'extension_id', 'client_name')} | {'profiles': [p['profile']]})
    if parsed.path not in ('/extension-pair', PREFIX + 'devices'):
        return False
    info = cookie_session(handler)
    if not info:
        return reply(handler, {'error': 'authenticated_cookie_required'}, 401)
    if parsed.path == PREFIX + 'devices':
        with store() as data:
            devices = [{k: v for k, v in p.items() if k != 'relays'} for p in data['devices'].values()
                       if not info.get('bound_profile') or p['profile'] == info['bound_profile']]
        return reply(handler, {'devices': devices})
    from api.auth import csrf_token_for_session, parse_cookie
    from api.helpers import t
    import html
    page = (Path(__file__).parent.parent / 'static/extension-pair.html').read_text()
    page = page.replace('__CSRF__', html.escape(csrf_token_for_session(parse_cookie(handler)) or '', quote=True))
    return t(handler, page, content_type='text/html; charset=utf-8', extra_headers={
        'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'"})


def handle_post(handler, parsed):
    # Do not emit success (especially a bearer) before the durable transaction
    # commits. A failed fsync/replace must never announce a consumed grant.
    payload, status = _handle_post(handler, parsed)
    return reply(handler, payload, status)


def _handle_post(handler, parsed):
    def reply(_handler, payload, status=200):
        return payload, status

    from api.routes import _check_csrf
    from api.helpers import read_body
    path = parsed.path
    if path not in PUBLIC | {PREFIX + x for x in ('inspect', 'approve', 'deny', 'revoke')}:
        return reply(handler, {'error': 'not_found'}, 404)
    if path in PUBLIC:
        if not public_request(handler):
            return reply(handler, {'error': 'invalid_request'}, 403)
    elif not _check_csrf(handler):
        return reply(handler, {'error': 'csrf_rejected'}, 403)
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if size < 0 or size > 8192:
            handler.close_connection = True
            return reply(handler, {'error': 'invalid_request'}, 400)
        body = read_body(handler)
        if not isinstance(body, dict):
            raise ValueError()
    except (ValueError, TypeError):
        return reply(handler, {'error': 'invalid_request'}, 400)
    now = time.time()
    if path in PUBLIC:
        ext = body.get('extension_id', ORIGIN_RE.fullmatch(origin(handler)).group(1) if path == PREFIX + 'token' else None)
        if not isinstance(ext, str) or origin(handler) != 'chrome-extension://' + ext:
            return reply(handler, {'error': 'invalid_extension'}, 400)
    with store() as data:
        # Durable fixed-window budgets bound even invalid polling/code guesses.
        window = int(now // 60)
        rates = data.setdefault('rates', {'window': window, 'total': 0, 'ips': {}})
        if rates['window'] != window:
            rates = data['rates'] = {'window': window, 'total': 0, 'ips': {}}
        ip_key = digest(str(handler.client_address[0]))
        count = rates['ips'].get(ip_key, 0)
        if (rates['total'] >= 2048 or count >= 60
                or (ip_key not in rates['ips'] and len(rates['ips']) >= 512)):
            return reply(handler, {'error': 'rate_limited'}, 429)
        rates['total'] += 1
        rates['ips'][ip_key] = count + 1
        if path == PREFIX + 'start':
            challenge = body.get('code_challenge', '')
            scopes = body.get('scopes', ['chat'])
            if isinstance(scopes, list):
                scopes = ['control' if s == 'controls' else s for s in scopes]
            name = body.get('client_name', 'Hermes extension')
            if (body.get('code_challenge_method', 'S256') != 'S256'
                    or not isinstance(challenge, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', challenge)
                    or not isinstance(scopes, list) or not all(isinstance(x, str) and x in SCOPES for x in scopes)
                    or not scopes or not isinstance(name, str) or not 1 <= len(name) <= 80):
                return reply(handler, {'error': 'invalid_request'}, 400)
            ip = str(handler.client_address[0])
            if len(data['pending']) >= MAX_PENDING or sum(p['ip'] == ip for p in data['pending'].values()) >= MAX_PER_IP:
                return reply(handler, {'error': 'rate_limited'}, 429)
            device = secrets.token_urlsafe(32)
            code = secrets.token_hex(5).upper()
            while any(p['user_code'] == code for p in data['pending'].values()):
                code = secrets.token_hex(5).upper()
            data['pending'][digest(device)] = dict(user_code=code, ip=ip, origin=origin(handler),
                extension_id=ext, client_name=name, challenge=challenge, scopes=sorted(set(scopes)),
                expires_at=now + 600, approved=False, last_poll=0)
            return reply(handler, dict(device_code=device, user_code=code, verification_uri='/extension-pair', expires_in=600, interval=3))
        if path == PREFIX + 'token':
            secret = body.get('device_code', '')
            verifier = body.get('code_verifier', '')
            if not isinstance(secret, str) or not isinstance(verifier, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier):
                return reply(handler, {'error': 'invalid_grant'}, 400)
            key = digest(secret)
            p = data['pending'].get(key)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            if not p or p['origin'] != origin(handler) or not hmac.compare_digest(p['challenge'], challenge):
                return reply(handler, {'error': 'invalid_grant'}, 400)
            if now - p['last_poll'] < 3:
                return reply(handler, {'error': 'slow_down'}, 429)
            p['last_poll'] = now
            if not p['approved']:
                return reply(handler, {'error': 'authorization_pending'}, 400)
            if len(data['devices']) >= MAX_DEVICES:
                return reply(handler, {'error': 'device_limit'}, 429)
            token = secrets.token_urlsafe(48)
            device_id = secrets.token_hex(16)
            expires = int(now + 30 * 86400)
            data['devices'][digest(token)] = dict(device_id=device_id, expires_at=expires,
                origin=p['origin'], extension_id=p['extension_id'], client_name=p['client_name'],
                profile=p['profile'], scopes=p['scopes'], relays=[])
            del data['pending'][key]
            return reply(handler, dict(access_token=token, device_id=device_id, expires_at=expires,
                                       scopes=p['scopes'], profile=p['profile'], profiles=[p['profile']]))
        principal = getattr(handler, '_extension_principal', None)
        info = cookie_session(handler) if not principal else None
        if path == PREFIX + 'revoke':
            target = principal['device_id'] if principal else body.get('device_id')
            if not principal and not info:
                return reply(handler, {'error': 'authenticated_cookie_required'}, 401)
            for key, p in list(data['devices'].items()):
                if p['device_id'] == target:
                    if info and info.get('bound_profile') and p['profile'] != info['bound_profile']:
                        return reply(handler, {'error': 'profile_forbidden'}, 403)
                    from api import sidecar_cdp
                    for relay_id in p['relays']:
                        sidecar_cdp.unregister_relay(relay_id)
                    del data['devices'][key]
            return reply(handler, {'ok': True})
        if not info:
            return reply(handler, {'error': 'authenticated_cookie_required'}, 401)
        code = body.get('user_code', '')
        pkey = next((k for k, p in data['pending'].items() if p['user_code'] == code), None)
        if not pkey:
            return reply(handler, {'error': 'invalid_code'}, 400)
        p = data['pending'][pkey]
        from api.profiles import get_active_profile_name
        profile = info.get('bound_profile') or get_active_profile_name()
        if path == PREFIX + 'inspect':
            return reply(handler, {k: p[k] for k in ('extension_id', 'client_name', 'scopes', 'user_code')} | {'profile': profile})
        if path == PREFIX + 'deny':
            del data['pending'][pkey]
            return reply(handler, {'ok': True})
        # Inspection is not consent: require explicit confirmation of exact grant.
        if body.get('profile') != profile or body.get('scopes') != p['scopes'] or body.get('confirm') is not True or p['approved']:
            return reply(handler, {'error': 'confirmation_required'}, 400)
        p.update(approved=True, profile=profile)
        return reply(handler, {'ok': True})


def body_profile_allowed(handler, body):
    p = getattr(handler, '_extension_principal', None)
    if not p:
        return True
    if not isinstance(body, dict):
        return False
    # Profile-selecting endpoints must not override the authenticated context.
    for key in ('profile', 'target_profile', 'source_profile'):
        if body.get(key) not in (None, '', p['profile']):
            return False
    if body.get('all_profiles') or body.get('profiles'):
        return False
    return True


def relay_guard(handler, path, body):
    """Check and persist per-device relay ownership before any relay action."""
    p = getattr(handler, '_extension_principal', None)
    if not p:
        return True
    rid = body.get('relay_id')
    if path.endswith('/register'):
        body['extension_id'] = 'device:' + p['device_id']
        return True
    if path.endswith('/command') and body.get('method') == 'cdp.listRelays':
        return True
    if rid not in p['relays']:
        reply(handler, {'error': 'relay_forbidden'}, 403)
        return False
    return True


def register_relay(handler, body, *, peer):
    from api import sidecar_cdp
    result = sidecar_cdp.register_relay(body, peer=peer)
    p = getattr(handler, '_extension_principal', None)
    if p:
        with store() as data:
            for device in data['devices'].values():
                if device['device_id'] == p['device_id']:
                    device['relays'] = [result['relay_id']]
    return result


def list_relays(handler):
    from api import sidecar_cdp
    rows = sidecar_cdp.list_relays()
    p = getattr(handler, '_extension_principal', None)
    return [r for r in rows if r['relay_id'] in p['relays']] if p else rows
