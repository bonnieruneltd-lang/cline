"""
shop/seed.py
============
Startup seeding for the Royal Shop. The catalog is re-seeded idempotently so
price/catalog edits in the DB are preserved while new entries flow in. Hard
bans are seeded only on first run so an admin's later unban is not clobbered.
"""
from __future__ import annotations

from shop.catalog_data import BANNED_KEYS, CATALOG


async def seed_shop(db) -> None:
    """Seed the catalog and (first run only) the hard-banned item list."""
    await db.seed_catalog(CATALOG)
    if not await db.banned_keys():
        for key in BANNED_KEYS:
            await db.ban_item(key, "Hard-banned by Royal Shop rules", 0)
