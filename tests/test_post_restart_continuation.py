"""Private owning-process continuation; all state and workers are disposable."""
import json
import os
import sqlite3
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest


def test_owning_routes_have_browser_independent_consumer():
    from api import routes
    assert callable(getattr(routes, "consume_post_restart_continuation", None))


@pytest.fixture
def armed(monkeypatch, tmp_path):
    from api import config, models, profiles, routes
    from api import post_restart_continuation as resume

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sidecars = tmp_path / "sessions"
    sidecars.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_CONTINUATION_DIR", str(private))
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "legacy-direct")
    monkeypatch.setenv("HERMES_WEBUI_CHAT_BACKEND", "legacy")
    monkeypatch.setattr(config, "SESSION_DIR", sidecars)
    monkeypatch.setattr(models, "SESSION_DIR", sidecars)
    monkeypatch.setattr(profiles, "_resolve_profile_home_for_name", lambda p: tmp_path)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda s: False)
    s = models.Session(session_id="resume-session", profile="default", workspace=str(workspace), model="fake-model", model_provider="fake-provider", title="Existing")
    # Use the real pending preparation helper, with a deterministic disposable
    # sidecar persistence sink. No provider, Agent, native call or service.
    def save(*args, **kw):
        (sidecars / (s.session_id + ".json")).write_text(json.dumps({k: v for k, v in vars(s).items() if k != "save"}))
    s.save = save
    save()
    monkeypatch.setitem(routes.SESSIONS, s.session_id, s)
    monkeypatch.setattr(routes, "get_session", lambda sid: s)
    dbpath = tmp_path / "state.db"
    with sqlite3.connect(dbpath) as db:
        db.executescript("CREATE TABLE sessions(id TEXT, source TEXT, profile_name TEXT, ended_at REAL, end_reason TEXT, parent_session_id TEXT); CREATE TABLE messages(id INTEGER, session_id TEXT, role TEXT, content TEXT);")
        db.execute("INSERT INTO sessions VALUES (?, 'webui', 'default', NULL, NULL, NULL)", (s.session_id,))
    op = "a" * 32
    deployment = resume.release_binding()
    binding = resume.inspect_binding(s.session_id, "default", str(workspace))
    request = {"operation_admission_id": op, "controller_operation_id": "controller-operation-1", "deployment": deployment, "binding": binding, "runtime_adapter": "legacy-direct", "message": "Continue the requested task."}
    proof = {"status": "terminal_verified", "operation_admission_id": op, "controller_operation_id": "controller-operation-1", "request_digest": resume.digest(request), "owner_pid": os.getpid(), "owner_started_at": routes.SERVER_START_TIME, "deployment": deployment}
    def publish():
        for name, value in (("request.json", request), ("terminal.json", proof)):
            path = private / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)
    publish()
    calls, entered = [], threading.Event()
    def worker(*args, **kwargs):
        entry = json.loads((private / (op + ".entered.json")).read_text())
        claim = json.loads((private / (op + ".claim.json")).read_text())
        assert entry["operation_admission_id"] == claim["operation_admission_id"] == op
        assert entry["stream_id"] == args[4]
        calls.append(args)
        entered.set()
    monkeypatch.setattr(routes, "_run_agent_streaming", worker)
    obj = SimpleNamespace(routes=routes, resume=resume, private=private, session=s, dbpath=dbpath, op=op, deployment=deployment, request=request, proof=proof, publish=publish, calls=calls, entered=entered)
    obj.consume = lambda: routes.consume_post_restart_continuation(deployment)
    yield obj
    if s.active_stream_id:
        routes._cleanup_chat_start_launch_failure(s, s.active_stream_id)


@pytest.mark.parametrize("mode", ["legacy-direct", "legacy-journal"])
def test_real_route_launches_fake_worker_and_durable_same_id(armed, monkeypatch, mode):
    a = armed
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", mode)
    a.request["runtime_adapter"] = mode
    a.proof["request_digest"] = a.resume.digest(a.request)
    a.publish()
    result = a.consume()
    assert result["status"] == "claimed"
    assert a.entered.wait(3)
    assert len(a.calls) == 1
    assert result["operation_admission_id"] == result["result"]["operation_admission_id"] == a.op
    assert a.session.pending_user_message == a.request["message"]
    assert a.consume()["status"] == "already_claimed"
    assert len(a.calls) == 1


def test_uncached_session_uses_real_lookup_before_real_route_launch(armed, monkeypatch):
    from api import models
    a = armed
    # Exercise process-restart-shaped cache loss using real Session loading and
    # pending persistence, not the fixture's lightweight get_session/save sink.
    monkeypatch.delitem(a.routes.SESSIONS, a.session.session_id)
    monkeypatch.setattr(a.routes, "get_session", models.get_session)
    result = a.consume()
    try:
        assert result["status"] == "claimed", result
        assert a.entered.wait(3)
        loaded = models.get_session(a.session.session_id)
        assert loaded.pending_user_message == a.request["message"]
        assert loaded is not a.session
        assert len(a.calls) == 1
        assert a.consume()["status"] == "already_claimed"
    finally:
        stream_id = (result.get("result") or {}).get("stream_id")
        if stream_id:
            a.routes._cleanup_chat_start_launch_failure(models.get_session(a.session.session_id), stream_id)


def test_startup_consumer_waits_for_terminal_proof_without_browser(armed):
    a = armed
    (a.private / "terminal.json").unlink()
    assert a.consume()["status"] == "waiting"
    stop = threading.Event()
    # Execute the exact startup helper without importing server.py (whose
    # module-level initialization would discover live/native dependencies).
    import ast
    from pathlib import Path
    tree = ast.parse((Path(__file__).resolve().parents[1] / "server.py").read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_start_post_restart_continuation_consumer")
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "server.py", "exec"), namespace)
    thread = namespace["_start_post_restart_continuation_consumer"](stop)
    try:
        a.publish()
        assert a.entered.wait(3)
        thread.join(3)
        assert not thread.is_alive()
        assert len(a.calls) == 1
    finally:
        stop.set()
        thread.join(3)


def test_duplicate_consumers_and_reinstantiation(armed):
    a = armed
    barrier = threading.Barrier(3)
    results = []
    def run():
        barrier.wait()
        results.append(a.consume())
    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join(3)
        assert not t.is_alive()
    assert a.entered.wait(3)
    assert sorted(r["status"] for r in results) == ["already_claimed", "claimed"], results
    # A fresh Store, no in-memory dedup state, sees the receipt even when the
    # first response is discarded and live session state is no longer present.
    with a.resume.Store(a.private) as fresh:
        assert fresh.read(a.op + ".claim.json")["operation_admission_id"] == a.op
    assert a.consume()["status"] == "already_claimed"
    assert len(a.calls) == 1


def test_busy_before_mutation(armed, monkeypatch):
    a = armed
    before = dict(vars(a.session))
    monkeypatch.setattr(a.routes, "_active_run_stream_for_session", lambda sid: "human-stream")
    assert a.consume()["status"] == "busy"
    assert vars(a.session) == before
    assert not list(a.private.glob("*.claim.json"))
    assert not a.calls


@pytest.mark.parametrize("change", ["workspace", "profile", "revision", "closed", "rotated", "foreign", "missing"])
def test_changed_binding_is_refused_without_mutation(armed, change):
    a = armed
    if change in ("workspace", "profile", "revision"):
        setattr(a.session, {"revision": "title"}.get(change, change), "changed")
        a.session.save()
    else:
        with sqlite3.connect(a.dbpath) as db:
            if change == "missing":
                db.execute("DELETE FROM sessions")
            else:
                column, value = {"closed": ("end_reason", "closed"), "rotated": ("end_reason", "compression"), "foreign": ("source", "cli")}[change]
                db.execute(f"UPDATE sessions SET {column}=?", (value,))
    before = dict(vars(a.session))
    assert a.consume()["status"] == "refused_or_ambiguous"
    assert vars(a.session) == before
    assert not a.calls
    assert not list(a.private.glob("*.claim.json"))


@pytest.mark.parametrize("mode,backend", [(None, "legacy"), ("bogus", "legacy"), ("runner-local", "legacy"), ("legacy-direct", "gateway"), ("legacy-direct", None), ("legacy-direct", "unknown")])
def test_unknown_external_backends_fail_closed(armed, monkeypatch, mode, backend):
    for key, value in (("HERMES_WEBUI_RUNTIME_ADAPTER", mode), ("HERMES_WEBUI_CHAT_BACKEND", backend)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(armed.routes, "get_config", lambda: {})
    assert armed.consume()["status"] == "refused_or_ambiguous"
    assert not armed.calls
    assert not armed.session.pending_user_message


@pytest.mark.parametrize("unsafe", ["mode", "symlink", "hardlink", "op", "proof", "release"])
def test_unsafe_request_or_proof_refused(armed, unsafe):
    a = armed
    path = a.private / "request.json"
    if unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "symlink":
        target = a.private / "other"
        path.rename(target)
        path.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(path, a.private / "other")
    else:
        if unsafe == "op":
            a.request["operation_admission_id"] = "../bad"
        elif unsafe == "proof":
            a.proof["owner_started_at"] = -1
        else:
            a.proof["deployment"] = {"release": "other"}
        a.publish()
    assert a.consume()["status"] == "refused_or_ambiguous"
    assert not a.calls
    assert not armed.session.pending_user_message


def test_foreign_owned_private_request_refused(armed, monkeypatch):
    a = armed
    inode = (a.private / "request.json").stat().st_ino
    original = a.resume.os.fstat
    def stat_file(fd):
        info = original(fd)
        if info.st_ino == inode:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1, st_nlink=1)
        return info
    monkeypatch.setattr(a.resume.os, "fstat", stat_file)
    assert a.consume()["status"] == "refused_or_ambiguous"
    assert not a.calls
    assert not a.session.pending_user_message


def test_unsafe_private_directory_refused(armed):
    a = armed
    a.private.chmod(0o755)
    assert a.consume()["status"] == "refused_or_ambiguous"
    assert not a.calls
    assert not a.session.pending_user_message


def test_claim_fsync_failure_before_any_mutation(armed, monkeypatch):
    a = armed
    before = dict(vars(a.session))
    with monkeypatch.context() as m:
        m.setattr(a.resume.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
        assert a.consume()["status"] == "refused_or_ambiguous"
        assert vars(a.session) == before
        assert not a.calls
    # Partial/successfully buffered but unacknowledged claim is not permission
    # to try again.
    assert a.consume()["status"] == "already_claimed"
    assert not a.calls


def test_accepted_response_lost_is_not_replayed(armed, monkeypatch):
    a = armed
    original = a.resume.Store.write
    def write(store, name, value):
        if name.endswith(".result.json"):
            raise OSError("lost result")
        return original(store, name, value)
    monkeypatch.setattr(a.resume.Store, "write", write)
    assert a.consume()["status"] == "refused_or_ambiguous"
    assert a.entered.wait(3)
    retry = a.consume()
    assert retry["status"] == "already_claimed"
    assert retry["worker_entry"]["operation_admission_id"] == a.op
    assert retry["result"] is None
    assert len(a.calls) == 1


def test_worker_entry_receipt_failure_never_enters_worker(armed, monkeypatch):
    a = armed
    original = a.resume.Store.write
    cleaned = threading.Event()
    cleanup = a.routes._cleanup_chat_start_launch_failure
    def write(store, name, value):
        if name.endswith(".entered.json"):
            raise OSError("receipt unavailable")
        return original(store, name, value)
    def on_failure(*args):
        cleanup(*args)
        cleaned.set()
    monkeypatch.setattr(a.resume.Store, "write", write)
    monkeypatch.setattr(a.routes, "_cleanup_chat_start_launch_failure", on_failure)
    assert a.consume()["status"] == "claimed"
    assert cleaned.wait(3)
    assert not a.calls
    assert a.consume()["status"] == "already_claimed"
    assert not a.session.active_stream_id


@pytest.mark.parametrize("entry", ["http", "server"])
def test_continuation_wins_ordinary_start_refused_before_mutation(armed, monkeypatch, entry):
    a = armed
    assert a.consume()["status"] == "claimed"
    assert a.entered.wait(3)
    before = dict(vars(a.session))
    monkeypatch.setattr(a.routes, "_get_or_materialize_session", lambda *args, **kw: pytest.fail("HTTP materialized before busy admission"))
    monkeypatch.setattr(a.routes, "_resolve_chat_workspace_with_recovery", lambda *args: pytest.fail("workspace mutated before busy admission"))
    monkeypatch.setattr(a.routes, "j", lambda handler, result, status=200: dict(result, _status=status))
    if entry == "http":
        result = a.routes._handle_chat_start(object(), {"session_id": a.session.session_id, "message": "Human followup", "workspace": "/changed"})
    else:
        result = a.routes.start_session_turn(a.session.session_id, "Human followup", source="webui")
    assert result["_status"] == 409
    assert vars(a.session) == before
    assert len(a.calls) == 1


@pytest.mark.parametrize("entry", ["http", "server"])
def test_human_wins_consumer_waits_until_before_claim_revalidation(armed, monkeypatch, entry):
    a = armed
    reached, release, attempted = threading.Event(), threading.Event(), threading.Event()
    blocked = threading.Event()
    class ObservedLock:
        def __init__(self):
            self.lock = threading.RLock()
        def __enter__(self):
            if not self.lock.acquire(blocking=False):
                blocked.set()
                self.lock.acquire()
            return self
        def __exit__(self, *exc):
            self.lock.release()
    monkeypatch.setitem(a.resume._LOCKS, a.session.session_id, ObservedLock())
    human_done, results = [], []
    monkeypatch.setattr(a.routes, "j", lambda handler, result, status=200: dict(result, _status=status))
    monkeypatch.setattr(a.routes, "bad", lambda handler, error, status=400: {"_status": status, "error": error})
    monkeypatch.setattr(a.routes, "_get_or_materialize_session", lambda *args, **kw: a.session)
    monkeypatch.setattr(a.routes, "_session_visible_to_active_profile", lambda *args: True)
    monkeypatch.setattr(a.routes, "_get_active_profile_name", lambda: "default")
    from api import compression_continuation
    monkeypatch.setattr(compression_continuation, "durable_compression_continuation", lambda s: (False, None))
    def workspace(session, requested):
        # Real admission entrypoint reached its first mutating helper. Pause
        # here to expose a continuation-only TOCTOU implementation.
        reached.set()
        assert release.wait(3)
        session.title = "Human changed binding"
        session.save()
        raise ValueError("deterministic rejection after workspace binding update")
    monkeypatch.setattr(a.routes, "_resolve_chat_workspace_with_recovery", workspace)
    def human():
        if entry == "http":
            human_done.append(a.routes._handle_chat_start(object(), {"session_id": a.session.session_id, "message": "Human"}))
        else:
            human_done.append(a.routes.start_session_turn(a.session.session_id, "Human", source="webui"))
    def continuation():
        attempted.set()
        results.append(a.consume())
    first = threading.Thread(target=human)
    second = threading.Thread(target=continuation)
    first.start()
    assert reached.wait(3)
    second.start()
    assert attempted.wait(3)
    assert blocked.wait(3), "consumer did not participate in the ordinary admission edge"
    # The consumer cannot claim while a prior ordinary mutating start holds
    # admission. Final binding revalidation sees the persisted human change.
    assert not list(a.private.glob("*.claim.json"))
    release.set()
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert human_done[0]["_status"] == 400
    assert results[0]["status"] == "refused_or_ambiguous"
    assert not list(a.private.glob("*.claim.json"))
    assert not a.calls


def test_receipt_survives_fresh_python_process(armed):
    import subprocess
    import sys
    a = armed
    assert a.consume()["status"] == "claimed"
    assert a.entered.wait(3)
    # Only the standalone private store module is imported, never Agent/native
    # services. A fresh interpreter has no access to the first process's locks.
    script = "from api.post_restart_continuation import consume; import json; print(json.dumps(consume(None, {})))"
    result = subprocess.run([sys.executable, "-c", script], cwd=str(Path(__file__).resolve().parents[1]), env=dict(os.environ), text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "already_claimed"
    assert receipt["worker_entry"]["operation_admission_id"] == a.op
    assert len(a.calls) == 1


def test_independent_processes_share_atomic_claim_boundary(armed):
    import subprocess
    import sys
    script = """from api.post_restart_continuation import Store
import os
with Store(os.environ['HERMES_WEBUI_CONTINUATION_DIR']) as store, store.locked():
    if store.read('process-claim.json') is None:
        store.write('process-claim.json', {'status': 'claimed'})
        print('claimed')
    else:
        print('existing')
"""
    processes = [subprocess.Popen([sys.executable, "-c", script], cwd=str(Path(__file__).resolve().parents[1]), env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    results = []
    for process in processes:
        out, err = process.communicate(timeout=5)
        assert process.returncode == 0, err
        results.append(out.strip())
    assert sorted(results) == ["claimed", "existing"]


def test_frozen_release_identity_needs_no_git_checkout(armed, monkeypatch, tmp_path):
    import hashlib
    a = armed
    root = Path(__file__).resolve().parents[1]
    frozen = tmp_path / "frozen"
    files = ("server.py", "api/routes.py", "api/post_restart_continuation.py", "api/runtime_adapter.py", "api/streaming.py")
    hashes = {}
    for name in files:
        target = frozen / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / name).read_bytes())
        hashes[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    deployment = {"release": str(frozen), "release_revision": "b" * 40, "execution_digest": a.resume.digest(hashes)}
    with a.resume.Store(a.private) as store:
        store.write("deployment.json", deployment)
    monkeypatch.setattr(a.resume, "__file__", str(frozen / "api/post_restart_continuation.py"))
    assert not (frozen / ".git").exists()
    assert a.resume.release_binding() == deployment
    (frozen / "api/streaming.py").write_text("changed")
    with pytest.raises(a.resume.Refused):
        a.resume.release_binding()


def test_operation_id_cannot_be_reused_with_new_request(armed):
    a = armed
    assert a.consume()["status"] == "claimed"
    assert a.entered.wait(3)
    a.request["message"] = "Different instruction"
    a.proof["request_digest"] = a.resume.digest(a.request)
    a.publish()
    assert a.consume()["status"] == "refused_or_ambiguous"
    assert len(a.calls) == 1


def test_postclaim_crash_never_replays(armed, monkeypatch):
    a = armed
    monkeypatch.setattr(a.routes, "get_session", lambda sid: (_ for _ in ()).throw(OSError("crash")))
    assert a.consume()["status"] == "refused_or_ambiguous"
    assert a.consume()["status"] == "already_claimed"
    assert not a.calls
    assert not a.session.pending_user_message
