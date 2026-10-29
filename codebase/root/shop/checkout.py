"""
shop/checkout.py
================
Order creation pipeline shared by the shop and kit cogs. Validates bans,
prices the cart, deducts the treasury atomically, and writes the order.
"""
from __future__ import annotations

import math
import secrets

from database import InsufficientFunds
from shop import pricing
from utils import money


class ShopError(Exception):
    """A user-facing checkout failure."""


def generate_redeem_key() -> str:
    a = secrets.token_hex(3).upper()
    b = secrets.token_hex(3).upper()
    return f"RC-{a}-{b}"


async def _resolve_components(db, lines):
    """Expand ``(row, units)`` lines into concrete ``(key, display_name, qty, max_stack)``."""
    components: list[tuple[str, str, int, int]] = []
    for row, units in lines:
        for ckey, qty in pricing.expand_line(row["key"], units, row["unit_qty"]):
            comp = await db.get_catalog_item(ckey)
            max_stack = comp["max_stack"] if comp else 1
            display_name = comp["display_name"] if comp else row["display_name"]
            components.append((ckey, display_name, qty, max_stack))
    return components


def _insufficient_funds(treasury_type: str, name: str, balance: int, final_total: int) -> ShopError:
    label = "Nation" if treasury_type == "nation" else "Kingdom"
    return ShopError(
        f"Insufficient funds — {label} **{name}** holds "
        f"**{money(balance)}**, but this order costs **{money(final_total)}**."
    )


async def _place_order(
    db,
    *,
    guild_id: int,
    buyer_id: int,
    treasury_type: str,
    treasury_row,
    quote: dict,
    components: list[tuple[str, str, int, int]],
    kind: str,
    note: str,
):
    """Deduct the treasury and write the order atomically. Returns the order row."""
    assert treasury_type in ("nation", "kingdom")
    table = "nations" if treasury_type == "nation" else "kingdoms"
    redeem_key = generate_redeem_key()

    async with db.transaction():
        try:
            after = await db.debit(table, treasury_row["id"], quote["final_total"])
        except InsufficientFunds as exc:
            raise _insufficient_funds(
                treasury_type, treasury_row["name"], exc.balance, quote["final_total"]
            ) from exc
        order_id = await db.create_order(
            guild_id=guild_id,
            redeem_key=redeem_key,
            buyer_id=buyer_id,
            treasury_type=treasury_type,
            treasury_id=treasury_row["id"],
            items_total=quote["items_total"],
            shulker_fee=quote["shulker_fee"],
            discount=quote["discount"],
            union_discount=quote["union_discount"],
            final_total=quote["final_total"],
        )
        for ckey, display_name, qty, _ms in components:
            await db.add_order_item(order_id, ckey, display_name, qty, 0)
        await db.log_tx(
            guild_id=guild_id,
            kind=kind,
            account_type=treasury_type,
            account_id=treasury_row["id"],
            account_name=treasury_row["name"],
            amount=-quote["final_total"],
            balance_after=after,
            actor_id=buyer_id,
            note=f"{note} · Order {redeem_key}",
        )

    return await db.get_order(order_id)


async def checkout(
    bot,
    db,
    *,
    guild_id: int,
    buyer_id: int,
    treasury_type: str,
    treasury_row,
    lines: list[tuple],
    force_shulker: bool = False,
    union_member: bool = False,
):
    """
    ``lines`` is a list of ``(catalog_row, units)``.

    Returns ``(order_row, quote, components)``. Raises ``ShopError`` on any
    validation or funding problem. The treasury deduction and order write happen
    inside one SQLite transaction so a crash can never charge without creating
    the order.
    """
    assert treasury_type in ("nation", "kingdom")

    banned = await db.banned_keys()
    items_total = 0
    member_items_total = 0
    for row, units in lines:
        if units <= 0:
            raise ShopError("Quantities must be greater than zero.")
        if pricing.is_banned(row["key"], banned):
            raise ShopError(f"**{row['display_name']}** is banned and cannot be sold.")
        line_total = units * row["price"]
        items_total += line_total
        if union_member and pricing.is_union_eligible(row["category"]):
            member_items_total += line_total

    components = await _resolve_components(db, lines)
    # Bundles expand into component keys, so a ban on a single piece (e.g. one
    # armour item) must also block the bundle that contains it.
    for ckey, display_name, _qty, _ms in components:
        if pricing.is_banned(ckey, banned):
            raise ShopError(f"**{display_name}** is banned and cannot be sold.")

    slots = pricing.compute_slots((k, q, ms) for k, _dn, q, ms in components)
    quote = pricing.quote(
        items_total,
        slots,
        treasury_type,
        force_shulker=force_shulker,
        member_items_total=member_items_total,
    )

    if treasury_row["balance"] < quote["final_total"]:
        raise _insufficient_funds(
            treasury_type, treasury_row["name"], treasury_row["balance"], quote["final_total"]
        )

    order = await _place_order(
        db,
        guild_id=guild_id,
        buyer_id=buyer_id,
        treasury_type=treasury_type,
        treasury_row=treasury_row,
        quote=quote,
        components=components,
        kind="shop_purchase",
        note="Royal Shop purchase",
    )
    return order, quote, components


async def checkout_kit(
    bot,
    db,
    *,
    guild_id: int,
    buyer_id: int,
    treasury_type: str,
    treasury_row,
    kit_row,
    kit_items: list,
    union_member: bool = False,
):
    """
    Create an order for a kit. ``kit_items`` are rows with ``item_key`` and an
    exact ``quantity`` (total item count, not catalog units). Kits are always
    shulker-packaged, so the shulker fee is forced on.
    """
    assert treasury_type in ("nation", "kingdom")

    banned = await db.banned_keys()
    components: list[tuple[str, str, int, int]] = []
    items_total = 0
    member_items_total = 0

    for kit_item in kit_items:
        cat = await db.get_catalog_item(kit_item["item_key"])
        if not cat:
            raise ShopError(f"Kit references an unknown item key: `{kit_item['item_key']}`.")
        if pricing.is_banned(cat["key"], banned):
            raise ShopError(f"**{cat['display_name']}** is banned and cannot be sold.")
        qty = int(kit_item["quantity"])
        if qty <= 0:
            continue
        unit_qty = cat["unit_qty"] or 1
        units = math.ceil(qty / unit_qty)
        items_total += units * cat["price"]
        if union_member and pricing.is_union_eligible(cat["category"]):
            member_items_total += units * cat["price"]
        components.append((cat["key"], cat["display_name"], qty, cat["max_stack"]))

    if not components:
        raise ShopError("That kit has no deliverable items.")

    slots = pricing.compute_slots((k, q, ms) for k, _dn, q, ms in components)
    quote = pricing.quote(
        items_total,
        slots,
        treasury_type,
        force_shulker=True,
        member_items_total=member_items_total,
    )

    if treasury_row["balance"] < quote["final_total"]:
        raise _insufficient_funds(
            treasury_type, treasury_row["name"], treasury_row["balance"], quote["final_total"]
        )

    order = await _place_order(
        db,
        guild_id=guild_id,
        buyer_id=buyer_id,
        treasury_type=treasury_type,
        treasury_row=treasury_row,
        quote=quote,
        components=components,
        kind="kit_purchase",
        note=f"Kit #{kit_row['id']} ({kit_row['name']})",
    )
    return order, quote, components
