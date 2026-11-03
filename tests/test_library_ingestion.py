"""Recognition and processing against an isolated database."""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.main import Offer, Product, Supplier
from app import miniapp
from app.integrated_collector import SupplierSource, persist
from tests.test_incoming_products import msg, setup


def generic_source(monkeypatch):
    engine, factory = setup(monkeypatch)
    # This is the general supplier feed, not the separate HI Android policy.
    with factory() as db:
        db.scalar(select(SupplierSource)).is_hi_catalog = False
        db.commit()
    return engine, factory


def test_library_stages_and_applies_explicit_nonphone_models(monkeypatch):
    engine, factory = generic_source(monkeypatch)
    now = datetime.now(timezone.utc)
    source = 'miniapp-1234'
    raw = ('**• Meta Ray-Ban Wayfarer Gen2 S50 Black RW4012🇺🇸**\n'
           '🇺🇸 От 1 шт - 31 500\n'
           '**• Insta 360 X5 Essentials Bundle🇬🇧**\n'
           '🇬🇧 От 1 шт - 43 500\n'
           '**• Яндекс Стрит Зеленая Алиса**\n'
           '🇷🇺 От 1 шт - 7 400\n'
           '**• Apple USB-C 20W Power Adapter A2347🇪🇺**\n'
           '🇪🇺 От 1 шт - 1 000\n'
           'Всего найдено 4 товаров')
    incoming = msg(80, raw, now)
    persist(factory, source, 1, incoming)
    miniapp.stage_incoming_product(source, 1, incoming, now)
    miniapp.stage_incoming_product(source, 1, incoming, now)
    with factory() as db:
        products = db.scalars(select(Product).order_by(Product.id)).all()
        staged = db.scalars(select(miniapp.IncomingProduct).order_by(miniapp.IncomingProduct.line)).all()
        assert len(products) == len(staged) == 4
        assert {(p.brand, p.category) for p in products} == {
            ('Meta Ray-Ban', 'Очки'), ('Insta360', 'Камеры'),
            ('Яндекс', 'Колонки'), ('Apple', 'Зарядные устройства'),
        }
        assert all(not p.hidden for p in products)
        assert {p.brand for p in products if p.approved} == {'Meta Ray-Ban', 'Insta360', 'Apple'}
        assert {p.brand for p in products if not p.approved} == {'Яндекс'}
        assert {p.id for p in products} == {s.product_id for s in staged}
        assert [s.line for s in staged] == [1, 3, 5, 7]
        assert [s.source_price for s in staged] == [
            Decimal('31500'), Decimal('43500'), Decimal('7400'), Decimal('1000')]
        assert all(s.account == source and s.chat_id == 1 and s.message_id == 80 for s in staged)
        assert all(s.stored_message_id and len(s.message_hash) == 64 for s in staged)
        assert len(db.scalars(select(Offer)).all()) == 3
        assert len(db.scalars(select(Supplier)).all()) == 1
    engine.dispose()


def test_recognised_phone_without_sim_never_becomes_confirmed_offer(monkeypatch):
    engine, factory = generic_source(monkeypatch)
    now = datetime.now(timezone.utc)
    incoming = msg(81, '**• Samsung Galaxy S26 Ultra 512 Black🇿🇦**\n'
                       '🇿🇦 От 1 шт - 91 500', now)
    persist(factory, 'miniapp-1234', 1, incoming)
    miniapp.stage_incoming_product('miniapp-1234', 1, incoming, now)
    with factory() as db:
        assert not db.scalars(select(Offer)).all()
        assert not db.scalars(select(Supplier)).all()
        assert not db.scalars(select(miniapp.IncomingProduct)).all()
        assert not db.scalars(select(Product)).all()
    engine.dispose()
