"""Run only against an explicitly designated, disposable local PostgreSQL database."""
import hashlib
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

import app.main as main
from app import miniapp
from app.integrated_collector import Base as CollectorBase, SupplierMessage, SupplierMessageTime, SupplierSource, persist


def test_import_on_disposable_postgres(monkeypatch):
    url = os.getenv('VOLT_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('No disposable PostgreSQL configured')
    parsed = make_url(url)
    if parsed.host not in ('127.0.0.1', 'localhost') or parsed.database != 'volt_control_test':
        pytest.fail('VOLT_TEST_POSTGRES_URL must target local disposable volt_control_test')
    engine = create_engine(url)
    main.Base.metadata.create_all(engine)
    CollectorBase.metadata.create_all(engine)
    db_factory = sessionmaker(engine)
    monkeypatch.setattr(miniapp, 'Sessions', db_factory)
    automatic_candidate = miniapp._automatic_product_candidate
    monkeypatch.setattr(miniapp, '_automatic_product_candidate', lambda candidate: False)
    try:
        with db_factory() as db:
            db.add(miniapp.ReaderAccount(slot=1, telegram_id='111', display_name='Test', encrypted_session='unused', chats_json='[]'))
            db.add(SupplierSource(account='miniapp-111', chat_id=12345678901, is_hi_catalog=False))
            db.add(SupplierMessage(account='miniapp-111', chat_id=12345678901, message_id=50,
                                   text='iPhone 16 eSIM 60000', seen_at=datetime.now(timezone.utc)))
            db.commit()
            message_id = db.scalar(select(SupplierMessage.id))
        now = datetime.now(timezone.utc)
        incoming = SimpleNamespace(id=51, raw_text='AirPods 4 - 8400', date=now, edit_date=None, out=False)
        persist(db_factory, 'miniapp-111', 12345678901, incoming)
        miniapp.stage_incoming_product('miniapp-111', 12345678901, incoming, now)
        miniapp.stage_incoming_product('miniapp-111', 12345678901, incoming, now)
        with db_factory() as db:
            stored = db.scalar(select(SupplierMessage).where(SupplierMessage.message_id == 51))
            assert db.get(SupplierMessageTime, stored.id).source_at is not None
            staged = db.scalars(select(miniapp.IncomingProduct)).all()
            assert len(staged) == 1
            assert not db.get(main.Product, staged[0].product_id).approved
            assert db.scalars(select(main.Offer)).all() == []
        content_hash = hashlib.sha256(b'iPhone 16 eSIM 60000').hexdigest()
        result = miniapp.import_line(1, message_id, miniapp.ImportLineIn(
            line=1, message_hash=content_hash, supplier_name='Local test supplier', confirmed_today=True))
        assert result['approved'] is False
        sources = miniapp.product_sources()
        assert len(sources) == 1
        source = sources[0]
        assert source['supplier_name'] == 'Local test supplier'
        miniapp.import_line(1, source['message_id'], miniapp.ImportLineIn(
            source_id=source['source_id'], line=source['line'], message_hash=source['message_hash'], confirmed_today=True))
        with db_factory() as db:
            assert len(db.scalars(select(main.Supplier)).all()) == 1
            assert db.get(main.Product, result['product_id']).approved is False
            audit = db.scalar(select(miniapp.OfferImport).where(miniapp.OfferImport.message_id == 50))
            assert audit.chat_id == 12345678901
            assert audit.message_hash == content_hash
            db.add(miniapp.CatalogDestination(id=1,chat_id=-10099,title='Local',is_forum=True,can_manage_topics=True,checked_at=datetime.now(timezone.utc)))
            miniapp.save_topic(db,-10099,17,'Local topic',False)
            db.add(miniapp.CatalogTopicRule(chat_id=-10099,brand='Apple',category='Смартфоны',brand_key='apple',category_key='смартфоны',product_id=0,thread_id=17))
            db.commit()
            assert miniapp.topic_for_product(db,-10099,'Apple','Смартфоны').thread_id == 17
            db.scalar(select(SupplierSource).where(SupplierSource.chat_id == 12345678901)).is_hi_catalog = True
            db.commit()
        monkeypatch.setattr(miniapp, '_automatic_product_candidate', automatic_candidate)
        stamp = datetime.now(timezone.utc)
        phone = SimpleNamespace(id=52, raw_text='17 Pro 256 Silver (eSIM) - 99000', date=stamp, edit_date=None, out=False)
        persist(db_factory, 'miniapp-111', 12345678901, phone)
        miniapp.stage_incoming_product('miniapp-111', 12345678901, phone, stamp)
        miniapp.stage_incoming_product('miniapp-111', 12345678901, phone, stamp)
        with db_factory() as db:
            auto = db.scalar(select(main.Product).where(main.Product.model.like('iPhone 17 Pro 256%')))
            assert auto.approved
            assert db.scalar(select(main.Offer).where(main.Offer.product_id == auto.id)).cost == 99000
            assert len(db.scalars(select(miniapp.OfferImport).where(miniapp.OfferImport.product_id == auto.id)).all()) == 1
        watch = SimpleNamespace(id=53, raw_text='S10 46 Silver (M/L) 🇺🇸 - 23500', date=stamp, edit_date=None, out=False)
        persist(db_factory, 'miniapp-111', 12345678901, watch)
        miniapp.stage_incoming_product('miniapp-111', 12345678901, watch, stamp)
        with db_factory() as db:
            auto = db.scalar(select(main.Product).where(main.Product.model.like('Apple Watch S10%')))
            assert auto.approved
            assert db.scalar(select(main.Offer).where(main.Offer.product_id == auto.id)).cost == 23500
        sources = miniapp.message_sources(1)['sources']
        assert len(sources) == 1 and sources[0]['count'] == 4
        from unittest.mock import AsyncMock
        from app import publisher
        import asyncio
        with db_factory() as db:
            phone = db.scalar(select(main.Product).where(main.Product.model.like('iPhone 17 Pro 256%')))
            db.add(publisher.PublicationSwitch(id=1, enabled=True))
            db.add(publisher.Publication(product_id=phone.id, chat_id=-10099, thread_id=17,
                                         message_id=123, content='old', state='sent', error=''))
            db.commit()
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=124)),
                              edit_message_text=AsyncMock(), delete_message=AsyncMock())
        assert asyncio.run(publisher.reconcile_publication(bot, db_factory))['edited'] == 1
        assert '<b>iPhone 17 Pro</b>' in bot.edit_message_text.await_args.kwargs['text']
        bot.send_message.assert_not_awaited()
    finally:
        CollectorBase.metadata.drop_all(engine)
        main.Base.metadata.drop_all(engine)
        engine.dispose()



def test_reader_lock_blocks_parallel_use_and_releases(monkeypatch):
    import asyncio
    url = os.getenv('VOLT_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('No disposable PostgreSQL configured')
    parsed = make_url(url)
    if parsed.host not in ('127.0.0.1', 'localhost') or parsed.database != 'volt_control_test':
        pytest.fail('VOLT_TEST_POSTGRES_URL must target local disposable volt_control_test')
    engine = create_engine(url)
    monkeypatch.setattr(main, 'engine', engine)
    async def check():
        started = asyncio.Event()
        stop = asyncio.Event()
        calls = []
        async def monitor(config, db):
            calls.append(1)
            started.set()
            await stop.wait()
        monkeypatch.setattr(miniapp, 'monitor_account', monitor)
        account = {'telegram_id': '123456'}
        first = asyncio.create_task(miniapp.guarded_monitor(1, account, {}))
        await asyncio.wait_for(started.wait(), 2)
        with pytest.raises(miniapp.ReaderBusy):
            await miniapp.guarded_monitor(1, account, {})
        assert len(calls) == 1
        stop.set()
        await first
        await miniapp.guarded_monitor(1, account, {})
        assert len(calls) == 2
    try:
        asyncio.run(check())
    finally:
        engine.dispose()
