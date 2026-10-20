"""Release guards for union-merge damage (rebaseline 2026-10-19).

332d7f4fc shipped (1) static/panels.js with a duplicated block -> SyntaxError,
which half-bricks boot for every fresh client, and (2) api/streaming.py calling
resolve_reasoning_effort_for_request without importing it (NameError swallowed
by a broad except -> reasoning effort silently dropped). The ESLint guard skips
when eslint is absent, so these need only node / pyflakes.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("js", sorted((REPO / "static").glob("*.js")), ids=lambda p: p.name)
def test_static_js_parses(js):
    r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_api_has_no_undefined_names():
    pyflakes = pytest.importorskip("pyflakes.api")
    from pyflakes import reporter as _rep
    import io

    out = io.StringIO()
    pyflakes.checkRecursive([str(REPO / "api")], _rep.Reporter(out, io.StringIO()))
    bad = []
    for line in out.getvalue().splitlines():
        if "undefined name" not in line:
            continue
        m = re.match(r"(.+?):(\d+):", line)
        src = Path(m.group(1)).read_text().splitlines()[int(m.group(2)) - 1]
        if "noqa: F821" not in src:
            bad.append(line)
    assert not bad, "\n".join(bad)


def test_streaming_imports_scoped_effort_resolver():
    import api.streaming as streaming
    assert callable(streaming.resolve_reasoning_effort_for_request)
