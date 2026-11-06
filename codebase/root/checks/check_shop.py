"""Royal Shop and kit checkout: pricing maths plus the real command callbacks.

The shop is the main way credits leave a treasury and the only money path that
had no check of its own. This script prices the cart in isolation (discounts,
shulker fee, bundles, bans, slots), then drives ``/shop buy``, ``/shop order``,
``/shop markredeemed``, ``/kit create`` and ``/kit buy`` against a throwaway
SQLite database so the debit, the order rows and the ledger are all observed.
"""

import asyncio
import math
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import config
import shop.pricing as pricing
from cogs.kits import Kits
from cogs.shop import Shop
from database import Database
from shop.catalog_data import BUNDLES
from shop.seed import seed_shop

fails = []


def check(label, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


class FakeUser:
    def __init__(self, uid, name, admin=False):
        self.id = uid
        self.display_name = name
        self.name = name
        self.mention = f"<@{uid}>"
        self.bot = False
        self.dms = []
        self.guild_permissions = SimpleNamespace(administrator=admin) if admin else None

    async def send(self, content=None, embed=None, **kw):
        self.dms.append(embed)
        return SimpleNamespace(id=1)


class FakeResp:
    def __init__(self, sink):
        self._sink = sink
        self._done = False

    async def send_message(self, content=None, embed=None, ephemeral=False, **kw):
        self._done = True
        self._sink.append(("response", embed, ephemeral))

    async def defer(self, thinking=False, ephemeral=False):
        self._done = True
        self._sink.append(("defer", None, ephemeral))

    async def send_modal(self, modal):
        self._done = True
        self._sink.append(("modal", getattr(modal, "name", None), True))

    def is_done(self):
        return self._done


class FakeFollowup:
    def __init__(self, sink):
        self._sink = sink

    async def send(self, content=None, embed=None, ephemeral=False, **kw):
        self._sink.append(("followup", embed, ephemeral))


class FakeInteraction:
    def __init__(self, user, guild_id=1):
        self.user = user
        self.guild_id = guild_id
        self.sent = []
        self.response = FakeResp(self.sent)
        self.followup = FakeFollowup(self.sent)

    def text(self):
        parts = []
        for _kind, embed, _ephemeral in self.sent:
            if embed is None:
                continue
            parts.append(embed.title or "")
            parts.append(embed.description or "")
            for field in embed.fields:
                parts.append(f"{field.name}: {field.value}")
        return " | ".join(parts)


class FakeBot:
    def __init__(self, db):
        self.db = db
        self.logger = SimpleNamespace(exception=lambda *a, **k: None)

    def get_channel(self, channel_id):
        return None

    def get_user(self, user_id):
        return None

    async def fetch_user(self, user_id):
        return None


async def seed(db, guild_id):
    await seed_shop(db)
    admin = FakeUser(1, "Admin", admin=True)
    leader = FakeUser(2, "Leader")
    member = FakeUser(3, "Member")
    for user in (admin, leader, member):
        await db.get_individual(guild_id, user.id, user.display_name)
        await db.credit_individual(guild_id, user.id, 100_000)
    nation_id = await db.create_nation(guild_id, "Aurum", leader.id)
    kingdom_id = await db.create_kingdom(guild_id, "Realm", leader.id)
    return admin, leader, member, nation_id, kingdom_id


def quote_cases():
    """The pricing contract, checked without a database."""
    fee = config.SHULKER_BOX_FEE

    q = pricing.quote(1000, config.SHOP_SLOT_LIMIT, "nation")
    check("a cart at the slot limit pays no shulker fee", q["shulker_fee"] == 0, q)
    q = pricing.quote(1000, config.SHOP_SLOT_LIMIT + 1, "nation")
    check("a cart past the slot limit pays the shulker fee", q["shulker_fee"] == fee, q)

    q = pricing.quote(1000, 1, "nation")
    check("a nation treasury gets its 20% discount",
          q["treasury_discount"] == 200 and q["final_total"] == 800, q)
    q = pricing.quote(1000, 1, "kingdom")
    check("a kingdom treasury gets its 25% discount",
          q["treasury_discount"] == 250 and q["final_total"] == 750, q)

    # Member: eligible items are 50% off *instead of* the treasury discount; the
    # rest of the cart and the shulker fee still earn the treasury discount.
    q = pricing.quote(1000, 1, "nation", member_items_total=1000)
    check("a member's eligible cart is half price",
          q["union_discount"] == 500 and q["treasury_discount"] == 0
          and q["final_total"] == 500, q)
    q = pricing.quote(1000, 30, "kingdom", member_items_total=600)
    check("member and treasury discounts split the cart without stacking",
          q["union_discount"] == 300
          and q["treasury_discount"] == (400 + fee) * 25 // 100
          and q["final_total"] == 1000 + fee - 300 - (400 + fee) * 25 // 100, q)
    q = pricing.quote(1000, 1, "nation", member_items_total=10_000)
    check("a member total larger than the cart is clamped",
          q["union_discount"] == 500 and q["final_total"] == 500, q)

    check("a non-member is priced exactly as before",
          pricing.quote(1000, 1, "nation")
          == pricing.quote(1000, 1, "nation", member_items_total=0))

    q = pricing.quote(1, 1, "kingdom")
    check("a one-credit kingdom cart floors its discount to zero",
          q["final_total"] == 1 and q["treasury_discount"] == 0, q)

    key = "bundle:iron_armor_set"
    parts = pricing.expand_line(key, 2, 1)
    check("a bundle expands to packaged components, never the bundle key",
          key not in [part[0] for part in parts]
          and parts == [(k, 2 * qty) for k, qty in BUNDLES[key]], parts)
    check("a plain line expands to units times the catalog quantity",
          pricing.expand_line("minecraft:oak_log", 3, 64) == [("minecraft:oak_log", 192)])

    check("slots round up per component",
          pricing.compute_slots([("a", 64, 64), ("b", 1, 1), ("c", 65, 64)]) == 1 + 1 + 2)
    check("an unknown treasury type earns no discount",
          pricing.discount_percent("individual") == 0)

    check("a hard ban blocks the exact key",
          pricing.is_banned("minecraft:elytra", {"minecraft:elytra"}))
    check("a family ban blocks a member of the family",
          pricing.is_banned("minecraft:netherite_sword", set()))
    check("an ordinary item is not banned", not pricing.is_banned("minecraft:oak_log", set()))

    check("union eligibility covers building blocks and food",
          pricing.is_union_eligible("Basic Building") and pricing.is_union_eligible("Food & Crops"))
    check("union eligibility excludes redstone and materials",
          not pricing.is_union_eligible("Redstone") and not pricing.is_union_eligible("Metals"))



async def shop_commands(db, admin, leader, member, nation_id):
    bot = FakeBot(db)
    shop = Shop(bot)
    guild = 1

    oak = await db.get_catalog_item("minecraft:oak_log")
    await db.add_balance("nations", nation_id, 10_000)
    starting = int((await db.get_nation(guild, "Aurum"))["balance"])

    bought = FakeInteraction(leader)
    await Shop.buy.callback(shop, bought, "nation:Aurum", "minecraft:oak_log", 2)
    print("  buy reply:", bought.text()[:160])

    orders = await db.list_orders(guild, leader.id)
    check("a successful purchase writes exactly one order", len(orders) == 1, len(orders))
    order = orders[0]
    items = await db.get_order_items(order["id"])
    print("  order:", {k: order[k] for k in ("items_total", "discount", "final_total", "status")},
          "items:", [(i["item_key"], i["quantity"]) for i in items])

    check("the order keeps the catalog total", order["items_total"] == 2 * oak["price"], dict(order))
    check("the order stores the treasury discount",
          order["discount"] == 2 * oak["price"] * 20 // 100, dict(order))
    check("the final total is the discounted price",
          order["final_total"] == 2 * oak["price"] - 2 * oak["price"] * 20 // 100, dict(order))
    check("the redeem key looks like RC-XXXXXX-XXXXXX",
          len(order["redeem_key"]) == 16 and order["redeem_key"].startswith("RC-")
          and order["redeem_key"].replace("RC-", "").replace("-", "").isalnum(),
          order["redeem_key"])
    check("the order starts pending", order["status"] == "pending", order["status"])
    check("the order item is a concrete key, not a bundle",
          items and items[0]["item_key"] == "minecraft:oak_log", [dict(i) for i in items])
    check("the order item carries the delivered quantity",
          items and int(items[0]["quantity"]) == 2 * int(oak["unit_qty"]),
          [dict(i) for i in items])

    after = int((await db.get_nation(guild, "Aurum"))["balance"])
    check("the treasury is debited exactly the final total",
          starting - after == order["final_total"], (starting, after, order["final_total"]))
    ledger = await db.fetchall(
        "SELECT * FROM transactions WHERE kind='shop_purchase' AND account_type='nation'"
    )
    check("the purchase writes one ledger row", len(ledger) == 1, len(ledger))
    check("the ledger row mirrors the debit",
          ledger and int(ledger[0]["amount"]) == -order["final_total"]
          and int(ledger[0]["balance_after"]) == after, [dict(r) for r in ledger])
    check("the buyer is DM'd the order", bool(leader.dms), len(leader.dms))

    # /shop order — the owner sees it, another member is refused.
    seen = FakeInteraction(leader)
    await Shop.order.callback(shop, seen, order["redeem_key"].lower())
    check("the buyer can look the order up", order["redeem_key"] in seen.text(),
          seen.text()[:200])
    stranger = FakeInteraction(member)
    await Shop.order.callback(shop, stranger, order["redeem_key"])
    check("another member cannot read the key",
          "Not Authorized" in stranger.text(), stranger.text()[:200])

    # /shop markredeemed is once-only.
    first = FakeInteraction(admin)
    await Shop.markredeemed.callback(shop, first, order["redeem_key"], "uuid-1234")
    refreshed = await db.get_order_by_key(order["redeem_key"])
    check("markredeemed flips the order", refreshed["status"] == "redeemed", dict(refreshed))
    second = FakeInteraction(admin)
    await Shop.markredeemed.callback(shop, second, order["redeem_key"], "uuid-9999")
    again = await db.get_order_by_key(order["redeem_key"])
    check("markredeemed refuses a second redemption", "Already Redeemed" in second.text(),
          second.text()[:200])
    check("the refused redemption does not rewrite the uuid",
          again["redeemed_uuid"] == "uuid-1234", dict(again))

    # A short treasury is refused with no order and no debit.
    await db.set_balance("nations", nation_id, 1)
    before_orders = len(await db.list_orders(guild, leader.id))
    poor = FakeInteraction(leader)
    await Shop.buy.callback(shop, poor, "nation:Aurum", "minecraft:oak_log", 2)
    check("an underfunded purchase is refused", "Checkout Failed" in poor.text(), poor.text()[:200])
    check("the refusal creates no order",
          len(await db.list_orders(guild, leader.id)) == before_orders)
    check("the refusal leaves the treasury alone",
          int((await db.get_nation(guild, "Aurum"))["balance"]) == 1)

    # A banned item is refused, and the ban is reversible.
    await db.add_balance("nations", nation_id, 100_000)
    await db.ban_item("minecraft:oak_log", "test ban", admin.id)
    banned = FakeInteraction(leader)
    await Shop.buy.callback(shop, banned, "nation:Aurum", "minecraft:oak_log", 1)
    check("a banned item is refused", "banned" in banned.text().lower(), banned.text()[:200])
    await db.unban_item("minecraft:oak_log")
    unbanned = FakeInteraction(leader)
    await Shop.buy.callback(shop, unbanned, "nation:Aurum", "minecraft:oak_log", 1)
    check("the unban restores the item", "Order Confirmed" in unbanned.text(),
          unbanned.text()[:200])

    # Only the leader or an admin may spend a treasury.
    intruder = FakeInteraction(member)
    await Shop.buy.callback(shop, intruder, "nation:Aurum", "minecraft:oak_log", 1)
    check("a non-leader cannot spend the treasury",
          "Not Authorized" in intruder.text(), intruder.text()[:200])

    # A Union member pays 50% of the eligible items and nothing else.
    await db.add_union_member(guild, "nation", nation_id)
    before = int((await db.get_nation(guild, "Aurum"))["balance"])
    member_buy = FakeInteraction(leader)
    await Shop.buy.callback(shop, member_buy, "nation:Aurum", "minecraft:dirt", 4)
    dirt = await db.get_catalog_item("minecraft:dirt")
    member_order = (await db.list_orders(guild, leader.id))[0]
    spent = before - int((await db.get_nation(guild, "Aurum"))["balance"])
    expected = 4 * dirt["price"] // 2
    check("a Union member pays half price on building blocks",
          spent == expected, (spent, expected))
    check("the member order records the Union discount",
          int(member_order["union_discount"]) == expected
          and int(member_order["discount"]) == expected, dict(member_order))


async def kit_commands(db, leader, member, nation_id):
    bot = FakeBot(db)
    kits = Kits(bot)
    guild = 1
    # The shop pass joined the Union; leave it so the treasury discount is the
    # only discount in play here.
    await db.remove_union_member(guild, "nation", nation_id)

    created = FakeInteraction(leader)
    await Kits.create.callback(kits, created, "Starter")
    check("/kit create opens the contents modal rather than buying",
          created.sent and created.sent[-1][0] == "modal", created.sent)

    submit = FakeInteraction(leader)
    await Kits._on_kit_submit(kits, submit, "Starter",
                              "minecraft:oak_log x64\nminecraft:iron_ingot x32")
    print("  kit submit:", submit.text()[:160])
    created_kits = await db.list_kits(guild)
    check("the submitted kit is stored", bool(created_kits), submit.text()[:200])
    lead_kit = created_kits[0]
    kit_items = await db.get_kit_items(lead_kit["id"])
    check("/kit create stores the submitted contents",
          {(i["item_key"], int(i["quantity"])) for i in kit_items}
          == {("minecraft:oak_log", 64), ("minecraft:iron_ingot", 32)},
          [dict(i) for i in kit_items])

    again = FakeInteraction(leader)
    await Kits._on_kit_submit(kits, again, "Second", "minecraft:oak_log x64")
    check("the 7-day cooldown refuses a second kit",
          "Cooldown" in again.text(), again.text()[:200])
    check("the refused kit is not stored", len(await db.list_kits(guild)) == 1)

    view = FakeInteraction(member)
    await Kits.view.callback(kits, view, lead_kit["id"])
    check("/kit view shows the contents and price",
          "Kit #" in view.text() and "Shulker" in view.text(), view.text()[:200])

    before = int((await db.get_nation(guild, "Aurum"))["balance"])
    bought = FakeInteraction(leader)
    await Kits.buy.callback(kits, bought, lead_kit["id"], "nation:Aurum")
    order = (await db.list_orders(guild, leader.id))[0]
    spent = before - int((await db.get_nation(guild, "Aurum"))["balance"])
    oak = await db.get_catalog_item("minecraft:oak_log")
    ingot = await db.get_catalog_item("minecraft:iron_ingot")
    items_total = (math.ceil(64 / oak["unit_qty"]) * oak["price"]
                   + math.ceil(32 / ingot["unit_qty"]) * ingot["price"])
    expected = (items_total + config.SHULKER_BOX_FEE) * 80 // 100
    check("a kit is always shulker-packaged",
          order["shulker_fee"] == config.SHULKER_BOX_FEE, dict(order))
    check("the kit price is contents plus the shulker fee, then the treasury discount",
          spent == expected, (spent, expected))
    delivered = await db.get_order_items(order["id"])
    check("the kit order delivers the exact quantities",
          {(i["item_key"], int(i["quantity"])) for i in delivered}
          == {("minecraft:oak_log", 64), ("minecraft:iron_ingot", 32)},
          [dict(i) for i in delivered])

    empty = await db.create_kit(guild, "Empty", leader.id)
    refused = FakeInteraction(leader)
    await Kits.buy.callback(kits, refused, empty, "nation:Aurum")
    check("an empty kit cannot be bought", "Checkout Failed" in refused.text(),
          refused.text()[:200])

    intruder = FakeInteraction(member)
    await Kits.buy.callback(kits, intruder, lead_kit["id"], "nation:Aurum")
    check("a non-leader cannot buy with the treasury",
          "Not Authorized" in intruder.text(), intruder.text()[:200])


async def main():
    quote_cases()

    db = Database(os.path.join(tempfile.mkdtemp(), "shop.db"))
    await db.connect()
    try:
        _admin, leader, member, nation_id, _kingdom_id = await seed(db, 1)
        await shop_commands(db, _admin, leader, member, nation_id)
        await kit_commands(db, leader, member, nation_id)
    finally:
        await db.close()

    print("\nFAILURES:", len(fails))
    for failure in fails:
        print(" -", failure)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
