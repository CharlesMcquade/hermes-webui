import builtins
from api.streaming import _webui_ephemeral_system_prompt


def test_missing_native_resolver_preserves_configured_overlay(monkeypatch):
    original = builtins.__import__
    def old_agent(name, *args, **kwargs):
        if name == 'hermes_cli.personality':
            raise ImportError('synthetic older agent')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', old_agent)
    cfg = {'agent': {'system_prompt': 'SYNTHETIC_OLD_AGENT_OVERLAY'}}
    prompt = _webui_ephemeral_system_prompt(None, config_data=cfg)
    assert prompt.count('SYNTHETIC_OLD_AGENT_OVERLAY') == 1


def test_old_agent_dict_personality_keeps_body_tone_style(monkeypatch):
    original = builtins.__import__
    def old_agent(name, *args, **kwargs):
        if name == 'hermes_cli.personality':
            raise ImportError('synthetic older agent')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', old_agent)
    cfg = {'agent': {'system_prompt': 'MANUAL', 'personalities': {'session': {
        'system_prompt': 'SESSION_STYLE', 'tone': 'calm', 'style': 'concise'}}}}
    prompt = _webui_ephemeral_system_prompt(None, config_data=cfg, personality_name='session')
    assert 'SESSION_STYLE\nTone: calm\nStyle: concise' in prompt
    assert 'MANUAL' not in prompt
