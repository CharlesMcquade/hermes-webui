"""No-network exercise of a real Agent AIAgent review fork in isolated state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("decision", ["no_change", "needs_parent", "uncertain"])
def test_real_agent_fork_with_local_stub_provider(tmp_path, decision):
    agent_source = os.environ.get("HERMES_AGENT_TRIAGE_SOURCE")
    if not agent_source:
        pytest.skip("set HERMES_AGENT_TRIAGE_SOURCE to the isolated feature checkout")
    code = r'''
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from run_agent import AIAgent
from agent.late_delegation_review import review_late_delegation_result

captured = []
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        request = json.loads(body)
        captured.append(request)
        answer = json.dumps({"decision": sys.argv[1]})
        chunk = {"id":"local-test","object":"chat.completion.chunk", "model":"local-test",
                 "choices":[{"index":0,"delta":{"role":"assistant","content":answer},"finish_reason":None}]}
        finish = {"id":"local-test","object":"chat.completion.chunk", "model":"local-test",
                  "choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
        payload = ("data: " + json.dumps(chunk) + "\n\n" +
                   "data: " + json.dumps(finish) + "\n\n" + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
server = HTTPServer(("127.0.0.1", 0), Handler)
worker = threading.Thread(target=server.serve_forever, daemon=True)
worker.start()
try:
    parent = AIAgent(api_key="local-test", base_url=f"http://127.0.0.1:{server.server_port}/v1",
                     provider="custom", model="local-test", platform="webui",
                     quiet_mode=True, skip_memory=True, enabled_toolsets=[])
    parent.session_id = "private-review-parent"
    parent._session_messages = [
        {"role":"user","content":"Check the report"},
        {"role":"assistant","content":"I checked the report."},
    ]
    before = json.dumps(parent._session_messages, sort_keys=True)
    webui_history = [
        {"role":"user","content":"Check the report", "_source":"human", "timestamp":123},
        {"role":"assistant","content":"I checked the report.", "id":"ui-message-1"},
    ]
    history_before = json.dumps(webui_history, sort_keys=True)
    verdict = review_late_delegation_result(parent, "Check the report", "The child checked the report; no corrections.",
                                             history=webui_history, timeout=12)
    assert verdict == sys.argv[1], (verdict, len(captured))
    assert captured and any("The child checked the report" in json.dumps(req["messages"]) for req in captured if "messages" in req)
    assert all(set(message) <= {"role", "content", "tool_calls", "tool_call_id", "name"}
               for req in captured if "messages" in req for message in req["messages"])
    assert json.dumps(webui_history, sort_keys=True) == history_before
    assert json.dumps(parent._session_messages, sort_keys=True) == before
    assert not (Path(os.environ["HERMES_HOME"]) / "sessions" / "private-review-parent.json").exists()
finally:
    server.shutdown()
    server.server_close()
'''
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path / "home")
    env["HERMES_WEBUI_STATE_DIR"] = str(tmp_path / "webui")
    env["PYTHONPATH"] = str(Path(agent_source).resolve())
    env.pop("PYTHONSAFEPATH", None)
    child = subprocess.run([sys.executable, "-c", code, decision], env=env,
                           capture_output=True, text=True, timeout=90)
    assert child.returncode == 0, child.stderr
