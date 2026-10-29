"""Odds, stake guards and money handling for /individual coinflip."""
import asyncio, os, sys, tempfile
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import coinflip
import config
from database import Database
from cogs.individual import Individuals

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


class Side:
    """Stands in for app_commands.Choice[str]."""
    def __init__(self, value):
        self.value = value
        self.name = value.title()


def pure_checks():
    # The win rate must be exactly the configured rate: over every possible
    # roll, 5000 of 10,000 outcomes win at the default 50%.
    wins = sum(1 for r in range(10_000) if coinflip.resolve(coinflip.HEADS, r)[0])
    check("win rate is 50% of rolls", wins == 5_000, wins)
    check("win rate matches config", wins == config.COINFLIP_WIN_BP, wins)
    losses = 10_000 - wins
    check("loss rate is 50% of rolls", losses == 5_000, losses)

    check("roll 4999 wins", coinflip.resolve(coinflip.HEADS, 4_999)[0] is True)
    check("roll 5000 loses", coinflip.resolve(coinflip.HEADS, 5_000)[0] is False)
    check("roll 9999 loses", coinflip.resolve(coinflip.HEADS, 9_999)[0] is False)

    # The call picks the face shown, never the odds.
    for side in coinflip.SIDES:
        wins_side = sum(1 for r in range(10_000) if coinflip.resolve(side, r)[0])
        check(f"{side} wins {wins_side} times", wins_side == 5_000, wins_side)

    won, landed = coinflip.resolve(coinflip.HEADS, 0)
    check("win shows the called face", (won, landed) == (True, coinflip.HEADS), landed)
    won, landed = coinflip.resolve(coinflip.HEADS, 9_999)
    check("loss shows the other face", (won, landed) == (False, coinflip.TAILS), landed)
    won, landed = coinflip.resolve(coinflip.TAILS, 0)
    check("tails win shows tails", (won, landed) == (True, coinflip.TAILS), landed)
    won, landed = coinflip.resolve(coinflip.TAILS, 9_999)
    check("tails loss shows heads", (won, landed) == (False, coinflip.HEADS), landed)

    check("gross payout doubles", coinflip.gross_payout(1_000) == 2_000)
    check("win nets +stake", coinflip.net_change(1_000, True) == 1_000)
    check("loss nets -stake", coinflip.net_change(1_000, False) == -1_000)

    # A 50/50 coin at 2x is a fair pass-through: the house edge is zero.
    check("house edge is 0%", coinflip.house_edge_bp() == 0, coinflip.house_edge_bp())
    check("house edge is not a faucet", coinflip.house_edge_bp() <= 0, coinflip.house_edge_bp())

    check("rejects unknown side", _raises(lambda: coinflip.resolve("edge", 0)))
    check("rejects roll 10000", _raises(lambda: coinflip.resolve(coinflip.HEADS, 10_000)))
    check("rejects negative roll", _raises(lambda: coinflip.resolve(coinflip.HEADS, -1)))

    check("min stake enforced",
          coinflip.stake_problem(config.COINFLIP_MIN_STAKE - 1, 10**6) is not None)
    check("min stake accepted",
          coinflip.stake_problem(config.COINFLIP_MIN_STAKE, 10**6) is None)
    check("max stake enforced",
          coinflip.stake_problem(config.COINFLIP_MAX_STAKE + 1, 10**9) is not None)
    check("overdraw refused", coinflip.stake_problem(5_000, 4_999) is not None)
    check("cap room enforced",
          coinflip.stake_problem(1_000, config.INDIVIDUAL_MAX_CREDITS) is not None)
    check("cap room respected",
          coinflip.stake_problem(1_000, config.INDIVIDUAL_MAX_CREDITS - 999) is not None)
    check("exact cap room allowed",
          coinflip.stake_problem(1_000, config.INDIVIDUAL_MAX_CREDITS - 1_000) is None)
    check("label reads 50", coinflip.win_label() == "50", coinflip.win_label())


def _raises(fn):
    try:
        fn()
    except ValueError:
        return True
    except Exception:
        return False
    return False


async def cog_checks():
    db = Database(os.path.join(tempfile.mkdtemp(), "flip.db"))
    await db.connect()
    g = 1
    cog = Individuals(FakeBot(db))
    alice = FakeUser(10, "Alice")

    # Seed the wallet the way /daily does: balance and ledger together, so the
    # reconciliation check below compares like with like.
    alice_row = await db.get_individual(g, 10, "Alice")
    await db.credit_individual(g, 10, 10_000)
    await db.log_tx(
        guild_id=g,
        kind="individual_grant",
        account_type="individual",
        account_id=alice_row["id"],
        account_name="Alice",
        amount=10_000,
        balance_after=10_000,
        actor_id=10,
    )
    # The Casino is the counterparty now: with an empty house every bet is
    # refused, so fund it before playing.
    await db.credit_casino(g, 500_000_000)

    real_roll = coinflip.roll
    try:
        # --- a forced win -------------------------------------------------
        coinflip.roll = lambda rng=None: 0
        i = FakeInteraction(alice)
        await Individuals.coinflip.callback(cog, i, Side("heads"), "1,000")
        print("  win reply:", i.text())
        check("win reply is success", "You Win" in i.text(), i.text())
        check("win card invites spectators", "The Floor" in i.text(), i.text())
        check("win credits net +stake",
              int((await db.find_individual(g, 10))["balance"]) == 11_000,
              (await db.find_individual(g, 10))["balance"])

        # --- a forced loss ------------------------------------------------
        coinflip.roll = lambda rng=None: 9_999
        i = FakeInteraction(alice)
        await Individuals.coinflip.callback(cog, i, Side("tails"), "1,000")
        print("  loss reply:", i.text())
        check("loss reply is an error", "You Lose" in i.text(), i.text())
        check("loss removes stake",
              int((await db.find_individual(g, 10))["balance"]) == 10_000,
              (await db.find_individual(g, 10))["balance"])
    finally:
        coinflip.roll = real_roll

    rows = await db.fetchall("SELECT * FROM transactions WHERE kind='individual_coinflip'")
    player_rows = [r for r in rows if r["account_type"] == "individual"]
    house_rows = [r for r in rows if r["account_type"] == "casino"]
    # The stake is recorded when it is held and the payout when it is paid, so a
    # win writes two rows and a loss writes only the stake.
    check("two stake rows and one payout row for a win and a loss",
          len(player_rows) == 3 and len([r for r in player_rows if int(r["amount"]) > 0]) == 1,
          [(r["account_type"], int(r["amount"])) for r in player_rows])
    check("player ledger nets to zero over a win and a loss",
          sum(int(r["amount"]) for r in player_rows) == 0,
          sum(int(r["amount"]) for r in player_rows))
    check("every player row has a mirror on the house side",
          len(house_rows) == len(player_rows), (len(house_rows), len(player_rows)))
    check("the house ledger is the exact mirror of the player ledger",
          sum(int(r["amount"]) for r in house_rows)
          == -sum(int(r["amount"]) for r in player_rows),
          (sum(int(r["amount"]) for r in house_rows),
           sum(int(r["amount"]) for r in player_rows)))

    balance = int((await db.find_individual(g, 10))["balance"])
    ledger = await db.fetchall(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions "
        "WHERE account_type='individual' AND account_id=?",
        ((await db.find_individual(g, 10))["id"],),
    )
    check("wallet reconciles with the ledger",
          balance == int(ledger[0]["s"]), (balance, ledger[0]["s"]))

    # --- refusals must not move money, and must not roll the dice --------
    await db.get_individual(g, 50, "Rich")
    rich = FakeUser(50, "Rich")
    await db.credit_individual(g, 50, 50_000_000)
    before = int((await db.find_individual(g, 10))["balance"])
    calls = []
    coinflip.roll = lambda rng=None: calls.append(1) or 0
    try:
        # Below the minimum stake.
        i = FakeInteraction(alice)
        await Individuals.coinflip.callback(cog, i, Side("heads"), str(config.COINFLIP_MIN_STAKE - 1))
        check("small stake refused", "Bet Refused" in i.text(), i.text())

        # Above the balance: the message must name the funds, not the ceiling.
        i = FakeInteraction(alice)
        await Individuals.coinflip.callback(cog, i, Side("heads"), "9,999,999")
        check("overdraw refused", "Bet Refused" in i.text(), i.text())
        check("overdraw names the funds", "do not hold" in i.text(), i.text())

        # Above the maximum stake, from a wallet rich enough to cover it.
        i = FakeInteraction(rich)
        await Individuals.coinflip.callback(cog, i, Side("heads"), f"{config.COINFLIP_MAX_STAKE + 1:,}")
        check("oversized stake refused", "Bet Refused" in i.text(), i.text())
        check("oversized stake names the ceiling", "maximum stake" in i.text(), i.text())

        check("refusals never rolled the coin", calls == [], calls)
    finally:
        coinflip.roll = real_roll
    check("refusals left the balance alone",
          int((await db.find_individual(g, 10))["balance"]) == before)

    # --- a wallet at the cap cannot bet at all --------------------------
    await db.get_individual(g, 40, "Full")
    full = FakeUser(40, "Full")
    await db.credit_individual(g, 40, config.INDIVIDUAL_MAX_CREDITS)
    i = FakeInteraction(full)
    await Individuals.coinflip.callback(cog, i, Side("heads"), "1,000")
    check("capped wallet refused", "Bet Refused" in i.text(), i.text())
    check("capped wallet intact",
          int((await db.find_individual(g, 40))["balance"]) == config.INDIVIDUAL_MAX_CREDITS)

    # --- no wallet ------------------------------------------------------
    i = FakeInteraction(FakeUser(99, "Nobody"))
    await Individuals.coinflip.callback(cog, i, Side("heads"), "1,000")
    check("no-wallet refused", "No Wallet" in i.text(), i.text())

    # --- guild only -----------------------------------------------------
    i = FakeInteraction(alice, guild_id=None)
    await Individuals.coinflip.callback(cog, i, Side("heads"), "1,000")
    check("guild-only enforced", "Guild Only" in i.text(), i.text())

    # --- 'all' is accepted and leaves a live wallet ---------------------
    coinflip.roll = lambda rng=None: 9_999
    try:
        i = FakeInteraction(alice)
        await Individuals.coinflip.callback(cog, i, Side("heads"), "all")
        check("all-in accepted", "You Lose" in i.text(), i.text())
        check("all-in empties the wallet",
              int((await db.find_individual(g, 10))["balance"]) == 0,
              (await db.find_individual(g, 10))["balance"])
    finally:
        coinflip.roll = real_roll

    await db.close()


def main():
    print("-- pure odds --")
    pure_checks()
    print("-- cog money handling --")
    asyncio.run(cog_checks())
    print(f"\nFAILURES: {len(fails)}")
    if fails:
        for f in fails:
            print("  -", f)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
