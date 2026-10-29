# `shop/` — Royal Shop internals

The Royal Shop is data-driven. `cogs/shop.py` and `cogs/kits.py` provide the
Discord commands; this package contains the catalog, pricing and checkout
rules. Keep Discord imports out of `pricing.py` so the math stays testable.

| File | Responsibility |
|---|---|
| `catalog_data.py` | Seed catalog, bundle definitions and hard-banned keys |
| `pricing.py` | Pure price/discount/packaging math |
| `checkout.py` | Validates a cart, debits the treasury atomically, writes the order |
| `embeds.py` | Shared order embed used by shop and kits |
| `autocomplete.py` | Item autocomplete for slash commands |
| `seed.py` | Idempotent startup seeding |
| `__init__.py` | Package marker |

---

## Catalog key conventions

| Prefix | Meaning | Example |
|---|---|---|
| `minecraft:` | Vanilla item | `minecraft:diamond` |
| `backport:` | Modern Content Backport item | `backport:copper_grate` |
| `enchant:` | Enchanted book | `enchant:sharpness:5` |
| `bundle:` | Multi-piece bundle, expanded at checkout | `bundle:iron_armor` |

A catalog row stores the display name, category, unit price, unit quantity,
stack size and optional enchant/backport metadata. Never change an existing
key casually: orders and bans reference keys.

---

## Pricing rules (`pricing.py`)

* `units` means catalog units, not raw item count. One unit is the quantity the
  catalog line is priced for (usually a 64-stack, or one non-stackable item).
* Nation treasury purchases receive `config.NATION_DISCOUNT` (20%).
* Kingdom treasury purchases receive `config.KINGDOM_DISCOUNT` (25%).
* Workers' Union members get `config.UNION_MEMBER_DISCOUNT` (50%) on lines from
  `config.UNION_MEMBER_DISCOUNT_CATEGORIES` — `Basic Building`,
  `Wood & Masonry`, `Food & Crops`. Redstone (`Redstone & Automation`) and
  materials (`Mob Drops & Materials`, e.g. iron blocks) are excluded.
* The member rate **replaces** the treasury discount on those lines; it does not
  stack. The shulker fee always earns the treasury discount only.
  `quote(..., member_items_total=...)` implements this; callers pass the
  eligible part of the cart and 0 for non-members, which is byte-identical to
  the old pricing.
* `orders.union_discount` stores the member part of `discount`; the order embed
  shows the two discounts separately.
* Purchases using more than `config.SHOP_SLOT_LIMIT` stacks are packed into
  shulker boxes and charged `config.SHULKER_BOX_FEE` per box.
* Bundles expand into their component items before pricing and delivery.
* Kit components are priced per catalog line too, so a member kit made of
  building blocks or food gets the 50% rate.
* Banned keys cannot be bought, including when they appear inside a bundle.

All results are integer credits. Never introduce float rounding into prices.

---

## Checkout flow (`checkout.py`)

1. Resolve the buying account (nation or kingdom) and verify the caller may
   spend from it.
2. Reject banned items.
3. Price the cart.
4. Open a database transaction and debit the treasury.
5. Write the `orders` row and one `order_items` row per line.
6. Generate a unique `RC-XXXXXX-XXXXXX` key.
7. Log the transaction.
8. Return the order so the cog can DM the key and post the confirmation.

If any step fails, the transaction rolls back and no credits are spent.

`ShopError` is the user-facing failure type. Raise it for expected problems
(not enough credits, banned item, bad quantity); let unexpected exceptions
bubble up and be logged.

---

## Seeding (`seed.py`)

`seed_shop(db)` runs at every startup:

* catalog rows are inserted or updated from `CATALOG`, preserving admin price
  edits;
* hard bans are applied only on the first run so a later admin unban is not
  overwritten.

---

## Order keys

* Format: `RC-XXXXXX-XXXXXX` (uppercase hex).
* Single-use and unique in the database.
* Delivered by DM; if DMs are closed, the cog shows the key in the ephemeral
  reply instead.
* Redeemed either by the Forge mod through `POST /api/redeem` or manually by an
  admin with `/shop markredeemed`.
* No Minecraft account linking: the key plus the player UUID supplied at
  redemption is the whole credential.
