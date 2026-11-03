from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app
from app import miniapp
from app.integrated_collector import persist, record_source
from tests.test_incoming_products import msg, setup
from tests.test_miniapp import signed


def test_messages_are_grouped_by_actual_peer_with_independent_pagination(monkeypatch):
    engine, factory = setup(monkeypatch)
    record_source(factory, 'miniapp-1234', 1, '@hi_catalog_bot', SimpleNamespace(username='hi_catalog_bot', first_name='HI', usernames=[]))
    record_source(factory, 'miniapp-1234', 2, '@tech_online_seller_bot', SimpleNamespace(username='tech_online_seller_bot', first_name='Tech', usernames=[]))
    now = datetime.now(timezone.utc)
    for i in range(22):
        persist(factory, 'miniapp-1234', 1, msg(i + 1, f'HI {i}', now))
        persist(factory, 'miniapp-1234', 2, msg(i + 1, f'Tech {i}', now))
    persist(factory, 'another-account', 2, msg(100, 'Private', now))
    client = TestClient(app)
    assert client.get('/miniapp/api/accounts/1/message-sources').status_code == 401
    headers = {'X-Telegram-Init-Data': signed()}
    response = client.get('/miniapp/api/accounts/1/message-sources', headers=headers)
    by_chat = {s['chat_id']: s for s in response.json()['sources']}
    assert by_chat['1']['label'] == 'HI · @hi_catalog_bot'
    assert by_chat['2']['label'] == 'Tech · @tech_online_seller_bot'
    assert by_chat['1']['count'] == by_chat['2']['count'] == 22
    page1 = client.get('/miniapp/api/accounts/1/messages?chat_id=1', headers=headers).json()
    assert len(page1['messages']) == 20 and all(m['chat_id'] == '1' for m in page1['messages'])
    page2 = client.get(f"/miniapp/api/accounts/1/messages?chat_id=1&before={page1['next_before']}", headers=headers).json()
    assert len(page2['messages']) == 2 and all(m['chat_id'] == '1' for m in page2['messages'])
    other = client.get('/miniapp/api/accounts/1/messages?chat_id=2', headers=headers).json()
    assert len(other['messages']) == 20 and all(m['chat_id'] == '2' for m in other['messages'])
    assert client.get('/miniapp/api/accounts/1/messages?chat_id=999999999999999999999999', headers=headers).status_code == 422
    engine.dispose()
