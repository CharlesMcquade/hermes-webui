"""Opt-in private local continuation admission. Not an HTTP/auth surface.

One owning WebUI process is required. flock fences receipt consumers, NOT
external Agent/CLI writers. A claim is irrevocable, including after a crash.
"""
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
from urllib.parse import quote

_LOCKS = {}
_LOCKS_LOCK = threading.Lock()
_ENV = "HERMES_WEBUI_CONTINUATION_DIR"


@contextmanager
def admission(session_id):
    # Same outer edge for HTTP, server wakeups and continuation. Never acquire
    # this while holding session/stream registry locks.
    with _LOCKS_LOCK:
        lock = _LOCKS.setdefault(str(session_id), threading.RLock())
    with lock:
        yield


def serialized_start(kind):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not os.environ.get(_ENV):
                return fn(*args, **kwargs)
            handler, body = None, {}
            if kind == "http":
                handler = args[0] if args else kwargs["handler"]
                body = args[1] if len(args) > 1 else kwargs["body"]
                sid = body.get("session_id")
            elif kind == "session":
                sid = args[0] if args else kwargs["session_id"]
            else:
                sid = (args[0] if args else kwargs["s"]).session_id
            with admission(sid):
                if kind in ("http", "session"):
                    # Ordinary starts must also reject a winning continuation
                    # before workspace/model/materialization can mutate state.
                    from api import routes
                    cached = routes.SESSIONS.get(sid)
                    stream = routes._active_run_stream_for_session(sid)
                    current = getattr(cached, "active_stream_id", None)
                    if not stream and current and routes._active_stream_blocks_chat_start(cached, current):
                        stream = current
                    message = body.get("message") if kind == "http" else (args[1] if len(args) > 1 else kwargs.get("message"))
                    visible = kind != "http" or (cached is not None and routes._session_visible_to_active_profile(getattr(cached, "profile", None), handler))
                    if stream and visible and not routes._is_silent_control_message(message):
                        result = {"error": "session already has an active stream", "active_stream_id": stream, "_status": 409}
                        if kind == "http":
                            return routes.j(handler, result, status=409)
                        return result
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class Refused(ValueError):
    pass


def _check_file(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise Refused("unsafe private file")


class Store:
    """Descriptor-relative, same-UID 0700 store with immutable 0600 records."""
    def __init__(self, path):
        self.path = Path(path)
        self.fd = -1

    def __enter__(self):
        if not self.path.is_absolute() or str(self.path) != str(self.path.resolve()):
            raise Refused("private store must be an absolute non-symlink path")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.path.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                if info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
                    raise Refused("unsafe private store ancestor")
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise Refused("private store must be owned 0700")
            self.fd = fd
            return self
        except BaseException:
            os.close(fd)
            raise

    def __exit__(self, *exc):
        os.close(self.fd)

    def read(self, name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "r") as f:
            _check_file(f.fileno())
            data = f.read(131073)
            if len(data) > 131072:
                raise Refused("oversized private record")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise Refused("invalid private record")
            return result

    def write(self, name, value):
        # Never unlink on failure: even a partial claim is an ambiguity fence.
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        with os.fdopen(fd, "w") as f:
            _check_file(f.fileno())
            json.dump(value, f, sort_keys=True, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.fsync(self.fd)

    @contextmanager
    def locked(self):
        import fcntl
        try:
            fd = os.open("lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            try:
                fd = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
            except FileExistsError:
                fd = os.open("lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        try:
            _check_file(fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def release_binding():
    """Pin checkout identity AND loaded execution sources; never run git/native tools."""
    root = Path(__file__).resolve().parent.parent
    files = ("server.py", "api/routes.py", "api/post_restart_continuation.py", "api/runtime_adapter.py", "api/streaming.py")
    execution_digest = digest({f: hashlib.sha256((root / f).read_bytes()).hexdigest() for f in files})
    # Frozen releases deliberately have no mutable Git checkout. Their trusted
    # supervisor publishes build identity from the independently validated
    # sealed inventory. Verify the local execution bytes/path, never infer a
    # release from request.json or require git/network during production boot.
    private = os.environ.get(_ENV)
    if private:
        with Store(private) as store:
            installed = store.read("deployment.json")
        if installed is not None:
            if (set(installed) != {"release", "release_revision", "execution_digest"}
                    or installed.get("release") != str(root)
                    or installed.get("execution_digest") != execution_digest
                    or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(installed.get("release_revision", "")))):
                raise Refused("installed release identity mismatch")
            return installed
    # Development checkout fallback; only read Git metadata, never run git.
    gitdir = root / ".git"
    if gitdir.is_file():
        pointer = gitdir.read_text().strip()
        if not pointer.startswith("gitdir: "):
            raise Refused("unknown release checkout")
        gitdir = (root / pointer[len("gitdir: "):]).resolve()
    revision = (gitdir / "HEAD").read_text().strip()
    if revision.startswith("ref: "):
        ref = revision[len("ref: "):]
        if not re.fullmatch(r"refs/[A-Za-z0-9_./-]+", ref) or ".." in ref:
            raise Refused("invalid release ref")
        common = gitdir
        if (gitdir / "commondir").is_file():
            common = (gitdir / (gitdir / "commondir").read_text().strip()).resolve()
        revision = ""
        for base in (gitdir, common):
            if (base / ref).is_file():
                revision = (base / ref).read_text().strip()
                break
        if not revision and (common / "packed-refs").is_file():
            for line in (common / "packed-refs").read_text().splitlines():
                fields = line.split()
                if len(fields) == 2 and fields[1] == ref:
                    revision = fields[0]
                    break
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise Refused("unknown release revision")
    return {"release": str(root), "release_revision": revision,
            "execution_digest": execution_digest}


def inspect_binding(session_id, profile, workspace):
    """Read authoritative durable state without Session.load's recovery writes."""
    from api import config
    from api.profiles import _resolve_profile_home_for_name
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", profile):
        raise Refused("invalid session/profile")
    if not Path(workspace).is_dir() or str(Path(workspace).resolve()) != workspace:
        raise Refused("workspace missing or aliased")
    sidecar_path = config.SESSION_DIR / (session_id + ".json")
    if sidecar_path.is_symlink():
        raise Refused("aliased sidecar")
    sidecar = json.loads(sidecar_path.read_text())
    if any(sidecar.get(k) for k in ("archived", "read_only", "is_cli_session", "pre_compression_snapshot", "gateway_run", "gateway_routing", "parent_session_id", "ended_at", "end_reason")):
        raise Refused("unsupported session")
    if (sidecar.get("session_id"), sidecar.get("profile"), sidecar.get("workspace")) != (session_id, profile, workspace):
        raise Refused("foreign binding")
    if not sidecar.get("model") or not sidecar.get("model_provider"):
        raise Refused("unknown model binding")
    if any(sidecar.get(k) for k in ("active_stream_id", "pending_user_message", "pending_started_at")):
        raise Refused("pending durable work")
    path = Path(_resolve_profile_home_for_name(profile)) / "state.db"
    with sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise Refused("missing durable owner")
        row = dict(row)
        if not {"source", "profile_name", "ended_at", "end_reason", "parent_session_id"}.issubset(row):
            raise Refused("unknown durable ownership schema")
        if row.get("source") != "webui" or row.get("profile_name") != profile:
            raise Refused("unknown or external owner")
        if row.get("ended_at") is not None or row.get("end_reason") or row.get("parent_session_id"):
            raise Refused("closed or rotated session")
        messages = [dict(r) for r in db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,))]
    return {"session_id": session_id, "profile": profile, "workspace": workspace,
            "model": sidecar["model"], "model_provider": sidecar["model_provider"],
            "session_revision": digest({"sidecar": sidecar, "row": row, "messages": messages})}


def supported_backend(routes):
    from api.runtime_adapter import runtime_adapter_mode
    from api.gateway_chat import WEBUI_LOCAL_CHAT_BACKEND
    mode = os.environ.get("HERMES_WEBUI_RUNTIME_ADAPTER")
    # The startup consumer has no browser profile context. Require the global
    # explicit environment selection rather than read another profile's config.
    raw = os.environ.get("HERMES_WEBUI_CHAT_BACKEND")
    # Intentionally stricter than ordinary fallback normalization.
    if mode not in ("legacy-direct", "legacy-journal") or runtime_adapter_mode() != mode or raw != WEBUI_LOCAL_CHAT_BACKEND:
        raise Refused("continuation requires explicit local legacy backend")
    return mode


class WorkerReceipt:
    def __init__(self, path, operation_id):
        self.path, self.operation_id = path, operation_id

    def wrap(self, target, on_failure):
        def entered(*args, **kwargs):
            # The executing thread, not Thread.start(), owns this receipt.
            # A failed receipt prevents entry to the Agent worker.
            try:
                with Store(self.path) as store, store.locked():
                    store.write(self.operation_id + ".entered.json", {
                        "operation_admission_id": self.operation_id,
                        "stream_id": args[4], "status": "worker_entered"})
            except Exception:
                on_failure()
                return
            return target(*args, **kwargs)
        return entered


def consume(routes, deployment):
    path = os.environ.get(_ENV)
    if not path:
        return {"status": "disabled"}
    identity = {}
    try:
        with Store(path) as store, store.locked():
            request = store.read("request.json")
            if request is None:
                return {"status": "waiting"}
            op = request.get("operation_admission_id")
            if not isinstance(op, str) or not re.fullmatch(r"[0-9a-f]{32}", op):
                raise Refused("invalid operation admission ID")
            identity = {"operation_admission_id": op}
            controller_operation = request.get("controller_operation_id")
            if not isinstance(controller_operation, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", controller_operation):
                raise Refused("invalid controller operation ID")
            claim = store.read(op + ".claim.json")
            if claim is not None:
                if claim.get("request_digest") != digest(request):
                    raise Refused("operation ID reused with different request")
                return {**identity, "status": "already_claimed", "result": store.read(op + ".result.json"), "worker_entry": store.read(op + ".entered.json")}
            proof = store.read("terminal.json")
            if proof is None:
                return {**identity, "status": "waiting"}
            if proof.get("status") != "terminal_verified":
                raise Refused("controller operation not verified")
            if (proof.get("request_digest") != digest(request)
                    or proof.get("operation_admission_id") != op
                    or proof.get("controller_operation_id") != controller_operation
                    or proof.get("owner_pid") != os.getpid()
                    or proof.get("owner_started_at") != routes.SERVER_START_TIME
                    or proof.get("deployment") != deployment
                    or request.get("deployment") != deployment):
                raise Refused("controller proof/binding mismatch")
            binding = request["binding"]
            msg = request["message"]
            if not isinstance(msg, str) or not msg.strip() or len(msg) > 32768 or msg.strip() == "[SILENT]":
                raise Refused("invalid continuation message")
            with admission(binding["session_id"]):
                mode = supported_backend(routes)
                if request.get("runtime_adapter") != mode:
                    raise Refused("adapter binding changed")
                sid = binding["session_id"]
                if routes._active_run_stream_for_session(sid):
                    return {**identity, "status": "busy"}
                cached = routes.SESSIONS.get(sid)
                if cached is not None and (getattr(cached, "active_stream_id", None) or getattr(cached, "pending_user_message", None)):
                    return {**identity, "status": "busy"}
                current = inspect_binding(sid, binding["profile"], binding["workspace"])
                if current != binding:
                    raise Refused("session binding changed")
                if cached is not None and any(getattr(cached, key, None) != binding[key]
                        for key in ("session_id", "profile", "workspace", "model", "model_provider")):
                    raise Refused("in-memory binding changed")
                if release_binding() != deployment:
                    raise Refused("release changed since startup")
                if routes._agent_runtime_barrier_response(runner_local_owned=True) is not None:
                    raise Refused("runtime not ready")
                store.write(op + ".claim.json", {**identity, "request_digest": digest(request), "status": "claimed"})
                # All subsequent failures are terminal ambiguity, never replay.
                session = routes.get_session(sid)
                if any(getattr(session, key, None) != binding[key] for key in
                        ("session_id", "profile", "workspace", "model", "model_provider")):
                    raise Refused("resolved session changed binding")
                if inspect_binding(sid, binding["profile"], binding["workspace"]) != binding:
                    raise Refused("session recovery changed revision")
                receipt = WorkerReceipt(path, op)
                result = routes._start_run(session, msg=msg, attachments=[], workspace=binding["workspace"],
                    model=binding["model"], model_provider=binding["model_provider"], normalized_model=False,
                    source="webui", route="post_restart_continuation", gateway_chat_enabled=False,
                    continuation_receipt=receipt)
                result = {**result, **identity}
                store.write(op + ".result.json", result)
                return {**identity, "status": "claimed", "result": result}
    except Exception as exc:
        # No prompt/path data in diagnostics. Partial/corrupt claim files remain
        # on disk and fail closed on all later attempts.
        return {**identity, "status": "refused_or_ambiguous", "reason": type(exc).__name__}


def start_consumer(consume_once, stop=None):
    """Nonblocking startup; terminal proof may arrive only AFTER HTTP health."""
    if not os.environ.get(_ENV):
        return None
    stop = stop or threading.Event()
    def loop():
        while not stop.is_set():
            result = consume_once()
            if result.get("status") not in ("waiting", "busy"):
                return
            stop.wait(0.5)
    thread = threading.Thread(target=loop, name="post-restart-continuation", daemon=True)
    thread.start()
    return thread
