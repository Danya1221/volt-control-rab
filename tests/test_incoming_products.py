import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import miniapp
from app.main import app, Product, Supplier, Offer
from app.integrated_collector import Base as CollectorBase, SupplierMessage, SupplierSource, persist
from tests.test_miniapp import signed


def setup(monkeypatch):
    monkeypatch.setenv('VOLT_MINIAPP_ENABLED','1')
    monkeypatch.setenv('TG_BOT_TOKEN','bot-token')
    monkeypatch.setenv('VOLT_OWNER_TG_ID','123')
    engine = create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=StaticPool)
    miniapp.Base.metadata.create_all(engine)
    CollectorBase.metadata.create_all(engine)
    db = sessionmaker(engine)
    monkeypatch.setattr(miniapp, 'Sessions', db)
    with db() as session:
        session.add(miniapp.ReaderAccount(slot=1, telegram_id='1234', display_name='Reader', encrypted_session='x', chats_json='[]'))
        session.add(SupplierSource(account='miniapp-1234', chat_id=1, is_hi_catalog=True))
        session.commit()
    return engine, db


def msg(mid,text,date,out=False,edit_date=None):
    return SimpleNamespace(id=mid,raw_text=text,date=date,out=out,edit_date=edit_date)


def test_automatic_staging_is_idempotent_and_filters_hi_android(monkeypatch):
    engine, db = setup(monkeypatch)
    now = datetime.now(timezone.utc)
    source='miniapp-1234'
    text='🎧AirPods 4 - 8400\n(От 10шт ) - 8300\n17 Pro 256 Silver (eSIM) - 99000\nSamsung Galaxy S25 eSIM - 70000\n16 Pro 256 CPO - 76000'
    m=msg(1,text,now)
    persist(db,source,1,m)
    miniapp.stage_incoming_product(source,1,m,now)
    miniapp.stage_incoming_product(source,1,m,now)
    with db() as session:
        products=session.scalars(select(Product)).all()
        assert len(products)==2
        phone = next(p for p in products if p.category == 'Смартфоны')
        headphones = next(p for p in products if p.category == 'Наушники')
        assert phone.approved and not headphones.approved
        assert len(session.scalars(select(miniapp.IncomingProduct)).all())==2
        assert len(session.scalars(select(Supplier)).all())==1
        assert session.scalar(select(Offer)).product_id == phone.id
    other=msg(2,'🎧AirPods 5 - 10600',now-timedelta(days=1))
    outgoing=msg(3,'🎧AirPods 5 - 10600',now,out=True)
    unknown=msg(4,'🎧AirPods 5 - 10600',now)
    for chat, item in [(1,other),(1,outgoing),(2,unknown)]:
        persist(db,source,chat,item)
        miniapp.stage_incoming_product(source,chat,item,now)
    with db() as session:
        assert len(session.scalars(select(Product)).all())==2
    engine.dispose()


def test_incoming_source_exposed_only_to_owner_and_confirmed_explicitly(monkeypatch):
    engine, db=setup(monkeypatch)
    now=datetime.now(timezone.utc)
    m=msg(10,'🎧AirPods 4 - 8400',now)
    persist(db,'miniapp-1234',1,m)
    miniapp.stage_incoming_product('miniapp-1234',1,m,now)
    client=TestClient(app)
    assert client.get('/miniapp/api/product-sources').status_code==401
    headers={'X-Telegram-Init-Data':signed()}
    data=client.get('/miniapp/api/product-sources',headers=headers).json()
    assert len(data)==1 and data[0]['price']=='8400.00' and data[0]['slot']==1
    source=data[0]
    path=f"/miniapp/api/accounts/1/messages/{source['message_id']}/import-line"
    payload={'line':source['line'],'message_hash':source['message_hash'],'supplier_name':'Real supplier','confirmed_today':True}
    assert client.post(path,json=payload,headers=headers).status_code==200
    with db() as session:
        product=session.scalar(select(Product))
        assert not product.approved
        assert session.scalar(select(Offer)).cost == 8400
    # After the original message changes, its old price is not shown as current.
    with db() as session:
        session.get(SupplierMessage,source['message_id']).text='changed'
        session.commit()
    assert client.get('/miniapp/api/product-sources',headers=headers).json()==[]
    assert client.post(path,json=payload,headers=headers).status_code==409
    engine.dispose()


def test_confirmed_short_iphone_retains_real_telegram_timestamp(monkeypatch):
    from app.publication_pricing import _source_time
    from app.integrated_collector import SupplierMessageTime
    engine, db = setup(monkeypatch)
    now = datetime.now(timezone.utc)
    source = msg(15, '17 Pro 256 Silver (1Sim+eSim) - 99000', now-timedelta(seconds=30))
    persist(db, 'miniapp-1234', 1, source)
    client = TestClient(app)
    headers = {'X-Telegram-Init-Data':signed()}
    with db() as session:
        message = session.scalar(select(SupplierMessage).where(SupplierMessage.message_id==15))
        assert session.get(SupplierMessageTime, message.id)
        msg_id = message.id
    path = f'/miniapp/api/accounts/1/messages/{msg_id}'
    preview = client.get(path+'/preview', headers=headers).json()
    payload = {'line':1, 'message_hash':preview['message_hash'], 'supplier_name':'Real supplier',
               'confirmed_today':True, 'confirmed_iphone':True}
    response = client.post(path+'/import-line', headers=headers, json=payload)
    assert response.status_code==200
    with db() as session:
        offer = session.scalar(select(Offer))
        assert _source_time(session, offer, datetime.now(timezone.utc)+timedelta(seconds=1)) is not None
    engine.dispose()


def test_ambiguous_source_without_telegram_date_never_gets_fabricated_provenance(monkeypatch):
    from app.publication_pricing import _source_time
    engine, db=setup(monkeypatch)
    with db() as session:
        session.add(SupplierMessage(account='miniapp-1234',chat_id=1,message_id=16,
            text='17 Pro 256 Silver (eSIM) - 99000',seen_at=datetime.now(timezone.utc)))
        session.commit()
        msg_id=session.scalar(select(SupplierMessage.id))
    client=TestClient(app);headers={'X-Telegram-Init-Data':signed()}
    path=f'/miniapp/api/accounts/1/messages/{msg_id}'
    preview=client.get(path+'/preview',headers=headers).json()
    payload={'line':1,'message_hash':preview['message_hash'],'supplier_name':'Real supplier',
             'confirmed_today':True,'confirmed_iphone':True}
    assert client.post(path+'/import-line',json=payload,headers=headers).status_code==200
    with db() as session:
        assert _source_time(session,session.scalar(select(Offer)),datetime.now(timezone.utc)+timedelta(seconds=1)) is None
    engine.dispose()


def test_prior_manual_import_recovers_provenance_only_from_actual_message(monkeypatch):
    from app.publication_pricing import _source_time
    engine, db = setup(monkeypatch)
    raw='17 Pro 256 Silver (eSIM) - 99000'
    with db() as session:
        session.add(SupplierMessage(account='miniapp-1234', chat_id=1, message_id=17, text=raw, seen_at=datetime.now(timezone.utc)))
        session.commit()
        stored_id=session.scalar(select(SupplierMessage.id))
    client=TestClient(app);headers={'X-Telegram-Init-Data':signed()};path=f'/miniapp/api/accounts/1/messages/{stored_id}'
    preview=client.get(path+'/preview',headers=headers).json()
    assert client.post(path+'/import-line',headers=headers,json={'line':1,'message_hash':preview['message_hash'],
        'supplier_name':'Real supplier','confirmed_today':True,'confirmed_iphone':True}).status_code==200
    with db() as session:
        assert not session.scalars(select(miniapp.IncomingProduct)).all()
    stamp=datetime.now(timezone.utc)-timedelta(minutes=1)
    message=msg(17,raw,stamp)
    persist(db,'miniapp-1234',1,message)
    miniapp.stage_incoming_product('miniapp-1234',1,message,datetime.now(timezone.utc))
    with db() as session:
        assert len(session.scalars(select(miniapp.IncomingProduct)).all())==1
        assert _source_time(session,session.scalar(select(Offer)),datetime.now(timezone.utc)+timedelta(seconds=1))
    engine.dispose()


def test_repeated_messages_latest_only_and_source_bound_without_typing(monkeypatch):
    from app.main import Supplier
    engine, db = setup(monkeypatch)
    now=datetime.now(timezone.utc)
    for mid, amount in [(20, 8000), (21, 8400), (22, 8500)]:
        message=msg(mid, f'🎧AirPods 4 - {amount}', now-timedelta(minutes=22-mid))
        persist(db,'miniapp-1234',1,message)
        miniapp.stage_incoming_product('miniapp-1234',1,message,now)
    client=TestClient(app);headers={'X-Telegram-Init-Data':signed()}
    sources=client.get('/miniapp/api/product-sources',headers=headers).json()
    assert len(sources)==1 and sources[0]['price']=='8500.00'
    source=sources[0]
    payload={'source_id':source['source_id'],'line':source['line'],'message_hash':source['message_hash'],'confirmed_today':True}
    path=f"/miniapp/api/accounts/1/messages/{source['message_id']}/import-line"
    assert client.post(path,json=payload,headers=headers).status_code==200
    assert client.get('/miniapp/api/product-sources',headers=headers).json()==[]
    with db() as session:
        binding=session.scalar(select(miniapp.SupplierSourceBinding))
        supplier=session.get(Supplier,binding.supplier_id)
        assert supplier.name == 'Telegram miniapp-1234:1'
        assert len(session.scalars(select(Supplier)).all())==1
    engine.dispose()


def test_stale_selection_is_rejected_and_other_sources_remain(monkeypatch):
    engine, db=setup(monkeypatch)
    now=datetime.now(timezone.utc)
    first=msg(30,'🎧AirPods 4 - 8400',now-timedelta(minutes=3))
    persist(db,'miniapp-1234',1,first);miniapp.stage_incoming_product('miniapp-1234',1,first,now)
    client=TestClient(app);headers={'X-Telegram-Init-Data':signed()}
    selected=client.get('/miniapp/api/product-sources',headers=headers).json()[0]
    newer=msg(31,'🎧AirPods 4 - 8500',now-timedelta(minutes=1))
    persist(db,'miniapp-1234',1,newer);miniapp.stage_incoming_product('miniapp-1234',1,newer,now)
    with db() as session:
        session.add(SupplierSource(account='miniapp-1234',chat_id=2,is_hi_catalog=False));session.commit()
    other=msg(32,'🎧AirPods 4 - 8200',now)
    persist(db,'miniapp-1234',2,other);miniapp.stage_incoming_product('miniapp-1234',2,other,now)
    rows=client.get('/miniapp/api/product-sources',headers=headers).json()
    assert len(rows)==2 and {row['chat_id'] for row in rows}=={'1','2'}
    payload={'source_id':selected['source_id'],'line':selected['line'],'message_hash':selected['message_hash'],'confirmed_today':True}
    assert client.post(f"/miniapp/api/accounts/1/messages/{selected['message_id']}/import-line",json=payload,headers=headers).status_code==409
    latest=next(row for row in rows if row['chat_id']=='1')
    with db() as session:
        session.get(SupplierMessage,latest['message_id']).text='edited'
        session.commit()
    assert {row['chat_id'] for row in client.get('/miniapp/api/product-sources',headers=headers).json()}=={'2'}
    engine.dispose()
