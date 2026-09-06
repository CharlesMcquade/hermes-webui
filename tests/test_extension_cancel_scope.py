"""The deployed chat cancel contract is GET, not a guessed POST route."""
from api.extension_auth import allowed


def test_chat_device_can_cancel_via_actual_get_contract():
    assert allowed('GET', '/api/chat/cancel', ['chat'])
    assert allowed('GET', '/api/chat/cancel', ['control'])
    assert not allowed('GET', '/api/chat/cancel', ['cdp'])
    assert not allowed('POST', '/api/chat/cancel', ['chat'])
