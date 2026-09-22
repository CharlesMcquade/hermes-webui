"""WebUI self-restart admission; optional local service-manager preflight.

Only local deployment metadata selects the command. HTTP input never does.
Acceptance is not proof that a supervisor has respawned a healthy server.
"""
import json
import logging
import os
from pathlib import Path
import signal
import stat
import subprocess
import threading
import time

from api import config as api_config
from api.helpers import j, read_body

logger = logging.getLogger(__name__)
_RESTART_LOCK = threading.Lock()
_PREFLIGHT_TIMEOUT = 30
_MANAGER_MAX_BYTES = 16384


class PreflightError(Exception):
    """A safe, actionable public error (never includes command output)."""


def _preflight() -> None:
    manager = Path(api_config.STATE_DIR) / "service-manager.json"
    try:
        # Only an absent directory entry opts out. Dangling symlinks fail closed.
        manager.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise PreflightError("Cannot read service-manager.json; repair local manager metadata and retry.") from None
    try:
        resolved = manager.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("not a file")
        with resolved.open("rb") as stream:
            raw = stream.read(_MANAGER_MAX_BYTES + 1)
        if len(raw) > _MANAGER_MAX_BYTES:
            raise ValueError("too large")
        data = json.loads(raw)
        argv = data["webui_restart_preflight"]
        if (not isinstance(argv, list) or len(argv) != 4
                or not all(isinstance(arg, str) and arg and "\x00" not in arg for arg in argv)
                or argv[2:] != ["webui", "--check"]):
            raise ValueError("invalid command")
        for arg in argv[:2]:
            path = Path(arg)
            if not path.is_absolute() or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("not an absolute local file")
        if not os.access(argv[0], os.X_OK) or not os.access(argv[1], os.R_OK):
            raise ValueError("command not accessible")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        raise PreflightError(
            "Invalid service-manager.json; configure webui_restart_preflight with an absolute "
            "executable, readable local script, and webui --check, then retry."
        ) from None
    try:
        # Discard all output, including on timeout: arbitrary diagnostics may
        # contain credentials. Fixed error messages are the public boundary.
        result = subprocess.run(
            argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=_PREFLIGHT_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        raise PreflightError("WebUI restart preflight timed out (30s); check the local service manager and retry.") from None
    except (OSError, ValueError):
        raise PreflightError("Could not run WebUI restart preflight; repair the local manager command and retry.") from None
    if result.returncode != 0:
        raise PreflightError("WebUI restart preflight failed; run the local manager check, resolve its errors, and retry.")


def handle_restart(handler) -> bool:
    # Consume even rejected/busy requests so their bytes cannot become {}POST
    # on a reused connection. Ambiguous framing is rejected without draining.
    try:
        lengths = (handler.headers.get_all("Content-Length", [])
                   if hasattr(handler.headers, "get_all") else [])
        if handler.headers.get("Transfer-Encoding") or len(lengths) > 1:
            raise ValueError("unsupported framing")
        read_body(handler, max_bytes=4096, strict=True)
    except (ValueError, OSError):
        handler.close_connection = True
        j(handler, {"ok": False, "error": "Invalid restart request; send a JSON object of at most 4096 bytes."},
          status=400, extra_headers={"Connection": "close"})
        return True

    ready = threading.Event()
    if not _RESTART_LOCK.acquire(blocking=False):
        j(handler, {"ok": False, "error": "WebUI restart already in progress."}, status=429)
        return True

    handed_off = False

    def interrupt():
        ready.wait()
        if not handed_off:
            return
        try:
            time.sleep(0.3)
            os.kill(os.getpid(), signal.SIGINT)
        finally:
            _RESTART_LOCK.release()

    try:
        try:
            _preflight()
        except PreflightError as exc:
            j(handler, {"ok": False, "error": str(exc)}, status=503)
            return True
        try:
            threading.Thread(target=interrupt, daemon=True).start()
        except Exception:
            j(handler, {"ok": False, "error": "Could not schedule WebUI restart; retry the request."}, status=503)
            return True
        j(handler, {"ok": True, "status": "restarting",
                    "message": "WebUI restart accepted; waiting for the service manager to restart it."},
          raise_disconnect=True)
        handler.wfile.flush()
        logger.info("[webui-restart-request] accepted (restart not yet verified)")
        handed_off = True
        return True
    finally:
        if not handed_off:
            _RESTART_LOCK.release()
        ready.set()
