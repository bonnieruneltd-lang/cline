"""Paytable, odds, guards and money handling for /slots.

    python3 checks/check_slots.py

The machine prices itself: every one of the 40^3 reel outcomes is enumerated and
compared with ``slots.expected_return()``, so a mistyped weight or payout cannot
sneak through as a silent faucet. The second half drives the real cog against a
throwaway database and asserts the house and the wallet always move by equal and
opposite amounts.
"""
import asyncio, os, random, sys, tempfile
from fractions import Fraction
from itertools import product
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import config
import slots
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


# ---------------------------------------------------------------------------
# The strip and the paytable
# ---------------------------------------------------------------------------
def pure_checks():
    check("strip holds one rung per weight",
          slots.TOTAL_WEIGHT == sum(s.weight for s in slots.SYMBOLS),
          slots.TOTAL_WEIGHT)
    check("strip maps every rung to a symbol",
          len(slots.STRIP) == slots.TOTAL_WEIGHT
          and all(0 <= index < len(slots.SYMBOLS) for index in slots.STRIP))
    for symbol in slots.SYMBOLS:
        rungs = sum(1 for index in slots.STRIP if slots.SYMBOLS[index] == symbol)
        check(f"{symbol.name} sits on {symbol.weight} rungs", rungs == symbol.weight)

    # Brute force: every equally likely reel outcome, priced.
    wins = 0
    paid = 0
    total = slots.TOTAL_WEIGHT ** 3
    for i, j, k in product(range(slots.TOTAL_WEIGHT), repeat=3):
        reels = (slots.SYMBOLS[slots.STRIP[i]], slots.SYMBOLS[slots.STRIP[j]],
                 slots.SYMBOLS[slots.STRIP[k]])
        multiple = slots.multiplier(reels)
        if multiple:
            wins += 1
            paid += multiple
    check("brute-force return matches expected_return",
          Fraction(paid, total) == slots.expected_return(),
          (Fraction(paid, total), slots.expected_return()))
    check("brute-force win rate matches win_chance",
          Fraction(wins, total) == slots.win_chance(),
          (Fraction(wins, total), slots.win_chance()))
    print(f"  return {float(slots.expected_return()):.4f} "
          f"edge {float(slots.house_edge()):.4f} win {wins}/{total}")

    # A sink, but not a swindle: the edge has to stay in a sane band.
    check("house edge is positive", slots.house_edge() > 0, slots.house_edge())
    check("house edge stays under 20%", slots.house_edge() < Fraction(1, 5),
          slots.house_edge())

    top = slots.MAX_PAYOUT_SYMBOL
    cherry, lemon, bell = slots.SYMBOLS[0], slots.SYMBOLS[1], slots.SYMBOLS[2]
    for symbol in slots.SYMBOLS:
        if slots.multiplier((symbol, symbol, symbol)) != symbol.payout:
            check(f"three {symbol.name} pays {symbol.payout}", False)
            break
    else:
        check("every three of a kind pays its own multiple", True)
    check("a pair pays nothing", slots.multiplier((cherry, cherry, lemon)) == 0)
    check("three different symbols pay nothing",
          slots.multiplier((cherry, lemon, bell)) == 0)
    check("order does not matter",
          slots.multiplier((lemon, cherry, cherry))
          == slots.multiplier((cherry, cherry, lemon)))
    check("won() is the multiplier predicate",
          slots.won((top, top, top)) is True
          and slots.won((cherry, lemon, cherry)) is False)
    check("gross payout is multiple times stake",
          slots.gross_payout((top, top, top), 1_000) == slots.MAX_PAYOUT * 1_000)
    check("a loss pays nothing", slots.gross_payout((cherry, lemon, cherry), 1_000) == 0)
    check("max_gross reserves the jackpot",
          slots.max_gross(1_000) == slots.MAX_PAYOUT * 1_000, slots.max_gross(1_000))
    check("net change on a win",
          slots.net_change((top, top, top), 1_000) == slots.MAX_PAYOUT * 1_000 - 1_000)
    check("net change on a loss",
          slots.net_change((cherry, lemon, cherry), 1_000) == -1_000)

    check("label renders the machine line",
          slots.label((cherry, cherry, cherry)).count("│") == 2,
          slots.label((cherry, cherry, cherry)))
    check("names says three Cherries",
          slots.names((cherry, cherry, cherry)) == "three Cherries",
          slots.names((cherry, cherry, cherry)))
    check("names says no match", slots.names((cherry, lemon, cherry)) == "no match")
    check("partial spots the near miss",
          slots.partial((cherry, cherry, lemon)) == "two Cherries",
          slots.partial((cherry, cherry, lemon)))
    check("a win has no near miss", slots.partial((cherry, cherry, cherry)) is None)
    check("no pair is no near miss", slots.partial((cherry, lemon, bell)) is None)
    check("paytable lists every symbol",
          len(slots.paytable().splitlines()) == len(slots.SYMBOLS))
    check("odds label renders", slots.chance_label().startswith("1 in"), slots.chance_label())
    check("edge label renders", slots.edge_label().endswith("%"), slots.edge_label())


def guard_checks():
    check("minimum stake refused",
          slots.stake_problem(config.SLOTS_MIN_STAKE - 1, 10_000) is not None)
    check("minimum stake accepted",
          slots.stake_problem(config.SLOTS_MIN_STAKE, 10_000) is None)
    check("maximum stake refused",
          slots.stake_problem(config.SLOTS_MAX_STAKE + 1, 10**12) is not None)
    check("stake above balance refused", slots.stake_problem(5_000, 1_000) is not None)

    # A jackpot-sized stake on a near-cap wallet has to be refused up front.
    whale = config.INDIVIDUAL_MAX_CREDITS - 1_000
    check("cap-breaking spin refused", slots.stake_problem(1_000, whale) is not None,
          slots.stake_problem(1_000, whale))
    check("the refusal names the cap",
          "cap" in (slots.stake_problem(1_000, whale) or "").lower())
    # The same wallet further from the cap can still spin small.
    richer = config.INDIVIDUAL_MAX_CREDITS - 100_000
    check("a small spin from a near-cap wallet is fine",
          slots.stake_problem(100, richer) is None, slots.stake_problem(100, richer))

    # A seeded run must actually pay at the configured rate.
    rng = random.Random(20261106)
    spins = [slots.spin(rng) for _ in range(20_000)]
    paid = sum(1 for reels in spins if slots.won(reels))
    rate = float(slots.win_chance())
    expected = rate * len(spins)
    sigma = (len(spins) * rate * (1 - rate)) ** 0.5
    check("seeded 20,000 spins land within 4 sigma of the pay rate",
          abs(paid - expected) < 4 * sigma + 1, (paid, expected, sigma))
    check("every seeded spin is three known symbols",
          all(len(reels) == 3 and all(reel in slots.SYMBOLS for reel in reels)
              for reels in spins))


# ---------------------------------------------------------------------------
# The cog, against a throwaway database
# ---------------------------------------------------------------------------
async def cog_checks():
    db = Database(os.path.join(tempfile.mkdtemp(), "slots.db"))
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

    real_spin = slots.spin
    try:
        # --- a forced jackpot -------------------------------------------
        slots.spin = lambda rng=None: (slots.MAX_PAYOUT_SYMBOL,) * 3
        i = FakeInteraction(alice)
        await Casino.slots.callback(cog, i, "1000")
        print("  win reply:", i.text()[:110])
        check("win card is a slots win", "SLOTS · You Win" in i.text(), i.text())
        check("win card shows the reels", "│" in i.text(), i.text())
        check("win card names the payer", f"{slots.MAX_PAYOUT}×" in i.text(), i.text())
        check("win card invites spectators", "The Floor" in i.text(), i.text())
        check("jackpot pays 250x the stake",
              int((await db.find_individual(g, 10))["balance"]) == 250_000,
              (await db.find_individual(g, 10))["balance"])
        house = await db.get_casino_balance(g)
        check("the house paid the jackpot",
              house == 500_000_000 + 1_000 - 250_000, house)

        # --- a forced near miss -----------------------------------------
        cherry, lemon = slots.SYMBOLS[0], slots.SYMBOLS[1]
        slots.spin = lambda rng=None: (cherry, lemon, cherry)
        i = FakeInteraction(alice)
        await Casino.slots.callback(cog, i, "1000")
        print("  loss reply:", i.text()[:110])
        check("loss card is a miss", "No Match" in i.text(), i.text())
        check("loss card spots the pair", "two Cherries" in i.text(), i.text())
        check("loss card takes the stake",
              int((await db.find_individual(g, 10))["balance"]) == 249_000,
              (await db.find_individual(g, 10))["balance"])

        # --- nothing was created or destroyed ---------------------------
        house = await db.get_casino_balance(g)
        total = int((await db.find_individual(g, 10))["balance"]) + house
        check("the realm's credits are conserved", total == start_total,
              (total, start_total))

        # --- refusals must not spin or move money -----------------------
        calls = []
        slots.spin = lambda rng=None: calls.append(1) or (slots.MAX_PAYOUT_SYMBOL,) * 3
        i = FakeInteraction(alice)
        await Casino.slots.callback(cog, i, "50")
        check("below the minimum refused", "Bet Refused" in i.text(), i.text())
        check("the minimum is named", "minimum" in i.text().lower(), i.text())

        await db.get_individual(g, 30, "Whale")
        await db.credit_individual(g, 30, 99_999_000)
        i = FakeInteraction(FakeUser(30, "Whale"))
        await Casino.slots.callback(cog, i, "1000")
        check("cap-breaking spin refused", "Bet Refused" in i.text(), i.text())
        check("the cap is named", "cap" in i.text().lower(), i.text())
        check("the whale was not charged",
              int((await db.find_individual(g, 30))["balance"]) == 99_999_000)

        # --- a house that cannot cover the jackpot ----------------------
        drained = await db.get_casino_balance(g)
        await db.debit_casino(g, drained)
        i = FakeInteraction(alice)
        await Casino.slots.callback(cog, i, "1000")
        check("a house that cannot cover the jackpot refuses",
              "Bet Refused" in i.text(), i.text())
        check("the refusal explains cover",
              "cannot cover" in i.text().lower(), i.text())
        await db.credit_casino(g, drained)
        check("refusals never spun", calls == [], calls)

        # --- a wallet that cannot cover even the minimum ----------------
        await db.get_individual(g, 20, "Poor")
        i = FakeInteraction(FakeUser(20, "Poor"))
        await Casino.slots.callback(cog, i, "100")
        check("an empty wallet is refused", "Bet Refused" in i.text(), i.text())

        # --- no wallet / no guild --------------------------------------
        i = FakeInteraction(FakeUser(99, "Nobody"))
        await Casino.slots.callback(cog, i, "1000")
        check("no wallet refused", "No Wallet" in i.text(), i.text())

        i = FakeInteraction(alice, guild_id=None)
        await Casino.slots.callback(cog, i, "1000")
        check("guild-only enforced", "Guild Only" in i.text(), i.text())
    finally:
        slots.spin = real_spin
    check("the balance did not drift across the refusals",
          int((await db.find_individual(g, 10))["balance"]) == 249_000,
          (await db.find_individual(g, 10))["balance"])

    # Both sides of every settled spin are in the ledger and they mirror.
    rows = await db.fetchall("SELECT * FROM transactions WHERE kind='individual_slots'")
    player_rows = [r for r in rows if r["account_type"] == "individual"]
    house_rows = [r for r in rows if r["account_type"] == "casino"]
    check("stake and payout rows both exist",
          sorted(int(r["amount"]) for r in player_rows) == [-1_000, -1_000, 250_000],
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
    print("-- strip and paytable --")
    pure_checks()
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
