"""Saved YAML keeps references attached to the right provider identity."""
import copy
import pytest
import yaml
from api import config


def test_reordered_rotated_providers_keep_own_references(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    raw = {'custom_providers': [{'name': 'a', 'api_key': '${A_DEMO}'}, {'name': 'b', 'api_key': '${B_DEMO}'}]}
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv('A_DEMO', 'synthetic-a')
    monkeypatch.setenv('B_DEMO', 'synthetic-b')
    current = config._load_yaml_config_file(path)
    current['custom_providers'].reverse()
    monkeypatch.setenv('A_DEMO', 'synthetic-a-rotated')
    monkeypatch.setenv('B_DEMO', 'synthetic-b-rotated')
    before = copy.deepcopy(current)
    config._save_yaml_config_file(path, current)
    assert yaml.safe_load(path.read_text()) == {'custom_providers': list(reversed(raw['custom_providers']))}
    assert current == before


def test_ambiguous_named_list_fails_without_writing(tmp_path):
    path = tmp_path / 'config.yaml'
    raw = {'custom_providers': [{'name': 'same', 'api_key': '${A_DEMO}'}, {'name': 'same', 'api_key': '${B_DEMO}'}]}
    original = yaml.safe_dump(raw)
    path.write_text(original)
    current = copy.deepcopy(raw)
    current['custom_providers'].reverse()
    with pytest.raises(ValueError, match='ambiguous named'):
        config._save_yaml_config_file(path, current)
    assert path.read_text() == original


@pytest.mark.parametrize('operation', ['add', 'remove', 'replace_reference'])
def test_named_provider_edits(tmp_path, operation):
    path = tmp_path / 'config.yaml'
    raw = {'custom_providers': [{'name': 'a', 'api_key': '${A_DEMO}'}, {'name': 'b', 'api_key': '${B_DEMO}'}]}
    path.write_text(yaml.safe_dump(raw))
    current = copy.deepcopy(raw)
    if operation == 'add':
        current['custom_providers'].append({'name': 'c', 'api_key': '${C_DEMO}'})
    elif operation == 'remove':
        current['custom_providers'].pop(0)
    else:
        current['custom_providers'][0]['api_key'] = '${env:NEW_DEMO}'
    config._save_yaml_config_file(path, current)
    assert yaml.safe_load(path.read_text()) == current


def test_incompatible_companion_cannot_write_expansions(tmp_path, monkeypatch):
    from hermes_cli import config as core
    path = tmp_path / 'config.yaml'
    original = 'token: ${DEMO}\n'
    path.write_text(original)
    def old_helper(current, raw, loaded_expanded=None):
        return current
    monkeypatch.setattr(core, '_preserve_env_ref_templates', old_helper)
    with pytest.raises(ValueError, match='compatible reference-preserving agent'):
        config._save_yaml_config_file(path, {'token': 'synthetic-expanded'})
    assert path.read_text() == original
