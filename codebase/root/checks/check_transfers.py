"""End-to-end checks for /individual transfer, /individual pay and /give via the real cogs."""
import asyncio, os, sys, tempfile
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import config
from database import Database
from cogs.individual import Individuals
from cogs.transfers import Transfers

fails = []
def check(label, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


class FakeUser:
    def __init__(self, uid, name):
        self.id = uid
        self.display_name = name
        self.mention = f"<@{uid}>"
        self.bot = False
        self.guild_permissions = None


class FakeResp:
    def __init__(self, sink):
        self._sink = sink
    async def send_message(self, embed=None, **kw):
        self._sink.append(embed)


class FakeInteraction:
    def __init__(self, user, guild_id=1):
        self.user = user
        self.guild_id = guild_id
        self.sent = []
        self.response = FakeResp(self.sent)

    def text(self):
        e = self.sent[-1]
        parts = [e.title or ""]
        for f in e.fields:
            parts.append(f"{f.name}: {f.value}")
        if e.description:
            parts.append(e.description)
        return " | ".join(parts)


class FakeBot:
    def __init__(self, db):
        self.db = db


async def main():
    db = Database(os.path.join(tempfile.mkdtemp(), "e2e.db"))
    await db.connect()
    g = 1
    cog = Individuals(FakeBot(db))

    alice = FakeUser(10, "Alice")
    bob = FakeUser(20, "Bob")

    await db.get_individual(g, 10, "Alice")
    await db.credit_individual(g, 10, 10_000)

    # --- /individual transfer -------------------------------------------
    i = FakeInteraction(alice)
    await Individuals.transfer.callback(cog, i, bob, "4,000")
    print("  transfer reply:", i.text())
    check("transfer reply is success",
          "Transfer Complete" in i.text(), i.text())
    check("alice debited", int((await db.find_individual(g, 10))["balance"]) == 6_000,
          (await db.find_individual(g, 10))["balance"])
    check("bob credited", int((await db.find_individual(g, 20))["balance"]) == 4_000,
          (await db.find_individual(g, 20))["balance"])
    rows = await db.fetchall("SELECT * FROM transactions WHERE kind='individual_transfer'")
    check("two ledger rows", len(rows) == 2, len(rows))
    check("ledger nets to zero", sum(int(r["amount"]) for r in rows) == 0)

    # --- over-balance transfer ------------------------------------------
    i = FakeInteraction(alice)
    await Individuals.transfer.callback(cog, i, bob, "999,999")
    check("overdraw refused", "Insufficient Funds" in i.text(), i.text())
    check("alice unchanged", int((await db.find_individual(g, 10))["balance"]) == 6_000)

    # --- self transfer --------------------------------------------------
    i = FakeInteraction(alice)
    await Individuals.transfer.callback(cog, i, alice, "100")
    check("self transfer refused", "Invalid" in i.text(), i.text())

    # --- all keyword ----------------------------------------------------
    i = FakeInteraction(alice)
    await Individuals.transfer.callback(cog, i, bob, "half")
    check("half moves 3000", int((await db.find_individual(g, 10))["balance"]) == 3_000,
          (await db.find_individual(g, 10))["balance"])
    check("bob now 7000", int((await db.find_individual(g, 20))["balance"]) == 7_000,
          (await db.find_individual(g, 20))["balance"])

    # --- recipient at cap ------------------------------------------------
    await db.get_individual(g, 30, "Rich")
    rich = FakeUser(30, "Rich")
    await db.credit_individual(g, 30, config.INDIVIDUAL_MAX_CREDITS - 10)
    i = FakeInteraction(alice)
    await Individuals.transfer.callback(cog, i, rich, "1,000")
    print("  cap reply:", i.text())
    check("cap refused", "Recipient At Cap" in i.text(), i.text())
    check("alice not debited on cap refusal",
          int((await db.find_individual(g, 10))["balance"]) == 3_000,
          (await db.find_individual(g, 10))["balance"])

    # --- /individual pay into a nation ----------------------------------
    nid = await db.create_nation(g, "Aurum", 10)
    i = FakeInteraction(alice)
    await Individuals.pay.callback(cog, i, "nation:Aurum", "1,000")
    print("  pay reply:", i.text())
    check("pay success", "Payment Complete" in i.text(), i.text())
    check("nation credited", int((await db.get_nation_by_id(nid))["balance"]) == 1_000,
          (await db.get_nation_by_id(nid))["balance"])
    check("alice debited by pay", int((await db.find_individual(g, 10))["balance"]) == 2_000,
          (await db.find_individual(g, 10))["balance"])

    payrows = await db.fetchall("SELECT * FROM transactions WHERE kind='individual_payment'")
    check("pay has two ledger rows", len(payrows) == 2, len(payrows))

    # --- /give: a treasury leader pays a member wallet -------------------
    transfers_cog = Transfers(FakeBot(db))
    await db.add_balance("nations", nid, 100_000)
    nation_before = int((await db.get_nation_by_id(nid))["balance"])
    bob_before = int((await db.find_individual(g, 20))["balance"])
    i = FakeInteraction(alice)
    await Transfers.give.callback(transfers_cog, i, "nation:Aurum", bob, "1,000")
    print("  give reply:", i.text())
    check("give success", "Grant Sent" in i.text(), i.text())
    check(
        "give debits the treasury",
        int((await db.get_nation_by_id(nid))["balance"]) == nation_before - 1_000,
        (await db.get_nation_by_id(nid))["balance"],
    )
    check(
        "give credits the member",
        int((await db.find_individual(g, 20))["balance"]) == bob_before + 1_000,
        (await db.find_individual(g, 20))["balance"],
    )
    giverows = await db.fetchall("SELECT * FROM transactions WHERE kind='treasury_give'")
    check("give has two ledger rows", len(giverows) == 2, len(giverows))
    check("give ledger nets to zero", sum(int(r["amount"]) for r in giverows) == 0)

    # The same shortcut works from a kingdom treasury.
    kid = await db.create_kingdom(g, "Crownlands", 10)
    await db.add_balance("kingdoms", kid, 50_000)
    i = FakeInteraction(alice)
    await Transfers.give.callback(transfers_cog, i, "kingdom:Crownlands", bob, "2,000")
    check("kingdom give success", "Grant Sent" in i.text(), i.text())
    check(
        "kingdom give debits the treasury",
        int((await db.get_kingdom_by_id(kid))["balance"]) == 48_000,
        (await db.get_kingdom_by_id(kid))["balance"],
    )
    check(
        "kingdom give credits the member",
        int((await db.find_individual(g, 20))["balance"]) == bob_before + 3_000,
        (await db.find_individual(g, 20))["balance"],
    )

    # A member who does not lead the treasury cannot give from it.
    eve = FakeUser(99, "Eve")
    i = FakeInteraction(eve)
    await Transfers.give.callback(transfers_cog, i, "nation:Aurum", bob, "10")
    check("give refuses a non-leader", "Not Authorized" in i.text(), i.text())

    # /give only accepts a treasury source, never a raw individual wallet.
    i = FakeInteraction(alice)
    await Transfers.give.callback(transfers_cog, i, "individual:20", bob, "10")
    check("give refuses an individual source", "Source Not Found" in i.text(), i.text())

    # A capped wallet is refused before the treasury is debited.
    rich_before = int((await db.find_individual(g, 30))["balance"])
    nation_before = int((await db.get_nation_by_id(nid))["balance"])
    i = FakeInteraction(alice)
    await Transfers.give.callback(transfers_cog, i, "nation:Aurum", rich, "1,000")
    check("give refuses a capped wallet", "Recipient At Cap" in i.text(), i.text())
    check(
        "capped give leaves the treasury untouched",
        int((await db.get_nation_by_id(nid))["balance"]) == nation_before,
        (await db.get_nation_by_id(nid))["balance"],
    )
    check(
        "capped give leaves the wallet untouched",
        int((await db.find_individual(g, 30))["balance"]) == rich_before,
        (await db.find_individual(g, 30))["balance"],
    )

    # --- nations/kingdoms are never capped or taxed ----------------------
    await db.add_balance("nations", nid, config.INDIVIDUAL_MAX_CREDITS * 3)
    check("nation balance above individual cap allowed",
          int((await db.get_nation_by_id(nid))["balance"]) > config.INDIVIDUAL_MAX_CREDITS,
          (await db.get_nation_by_id(nid))["balance"])

    # --- wallet deleted: transfers stop ---------------------------------
    await db.delete_individual(g, 10)
    i = FakeInteraction(alice)
    await Individuals.transfer.callback(cog, i, bob, "10")
    check("no wallet refused", "No Wallet" in i.text(), i.text())

    await db.close()
    print("\nFAILURES:", len(fails))

asyncio.run(main())
raise SystemExit(1 if fails else 0)
