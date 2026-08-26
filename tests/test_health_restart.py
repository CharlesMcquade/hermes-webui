"""Health route and shared gateway restart helper checks."""

import io
import subprocess
import threading
import types

import api.gateway_restart as gateway_restart
import api.routes as routes


class MockPopen:
    def __init__(
        self,
        args,
        *,
        stdout_text="",
        stderr_text="",
        returncode=0,
        communicate_timeout=False,
        wait_timeout=False,
        env=None,
    ):
        self.args = args
        self.env = env or {}
        self.returncode = returncode
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.communicate_timeout = communicate_timeout
        self.wait_timeout = wait_timeout
        self.terminated = False
        self.killed = False
        self.communicate_timeout_arg = None
        self.wait_timeout_arg = None

    def communicate(self, timeout=None):
        self.communicate_timeout_arg = timeout
        if self.communicate_timeout:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.stdout.getvalue(), self.stderr.getvalue()

    def wait(self, timeout=None):
        self.wait_timeout_arg = timeout
        if self.wait_timeout:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class InlineThread:
    def __init__(self, *, target, args=(), daemon=None):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)


def _call_health_restart(monkeypatch, helper_result):
    handler = types.SimpleNamespace()
    responses = []
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, **kw: responses.append((payload, kw.get("status", 200))) or True,
    )
    monkeypatch.setattr(routes, "restart_active_profile_gateway", lambda: dict(helper_result))
    return routes._handle_health_restart(handler), responses


def test_restart_active_profile_gateway_success_uses_active_profile_home(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    called = {}

    def fake_popen(args, stdout=None, stderr=None, text=True, env=None):
        called["args"] = args
        called["env"] = env
        return MockPopen(
            args,
            stdout_text="✓ Service restarted",
            returncode=0,
            env=env,
        )

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fake_popen)

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "completed"
    assert result["message"] == "Gateway service restarted successfully"
    assert called["args"] == ["/mock/bin/hermes", "--profile", "default", "gateway", "restart"]
    assert called["env"]["HERMES_HOME"] == "/mock/hermes/home"
    assert "_HERMES_GATEWAY" not in called["env"]
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_active_profile_gateway_pins_explicit_default_profile(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    called = {}

    def fake_popen(args, stdout=None, stderr=None, text=True, env=None):
        called["args"] = args
        called["env"] = env
        return MockPopen(args, stdout_text="ok", returncode=0, env=env)

    monkeypatch.setattr(
        gateway_restart,
        "get_hermes_home_for_profile",
        lambda profile: "/mock/hermes/default" if profile == "default" else "/mock/hermes/profiles/work",
    )
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fake_popen)

    result = gateway_restart.restart_active_profile_gateway(profile="default")

    assert result["status"] == "completed"
    assert called["args"] == ["/mock/bin/hermes", "--profile", "default", "gateway", "restart"]
    assert called["env"]["HERMES_HOME"] == "/mock/hermes/default"


def test_restart_active_profile_gateway_omits_profile_for_isolated_default_home(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    called = {}

    def fake_popen(args, stdout=None, stderr=None, text=True, env=None):
        called["args"] = args
        called["env"] = env
        return MockPopen(args, stdout_text="ok", returncode=0, env=env)

    monkeypatch.setattr(
        gateway_restart,
        "get_hermes_home_for_profile",
        lambda profile: "/mock/hermes/profiles/default",
    )
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fake_popen)

    result = gateway_restart.restart_active_profile_gateway(profile="default")

    assert result["status"] == "completed"
    assert called["args"] == ["/mock/bin/hermes", "gateway", "restart"]
    assert called["env"]["HERMES_HOME"] == "/mock/hermes/profiles/default"


def test_restart_active_profile_gateway_rejects_malformed_explicit_profile(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()

    def fail_popen(*args, **kwargs):
        raise AssertionError("malformed explicit profile must not launch subprocess")

    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fail_popen)

    for profile in ("", " default", "default ", "default\n", "../bad", "bad;echo"):
        result = gateway_restart.restart_active_profile_gateway(profile=profile)

        assert result["status"] == "failed"
        assert "Invalid profile for gateway restart" in result["message"]
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_active_profile_gateway_accepts_renamed_root_alias(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    called = {}

    def fake_popen(args, stdout=None, stderr=None, text=True, env=None):
        called["args"] = args
        called["env"] = env
        return MockPopen(args, stdout_text="ok", returncode=0, env=env)

    monkeypatch.setattr(
        gateway_restart,
        "get_hermes_home_for_profile",
        lambda profile: "/mock/hermes/root" if profile == "rootalias" else "/mock/hermes/other",
    )
    monkeypatch.setattr(
        gateway_restart,
        "_is_root_profile",
        lambda profile: profile in {"default", "rootalias"},
    )
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", fake_popen)

    result = gateway_restart.restart_active_profile_gateway(profile="rootalias")

    assert result["status"] == "completed"
    assert called["args"] == ["/mock/bin/hermes", "--profile", "default", "gateway", "restart"]
    assert called["env"]["HERMES_HOME"] == "/mock/hermes/root"


def test_restart_active_profile_gateway_failure_preserves_empty_output_contract(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()

    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(
        gateway_restart.subprocess,
        "Popen",
        lambda args, stdout=None, stderr=None, text=True, env=None: MockPopen(
            args,
            returncode=7,
            env=env,
        ),
    )

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "failed"
    assert result["message"] == "Restart failed: "
    assert result["returncode"] == 7
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_active_profile_gateway_timeout_releases_lock_after_background_wait(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    proc = MockPopen(
        ["/mock/bin/hermes", "gateway", "restart"],
        communicate_timeout=True,
        env={"HERMES_HOME": "/mock/hermes/home"},
    )

    monkeypatch.setattr(gateway_restart, "get_active_hermes_home", lambda: "/mock/hermes/home")
    monkeypatch.setattr(gateway_restart.shutil, "which", lambda cmd: "/mock/bin/hermes")
    monkeypatch.setattr(gateway_restart.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(gateway_restart.threading, "Thread", InlineThread)

    result = gateway_restart.restart_active_profile_gateway()

    assert result["status"] == "in_progress"
    assert proc.communicate_timeout_arg == 2.0
    assert proc.wait_timeout_arg == 240.0
    assert gateway_restart._GATEWAY_RESTART_LOCK.locked() is False


def test_restart_active_profile_gateway_busy_reports_contention(monkeypatch):
    gateway_restart._GATEWAY_RESTART_LOCK = threading.Lock()
    assert gateway_restart._GATEWAY_RESTART_LOCK.acquire(blocking=False) is True

    try:
        result = gateway_restart.restart_active_profile_gateway()
    finally:
        gateway_restart._GATEWAY_RESTART_LOCK.release()

    assert result == {
        "status": "busy",
        "message": "Restart already in progress. Please wait a moment and try again.",
    }


def test_handle_health_restart_success(monkeypatch):
    result, responses = _call_health_restart(
        monkeypatch,
        {"status": "completed", "message": "Gateway service restarted successfully"},
    )
    assert result is True
    assert responses == [({"ok": True, "message": "Gateway service restarted successfully"}, 200)]


def test_handle_health_restart_timeout(monkeypatch):
    result, responses = _call_health_restart(
        monkeypatch,
        {"status": "in_progress", "message": "Gateway service restart initiated (in progress)"},
    )
    assert result is True
    assert responses == [({"ok": True, "message": "Gateway service restart initiated (in progress)"}, 200)]


def test_handle_health_restart_failure_preserves_empty_output_message(monkeypatch):
    result, responses = _call_health_restart(
        monkeypatch,
        {"status": "failed", "message": "Restart failed: "},
    )
    assert result is True
    assert responses == [({"ok": False, "error": "Restart failed: "}, 500)]


def test_handle_health_restart_failure(monkeypatch):
    result, responses = _call_health_restart(
        monkeypatch,
        {"status": "failed", "message": "Restart failed: bad thing"},
    )
    assert result is True
    assert responses == [({"ok": False, "error": "Restart failed: bad thing"}, 500)]


def test_handle_health_restart_internal_error(monkeypatch):
    _, responses = _call_health_restart(
        monkeypatch,
        {"status": "failed", "message": "Internal error running restart: OSError: bad spawn"},
    )
    assert responses == [({"ok": False, "error": "Internal error running restart: OSError: bad spawn"}, 500)]


def test_handle_health_restart_concurrency(monkeypatch):
    _, responses = _call_health_restart(
        monkeypatch,
        {"status": "busy", "message": "Restart already in progress. Please wait a moment and try again."},
    )
    assert responses == [
        (
            {"ok": False, "error": "Restart already in progress. Please wait a moment and try again."},
            429,
        )
    ]


# ── _run_gateway_lifecycle_command env scrub ──────────────────────────────────
# The WebUI process imports gateway code (gateway/run.py sets
# os.environ["_HERMES_GATEWAY"]="1" at module load). The gateway lifecycle
# button hits /api/gateway/restart → _run_gateway_lifecycle_command, which must
# scrub _HERMES_GATEWAY from the child env — otherwise the child CLI trips the
# self-restart loop guard (hermes_cli/gateway.py:7828) and exits 1 before doing
# anything. This is the same fix already applied to restart_active_profile_gateway
# in gateway_restart.py:106, but the button's code path was never patched.

def test_run_gateway_lifecycle_command_scrubs_hermes_gateway_env(monkeypatch):
    """_run_gateway_lifecycle_command must not pass _HERMES_GATEWAY to the child."""
    routes._GATEWAY_ACTION_LOCK = threading.Lock()
    monkeypatch.setenv("_HERMES_GATEWAY", "1")

    called = {}

    class FakeCompletedProcess:
        def __init__(self, args, env):
            self.args = args
            self.returncode = 0
            self.stdout = "ok"
            self.stderr = ""

    def fake_run(cmd, *, cwd=None, env=None, capture_output=True, text=True, timeout=None):
        called["cmd"] = cmd
        called["env"] = env
        called["cwd"] = cwd
        return FakeCompletedProcess(cmd, env)

    # Stub the config/profile lookups so the function doesn't need real state
    import api.config as api_config
    monkeypatch.setattr(api_config, "_AGENT_DIR", "/mock/agent", raising=False)
    monkeypatch.setattr(api_config, "PYTHON_EXE", "/mock/python", raising=False)
    from pathlib import Path
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: "default", raising=False)
    monkeypatch.setattr(routes.subprocess, "run", fake_run)

    result = routes._run_gateway_lifecycle_command("restart")

    assert result.returncode == 0
    assert "_HERMES_GATEWAY" not in called["env"], (
        "_HERMES_GATEWAY must be scrubbed from the child env — the WebUI process "
        "inherits it from gateway/run.py:1929 at import time, and the child CLI "
        "trips the self-restart loop guard (hermes_cli/gateway.py:7828) if it leaks through."
    )
    assert called["env"].get("PYTHONUTF8") == "1"
    assert called["env"].get("BROWSER") == "echo"
