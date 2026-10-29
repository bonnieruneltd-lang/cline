"""
shop/pricing.py
===============
Pure, testable pricing rules for the Royal Shop. No Discord or DB imports so
the math can be unit-tested in isolation.

Conventions
-----------
* ``units`` always means "number of catalog units". One unit is the quantity a
  catalog line is priced for (e.g. one 64-stack, or one non-stackable item).
* ``key`` is the catalog key. Bundle keys expand into their component items.
"""
from __future__ import annotations

import math
from typing import Iterable

import config
from shop.catalog_data import BANNED_PATTERNS, BUNDLES


def is_banned(key: str, banned_keys: set[str]) -> bool:
    """True if ``key`` is banned by exact match or by a family pattern."""
    lowered = key.lower()
    if lowered in banned_keys:
        return True
    return any(pattern in lowered for pattern in BANNED_PATTERNS)


def is_union_eligible(category: str | None) -> bool:
    """True when a catalog category qualifies for the Union member discount.

    Only building blocks and food qualify. Redstone and materials such as
    iron blocks are deliberately excluded.
    """
    return category in config.UNION_MEMBER_DISCOUNT_CATEGORIES


def expand_line(key: str, units: int, unit_qty: int) -> list[tuple[str, int]]:
    """Expand a catalog line into concrete ``(component_key, total_items)`` pairs.

    Bundle keys (armor sets) fan out to their component items; normal items
    return a single pair whose quantity is ``units * unit_qty``.
    """
    if key in BUNDLES:
        return [(component_key, units * component_qty) for component_key, component_qty in BUNDLES[key]]
    return [(key, units * unit_qty)]


def compute_slots(components: Iterable[tuple[str, int, int]]) -> int:
    """Total inventory slots used by ``(key, quantity, max_stack)`` components."""
    total = 0
    for _key, quantity, max_stack in components:
        total += math.ceil(quantity / max_stack) if max_stack > 0 else quantity
    return total


def discount_percent(treasury_type: str) -> int:
    if treasury_type == "nation":
        return round(config.NATION_DISCOUNT * 100)
    if treasury_type == "kingdom":
        return round(config.KINGDOM_DISCOUNT * 100)
    return 0


def quote(
    items_total: int,
    slots: int,
    treasury_type: str,
    *,
    force_shulker: bool = False,
    member_items_total: int = 0,
) -> dict:
    """Produce the full price breakdown for an order.

    ``force_shulker`` is set for kits (kits are always shulker-packaged).

    ``member_items_total`` is the part of ``items_total`` bought by a Workers'
    Union member from eligible categories (building blocks and food). That part
    gets the flat member discount instead of the treasury discount; everything
    else keeps the normal treasury discount. Non-members pass 0, which
    reproduces the original pricing exactly.
    """
    shulker_fee = config.SHULKER_BOX_FEE if (force_shulker or slots > config.SHOP_SLOT_LIMIT) else 0
    member_items_total = max(0, min(member_items_total, items_total))

    union_discount = member_items_total * config.UNION_MEMBER_DISCOUNT // 100
    remainder = items_total - member_items_total + shulker_fee
    treasury_discount = remainder * discount_percent(treasury_type) // 100

    subtotal = items_total + shulker_fee
    discount = union_discount + treasury_discount
    return {
        "items_total": items_total,
        "slots": slots,
        "shulker_fee": shulker_fee,
        "union_discount": union_discount,
        "treasury_discount": treasury_discount,
        "discount": discount,
        "subtotal": subtotal,
        "final_total": subtotal - discount,
    }


def parse_item_key(key: str) -> dict:
    """Decode a catalog key into its delivery-relevant parts."""
    if key.startswith("enchant:"):
        parts = key.split(":")
        return {"namespace": "enchant", "item": "minecraft:enchanted_book",
                "enchant_id": parts[1], "enchant_level": int(parts[2])}
    namespace, _, resource = key.partition(":")
    return {"namespace": namespace or "minecraft", "item": key,
            "enchant_id": None, "enchant_level": None}


def is_bundle(key: str) -> bool:
    return key in BUNDLES
