from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.main import Product, Offer
from app import miniapp
from app.publisher import _desired
from app.integrated_collector import persist
from tests.test_incoming_products import setup, msg


def test_recognised_families_in_screenshot_reach_catalog_without_price_review(monkeypatch):
    engine, factory = setup(monkeypatch)
    now = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    raw = ('SE3 44 Starlight(S/M) 2025 🇺🇸 - 21500\n'
           'S10 46 Jet Black (S/M) 🇺🇸 - 23500\n'
           'UL 3 Natural / Anchor Blue OB 🇺🇸 - 52800\n'
           'Xiaomi Pad 7 8/256 Gray - 38000\n'
           '🎧AirPods MAX 2 (2026) Orange - 39900\n'
           'Air 15 MC6L4 Midnight (M4 24/512) - 141500\n'
           'Samsung Galaxy S25 256 eSIM - 70000\n'
           '17 Pro 256 Silver 🇺🇸 - 99000\n'
           '16 Pro 256 CPO eSIM - 76000')
    m = msg(30, raw, now-timedelta(minutes=1))
    persist(factory, 'miniapp-1234', 1, m)
    miniapp.stage_incoming_product('miniapp-1234', 1, m, now)
    miniapp.stage_incoming_product('miniapp-1234', 1, m, now)
    with factory() as db:
        products = db.scalars(select(Product)).all()
        offers = db.scalars(select(Offer)).all()
        assert len(products) == len(offers) == 6
        assert all(p.approved for p in products)
        assert {p.category for p in products} == {'Умные часы', 'Планшеты', 'Наушники', 'Ноутбуки'}
        assert len(db.scalars(select(miniapp.OfferImport)).all()) == 6
        assert len(db.scalars(select(miniapp.AutomaticProduct)).all()) == 6
        db.add(miniapp.CatalogDestination(id=1, chat_id=-100, title='Catalog', is_forum=True,
                                         can_manage_topics=True, checked_at=now))
        for number, brand in ((40, 'Apple'), (41, 'Xiaomi')):
            db.add(miniapp.CatalogTopic(chat_id=-100, thread_id=number, title=brand, closed=False, checked_at=now))
            db.add(miniapp.CatalogTopicRule(chat_id=-100, thread_id=number, brand=brand,
                                           brand_key=brand.casefold(), category='', category_key='', product_id=0))
        db.commit()
        desired = _desired(db)
        assert len(desired) == 6
        assert all(desired[p.id][1] == (40 if p.brand == 'Apple' else 41) for p in products)
    engine.dispose()


def test_ambiguous_bare_family_or_watch_size_is_not_auto_approved(monkeypatch):
    engine, factory = setup(monkeypatch)
    now = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    m = msg(31, 'AirPods Pro - 13000\nApple Watch 44 mm - 20000', now-timedelta(minutes=1))
    persist(factory, 'miniapp-1234', 1, m)
    miniapp.stage_incoming_product('miniapp-1234', 1, m, now)
    with factory() as db:
        assert not db.scalars(select(Offer)).all()
        assert all(not p.approved for p in db.scalars(select(Product)))
    engine.dispose()


def test_old_iphone_marker_still_preserves_rejection(monkeypatch):
    engine, factory = setup(monkeypatch)
    now = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    m = msg(32, '17 Pro 256 Silver (eSIM) - 99000', now-timedelta(minutes=1))
    persist(factory, 'miniapp-1234', 1, m)
    miniapp.stage_incoming_product('miniapp-1234', 1, m, now)
    with factory() as db:
        product = db.scalar(select(Product))
        db.delete(db.get(miniapp.AutomaticProduct, product.id))
        db.add(miniapp.AutomaticIphone(product_id=product.id))
        product.approved = False
        db.commit()
    miniapp.stage_incoming_product('miniapp-1234', 1, m, now)
    with factory() as db:
        assert not db.scalar(select(Product)).approved
    engine.dispose()
