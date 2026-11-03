import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import Base, Offer, Product, Supplier
from app.miniapp import CatalogDestination, CatalogTopic, CatalogTopicRule
from app import publisher


def setup_db():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as db:
        db.add_all([
            CatalogDestination(id=1, chat_id=-100, title='VOLT', is_forum=True, can_manage_topics=True,
                               checked_at=datetime.now(timezone.utc)),
            CatalogTopic(chat_id=-100, thread_id=12, title='Samsung', closed=False, checked_at=datetime.now(timezone.utc)),
            CatalogTopicRule(chat_id=-100, thread_id=12, brand='Samsung', brand_key='samsung', category='', category_key='', product_id=0),
            Supplier(id=1, name='Supplier'),
            publisher.PublicationSwitch(id=1, enabled=True),
            Product(id=1, sku_key='a', brand='Samsung', model='S26 🇪🇺', category='Смартфоны', memory='256GB', color='Black', sim='SIM+eSIM', approved=True, hidden=False),
            Product(id=2, sku_key='b', brand='Samsung', model='S25', category='Смартфоны', memory='', color='', sim='eSIM', approved=False, hidden=False),
            Product(id=3, sku_key='c', brand='Samsung', model='S24', category='Смартфоны', memory='', color='', sim='', approved=True, hidden=False),
        ])
        db.flush()
        for product_id in (1, 2, 3):
            db.add(Offer(supplier_id=1, product_id=product_id, cost=Decimal('50000'), confirmed_date='2026-10-30', active=True))
        db.commit()
    return engine, factory


def mock_bot():
    return SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=101)),
                           edit_message_text=AsyncMock(), delete_message=AsyncMock())


def test_publish_only_approved_routed_real_active_sku_without_inventing_price():
    engine, factory = setup_db()
    bot = mock_bot()
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['sent'] == 1
    call = bot.send_message.await_args.kwargs
    assert call['chat_id'] == -100 and call['message_thread_id'] == 12
    assert 'S26' in call['text'] and 'SIM+eSIM' in call['text']
    assert 'По запросу' in call['text'] and '50000' not in call['text'] and '🇪🇺' not in call['text']
    assert asyncio.run(publisher.reconcile_publication(bot, factory))['sent'] == 0
    assert bot.send_message.await_count == 1
    with factory() as db:
        assert db.scalar(select(publisher.Publication)).message_id == 101
    engine.dispose()


def test_changes_edit_only_and_hidden_offer_deletes():
    engine, factory = setup_db(); bot = mock_bot()
    asyncio.run(publisher.reconcile_publication(bot, factory))
    with factory() as db:
        db.get(Product, 1).color = 'Blue'; db.commit()
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['edited'] == 1 and 'Blue' in bot.edit_message_text.await_args.kwargs['text']
    assert bot.send_message.await_count == 1
    with factory() as db:
        db.scalar(select(Offer).where(Offer.product_id == 1)).active = False; db.commit()
    assert asyncio.run(publisher.reconcile_publication(bot, factory))['deleted'] == 1
    bot.delete_message.assert_awaited_once_with(chat_id=-100, message_id=101)
    engine.dispose()


def test_uncertain_send_never_retries_and_is_persistent():
    engine, factory = setup_db(); bot = mock_bot()
    bot.send_message.side_effect = TimeoutError()
    assert asyncio.run(publisher.reconcile_publication(bot, factory))['uncertain'] == 1
    assert asyncio.run(publisher.reconcile_publication(bot, factory))['uncertain'] == 1
    assert bot.send_message.await_count == 1
    with factory() as db:
        row = db.scalar(select(publisher.Publication)); assert row.state == 'uncertain' and row.message_id is None
    engine.dispose()


def test_crash_pending_is_never_replayed():
    engine, factory = setup_db(); bot = mock_bot()
    with factory() as db:
        db.add(publisher.Publication(product_id=1, chat_id=-100, thread_id=12, state='pending', content='', error='')); db.commit()
    assert asyncio.run(publisher.reconcile_publication(bot, factory))['uncertain'] == 1
    bot.send_message.assert_not_awaited()
    engine.dispose()


def test_rebinding_sends_new_post_before_removing_old_and_missing_destination_does_not_delete():
    engine, factory = setup_db(); bot = mock_bot()
    asyncio.run(publisher.reconcile_publication(bot, factory))
    with factory() as db:
        db.get(CatalogDestination, 1).is_forum = False; db.commit()
    try:
        asyncio.run(publisher.reconcile_publication(bot, factory))
        assert False, 'Should fail closed'
    except HTTPException as exc:
        assert exc.status_code == 409
    bot.delete_message.assert_not_awaited()
    with factory() as db:
        db.get(CatalogDestination, 1).is_forum = True
        rule = db.scalar(select(CatalogTopicRule)); rule.thread_id = 13
        db.add(CatalogTopic(chat_id=-100, thread_id=13, title='New', closed=False, checked_at=datetime.now(timezone.utc)))
        db.commit()
    bot.delete_message.side_effect = OSError()
    assert asyncio.run(publisher.reconcile_publication(bot, factory))['errors'] == 1
    assert bot.send_message.await_count == 2
    assert asyncio.run(publisher.reconcile_publication(bot, factory))['errors'] == 1
    assert bot.send_message.await_count == 2
    engine.dispose()


def test_telegram_retry_after_is_persisted_and_prevents_early_retry():
    from aiogram.exceptions import TelegramRetryAfter
    from datetime import timedelta
    engine, factory = setup_db(); bot = mock_bot()
    bot.send_message.side_effect = TelegramRetryAfter(method=SimpleNamespace(chat_id=-100), message='Flood', retry_after=120)
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['errors'] == 1 and result['retry_after'] > 0
    bot.send_message.side_effect = None
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['retry_after'] > 0
    assert bot.send_message.await_count == 1
    with factory() as db:
        db.get(publisher.PublicationSwitch, 1).retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert asyncio.run(publisher.reconcile_publication(bot, factory))['sent'] == 1
    engine.dispose()


def test_pause_is_checked_before_each_send_in_a_batch():
    engine, factory = setup_db(); bot = mock_bot()
    with factory() as db:
        db.get(Product, 2).approved = True
        db.commit()
    async def send_and_pause(**kwargs):
        with factory() as db:
            db.get(publisher.PublicationSwitch, 1).enabled = False
            db.commit()
        return SimpleNamespace(message_id=101)
    bot.send_message.side_effect = send_and_pause
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['paused'] is True
    assert bot.send_message.await_count == 1
    engine.dispose()


def test_verified_price_appears_then_stale_price_is_removed_by_edit(monkeypatch):
    engine, factory = setup_db(); bot = mock_bot()
    price = {'status': 'on_request', 'price': 'По запросу', 'discount': '0', 'base_price': None}
    monkeypatch.setattr(publisher, 'publication_price', lambda db, product: dict(price))

    assert asyncio.run(publisher.reconcile_publication(bot, factory))['sent'] == 1
    assert 'По запросу' in bot.send_message.await_args.kwargs['text']

    price.update(status='priced', price='54300', base_price='55000', discount='700')
    assert asyncio.run(publisher.reconcile_publication(bot, factory))['edited'] == 1
    sent_text = bot.edit_message_text.await_args.kwargs['text']
    assert '<s>55 000 ₽</s> 54 300 ₽' in sent_text
    assert '50 000' not in sent_text
    assert bot.edit_message_text.await_args.kwargs['message_id'] == 101

    price.update(status='on_request', price='По запросу', base_price=None, discount='0')
    assert asyncio.run(publisher.reconcile_publication(bot, factory))['edited'] == 1
    assert 'По запросу' in bot.edit_message_text.await_args.kwargs['text']
    assert '54 300' not in bot.edit_message_text.await_args.kwargs['text']
    assert bot.send_message.await_count == 1
    assert bot.edit_message_text.await_count == 2
    engine.dispose()


def test_batch_stops_if_product_changes_before_second_send():
    engine, factory = setup_db(); bot = mock_bot()
    with factory() as db:
        db.get(Product, 2).approved = True
        db.commit()
    async def send_and_change(**kwargs):
        with factory() as db:
            db.get(Product, 1).hidden = True
            db.commit()
        return SimpleNamespace(message_id=101)
    bot.send_message.side_effect = send_and_change
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['stopped'] == 'data_changed'
    assert bot.send_message.await_count == 1
    engine.dispose()


def test_batch_rechecks_product_after_rate_delay(monkeypatch):
    engine, factory = setup_db(); bot = mock_bot()
    with factory() as db:
        db.get(Product, 2).approved = True
        db.commit()
    async def sleep_and_change(delay):
        with factory() as db:
            db.get(Product, 1).color = 'Blue'
            db.commit()
    monkeypatch.setattr(publisher.asyncio, 'sleep', sleep_and_change)
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['stopped'] == 'data_changed'
    assert bot.send_message.await_count == 1
    engine.dispose()


def test_batch_stops_when_database_lock_connection_is_lost(monkeypatch):
    engine, factory = setup_db(); bot = mock_bot()
    with factory() as db:
        db.get(Product, 2).approved = True
        db.commit()
    checks = []
    def lose_lock(connection):
        checks.append(True)
        if len(checks) >= 3:
            raise ConnectionError('lost')
    monkeypatch.setattr(publisher, '_check_lock', lose_lock)
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['stopped'] == 'database_connection_lost'
    assert bot.send_message.await_count == 1
    engine.dispose()


def test_batch_stops_when_destination_changes():
    engine, factory = setup_db(); bot = mock_bot()
    with factory() as db:
        db.get(Product, 2).approved = True
        db.commit()
    async def send_and_rebind(**kwargs):
        with factory() as db:
            db.get(CatalogDestination, 1).chat_id = -200
            db.commit()
        return SimpleNamespace(message_id=101)
    bot.send_message.side_effect = send_and_rebind
    result = asyncio.run(publisher.reconcile_publication(bot, factory))
    assert result['stopped'] == 'data_changed'
    assert bot.send_message.await_count == 1
    engine.dispose()


def test_publication_status_explains_ineligible_products(monkeypatch):
    engine, factory = setup_db()
    monkeypatch.setattr(publisher, 'Sessions', factory)
    with factory() as db:
        db.add(Product(sku_key='no-offer',brand='Apple',model='AirPods',category='Наушники',
                       memory='',color='',sim='',approved=True,hidden=False))
        db.commit()
    result = publisher.publication_status()
    assert result['blockers']['total'] == 4
    assert result['blockers']['needs_approval'] == 1
    assert result['blockers']['missing_sim'] == 1
    assert result['blockers']['no_offer'] == 1
    assert len(result['items']) == 1
    engine.dispose()
