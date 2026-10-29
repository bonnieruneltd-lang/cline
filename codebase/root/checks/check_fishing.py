"""Pure and end-to-end checks for /fish and the Fishing Guild house account.

The end-to-end half drives the real cog callback with a throwaway SQLite
database, so no Discord login or live data is needed.
"""

import asyncio
import os
import random
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import config
import fishing
from database import Database, InsufficientFunds
from cogs.fishing import Fishing

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

    async def send_message(self, embed=None, ephemeral=False, **kw):
        self._sink.append((embed, ephemeral))


class FakeInteraction:
    def __init__(self, user, guild_id=1):
        self.user = user
        self.guild_id = guild_id
        self.sent = []
        self.response = FakeResp(self.sent)

    def text(self):
        embed, _ephemeral = self.sent[-1]
        parts = [embed.title or ""]
        for field in embed.fields:
            parts.append(f"{field.name}: {field.value}")
        if embed.description:
            parts.append(embed.description)
        return " | ".join(parts)


class FakeBot:
    def __init__(self, db):
        self.db = db


async def main():
    # ------------------------------------------------------------------
    # Pure fishing maths
    # ------------------------------------------------------------------
    rng = random.Random(20261027)
    seen_names = set()
    amounts = []
    for _ in range(500):
        name, payout = fishing.catch(rng)
        seen_names.add(name)
        amounts.append(payout)
        if not (config.FISHING_MIN_CREDITS <= payout <= config.FISHING_MAX_CREDITS):
            check("catch payout inside configured range", False, (name, payout))
            break
    else:
        check("catch payout inside configured range", True)
    check("catch names come from the configured list",
          seen_names <= set(config.FISHING_NAMES), seen_names - set(config.FISHING_NAMES))
    check("catch can produce the minimum", min(amounts) == config.FISHING_MIN_CREDITS,
          min(amounts))
    check("catch can produce the maximum", max(amounts) == config.FISHING_MAX_CREDITS,
          max(amounts))
    check("seeded catch replays identically",
          fishing.catch(random.Random(7)) == fishing.catch(random.Random(7)))

    check("a valid catch has no problem",
          fishing.catch_problem(payout=20, balance=0, house=20) is None)
    check("below-range payout refused",
          fishing.catch_problem(payout=19, balance=0, house=1_000) is not None)
    check("above-range payout refused",
          fishing.catch_problem(payout=101, balance=0, house=1_000) is not None)
    check("individual cap refused",
          fishing.catch_problem(payout=20, balance=config.INDIVIDUAL_MAX_CREDITS - 10,
                                house=1_000) is not None)
    check("short Fishing Guild refused",
          fishing.catch_problem(payout=20, balance=0, house=19) is not None)

    # ------------------------------------------------------------------
    # Fishing Guild balance guards
    # ------------------------------------------------------------------
    db = Database(os.path.join(tempfile.mkdtemp(), "fishing.db"))
    await db.connect()
    guild_id = 1

    check("new Fishing Guild starts empty", await db.get_fishing_balance(guild_id) == 0)
    check("funding the guild credits it",
          await db.credit_fishing_guild(guild_id, 1_000_000) == 1_000_000)
    await db.log_tx(guild_id=guild_id, kind="fishing_guild_fund",
                    account_type="fishing_guild", account_id=guild_id,
                    account_name="Fishing Guild", amount=1_000_000,
                    balance_after=1_000_000)
    try:
        await db.debit_fishing_guild(guild_id, 1_000_001)
        check("guild cannot be overdrawn", False)
    except InsufficientFunds:
        check("guild cannot be overdrawn", True)
    check("failed guild debit leaves the balance alone",
          await db.get_fishing_balance(guild_id) == 1_000_000)
    check("a guild debit returns the new balance",
          await db.debit_fishing_guild(guild_id, 250_000) == 750_000)
    await db.log_tx(guild_id=guild_id, kind="fishing_guild_withdraw",
                    account_type="fishing_guild", account_id=guild_id,
                    account_name="Fishing Guild", amount=-250_000,
                    balance_after=750_000)
    await db.credit_fishing_guild(guild_id, 250_000)
    await db.log_tx(guild_id=guild_id, kind="fishing_guild_fund",
                    account_type="fishing_guild", account_id=guild_id,
                    account_name="Fishing Guild", amount=250_000,
                    balance_after=1_000_000)

    # ------------------------------------------------------------------
    # End-to-end /fish
    # ------------------------------------------------------------------
    cog = Fishing(FakeBot(db))
    alice = FakeUser(10, "Alice")
    bob = FakeUser(20, "Bob")
    alice_row = await db.get_individual(guild_id, 10, "Alice")
    alice_added, _ = await db.credit_individual(guild_id, 10, 10_000)
    await db.log_tx(guild_id=guild_id, kind="individual_grant", account_type="individual",
                    account_id=alice_row["id"], account_name="Alice",
                    amount=alice_added, balance_after=10_000)
    bob_row = await db.get_individual(guild_id, 20, "Bob")
    bob_added, _ = await db.credit_individual(guild_id, 20, 10_000)
    await db.log_tx(guild_id=guild_id, kind="individual_grant", account_type="individual",
                    account_id=bob_row["id"], account_name="Bob",
                    amount=bob_added, balance_after=10_000)

    real_catch = fishing.catch
    fishing.catch = lambda rng=None: ("Test Fish", 20)
    try:
        house_before = await db.get_fishing_balance(guild_id)
        alice_before = int((await db.find_individual(guild_id, 10))["balance"])
        bob_before = int((await db.find_individual(guild_id, 20))["balance"])
        interaction = FakeInteraction(alice)
        await Fishing.fish.callback(cog, interaction)
        print("  fish reply:", interaction.text()[:240])
        check("fish result is public", interaction.sent[-1][1] is False, interaction.sent[-1])
        check("fish reply names the catch", "Test Fish" in interaction.text(), interaction.text())
        alice_after = int((await db.find_individual(guild_id, 10))["balance"])
        house_after = await db.get_fishing_balance(guild_id)
        check("fish credits the wallet", alice_after == alice_before + 20,
              (alice_before, alice_after))
        check("fish debits the Fishing Guild", house_after == house_before - 20,
              (house_before, house_after))
        check("fish conserves credits",
              alice_after + house_after + bob_before
              == alice_before + house_before + bob_before)
        rows = await db.fetchall(
            "SELECT * FROM transactions WHERE kind='fishing_catch' ORDER BY id"
        )
        check("fish writes two ledger rows", len(rows) == 2, len(rows))
        check("fish ledger rows net to zero",
              sum(int(row["amount"]) for row in rows) == 0,
              [int(row["amount"]) for row in rows])
        check("fish ledger has both account types",
              {row["account_type"] for row in rows} == {"individual", "fishing_guild"},
              {row["account_type"] for row in rows})
        player_ledger = await db.fetchall(
            "SELECT COALESCE(SUM(amount),0) AS s FROM transactions "
            "WHERE account_type='individual' AND account_id=?",
            (alice_row["id"],),
        )
        house_ledger = await db.fetchall(
            "SELECT COALESCE(SUM(amount),0) AS s FROM transactions "
            "WHERE account_type='fishing_guild' AND account_id=?",
            (guild_id,),
        )
        check("fish wallet reconciles with its ledger",
              alice_after == int(player_ledger[0]["s"]), (alice_after, player_ledger[0]["s"]))
        check("fish guild reconciles with its ledger",
              house_after == int(house_ledger[0]["s"]), (house_after, house_ledger[0]["s"]))

        # The cap is checked before any money moves.
        catch_rows_before = len(await db.fetchall(
            "SELECT * FROM transactions WHERE kind='fishing_catch'"))
        cap = config.INDIVIDUAL_MAX_CREDITS
        room = cap - alice_after
        added, _ = await db.credit_individual(guild_id, 10, room)
        await db.log_tx(guild_id=guild_id, kind="individual_grant", account_type="individual",
                        account_id=alice_row["id"], account_name="Alice",
                        amount=added, balance_after=cap)
        capped = FakeInteraction(alice)
        await Fishing.fish.callback(cog, capped)
        check("a cap-breaking catch is refused", "Catch Refused" in capped.text(), capped.text())
        check("cap refusal leaves the guild alone",
              await db.get_fishing_balance(guild_id) == house_after)
        check("cap refusal writes no catch rows",
              len(await db.fetchall("SELECT * FROM transactions WHERE kind='fishing_catch'"))
              == catch_rows_before)
    finally:
        fishing.catch = real_catch

    # A short Fishing Guild refuses before touching either balance.
    house_now = await db.get_fishing_balance(guild_id)
    await db.debit_fishing_guild(guild_id, house_now)
    await db.log_tx(guild_id=guild_id, kind="fishing_guild_withdraw",
                    account_type="fishing_guild", account_id=guild_id,
                    account_name="Fishing Guild", amount=-house_now,
                    balance_after=0)
    bob_before_short = int((await db.find_individual(guild_id, 20))["balance"])
    short = FakeInteraction(bob)
    await Fishing.fish.callback(cog, short)
    print("  short-guild reply:", short.text()[:180])
    check("a short Fishing Guild refuses the catch",
          "Catch Refused" in short.text(), short.text())
    check("short refusal leaves Bob alone",
          int((await db.find_individual(guild_id, 20))["balance"]) == bob_before_short)
    check("short refusal leaves the guild empty",
          await db.get_fishing_balance(guild_id) == 0)

    # A user without a wallet is told to /daily before any catch is rolled.
    no_wallet = FakeInteraction(FakeUser(99, "Newcomer"))
    await Fishing.fish.callback(cog, no_wallet)
    check("fishing without a wallet is refused",
          "No Wallet" in no_wallet.text(), no_wallet.text())

    # ------------------------------------------------------------------
    # /fishingguild balance, fund and withdraw
    # ------------------------------------------------------------------
    admin = FakeUser(1, "Admin")
    admin.guild_permissions = SimpleNamespace(administrator=True)
    funded = FakeInteraction(admin)
    await Fishing.fund.callback(cog, funded, 500)
    check("admin can fund the Fishing Guild",
          "Fishing Guild Funded" in funded.text(), funded.text())
    check("funding adds to the guild", await db.get_fishing_balance(guild_id) == 500)
    withdrawn = FakeInteraction(admin)
    await Fishing.withdraw.callback(cog, withdrawn, 200)
    check("admin can withdraw from the Fishing Guild",
          "Fishing Guild Withdrawn" in withdrawn.text(), withdrawn.text())
    check("withdraw removes from the guild", await db.get_fishing_balance(guild_id) == 300)
    over = FakeInteraction(admin)
    await Fishing.withdraw.callback(cog, over, 10**9)
    check("an over-withdrawal is refused", "Insufficient Funds" in over.text(), over.text())
    check("over-withdrawal leaves the guild alone",
          await db.get_fishing_balance(guild_id) == 300)

    shown = FakeInteraction(admin)
    await Fishing.balance.callback(cog, shown)
    check("the guild balance card is public", shown.sent[-1][1] is False, shown.sent[-1])
    check("the guild balance card names the guild", "Fishing Guild" in shown.text(),
          shown.text())

    final_house = await db.get_fishing_balance(guild_id)
    house_ledger = await db.fetchall(
        "SELECT COALESCE(SUM(amount),0) AS s FROM transactions "
        "WHERE account_type='fishing_guild' AND account_id=?",
        (guild_id,),
    )
    check("the Fishing Guild reconciles with its ledger",
          final_house == int(house_ledger[0]["s"]), (final_house, house_ledger[0]["s"]))

    await db.close()
    print("\nFAILURES:", len(fails))
    for failure in fails:
        print(" -", failure)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    asyncio.run(main())

