"""Narrow, explicit shared-board control routes; never chat-only or dispatch."""
import pytest
from api.extension_auth import allowed

@pytest.mark.parametrize('method,path', [
    ('GET','/api/kanban/boards'),('GET','/api/kanban/board'),
    ('GET','/api/kanban/tasks/abc123'),('POST','/api/kanban/tasks'),
    ('POST','/api/kanban/tasks/abc123/patch'),
])
def test_shared_kanban_controls_require_administrative_scope(method,path):
    assert allowed(method,path,['control'])
    assert not allowed(method,path,['chat'])
    assert not allowed(method,path,['cdp'])

@pytest.mark.parametrize('method,path', [
    ('POST','/api/kanban/boards'),('POST','/api/kanban/tasks/abc/dispatch'),
    ('DELETE','/api/kanban/tasks/abc'),('POST','/api/kanban/tasks/abc/delete'),
    ('GET','/api/kanban/events'),('GET','/api/kanban/tasks/../private'),
])
def test_unimplemented_kanban_actions_are_not_granted(method,path):
    assert not allowed(method,path,['chat','control','cdp'])
