import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import Base, Offer, Product, Supplier
from app.miniapp import CatalogDestination, CatalogTopic, CatalogTopicRule
from app import publisher


def fixture():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as db:
        now = datetime.now(timezone.utc)
        db.add_all([CatalogDestination(id=1, chat_id=-100, title='VOLT', is_forum=True, can_manage_topics=True, checked_at=now),
                    CatalogTopic(chat_id=-100, thread_id=7, title='Apple', closed=False, checked_at=now),
                    CatalogTopicRule(chat_id=-100, thread_id=7, brand='Apple', brand_key='apple', category='', category_key='', product_id=0),
                    publisher.PublicationSwitch(id=1, enabled=True), Supplier(id=1, name='Source')])
        for i, color in enumerate(('Blue', 'Orange', 'Silver'), 1):
            db.add(Product(id=i, sku_key=str(i), brand='Apple', model=f'iPhone 17 Pro 256 {color} (1Sim+eSim) 🇺🇸',
                           category='Смартфоны', memory='', color='', sim='SIM + eSIM', approved=True, hidden=False))
            db.add(Offer(product_id=i, supplier_id=1, cost=Decimal('90000'), confirmed_date='2026-11-03', active=True))
            db.add(publisher.Publication(product_id=i, chat_id=-100, thread_id=7, message_id=100+i,
                                         content=f'old {i}', state='sent', error=''))
        db.commit()
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=500)),
                          edit_message_text=AsyncMock(), delete_message=AsyncMock())
    return engine, factory, bot


def test_existing_single_posts_are_safely_consolidated_and_reused(monkeypatch):
    engine, factory, bot = fixture()
    price = {'value': '93000'}
    monkeypatch.setattr(publisher, 'publication_price', lambda db, p: {
        'status': 'priced', 'price': price['value'], 'base_price': price['value'], 'discount': '0'})
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['edited'] == 1 and result['deleted'] == 2 and result['sent'] == 0
    bot.send_message.assert_not_awaited()
    text = bot.edit_message_text.await_args.kwargs['text']
    assert '<b>iPhone 17 Pro</b>' in text and all(color in text for color in ('Blue', 'Orange', 'Silver'))
    assert text.count('SIM + eSIM') == 0 and text.count('1Sim+eSim') == 3
    assert '🇺🇸' not in text
    assert len(text.encode('utf-16-le')) // 2 < 4096
    anchor = bot.edit_message_text.await_args.kwargs['message_id']
    price['value'] = '94000'
    assert asyncio.run(publisher.reconcile_publication(bot, factory))['edited'] == 1
    assert bot.edit_message_text.await_args.kwargs['message_id'] == anchor
    assert '94 000 ₽' in bot.edit_message_text.await_args.kwargs['text']
    assert bot.send_message.await_count == 0 and bot.delete_message.await_count == 2
    engine.dispose()


def test_failed_group_edit_never_deletes_originals_and_retries(monkeypatch):
    engine, factory, bot = fixture()
    bot.edit_message_text.side_effect = OSError('unavailable')
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['errors'] == 1 and result['deleted'] == 0
    bot.delete_message.assert_not_awaited()
    with factory() as db:
        assert len(db.scalars(select(publisher.Publication)).all()) == 3
    bot.edit_message_text.side_effect = None
    assert asyncio.run(publisher.reconcile_publication(bot, factory))['deleted'] == 2
    assert bot.send_message.await_count == 0
    engine.dispose()


def _bad_request(message):
    return TelegramBadRequest(method=SimpleNamespace(), message=message)


def test_deleted_group_anchor_is_recreated_and_old_posts_are_cleaned_up():
    engine, factory, bot = fixture()
    bot.edit_message_text.side_effect = _bad_request('Bad Request: message to edit not found')
    bot.delete_message.side_effect = _bad_request('Bad Request: message to delete not found')
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['sent'] == 1 and result['deleted'] == 2 and result['errors'] == 0
    assert bot.send_message.await_count == 1
    with factory() as db:
        row = db.scalar(select(publisher.Publication))
        assert row.message_id == 500 and row.state == 'sent'
    bot.edit_message_text.reset_mock()
    assert asyncio.run(publisher.reconcile_publication(bot, factory))['sent'] == 0
    bot.edit_message_text.assert_not_awaited()
    assert bot.send_message.await_count == 1
    engine.dispose()


def test_manual_verify_recovers_deleted_unchanged_group():
    engine, factory, bot = fixture()
    asyncio.run(publisher.reconcile_publication(bot, factory))
    bot.edit_message_text.side_effect = _bad_request('Bad Request: message to edit not found')
    result = asyncio.run(publisher.reconcile_publication(bot, factory, verify_existing=True))
    assert result['sent'] == 1 and result['errors'] == 0
    assert bot.send_message.await_count == 1
    with factory() as db:
        row = db.scalar(select(publisher.Publication))
        assert row.message_id == 500 and row.state == 'sent'
    assert asyncio.run(publisher.reconcile_publication(bot, factory))['sent'] == 0
    assert bot.send_message.await_count == 1
    engine.dispose()


def test_manual_verify_keeps_existing_unchanged_post():
    engine, factory, bot = fixture()
    asyncio.run(publisher.reconcile_publication(bot, factory))
    bot.edit_message_text.side_effect = _bad_request('Bad Request: message is not modified')
    result = asyncio.run(publisher.reconcile_publication(bot, factory, verify_existing=True))
    assert result['sent'] == 0 and result['errors'] == 0
    bot.send_message.assert_not_awaited()
    with factory() as db:
        row = db.scalar(select(publisher.Publication))
        assert row.message_id == 101 and row.state == 'sent'
    engine.dispose()


def test_other_bad_request_never_reposts_or_deletes_originals():
    engine, factory, bot = fixture()
    bot.edit_message_text.side_effect = _bad_request("Bad Request: message can't be edited")
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['errors'] == 1
    bot.send_message.assert_not_awaited()
    bot.delete_message.assert_not_awaited()
    with factory() as db:
        row = db.get(publisher.Publication, 1)
        assert row.message_id == 101 and row.state == 'edit_failed'
    engine.dispose()


def test_unconfirmed_replacement_is_never_automatically_repeated():
    engine, factory, bot = fixture()
    bot.edit_message_text.side_effect = _bad_request('Bad Request: message to edit not found')
    bot.send_message.side_effect = TimeoutError()
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['uncertain'] == 1
    assert bot.send_message.await_count == 1
    asyncio.run(publisher.reconcile_publication(bot, factory, verify_existing=True))
    assert bot.send_message.await_count == 1
    bot.delete_message.assert_not_awaited()
    engine.dispose()


def test_failed_edit_with_unchanged_content_is_actually_checked_again():
    engine, factory, bot = fixture()
    asyncio.run(publisher.reconcile_publication(bot, factory))
    with factory() as db:
        row = db.scalar(select(publisher.Publication))
        row.state, row.error = 'edit_failed', 'TelegramBadRequest'
        db.commit()
    bot.edit_message_text.side_effect = _bad_request('Bad Request: message to edit not found')
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['sent'] == 1
    with factory() as db:
        row = db.scalar(select(publisher.Publication))
        assert row.message_id == 500 and row.state == 'sent'
    engine.dispose()


def test_manual_sync_requests_verification(monkeypatch):
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    reconcile = AsyncMock(return_value={'sent': 0})
    monkeypatch.setenv('TG_BOT_TOKEN', 'test-token')
    monkeypatch.setattr(publisher, 'publication_enabled', lambda: True)
    monkeypatch.setattr(publisher, 'Bot', lambda token: bot)
    monkeypatch.setattr(publisher, 'reconcile_publication', reconcile)
    assert asyncio.run(publisher.sync_publication()) == {'sent': 0}
    reconcile.assert_awaited_once_with(bot, verify_existing=True)
    bot.session.close.assert_awaited_once()


def test_uncertain_old_send_blocks_group_and_does_not_delete(monkeypatch):
    engine, factory, bot = fixture()
    with factory() as db:
        row = db.scalar(select(publisher.Publication).where(publisher.Publication.product_id == 2))
        row.state, row.message_id = 'uncertain', None
        db.commit()
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['uncertain'] == 1
    bot.send_message.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()
    bot.delete_message.assert_not_awaited()
    engine.dispose()


def test_pages_fit_telegram_limit_and_group_identity_survives_item_removal():
    engine, factory, bot = fixture()
    with factory() as db:
        for i in range(4, 130):
            db.add(Product(id=i, sku_key=str(i), brand='Apple', model=f'iPhone 17 Pro 256 Variant {i} ' + 'X'*60 + ' eSIM',
                           category='Смартфоны', memory='', color='', sim='eSIM', approved=True, hidden=False))
            db.add(Offer(product_id=i, supplier_id=1, cost=Decimal('90000'), confirmed_date='2026-11-03', active=True))
        db.commit()
        targets, members, _ = publisher._group_targets(db, create=True)
        db.commit()
        assert len(targets) > 1 and len(members) == 129
        assert all(publisher._units(row[2]) < 4096 for row in targets.values())
        first_key = members[1]
        db.get(Product, 1).hidden = True
        db.commit()
        updated, members2, _ = publisher._group_targets(db, create=True)
        assert first_key in updated and members2[2] == first_key
    engine.dispose()
