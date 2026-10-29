"""
database.py
===========
Async SQLite persistence layer for the Royal Court bot.

Tables
------
nations        : one row per nation (exactly one leader/person each)
kingdoms       : one row per kingdom (up to 6 member nations)
transactions   : append-only audit log of EVERY money movement / stat change
settings       : per-guild config (payout channel, cycle timestamps)
pending_draws  : nation -> kingdom withdrawal requests awaiting leader approval
pending_creations : nation/kingdom creation petitions awaiting admin approval
blackjack_hands : a multi-step blackjack stake, closeable at startup to refund
poker_hands    : a held poker buy-in, closeable at startup to refund

Design notes
------------
* The connection runs in SQLite **autocommit** mode (``isolation_level=None``).
  Multi-statement money movements are wrapped in an explicit ``BEGIN IMMEDIATE``
  transaction via :meth:`Database.transaction` so a crash mid-transfer can never
  leave the ledger half-applied.
* :meth:`Database.migrate` adds columns that may be missing on databases created
  by older versions of the bot (``CREATE TABLE IF NOT EXISTS`` cannot add columns).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Optional, Sequence

import aiosqlite

import config

ISO = "%Y-%m-%dT%H:%M:%S"


class InsufficientFunds(Exception):
    """Raised when an atomic debit finds less than the requested amount."""

    def __init__(self, table: str, row_id: int, amount: int, balance: int) -> None:
        super().__init__(f"balance {balance} < {amount}")
        self.table = table
        self.row_id = row_id
        self.amount = amount
        self.balance = balance


class CapExceeded(Exception):
    """Raised when a credit would push an individual wallet over its cap.

    The transfer is refused instead of silently trimming the credit: trimming
    would destroy credits the sender already paid.
    """

    def __init__(self, balance: int, amount: int, room: int) -> None:
        super().__init__(
            f"individual cap reached: balance={balance} amount={amount} room={room}"
        )
        self.balance = balance
        self.amount = amount
        self.room = room


class AccountMissing(Exception):
    """Raised when a balance operation targets a deleted account."""

    def __init__(self, table: str, row_id: int) -> None:
        super().__init__(f"{table} row {row_id} no longer exists")
        self.table = table
        self.row_id = row_id


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(ISO)


def parse_iso(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, ISO).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS nations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    name              TEXT    NOT NULL,
    leader_id         INTEGER,
    happy_workers     INTEGER NOT NULL DEFAULT 0,
    regular_workers   INTEGER NOT NULL DEFAULT 0,
    happy_soldiers    INTEGER NOT NULL DEFAULT 0,
    regular_soldiers  INTEGER NOT NULL DEFAULT 0,
    balance           INTEGER NOT NULL DEFAULT 0,
    kingdom_id        INTEGER,
    created_at        TEXT    NOT NULL,
    UNIQUE (guild_id, name)
);

CREATE TABLE IF NOT EXISTS kingdoms (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    name              TEXT    NOT NULL,
    leader_id         INTEGER,
    balance           INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL,
    UNIQUE (guild_id, name)
);

CREATE TABLE IF NOT EXISTS transactions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER NOT NULL,
    ts                 TEXT    NOT NULL,
    kind               TEXT    NOT NULL,
    account_type       TEXT,
    account_id         INTEGER,
    account_name       TEXT,
    counterparty_type  TEXT,
    counterparty_id    INTEGER,
    counterparty_name  TEXT,
    amount             INTEGER NOT NULL DEFAULT 0,
    balance_after      INTEGER,
    actor_id           INTEGER,
    note               TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    guild_id                  INTEGER PRIMARY KEY,
    payout_channel_id         INTEGER,
    approval_channel_id       INTEGER,
    last_cycle_at             TEXT,
    next_cycle_at             TEXT,
    contract_board_channel_id INTEGER,
    union_commit_channel_id   INTEGER
);

CREATE TABLE IF NOT EXISTS pending_draws (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    kingdom_id    INTEGER NOT NULL,
    nation_id     INTEGER NOT NULL,
    amount        INTEGER NOT NULL,
    requested_by  INTEGER NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_creations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    kind          TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    leader_id     INTEGER,
    requested_by  INTEGER NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    channel_id    INTEGER,
    message_id    INTEGER,
    created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_guild   ON transactions (guild_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_tx_account ON transactions (account_type, account_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_nation_kg  ON nations (kingdom_id);
CREATE INDEX IF NOT EXISTS idx_creations  ON pending_creations (guild_id, status);
CREATE INDEX IF NOT EXISTS idx_draws      ON pending_draws (guild_id, status);

-- ---------------------------------------------------------------------------
-- Royal Shop: catalog, kits, orders & bans
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS catalog_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT    NOT NULL UNIQUE,
    display_name  TEXT    NOT NULL,
    category      TEXT    NOT NULL,
    price         INTEGER NOT NULL,
    unit_qty      INTEGER NOT NULL DEFAULT 1,
    stackable     INTEGER NOT NULL DEFAULT 1,
    max_stack     INTEGER NOT NULL DEFAULT 64,
    is_backported INTEGER NOT NULL DEFAULT 0,
    enchant_id    TEXT,
    enchant_level INTEGER,
    sort_order    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    creator_id  INTEGER NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'player',
    status      TEXT    NOT NULL DEFAULT 'active',
    channel_id  INTEGER,
    message_id  INTEGER,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS kit_items (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kit_id    INTEGER NOT NULL,
    item_key  TEXT    NOT NULL,
    quantity  INTEGER NOT NULL,
    FOREIGN KEY (kit_id) REFERENCES kits (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    redeem_key     TEXT    NOT NULL UNIQUE,
    buyer_id       INTEGER NOT NULL,
    treasury_type  TEXT    NOT NULL,
    treasury_id    INTEGER NOT NULL,
    items_total    INTEGER NOT NULL,
    shulker_fee    INTEGER NOT NULL DEFAULT 0,
    discount       INTEGER NOT NULL DEFAULT 0,
    union_discount INTEGER NOT NULL DEFAULT 0,
    final_total    INTEGER NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'pending',
    created_at     TEXT    NOT NULL,
    redeemed_at    TEXT,
    redeemed_uuid  TEXT
);

CREATE TABLE IF NOT EXISTS order_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL,
    item_key     TEXT    NOT NULL,
    display_name TEXT    NOT NULL,
    quantity     INTEGER NOT NULL,
    unit_price   INTEGER NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS banned_items (
    key        TEXT PRIMARY KEY,
    reason     TEXT,
    banned_by  INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kit_cooldowns (
    guild_id         INTEGER NOT NULL,
    discord_id       INTEGER NOT NULL,
    last_created_at  TEXT NOT NULL,
    PRIMARY KEY (guild_id, discord_id)
);

CREATE INDEX IF NOT EXISTS idx_catalog_cat  ON catalog_items (category);
CREATE INDEX IF NOT EXISTS idx_kits_guild   ON kits (guild_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_guild ON orders (guild_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_key   ON orders (redeem_key);
CREATE INDEX IF NOT EXISTS idx_kit_items    ON kit_items (kit_id);

-- ---------------------------------------------------------------------------
-- Workers' Union: contracts, natural-cycle log & case counters
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contracts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    case_id           TEXT    NOT NULL UNIQUE,
    nation_id         INTEGER NOT NULL,
    nation_name       TEXT    NOT NULL,
    nation_tag        TEXT    NOT NULL,
    owner_type        TEXT    NOT NULL DEFAULT 'nation',
    owner_id          INTEGER,
    requester_id      INTEGER NOT NULL,
    thread_id         INTEGER,
    claimed_by        INTEGER,
    claimed_at        TEXT,
    board_channel_id  INTEGER,
    board_message_id  INTEGER,
    claim_account_type TEXT,
    claim_account_id  INTEGER,
    upfront_claimed   INTEGER NOT NULL DEFAULT 0,
    final_claimed     INTEGER NOT NULL DEFAULT 0,
    status            TEXT    NOT NULL DEFAULT 'negotiating',
    cycle             INTEGER NOT NULL,
    budget            INTEGER NOT NULL,
    upfront           INTEGER NOT NULL DEFAULT 0,
    escrow            INTEGER NOT NULL DEFAULT 0,
    released          INTEGER NOT NULL DEFAULT 0,
    redeem_key        TEXT,
    final_key         TEXT,
    upfront_redeemed  INTEGER NOT NULL DEFAULT 0,
    final_redeemed    INTEGER NOT NULL DEFAULT 0,
    project           TEXT,
    history           TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    approved_at       TEXT,
    closed_at         TEXT,
    completed_at      TEXT
);

CREATE TABLE IF NOT EXISTS contract_cycles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    cycle       INTEGER NOT NULL,
    source      TEXT    NOT NULL,
    recorded_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS case_counters (
    guild_id   INTEGER NOT NULL,
    nation_tag TEXT    NOT NULL,
    last_id    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, nation_tag)
);

-- ``contracts.case_id`` is globally UNIQUE, but counters used to be per guild.
-- Two guilds with a nation of the same tag both generated UN-AURUM-001 and the
-- second insert failed. This counter is global per tag; the per-guild table
-- above is kept only as a migration seed (its largest value raises the floor).
CREATE TABLE IF NOT EXISTS case_counters_global (
    nation_tag TEXT PRIMARY KEY,
    last_id    INTEGER NOT NULL DEFAULT 0
);

-- Case numbers are allocated per faction prefix, so the first Crown case for a
-- tag is CR-<TAG>-001 even when the Union already holds UN-<TAG>-001..NNN. The
-- shared ``case_counters_global`` row stays in sync for UN allocations and is a
-- rollback mirror; CR allocations use only this table and existing CR case IDs.
CREATE TABLE IF NOT EXISTS case_counters_prefix (
    prefix     TEXT    NOT NULL,
    nation_tag TEXT    NOT NULL,
    last_id    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (prefix, nation_tag)
);

-- ---------------------------------------------------------------------------
-- Workers' Union treasury & membership
-- ---------------------------------------------------------------------------
-- The Union is the only source of contract credits. Its balance grows from
-- admin funding (/union fund), donations and member dues, and shrinks only
-- when a tranche is claimed.
CREATE TABLE IF NOT EXISTS union_account (
    guild_id INTEGER PRIMARY KEY,
    balance  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS union_members (
    guild_id     INTEGER NOT NULL,
    account_type TEXT    NOT NULL,
    account_id   INTEGER NOT NULL,
    joined_at    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, account_type, account_id)
);

-- Standing commitment to donate the whole balance to the Union after every
-- credit. A row starts 'pending' and only an admin approval makes it 'active'.
CREATE TABLE IF NOT EXISTS union_commits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    account_type  TEXT    NOT NULL,
    account_id    INTEGER NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    requested_by  INTEGER,
    requested_at  TEXT    NOT NULL,
    decided_by    INTEGER,
    decided_at    TEXT,
    channel_id    INTEGER,
    message_id    INTEGER,
    UNIQUE (guild_id, account_type, account_id)
);

CREATE INDEX IF NOT EXISTS idx_union_commits_status ON union_commits (guild_id, status);

-- ---------------------------------------------------------------------------
-- Factions: one allegiance per nation/kingdom
-- ---------------------------------------------------------------------------
-- The Workers' Union and The Crown are mutually exclusive. A single table with
-- a primary key on the account enforces that at the database level; the legacy
-- union_members table is migrated into this one and kept as a mirror for a
-- rollback to an older build.
CREATE TABLE IF NOT EXISTS faction_members (
    guild_id     INTEGER NOT NULL,
    account_type TEXT    NOT NULL CHECK (account_type IN ('nation', 'kingdom')),
    account_id   INTEGER NOT NULL,
    faction      TEXT    NOT NULL CHECK (faction IN ('union', 'crown')),
    status       TEXT    NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'ineligible')),
    joined_at    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, account_type, account_id)
);

CREATE INDEX IF NOT EXISTS idx_faction_members
    ON faction_members (guild_id, faction, status);

-- The Crown house account. It receives the Crown's regular-soldier tax and
-- credit donations, and pays supplier contracts. Tax/supplier/donation rows
-- are all logged, so the balance reconciles with the ledger.
CREATE TABLE IF NOT EXISTS crown_account (
    guild_id INTEGER PRIMARY KEY,
    balance  INTEGER NOT NULL DEFAULT 0
);

-- A Crown supplier contract purchases one specific catalog item. The value is
-- locked at signing from the current demand; delivery is recorded by
-- /faction deliver and payment is made only when the quantity is complete.
CREATE TABLE IF NOT EXISTS crown_contracts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    account_type   TEXT    NOT NULL CHECK (account_type IN ('nation', 'kingdom')),
    account_id     INTEGER NOT NULL,
    item_key       TEXT    NOT NULL,
    display_name   TEXT    NOT NULL,
    quantity       INTEGER NOT NULL,
    delivered      INTEGER NOT NULL DEFAULT 0,
    unit_price     INTEGER NOT NULL,
    demand_bp      INTEGER NOT NULL,
    value          INTEGER NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'fulfilled', 'cancelled', 'expired')),
    cycle          INTEGER NOT NULL,
    deadline_cycle INTEGER NOT NULL,
    created_at     TEXT    NOT NULL,
    fulfilled_at   TEXT,
    note           TEXT
);

CREATE INDEX IF NOT EXISTS idx_crown_contracts_account
    ON crown_contracts (guild_id, account_type, account_id, status);
CREATE INDEX IF NOT EXISTS idx_crown_contracts_status
    ON crown_contracts (guild_id, status);

-- Per-account, per-item supplier history. ``demand_bp`` starts at 10,000 and
-- falls once per natural cycle in which a contract for that item is fulfilled.
CREATE TABLE IF NOT EXISTS crown_sales (
    guild_id             INTEGER NOT NULL,
    account_type         TEXT    NOT NULL CHECK (account_type IN ('nation', 'kingdom')),
    account_id           INTEGER NOT NULL,
    item_key             TEXT    NOT NULL,
    units_sold           INTEGER NOT NULL DEFAULT 0,
    demand_bp            INTEGER NOT NULL DEFAULT 10000,
    last_cycle           INTEGER,
    last_unit_price      INTEGER,
    supplier_until_cycle INTEGER,
    updated_at           TEXT    NOT NULL,
    PRIMARY KEY (guild_id, account_type, account_id, item_key)
);

-- Donation history for both factions: credits and Crown item deliveries.
CREATE TABLE IF NOT EXISTS faction_donations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    account_type TEXT    NOT NULL CHECK (account_type IN ('nation', 'kingdom')),
    account_id   INTEGER NOT NULL,
    faction      TEXT    NOT NULL CHECK (faction IN ('union', 'crown')),
    kind         TEXT    NOT NULL,
    item_key     TEXT,
    quantity     INTEGER NOT NULL DEFAULT 0,
    amount       INTEGER NOT NULL DEFAULT 0,
    cycle        INTEGER,
    actor_id     INTEGER,
    note         TEXT,
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_faction_donations_account
    ON faction_donations (guild_id, account_type, account_id, id DESC);

-- Persistent Albert conversation memory. State lives in the tables above; this
-- only keeps the last exchanges so the NPC remembers what was said.
CREATE TABLE IF NOT EXISTS albert_memory (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    account_type TEXT    NOT NULL CHECK (account_type IN ('nation', 'kingdom')),
    account_id   INTEGER NOT NULL,
    npc          TEXT    NOT NULL DEFAULT 'albert',
    speaker      TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_albert_memory_account
    ON albert_memory (guild_id, account_type, account_id, id DESC);

-- ---------------------------------------------------------------------------
-- Individuals: one personal wallet per Discord member per guild
-- ---------------------------------------------------------------------------
-- Open to every member, capped at config.INDIVIDUAL_MAX_CREDITS. The daily
-- claim state (streak, multiplier in basis points, last claim timestamp) lives
-- on the same row so a claim is a single guarded UPDATE. Deleting the row plus
-- its transactions is the "erase my data" trigger.
CREATE TABLE IF NOT EXISTS individuals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    discord_id    INTEGER NOT NULL,
    display_name  TEXT,
    balance       INTEGER NOT NULL DEFAULT 0,
    daily_streak  INTEGER NOT NULL DEFAULT 0,
    daily_bp      INTEGER NOT NULL DEFAULT 10000,
    last_daily_at TEXT,
    created_at    TEXT    NOT NULL,
    UNIQUE (guild_id, discord_id)
);

CREATE INDEX IF NOT EXISTS idx_individuals_balance ON individuals (guild_id, balance DESC);

-- ---------------------------------------------------------------------------
-- The Casino: the house account every wager settles against
-- ---------------------------------------------------------------------------
-- Gambling is zero-sum against this balance rather than against the void: a
-- losing bet credits the Casino, a winning bet is paid out of it. That is what
-- keeps the games from inflating the economy - no bet can ever create credits
-- that another account did not lose. The only faucet is an admin funding it
-- with /casino fund, and a wager is refused when the house cannot cover the
-- potential payout.
CREATE TABLE IF NOT EXISTS casino_account (
    guild_id INTEGER PRIMARY KEY,
    balance  INTEGER NOT NULL DEFAULT 0
);

-- The Fishing Guild house account. /fish pays its catch out of this balance
-- instead of minting it, the same zero-sum rule as the Casino. The only taps
-- are the admin /fishingguild fund and /fishingguild withdraw commands.
CREATE TABLE IF NOT EXISTS fishing_guild_account (
    guild_id INTEGER PRIMARY KEY,
    balance  INTEGER NOT NULL DEFAULT 0
);

-- Hands in progress. Blackjack is the only multi-step wager: the stake is held
-- while the player decides, and the buttons live in memory, so a restart would
-- otherwise leave the stake with the house and no result. The row lets startup
-- find those hands and refund them (see cogs/games.py).
CREATE TABLE IF NOT EXISTS blackjack_hands (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    discord_id   INTEGER NOT NULL,
    individual_id INTEGER NOT NULL,
    stake        INTEGER NOT NULL,
    player_cards TEXT    NOT NULL,
    dealer_cards TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'open',
    created_at   TEXT    NOT NULL,
    settled_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_blackjack_status ON blackjack_hands (status);

-- Poker tables in progress. The whole hand lives in memory, but the buy-in is
-- real money held by the Casino. A restart cannot rebuild the table, so this
-- row lets startup find the table and return the stake instead of stranding it
-- (same safety rule as blackjack_hands).
CREATE TABLE IF NOT EXISTS poker_hands (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    discord_id   INTEGER NOT NULL,
    individual_id INTEGER NOT NULL,
    stake        INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'open',
    created_at   TEXT    NOT NULL,
    settled_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_poker_status ON poker_hands (status);

CREATE INDEX IF NOT EXISTS idx_contracts_guild  ON contracts (guild_id, status);
CREATE INDEX IF NOT EXISTS idx_contracts_nation ON contracts (nation_id, cycle);
CREATE INDEX IF NOT EXISTS idx_contracts_thread ON contracts (thread_id);
CREATE INDEX IF NOT EXISTS idx_cycles_guild     ON contract_cycles (guild_id, cycle);
"""

# Columns that older databases may be missing. (table, column, ddl-type)
_MIGRATIONS = [
    ("settings", "approval_channel_id", "INTEGER"),
    ("pending_creations", "channel_id", "INTEGER"),
    ("pending_creations", "message_id", "INTEGER"),
    ("settings", "kit_channel_id", "INTEGER"),
    ("settings", "contract_channel_id", "INTEGER"),
    ("settings", "union_admin_role_id", "INTEGER"),
    ("settings", "contract_cycle", "INTEGER NOT NULL DEFAULT 1"),
    ("settings", "contract_board_channel_id", "INTEGER"),
    ("settings", "union_commit_channel_id", "INTEGER"),
    ("contracts", "owner_type", "TEXT NOT NULL DEFAULT 'nation'"),
    ("contracts", "owner_id", "INTEGER"),
    ("contracts", "claimed_by", "INTEGER"),
    ("contracts", "claimed_at", "TEXT"),
    ("contracts", "board_channel_id", "INTEGER"),
    ("contracts", "board_message_id", "INTEGER"),
    ("contracts", "claim_account_type", "TEXT"),
    ("contracts", "claim_account_id", "INTEGER"),
    ("contracts", "upfront_claimed", "INTEGER NOT NULL DEFAULT 0"),
    ("contracts", "final_claimed", "INTEGER NOT NULL DEFAULT 0"),
    ("orders", "union_discount", "INTEGER NOT NULL DEFAULT 0"),
    ("albert_memory", "npc", "TEXT NOT NULL DEFAULT 'albert'"),
    # Contracts are funded by the faction they were requested from. Every row
    # that predates this column was a Workers' Union case, hence the default.
    ("contracts", "faction", "TEXT NOT NULL DEFAULT 'union'"),
]


class Database:
    """Thin async wrapper around aiosqlite."""

    def __init__(self, path: str = "royal_bank.db") -> None:
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None
        # Depth and owner are per asyncio task, not per process. A process-wide
        # counter let one task's ``async with db.transaction()`` join another
        # task's open transaction, so its work was committed (or rolled back)
        # with a stranger's block instead of its own. The owner check also
        # stops a child task from inheriting its parent's depth and joining a
        # transaction it does not own.
        self._tx_depth: ContextVar[int] = ContextVar(f"tx_depth:{id(self)}", default=0)
        self._tx_owner: ContextVar[Optional[asyncio.Task]] = ContextVar(
            f"tx_owner:{id(self)}", default=None
        )
        self._tx_lock: Optional[asyncio.Lock] = None

    @property
    def _in_tx(self) -> bool:
        return (
            self._tx_depth.get() > 0
            and self._tx_owner.get() is asyncio.current_task()
        )

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        # isolation_level=None => autocommit; we manage transactions explicitly.
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        self._tx_lock = asyncio.Lock()
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await self.conn.execute("PRAGMA journal_mode = WAL")
        await self.conn.execute("PRAGMA busy_timeout = 5000")
        await self.conn.execute("PRAGMA synchronous = NORMAL")
        await self.conn.executescript(SCHEMA)
        await self.migrate()
        await self.conn.commit()

    async def migrate(self) -> None:
        """Add any columns missing from databases created by older versions."""
        assert self.conn is not None
        added: set[tuple[str, str]] = set()
        for table, column, decl in _MIGRATIONS:
            cur = await self.conn.execute(f"PRAGMA table_info({table})")
            cols = {row[1] for row in await cur.fetchall()}
            await cur.close()
            if column not in cols:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                added.add((table, column))

        # Existing contracts predate owner tracking, so backfill their owner
        # from the nation columns before owner-scoped cycle limits are used.
        await self.conn.execute(
            "UPDATE contracts SET owner_id = nation_id WHERE owner_id IS NULL"
        )

        # Existing funded contracts were paid to their requester before the
        # claim board existed. Record that once, on migration, so a reboot
        # cannot hand the same tranche to a second person.
        if ("contracts", "claimed_by") in added:
            await self.conn.execute(
                """UPDATE contracts
                   SET claimed_by = requester_id,
                       claimed_at = COALESCE(approved_at, updated_at)
                   WHERE approved_at IS NOT NULL AND claimed_by IS NULL"""
            )

        # Union membership predates the faction table. Seed one faction row per
        # existing member; INSERT OR IGNORE keeps a Crown row authoritative if
        # one somehow already exists. union_members stays as a rollback mirror.
        await self.conn.execute(
            """INSERT OR IGNORE INTO faction_members
               (guild_id, account_type, account_id, faction, status, joined_at)
               SELECT guild_id, account_type, account_id, 'union', 'active', joined_at
               FROM union_members"""
        )
        # The Crown no longer has a soldier eligibility gate; old rows marked
        # ineligible become active; the soldier count is informational.
        await self.conn.execute(
            "UPDATE faction_members SET status = 'active' WHERE faction = 'crown'"
        )

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    # -- transactions ------------------------------------------------------
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Run a block of statements atomically (BEGIN IMMEDIATE … COMMIT).

        aiosqlite serialises statements on one connection, but two coroutines
        can still interleave *between* statements, and a second ``BEGIN``
        while a transaction is open fails with "cannot start a transaction
        within a transaction". The lock serialises whole transactions; the
        per-task depth counter lets a nested ``async with db.transaction()`` in
        the *same* task join the outer transaction instead of starting a second
        one, while a different task waits for the lock and gets its own.
        """
        assert self.conn is not None, "Database not connected"
        assert self._tx_lock is not None, "Database not connected"
        depth = self._tx_depth.get()
        owner = self._tx_owner.get()
        current = asyncio.current_task()
        nested = depth > 0 and owner is current
        outermost = not nested
        if outermost:
            await self._tx_lock.acquire()
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
            except BaseException:
                self._tx_lock.release()
                raise
            self._tx_depth.set(1)
            self._tx_owner.set(current)
        else:
            self._tx_depth.set(depth + 1)
        try:
            yield
        except BaseException:
            self._tx_depth.set(depth)
            self._tx_owner.set(owner)
            if outermost:
                try:
                    await self.conn.rollback()
                finally:
                    self._tx_lock.release()
            raise
        else:
            self._tx_depth.set(depth)
            self._tx_owner.set(owner)
            if outermost:
                try:
                    await self.conn.commit()
                finally:
                    self._tx_lock.release()

    # -- low level helpers -------------------------------------------------
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Cursor:
        """Run one statement.

        Statements outside a transaction take the transaction lock and commit
        immediately. Without that lock a plain write from task B would call
        ``conn.commit()`` while task A had ``BEGIN IMMEDIATE`` open, committing
        task A's partial money movement before A could roll it back.
        """
        assert self.conn is not None, "Database not connected"
        if self._in_tx:
            return await self.conn.execute(sql, params)
        assert self._tx_lock is not None, "Database not connected"
        async with self._tx_lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[aiosqlite.Row]:
        assert self.conn is not None, "Database not connected"
        if self._in_tx:
            cur = await self.conn.execute(sql, params)
            row = await cur.fetchone()
            await cur.close()
            return row
        assert self._tx_lock is not None, "Database not connected"
        async with self._tx_lock:
            cur = await self.conn.execute(sql, params)
            row = await cur.fetchone()
            await cur.close()
            return row

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        assert self.conn is not None, "Database not connected"
        if self._in_tx:
            cur = await self.conn.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
            return list(rows)
        assert self._tx_lock is not None, "Database not connected"
        async with self._tx_lock:
            cur = await self.conn.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
            return list(rows)

    # -- nations -----------------------------------------------------------
    async def create_nation(self, guild_id: int, name: str, leader_id: Optional[int]) -> int:
        cur = await self.execute(
            "INSERT INTO nations (guild_id, name, leader_id, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, name, leader_id, now_iso()),
        )
        return cur.lastrowid

    async def get_nation(self, guild_id: int, name: str) -> Optional[aiosqlite.Row]:
        return await self.fetchone(
            "SELECT * FROM nations WHERE guild_id = ? AND name = ? COLLATE NOCASE",
            (guild_id, name),
        )

    async def get_nation_by_id(self, nation_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM nations WHERE id = ?", (nation_id,))

    async def list_nations(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM nations WHERE guild_id = ? ORDER BY name COLLATE NOCASE",
            (guild_id,),
        )

    async def nations_in_kingdom(self, kingdom_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM nations WHERE kingdom_id = ? ORDER BY name COLLATE NOCASE",
            (kingdom_id,),
        )

    async def kingdom_member_counts(self, guild_id: int) -> dict[int, int]:
        """Return {kingdom_id: member_count} in a single query (avoids N+1)."""
        rows = await self.fetchall(
            """SELECT kingdom_id, COUNT(*) AS c FROM nations
               WHERE guild_id = ? AND kingdom_id IS NOT NULL
               GROUP BY kingdom_id""",
            (guild_id,),
        )
        return {int(r["kingdom_id"]): int(r["c"]) for r in rows}

    async def update_nation_stats(
        self,
        nation_id: int,
        happy_workers: int,
        regular_workers: int,
        happy_soldiers: int,
        regular_soldiers: int,
    ) -> None:
        await self.execute(
            """UPDATE nations
               SET happy_workers = ?, regular_workers = ?, happy_soldiers = ?, regular_soldiers = ?
               WHERE id = ?""",
            (happy_workers, regular_workers, happy_soldiers, regular_soldiers, nation_id),
        )

    async def set_nation_leader(self, nation_id: int, leader_id: Optional[int]) -> None:
        await self.execute("UPDATE nations SET leader_id = ? WHERE id = ?", (leader_id, nation_id))

    async def set_nation_kingdom(self, nation_id: int, kingdom_id: Optional[int]) -> None:
        await self.execute("UPDATE nations SET kingdom_id = ? WHERE id = ?", (kingdom_id, nation_id))

    async def delete_nation(self, nation_id: int) -> None:
        async with self.transaction():
            await self.execute(
                "DELETE FROM union_members WHERE account_type = 'nation' AND account_id = ?",
                (nation_id,),
            )
            await self.execute(
                "DELETE FROM faction_members WHERE account_type = 'nation' AND account_id = ?",
                (nation_id,),
            )
            await self.execute(
                "UPDATE crown_contracts SET status = 'cancelled'"
                " WHERE account_type = 'nation' AND account_id = ? AND status = 'active'",
                (nation_id,),
            )
            await self.execute(
                "DELETE FROM union_commits WHERE account_type = 'nation' AND account_id = ?",
                (nation_id,),
            )
            await self.execute("DELETE FROM nations WHERE id = ?", (nation_id,))

    # -- kingdoms ----------------------------------------------------------
    async def create_kingdom(self, guild_id: int, name: str, leader_id: Optional[int]) -> int:
        cur = await self.execute(
            "INSERT INTO kingdoms (guild_id, name, leader_id, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, name, leader_id, now_iso()),
        )
        return cur.lastrowid

    async def get_kingdom(self, guild_id: int, name: str) -> Optional[aiosqlite.Row]:
        return await self.fetchone(
            "SELECT * FROM kingdoms WHERE guild_id = ? AND name = ? COLLATE NOCASE",
            (guild_id, name),
        )

    async def get_kingdom_by_id(self, kingdom_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM kingdoms WHERE id = ?", (kingdom_id,))

    async def list_kingdoms(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM kingdoms WHERE guild_id = ? ORDER BY name COLLATE NOCASE",
            (guild_id,),
        )

    async def set_kingdom_leader(self, kingdom_id: int, leader_id: Optional[int]) -> None:
        await self.execute("UPDATE kingdoms SET leader_id = ? WHERE id = ?", (leader_id, kingdom_id))

    async def delete_kingdom(self, kingdom_id: int) -> None:
        async with self.transaction():
            await self.execute("UPDATE nations SET kingdom_id = NULL WHERE kingdom_id = ?", (kingdom_id,))
            await self.execute(
                "DELETE FROM union_members WHERE account_type = 'kingdom' AND account_id = ?",
                (kingdom_id,),
            )
            await self.execute(
                "DELETE FROM faction_members WHERE account_type = 'kingdom' AND account_id = ?",
                (kingdom_id,),
            )
            await self.execute(
                "UPDATE crown_contracts SET status = 'cancelled'"
                " WHERE account_type = 'kingdom' AND account_id = ? AND status = 'active'",
                (kingdom_id,),
            )
            await self.execute(
                "DELETE FROM union_commits WHERE account_type = 'kingdom' AND account_id = ?",
                (kingdom_id,),
            )
            await self.execute("DELETE FROM kingdoms WHERE id = ?", (kingdom_id,))

    async def delete_account(
        self,
        account_type: str,
        account_id: int,
        *,
        actor_id: Optional[int] = None,
        guild_id: Optional[int] = None,
        note: str = "",
    ) -> int:
        """Delete a nation/kingdom and log any balance burned with it.

        A dissolve is a real balance change: the row and its remaining credits
        disappear together. The delete and a negative ledger row share one
        transaction, so the books stay reconciled and the burned amount is
        explicit. ``guild_id`` is checked when supplied, so a stale or forged id
        cannot remove another server's treasury. Returns the burned balance (0
        when the row was already gone or belongs to another guild).
        """
        assert account_type in ("nation", "kingdom")
        table = "nations" if account_type == "nation" else "kingdoms"
        async with self.transaction():
            row = await self.fetchone(f"SELECT * FROM {table} WHERE id = ?", (account_id,))
            if row is None:
                return 0
            if guild_id is not None and int(row["guild_id"]) != int(guild_id):
                return 0
            balance = int(row["balance"])
            if account_type == "nation":
                await self.delete_nation(account_id)
            else:
                await self.delete_kingdom(account_id)
            await self.log_tx(
                guild_id=int(row["guild_id"]),
                kind=f"{account_type}_delete",
                account_type=account_type,
                account_id=account_id,
                account_name=row["name"],
                amount=-balance,
                balance_after=0,
                actor_id=actor_id,
                note=note or (
                    f"Disbanded; {balance:,} credits burned"
                    if balance
                    else "Disbanded"
                ),
            )
        return balance

    async def rename_account(self, account_type: str, account_id: int, new_name: str) -> None:
        """Rename a nation or kingdom without changing its id or balance.

        Open contracts store the account name as a snapshot, so they are
        refreshed in the same transaction. Closed cases keep the name they were
        argued under; ``transactions`` rows are history and are never rewritten.
        """
        assert account_type in ("nation", "kingdom")
        table = "nations" if account_type == "nation" else "kingdoms"
        async with self.transaction():
            await self.execute(
                f"UPDATE {table} SET name = ? WHERE id = ?", (new_name, account_id)
            )
            await self.execute(
                """UPDATE contracts SET nation_name = ?
                   WHERE owner_type = ? AND owner_id = ?
                     AND status IN ('negotiating', 'approved')""",
                (new_name, account_type, account_id),
            )

    # -- balances ----------------------------------------------------------
    async def set_balance(self, table: str, row_id: int, amount: int) -> None:
        assert table in ("nations", "kingdoms")
        await self.execute(f"UPDATE {table} SET balance = ? WHERE id = ?", (amount, row_id))

    async def add_balance(self, table: str, row_id: int, delta: int) -> int:
        assert table in ("nations", "kingdoms")
        cur = await self.execute(
            f"UPDATE {table} SET balance = balance + ? WHERE id = ?", (delta, row_id)
        )
        if cur.rowcount != 1:
            raise AccountMissing(table, row_id)
        row = await self.fetchone(f"SELECT balance FROM {table} WHERE id = ?", (row_id,))
        if row is None:
            raise AccountMissing(table, row_id)
        return int(row["balance"])

    async def debit(self, table: str, row_id: int, amount: int) -> int:
        """Subtract ``amount`` only if the balance covers it. Returns the new balance.

        The guard lives in the UPDATE itself, so two concurrent purchases can
        never both pass a stale check and push the balance negative.
        Raises :class:`InsufficientFunds` when the account cannot pay.
        """
        assert table in ("nations", "kingdoms")
        assert amount >= 0
        cur = await self.execute(
            f"UPDATE {table} SET balance = balance - ? WHERE id = ? AND balance >= ?",
            (amount, row_id, amount),
        )
        row = await self.fetchone(f"SELECT balance FROM {table} WHERE id = ?", (row_id,))
        balance = int(row["balance"]) if row else 0
        if cur.rowcount != 1:
            raise InsufficientFunds(table, row_id, amount, balance)
        return balance

    async def transfer_balance(
        self,
        src_table: str,
        src_id: int,
        dst_table: str,
        dst_id: int,
        amount: int,
    ) -> tuple[int, int]:
        """Atomically move ``amount`` between two accounts.

        Returns ``(source_balance_after, destination_balance_after)``. The whole
        operation runs inside a single transaction so it is all-or-nothing, and
        the source debit is conditional so the source can never go negative.
        Raises :class:`InsufficientFunds` if the source cannot cover ``amount``.
        """
        assert src_table in ("nations", "kingdoms") and dst_table in ("nations", "kingdoms")
        async with self.transaction():
            await self.debit(src_table, src_id, amount)
            cur = await self.execute(
                f"UPDATE {dst_table} SET balance = balance + ? WHERE id = ?", (amount, dst_id)
            )
            if cur.rowcount != 1:
                raise AccountMissing(dst_table, dst_id)
            s_row = await self.fetchone(f"SELECT balance FROM {src_table} WHERE id = ?", (src_id,))
            d_row = await self.fetchone(f"SELECT balance FROM {dst_table} WHERE id = ?", (dst_id,))
        return (
            int(s_row["balance"]) if s_row else 0,
            int(d_row["balance"]) if d_row else 0,
        )

    # -- transactions ------------------------------------------------------
    async def log_tx(
        self,
        *,
        guild_id: int,
        kind: str,
        account_type: Optional[str] = None,
        account_id: Optional[int] = None,
        account_name: Optional[str] = None,
        counterparty_type: Optional[str] = None,
        counterparty_id: Optional[int] = None,
        counterparty_name: Optional[str] = None,
        amount: int = 0,
        balance_after: Optional[int] = None,
        actor_id: Optional[int] = None,
        note: Optional[str] = None,
    ) -> int:
        cur = await self.execute(
            """INSERT INTO transactions
               (guild_id, ts, kind, account_type, account_id, account_name,
                counterparty_type, counterparty_id, counterparty_name,
                amount, balance_after, actor_id, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                guild_id, now_iso(), kind, account_type, account_id, account_name,
                counterparty_type, counterparty_id, counterparty_name,
                amount, balance_after, actor_id, note,
            ),
        )
        return cur.lastrowid

    async def recent_transactions(
        self, guild_id: int, limit: int = 15,
        account_type: Optional[str] = None, account_id: Optional[int] = None,
    ) -> list[aiosqlite.Row]:
        if account_type and account_id is not None:
            return await self.fetchall(
                """SELECT * FROM transactions
                   WHERE guild_id = ? AND account_type = ? AND account_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (guild_id, account_type, account_id, limit),
            )
        return await self.fetchall(
            "SELECT * FROM transactions WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        )

    async def all_transactions(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM transactions WHERE guild_id = ? ORDER BY id ASC", (guild_id,)
        )

    # -- settings ----------------------------------------------------------
    async def get_settings(self, guild_id: Optional[int]) -> Optional[aiosqlite.Row]:
        # A DM has no guild. Without this guard the INSERT below creates a ghost
        # row whose auto-assigned rowid is returned by nothing, because
        # ``guild_id = NULL`` never matches. Every settings caller is inside a
        # guild, so ``None`` is simply "no settings".
        if guild_id is None:
            return None
        row = await self.fetchone("SELECT * FROM settings WHERE guild_id = ?", (guild_id,))
        if row is None:
            # INSERT OR IGNORE avoids a race where two calls try to insert at once.
            await self.execute("INSERT OR IGNORE INTO settings (guild_id) VALUES (?)", (guild_id,))
            row = await self.fetchone("SELECT * FROM settings WHERE guild_id = ?", (guild_id,))
        return row

    async def set_payout_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        await self.get_settings(guild_id)
        await self.execute(
            "UPDATE settings SET payout_channel_id = ? WHERE guild_id = ?", (channel_id, guild_id)
        )

    async def set_approval_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        await self.get_settings(guild_id)
        await self.execute(
            "UPDATE settings SET approval_channel_id = ? WHERE guild_id = ?", (channel_id, guild_id)
        )

    async def set_kit_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        await self.get_settings(guild_id)
        await self.execute(
            "UPDATE settings SET kit_channel_id = ? WHERE guild_id = ?", (channel_id, guild_id)
        )

    async def set_union_commit_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        await self.get_settings(guild_id)
        await self.execute(
            "UPDATE settings SET union_commit_channel_id = ? WHERE guild_id = ?",
            (channel_id, guild_id),
        )

    async def set_cycle_times(self, guild_id: int, last: Optional[str], nxt: Optional[str]) -> None:
        await self.get_settings(guild_id)
        await self.execute(
            "UPDATE settings SET last_cycle_at = ?, next_cycle_at = ? WHERE guild_id = ?",
            (last, nxt, guild_id),
        )

    async def all_settings(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM settings")

    # -- pending draws -----------------------------------------------------
    async def create_pending_draw(
        self, guild_id: int, kingdom_id: int, nation_id: int,
        amount: int, requested_by: int,
    ) -> int:
        cur = await self.execute(
            """INSERT INTO pending_draws
               (guild_id, kingdom_id, nation_id, amount, requested_by, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (guild_id, kingdom_id, nation_id, amount, requested_by, now_iso()),
        )
        return cur.lastrowid

    async def get_pending_draw(self, draw_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM pending_draws WHERE id = ?", (draw_id,))

    async def set_draw_status(self, draw_id: int, status: str) -> None:
        await self.execute("UPDATE pending_draws SET status = ? WHERE id = ?", (status, draw_id))

    async def claim_pending_draw(self, draw_id: int, status: str) -> bool:
        """Atomically move a draw out of ``pending``. Returns True if this call did it.

        Used before any money moves so two simultaneous approvals cannot both
        pass a "is it pending?" check and pay the same request twice.
        """
        cur = await self.execute(
            "UPDATE pending_draws SET status = ? WHERE id = ? AND status = 'pending'",
            (status, draw_id),
        )
        return cur.rowcount == 1

    async def pending_draws_for_kingdom(self, kingdom_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM pending_draws WHERE kingdom_id = ? AND status = 'pending'",
            (kingdom_id,),
        )

    async def all_pending_draws(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM pending_draws WHERE status = 'pending'")

    # -- pending creations -------------------------------------------------
    async def create_pending_creation(
        self, guild_id: int, kind: str, name: str, leader_id: Optional[int], requested_by: int
    ) -> int:
        cur = await self.execute(
            """INSERT INTO pending_creations
               (guild_id, kind, name, leader_id, requested_by, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (guild_id, kind, name, leader_id, requested_by, now_iso()),
        )
        return cur.lastrowid

    async def get_pending_creation(self, creation_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM pending_creations WHERE id = ?", (creation_id,))

    async def set_creation_status(self, creation_id: int, status: str) -> None:
        await self.execute("UPDATE pending_creations SET status = ? WHERE id = ?", (status, creation_id))

    async def claim_pending_creation(self, creation_id: int, status: str) -> bool:
        """Resolve a petition atomically. False when another click already did.

        The account, its bonus and this status flip must share one transaction;
        the ``AND status = 'pending'`` guard is what makes a one-shot decision
        one-shot even if two administrators click at the same moment.
        """
        cur = await self.execute(
            "UPDATE pending_creations SET status = ? WHERE id = ? AND status = 'pending'",
            (status, creation_id),
        )
        return cur.rowcount == 1

    async def set_creation_message(self, creation_id: int, channel_id: int, message_id: int) -> None:
        await self.execute(
            "UPDATE pending_creations SET channel_id = ?, message_id = ? WHERE id = ?",
            (channel_id, message_id, creation_id),
        )

    async def all_pending_creations(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM pending_creations WHERE status = 'pending'")

    async def pending_creations_for_guild(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM pending_creations WHERE guild_id = ? AND status = 'pending' ORDER BY id ASC",
            (guild_id,),
        )

    # ------------------------------------------------------------------
    # Royal Shop: catalog
    # ------------------------------------------------------------------
    async def catalog_count(self) -> int:
        row = await self.fetchone("SELECT COUNT(*) AS c FROM catalog_items")
        return int(row["c"]) if row else 0

    async def seed_catalog(self, items: Sequence[dict]) -> None:
        """Insert catalog rows that are not already present (idempotent)."""
        for it in items:
            await self.execute(
                """INSERT OR IGNORE INTO catalog_items
                   (key, display_name, category, price, unit_qty, stackable, max_stack,
                    is_backported, enchant_id, enchant_level, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    it["key"], it["display_name"], it["category"], it["price"],
                    it.get("unit_qty", 1), it.get("stackable", 1), it.get("max_stack", 64),
                    it.get("is_backported", 0), it.get("enchant_id"), it.get("enchant_level"),
                    it.get("sort_order", 0),
                ),
            )

    async def get_catalog_item(self, key: str) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM catalog_items WHERE key = ?", (key,))

    async def get_catalog_item_by_id(self, item_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM catalog_items WHERE id = ?", (item_id,))

    async def list_catalog(self, category: Optional[str] = None) -> list[aiosqlite.Row]:
        if category:
            return await self.fetchall(
                "SELECT * FROM catalog_items WHERE category = ? ORDER BY sort_order, display_name COLLATE NOCASE",
                (category,),
            )
        return await self.fetchall(
            "SELECT * FROM catalog_items ORDER BY sort_order, category, display_name COLLATE NOCASE"
        )

    async def list_catalog_categories(self) -> list[str]:
        rows = await self.fetchall("SELECT DISTINCT category FROM catalog_items ORDER BY category")
        return [r["category"] for r in rows]

    async def set_catalog_price(self, key: str, price: int) -> None:
        await self.execute("UPDATE catalog_items SET price = ? WHERE key = ?", (price, key))

    async def add_catalog_item(self, it: dict) -> None:
        await self.execute(
            """INSERT INTO catalog_items
               (key, display_name, category, price, unit_qty, stackable, max_stack,
                is_backported, enchant_id, enchant_level, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 display_name=excluded.display_name, category=excluded.category,
                 price=excluded.price, unit_qty=excluded.unit_qty,
                 stackable=excluded.stackable, max_stack=excluded.max_stack,
                 is_backported=excluded.is_backported, enchant_id=excluded.enchant_id,
                 enchant_level=excluded.enchant_level""",
            (
                it["key"], it["display_name"], it["category"], it["price"],
                it.get("unit_qty", 1), it.get("stackable", 1), it.get("max_stack", 64),
                it.get("is_backported", 0), it.get("enchant_id"), it.get("enchant_level"),
                it.get("sort_order", 0),
            ),
        )

    # -- bans -----------------------------------------------------------
    async def ban_item(self, key: str, reason: Optional[str], banned_by: int) -> None:
        await self.execute(
            """INSERT INTO banned_items (key, reason, banned_by, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 reason=excluded.reason, banned_by=excluded.banned_by, created_at=excluded.created_at""",
            (key, reason, banned_by, now_iso()),
        )

    async def unban_item(self, key: str) -> None:
        await self.execute("DELETE FROM banned_items WHERE key = ?", (key,))

    async def banned_keys(self) -> set[str]:
        rows = await self.fetchall("SELECT key FROM banned_items")
        return {r["key"] for r in rows}

    # ------------------------------------------------------------------
    # Kits
    # ------------------------------------------------------------------
    async def create_kit(self, guild_id: int, name: str, creator_id: int, kind: str = "player") -> int:
        cur = await self.execute(
            "INSERT INTO kits (guild_id, name, creator_id, kind, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, name, creator_id, kind, now_iso()),
        )
        return cur.lastrowid

    async def add_kit_item(self, kit_id: int, item_key: str, quantity: int) -> None:
        await self.execute(
            "INSERT INTO kit_items (kit_id, item_key, quantity) VALUES (?, ?, ?)",
            (kit_id, item_key, quantity),
        )

    async def get_kit(self, kit_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM kits WHERE id = ?", (kit_id,))

    async def get_kit_items(self, kit_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM kit_items WHERE kit_id = ? ORDER BY id", (kit_id,))

    async def list_kits(self, guild_id: int, kind: Optional[str] = None, status: str = "active") -> list[aiosqlite.Row]:
        if kind:
            return await self.fetchall(
                "SELECT * FROM kits WHERE guild_id = ? AND kind = ? AND status = ? ORDER BY id DESC",
                (guild_id, kind, status),
            )
        return await self.fetchall(
            "SELECT * FROM kits WHERE guild_id = ? AND status = ? ORDER BY id DESC",
            (guild_id, status),
        )

    async def set_kit_message(self, kit_id: int, channel_id: int, message_id: int) -> None:
        await self.execute(
            "UPDATE kits SET channel_id = ?, message_id = ? WHERE id = ?",
            (channel_id, message_id, kit_id),
        )

    async def set_kit_status(self, kit_id: int, status: str) -> None:
        await self.execute("UPDATE kits SET status = ? WHERE id = ?", (status, kit_id))

    # -- kit cooldowns ---------------------------------------------------
    async def get_kit_cooldown(self, guild_id: int, discord_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone(
            "SELECT * FROM kit_cooldowns WHERE guild_id = ? AND discord_id = ?",
            (guild_id, discord_id),
        )

    async def set_kit_cooldown(self, guild_id: int, discord_id: int, ts: str) -> None:
        await self.execute(
            """INSERT INTO kit_cooldowns (guild_id, discord_id, last_created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id, discord_id) DO UPDATE SET last_created_at=excluded.last_created_at""",
            (guild_id, discord_id, ts),
        )

    async def claim_kit_cooldown(
        self, guild_id: int, discord_id: int, ts: str, ready_at: str
    ) -> bool:
        """Take the kit slot atomically for this window.

        ``ready_at`` is the ISO cutoff: a row older than it may be replaced.
        Returns False when another submission already consumed the window, so
        two simultaneous modals cannot both create a kit during one cooldown.
        """
        cur = await self.execute(
            """INSERT INTO kit_cooldowns (guild_id, discord_id, last_created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id, discord_id) DO UPDATE
                   SET last_created_at = excluded.last_created_at
                   WHERE kit_cooldowns.last_created_at <= ?""",
            (guild_id, discord_id, ts, ready_at),
        )
        return cur.rowcount == 1

    async def reset_kit_cooldown(self, guild_id: int, discord_id: int) -> None:
        await self.execute(
            "DELETE FROM kit_cooldowns WHERE guild_id = ? AND discord_id = ?",
            (guild_id, discord_id),
        )

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    async def create_order(
        self, *, guild_id: int, redeem_key: str, buyer_id: int,
        treasury_type: str, treasury_id: int, items_total: int,
        shulker_fee: int, discount: int, final_total: int,
        union_discount: int = 0,
    ) -> int:
        cur = await self.execute(
            """INSERT INTO orders
               (guild_id, redeem_key, buyer_id, treasury_type, treasury_id,
                items_total, shulker_fee, discount, union_discount, final_total,
                status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (guild_id, redeem_key, buyer_id, treasury_type, treasury_id,
             items_total, shulker_fee, discount, union_discount, final_total, now_iso()),
        )
        return cur.lastrowid

    async def add_order_item(self, order_id: int, item_key: str, display_name: str, quantity: int, unit_price: int) -> None:
        await self.execute(
            "INSERT INTO order_items (order_id, item_key, display_name, quantity, unit_price) VALUES (?, ?, ?, ?, ?)",
            (order_id, item_key, display_name, quantity, unit_price),
        )

    async def get_order_by_key(self, redeem_key: str) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM orders WHERE redeem_key = ?", (redeem_key,))

    async def get_order(self, order_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))

    async def get_order_items(self, order_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,))

    async def mark_order_redeemed(self, order_id: int, uuid: str) -> None:
        await self.execute(
            "UPDATE orders SET status = 'redeemed', redeemed_at = ?, redeemed_uuid = ? WHERE id = ?",
            (now_iso(), uuid, order_id),
        )

    async def redeem_order(self, order_id: int, uuid: str) -> bool:
        """Atomically redeem a *pending* order. Returns True if this call did it.

        The ``AND status = 'pending'`` guard makes double-redemption impossible
        even if two redeem requests race.
        """
        cur = await self.execute(
            """UPDATE orders
               SET status = 'redeemed', redeemed_at = ?, redeemed_uuid = ?
               WHERE id = ? AND status = 'pending'""",
            (now_iso(), uuid, order_id),
        )
        return cur.rowcount == 1

    async def list_orders(self, guild_id: int, buyer_id: Optional[int] = None) -> list[aiosqlite.Row]:
        if buyer_id is not None:
            return await self.fetchall(
                "SELECT * FROM orders WHERE guild_id = ? AND buyer_id = ? ORDER BY id DESC",
                (guild_id, buyer_id),
            )
        return await self.fetchall("SELECT * FROM orders WHERE guild_id = ? ORDER BY id DESC", (guild_id,))

    # ------------------------------------------------------------------
    # Workers' Union: settings
    # ------------------------------------------------------------------
    async def set_contract_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        await self.get_settings(guild_id)
        await self.execute(
            "UPDATE settings SET contract_channel_id = ? WHERE guild_id = ?", (channel_id, guild_id)
        )

    async def set_union_admin_role(self, guild_id: int, role_id: Optional[int]) -> None:
        await self.get_settings(guild_id)
        await self.execute(
            "UPDATE settings SET union_admin_role_id = ? WHERE guild_id = ?", (role_id, guild_id)
        )

    async def set_contract_board_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        await self.get_settings(guild_id)
        await self.execute(
            "UPDATE settings SET contract_board_channel_id = ? WHERE guild_id = ?",
            (channel_id, guild_id),
        )

    # ------------------------------------------------------------------
    # Workers' Union: natural payout cycles
    # ------------------------------------------------------------------
    async def current_contract_cycle(self, guild_id: int) -> int:
        settings = await self.get_settings(guild_id)
        return int(settings["contract_cycle"] or 1)

    async def advance_contract_cycle(self, guild_id: int, *, natural: bool) -> int:
        """Record a payout run. Only natural cycles advance contract eligibility."""
        settings = await self.get_settings(guild_id)
        cycle = int(settings["contract_cycle"] or 1)
        if natural:
            cycle += 1
            await self.execute(
                "UPDATE settings SET contract_cycle = ? WHERE guild_id = ?", (cycle, guild_id)
            )
        await self.execute(
            "INSERT INTO contract_cycles (guild_id, cycle, source, recorded_at) VALUES (?, ?, ?, ?)",
            (guild_id, cycle, "natural" if natural else "manual", now_iso()),
        )
        return cycle

    async def contract_cycle_history(self, guild_id: int, limit: int = 20) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM contract_cycles WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        )

    # ------------------------------------------------------------------
    # Workers' Union: case IDs
    # ------------------------------------------------------------------
    async def next_case_number(
        self, guild_id: int, nation_tag: str, prefix: str = "UN"
    ) -> int:
        """Reserve a globally unique case number for a tag and faction prefix.

        ``contracts.case_id`` is globally UNIQUE while the old counter was per
        ``(guild_id, nation_tag)``. Two guilds with a nation of the same tag
        therefore both generated ``UN-AURUM-001`` and the second insert failed.
        The counter below is reserved inside one transaction, so concurrent
        requests (in one guild or across guilds) cannot collide.

        ``prefix`` is the case prefix (``UN`` for the Workers' Union, ``CR`` for
        The Crown). Numbers are allocated **per prefix**, so the first Crown
        case for a tag is ``CR-<TAG>-001`` even when the Union already holds
        ``UN-<TAG>-001..NNN``. The old shared ``case_counters_global`` row is
        read as a floor and kept in sync for UN allocations, so existing Union
        numbering continues where it left off.

        ``guild_id`` is kept in the signature for callers and for the legacy
        per-guild counter seed; uniqueness no longer depends on it.
        """
        prefix = (prefix or "UN").strip().upper()
        full_prefix = f"{prefix}-{nation_tag}-"
        like = full_prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        async with self.transaction():
            await self.execute(
                """INSERT OR IGNORE INTO case_counters_prefix
                   (prefix, nation_tag, last_id) VALUES (?, ?, 0)""",
                (prefix, nation_tag),
            )
            used = await self.fetchone(
                """SELECT COALESCE(MAX(CAST(substr(case_id, ?) AS INTEGER)), 0) AS n
                   FROM contracts WHERE case_id LIKE ? ESCAPE '!'""",
                (len(full_prefix) + 1, like),
            )
            current = await self.fetchone(
                """SELECT last_id FROM case_counters_prefix
                   WHERE prefix = ? AND nation_tag = ?""",
                (prefix, nation_tag),
            )
            floor = max(
                int(used["n"]) if used else 0,
                int(current["last_id"]) if current else 0,
            )
            if prefix == "UN":
                # Continue the legacy numbering: the per-guild seed and the old
                # shared global row were the authority before this table.
                legacy = await self.fetchone(
                    "SELECT COALESCE(MAX(last_id), 0) AS n FROM case_counters WHERE nation_tag = ?",
                    (nation_tag,),
                )
                shared = await self.fetchone(
                    "SELECT last_id FROM case_counters_global WHERE nation_tag = ?",
                    (nation_tag,),
                )
                floor = max(
                    floor,
                    int(legacy["n"]) if legacy else 0,
                    int(shared["last_id"]) if shared else 0,
                )
            number = floor + 1
            await self.execute(
                """UPDATE case_counters_prefix SET last_id = ?
                   WHERE prefix = ? AND nation_tag = ?""",
                (number, prefix, nation_tag),
            )
            if prefix == "UN":
                # Rollback mirror only; CR allocations never touch this table.
                await self.execute(
                    "INSERT OR IGNORE INTO case_counters_global (nation_tag, last_id) VALUES (?, 0)",
                    (nation_tag,),
                )
                await self.execute(
                    "UPDATE case_counters_global SET last_id = ? WHERE nation_tag = ?",
                    (number, nation_tag),
                )
        return number

    # ------------------------------------------------------------------
    # Workers' Union: contracts
    # ------------------------------------------------------------------
    _CONTRACT_FIELDS = {
        "status", "thread_id", "budget", "upfront", "escrow", "released",
        "redeem_key", "final_key", "upfront_redeemed", "final_redeemed",
        "project", "history", "approved_at", "closed_at", "completed_at",
        "claim_account_type", "claim_account_id", "upfront_claimed", "final_claimed",
        "board_channel_id", "board_message_id", "claimed_by", "claimed_at",
    }

    async def create_contract(
        self,
        *,
        guild_id: int,
        case_id: str,
        nation_id: int,
        nation_name: str,
        nation_tag: str,
        requester_id: int,
        cycle: int,
        budget: int,
        proposal: Optional[str] = None,
        project: Optional[str] = None,  # legacy keyword, kept for one deploy window
        owner_type: str = "nation",
        owner_id: Optional[int] = None,
        quota_limit: Optional[int] = None,
        faction: str = "union",
    ) -> int:
        """Insert a negotiating contract, optionally enforcing the cycle quota.

        ``quota_limit`` is checked **inside** the insert transaction. The command
        also checks before opening a Discord thread for a friendly message, but
        two concurrent requests could both pass that check; this guard is what
        makes the limit atomic. Returns ``0`` when the quota is full.

        ``proposal`` is the free text the representative reads. It is stored in
        the legacy ``contracts.project`` column, which predates the rename and
        is kept so existing rows and backups stay readable.

        ``faction`` records whose treasury funds the case (``union`` or
        ``crown``). The quota is per faction, so a Crown case never uses up a
        Workers' Union slot.
        """
        now = now_iso()
        if proposal is None:
            proposal = project
        if proposal is None:
            raise ValueError("create_contract requires a proposal")
        faction = (faction or "union").strip().lower()
        assert faction in ("union", "crown")
        if owner_id is None:
            owner_id = nation_id
        async with self.transaction():
            if quota_limit is not None:
                existing = await self.count_contracts_for_cycle(
                    guild_id, owner_type, owner_id, cycle, faction=faction
                )
                if existing >= int(quota_limit):
                    return 0
            cur = await self.execute(
                """INSERT INTO contracts
                   (guild_id, case_id, nation_id, nation_name, nation_tag, owner_type,
                    owner_id, requester_id, status, cycle, budget, project, history,
                    faction, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'negotiating', ?, ?, ?, '[]', ?, ?, ?)""",
                (guild_id, case_id, nation_id, nation_name, nation_tag, owner_type,
                 owner_id, requester_id, cycle, budget, proposal, faction, now, now),
            )
        return cur.lastrowid

    async def get_contract(self, contract_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM contracts WHERE id = ?", (contract_id,))

    async def get_contract_by_case(self, case_id: str) -> Optional[aiosqlite.Row]:
        return await self.fetchone(
            "SELECT * FROM contracts WHERE case_id = ? COLLATE NOCASE", (case_id,)
        )

    async def get_contract_by_thread(self, thread_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM contracts WHERE thread_id = ?", (thread_id,))

    async def get_contract_by_key(self, key: str) -> Optional[aiosqlite.Row]:
        return await self.fetchone(
            """SELECT * FROM contracts
               WHERE redeem_key = ? COLLATE NOCASE OR final_key = ? COLLATE NOCASE""",
            (key, key),
        )

    async def redeem_contract_key(self, contract_id: int, kind: str) -> bool:
        """Atomically mark an upfront/final key redeemed. Returns True on success."""
        if kind not in ("upfront", "final"):
            return False
        column = "upfront_redeemed" if kind == "upfront" else "final_redeemed"
        cur = await self.execute(
            f"UPDATE contracts SET {column} = 1, updated_at = ? WHERE id = ? AND {column} = 0",
            (now_iso(), contract_id),
        )
        return cur.rowcount == 1

    async def claim_contract_tranche(self, contract_id: int, kind: str) -> bool:
        """Atomically claim an upfront/final tranche. Returns True if this call did it."""
        if kind not in ("upfront", "final"):
            return False
        column = "upfront_claimed" if kind == "upfront" else "final_claimed"
        cur = await self.execute(
            f"UPDATE contracts SET {column} = 1, updated_at = ? WHERE id = ? AND {column} = 0",
            (now_iso(), contract_id),
        )
        return cur.rowcount == 1

    async def contracts_with_board_messages(self, guild_id: Optional[int] = None) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM contracts WHERE board_message_id IS NOT NULL"
        params: tuple = ()
        if guild_id is not None:
            sql += " AND guild_id = ?"
            params = (guild_id,)
        return await self.fetchall(sql + " ORDER BY id ASC", params)

    async def set_contract_thread(self, contract_id: int, thread_id: int) -> None:
        await self.execute(
            "UPDATE contracts SET thread_id = ?, updated_at = ? WHERE id = ?",
            (thread_id, now_iso(), contract_id),
        )

    async def update_contract(self, contract_id: int, **fields) -> None:
        updates = {k: v for k, v in fields.items() if k in self._CONTRACT_FIELDS}
        if not updates:
            return
        updates["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in updates)
        await self.execute(
            f"UPDATE contracts SET {cols} WHERE id = ?",
            (*updates.values(), contract_id),
        )

    async def set_contract_status(self, contract_id: int, status: str, **fields) -> None:
        await self.update_contract(contract_id, status=status, **fields)

    async def approve_contract(self, contract_id: int, **fields) -> bool:
        """Fund a negotiating contract exactly once. False if someone else did.

        ``approve`` moves no credits, but it decides who gets paid later, so the
        status flip is guarded like a money move: two admins clicking Approve
        at once must produce one funding event, not two board posts and DMs.
        """
        updates = {k: v for k, v in fields.items() if k in self._CONTRACT_FIELDS}
        if not updates:
            return False
        updates["status"] = "approved"
        updates["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in updates)
        cur = await self.execute(
            f"""UPDATE contracts SET {cols}
                WHERE id = ? AND status = 'negotiating' AND approved_at IS NULL""",
            (*updates.values(), contract_id),
        )
        return cur.rowcount == 1

    async def transition_contract(
        self,
        contract_id: int,
        *,
        from_statuses: Sequence[str],
        to_status: str,
        **fields,
    ) -> bool:
        """Move a contract between statuses only if it is still in one of them.

        Release, reject, close and reopen are one-shot decisions. Reading the
        row first and updating it unconditionally lets two admins (or a reject
        racing a close) overwrite each other; the ``status IN (...)`` guard is
        what makes the transition atomic. Returns False when the contract was
        already moved by someone else.
        """
        statuses = [s for s in from_statuses if s]
        if not statuses:
            return False
        updates = {k: v for k, v in fields.items() if k in self._CONTRACT_FIELDS}
        updates["status"] = to_status
        updates["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in updates)
        placeholders = ", ".join("?" for _ in statuses)
        cur = await self.execute(
            f"""UPDATE contracts SET {cols}
                WHERE id = ? AND status IN ({placeholders})""",
            (*updates.values(), contract_id, *statuses),
        )
        return cur.rowcount == 1

    async def list_contracts(
        self,
        guild_id: int,
        status: Optional[str] = None,
        requester_id: Optional[int] = None,
        faction: Optional[str] = None,
        owner_type: Optional[str] = None,
        owner_id: Optional[int] = None,
    ) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM contracts WHERE guild_id = ?"
        params: list = [guild_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if requester_id is not None:
            sql += " AND requester_id = ?"
            params.append(requester_id)
        if faction:
            sql += " AND faction = ?"
            params.append(faction)
        if owner_type:
            sql += " AND owner_type = ?"
            params.append(owner_type)
        if owner_id is not None:
            sql += " AND COALESCE(owner_id, nation_id) = ?"
            params.append(owner_id)
        sql += " ORDER BY id DESC"
        return await self.fetchall(sql, tuple(params))

    async def count_contracts_for_cycle(
        self, guild_id: int, owner_type: str, owner_id: int, cycle: int,
        faction: Optional[str] = None,
    ) -> int:
        sql = """SELECT COUNT(*) AS c FROM contracts
               WHERE guild_id = ? AND owner_type = ? AND COALESCE(owner_id, nation_id) = ?
                 AND cycle = ? AND status NOT IN ('cancelled', 'rejected')"""
        params: list = [guild_id, owner_type, owner_id, cycle]
        if faction:
            sql += " AND faction = ?"
            params.append(faction)
        row = await self.fetchone(sql, tuple(params))
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------------
    # Workers' Union: treasury
    # ------------------------------------------------------------------
    async def get_union_balance(self, guild_id: int) -> int:
        """Current Union treasury for a guild (creates the row on first read)."""
        row = await self.fetchone(
            "SELECT balance FROM union_account WHERE guild_id = ?", (guild_id,)
        )
        if row is None:
            await self.execute(
                "INSERT OR IGNORE INTO union_account (guild_id) VALUES (?)", (guild_id,)
            )
            row = await self.fetchone(
                "SELECT balance FROM union_account WHERE guild_id = ?", (guild_id,)
            )
        return int(row["balance"]) if row else 0

    async def credit_union(self, guild_id: int, amount: int) -> int:
        """Add credits to the Union treasury. Returns the new balance."""
        assert amount >= 0
        await self.get_union_balance(guild_id)
        await self.execute(
            "UPDATE union_account SET balance = balance + ? WHERE guild_id = ?",
            (amount, guild_id),
        )
        row = await self.fetchone(
            "SELECT balance FROM union_account WHERE guild_id = ?", (guild_id,)
        )
        return int(row["balance"]) if row else 0

    async def debit_union(self, guild_id: int, amount: int) -> int:
        """Spend Union credits, refusing to go negative. Returns the new balance."""
        assert amount >= 0
        await self.get_union_balance(guild_id)
        cur = await self.execute(
            "UPDATE union_account SET balance = balance - ? WHERE guild_id = ? AND balance >= ?",
            (amount, guild_id, amount),
        )
        row = await self.fetchone(
            "SELECT balance FROM union_account WHERE guild_id = ?", (guild_id,)
        )
        balance = int(row["balance"]) if row else 0
        if cur.rowcount != 1:
            raise InsufficientFunds("union_account", guild_id, amount, balance)
        return balance

    # ------------------------------------------------------------------
    # The Crown: treasury
    # ------------------------------------------------------------------
    async def current_cycle(self, guild_id: int) -> int:
        """The natural payout cycle number, defaulting to 1 before settings exist."""
        row = await self.fetchone(
            "SELECT contract_cycle FROM settings WHERE guild_id = ?", (guild_id,)
        )
        return int(row["contract_cycle"]) if row else 1

    async def get_crown_balance(self, guild_id: int) -> int:
        """Current Crown treasury balance (creates the row on first read)."""
        row = await self.fetchone(
            "SELECT balance FROM crown_account WHERE guild_id = ?", (guild_id,)
        )
        if row is None:
            await self.execute(
                "INSERT OR IGNORE INTO crown_account (guild_id) VALUES (?)", (guild_id,)
            )
            row = await self.fetchone(
                "SELECT balance FROM crown_account WHERE guild_id = ?", (guild_id,)
            )
        return int(row["balance"]) if row else 0

    async def credit_crown(self, guild_id: int, amount: int) -> int:
        """Add credits to the Crown treasury. Returns the new balance."""
        assert amount >= 0
        await self.get_crown_balance(guild_id)
        await self.execute(
            "UPDATE crown_account SET balance = balance + ? WHERE guild_id = ?",
            (amount, guild_id),
        )
        row = await self.fetchone(
            "SELECT balance FROM crown_account WHERE guild_id = ?", (guild_id,)
        )
        return int(row["balance"]) if row else 0

    async def debit_crown(self, guild_id: int, amount: int) -> int:
        """Spend Crown credits, refusing to go negative. Returns the new balance."""
        assert amount >= 0
        await self.get_crown_balance(guild_id)
        cur = await self.execute(
            """UPDATE crown_account SET balance = balance - ?
               WHERE guild_id = ? AND balance >= ?""",
            (amount, guild_id, amount),
        )
        row = await self.fetchone(
            "SELECT balance FROM crown_account WHERE guild_id = ?", (guild_id,)
        )
        balance = int(row["balance"]) if row else 0
        if cur.rowcount != 1:
            raise InsufficientFunds("crown_account", guild_id, amount, balance)
        return balance

    async def get_crown_contract(self, contract_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone(
            "SELECT * FROM crown_contracts WHERE id = ?", (contract_id,)
        )

    async def list_crown_contracts(
        self,
        guild_id: int,
        *,
        account_type: Optional[str] = None,
        account_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM crown_contracts WHERE guild_id = ?"
        params: list[Any] = [guild_id]
        if account_type is not None:
            sql += " AND account_type = ?"
            params.append(account_type)
        if account_id is not None:
            sql += " AND account_id = ?"
            params.append(account_id)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return await self.fetchall(sql, tuple(params))

    async def count_active_crown_contracts(
        self, guild_id: int, account_type: str, account_id: int
    ) -> int:
        row = await self.fetchone(
            """SELECT COUNT(*) AS c FROM crown_contracts
               WHERE guild_id = ? AND account_type = ? AND account_id = ?
                 AND status = 'active'""",
            (guild_id, account_type, account_id),
        )
        return int(row["c"]) if row else 0

    async def crown_reserved(self, guild_id: int) -> int:
        """Credits the Crown has already committed to active supplier contracts."""
        row = await self.fetchone(
            """SELECT COALESCE(SUM(value), 0) AS s FROM crown_contracts
               WHERE guild_id = ? AND status = 'active'""",
            (guild_id,),
        )
        return int(row["s"]) if row else 0

    async def get_crown_sale(
        self, guild_id: int, account_type: str, account_id: int, item_key: str
    ) -> Optional[aiosqlite.Row]:
        return await self.fetchone(
            """SELECT * FROM crown_sales
               WHERE guild_id = ? AND account_type = ? AND account_id = ?
                 AND item_key = ?""",
            (guild_id, account_type, account_id, item_key),
        )

    async def list_crown_sales(
        self, guild_id: int, account_type: str, account_id: int
    ) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """SELECT * FROM crown_sales
               WHERE guild_id = ? AND account_type = ? AND account_id = ?
               ORDER BY item_key""",
            (guild_id, account_type, account_id),
        )

    async def record_faction_donation(
        self,
        *,
        guild_id: int,
        account_type: str,
        account_id: int,
        faction: str,
        kind: str,
        item_key: Optional[str] = None,
        quantity: int = 0,
        amount: int = 0,
        cycle: Optional[int] = None,
        actor_id: Optional[int] = None,
        note: str = "",
    ) -> int:
        cur = await self.execute(
            """INSERT INTO faction_donations
               (guild_id, account_type, account_id, faction, kind, item_key,
                quantity, amount, cycle, actor_id, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                guild_id, account_type, account_id, faction, kind, item_key,
                int(quantity), int(amount), cycle, actor_id, note, now_iso(),
            ),
        )
        return cur.lastrowid

    async def list_faction_donations(
        self,
        guild_id: int,
        account_type: str,
        account_id: int,
        *,
        faction: Optional[str] = None,
        limit: int = 10,
    ) -> list[aiosqlite.Row]:
        sql = """SELECT * FROM faction_donations
                 WHERE guild_id = ? AND account_type = ? AND account_id = ?"""
        params: list[Any] = [guild_id, account_type, account_id]
        if faction is not None:
            sql += " AND faction = ?"
            params.append(faction)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return await self.fetchall(sql, tuple(params))

    async def append_albert_memory(
        self,
        *,
        guild_id: int,
        account_type: str,
        account_id: int,
        speaker: str,
        content: str,
        npc: str = "albert",
        keep: int = 20,
    ) -> None:
        async with self.transaction():
            await self.execute(
                """INSERT INTO albert_memory
                   (guild_id, account_type, account_id, npc, speaker, content, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    guild_id, account_type, account_id, npc, speaker,
                    content, now_iso(),
                ),
            )
            await self.execute(
                """DELETE FROM albert_memory
                   WHERE guild_id = ? AND account_type = ? AND account_id = ?
                     AND npc = ?
                     AND id NOT IN (
                         SELECT id FROM albert_memory
                         WHERE guild_id = ? AND account_type = ? AND account_id = ?
                           AND npc = ?
                         ORDER BY id DESC LIMIT ?
                     )""",
                (
                    guild_id, account_type, account_id, npc,
                    guild_id, account_type, account_id, npc, int(keep),
                ),
            )

    async def list_albert_memory(
        self,
        guild_id: int,
        account_type: str,
        account_id: int,
        *,
        npc: str = "albert",
        limit: int = 10,
    ) -> list[aiosqlite.Row]:
        rows = await self.fetchall(
            """SELECT * FROM albert_memory
               WHERE guild_id = ? AND account_type = ? AND account_id = ?
                 AND npc = ?
               ORDER BY id DESC LIMIT ?""",
            (guild_id, account_type, account_id, npc, int(limit)),
        )
        return list(reversed(rows))

    # ------------------------------------------------------------------
    # The Casino: house balance
    # ------------------------------------------------------------------
    async def get_casino_balance(self, guild_id: int) -> int:
        """Current Casino house balance for a guild (creates the row on first read)."""
        row = await self.fetchone(
            "SELECT balance FROM casino_account WHERE guild_id = ?", (guild_id,)
        )
        if row is None:
            await self.execute(
                "INSERT OR IGNORE INTO casino_account (guild_id) VALUES (?)", (guild_id,)
            )
            row = await self.fetchone(
                "SELECT balance FROM casino_account WHERE guild_id = ?", (guild_id,)
            )
        return int(row["balance"]) if row else 0

    async def credit_casino(self, guild_id: int, amount: int) -> int:
        """Add credits to the house. Returns the new balance."""
        assert amount >= 0
        await self.get_casino_balance(guild_id)
        await self.execute(
            "UPDATE casino_account SET balance = balance + ? WHERE guild_id = ?",
            (amount, guild_id),
        )
        row = await self.fetchone(
            "SELECT balance FROM casino_account WHERE guild_id = ?", (guild_id,)
        )
        return int(row["balance"]) if row else 0

    async def debit_casino(self, guild_id: int, amount: int) -> int:
        """Pay credits out of the house, refusing to go negative.

        The guard is in the UPDATE, so two concurrent payouts cannot both spend
        the same credits. Raises :class:`InsufficientFunds` when the house is
        short.
        """
        assert amount >= 0
        await self.get_casino_balance(guild_id)
        cur = await self.execute(
            """UPDATE casino_account SET balance = balance - ?
               WHERE guild_id = ? AND balance >= ?""",
            (amount, guild_id, amount),
        )
        row = await self.fetchone(
            "SELECT balance FROM casino_account WHERE guild_id = ?", (guild_id,)
        )
        balance = int(row["balance"]) if row else 0
        if cur.rowcount != 1:
            raise InsufficientFunds("casino_account", guild_id, amount, balance)
        return balance

    # ------------------------------------------------------------------
    # The Fishing Guild: the house account behind /fish
    # ------------------------------------------------------------------
    async def get_fishing_balance(self, guild_id: int) -> int:
        """Current Fishing Guild balance for a guild (creates the row on first read)."""
        row = await self.fetchone(
            "SELECT balance FROM fishing_guild_account WHERE guild_id = ?", (guild_id,)
        )
        if row is None:
            await self.execute(
                "INSERT OR IGNORE INTO fishing_guild_account (guild_id) VALUES (?)",
                (guild_id,),
            )
            row = await self.fetchone(
                "SELECT balance FROM fishing_guild_account WHERE guild_id = ?", (guild_id,)
            )
        return int(row["balance"]) if row else 0

    async def credit_fishing_guild(self, guild_id: int, amount: int) -> int:
        """Add credits to the Fishing Guild. Returns the new balance."""
        assert amount >= 0
        await self.get_fishing_balance(guild_id)
        await self.execute(
            "UPDATE fishing_guild_account SET balance = balance + ? WHERE guild_id = ?",
            (amount, guild_id),
        )
        row = await self.fetchone(
            "SELECT balance FROM fishing_guild_account WHERE guild_id = ?", (guild_id,)
        )
        return int(row["balance"]) if row else 0

    async def debit_fishing_guild(self, guild_id: int, amount: int) -> int:
        """Pay a catch out of the Fishing Guild, refusing to go negative.

        The guard lives in the UPDATE, so concurrent catches cannot spend the
        same credits. Raises :class:`InsufficientFunds` when the guild is short.
        """
        assert amount >= 0
        await self.get_fishing_balance(guild_id)
        cur = await self.execute(
            """UPDATE fishing_guild_account SET balance = balance - ?
               WHERE guild_id = ? AND balance >= ?""",
            (amount, guild_id, amount),
        )
        row = await self.fetchone(
            "SELECT balance FROM fishing_guild_account WHERE guild_id = ?", (guild_id,)
        )
        balance = int(row["balance"]) if row else 0
        if cur.rowcount != 1:
            raise InsufficientFunds("fishing_guild_account", guild_id, amount, balance)
        return balance

    # ------------------------------------------------------------------
    # The Casino: blackjack hands in progress
    # ------------------------------------------------------------------
    async def open_blackjack_hand(
        self, *, guild_id: int, discord_id: int, individual_id: int, stake: int,
        player_cards: str, dealer_cards: str,
    ) -> int:
        """Record a dealt hand so a restart can find and refund it."""
        cur = await self.execute(
            """INSERT INTO blackjack_hands
               (guild_id, discord_id, individual_id, stake, player_cards,
                dealer_cards, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'open', ?)""",
            (guild_id, discord_id, individual_id, stake, player_cards,
             dealer_cards, now_iso()),
        )
        return cur.lastrowid

    async def close_blackjack_hand(self, hand_id: int) -> bool:
        """Mark a hand settled once and only once.

        The guard makes the timeout and the button callback safe to race: the
        second caller sees no row and must not pay out again.
        """
        cur = await self.execute(
            """UPDATE blackjack_hands SET status = 'settled', settled_at = ?
               WHERE id = ? AND status = 'open'""",
            (now_iso(), hand_id),
        )
        return cur.rowcount == 1

    async def all_open_blackjack_hands(self) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM blackjack_hands WHERE status = 'open' ORDER BY id"
        )

    # ------------------------------------------------------------------
    # The Casino: poker tables in progress
    # ------------------------------------------------------------------
    async def open_poker_hand(
        self, *, guild_id: int, discord_id: int, individual_id: int, stake: int,
    ) -> int:
        """Record a poker buy-in so a restart can find and refund it."""
        cur = await self.execute(
            """INSERT INTO poker_hands
               (guild_id, discord_id, individual_id, stake, status, created_at)
               VALUES (?, ?, ?, ?, 'open', ?)""",
            (guild_id, discord_id, individual_id, stake, now_iso()),
        )
        return cur.lastrowid

    async def close_poker_hand(self, hand_id: int) -> bool:
        """Mark a poker table settled once and only once."""
        cur = await self.execute(
            """UPDATE poker_hands SET status = 'settled', settled_at = ?
               WHERE id = ? AND status = 'open'""",
            (now_iso(), hand_id),
        )
        return cur.rowcount == 1

    async def all_open_poker_hands(self) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM poker_hands WHERE status = 'open' ORDER BY id"
        )

    # ------------------------------------------------------------------
    # Faction membership (Union / Crown) and the Workers' Union view
    # ------------------------------------------------------------------
    async def get_faction_member(
        self, guild_id: int, account_type: str, account_id: int
    ) -> Optional[aiosqlite.Row]:
        return await self.fetchone(
            """SELECT * FROM faction_members
               WHERE guild_id = ? AND account_type = ? AND account_id = ?""",
            (guild_id, account_type, account_id),
        )

    async def list_faction_members(
        self,
        guild_id: int,
        *,
        faction: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM faction_members WHERE guild_id = ?"
        params: list[Any] = [guild_id]
        if faction is not None:
            sql += " AND faction = ?"
            params.append(faction)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY account_type, account_id"
        return await self.fetchall(sql, tuple(params))

    async def is_union_member(self, guild_id: int, account_type: str, account_id: int) -> bool:
        row = await self.fetchone(
            """SELECT 1 AS hit FROM faction_members
               WHERE guild_id = ? AND account_type = ? AND account_id = ?
                 AND faction = 'union' AND status = 'active'""",
            (guild_id, account_type, account_id),
        )
        return row is not None

    async def add_union_member(self, guild_id: int, account_type: str, account_id: int) -> None:
        """Low-level Union join. Prefer factions.join_faction() in commands, which
        also cancels the other faction's commitments and receipts."""
        stamp = now_iso()
        async with self.transaction():
            await self.execute(
                """UPDATE crown_contracts SET status = 'cancelled'
                   WHERE guild_id = ? AND account_type = ? AND account_id = ?
                     AND status = 'active'""",
                (guild_id, account_type, account_id),
            )
            await self.execute(
                """INSERT INTO faction_members
                   (guild_id, account_type, account_id, faction, status, joined_at)
                   VALUES (?, ?, ?, 'union', 'active', ?)
                   ON CONFLICT(guild_id, account_type, account_id) DO UPDATE SET
                       faction = 'union', status = 'active',
                       joined_at = excluded.joined_at""",
                (guild_id, account_type, account_id, stamp),
            )
            # Rollback mirror for an older build.
            await self.execute(
                """INSERT OR REPLACE INTO union_members
                   (guild_id, account_type, account_id, joined_at) VALUES (?, ?, ?, ?)""",
                (guild_id, account_type, account_id, stamp),
            )

    async def remove_union_member(self, guild_id: int, account_type: str, account_id: int) -> bool:
        async with self.transaction():
            cur = await self.execute(
                """DELETE FROM faction_members
                   WHERE guild_id = ? AND account_type = ? AND account_id = ?
                     AND faction = 'union'""",
                (guild_id, account_type, account_id),
            )
            await self.execute(
                """DELETE FROM union_members
                   WHERE guild_id = ? AND account_type = ? AND account_id = ?""",
                (guild_id, account_type, account_id),
            )
            # Leaving the faction ends any standing whole-balance commitment;
            # otherwise future credits would keep donating to a faction the
            # account no longer belongs to.
            await self.execute(
                """UPDATE union_commits SET status = 'cancelled'
                   WHERE guild_id = ? AND account_type = ? AND account_id = ?
                     AND status IN ('pending', 'active')""",
                (guild_id, account_type, account_id),
            )
        return cur.rowcount == 1

    async def list_union_members(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """SELECT account_type, account_id, joined_at FROM faction_members
               WHERE guild_id = ? AND faction = 'union' AND status = 'active'
               ORDER BY account_type, account_id""",
            (guild_id,),
        )

    async def union_contributions(
        self, guild_id: int, account_type: str, account_id: int
    ) -> dict[str, int]:
        """Credits this account has paid into the Union, by source."""
        rows = await self.fetchall(
            """SELECT kind, COALESCE(SUM(-amount), 0) AS total
               FROM transactions
               WHERE guild_id = ? AND account_type = ? AND account_id = ?
                 AND kind IN ('union_tax', 'union_donate', 'union_commit') AND amount < 0
               GROUP BY kind""",
            (guild_id, account_type, account_id),
        )
        totals = {row["kind"]: int(row["total"]) for row in rows}
        dues = totals.get("union_tax", 0)
        donations = totals.get("union_donate", 0)
        committed = totals.get("union_commit", 0)
        return {
            "dues": dues,
            "donations": donations,
            "committed": committed,
            "total": dues + donations + committed,
        }

    # ------------------------------------------------------------------
    # Workers' Union: standing commitment to donate the whole balance
    # ------------------------------------------------------------------
    async def get_union_commit(
        self, guild_id: int, account_type: str, account_id: int
    ) -> Optional[aiosqlite.Row]:
        return await self.fetchone(
            """SELECT * FROM union_commits
               WHERE guild_id = ? AND account_type = ? AND account_id = ?""",
            (guild_id, account_type, account_id),
        )

    async def get_union_commit_by_id(self, commit_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM union_commits WHERE id = ?", (commit_id,))

    async def is_union_committed(self, guild_id: int, account_type: str, account_id: int) -> bool:
        row = await self.get_union_commit(guild_id, account_type, account_id)
        return bool(row and row["status"] == "active")

    async def create_union_commit_request(
        self, guild_id: int, account_type: str, account_id: int, requested_by: Optional[int]
    ) -> int:
        """Create or reset the account's commit request to 'pending'."""
        await self.execute(
            """INSERT INTO union_commits
               (guild_id, account_type, account_id, status, requested_by, requested_at)
               VALUES (?, ?, ?, 'pending', ?, ?)
               ON CONFLICT (guild_id, account_type, account_id) DO UPDATE SET
                   status = 'pending',
                   requested_by = excluded.requested_by,
                   requested_at = excluded.requested_at,
                   decided_by = NULL,
                   decided_at = NULL,
                   channel_id = NULL,
                   message_id = NULL""",
            (guild_id, account_type, account_id, requested_by, now_iso()),
        )
        row = await self.get_union_commit(guild_id, account_type, account_id)
        return int(row["id"])

    async def set_union_commit_message(
        self, commit_id: int, channel_id: int, message_id: int
    ) -> None:
        await self.execute(
            "UPDATE union_commits SET channel_id = ?, message_id = ? WHERE id = ?",
            (channel_id, message_id, commit_id),
        )

    async def decide_union_commit(
        self, commit_id: int, status: str, decided_by: int
    ) -> bool:
        """Resolve a pending request atomically; False if it was already handled."""
        assert status in ("active", "denied")
        cur = await self.execute(
            """UPDATE union_commits
               SET status = ?, decided_by = ?, decided_at = ?
               WHERE id = ? AND status = 'pending'""",
            (status, decided_by, now_iso(), commit_id),
        )
        return cur.rowcount == 1

    async def cancel_union_commit(
        self, guild_id: int, account_type: str, account_id: int, actor_id: Optional[int]
    ) -> bool:
        """Stop a pending or active commitment. False if there was none."""
        cur = await self.execute(
            """UPDATE union_commits
               SET status = 'cancelled', decided_by = ?, decided_at = ?
               WHERE guild_id = ? AND account_type = ? AND account_id = ?
                 AND status IN ('pending', 'active')""",
            (actor_id, now_iso(), guild_id, account_type, account_id),
        )
        return cur.rowcount == 1

    async def all_pending_union_commits(self) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM union_commits WHERE status = 'pending' ORDER BY id"
        )

    async def count_active_union_commits(self, guild_id: int) -> int:
        row = await self.fetchone(
            """SELECT COUNT(*) AS n FROM union_commits
               WHERE guild_id = ? AND status = 'active'""",
            (guild_id,),
        )
        return int(row["n"]) if row else 0

    async def commit_balance_to_union(
        self, guild_id: int, account_type: str, account_id: int
    ) -> int:
        """Donate an active committer's whole balance to the Union.

        Must run inside the caller's transaction so the credit that triggered it,
        the donation and both ledger rows commit or roll back together. Returns
        the amount donated (0 when inactive, unknown or empty).
        """
        assert self._in_tx, "commit_balance_to_union must run inside a transaction"
        assert account_type in ("nation", "kingdom")
        if not await self.is_union_committed(guild_id, account_type, account_id):
            return 0
        table = "nations" if account_type == "nation" else "kingdoms"
        row = await self.fetchone(
            f"SELECT name, balance, guild_id FROM {table} WHERE id = ?", (account_id,)
        )
        if row is None or int(row["guild_id"]) != guild_id:
            return 0
        amount = int(row["balance"])
        if amount <= 0:
            return 0

        after = await self.debit(table, account_id, amount)
        union_after = await self.credit_union(guild_id, amount)
        await self.log_tx(
            guild_id=guild_id, kind="union_commit",
            account_type=account_type, account_id=account_id, account_name=row["name"],
            counterparty_type="union", counterparty_name="Workers' Union",
            amount=-amount, balance_after=after,
            note="Standing commitment: whole balance donated to the Union",
        )
        await self.log_tx(
            guild_id=guild_id, kind="union_commit",
            account_type="union", account_id=guild_id, account_name="Workers' Union",
            counterparty_type=account_type, counterparty_id=account_id,
            counterparty_name=row["name"],
            amount=amount, balance_after=union_after,
            note=f"Committed balance from {row['name']}",
        )
        return amount


    # ------------------------------------------------------------------
    # Individuals: one personal wallet per member
    # ------------------------------------------------------------------
    async def get_individual(
        self, guild_id: int, discord_id: int, display_name: Optional[str] = None
    ) -> aiosqlite.Row:
        """Return the member's wallet, creating it on first use.

        ``display_name`` is only a cached label for the leaderboard; it is
        refreshed whenever the caller supplies a newer one.
        """
        assert self.conn is not None, "Database not connected"
        row = await self.fetchone(
            "SELECT * FROM individuals WHERE guild_id = ? AND discord_id = ?",
            (guild_id, discord_id),
        )
        if row is None:
            await self.execute(
                """INSERT OR IGNORE INTO individuals
                   (guild_id, discord_id, display_name, created_at) VALUES (?, ?, ?, ?)""",
                (guild_id, discord_id, display_name, now_iso()),
            )
            row = await self.fetchone(
                "SELECT * FROM individuals WHERE guild_id = ? AND discord_id = ?",
                (guild_id, discord_id),
            )
        elif display_name and row["display_name"] != display_name:
            await self.execute(
                "UPDATE individuals SET display_name = ? WHERE id = ?", (display_name, row["id"])
            )
            row = await self.fetchone("SELECT * FROM individuals WHERE id = ?", (row["id"],))
        return row

    async def find_individual(
        self, guild_id: int, discord_id: int
    ) -> Optional[aiosqlite.Row]:
        """Read a wallet without creating one (viewing is not a signup)."""
        return await self.fetchone(
            "SELECT * FROM individuals WHERE guild_id = ? AND discord_id = ?",
            (guild_id, discord_id),
        )

    async def get_individual_by_id(self, individual_id: int) -> Optional[aiosqlite.Row]:
        return await self.fetchone("SELECT * FROM individuals WHERE id = ?", (individual_id,))

    async def list_individuals(self, guild_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """SELECT * FROM individuals WHERE guild_id = ?
               ORDER BY balance DESC, discord_id ASC""",
            (guild_id,),
        )

    async def credit_individual(
        self,
        guild_id: int,
        discord_id: int,
        amount: int,
        *,
        display_name: Optional[str] = None,
    ) -> tuple[int, int]:
        """Credit an individual, honouring the hard cap.

        Returns ``(added, balance_after)``. ``added`` is smaller than ``amount``
        when the cap is reached, and 0 when the wallet is already full: credits
        beyond the cap are never stored, not banked for later.
        """
        assert amount >= 0, "credit_individual takes a non-negative amount"
        async with self.transaction():
            row = await self.get_individual(guild_id, discord_id, display_name)
            before = int(row["balance"])
            allowed = max(0, min(int(amount), config.INDIVIDUAL_MAX_CREDITS - before))
            if allowed:
                await self.execute(
                    "UPDATE individuals SET balance = balance + ? WHERE id = ?",
                    (allowed, row["id"]),
                )
            after = before + allowed
        return allowed, after

    async def debit_individual(self, guild_id: int, discord_id: int, amount: int) -> int:
        """Spend from an individual wallet. The guard lives in the UPDATE."""
        assert amount >= 0
        async with self.transaction():
            # Spending must never create a wallet. ``get_individual`` would
            # insert a fresh zero row if the member deleted their wallet between
            # the caller's read and this debit, resurrecting erased data.
            row = await self.find_individual(guild_id, discord_id)
            if row is None:
                raise AccountMissing("individuals", discord_id)
            cur = await self.execute(
                "UPDATE individuals SET balance = balance - ? WHERE id = ? AND balance >= ?",
                (amount, row["id"], amount),
            )
            fresh = await self.fetchone(
                "SELECT balance FROM individuals WHERE id = ?", (row["id"],)
            )
            balance = int(fresh["balance"]) if fresh else 0
            if cur.rowcount != 1:
                raise InsufficientFunds("individuals", row["id"], amount, balance)
        return balance

    async def credit_individual_strict(
        self,
        guild_id: int,
        discord_id: int,
        amount: int,
        *,
        display_name: Optional[str] = None,
    ) -> tuple[int, int]:
        """Credit an individual, refusing (not trimming) when the cap would break.

        ``credit_individual`` trims the excess, which is right for a faucet but
        wrong for a transfer: trimming would take credits the sender has already
        been debited for and delete them. Transfers use this instead and surface
        :class:`CapExceeded` so the sender can retry with a smaller amount.
        """
        assert amount >= 0
        async with self.transaction():
            # A settled bet must not recreate a wallet the member erased while
            # the hand was open. If the row is gone, fail and let the caller's
            # transaction roll back (the hand stays open for the delete guard).
            row = await self.find_individual(guild_id, discord_id)
            if row is None:
                raise AccountMissing("individuals", discord_id)
            before = int(row["balance"])
            room = config.INDIVIDUAL_MAX_CREDITS - before
            if amount > room:
                raise CapExceeded(before, amount, max(0, room))
            await self.execute(
                "UPDATE individuals SET balance = balance + ? WHERE id = ?",
                (amount, row["id"]),
            )
            if display_name and row["display_name"] != display_name:
                await self.execute(
                    "UPDATE individuals SET display_name = ? WHERE id = ?",
                    (display_name, row["id"]),
                )
        return amount, before + amount

    async def tax_individual(
        self,
        guild_id: int,
        wallet,
        amount: int,
        *,
        to_union: bool,
        note: str = "",
    ) -> tuple[int, int]:
        """Charge one individual's net worth tax and record both ledger sides.

        ``wallet`` is the ``individuals`` row. The debit, the destination credit
        and the ledger rows share the caller's transaction, so the cycle cannot
        take tax without recording it. Returns ``(paid, balance_after)``; the tax
        is capped at the balance so a wallet can never go negative.
        """
        assert amount >= 0
        if amount <= 0:
            return 0, int(wallet["balance"])

        paid = min(int(amount), int(wallet["balance"]))
        if paid <= 0:
            return 0, int(wallet["balance"])

        after = await self.debit_individual(guild_id, wallet["discord_id"], paid)
        label = wallet["display_name"] or str(wallet["discord_id"])

        await self.log_tx(
            guild_id=guild_id,
            kind="individual_tax",
            account_type="individual",
            account_id=wallet["id"],
            account_name=label,
            counterparty_type="union" if to_union else None,
            counterparty_name="Workers' Union" if to_union else None,
            amount=-paid,
            balance_after=after,
            note=note or f"Net worth tax {paid}",
        )

        if to_union:
            union_after = await self.credit_union(guild_id, paid)
            await self.log_tx(
                guild_id=guild_id,
                kind="individual_tax",
                account_type="union",
                account_id=guild_id,
                account_name="Workers' Union",
                counterparty_type="individual",
                counterparty_id=wallet["id"],
                counterparty_name=label,
                amount=paid,
                balance_after=union_after,
                note=f"Net worth tax from {label}",
            )
        return paid, after

    async def claim_individual_daily(
        self,
        guild_id: int,
        discord_id: int,
        *,
        expected_last: Optional[str],
        new_last: str,
        streak: int,
        multiplier_bp: int,
    ) -> bool:
        """Advance the daily state once, guarded by the timestamp we read.

        The guard makes a double click harmless even if the caller is ever run
        outside a transaction: only the call that still sees ``expected_last``
        wins. Returns True when this call moved the state.
        """
        cur = await self.execute(
            """UPDATE individuals
               SET last_daily_at = ?, daily_streak = ?, daily_bp = ?
               WHERE guild_id = ? AND discord_id = ? AND last_daily_at IS ?""",
            (new_last, streak, multiplier_bp, guild_id, discord_id, expected_last),
        )
        return cur.rowcount == 1

    async def has_open_hand(self, guild_id: int, discord_id: int) -> bool:
        """Whether this member has a blackjack or poker hand in progress.

        Both games hold a stake in the Casino while the hand is decided. Erasing
        the wallet mid-hand used to let settlement recreate it under a new id,
        leaving the payout ledger row attached to the deleted wallet. Deletion
        is refused until the hand settles or a restart refunds it.
        """
        row = await self.fetchone(
            """SELECT 1 AS hit FROM blackjack_hands
               WHERE guild_id = ? AND discord_id = ? AND status = 'open'
               UNION ALL
               SELECT 1 AS hit FROM poker_hands
               WHERE guild_id = ? AND discord_id = ? AND status = 'open'
               LIMIT 1""",
            (guild_id, discord_id, guild_id, discord_id),
        )
        return row is not None

    async def delete_individual_checked(self, guild_id: int, discord_id: int) -> str:
        """Erase a wallet, reporting why it could not: deleted/missing/open.

        The open-hand check and both deletes share one transaction, so a hand
        cannot be opened between the guard and the erasure.
        """
        async with self.transaction():
            if await self.has_open_hand(guild_id, discord_id):
                return "open"
            row = await self.fetchone(
                "SELECT id FROM individuals WHERE guild_id = ? AND discord_id = ?",
                (guild_id, discord_id),
            )
            if row is None:
                return "missing"
            await self.execute(
                "DELETE FROM transactions WHERE account_type = 'individual' AND account_id = ?",
                (row["id"],),
            )
            await self.execute("DELETE FROM individuals WHERE id = ?", (row["id"],))
        return "deleted"

    async def delete_individual(self, guild_id: int, discord_id: int) -> bool:
        """Erase an individual's wallet and every ledger row it owns.

        This is the deliberate exception to the append-only ledger: the member
        asked for their stored data to be removed, so the credit history goes
        with the balance. Nothing else references an individual.

        Returns ``False`` when the row is missing **or** a hand is open; callers
        that need to tell those apart use :meth:`delete_individual_checked`.
        """
        return await self.delete_individual_checked(guild_id, discord_id) == "deleted"
