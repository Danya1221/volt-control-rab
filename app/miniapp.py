"""Owner-only Mini App and QR login for up to two Telegram reader accounts."""
import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import time
from contextlib import suppress
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from urllib.parse import parse_qsl, urlparse
import re
from zoneinfo import ZoneInfo

import qrcode
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Integer, String, Text, DateTime, Numeric, ForeignKey, UniqueConstraint, select, text, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column
from telethon import TelegramClient, functions
from telethon.errors import SessionPasswordNeededError, PasswordHashInvalidError
from telethon.sessions import StringSession

from app.main import Base, Sessions, Supplier, Product, Offer, Markup, ProductIn, FlagIn, AmountIn, key_for, today
from app.main import products as list_products, approve as approve_product, hide as hide_product, markup as set_markup
from app.integrated_collector import Base as CollectorBase, SupplierMessage, SupplierMessageTime, SupplierSource, SupplierSourceInfo, monitor_account, NoAccessibleChats
from app.price_parser import parse_lines

log = logging.getLogger(__name__)
router = APIRouter(prefix='/miniapp/api', dependencies=[])
attempts = {}
workers = {}
worker_states = {}
worker_details = {}
worker_clients = {}
locks = {1: asyncio.Lock(), 2: asyncio.Lock()}
destination_lock = asyncio.Lock()

class ReaderAccount(Base):
    __tablename__ = 'telegram_reader_accounts'
    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    encrypted_session: Mapped[str] = mapped_column(Text)
    chats_json: Mapped[str] = mapped_column(Text, default='[]')


class ReaderSwitch(Base):
    # Missing rows deliberately mean paused during the one-time migration.
    __tablename__ = 'telegram_reader_switches'
    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False)


def reader_enabled(slot):
    with Sessions() as db:
        row = db.get(ReaderSwitch, slot)
        return bool(row and row.enabled)


class ReaderBusy(Exception):
    pass


async def guarded_monitor(slot, data, config):
    """Hold a PostgreSQL session lock while this Telegram session is connected."""
    from app.main import engine
    key = int.from_bytes(hashlib.sha256(('volt-reader:' + data['telegram_id']).encode()).digest()[:8], 'big', signed=True)
    conn = engine.connect().execution_options(isolation_level='AUTOCOMMIT')
    locked = False
    task = None
    try:
        locked = bool(conn.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': key}).scalar())
        if not locked:
            raise ReaderBusy()
        task = asyncio.create_task(monitor_account(config, Sessions))
        while not task.done():
            # Fail closed if the database connection holding the lock disappears.
            done, _ = await asyncio.wait({task}, timeout=10)
            if not done:
                conn.execute(text('SELECT 1'))
        await task
    finally:
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if locked:
            try:
                conn.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': key})
            except Exception:
                conn.invalidate()
        conn.close()


class OfferImport(Base):
    """Trace each applied offer back to its captured message."""
    __tablename__ = 'reviewed_offer_imports'
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey('suppliers.id'))
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'))
    account: Mapped[str] = mapped_column(String(100))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    line: Mapped[int] = mapped_column(Integer)
    message_hash: Mapped[str] = mapped_column(String(64))
    cost: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SupplierSourceBinding(Base):
    __tablename__ = 'supplier_source_bindings'
    __table_args__ = (UniqueConstraint('account', 'chat_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(100))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    supplier_id: Mapped[int] = mapped_column(ForeignKey('suppliers.id'))


class IncomingProduct(Base):
    """An unconfirmed price from an actual captured supplier message."""
    __tablename__ = 'incoming_product_sources'
    __table_args__ = (UniqueConstraint('account', 'chat_id', 'message_id', 'line', 'message_hash'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'))
    account: Mapped[str] = mapped_column(String(100))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    stored_message_id: Mapped[int] = mapped_column(Integer)
    line: Mapped[int] = mapped_column(Integer)
    message_hash: Mapped[str] = mapped_column(String(64))
    source_price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    source_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AutomaticIphone(Base):
    # Retain the previous iPhone marker so a later rejection stays rejected.
    __tablename__ = 'automatic_iphone_products'
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'), primary_key=True)


class AutomaticProduct(Base):
    # Never re-approve a product that an owner later rejects.
    __tablename__ = 'automatic_products'
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'), primary_key=True)


AUTO_CATEGORIES = {'Смартфоны', 'Планшеты', 'Ноутбуки', 'Умные часы', 'Наушники',
                   'Умные кольца', 'Микрофоны', 'Веб-камеры', 'Игровые приставки',
                   'Очки', 'Камеры', 'Портативные электростанции', 'Уход за волосами',
                   'Пылесосы', 'Колонки', 'Сетевое оборудование', 'Фитнес-браслеты',
                   'Навигаторы', 'Зарядные устройства', 'Аксессуары'}


def stage_incoming_product(account, chat_id, message, now=None):
    """Stage current incoming items and apply unambiguous source-backed prices."""
    if getattr(message, 'out', False) or not getattr(message, 'date', None):
        return
    stamp = getattr(message, 'edit_date', None) or message.date
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if stamp > now + timedelta(minutes=5) or stamp.astimezone(ZoneInfo('Europe/Moscow')).date() != now.astimezone(ZoneInfo('Europe/Moscow')).date():
        return
    raw_text = message.raw_text or ''
    if not raw_text or len(raw_text) > 500000:
        return
    fingerprint = hashlib.sha256(raw_text.encode('utf-8')).hexdigest()
    with Sessions() as db:
        source = db.scalar(select(SupplierSource).where(SupplierSource.account == account, SupplierSource.chat_id == chat_id))
        row = db.scalar(select(SupplierMessage).where(SupplierMessage.account == account,
                                                       SupplierMessage.chat_id == chat_id,
                                                       SupplierMessage.message_id == message.id))
        if not source or not row or not hmac.compare_digest(hashlib.sha256(row.text.encode('utf-8')).hexdigest(), fingerprint):
            return
        parsed = parse_lines(raw_text, exclude_hi_android=bool(source.is_hi_catalog))
        # Recover provenance for prior manual confirmations using Telegram's actual date.
        candidates = parsed['candidates'] + [r['possible_iphone'] for r in parsed['needs_review'] if r.get('possible_iphone')]
        imports = db.scalars(select(OfferImport).where(OfferImport.account == account,
                            OfferImport.chat_id == chat_id, OfferImport.message_id == message.id,
                            OfferImport.message_hash == fingerprint)).all()
        for imported in imports:
            candidate = next((c for c in candidates if c['line'] == imported.line and Decimal(c['cost']) == imported.cost), None)
            product = db.get(Product, imported.product_id)
            if not candidate or not product:
                continue
            item = ProductIn(brand=candidate['brand'], model=candidate['model'], category=candidate['category'], sim=candidate['sim'])
            if product.sku_key != key_for(item):
                continue
            exists = db.scalar(select(IncomingProduct).where(IncomingProduct.account == account,
                IncomingProduct.chat_id == chat_id, IncomingProduct.message_id == message.id,
                IncomingProduct.line == imported.line, IncomingProduct.message_hash == fingerprint))
            if not exists:
                db.add(IncomingProduct(product_id=product.id, account=account, chat_id=chat_id,
                    message_id=message.id, stored_message_id=row.id, line=imported.line,
                    message_hash=fingerprint, source_price=imported.cost, source_at=stamp))
        db.commit()
        # HI's documented abbreviated iPhone format is accepted only from that
        # resolved Telegram peer, and only with an explicit, unambiguous SIM.
        candidates = list(parsed['candidates'])
        if source.is_hi_catalog:
            candidates += [r['possible_iphone'] for r in parsed['needs_review'] if r.get('possible_iphone')]
        for candidate in candidates:
            if candidate['category'] not in AUTO_CATEGORIES or len(candidate['model']) > 200:
                continue
            cost = Decimal(candidate['cost'])
            if cost <= 0 or cost > Decimal('999999999999.99'):
                continue
            item = ProductIn(brand=candidate['brand'], model=candidate['model'],
                             category=candidate['category'], sim=candidate['sim'])
            sku = key_for(item)
            if len(sku) > 600:
                continue
            existing = db.scalar(select(IncomingProduct).where(IncomingProduct.account == account,
                IncomingProduct.chat_id == chat_id, IncomingProduct.message_id == message.id,
                IncomingProduct.line == candidate['line'], IncomingProduct.message_hash == fingerprint))
            for attempt in range(2) if not existing else ():
                try:
                    with db.begin_nested():
                        product = db.scalar(select(Product).where(Product.sku_key == sku))
                        if product is None:
                            product = Product(sku_key=sku, **item.model_dump(), approved=False, hidden=False)
                            db.add(product)
                            db.flush()
                        db.add(IncomingProduct(product_id=product.id, account=account, chat_id=chat_id,
                            message_id=message.id, stored_message_id=row.id, line=candidate['line'],
                            message_hash=fingerprint, source_price=cost, source_at=stamp))
                        db.flush()
                    break
                except IntegrityError:
                    if attempt:
                        raise
            # Commit each entry so retries cannot discard earlier valid products.
            db.commit()
            if _automatic_product_candidate(candidate):
                _apply_automatic_product(db, account, chat_id, message.id, candidate, fingerprint, now)


def _automatic_product_candidate(candidate):
    """Accept recognised exact supplier labels, not uncertain parser results."""
    if candidate['category'] == 'Смартфоны':
        # Keep the existing stricter phone identity and explicit SIM requirement.
        return (candidate['brand'] == 'Apple'
                and candidate['sim'] in {'eSIM', 'SIM + eSIM', '2 SIM', '1 SIM'}
                and bool(re.match(r'^iPhone\s+1[3-8](?:[eе])?(?:\s+(?:pro(?:\s+max)?|plus|air))?\s+(?:128|256|512|1\s*tb|2\s*tb)\b',
                                  candidate['model'], re.I)))
    # A number supplies an explicit model, series or version, so a bare family
    # such as "AirPods Pro" does not silently stand for multiple generations.
    if not re.search(r'\d', candidate['model']):
        return False
    if candidate['brand'] == 'Apple' and candidate['category'] == 'Умные часы':
        return bool(re.search(r'\b(?:se\s*\d+|s\s*\d+|series\s*\d+|ul(?:tra)?\s*\d+)\b', candidate['model'], re.I))
    return candidate['category'] in AUTO_CATEGORIES


def _apply_automatic_product(db, account, chat_id, message_id, candidate, fingerprint, now):
    """Apply a fresh exact source line; never infer absence or overwrite newer data."""
    source = db.scalar(select(SupplierSource).where(SupplierSource.account == account,
                       SupplierSource.chat_id == chat_id).with_for_update())
    message = db.scalar(select(SupplierMessage).where(SupplierMessage.account == account,
                        SupplierMessage.chat_id == chat_id, SupplierMessage.message_id == message_id).with_for_update())
    if not source or not message or not hmac.compare_digest(hashlib.sha256(message.text.encode('utf-8')).hexdigest(), fingerprint):
        db.rollback()
        return
    incoming = db.scalar(select(IncomingProduct).where(IncomingProduct.account == account,
        IncomingProduct.chat_id == chat_id, IncomingProduct.message_id == message_id,
        IncomingProduct.line == candidate['line'], IncomingProduct.message_hash == fingerprint))
    if not incoming:
        db.rollback()
        return
    source_at = incoming.source_at
    if source_at.tzinfo is None:
        source_at = source_at.replace(tzinfo=timezone.utc)
    if source_at > now or source_at.astimezone(ZoneInfo('Europe/Moscow')).date() != now.astimezone(ZoneInfo('Europe/Moscow')).date():
        db.rollback()
        return
    latest = db.scalar(select(IncomingProduct).where(IncomingProduct.account == account,
        IncomingProduct.chat_id == chat_id, IncomingProduct.product_id == incoming.product_id)
        .order_by(IncomingProduct.source_at.desc(), IncomingProduct.id.desc()).limit(1))
    if not latest or latest.id != incoming.id:
        db.rollback()
        return
    product = db.scalar(select(Product).where(Product.id == incoming.product_id).with_for_update())
    if not product or product.hidden:
        db.rollback()
        return
    binding = db.scalar(select(SupplierSourceBinding).where(SupplierSourceBinding.account == account,
                        SupplierSourceBinding.chat_id == chat_id))
    if not binding:
        previous = db.scalars(select(OfferImport.supplier_id).where(OfferImport.account == account,
                              OfferImport.chat_id == chat_id).distinct().limit(2)).all()
        if len(previous) > 1:
            db.rollback()
            return
        supplier = db.get(Supplier, previous[0]) if previous else None
        if previous and not supplier:
            db.rollback()
            return
        if not supplier:
            source_name = f'Telegram {account}:{chat_id}'
            supplier = db.scalar(select(Supplier).where(Supplier.name == source_name))
            if not supplier:
                supplier = Supplier(name=source_name)
                db.add(supplier)
                db.flush()
        binding = SupplierSourceBinding(account=account, chat_id=chat_id, supplier_id=supplier.id)
        db.add(binding)
        db.flush()
    other_binding = db.scalar(select(SupplierSourceBinding.id).where(
        SupplierSourceBinding.supplier_id == binding.supplier_id,
        SupplierSourceBinding.id != binding.id).limit(1))
    if other_binding:
        db.rollback()
        return
    marker = db.get(AutomaticProduct, product.id) or db.get(AutomaticIphone, product.id)
    if marker and not product.approved:
        db.rollback()
        return
    if not marker:
        db.add(AutomaticProduct(product_id=product.id))
        product.approved = True
    imported = db.scalar(select(OfferImport.id).where(OfferImport.account == account,
        OfferImport.chat_id == chat_id, OfferImport.message_id == message_id,
        OfferImport.line == candidate['line'], OfferImport.message_hash == fingerprint,
        OfferImport.product_id == product.id, OfferImport.supplier_id == binding.supplier_id))
    if not imported:
        offer = db.scalar(select(Offer).where(Offer.product_id == product.id,
                          Offer.supplier_id == binding.supplier_id).with_for_update())
        if offer is None:
            offer = Offer(product_id=product.id, supplier_id=binding.supplier_id)
            db.add(offer)
        offer.cost = incoming.source_price
        offer.confirmed_date = source_at.astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()
        offer.active = True
        db.add(OfferImport(supplier_id=binding.supplier_id, product_id=product.id, account=account,
               chat_id=chat_id, message_id=message_id, line=candidate['line'], message_hash=fingerprint,
               cost=incoming.source_price, imported_at=now))
    db.commit()


class CatalogDestination(Base):
    __tablename__ = 'catalog_destination'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(255))
    is_forum: Mapped[bool] = mapped_column(default=False)
    can_manage_topics: Mapped[bool] = mapped_column(default=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CatalogTopic(Base):
    __tablename__ = 'catalog_topics'
    __table_args__ = (UniqueConstraint('chat_id', 'thread_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    thread_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(255))
    closed: Mapped[bool | None] = mapped_column(nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CatalogTopicRule(Base):
    __tablename__ = 'catalog_topic_rules'
    __table_args__ = (UniqueConstraint('chat_id', 'brand_key', 'category_key', 'product_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    brand: Mapped[str] = mapped_column(String(200), default='')
    category: Mapped[str] = mapped_column(String(100), default='')
    brand_key: Mapped[str] = mapped_column(String(200), default='')
    category_key: Mapped[str] = mapped_column(String(100), default='')
    thread_id: Mapped[int] = mapped_column(BigInteger)
    product_id: Mapped[int] = mapped_column(Integer, default=0)


def topic_for_product(db, chat_id, brand, category, product_id=None):
    # SKU first, then brand+category, brand, and category. No implicit default.
    if product_id:
        rule = db.scalar(select(CatalogTopicRule).where(CatalogTopicRule.chat_id == chat_id, CatalogTopicRule.product_id == product_id))
        if rule:
            topic = db.scalar(select(CatalogTopic).where(CatalogTopic.chat_id == chat_id, CatalogTopic.thread_id == rule.thread_id))
            return topic if topic and topic.closed is not True else None
    brand, category = brand.strip().casefold(), category.strip().casefold()
    for b, c in ((brand, category), (brand, ''), ('', category)):
        if not (b or c):
            continue
        rule = db.scalar(select(CatalogTopicRule).where(CatalogTopicRule.chat_id == chat_id,
                            CatalogTopicRule.brand_key == b, CatalogTopicRule.category_key == c, CatalogTopicRule.product_id == 0))
        if rule:
            topic = db.scalar(select(CatalogTopic).where(CatalogTopic.chat_id == chat_id,
                                                         CatalogTopic.thread_id == rule.thread_id))
            return topic if topic and topic.closed is not True else None
    return None


def verify_init_data(raw, token, owner_id, now=None):
    if not raw or len(raw) > 8192:
        return False
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=64)
        data = dict(pairs)
        if len(pairs) != len(data):
            return False
        received = data.pop('hash')
        check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
        secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return False
        age = (time.time() if now is None else now) - int(data['auth_date'])
        user = json.loads(data['user'])
        return -30 <= age <= 3600 and type(user.get('id')) is int and user['id'] == owner_id
    except (ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError):
        return False


def owner(x_telegram_init_data: str = Header(default='')):
    if os.getenv('VOLT_MINIAPP_ENABLED') != '1':
        raise HTTPException(503, 'Mini App отключено')
    token, owner_id = os.getenv('TG_BOT_TOKEN', ''), os.getenv('VOLT_OWNER_TG_ID', '')
    if not token or not owner_id.isdigit() or not verify_init_data(x_telegram_init_data, token, int(owner_id)):
        raise HTTPException(401, 'Откройте приложение заново из Telegram под аккаунтом владельца')


def destination_payload(row):
    return {'connected': True, 'chat_id': str(row.chat_id), 'title': row.title,
            'is_forum': row.is_forum, 'can_manage_topics': row.can_manage_topics,
            'checked_at': row.checked_at.isoformat(), 'publication_enabled': False}


@router.get('/destination', dependencies=[Depends(owner)])
def get_destination():
    with Sessions() as db:
        row = db.get(CatalogDestination, 1)
        return destination_payload(row) if row else {'connected': False, 'publication_enabled': False}


class DestinationIn(BaseModel):
    chat: str = Field(min_length=1, max_length=128, pattern=r'^(?:-\d+|@[A-Za-z0-9_]{5,32})$')


async def verify_destination(bot, target):
    chat = await bot.get_chat(target)
    if chat.type not in ('group', 'supergroup'):
        raise HTTPException(422, 'Укажите группу или супергруппу, а не личный чат или канал')
    admin_statuses = ('creator', 'administrator')
    member = await bot.get_chat_member(chat.id, bot.id)
    if member.status not in admin_statuses:
        raise HTTPException(422, 'Добавьте бота в группу и назначьте администратором')
    account = await bot.get_chat_member(chat.id, int(os.environ['VOLT_OWNER_TG_ID']))
    if account.status not in admin_statuses:
        raise HTTPException(403, 'Аккаунт владельца VOLT должен быть администратором этой группы')
    return {'chat_id': chat.id, 'title': (chat.title or str(chat.id))[:255],
            'is_forum': bool(getattr(chat, 'is_forum', False)),
            'can_manage_topics': member.status == 'creator' or bool(getattr(member, 'can_manage_topics', False))}


@router.put('/destination', dependencies=[Depends(owner)])
async def bind_destination(body: DestinationIn):
    token = os.getenv('TG_BOT_TOKEN')
    if not token:
        raise HTTPException(503, 'Токен бота не настроен')
    async with destination_lock:
        bot = Bot(token=token)
        try:
            target = int(body.chat) if body.chat.startswith('-') else body.chat
            data = await asyncio.wait_for(verify_destination(bot, target), timeout=15)
        except (TelegramAPIError, asyncio.TimeoutError):
            raise HTTPException(422, 'Группа недоступна. Проверьте ID, добавление бота и его права администратора') from None
        finally:
            await bot.session.close()
        with Sessions() as db:
            row = db.get(CatalogDestination, 1)
            if row is None:
                row = CatalogDestination(id=1, **data, checked_at=datetime.now(timezone.utc))
                db.add(row)
            else:
                for key, value in data.items():
                    setattr(row, key, value)
                row.checked_at = datetime.now(timezone.utc)
            db.commit()
            return destination_payload(row)


@router.delete('/destination', dependencies=[Depends(owner)])
async def unbind_destination():
    async with destination_lock:
        with Sessions() as db:
            row = db.get(CatalogDestination, 1)
            if row:
                db.delete(row)
                db.commit()
    return {'connected': False, 'publication_enabled': False}


def current_forum(db):
    destination = db.get(CatalogDestination, 1)
    if not destination:
        raise HTTPException(409, 'Сначала привяжите чат каталога')
    if not destination.is_forum:
        raise HTTPException(422, 'В привязанном чате не включены темы')
    return destination


def save_topic(db, chat_id, thread_id, title, closed=None):
    topic = db.scalar(select(CatalogTopic).where(CatalogTopic.chat_id == chat_id, CatalogTopic.thread_id == thread_id))
    if topic is None:
        topic = CatalogTopic(chat_id=chat_id, thread_id=thread_id)
        db.add(topic)
    topic.title = title[:255]
    topic.closed = closed
    topic.checked_at = datetime.now(timezone.utc)
    return topic


def topic_data(db, destination):
    topics = db.scalars(select(CatalogTopic).where(CatalogTopic.chat_id == destination.chat_id).order_by(CatalogTopic.title)).all()
    rules = db.scalars(select(CatalogTopicRule).where(CatalogTopicRule.chat_id == destination.chat_id).order_by(CatalogTopicRule.id)).all()
    return {'chat_id': str(destination.chat_id),
            'topics': [{'thread_id': t.thread_id, 'title': t.title, 'closed': t.closed} for t in topics],
            'rules': [{'id': r.id, 'brand': r.brand, 'category': r.category, 'product_id': r.product_id, 'thread_id': r.thread_id} for r in rules],
            'publication_enabled': False}


@router.get('/destination/topics', dependencies=[Depends(owner)])
def list_catalog_topics():
    with Sessions() as db:
        return topic_data(db, current_forum(db))


@router.post('/destination/topics/sync', dependencies=[Depends(owner)])
async def sync_catalog_topics():
    # Bot API does not expose a list of forum topics. Return observed topics;
    # never mark unseen topics closed or use supplier-account sessions here.
    async with destination_lock:
        with Sessions() as db:
            data = topic_data(db, current_forum(db))
            data['loaded_count'] = len(data['topics'])
            data['incomplete'] = True
            return data


@router.post('/destination/topics/join/{slot}', dependencies=[Depends(owner)])
async def join_catalog_group(slot: int):
    validate_slot(slot)
    raise HTTPException(410, 'Личные аккаунты для тем больше не используются.')


async def observe_catalog_topic(message):
    """Observe Bot API forum events without replying or fetching user sessions."""
    chat = getattr(message, 'chat', None)
    if not chat or getattr(chat, 'type', None) != 'supergroup' or not getattr(chat, 'is_forum', False):
        return
    created = getattr(message, 'forum_topic_created', None)
    edited = getattr(message, 'forum_topic_edited', None)
    closed_event = getattr(message, 'forum_topic_closed', None)
    reopened = getattr(message, 'forum_topic_reopened', None)
    general_hidden = getattr(message, 'general_forum_topic_hidden', None)
    general_unhidden = getattr(message, 'general_forum_topic_unhidden', None)
    thread_id = getattr(message, 'message_thread_id', None)
    if not thread_id and created:
        thread_id = getattr(message, 'message_id', None)
    if not thread_id and (general_hidden or general_unhidden):
        thread_id = 1
    if type(thread_id) is not int or thread_id <= 0:
        return
    title = getattr(created or edited, 'name', None)
    if not isinstance(title, str) or not title.strip():
        title = None
    # Hiding General changes its visibility, not whether it is closed.
    state = True if closed_event else False if created or reopened else None
    async with destination_lock:
        with Sessions() as db:
            destination = db.get(CatalogDestination, 1)
            if not destination or not destination.is_forum or destination.chat_id != chat.id:
                return
            topic = db.scalar(select(CatalogTopic).where(CatalogTopic.chat_id == chat.id,
                                                         CatalogTopic.thread_id == thread_id))
            if topic is None:
                topic = CatalogTopic(chat_id=chat.id, thread_id=thread_id,
                                     title=(title or f'Тема {thread_id}')[:255], closed=state,
                                     checked_at=datetime.now(timezone.utc))
                db.add(topic)
            else:
                if title:
                    topic.title = title[:255]
                if state is not None:
                    topic.closed = state
                topic.checked_at = datetime.now(timezone.utc)
            db.commit()


class TopicRuleIn(BaseModel):
    brand: str = Field(default='', max_length=200)
    category: str = Field(default='', max_length=100)
    thread_id: int = Field(gt=0)
    product_id: int = Field(default=0, ge=0)


@router.put('/destination/routes', dependencies=[Depends(owner)])
async def save_topic_rule(body: TopicRuleIn):
    brand, category = body.brand.strip(), body.category.strip()
    if not brand and not category and not body.product_id:
        raise HTTPException(422, 'Выберите товар, бренд или категорию')
    if body.product_id and (brand or category):
        raise HTTPException(422, 'Для отдельного товара не выбирайте бренд и категорию')
    async with destination_lock:
        with Sessions() as db:
            destination = current_forum(db)
            topic = db.scalar(select(CatalogTopic).where(CatalogTopic.chat_id == destination.chat_id,
                                                          CatalogTopic.thread_id == body.thread_id))
            if not topic or topic.closed is True:
                raise HTTPException(422, 'Тема не найдена или закрыта. Обновите список тем')
            if body.product_id and db.get(Product, body.product_id) is None:
                raise HTTPException(404, 'Товар не найден')
            bk, ck = brand.casefold(), category.casefold()
            row = db.scalar(select(CatalogTopicRule).where(CatalogTopicRule.chat_id == destination.chat_id,
                                                            CatalogTopicRule.brand_key == bk, CatalogTopicRule.category_key == ck,
                                                            CatalogTopicRule.product_id == body.product_id))
            if row is None:
                row = CatalogTopicRule(chat_id=destination.chat_id, brand_key=bk, category_key=ck, product_id=body.product_id)
                db.add(row)
            row.brand, row.category, row.thread_id = brand, category, body.thread_id
            db.commit()
            return {'ok': True}


@router.delete('/destination/routes/{rule_id}', dependencies=[Depends(owner)])
async def delete_topic_rule(rule_id: int):
    async with destination_lock:
        with Sessions() as db:
            destination = current_forum(db)
            row = db.scalar(select(CatalogTopicRule).where(CatalogTopicRule.id == rule_id, CatalogTopicRule.chat_id == destination.chat_id))
            if row is None:
                raise HTTPException(404, 'Правило не найдено')
            db.delete(row)
            db.commit()
    return {'ok': True}


async def register_catalog_topic(chat_id, thread_id, title, bot):
    async with destination_lock:
        with Sessions() as db:
            destination = current_forum(db)
            if destination.chat_id != chat_id:
                raise HTTPException(422, 'Это не привязанный чат каталога')
        try:
            checked = await asyncio.wait_for(verify_destination(bot, chat_id), timeout=15)
        except (TelegramAPIError, asyncio.TimeoutError):
            raise HTTPException(422, 'Не удалось проверить права в чате') from None
        if not checked['is_forum']:
            raise HTTPException(422, 'В этом чате не включены темы')
        with Sessions() as db:
            destination = current_forum(db)
            if destination.chat_id != chat_id:
                raise HTTPException(409, 'Привязанный чат изменился')
            existing = db.scalar(select(CatalogTopic).where(CatalogTopic.chat_id == chat_id,
                                                            CatalogTopic.thread_id == thread_id))
            save_topic(db, chat_id, thread_id, title, existing.closed if existing else None)
            db.commit()


def settings():
    try:
        api_id = int(os.environ['TG_API_ID'])
        api_hash = os.environ['TG_API_HASH']
        cipher = Fernet(os.environ['VOLT_SESSION_ENCRYPTION_KEY'].encode())
        if api_id <= 0 or not api_hash:
            raise ValueError()
        return api_id, api_hash, cipher
    except (KeyError, ValueError, TypeError):
        raise RuntimeError('TG_API_ID, TG_API_HASH and a valid VOLT_SESSION_ENCRYPTION_KEY are required') from None


def validate_slot(slot):
    if slot not in (1, 2):
        raise HTTPException(404, 'Аккаунт не найден')


def saved(slot):
    with Sessions() as db:
        row = db.get(ReaderAccount, slot)
        if not row:
            return None
        return {'slot': slot, 'telegram_id': row.telegram_id, 'name': row.display_name,
                'session': row.encrypted_session, 'chats': json.loads(row.chats_json)}


async def reader_worker(slot):
    while True:
        data = saved(slot)
        if not data or not data['chats']:
            worker_states[slot] = 'not_configured'
            return
        if not reader_enabled(slot):
            worker_states[slot] = 'paused'
            return
        try:
            api_id, api_hash, cipher = settings()
            session_string = cipher.decrypt(data['session'].encode()).decode()
            worker_states[slot] = 'connecting'
            worker_details[slot] = {}
            def chats_checked(inaccessible):
                worker_details[slot]['inaccessible_chats'] = inaccessible
            def ready():
                worker_states[slot] = 'partial' if worker_details[slot].get('inaccessible_chats') else 'monitoring'
            account_key = f"miniapp-{data['telegram_id']}"
            async def stage(chat_id, message):
                await asyncio.to_thread(stage_incoming_product, account_key, chat_id, message)
            await guarded_monitor(slot, data, {'name': account_key, 'chats': data['chats'],
                                   'session_string': session_string, 'api_id': api_id,
                                   'api_hash': api_hash, 'on_chats': chats_checked, 'on_ready': ready,
                                   'on_message': stage,
                                   'on_client': lambda client: worker_clients.__setitem__(slot, client) if client else worker_clients.pop(slot, None)})
            worker_states[slot] = 'disconnected'
        except asyncio.CancelledError:
            raise
        except ReaderBusy:
            worker_states[slot] = 'waiting'
        except Exception as exc:
            # Only safe classifications are exposed; exception text may contain secrets.
            from telethon.errors import FloodWaitError, AuthKeyError, SessionRevokedError
            from sqlalchemy.exc import SQLAlchemyError
            if isinstance(exc, NoAccessibleChats):
                reason = 'chats_unavailable'
            elif isinstance(exc, FloodWaitError):
                reason = 'telegram_limit'
            elif isinstance(exc, (AuthKeyError, SessionRevokedError)):
                reason = 'session_invalid'
            elif isinstance(exc, SQLAlchemyError):
                reason = 'database'
            elif isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
                reason = 'network'
            else:
                reason = 'unknown'
            worker_details.setdefault(slot, {})['reason'] = reason
            worker_details[slot]['error_type'] = type(exc).__name__[:80]
            log.error('Reader slot %s failed (%s); retrying in 30 seconds', slot, type(exc).__name__)
            worker_states[slot] = 'retrying'
        await asyncio.sleep(30)


async def stop_worker(slot):
    task = workers.pop(slot, None)
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    worker_states[slot] = 'not_configured'
    worker_details.pop(slot, None)


async def restart_worker(slot):
    await stop_worker(slot)
    data = saved(slot)
    if data and data['chats']:
        if reader_enabled(slot):
            workers[slot] = asyncio.create_task(reader_worker(slot))
        else:
            worker_states[slot] = 'paused'


async def start():
    if os.getenv('VOLT_MINIAPP_ENABLED') != '1':
        return
    from app.main import engine, db_url
    if not db_url.startswith('postgresql+psycopg://'):
        raise RuntimeError('Mini App reader accounts require PostgreSQL')
    if os.getenv('WEB_CONCURRENCY', '1') != '1' or os.getenv('VOLT_COLLECTOR_ENABLED') == '1':
        raise RuntimeError('Mini App requires one worker and the legacy collector disabled')
    if not os.getenv('TG_BOT_TOKEN') or not os.getenv('VOLT_OWNER_TG_ID', '').isdigit():
        raise RuntimeError('Mini App requires TG_BOT_TOKEN and VOLT_OWNER_TG_ID')
    settings()
    Base.metadata.create_all(engine)
    CollectorBase.metadata.create_all(engine)
    for slot in (1, 2):
        await restart_worker(slot)


async def stop():
    for slot in (1, 2):
        await cancel_attempt(slot)
        await stop_worker(slot)


async def cancel_attempt(slot):
    attempt = attempts.pop(slot, None)
    if attempt:
        task = attempt.get('task')
        if task and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        else:
            await attempt['client'].disconnect()


async def complete_login(slot, attempt, qr):
    try:
        try:
            await qr.wait()
        except SessionPasswordNeededError:
            attempt['state'] = 'password_required'
            password = await asyncio.wait_for(attempt['password'].get(), timeout=180)
            try:
                await attempt['client'].sign_in(password=password)
            except PasswordHashInvalidError:
                attempt['state'] = 'password_invalid'
                return
            finally:
                password = None
        me = await attempt['client'].get_me()
        if not me or getattr(me, 'bot', False):
            raise RuntimeError('A Telegram user account is required')
        _, _, cipher = settings()
        encrypted = cipher.encrypt(attempt['client'].session.save().encode()).decode()
        # Disconnect before the monitor opens the same session.
        await attempt['client'].disconnect()
        expected_id = attempt.get('expected_id')
        if expected_id and str(me.id) != expected_id:
            attempt['state'] = 'wrong_account'
            return
        async with locks[slot]:
            if expected_id:
                current = saved(slot)
                if not current or current['telegram_id'] != expected_id or current['session'] != attempt['old_session']:
                    attempt['state'] = 'failed'
                    return
                # Stop the old session before replacing it; retain chats and account identity.
                await stop_worker(slot)
                with Sessions() as db:
                    row = db.get(ReaderAccount, slot)
                    if not row or row.telegram_id != expected_id or row.encrypted_session != attempt['old_session']:
                        attempt['state'] = 'failed'
                        return
                    row.encrypted_session = encrypted
                    row.display_name = (me.first_name or 'Telegram')[:200]
                    db.commit()
                await restart_worker(slot)
            else:
                with Sessions() as db:
                    if db.get(ReaderAccount, slot) or db.scalar(select(ReaderAccount).where(ReaderAccount.telegram_id == str(me.id))):
                        attempt['state'] = 'already_connected'
                        return
                    db.add(ReaderAccount(slot=slot, telegram_id=str(me.id),
                                         display_name=(me.first_name or 'Telegram')[:200],
                                         encrypted_session=encrypted, chats_json='[]'))
                    db.commit()
        attempt['state'] = 'connected'
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        attempt['state'] = 'expired'
    except Exception:
        log.error('Telegram login failed for slot %s', slot)
        attempt['state'] = 'failed'
    finally:
        await attempt['client'].disconnect()
        attempt.pop('qr_image', None)
        attempt.pop('old_session', None)


@router.get('/accounts', dependencies=[Depends(owner)])
async def accounts():
    result = []
    for slot in (1, 2):
        row = saved(slot)
        result.append({'slot': slot, 'connected': bool(row), 'name': row['name'] if row else None,
                       'chats': row['chats'] if row else [], 'monitor': worker_states.get(slot, 'not_configured'),
                       'monitor_details': worker_details.get(slot, {}),
                       'login': attempts.get(slot, {}).get('state', 'idle')})
    return result


@router.post('/accounts/{slot}/resume', dependencies=[Depends(owner)])
async def resume_reader(slot: int):
    validate_slot(slot)
    async with locks[slot]:
        with Sessions() as db:
            account = db.get(ReaderAccount, slot)
            if not account:
                raise HTTPException(404, 'Сначала подключите аккаунт')
            if not json.loads(account.chats_json):
                raise HTTPException(422, 'Сначала сохраните чаты')
            switch = db.get(ReaderSwitch, slot)
            if switch is None:
                db.add(ReaderSwitch(slot=slot, enabled=True))
            else:
                switch.enabled = True
            db.commit()
        await restart_worker(slot)
    return {'ok': True}


@router.get('/accounts/{slot}/message-sources', dependencies=[Depends(owner)])
def message_sources(slot: int):
    """Group saved messages by the actual Telegram peer, within one account."""
    validate_slot(slot)
    account = saved(slot)
    if not account:
        raise HTTPException(404, 'Сначала подключите аккаунт')
    account_key = f"miniapp-{account['telegram_id']}"
    with Sessions() as db:
        rows = db.execute(select(SupplierMessage.chat_id, func.count(SupplierMessage.id),
                          func.max(SupplierMessage.seen_at)).where(SupplierMessage.account == account_key)
                          .group_by(SupplierMessage.chat_id).order_by(func.max(SupplierMessage.seen_at).desc())).all()
        infos = {x.chat_id: x for x in db.scalars(select(SupplierSourceInfo).where(SupplierSourceInfo.account == account_key))}
        hi = {x.chat_id for x in db.scalars(select(SupplierSource).where(SupplierSource.account == account_key,
                                                                            SupplierSource.is_hi_catalog.is_(True)))}
        result = []
        for chat_id, count, last in rows:
            info = infos.get(chat_id)
            username = info.username if info else ''
            title = info.title if info else ''
            if not username and chat_id in hi:
                username = 'hi_catalog_bot'
            label = title + (' · @' + username if username else '') if title else ('@' + username if username else 'Чат ' + str(chat_id))
            result.append({'chat_id': str(chat_id), 'label': label, 'count': count,
                           'last_seen_at': last.isoformat() if last else None})
        return {'sources': result}


@router.get('/accounts/{slot}/messages', dependencies=[Depends(owner)])
def account_messages(slot: int, before: int | None = None, chat_id: int | None = None):
    """Show saved messages only; never fetch, send or apply prices here."""
    validate_slot(slot)
    account = saved(slot)
    if not account:
        raise HTTPException(404, 'Сначала подключите аккаунт')
    with Sessions() as db:
        query = select(SupplierMessage).where(
            SupplierMessage.account == f"miniapp-{account['telegram_id']}"
        )
        if chat_id is not None:
            if not -(2**63) <= chat_id < 2**63:
                raise HTTPException(422, 'Некорректный ID чата')
            query = query.where(SupplierMessage.chat_id == chat_id)
        if before is not None:
            query = query.where(SupplierMessage.id < before)
        rows = db.scalars(query.order_by(SupplierMessage.id.desc()).limit(21)).all()
        return {'messages': [{'id': row.id, 'chat_id': str(row.chat_id),
                               'message_id': row.message_id, 'text': row.text[:4000],
                               'truncated': len(row.text) > 4000,
                               'seen_at': row.seen_at.isoformat()} for row in rows[:20]],
                'next_before': rows[19].id if len(rows) > 20 else None}


def source_policy(db, row):
    source = db.scalar(select(SupplierSource).where(SupplierSource.account == row.account,
                                                    SupplierSource.chat_id == row.chat_id))
    if source is None:
        raise HTTPException(409, 'Источник сообщения ещё не определён. Возобновите мониторинг и попробуйте снова')
    return bool(source.is_hi_catalog)


@router.get('/accounts/{slot}/messages/{message_id}/preview', dependencies=[Depends(owner)])
def message_preview(slot: int, message_id: int):
    validate_slot(slot)
    account = saved(slot)
    if not account:
        raise HTTPException(404, 'Сначала подключите аккаунт')
    with Sessions() as db:
        row = db.scalar(select(SupplierMessage).where(
            SupplierMessage.id == message_id,
            SupplierMessage.account == f"miniapp-{account['telegram_id']}"))
        if row is None:
            raise HTTPException(404, 'Сообщение не найдено')
        if len(row.text) > 500000:
            raise HTTPException(422, 'Сообщение слишком большое для предварительного разбора')
        is_hi = source_policy(db, row)
        result = parse_lines(row.text, exclude_hi_android=is_hi)
        result['hi_android_excluded'] = is_hi
        result['message_hash'] = hashlib.sha256(row.text.encode('utf-8')).hexdigest()
        return result


class ImportLineIn(BaseModel):
    line: int = Field(ge=1)
    message_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    supplier_name: str = Field(default='', max_length=200)
    confirmed_today: bool
    confirmed_iphone: bool = False
    source_id: int | None = Field(default=None, gt=0)


@router.post('/accounts/{slot}/messages/{message_id}/import-line', dependencies=[Depends(owner)])
def import_line(slot: int, message_id: int, body: ImportLineIn):
    """Import exactly one reviewed candidate, never a complete supplier batch."""
    validate_slot(slot)
    name = body.supplier_name.strip()
    if not body.confirmed_today:
        raise HTTPException(422, 'Подтвердите, что цена актуальна сегодня')
    with Sessions() as db:
        account = db.get(ReaderAccount, slot)
        if not account:
            raise HTTPException(404, 'Сначала подключите аккаунт')
        account_key = f'miniapp-{account.telegram_id}'
        row = db.scalar(select(SupplierMessage).where(SupplierMessage.id == message_id,
                                                       SupplierMessage.account == account_key).with_for_update())
        if row is None:
            raise HTTPException(404, 'Сообщение не найдено')
        if len(row.text) > 500000:
            raise HTTPException(422, 'Сообщение слишком большое')
        fingerprint = hashlib.sha256(row.text.encode('utf-8')).hexdigest()
        if not hmac.compare_digest(fingerprint, body.message_hash):
            raise HTTPException(409, 'Сообщение изменилось. Разберите его заново')
        parsed = parse_lines(row.text, exclude_hi_android=source_policy(db, row))
        candidate = next((c for c in parsed['candidates'] if c['line'] == body.line), None)
        if candidate is None and body.confirmed_iphone:
            candidate = next((r['possible_iphone'] for r in parsed['needs_review']
                              if r['line'] == body.line and r.get('possible_iphone')), None)
        if not candidate:
            raise HTTPException(422, 'Эта строка требует дополнительной проверки и не может быть добавлена')
        cost = Decimal(candidate['cost'])
        if cost > Decimal('999999999999.99') or len(candidate['model']) > 200:
            raise HTTPException(422, 'Название или цена превышает допустимый размер')
        product_input = ProductIn(brand=candidate['brand'], model=candidate['model'],
                                  category=candidate['category'], sim=candidate['sim'])
        sku = key_for(product_input)
        if len(sku) > 600:
            raise HTTPException(422, 'Описание товара слишком длинное')
        try:
            binding = db.scalar(select(SupplierSourceBinding).where(SupplierSourceBinding.account == account_key,
                                 SupplierSourceBinding.chat_id == row.chat_id))
            if binding:
                supplier = db.get(Supplier, binding.supplier_id)
                if not supplier:
                    raise HTTPException(409, 'Сохранённый поставщик недоступен')
                if name and name.casefold() != supplier.name.casefold():
                    raise HTTPException(409, 'Для этого чата уже сохранён другой поставщик')
            else:
                previous_ids = db.scalars(select(OfferImport.supplier_id).where(
                    OfferImport.account == account_key, OfferImport.chat_id == row.chat_id).distinct().limit(2)).all()
                if len(previous_ids) > 1:
                    raise HTTPException(409, 'Для этого источника ранее использовались разные поставщики; требуется сопоставление')
                supplier = db.get(Supplier, previous_ids[0]) if previous_ids else None
                if supplier and name and name.casefold() != supplier.name.casefold():
                    raise HTTPException(409, 'Для этого источника уже использовался другой поставщик')
                if not supplier:
                    # This is a stable identifier for the actual connected account and chat,
                    # not a generated supplier or sample record.
                    source_name = name or f'Telegram {account_key}:{row.chat_id}'
                    supplier = db.scalar(select(Supplier).where(Supplier.name == source_name))
                    if supplier is None:
                        supplier = Supplier(name=source_name)
                        db.add(supplier)
                        db.flush()
                db.add(SupplierSourceBinding(account=account_key, chat_id=row.chat_id, supplier_id=supplier.id))
            product = db.scalar(select(Product).where(Product.sku_key == sku))
            if body.source_id is not None:
                if product is None:
                    raise HTTPException(409, 'Товар изменился. Обновите проверку')
                latest = db.scalar(select(IncomingProduct).where(IncomingProduct.account == account_key,
                    IncomingProduct.chat_id == row.chat_id, IncomingProduct.product_id == product.id)
                    .order_by(IncomingProduct.source_at.desc(), IncomingProduct.id.desc()).limit(1))
                if not latest or latest.id != body.source_id or latest.stored_message_id != row.id or latest.line != body.line or latest.message_hash != fingerprint:
                    raise HTTPException(409, 'Появилась более новая запись. Обновите проверку')
            if product is None:
                product = Product(sku_key=sku, **product_input.model_dump(), approved=False, hidden=False)
                db.add(product)
                db.flush()
            offer = db.scalar(select(Offer).where(Offer.product_id == product.id, Offer.supplier_id == supplier.id))
            if offer is None:
                offer = Offer(product_id=product.id, supplier_id=supplier.id, cost=cost,
                              confirmed_date=today(), active=True)
                db.add(offer)
            else:
                offer.cost, offer.confirmed_date, offer.active = cost, today(), True
            meta = db.get(SupplierMessageTime, row.id)
            if meta and not meta.outgoing:
                verified = db.scalar(select(IncomingProduct).where(IncomingProduct.account == account_key,
                    IncomingProduct.chat_id == row.chat_id, IncomingProduct.message_id == row.message_id,
                    IncomingProduct.line == body.line, IncomingProduct.message_hash == fingerprint))
                if verified is None:
                    db.add(IncomingProduct(product_id=product.id, account=account_key, chat_id=row.chat_id,
                        message_id=row.message_id, stored_message_id=row.id, line=body.line,
                        message_hash=fingerprint, source_price=cost, source_at=meta.source_at))
            db.add(OfferImport(supplier_id=supplier.id, product_id=product.id, account=account_key,
                               chat_id=row.chat_id, message_id=row.message_id, line=body.line,
                               message_hash=fingerprint, cost=cost, imported_at=datetime.now(timezone.utc)))
            db.commit()
            return {'product_id': product.id, 'approved': product.approved}
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, 'Данные изменились. Обновите панель и повторите') from None


@router.get('/products', dependencies=[Depends(owner)])
def mini_products():
    with Sessions() as db:
        return list_products(db)


@router.get('/product-sources', dependencies=[Depends(owner)])
def product_sources():
    with Sessions() as db:
        accounts = {f'miniapp-{a.telegram_id}': a.slot for a in db.scalars(select(ReaderAccount)).all()}
        confirmed = select(OfferImport.id).where(OfferImport.product_id == IncomingProduct.product_id,
                          OfferImport.account == IncomingProduct.account, OfferImport.chat_id == IncomingProduct.chat_id,
                          OfferImport.message_id == IncomingProduct.message_id, OfferImport.line == IncomingProduct.line,
                          OfferImport.message_hash == IncomingProduct.message_hash).exists()
        latest_per_source = select(IncomingProduct.id.label('id'), func.row_number().over(
            partition_by=(IncomingProduct.product_id, IncomingProduct.account, IncomingProduct.chat_id),
            order_by=(IncomingProduct.source_at.desc(), IncomingProduct.id.desc())).label('rank')).subquery()
        records = db.execute(select(IncomingProduct, SupplierMessage, confirmed.label('confirmed'))
                             .join(latest_per_source, latest_per_source.c.id == IncomingProduct.id)
                             .join(SupplierMessage, SupplierMessage.id == IncomingProduct.stored_message_id)
                             .where(latest_per_source.c.rank == 1)
                             .order_by(IncomingProduct.source_at.desc(), IncomingProduct.id.desc()).limit(5000)).all()
        bindings = {(b.account, b.chat_id): name for b, name in db.execute(
            select(SupplierSourceBinding, Supplier.name).join(Supplier, Supplier.id == SupplierSourceBinding.supplier_id)).all()}
        output = []
        seen = set()
        for source, message, already_confirmed in records:
            slot = accounts.get(source.account)
            if not slot:
                continue
            key = (source.product_id, source.account, source.chat_id)
            if key in seen:
                continue
            # Never fall back to older prices if the latest source was confirmed or changed.
            seen.add(key)
            if already_confirmed or not hmac.compare_digest(hashlib.sha256(message.text.encode('utf-8')).hexdigest(), source.message_hash):
                continue
            output.append({'source_id': source.id, 'product_id': source.product_id, 'slot': slot, 'chat_id': str(source.chat_id),
                           'message_id': source.stored_message_id, 'line': source.line,
                           'message_hash': source.message_hash, 'price': str(source.source_price),
                           'supplier_name': bindings.get((source.account, source.chat_id), ''),
                           'source_at': source.source_at.isoformat()})
        return output


@router.patch('/products/{product_id}/approve', dependencies=[Depends(owner)])
def mini_approve(product_id: int, body: FlagIn):
    with Sessions() as db:
        return approve_product(product_id, body, db)


@router.patch('/products/{product_id}/hide', dependencies=[Depends(owner)])
def mini_hide(product_id: int, body: FlagIn):
    with Sessions() as db:
        return hide_product(product_id, body, db)


class MiniMarkupIn(BaseModel):
    scope: str
    target: str = Field(min_length=1, max_length=200)
    amount: Decimal = Field(ge=0, le=Decimal('999999999999.99'), max_digits=14, decimal_places=2)


@router.get('/markups', dependencies=[Depends(owner)])
def mini_markups():
    with Sessions() as db:
        return [{'scope': rule.scope, 'target': rule.target, 'amount': str(rule.amount)}
                for rule in db.scalars(select(Markup).order_by(Markup.scope, Markup.target)).all()]


@router.put('/markups', dependencies=[Depends(owner)])
def mini_set_markup(body: MiniMarkupIn):
    if body.target != body.target.strip() or not body.target.strip():
        raise HTTPException(422, 'Укажите категорию, модель или ID товара')
    if body.scope == 'sku' and not body.target.isdigit():
        raise HTTPException(422, 'Для товара укажите его числовой ID')
    with Sessions() as db:
        if body.scope == 'sku' and db.get(Product, int(body.target)) is None:
            raise HTTPException(404, 'Товар не найден')
        return set_markup(body.scope, body.target, AmountIn(amount=body.amount), db)


async def begin_login(slot: int, reconnect: bool = False):
    validate_slot(slot)
    async with locks[slot]:
        existing = saved(slot)
        if reconnect and not existing:
            raise HTTPException(404, 'Сначала подключите аккаунт')
        if not reconnect and existing:
            raise HTTPException(409, 'Аккаунт уже подключён')
        await cancel_attempt(slot)
        api_id, api_hash, _ = settings()
        client = TelegramClient(StringSession(), api_id, api_hash)
        try:
            await client.connect()
            qr = await client.qr_login()
            out = io.BytesIO()
            qrcode.make(qr.url).save(out, format='PNG')
            attempt = {'client': client, 'state': 'waiting', 'password': asyncio.Queue(maxsize=1),
                       'qr_image': base64.b64encode(out.getvalue()).decode()}
            if reconnect:
                attempt.update(expected_id=existing['telegram_id'], old_session=existing['session'])
            attempts[slot] = attempt
            attempt['task'] = asyncio.create_task(complete_login(slot, attempt, qr))
            await asyncio.sleep(0)  # Install Telethon's login-token handler before returning the QR.
            return {'state': 'waiting', 'qr_image': attempt['qr_image']}
        except Exception:
            await client.disconnect()
            raise HTTPException(502, 'Не удалось начать вход в Telegram') from None


@router.post('/accounts/{slot}/login', dependencies=[Depends(owner)])
async def login(slot: int):
    return await begin_login(slot)


@router.post('/accounts/{slot}/reconnect', dependencies=[Depends(owner)])
async def reconnect(slot: int):
    return await begin_login(slot, reconnect=True)


@router.get('/accounts/{slot}/login', dependencies=[Depends(owner)])
async def login_status(slot: int):
    validate_slot(slot)
    attempt = attempts.get(slot, {})
    return {'state': attempt.get('state', 'idle')}


class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=256, repr=False)

@router.post('/accounts/{slot}/password', dependencies=[Depends(owner)])
async def password(slot: int, body: PasswordIn):
    validate_slot(slot)
    async with locks[slot]:
        attempt = attempts.get(slot)
        if not attempt or attempt['state'] != 'password_required':
            raise HTTPException(409, 'Пароль сейчас не требуется')
        attempt['state'] = 'checking_password'
        attempt['password'].put_nowait(body.password)
    return {'state': 'checking_password'}


class ChatsIn(BaseModel):
    chats: list[str] = Field(max_length=30)

@router.put('/accounts/{slot}/chats', dependencies=[Depends(owner)])
async def configure_chats(slot: int, body: ChatsIn):
    validate_slot(slot)
    chats = [c.strip() for c in body.chats]
    if any(not c or len(c) > 128 or not (c.startswith('@') or c.lstrip('-').isdigit()) for c in chats) or len(set(chats)) != len(chats):
        raise HTTPException(422, 'Укажите уникальные @username или числовые ID чатов')
    values = [int(c) if c.lstrip('-').isdigit() else c for c in chats]
    async with locks[slot]:
        with Sessions() as db:
            row = db.get(ReaderAccount, slot)
            if not row:
                raise HTTPException(404, 'Сначала подключите аккаунт')
            row.chats_json = json.dumps(values, ensure_ascii=False)
            db.commit()
        await restart_worker(slot)
    return {'ok': True}


@router.delete('/accounts/{slot}', dependencies=[Depends(owner)])
async def disconnect(slot: int):
    validate_slot(slot)
    async with locks[slot]:
        await cancel_attempt(slot)
        await stop_worker(slot)
        with Sessions() as db:
            row = db.get(ReaderAccount, slot)
            if row:
                db.delete(row)
                db.commit()
    return {'ok': True}




