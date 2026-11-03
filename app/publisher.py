"""Publish approved catalogue items without guessing prices or retrying uncertain sends.

The Telegram Bot API cannot deduplicate sendMessage, so intent is persisted
before sending; an interrupted or uncertain send requires manual resolution.
"""
from __future__ import annotations

import asyncio
import html
import hashlib
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter, TelegramUnauthorizedError
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint, select, text
from sqlalchemy.orm import Mapped, mapped_column

from app.main import Base, Offer, Product, Sessions
from app.miniapp import CatalogDestination, owner, topic_for_product
from app.publication_pricing import publication_price
from app.price_parser import sim_configuration
from app.product_lexicon import normalize_product_name

router = APIRouter(prefix='/miniapp/api/publication', dependencies=[Depends(owner)])
_lock = asyncio.Lock()


class Publication(Base):
    __tablename__ = 'catalog_publications'
    __table_args__ = (UniqueConstraint('product_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content: Mapped[str] = mapped_column(Text, default='')
    state: Mapped[str] = mapped_column(String(32), default='pending')
    error: Mapped[str] = mapped_column(String(100), default='')
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PublicationSwitch(Base):
    __tablename__ = 'catalog_publication_switch'
    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PublicationGroup(Base):
    """Stable identity for a model/topic/page, independent of its products."""
    __tablename__ = 'catalog_publication_groups'
    id: Mapped[int] = mapped_column(primary_key=True)
    group_key: Mapped[str] = mapped_column(String(64), unique=True)
    publication_key: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    thread_id: Mapped[int] = mapped_column(BigInteger)
    title_key: Mapped[str] = mapped_column(String(300))
    page: Mapped[int] = mapped_column(Integer)


def publication_enabled(factory=None):
    with (factory or Sessions)() as db:
        row = db.get(PublicationSwitch, 1)
        return bool(row and row.enabled)


def _display_name(p: Product) -> str:
    parts = []
    for item in (p.brand, p.model, p.memory, p.color, p.sim):
        item = re.sub(r'[\U0001F1E6-\U0001F1FF]{2}', '', item or '').strip()
        if item and item == (p.sim or '').strip() and item in sim_configuration(p.model or ''):
            continue
        if item and not re.search(r'(?<!\w)' + re.escape(item) + r'(?!\w)', ' '.join(parts), re.I):
            parts.append(item)
    return ' '.join(parts)


def _display(p: Product, pricing: dict) -> str:
    name = html.escape(_display_name(p))
    if pricing['status'] != 'priced':
        return f'{name}\nПо запросу'
    def money(value):
        value = Decimal(value)
        return f'{value:,.2f}'.replace(',', ' ').removesuffix('.00').replace('.', ',') + ' ₽'
    price = money(pricing['price'])
    if Decimal(pricing['discount']) > 0:
        return f"{name}\n<s>{money(pricing['base_price'])}</s> {price}"
    return f'{name}\n{price}'


def _eligible(db, p):
    if not p.approved or p.hidden:
        return False
    if p.category.casefold().strip() == 'смартфоны' and not p.sim.strip():
        return False
    return bool(db.scalar(select(Offer.id).where(Offer.product_id == p.id, Offer.active.is_(True)).limit(1)))


def _desired(db):
    destination = db.get(CatalogDestination, 1)
    if not destination or not destination.is_forum:
        raise HTTPException(409, 'Сначала привяжите чат с темами')
    result = {}
    for p in db.scalars(select(Product).order_by(Product.brand, Product.model, Product.memory, Product.color, Product.id)).all():
        if not _eligible(db, p):
            continue
        topic = topic_for_product(db, destination.chat_id, p.brand, p.category, p.id)
        if topic is not None:
            result[p.id] = (destination.chat_id, topic.thread_id, _display(p, publication_price(db, p)))
    return result


def _group_title(product):
    name = normalize_product_name(product.model or '')
    if product.brand.casefold() == 'samsung' and product.category.casefold() == 'смартфоны':
        match = re.search(r'\b(?:samsung\s+)?(?:galaxy\s+)?(?:[as]\d{2}[a-z]?|z\s+(?:flip|fold)\s*\d+)(?:\s+(?:ultra|plus|pro))?\b', name, re.I)
        if match:
            return re.sub(r'\s+', ' ', match.group(0)).strip()
    for pattern in (
        r'\biphone\s+(?:\d{1,2}[a-zе]?|se\s*\d*)(?:\s+(?:pro\s+max|pro|plus|air))?',
        r'\bapple\s+watch\s+(?:se\s*\d+|s\s*\d+|ul(?:tra)?\s*\d+)',
        r'\bairpods\s+(?:max\s*\d*|pro\s*\d*|\d+(?:\s+anc)?)',
        r'\bmacbook\s+(?:air|pro|neo)\s*\d*',
        r'\bipad\s+(?:air|pro|mini|\d+)\s*\d*',
    ):
        match = re.search(pattern, name, re.I)
        if match:
            return re.sub(r'\s+', ' ', match.group(0)).strip()
    # Keep unrelated categories separate even if they share a Telegram topic.
    return (product.brand + ' · ' + product.category).strip(' ·')


def _units(value):
    return len(value.encode('utf-16-le')) // 2


def _pages(title, entries):
    header = '<b>' + html.escape(title) + '</b>'
    pages, chunk = [], []
    for item in entries:
        candidate = header + '\n\n' + '\n\n'.join(row[1] for row in [*chunk, item])
        if chunk and _units(candidate) > 3500:
            pages.append(chunk)
            chunk = []
        chunk.append(item)
    if chunk:
        pages.append(chunk)
    return [(header + (f' · {number + 1}' if len(pages) > 1 else '') + '\n\n' +
             '\n\n'.join(row[1] for row in rows), rows) for number, rows in enumerate(pages)]


def _group_targets(db, create=False):
    """Build stable model/topic pages from individually eligible products."""
    individual = _desired(db)
    products = {p.id: p for p in db.scalars(select(Product)).all()}
    grouped = {}
    for pid, (chat, thread, content) in individual.items():
        product = products[pid]
        title = _group_title(product)
        grouped.setdefault((chat, thread, title.casefold()), (title, []))[1].append((pid, content))
    indexes = {x.group_key: x for x in db.scalars(select(PublicationGroup)).all()}
    reserved = {x.publication_key for x in indexes.values() if x.publication_key is not None}
    old = {x.product_id: x for x in db.scalars(select(Publication)).all()}
    targets, members = {}, {}
    for (chat, thread, title_key), (title, entries) in sorted(grouped.items()):
        for page, (content, rows) in enumerate(_pages(title, entries)):
            key = hashlib.sha256(f'{chat}:{thread}:{title_key}:{page}'.encode()).hexdigest()
            index = indexes.get(key)
            if index is None and create:
                index = PublicationGroup(group_key=key, publication_key=None, chat_id=chat,
                                         thread_id=thread, title_key=title_key, page=page)
                db.add(index); db.flush()
                anchor = next((pid for pid, _ in rows if pid not in reserved and pid in old
                               and old[pid].message_id and old[pid].chat_id == chat
                               and old[pid].thread_id == thread and old[pid].state != 'uncertain'), None)
                index.publication_key = anchor if anchor is not None else -index.id
                reserved.add(index.publication_key); indexes[key] = index
                db.flush()
            publication_key = index.publication_key if index else 'preview:' + key
            if publication_key is None:
                continue
            targets[publication_key] = (chat, thread, content)
            for pid, _ in rows:
                members[pid] = publication_key
    return targets, members, individual


def _current_group_target(factory, group_id, expected_chat):
    with factory() as fresh:
        destination = fresh.get(CatalogDestination, 1)
        if not destination or not destination.is_forum or destination.chat_id != expected_chat:
            raise HTTPException(409, 'Привязка чата изменена')
        group = fresh.scalar(select(PublicationGroup).where(PublicationGroup.publication_key == group_id))
        if not group or group.chat_id != destination.chat_id:
            return None
        entries, title = [], None
        query = select(Product).order_by(Product.brand, Product.model, Product.memory, Product.color, Product.id)
        for product in fresh.scalars(query):
            current_title = _group_title(product)
            if current_title.casefold() != group.title_key or not _eligible(fresh, product):
                continue
            topic = topic_for_product(fresh, destination.chat_id, product.brand, product.category, product.id)
            if topic is None or topic.thread_id != group.thread_id:
                continue
            title = title or current_title
            entries.append((product.id, _display(product, publication_price(fresh, product))))
        pages = _pages(title, entries) if title else []
        if group.page >= len(pages):
            return None
        return (group.chat_id, group.thread_id, pages[group.page][0])


def _current_target(factory, product_id, expected_chat):
    # A batch can run for minutes. Never reuse the SQLAlchemy identity map
    # for prices, routing or visibility when deciding to touch Telegram.
    with factory() as fresh:
        destination = fresh.get(CatalogDestination, 1)
        if not destination or not destination.is_forum or destination.chat_id != expected_chat:
            raise HTTPException(409, 'Привязка чата изменена')
        product = fresh.get(Product, product_id)
        if not product or not _eligible(fresh, product):
            return None
        topic = topic_for_product(fresh, destination.chat_id, product.brand, product.category, product.id)
        if topic is None:
            return None
        return (destination.chat_id, topic.thread_id, _display(product, publication_price(fresh, product)))


def _check_lock(connection):
    # A severed PostgreSQL session releases its advisory lock. Stop before
    # further Telegram calls if the lock-holding connection has been lost.
    if connection is not None:
        connection.execute(text('SELECT 1'))


@asynccontextmanager
async def _exclusive(factory):
    # SQLite is used only in isolated tests. PostgreSQL session advisory lock
    # prevents two service instances from sending the same row concurrently.
    async with _lock:
        engine = factory.kw['bind']
        connection = None
        acquired = False
        try:
            if engine.dialect.name == 'postgresql':
                connection = engine.connect().execution_options(isolation_level='AUTOCOMMIT')
                acquired = bool(connection.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': 5642817948301}).scalar())
                if not acquired:
                    raise HTTPException(409, 'Публикация уже выполняется')
            yield connection
        finally:
            if connection is not None:
                if acquired:
                    try:
                        connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': 5642817948301})
                    except Exception:
                        pass
                try:
                    connection.close()
                except Exception:
                    pass


def _stamp(row, state, error=''):
    row.state = state
    row.error = error[:100]
    row.updated_at = datetime.now(timezone.utc)


def _defer(db, exc):
    switch = db.get(PublicationSwitch, 1)
    switch.retry_at = datetime.now(timezone.utc) + timedelta(seconds=max(1, int(exc.retry_after)))


def _retry_seconds(switch):
    if not switch or not switch.retry_at:
        return 0
    due = switch.retry_at
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return max(0, int((due - datetime.now(timezone.utc)).total_seconds() + 0.999))


async def reconcile_publication(bot, factory=None):
    """Update shared model posts, then remove superseded individual posts.

    Existing individual messages are reused as anchors when possible. Intent
    is persisted before a new send; uncertain sends are never retried.
    """
    factory = factory or Sessions
    stats = {'sent': 0, 'edited': 0, 'deleted': 0, 'uncertain': 0, 'errors': 0}
    async with _exclusive(factory) as lock_connection:
        if not publication_enabled(factory):
            raise HTTPException(409, 'Публикация выключена')
        with factory() as db:
            wait = _retry_seconds(db.get(PublicationSwitch, 1))
            if wait:
                stats['retry_after'] = wait
                return stats
            for row in db.scalars(select(Publication).where(Publication.state == 'pending')).all():
                _stamp(row, 'uncertain', 'interrupted_send')
            db.commit()
            desired, members, individual = _group_targets(db, create=True)
            db.commit()
            expected_chat = db.get(CatalogDestination, 1).chat_id
            existing = {row.product_id: row for row in db.scalars(select(Publication)).all()}
            group_keys = {g.publication_key for g in db.scalars(select(PublicationGroup)).all()}
            last_send = None
            for group_id, target in desired.items():
                if not publication_enabled(factory):
                    stats['paused'] = True
                    break
                row = existing.get(group_id)
                # An old per-product send may have succeeded without a stored ID.
                # Do not produce another post for that page until it is resolved.
                uncertain_legacy = any(pid != group_id and group == group_id and
                    (legacy := existing.get(pid)) is not None and legacy.state == 'uncertain' and not legacy.message_id
                    for pid, group in members.items())
                if uncertain_legacy or (row and row.state == 'uncertain'):
                    stats['uncertain'] += 1
                    continue
                try:
                    _check_lock(lock_connection)
                    if _current_group_target(factory, group_id, expected_chat) != target:
                        stats['stopped'] = 'data_changed'; break
                except HTTPException:
                    stats['stopped'] = 'data_changed'; break
                except Exception:
                    stats['stopped'] = 'database_connection_lost'; break
                chat_id, thread_id, content = target
                if row and row.message_id:
                    if (row.chat_id, row.thread_id) != (chat_id, thread_id):
                        stats['stopped'] = 'data_changed'; break
                    if row.content == content:
                        if row.state != 'sent':
                            _stamp(row, 'sent'); db.commit()
                        continue
                    try:
                        await bot.edit_message_text(chat_id=chat_id, message_id=row.message_id,
                                                    text=content, parse_mode='HTML')
                    except asyncio.CancelledError:
                        raise
                    except TelegramRetryAfter as exc:
                        _stamp(row, 'edit_failed', type(exc).__name__); _defer(db, exc)
                        db.commit(); stats['errors'] += 1
                        stats['retry_after'] = _retry_seconds(db.get(PublicationSwitch, 1)); break
                    except TelegramBadRequest as exc:
                        if 'message is not modified' not in str(exc).casefold():
                            _stamp(row, 'edit_failed', type(exc).__name__); db.commit(); stats['errors'] += 1; continue
                    except Exception as exc:
                        _stamp(row, 'edit_failed', type(exc).__name__); db.commit(); stats['errors'] += 1; continue
                    row.content = content; _stamp(row, 'sent'); db.commit(); stats['edited'] += 1
                    continue
                if row and row.state != 'send_failed':
                    stats['uncertain'] += 1
                    continue
                if not row:
                    row = Publication(product_id=group_id, chat_id=chat_id, thread_id=thread_id,
                                      state='pending', content='', error='')
                    db.add(row); existing[group_id] = row
                if last_send is not None:
                    remaining = 3.1 - (asyncio.get_running_loop().time() - last_send)
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                if not publication_enabled(factory):
                    stats['paused'] = True; break
                try:
                    _check_lock(lock_connection)
                    if _current_group_target(factory, group_id, expected_chat) != target:
                        stats['stopped'] = 'data_changed'; break
                except HTTPException:
                    stats['stopped'] = 'data_changed'; break
                except Exception:
                    stats['stopped'] = 'database_connection_lost'; break
                row.chat_id, row.thread_id = chat_id, thread_id
                _stamp(row, 'pending'); db.commit()
                try:
                    sent = await bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                                  text=content, parse_mode='HTML')
                    last_send = asyncio.get_running_loop().time()
                    message_id = getattr(sent, 'message_id', None)
                    if type(message_id) is not int or message_id <= 0:
                        raise ValueError('Missing Telegram message ID')
                except asyncio.CancelledError:
                    _stamp(row, 'uncertain', 'cancelled_during_send'); db.commit(); raise
                except TelegramRetryAfter as exc:
                    _stamp(row, 'send_failed', type(exc).__name__); _defer(db, exc)
                    db.commit(); stats['errors'] += 1
                    stats['retry_after'] = _retry_seconds(db.get(PublicationSwitch, 1)); break
                except (TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError) as exc:
                    _stamp(row, 'send_failed', type(exc).__name__); db.commit(); stats['errors'] += 1; continue
                except Exception as exc:
                    _stamp(row, 'uncertain', type(exc).__name__); db.commit(); stats['uncertain'] += 1; continue
                row.message_id, row.content = message_id, content
                _stamp(row, 'sent'); db.commit(); stats['sent'] += 1

            # Remove old single-item posts only after their shared replacement
            # has definitely been sent/edited and still contains this product.
            all_ready = not (stats['errors'] or stats['uncertain'] or stats.get('stopped') or stats.get('paused'))
            all_ready &= all((r := existing.get(k)) is not None and r.state == 'sent' and
                             r.message_id and r.content == v[2] for k, v in desired.items())
            if not all_ready:
                return stats
            for old_id, row in list(existing.items()):
                if old_id in desired or row.state == 'uncertain':
                    continue
                if old_id in group_keys:
                    # An old page may be removed only after all new pages exist.
                    try:
                        if _current_group_target(factory, old_id, expected_chat) is not None:
                            stats['stopped'] = 'data_changed'; break
                    except Exception:
                        stats['stopped'] = 'data_changed'; break
                else:
                    group_id = members.get(old_id)
                    if group_id is None:
                        continue
                    target_row = existing.get(group_id)
                    try:
                        _check_lock(lock_connection)
                        if (_current_target(factory, old_id, expected_chat) != individual[old_id] or
                                not target_row or target_row.state != 'sent' or target_row.content != desired[group_id][2]):
                            stats['stopped'] = 'data_changed'; break
                    except Exception:
                        stats['stopped'] = 'data_changed'; break
                if not publication_enabled(factory):
                    stats['paused'] = True; break
                if row.message_id:
                    try:
                        _check_lock(lock_connection)
                        await bot.delete_message(chat_id=row.chat_id, message_id=row.message_id)
                    except asyncio.CancelledError:
                        raise
                    except TelegramRetryAfter as exc:
                        _stamp(row, 'delete_failed', type(exc).__name__); _defer(db, exc)
                        db.commit(); stats['errors'] += 1
                        stats['retry_after'] = _retry_seconds(db.get(PublicationSwitch, 1)); break
                    except TelegramBadRequest as exc:
                        if 'message to delete not found' not in str(exc).casefold():
                            _stamp(row, 'delete_failed', type(exc).__name__); db.commit(); stats['errors'] += 1; continue
                    except Exception as exc:
                        _stamp(row, 'delete_failed', type(exc).__name__); db.commit(); stats['errors'] += 1; continue
                db.delete(row); db.commit(); stats['deleted'] += 1
    return stats


@router.get('')
def publication_status():
    with Sessions() as db:
        rows = db.scalars(select(Publication)).all()
        by_product = {row.product_id: row for row in rows}
        items = []
        destination = db.get(CatalogDestination, 1)
        group_members, grouped_targets = {}, {}
        if destination and destination.is_forum:
            grouped_targets, group_members, _ = _group_targets(db)
        products = db.scalars(select(Product).order_by(Product.brand, Product.model, Product.memory, Product.color, Product.id)).all()
        blockers = {'total': len(products), 'needs_approval': 0, 'hidden': 0,
                    'missing_sim': 0, 'no_offer': 0, 'no_topic': 0, 'no_forum': 0}
        for product in products:
            if not product.approved:
                blockers['needs_approval'] += 1
            elif product.hidden:
                blockers['hidden'] += 1
            elif product.category.casefold().strip() == 'смартфоны' and not product.sim.strip():
                blockers['missing_sim'] += 1
            elif not db.scalar(select(Offer.id).where(Offer.product_id == product.id, Offer.active.is_(True)).limit(1)):
                blockers['no_offer'] += 1
            elif not destination or not destination.is_forum:
                blockers['no_forum'] += 1
            else:
                topic = topic_for_product(db, destination.chat_id, product.brand, product.category, product.id)
                if topic is None:
                    blockers['no_topic'] += 1
                    continue
                row = by_product.get(group_members.get(product.id, product.id))
                price = publication_price(db, product)
                items.append({'name': _display_name(product), 'topic': topic.title,
                              'price': price['price'], 'status': row.state if row else 'new'})
        return {'published': sum(row.state == 'sent' for row in rows),
                'enabled': bool((switch := db.get(PublicationSwitch, 1)) and switch.enabled),
                'retry_after': _retry_seconds(switch),
                'uncertain': sum(row.state in ('pending', 'uncertain') for row in rows),
                'errors': sum(row.state in ('send_failed', 'edit_failed', 'delete_failed') for row in rows),
                'price_mode': 'verified_only', 'blockers': blockers,
                'items': items,
                'groups': [{'thread_id': target[1], 'text': html.unescape(re.sub(r'<[^>]+>', '', target[2]))}
                           for target in grouped_targets.values()],
                'problem_products': [{'product_id': row.product_id, 'state': row.state, 'error': row.error}
                                     for row in rows if row.state != 'sent']}


@router.post('/sync')
async def sync_publication():
    if not publication_enabled():
        raise HTTPException(409, 'Сначала включите публикацию')
    token = os.getenv('TG_BOT_TOKEN')
    if not token:
        raise HTTPException(503, 'Токен бота не настроен')
    bot = Bot(token=token)
    try:
        return await reconcile_publication(bot)
    finally:
        await bot.session.close()


@router.post('/enable')
async def enable_publication():
    async with _lock:
        with Sessions() as db:
            destination = db.get(CatalogDestination, 1)
            if not destination or not destination.is_forum:
                raise HTTPException(409, 'Сначала привяжите чат с темами')
            row = db.get(PublicationSwitch, 1)
            if row is None:
                row = PublicationSwitch(id=1, enabled=True)
                db.add(row)
            else:
                row.enabled = True
            db.commit()
    return {'enabled': True}


@router.post('/pause')
async def pause_publication():
    # Never wait for a long-running batch. The loop re-checks this switch
    # immediately before each operation; a request already in flight may finish.
    with Sessions() as db:
        row = db.get(PublicationSwitch, 1)
        if row is None:
            row = PublicationSwitch(id=1, enabled=False)
            db.add(row)
        else:
            row.enabled = False
        db.commit()
    return {'enabled': False}
