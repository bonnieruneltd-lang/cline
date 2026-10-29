"""Bank/draw guards: fresh balances, atomic one-shot statuses and DM settings.

The checks drive the real command callbacks with throwaway SQLite databases and
minimal fake Discord objects, so no login or network is involved.
"""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import cogs.bank as bank_module  # noqa: E402
from cogs.bank import Bank, DrawApprovalView  # noqa: E402
from cogs.decree import Decree  # noqa: E402
from database import Database  # noqa: E402
from utils import encode_account  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


class FakeBot:
    def __init__(self, db):
        self.db = db


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.mention = f"<@{uid}>"
        self.guild_permissions = SimpleNamespace(administrator=False)


class FakeResponse:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, embed=None, ephemeral=False, **kwargs):
        self.sent.append(embed)

    async def edit_message(self, embed=None, view=None, **kwargs):
        self.edited.append(embed)

    async def edit_original_response(self, embed=None, **kwargs):
        self.sent.append(embed)


class FakeInteraction:
    def __init__(self, user, guild_id=1):
        self.user = user
        self.guild_id = guild_id
        self.response = FakeResponse()


async def setbalance_check(db):
    g = 1
    nid = await db.create_nation(g, "Aurum", 111)
    await db.add_balance("nations", nid, 1_000)
    await db.log_tx(
        guild_id=g, kind="test_grant", account_type="nation", account_id=nid,
        account_name="Aurum", amount=1_000, balance_after=1_000,
    )

    real_resolve = bank_module.resolve_account

    async def stale_resolve(db_, guild_id, account):
        atype, row = await real_resolve(db_, guild_id, account)
        # Simulate a logged deposit landing after the command's read, before its
        # transaction. The logged delta must use the fresh 2,000, not the stale
        # 1,000.
        new_balance = await db_.add_balance("nations", row["id"], 1_000)
        await db_.log_tx(
            guild_id=guild_id, kind="deposit", account_type="nation",
            account_id=row["id"], account_name=row["name"], amount=1_000,
            balance_after=new_balance,
        )
        return atype, row

    bank_module.resolve_account = stale_resolve
    cog = Bank(FakeBot(db))
    interaction = FakeInteraction(FakeUser(1))

    # Discord must be called after the commit. Awaiting it inside the
    # transaction holds BEGIN IMMEDIATE over every other money command and can
    # roll the edit back when the interaction has expired.
    sent_in_tx = []
    original_send = interaction.response.send_message

    async def spy_send(embed=None, **kwargs):
        sent_in_tx.append(db._in_tx)
        await original_send(embed=embed, **kwargs)

    interaction.response.send_message = spy_send
    try:
        await Bank.setbalance.callback(
            cog, interaction, encode_account("nation", "Aurum"), 1_500
        )
    finally:
        bank_module.resolve_account = real_resolve

    row = await db.get_nation_by_id(nid)
    total = await db.fetchone(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions "
        "WHERE account_type = 'nation' AND account_id = ?",
        (nid,),
    )
    delta = await db.fetchone(
        "SELECT amount FROM transactions WHERE kind = 'setbalance' ORDER BY id DESC LIMIT 1"
    )
    check("setbalance writes the requested balance", int(row["balance"]) == 1_500,
          row["balance"])
    check("setbalance logs the fresh delta", int(delta["amount"]) == -500,
          delta["amount"])
    check("setbalance ledger still reconciles", int(total["s"]) == 1_500,
          total["s"])
    check("setbalance replies", bool(interaction.response.sent), interaction.response.sent)
    check("setbalance replies after its transaction commits",
          sent_in_tx == [False], sent_in_tx)


async def draw_checks(db):
    g = 2
    leader = FakeUser(42)
    kid = await db.create_kingdom(g, "Crown", leader.id)
    nid = await db.create_nation(g, "Member", 99)
    await db.set_nation_kingdom(nid, kid)
    await db.add_balance("kingdoms", kid, 10_000)

    # A nation that left the kingdom after asking must not be paid.
    draw_id = await db.create_pending_draw(g, kid, nid, 5_000, 99)
    view = DrawApprovalView(FakeBot(db), draw_id)
    interaction = FakeInteraction(leader, guild_id=g)
    await db.set_nation_kingdom(nid, None)
    await view._finalize(interaction, True)
    draw = await db.get_pending_draw(draw_id)
    check("draw from an ex-member is marked invalid", draw["status"] == "invalid",
          draw["status"])
    check("ex-member draw moves no credits",
          int((await db.get_kingdom_by_id(kid))["balance"]) == 10_000
          and int((await db.get_nation_by_id(nid))["balance"]) == 0,
          ((await db.get_kingdom_by_id(kid))["balance"],
           (await db.get_nation_by_id(nid))["balance"]))
    check("ex-member draw explains the refusal",
          "No Longer a Member" in (interaction.response.edited[-1].title or ""),
          interaction.response.edited[-1].title)

    # An in-kingdom draw still pays exactly once.
    await db.set_nation_kingdom(nid, kid)
    draw_ok = await db.create_pending_draw(g, kid, nid, 4_000, 99)
    view_ok = DrawApprovalView(FakeBot(db), draw_ok)
    interaction_ok = FakeInteraction(leader, guild_id=g)
    await view_ok._finalize(interaction_ok, True)
    check("valid draw is approved",
          (await db.get_pending_draw(draw_ok))["status"] == "approved",
          (await db.get_pending_draw(draw_ok))["status"])
    check("valid draw moves exactly the amount",
          int((await db.get_kingdom_by_id(kid))["balance"]) == 6_000
          and int((await db.get_nation_by_id(nid))["balance"]) == 4_000,
          ((await db.get_kingdom_by_id(kid))["balance"],
           (await db.get_nation_by_id(nid))["balance"]))

    # A resolved draw cannot be overwritten by a late failure write.
    overwritten = await db.claim_pending_draw(draw_ok, "failed")
    check("one-shot draw status cannot be overwritten", overwritten is False,
          overwritten)
    check("draw status stays approved",
          (await db.get_pending_draw(draw_ok))["status"] == "approved",
          (await db.get_pending_draw(draw_ok))["status"])

    # Creation petitions use the same one-shot guard.
    creation_id = await db.create_pending_creation(g, "nation", "Test", 99, 99)
    check("creation status claims once",
          await db.claim_pending_creation(creation_id, "failed") is True)
    check("creation status cannot be overwritten",
          await db.claim_pending_creation(creation_id, "approved") is False)
    check("creation status stays failed",
          (await db.get_pending_creation(creation_id))["status"] == "failed",
          (await db.get_pending_creation(creation_id))["status"])


async def decree_dm_check(db):
    before = await db.fetchone("SELECT COUNT(*) AS c FROM settings")
    check("get_settings(None) returns None", await db.get_settings(None) is None)
    interaction = FakeInteraction(FakeUser(7), guild_id=None)
    cog = Decree(FakeBot(db))
    await Decree.info.callback(cog, interaction)
    after = await db.fetchone("SELECT COUNT(*) AS c FROM settings")
    check("DM info refuses instead of crashing", bool(interaction.response.sent),
          interaction.response.sent)
    check("DM info does not create a ghost settings row",
          int(after["c"]) == int(before["c"]), (before["c"], after["c"]))


async def main():
    db = Database(os.path.join(tempfile.mkdtemp(), "bank.db"))
    await db.connect()
    try:
        await setbalance_check(db)
        await draw_checks(db)
        await decree_dm_check(db)
    finally:
        await db.close()
    print("\nFAILURES:", len(fails))
    for failure in fails:
        print(" -", failure)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print("ERROR:", type(exc).__name__, exc)
        raise SystemExit(1)
    raise SystemExit(1 if fails else 0)
