"""Optional supplier monitor inside the API process.

Disabled unless VOLT_COLLECTOR_ENABLED=1. Telegram user sessions are provided
as Telethon StringSession secrets; collected messages persist in PostgreSQL.
No Telegram messages are sent and no prices are published automatically.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, UsernameInvalidError, UsernameNotOccupiedError, ChannelPrivateError
from telethon.sessions import StringSession

log = logging.getLogger(__name__)


class NoAccessibleChats(Exception):
    """None of the configured chats could be resolved."""


class Base(DeclarativeBase):
    pass

class SupplierMessage(Base):
    __tablename__ = "supplier_telegram_messages"
    __table_args__ = (UniqueConstraint("account", "chat_id", "message_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(100))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class SupplierMessageTime(Base):
    __tablename__ = "supplier_telegram_message_times"
    stored_message_id: Mapped[int] = mapped_column(primary_key=True)
    source_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    outgoing: Mapped[bool] = mapped_column(Boolean, default=False)


class SupplierSource(Base):
    __tablename__ = "supplier_telegram_sources"
    __table_args__ = (UniqueConstraint("account", "chat_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(100))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    is_hi_catalog: Mapped[bool] = mapped_column(Boolean, default=False)


class SupplierSourceInfo(Base):
    """Display names observed when resolving a real Telegram peer."""
    __tablename__ = 'supplier_telegram_source_info'
    __table_args__ = (UniqueConstraint('account', 'chat_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(100))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str] = mapped_column(String(255), default='')
    title: Mapped[str] = mapped_column(String(255), default='')


def record_source(db, account, chat_id, configured, entity):
    # Identity comes from resolving the Telegram peer, never the supplier name typed at import.
    names = [str(configured) if isinstance(configured, str) else '', getattr(entity, 'username', '') or '']
    names += [getattr(x, 'username', '') or '' for x in (getattr(entity, 'usernames', None) or [])]
    is_hi = any(n.lstrip('@').casefold() == 'hi_catalog_bot' for n in names)
    with db() as session:
        row = session.scalar(select(SupplierSource).where(SupplierSource.account == account, SupplierSource.chat_id == chat_id))
        if row is None:
            session.add(SupplierSource(account=account, chat_id=chat_id, is_hi_catalog=is_hi))
        elif is_hi:
            row.is_hi_catalog = True
        info = session.scalar(select(SupplierSourceInfo).where(SupplierSourceInfo.account == account,
                              SupplierSourceInfo.chat_id == chat_id))
        if info is None:
            info = SupplierSourceInfo(account=account, chat_id=chat_id)
            session.add(info)
        username = getattr(entity, 'username', None) or ''
        title = getattr(entity, 'title', None) or ' '.join(filter(None, (getattr(entity, 'first_name', None),
                                                                       getattr(entity, 'last_name', None))))
        info.username = str(username).lstrip('@')[:255]
        info.title = str(title or '')[:255]
        session.commit()

class ChatCursor(Base):
    __tablename__ = "supplier_telegram_cursors"
    __table_args__ = (UniqueConstraint("account", "chat_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(100))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    last_id: Mapped[int] = mapped_column(BigInteger, default=0)

def persist(db, account, chat_id, message, advance=False):
    with db() as session:
        key = (SupplierMessage.account == account, SupplierMessage.chat_id == chat_id,
               SupplierMessage.message_id == message.id)
        row = session.scalar(select(SupplierMessage).where(*key))
        if row is None:
            row = SupplierMessage(account=account, chat_id=chat_id, message_id=message.id)
            session.add(row)
        row.text = message.raw_text or ""
        row.edited_at = message.edit_date
        row.seen_at = datetime.now(timezone.utc)
        stamp = getattr(message, 'edit_date', None) or getattr(message, 'date', None)
        if stamp is not None:
            session.flush()
            meta = session.get(SupplierMessageTime, row.id)
            if meta is None:
                meta = SupplierMessageTime(stored_message_id=row.id, source_at=stamp,
                                           outgoing=bool(getattr(message, 'out', False)))
                session.add(meta)
            else:
                meta.source_at = stamp
                meta.outgoing = bool(getattr(message, 'out', False))
        if advance:
            cursor = session.scalar(select(ChatCursor).where(
                ChatCursor.account == account, ChatCursor.chat_id == chat_id))
            if cursor is None:
                cursor = ChatCursor(account=account, chat_id=chat_id, last_id=message.id)
                session.add(cursor)
            else:
                cursor.last_id = max(cursor.last_id, message.id)
        session.commit()

async def monitor_account(config, db):
    name = config["name"]
    if "session_string" in config:
        session_string = config["session_string"]
        api_id = config["api_id"]
        api_hash = config["api_hash"]
    else:
        session_string = os.environ[config["session_env"]]
        api_id = int(os.environ[config["api_id_env"]])
        api_hash = os.environ[config["api_hash_env"]]
    chats = config["chats"]
    if not chats:
        raise ValueError(f"{name}: no supplier chats configured")
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(f"{name}: session not authorized")
        if config.get('on_client'):
            config['on_client'](client)
        entities = {}
        # StringSession does not preserve Telethon's entity cache. Populate it
        # from dialogs so explicit numeric IDs also work after a restart.
        if any(isinstance(chat, int) for chat in chats):
            await client.get_dialogs()
        inaccessible = []
        for chat in chats:
            try:
                entity = await client.get_entity(chat)
                chat_id = await client.get_peer_id(entity)
                record_source(db, name, chat_id, chat, entity)
                entities[chat_id] = entity
            except (ValueError, UsernameInvalidError, UsernameNotOccupiedError, ChannelPrivateError):
                # A bad or inaccessible chat must not stop the other configured chats.
                inaccessible.append(str(chat))
        if config.get("on_chats"):
            config["on_chats"](inaccessible)
        if not entities:
            raise NoAccessibleChats()

        async def store_message(chat_id, message, advance=False):
            persist(db, name, chat_id, message, advance=advance)
            if config.get('on_message'):
                try:
                    await config['on_message'](chat_id, message)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Keep collecting; pending products can be recovered on the next catch-up.
                    log.error('Could not stage a supplier message (%s)', type(exc).__name__)

        @client.on(events.NewMessage(chats=list(entities.values())))
        async def on_new(event):
            await store_message(event.chat_id, event.message, advance=True)

        @client.on(events.MessageEdited(chats=list(entities.values())))
        async def on_edit(event):
            await store_message(event.chat_id, event.message)

        for chat_id, entity in entities.items():
            with db() as s:
                cursor = s.scalar(select(ChatCursor).where(
                    ChatCursor.account == name, ChatCursor.chat_id == chat_id))
                last_id = cursor.last_id if cursor else 0
            async for message in client.iter_messages(entity, min_id=last_id, reverse=True):
                await store_message(chat_id, message, advance=True)
            async for message in client.iter_messages(entity, limit=200):
                await store_message(chat_id, message)
        log.info("Telegram collector %s monitoring %s chats", name, len(entities))
        if config.get("on_ready"):
            config["on_ready"]()
        if inaccessible:
            # Keep the working chats live and retry inaccessible ones periodically.
            try:
                await asyncio.wait_for(asyncio.shield(client.disconnected), timeout=120)
            except asyncio.TimeoutError:
                pass
        else:
            await client.run_until_disconnected()
    finally:
        if config.get('on_client'):
            config['on_client'](None)
        await client.disconnect()

async def run_collector(database_url):
    if not database_url.startswith("postgresql+psycopg://"):
        raise RuntimeError("Integrated Telegram collector requires PostgreSQL")
    config = json.loads(os.environ["VOLT_SUPPLIER_CONFIG_JSON"])
    accounts = config.get("accounts", [])
    if not 1 <= len(accounts) <= 2:
        raise ValueError("Configure one or two supplier accounts")
    names = [account["name"] for account in accounts]
    if len(set(names)) != len(names):
        raise ValueError("Supplier account names must be unique")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        Base.metadata.create_all(engine)
        db = sessionmaker(engine)
        await asyncio.gather(*(monitor_account(account, db) for account in accounts))
    finally:
        engine.dispose()


