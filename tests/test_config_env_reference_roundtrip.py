"""Config saves must not materialize runtime credential expansions."""
import yaml
from api import config


def test_unrelated_save_preserves_env_reference(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    raw = {'mcp_servers': {'demo': {'env': {'DEMO_PASSWORD': '${DEMO_PASSWORD}'}}}}
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv('DEMO_PASSWORD', 'synthetic-roundtrip-fixture')
    loaded = config._load_yaml_config_file(path)
    loaded['display'] = {'theme': 'dark'}
    config._save_yaml_config_file(path, loaded)
    saved = yaml.safe_load(path.read_text())
    assert saved['mcp_servers'] == raw['mcp_servers']
    assert saved['display']['theme'] == 'dark'


def test_intentional_value_change_is_not_reverted(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({'endpoint': '${DEMO_ENDPOINT}'}))
    monkeypatch.setenv('DEMO_ENDPOINT', 'https://old.invalid')
    loaded = config._load_yaml_config_file(path)
    loaded['endpoint'] = 'https://new.invalid'
    config._save_yaml_config_file(path, loaded)
    assert yaml.safe_load(path.read_text())['endpoint'] == 'https://new.invalid'


def test_nested_list_template_survives(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    raw = {'items': [{'endpoint': 'https://${DEMO_HOST}/v1'}]}
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv('DEMO_HOST', 'test.invalid')
    config._save_yaml_config_file(path, config._load_yaml_config_file(path))
    assert yaml.safe_load(path.read_text()) == raw


def test_profile_reader_expansion_used_for_save(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    raw = {'value': '${PROFILE_SETTING}'}
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv('PROFILE_SETTING', 'root-setting')
    monkeypatch.setattr(config, '_expand_env_vars', lambda data: {'value': 'profile-setting'} if isinstance(data, dict) else 'profile-setting')
    config._save_yaml_config_file(path, {'value': 'profile-setting'})
    assert yaml.safe_load(path.read_text()) == raw


def test_partially_expanded_secret_template_is_restored(tmp_path, monkeypatch):
    path = tmp_path / 'settings.yaml'
    raw = {'endpoint': 'https://${SERVICE_TOKEN}@${UNRESOLVED_HOST}'}
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv('SERVICE_TOKEN', 'synthetic-token')
    monkeypatch.delenv('UNRESOLVED_HOST', raising=False)
    expanded = config._load_yaml_config_file(path)
    config._save_yaml_config_file(path, expanded)
    assert yaml.safe_load(path.read_text()) == raw


def test_rotation_between_read_and_save_never_materializes_secret(tmp_path, monkeypatch):
    path = tmp_path / 'settings.yaml'
    path.write_text('service:\n  password: ${ROUNDTRIP_DEMO}\n')
    monkeypatch.setenv('ROUNDTRIP_DEMO', 'synthetic-before')
    expanded = config._load_yaml_config_file(path)
    monkeypatch.setenv('ROUNDTRIP_DEMO', 'synthetic-after')
    config._save_yaml_config_file(path, expanded)
    assert yaml.safe_load(path.read_text())['service']['password'] == '${ROUNDTRIP_DEMO}'


def test_empty_existing_config_can_be_initialized(tmp_path):
    path = tmp_path / 'settings.yaml'
    path.write_text('# new configuration\n')
    config._save_yaml_config_file(path, {'display': {'theme': 'dark'}})
    assert yaml.safe_load(path.read_text()) == {'display': {'theme': 'dark'}}


def test_unreadable_existing_yaml_cannot_bypass_reference_protection(tmp_path):
    import pytest
    path = tmp_path / 'broken.yaml'
    original = 'value: [unfinished'
    path.write_text(original)
    with pytest.raises(ValueError, match='Cannot preserve environment references'):
        config._save_yaml_config_file(path, {'value': 'expanded-runtime-value'})
    assert path.read_text() == original
