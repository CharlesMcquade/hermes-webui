"""Restart admission and real HTTP/1.1 request framing regressions."""
import http.client
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import io
import signal
import subprocess
import sys
from email.message import Message
from pathlib import Path
from unittest.mock import Mock

import pytest

import api.routes as routes
import api.webui_restart as restart


@pytest.fixture(autouse=True)
def isolated_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(restart.api_config, "STATE_DIR", tmp_path)
    lock = threading.Lock()
    monkeypatch.setattr(restart, "_RESTART_LOCK", lock)
    kill = Mock()
    monkeypatch.setattr(os, "kill", kill)
    yield kill
    assert lock.acquire(timeout=3), "restart lock leaked"
    lock.release()


class Request:
    def __init__(self, body=b"{}", headers=None):
        self.headers = Message()
        for key, value in (headers or {"Content-Length": str(len(body))}).items():
            self.headers[key] = value
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.close_connection = False
        self.status = None
        self.response_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    @property
    def payload(self):
        return json.loads(self.wfile.getvalue())


def manager(tmp_path, script="pass"):
    script_path = tmp_path / "preflight.py"
    script_path.write_text(script)
    argv = [sys.executable, str(script_path), "webui", "--check"]
    (tmp_path / "service-manager.json").write_text(json.dumps({"webui_restart_preflight": argv}))
    return argv


def wait_restart():
    assert restart._RESTART_LOCK.acquire(timeout=3)
    restart._RESTART_LOCK.release()


@pytest.mark.parametrize("managed", [False, True])
def test_restart_consumes_body_on_keepalive(monkeypatch, tmp_path, managed):
    if managed:
        manager(tmp_path, "raise SystemExit(1)")
    signalled = threading.Event()
    monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.set())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            routes._handle_webui_restart(self)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    conn = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        conn.request("POST", "/api/webui/restart", body="{}",
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        assert response.status == (503 if managed else 200)
        payload = json.loads(response.read())
        if not managed:
            assert payload["status"] == "restarting"
            assert signalled.wait(2)
            wait_restart()
        else:
            assert not signalled.is_set()
            manager(tmp_path)
        sock = conn.sock
        conn.request("POST", "/api/webui/restart", body="{}",
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        assert conn.sock is sock
        wait_restart()
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        worker.join(3)


def test_real_preflight_failure_then_success_retry(tmp_path, isolated_restart, caplog):
    manager(tmp_path, "print('SECRET-DIAGNOSTIC'); raise SystemExit(7)")
    failed = Request()
    assert routes._handle_webui_restart(failed)
    assert failed.status == 503
    assert 'SECRET-DIAGNOSTIC' not in failed.wfile.getvalue().decode() + caplog.text
    isolated_restart.assert_not_called()
    assert not restart._RESTART_LOCK.locked()

    marker = tmp_path / "checked"
    manager(tmp_path, f"from pathlib import Path; Path({str(marker)!r}).touch()")
    accepted = Request()
    original = accepted.send_response

    def reply(status):
        assert marker.exists(), "response preceded preflight"
        isolated_restart.assert_not_called()
        original(status)

    accepted.send_response = reply
    routes._handle_webui_restart(accepted)
    assert accepted.status == 200
    assert accepted.payload['status'] == 'restarting'
    wait_restart()
    isolated_restart.assert_called_once_with(os.getpid(), signal.SIGINT)


@pytest.mark.parametrize('data', ['{', '[]', '{}', '{"webui_restart_preflight":"sh -c bad"}',
    '{"webui_restart_preflight":["python","local.py","webui","--check"]}'])
def test_invalid_manager_fails_closed(tmp_path, data, isolated_restart):
    (tmp_path / 'service-manager.json').write_text(data)
    request = Request()
    routes._handle_webui_restart(request)
    assert request.status == 503
    assert 'service-manager.json' in request.payload['error']
    isolated_restart.assert_not_called()
    assert not restart._RESTART_LOCK.locked()


@pytest.mark.parametrize('kind', ['dangling', 'directory', 'unreadable', 'missing-script', 'not-executable'])
def test_unusable_manager_fails_closed(tmp_path, monkeypatch, kind, isolated_restart):
    argv = manager(tmp_path)
    metadata = tmp_path / 'service-manager.json'
    if kind == 'dangling':
        metadata.unlink()
        metadata.symlink_to(tmp_path / 'missing')
    elif kind == 'directory':
        metadata.unlink()
        metadata.mkdir()
    elif kind == 'unreadable':
        original = Path.open
        def denied(path, *args, **kwargs):
            if path == metadata:
                raise PermissionError('SECRET')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', denied)
    elif kind == 'missing-script':
        Path(argv[1]).unlink()
    else:
        argv[0] = argv[1]
        metadata.write_text(json.dumps({'webui_restart_preflight': argv}))
    request = Request()
    routes._handle_webui_restart(request)
    assert request.status == 503
    isolated_restart.assert_not_called()


@pytest.mark.parametrize('error', ['timeout', 'spawn'])
def test_preflight_execution_errors_allow_retry(tmp_path, monkeypatch, error, isolated_restart):
    argv = manager(tmp_path)
    def run(command, **kwargs):
        assert command == argv
        assert kwargs == dict(shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30, check=False)
        if error == 'timeout':
            raise subprocess.TimeoutExpired(argv, 30, output='SECRET', stderr='SECRET')
        raise OSError('SECRET')
    monkeypatch.setattr(restart.subprocess, 'run', run)
    for _ in range(2):
        request = Request()
        routes._handle_webui_restart(request)
        assert request.status == 503
        assert 'SECRET' not in request.payload['error']
        assert not restart._RESTART_LOCK.locked()
    isolated_restart.assert_not_called()


@pytest.mark.parametrize('body,headers', [(b'{', None), (b'[]', None), (b'null', None),
    (b'{}', {'Content-Length': '-1'}), (b'{}', {'Content-Length': 'wat'}),
    (b'{}', {'Content-Length': '4097'}), (b'{}', {'Transfer-Encoding': 'chunked'}),
    (b'{}', {'Content-Length': '3'})])
def test_invalid_body_never_restarts(body, headers, isolated_restart):
    request = Request(body, headers)
    routes._handle_webui_restart(request)
    assert request.status == 400
    assert request.close_connection
    assert request.response_headers['Connection'] == 'close'
    isolated_restart.assert_not_called()


def test_duplicate_content_length_is_rejected(isolated_restart):
    request = Request()
    request.headers['Content-Length'] = '2'
    routes._handle_webui_restart(request)
    assert request.status == 400
    assert request.close_connection
    isolated_restart.assert_not_called()


def test_preflight_error_reply_failure_releases_lock(tmp_path, monkeypatch, isolated_restart):
    manager(tmp_path, "raise SystemExit(1)")
    request = Request()
    def fail(*args, **kwargs):
        raise RuntimeError('reply failed')
    monkeypatch.setattr(request, 'send_response', fail)
    with pytest.raises(RuntimeError, match='reply failed'):
        routes._handle_webui_restart(request)
    assert not restart._RESTART_LOCK.locked()
    isolated_restart.assert_not_called()


def test_busy_consumes_body(isolated_restart):
    restart._RESTART_LOCK.acquire()
    try:
        request = Request()
        routes._handle_webui_restart(request)
        assert request.status == 429
        assert request.rfile.read() == b''
    finally:
        restart._RESTART_LOCK.release()
    isolated_restart.assert_not_called()


@pytest.mark.parametrize('failure', ['write', 'flush', 'thread'])
def test_early_failure_releases_lock(monkeypatch, failure, isolated_restart):
    request = Request()
    def fail(*args, **kwargs):
        raise BrokenPipeError('client gone')
    if failure == 'thread':
        monkeypatch.setattr(restart.threading.Thread, 'start', fail)
        routes._handle_webui_restart(request)
        assert request.status == 503
    else:
        setattr(request.wfile, failure, fail)
        with pytest.raises(BrokenPipeError):
            routes._handle_webui_restart(request)
    assert not restart._RESTART_LOCK.locked()
    isolated_restart.assert_not_called()
