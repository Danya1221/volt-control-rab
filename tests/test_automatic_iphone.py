import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.main import Offer, Product
from app import miniapp
from app.integrated_collector import SupplierSource, persist
from app.publication_pricing import publication_price
from app import publisher
from app.publisher import _desired
from tests.test_incoming_products import msg, setup


def test_hi_iphone_reaches_configured_topic_and_latest_price_wins(monkeypatch):
    engine, db = setup(monkeypatch)
    now = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    older = msg(1, '17 Pro 256 Silver (1Sim+eSim) - 99000', now-timedelta(minutes=10))
    newer = msg(2, '17 Pro 256 Silver (1Sim+eSim) - 101000', now-timedelta(minutes=5))
    for m in (newer, older, newer):
        persist(db, 'miniapp-1234', 1, m)
        miniapp.stage_incoming_product('miniapp-1234', 1, m, now)
    with db() as session:
        product = session.scalar(select(Product))
        offer = session.scalar(select(Offer))
        assert product.approved and product.sim == 'SIM + eSIM'
        assert offer.cost == Decimal('101000')
        assert len(session.scalars(select(miniapp.OfferImport)).all()) == 1
        assert publication_price(session, product, now)['price'] == '101000.00'
        session.add(miniapp.CatalogDestination(id=1, chat_id=-1001, title='Catalog',
                    is_forum=True, can_manage_topics=True, checked_at=now))
        topic = miniapp.CatalogTopic(chat_id=-1001, thread_id=42, title='Apple', closed=False, checked_at=now)
        session.add(topic)
        session.flush()
        session.add(miniapp.CatalogTopicRule(chat_id=-1001, thread_id=42, brand='Apple', brand_key='apple', category='', category_key='', product_id=0))
        session.add(publisher.PublicationSwitch(id=1, enabled=True))
        session.commit()
        desired = _desired(session)
        assert desired[product.id][0:2] == (-1001, 42)
    monkeypatch.setattr(publisher, 'publication_price', lambda db, product: publication_price(db, product, now))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=20)),
                          edit_message_text=AsyncMock(), delete_message=AsyncMock())
    assert asyncio.run(publisher.reconcile_publication(bot, db))['sent'] == 1
    assert bot.send_message.await_args.kwargs['message_thread_id'] == 42
    assert '101 000 ₽' in bot.send_message.await_args.kwargs['text']
    updated = msg(3, '17 Pro 256 Silver (1Sim+eSim) - 102000', now-timedelta(minutes=1))
    persist(db, 'miniapp-1234', 1, updated)
    miniapp.stage_incoming_product('miniapp-1234', 1, updated, now)
    assert asyncio.run(publisher.reconcile_publication(bot, db))['edited'] == 1
    assert '102 000 ₽' in bot.edit_message_text.await_args.kwargs['text']
    assert bot.send_message.await_count == 1
    engine.dispose()


def test_only_unambiguous_current_incoming_iphones_auto_apply(monkeypatch):
    engine, db = setup(monkeypatch)
    now = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    text = ('17 Pro 256 Silver (eSIM) - 99000\n'
            '17 Pro 256 Silver 🇺🇸 - 99000\n'
            '17 Pro 256 Silver CPO (eSIM) - 90000\n'
            'Samsung Galaxy S25 256 eSIM - 70000\n'
            'iPhone Samsung 17 Pro 256 eSIM - 90000')
    m = msg(4, text, now-timedelta(minutes=1))
    persist(db, 'miniapp-1234', 1, m)
    miniapp.stage_incoming_product('miniapp-1234', 1, m, now)
    for m in (msg(5, '17 Pro 512 Blue (eSIM) - 100000', now, out=True),
              msg(6, '17 Pro 512 Blue (eSIM) - 100000', now-timedelta(days=1)),
              msg(7, '17 Pro 512 Blue (eSIM) - 100000', now+timedelta(minutes=1))):
        persist(db, 'miniapp-1234', 1, m)
        miniapp.stage_incoming_product('miniapp-1234', 1, m, now)
    with db() as session:
        offers = session.scalars(select(Offer)).all()
        assert len(offers) == 1
        assert session.get(Product, offers[0].product_id).model.startswith('iPhone 17 Pro 256')
    engine.dispose()


def test_replayed_message_does_not_reverse_explicit_rejection(monkeypatch):
    engine, db = setup(monkeypatch)
    now = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    m = msg(10, '17 Pro 256 Silver (eSIM) - 99000', now-timedelta(minutes=1))
    persist(db, 'miniapp-1234', 1, m)
    miniapp.stage_incoming_product('miniapp-1234', 1, m, now)
    with db() as session:
        session.scalar(select(Product)).approved = False
        session.commit()
    miniapp.stage_incoming_product('miniapp-1234', 1, m, now)
    with db() as session:
        assert not session.scalar(select(Product)).approved
    engine.dispose()


def test_short_iphone_is_not_assumed_for_other_suppliers(monkeypatch):
    engine, db = setup(monkeypatch)
    with db() as session:
        session.scalar(select(SupplierSource)).is_hi_catalog = False
        session.commit()
    now = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    m = msg(11, '17 Pro 256 Silver (eSIM) - 99000', now-timedelta(minutes=1))
    persist(db, 'miniapp-1234', 1, m)
    miniapp.stage_incoming_product('miniapp-1234', 1, m, now)
    with db() as session:
        assert not session.scalars(select(Offer)).all()
    engine.dispose()
