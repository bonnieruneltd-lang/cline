"""Wheel layout, pricing, guards and money handling for /roulette.

    python3 checks/check_roulette.py

Every bet shape is walked across all 37 pockets, so the 2.70% edge is proved by
enumeration rather than asserted. The second half drives the real cog against a
throwaway database: a forced pocket proves each settlement branch, and the
house and wallet are checked to move by equal and opposite amounts.
"""
import asyncio, os, random, sys, tempfile
from fractions import Fraction

from discord import app_commands

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import config
import roulette
from cogs.games import Casino
from database import Database

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
    async def edit_message(self, embed=None, view=None, **kw):
        self._sink.append(embed)


class FakeInteraction:
    def __init__(self, user, guild_id=1):
        self.user = user
        self.guild_id = guild_id
        self.sent = []
        self.response = FakeResp(self.sent)

    async def original_response(self):
        return None

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


def choice(value):
    """The app_commands.Choice the slash command would hand the callback."""
    return app_commands.Choice(name=value.title(), value=value)


# ---------------------------------------------------------------------------
# The layout
# ---------------------------------------------------------------------------
def layout_checks():
    pockets = set(range(config.ROULETTE_POCKETS))
    check("the wheel has 37 pockets", config.ROULETTE_POCKETS == 37)
    check("every pocket is 0-36", pockets == set(range(37)))
    check("red and black tile 1-36 exactly",
          roulette.RED_NUMBERS | roulette.BLACK_NUMBERS == set(range(1, 37)))
    check("red and black do not overlap",
          not (roulette.RED_NUMBERS & roulette.BLACK_NUMBERS))
    check("there are 18 red", len(roulette.RED_NUMBERS) == 18)
    check("there are 18 black", len(roulette.BLACK_NUMBERS) == 18)
    check("zero is in neither colour",
          0 not in roulette.RED_NUMBERS and 0 not in roulette.BLACK_NUMBERS)

    even = roulette.EVEN_MONEY["even"]
    odd = roulette.EVEN_MONEY["odd"]
    check("odd and even tile 1-36 exactly", even | odd == set(range(1, 37)))
    check("odd and even do not overlap", not (even & odd))
    check("there are 18 even and 18 odd", len(even) == 18 and len(odd) == 18)
    check("zero is neither odd nor even", 0 not in even and 0 not in odd)
    check("low and high tile 1-36 exactly",
          roulette.EVEN_MONEY["low"] | roulette.EVEN_MONEY["high"] == set(range(1, 37)))
    check("there are 18 low and 18 high",
          len(roulette.EVEN_MONEY["low"]) == 18
          and len(roulette.EVEN_MONEY["high"]) == 18)

    dozens = list(roulette.DOZENS.values())
    check("the dozens tile 1-36 exactly", set().union(*dozens) == set(range(1, 37)))
    check("the dozens do not overlap",
          sum(len(dozen) for dozen in dozens) == len(set().union(*dozens)))
    check("each dozen holds 12 numbers", all(len(dozen) == 12 for dozen in dozens))
    columns = list(roulette.COLUMNS.values())
    check("the columns tile 1-36 exactly", set().union(*columns) == set(range(1, 37)))
    check("the columns do not overlap",
          sum(len(column) for column in columns) == len(set().union(*columns)))
    check("each column holds 12 numbers", all(len(column) == 12 for column in columns))
    check("column 1 is 1, 4, 7 ... 34",
          sorted(roulette.COLUMNS["column1"]) == list(range(1, 37, 3)))
    check("column 3 is 3, 6, 9 ... 36",
          sorted(roulette.COLUMNS["column3"]) == list(range(3, 37, 3)))
    check("zero is in no dozen and no column",
          all(0 not in dozen for dozen in dozens)
          and all(0 not in column for column in columns))

    check("0 is green", roulette.colour(0) == "green")
    check("17 is black", roulette.colour(17) == "black")
    check("18 is red", roulette.colour(18) == "red")
    check("the colours cover every pocket",
          all(roulette.colour(p) in ("red", "black", "green") for p in pockets))
    check("pocket labels render", roulette.pocket_label(0) == "0 · green",
          roulette.pocket_label(0))


# ---------------------------------------------------------------------------
# Pricing every bet shape
# ---------------------------------------------------------------------------
def pricing_checks():
    priced = []
    for label, value in roulette.CHOICES:
        bet = roulette.make_bet(value, 0 if value == roulette.STRAIGHT else None)
        hits = sum(
            1 for pocket in range(config.ROULETTE_POCKETS) if roulette.resolve(bet, pocket)
        )
        priced.append((label, hits, len(bet.numbers), roulette.expected_return(bet)))
    check("every bet's coverage matches its pockets",
          all(hits == pockets for _, hits, pockets, _ in priced), priced)
    check("every bet shape returns exactly 36/37",
          all(ret == Fraction(36, 37) for _, _, _, ret in priced), priced)
    check("every bet shape carries the same 2.70% edge",
          all(roulette.house_edge(roulette.make_bet(v, 0 if v == roulette.STRAIGHT else None))
              == Fraction(1, 37) for _, v in roulette.CHOICES))
    print("  " + " ".join(f"{label.split('(')[0].strip()}={float(ret):.3f}"
                          for label, _, _, ret in priced[:4]))

    red = roulette.make_bet("red")
    dozen = roulette.make_bet("1st12")
    straight = roulette.make_bet(roulette.STRAIGHT, 17)
    check("red covers 18 pockets", len(red.numbers) == 18)
    check("a dozen covers 12 pockets", len(dozen.numbers) == 12)
    check("a straight covers 1 pocket", len(straight.numbers) == 1)
    check("a straight on 0 is allowed", roulette.make_bet(roulette.STRAIGHT, 0).numbers == {0})
    check("even money pays 2x gross", red.multiple == 2)
    check("a dozen pays 3x gross", dozen.multiple == 3)
    check("a straight pays 36x gross", straight.multiple == 36)
    check("even money is 1:1", red.odds_label == "1:1", red.odds_label)
    check("a dozen is 2:1", dozen.odds_label == "2:1", dozen.odds_label)
    check("a straight is 35:1", straight.odds_label == "35:1", straight.odds_label)
    check("max_gross reserves the straight up",
          roulette.max_gross(1_000) == 36_000, roulette.max_gross(1_000))
    check("a win pays the multiple", roulette.gross_payout(red, 1_000, True) == 2_000)
    check("a loss pays nothing", roulette.gross_payout(red, 1_000, False) == 0)
    check("net change on a win", roulette.net_change(red, 1_000, True) == 1_000)
    check("net change on a loss", roulette.net_change(red, 1_000, False) == -1_000)

    # The zero is the house edge: it takes every outside bet.
    for kind in ("red", "black", "odd", "even", "low", "high",
                 "1st12", "2nd12", "3rd12", "column1", "column2", "column3"):
        if roulette.resolve(roulette.make_bet(kind), 0):
            check(f"zero takes the {kind} bet", False)
            break
    else:
        check("zero takes every outside bet", True)
    check("a straight on 0 pays on zero",
          roulette.resolve(roulette.make_bet(roulette.STRAIGHT, 0), 0) is True)
    check("a straight on 17 loses on zero",
          roulette.resolve(roulette.make_bet(roulette.STRAIGHT, 17), 0) is False)

    check("the bet menu lists every shape",
          len(roulette.bet_table().splitlines()) == len(roulette.CHOICES))
    check("the choice values are the bet kinds",
          [value for _, value in roulette.CHOICES] == list(roulette.BET_KINDS))


def _raises(fn):
    try:
        fn()
    except ValueError:
        return True
    except Exception:
        return False
    return False


def bet_checks():
    check("an unknown bet is refused", _raises(lambda: roulette.make_bet("blue")))
    check("a straight without a number is refused",
          _raises(lambda: roulette.make_bet(roulette.STRAIGHT)))
    check("a straight above 36 is refused",
          _raises(lambda: roulette.make_bet(roulette.STRAIGHT, 37)))
    check("a negative straight is refused",
          _raises(lambda: roulette.make_bet(roulette.STRAIGHT, -1)))
    check("a number on an even-money bet is refused",
          _raises(lambda: roulette.make_bet("red", 5)))
    check("an out-of-range pocket is refused",
          _raises(lambda: roulette.resolve(roulette.make_bet("red"), 37)))


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def guard_checks():
    red = roulette.make_bet("red")
    straight = roulette.make_bet(roulette.STRAIGHT, 7)
    check("minimum bet refused",
          roulette.stake_problem(red, config.ROULETTE_MIN_STAKE - 1, 10_000) is not None)
    check("minimum bet accepted",
          roulette.stake_problem(red, config.ROULETTE_MIN_STAKE, 10_000) is None)
    check("maximum bet refused",
          roulette.stake_problem(red, config.ROULETTE_MAX_STAKE + 1, 10**12) is not None)
    check("a bet above the balance is refused",
          roulette.stake_problem(red, 5_000, 1_000) is not None)

    # The cap depends on the bet, not just the stake: 36x is the one that breaks.
    wallet = config.INDIVIDUAL_MAX_CREDITS - 1_000_000
    check("a straight that would breach the cap is refused",
          roulette.stake_problem(straight, 100_000, wallet) is not None,
          roulette.stake_problem(straight, 100_000, wallet))
    check("the refusal names the cap",
          "cap" in (roulette.stake_problem(straight, 100_000, wallet) or "").lower())
    check("the same stake on an even-money bet is fine",
          roulette.stake_problem(red, 100_000, wallet) is None,
          roulette.stake_problem(red, 100_000, wallet))

    rng = random.Random(20261106)
    spins = [roulette.spin(rng) for _ in range(5_000)]
    check("every seeded spin is a pocket",
          all(0 <= pocket <= 36 for pocket in spins))
    check("5,000 seeded spins reach every pocket",
          set(spins) == set(range(37)), sorted(set(range(37)) - set(spins)))
    uneven = max(spins.count(p) for p in range(37)) - min(spins.count(p) for p in range(37))
    check("the wheel is not wildly biased", uneven < 60, uneven)


# ---------------------------------------------------------------------------
# The cog, against a throwaway database
# ---------------------------------------------------------------------------
async def cog_checks():
    db = Database(os.path.join(tempfile.mkdtemp(), "roulette.db"))
    await db.connect()
    g = 1
    cog = Casino(FakeBot(db))
    alice = FakeUser(10, "Alice")

    row = await db.get_individual(g, 10, "Alice")
    await db.credit_individual(g, 10, 1_000)
    await db.credit_casino(g, 500_000_000)
    await db.log_tx(
        guild_id=g, kind="individual_grant", account_type="individual",
        account_id=row["id"], account_name="Alice", amount=1_000,
        balance_after=1_000, actor_id=10,
    )
    start_total = 1_000 + 500_000_000

    real_spin = roulette.spin
    try:
        # --- an even-money winner on 17 (black) -------------------------
        roulette.spin = lambda rng=None: 17
        i = FakeInteraction(alice)
        await Casino.roulette.callback(cog, i, "1000", choice("black"))
        print("  win reply:", i.text()[:110])
        check("win card is a roulette win", "ROULETTE · You Win" in i.text(), i.text())
        check("win card shows the pocket", "17 (black)" in i.text(), i.text())
        check("win card shows the odds", "1:1" in i.text(), i.text())
        check("even money doubles the stake",
              int((await db.find_individual(g, 10))["balance"]) == 2_000,
              (await db.find_individual(g, 10))["balance"])
        house = await db.get_casino_balance(g)
        check("the house paid the win", house == 500_000_000 - 1_000, house)

        # --- the zero takes an outside bet ------------------------------
        roulette.spin = lambda rng=None: 0
        i = FakeInteraction(alice)
        await Casino.roulette.callback(cog, i, "1000", choice("red"))
        print("  zero reply:", i.text()[:110])
        check("zero loses the bet", "ROULETTE · You Lose" in i.text(), i.text())
        check("the card explains the zero",
              "green zero" in i.text().lower(), i.text())
        check("the stake is gone",
              int((await db.find_individual(g, 10))["balance"]) == 1_000,
              (await db.find_individual(g, 10))["balance"])

        # --- a straight up hit pays 36x ---------------------------------
        roulette.spin = lambda rng=None: 17
        i = FakeInteraction(alice)
        await Casino.roulette.callback(cog, i, "1000", choice(roulette.STRAIGHT), 17)
        print("  straight reply:", i.text()[:110])
        check("straight card is a win", "ROULETTE · You Win" in i.text(), i.text())
        check("straight up pays 35:1", "35:1" in i.text(), i.text())
        check("the wallet holds 36x",
              int((await db.find_individual(g, 10))["balance"]) == 36_000,
              (await db.find_individual(g, 10))["balance"])

        # --- refusals must not spin or move money -----------------------
        calls = []
        roulette.spin = lambda rng=None: calls.append(1) or 17
        before = int((await db.find_individual(g, 10))["balance"])

        i = FakeInteraction(alice)
        await Casino.roulette.callback(cog, i, "1000", choice("red"), 5)
        check("a number on a colour bet is refused", "Invalid Bet" in i.text(), i.text())
        check("the refusal explains the number", "straight" in i.text().lower(), i.text())

        i = FakeInteraction(alice)
        await Casino.roulette.callback(cog, i, "1000", choice(roulette.STRAIGHT))
        check("a straight without a number is refused",
              "Invalid Bet" in i.text(), i.text())

        i = FakeInteraction(alice)
        await Casino.roulette.callback(cog, i, "10", choice("red"))
        check("a bet below the minimum is refused", "Bet Refused" in i.text(), i.text())

        # The cap depends on the bet: a straight can break it, red cannot.
        await db.get_individual(g, 30, "Whale")
        await db.credit_individual(g, 30, config.INDIVIDUAL_MAX_CREDITS - 1_000_000)
        i = FakeInteraction(FakeUser(30, "Whale"))
        await Casino.roulette.callback(cog, i, "100000", choice(roulette.STRAIGHT), 7)
        check("a cap-breaking straight is refused", "Bet Refused" in i.text(), i.text())
        check("the cap is named", "cap" in i.text().lower(), i.text())
        check("the whale was not charged",
              int((await db.find_individual(g, 30))["balance"])
              == config.INDIVIDUAL_MAX_CREDITS - 1_000_000)

        # --- a house that cannot cover a straight up --------------------
        drained = await db.get_casino_balance(g)
        await db.debit_casino(g, drained)
        i = FakeInteraction(alice)
        await Casino.roulette.callback(cog, i, "1000", choice("red"))
        check("a house that cannot cover refuses", "Bet Refused" in i.text(), i.text())
        check("the refusal explains cover",
              "cannot cover" in i.text().lower(), i.text())
        await db.credit_casino(g, drained)
        check("refusals never spun", calls == [], calls)
        check("refusals left the wallet alone",
              int((await db.find_individual(g, 10))["balance"]) == before)

        # --- no wallet / no guild --------------------------------------
        i = FakeInteraction(FakeUser(99, "Nobody"))
        await Casino.roulette.callback(cog, i, "1000", choice("red"))
        check("no wallet refused", "No Wallet" in i.text(), i.text())

        i = FakeInteraction(alice, guild_id=None)
        await Casino.roulette.callback(cog, i, "1000", choice("red"))
        check("guild-only enforced", "Guild Only" in i.text(), i.text())
    finally:
        roulette.spin = real_spin

    house = await db.get_casino_balance(g)
    total = int((await db.find_individual(g, 10))["balance"]) + house
    check("credits are conserved", total == start_total, (total, start_total))

    rows = await db.fetchall("SELECT * FROM transactions WHERE kind='individual_roulette'")
    player_rows = [r for r in rows if r["account_type"] == "individual"]
    house_rows = [r for r in rows if r["account_type"] == "casino"]
    check("stake and payout rows both exist",
          sorted(int(r["amount"]) for r in player_rows)
          == [-1_000, -1_000, -1_000, 2_000, 36_000],
          [int(r["amount"]) for r in player_rows])
    check("the house ledger mirrors the player ledger",
          sum(int(r["amount"]) for r in house_rows)
          == -sum(int(r["amount"]) for r in player_rows))
    ledger = await db.fetchall(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions "
        "WHERE account_type='individual' AND account_id=?", (row["id"],),
    )
    check("the wallet reconciles with its ledger",
          int((await db.find_individual(g, 10))["balance"]) == int(ledger[0]["s"]),
          (int((await db.find_individual(g, 10))["balance"]), int(ledger[0]["s"])))

    await db.close()


def main():
    print("-- layout --")
    layout_checks()
    print("-- pricing --")
    pricing_checks()
    print("-- bets --")
    bet_checks()
    print("-- guards --")
    guard_checks()
    print("-- cog money handling --")
    asyncio.run(cog_checks())
    print(f"\nFAILURES: {len(fails)}")
    for f in fails:
        print("  -", f)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
