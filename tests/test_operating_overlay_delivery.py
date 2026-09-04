"""Prompt delivery regressions using the real WebUI builder/resolver."""
import copy
import pytest
from api.streaming import _webui_ephemeral_system_prompt
resolve_ephemeral_system_prompt = pytest.importorskip("hermes_cli.personality").resolve_ephemeral_system_prompt


@pytest.mark.parametrize('selection, expected', [(None, 'PROFILE_STYLE'), ('', 'SYNTHETIC_CONTRACT'), ('none', 'SYNTHETIC_CONTRACT'), ('missing', 'SYNTHETIC_CONTRACT'), ('session', 'SESSION_STYLE')])
def test_webui_native_precedence_and_stable_repeated_build(selection, expected):
    cfg = {'agent': {'system_prompt': 'SYNTHETIC_CONTRACT', 'personalities': {'profile': 'PROFILE_STYLE', 'session': {'system_prompt': 'SESSION_STYLE', 'tone': 'calm'}}}, 'display': {'personality': 'profile'}}
    before = copy.deepcopy(cfg)
    context = {'source': 'webui', 'session_id': 'synthetic', 'workspace': '/tmp/frozen'}
    prompt = _webui_ephemeral_system_prompt(None, context, cfg, personality_name=selection)
    assert prompt.count(expected) == 1
    assert prompt == _webui_ephemeral_system_prompt(None, context, cfg, personality_name=selection)
    effective = copy.deepcopy(cfg)
    if selection is not None:
        effective['display']['personality'] = selection
    assert resolve_ephemeral_system_prompt(effective) in prompt
    assert cfg == before


def test_webui_delivers_configured_operating_overlay():
    cfg = {'agent': {'system_prompt': 'SYNTHETIC_CONTRACT'}}
    prompt = _webui_ephemeral_system_prompt(None, config_data=cfg)
    assert prompt.count('SYNTHETIC_CONTRACT') == 1


def test_explicit_body_remains_compatible():
    prompt = _webui_ephemeral_system_prompt('EXPLICIT', config_data={'agent': {'system_prompt': 'MANUAL'}})
    assert 'EXPLICIT' in prompt
    assert 'MANUAL' not in prompt
