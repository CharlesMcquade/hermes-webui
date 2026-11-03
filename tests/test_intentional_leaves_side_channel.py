"""Intentional-leaf side channel is consumed on entry, never carried across saves.

``set_intentional_leaves()`` marks leaf paths whose NEW literal values must
persist verbatim through the #8032/#8115 expansion-leak net for exactly ONE
``_save_yaml_config_file`` call. A save that raises (unreadable config,
incompatible companion agent, write error) must still consume the mark:
a stale thread-local left behind would let the NEXT unrelated save on the
same server thread copy a credential path verbatim past the leak net.
"""

from __future__ import annotations

import pytest
import yaml

from api import config


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"model": {"api_key": "${AUDIT_TEST_TOKEN}"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AUDIT_TEST_TOKEN", "synthetic-expanded-value")
    yield path
    # Leave no thread-local residue for unrelated tests on this thread.
    config.set_intentional_leaves(())


def _read_key(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))["model"]["api_key"]


def test_intentional_leaf_persists_new_literal_verbatim(config_path):
    config.set_intentional_leaves((("model", "api_key"),))
    config._save_yaml_config_file(config_path, {"model": {"api_key": "synthetic-expanded-value"}})
    assert _read_key(config_path) == "synthetic-expanded-value"


def test_failed_save_consumes_the_mark(config_path, monkeypatch):
    config.set_intentional_leaves((("model", "api_key"),))

    def boom(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(config._paths, "_atomic_write_text", boom)
    with pytest.raises(OSError):
        config._save_yaml_config_file(config_path, {"model": {"api_key": "synthetic-expanded-value"}})

    assert config._pending_intentional_leaves() == ()


def test_stale_mark_from_failed_save_does_not_leak_into_next_save(config_path, monkeypatch):
    config.set_intentional_leaves((("model", "api_key"),))
    real_write = config._paths._atomic_write_text
    calls = {"n": 0}

    def boom_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated write failure")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(config._paths, "_atomic_write_text", boom_once)
    with pytest.raises(OSError):
        config._save_yaml_config_file(config_path, {"model": {"api_key": "synthetic-expanded-value"}})

    # Next save on this thread sets NO intentional leaves: the expansion-leak
    # net must revert the previously-referenced field to its reference.
    config._save_yaml_config_file(config_path, {"model": {"api_key": "synthetic-expanded-value"}})
    assert _read_key(config_path) == "${AUDIT_TEST_TOKEN}"
