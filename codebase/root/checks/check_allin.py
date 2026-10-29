"""Odds, guards and money handling for /allin."""
import asyncio, os, sys, tempfile
from fractions import Fraction
from types import SimpleNamespace
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import allin
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
        self.edited = []
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


def pure_checks():
    # The headline rule: n wins at 1 in (1.1 * n). n=5 must be exactly 1 in 5.5.
    check("n=5 is 1 in 5.5", allin.chance_label(5) == "1 in 5.5", allin.chance_label(5))
    check("n=5 chance is 2/11", allin.win_chance(5) == Fraction(2, 11), allin.win_chance(5))
    check("n=2 is 1 in 2.2", allin.chance_label(2) == "1 in 2.2", allin.chance_label(2))
    check("n=100 is 1 in 110", allin.chance_label(100) == "1 in 110", allin.chance_label(100))

    # Exhaust every draw for every playable number and count the wins.
    for n in range(config.ALLIN_MIN_NUMBER, config.ALLIN_MAX_NUMBER + 1):
        total = allin.draw_size(n)
        wins = sum(1 for r in range(total) if allin.resolve(n, r))
        expect = allin.win_chance(n)
        if Fraction(wins, total) != expect:
            check(f"n={n} empirical {wins}/{total} == {expect}", False, f"{wins}/{total}")
            break
    else:
        check("every number draws its exact configured chance", True)

    # The edge must not depend on n: that is the whole point of "1.1x harder".
    evs = {n: allin.win_chance(n) * n for n in (2, 3, 5, 10, 50, 100)}
    check("expected return is flat across numbers", len(set(evs.values())) == 1, evs)
    check("expected return is 10/11", evs[5] == Fraction(10, 11), evs[5])
    check("house edge is 1/11", allin.house_edge() == Fraction(1, 11), allin.house_edge())
    check("house edge is positive", allin.house_edge() > 0, allin.house_edge())

    # 1x would return the stake with no profit, so it is refused.
    check("n=1 rejected", allin.number_problem(1) is not None)
    check("n=2 accepted", allin.number_problem(2) is None)
    check("n=100 accepted", allin.number_problem(100) is None)
    check("n=101 rejected", allin.number_problem(101) is not None)
    check("n=0 rejected", allin.number_problem(0) is not None)

    check("payout is n times stake", allin.gross_payout(5, 1_000) == 5_000)
    check("win nets (n-1) x stake", allin.net_change(5, 1_000, True) == 4_000)
    check("loss nets -stake", allin.net_change(5, 1_000, False) == -1_000)

    # A win after an all-in must leave balance * n.
    for n in (2, 5, 100):
        for bal in (100, 1_000, 50_000):
            final = bal - bal + allin.gross_payout(n, bal)
            if final != bal * n:
                check(f"all-in n={n} bal={bal} final {final} == {bal*n}", False, final)
    check("an all-in win multiplies the wallet by n", True)

    check("draw rejects out-of-range roll",
          _raises(lambda: allin.resolve(5, allin.draw_size(5))))
    check("draw rejects negative roll", _raises(lambda: allin.resolve(5, -1)))
    check("draw rejects unplayable number", _raises(lambda: allin.resolve(1, 0)))

    check("stake below minimum refused",
          allin.stake_problem(5, config.ALLIN_MIN_BALANCE - 1, config.ALLIN_MIN_BALANCE - 1))
    check("stake above balance refused",
          allin.stake_problem(5, 2_000, 1_000) is not None)
    # Payout, not stake, is what the cap governs.
    cap = config.INDIVIDUAL_MAX_CREDITS
    check("cap breach refused", allin.stake_problem(100, 2_000_000, 2_000_000) is not None)
    check("cap exactly reached allowed",
          allin.stake_problem(50, 2_000_000, 2_000_000) is None)
    check("cap room used is the payout",
          allin.stake_problem(2, cap // 2, cap // 2) is None)


def _raises(fn):
    try:
        fn()
    except ValueError:
        return True
    except Exception:
        return False
    return False


async def cog_checks():
    db = Database(os.path.join(tempfile.mkdtemp(), "allin.db"))
    await db.connect()
    g = 1
    cog = Individuals(FakeBot(db))
    alice = FakeUser(10, "Alice")

    row = await db.get_individual(g, 10, "Alice")
    await db.credit_individual(g, 10, 1_000)
    # The Casino is the counterparty: an empty house refuses every bet.
    await db.credit_casino(g, 500_000_000)
    await db.log_tx(
        guild_id=g, kind="individual_grant", account_type="individual",
        account_id=row["id"], account_name="Alice", amount=1_000,
        balance_after=1_000, actor_id=10,
    )

    real_roll = allin.roll
    try:
        # --- a forced win at 5x ------------------------------------------
        allin.roll = lambda number, rng=None: 0
        i = FakeInteraction(alice)
        await Individuals.allin.callback(cog, i, 5)
        print("  confirm reply:", i.text()[:90])
        check("proposal asks to confirm", "Confirm Your All-In" in i.text(), i.text())
        check("proposal names the whole balance", "1,000" in i.text(), i.text())
        check("proposal names the 1 in 5.5 chance", "1 in 5.5" in i.text(), i.text())
        check("proposal invites spectators", "The Floor" in i.text(), i.text())

        view = SimpleNamespace()
        i = FakeInteraction(alice)
        await Individuals.settle_allin(cog, i, 5, 1_000, view=view)
        print("  win reply:", i.text())
        check("win reply is success", "You Win Big" in i.text(), i.text())
        check("win card invites spectators", "The Floor" in i.text(), i.text())
        check("5x win turns 1,000 into 5,000",
              int((await db.find_individual(g, 10))["balance"]) == 5_000,
              (await db.find_individual(g, 10))["balance"])

        # A double click on the same confirmation must not place a second bet.
        duplicate = FakeInteraction(alice)
        await Individuals.settle_allin(cog, duplicate, 5, 1_000, view=view)
        check("second click on the confirmation is refused",
              "Already Settled" in duplicate.text(), duplicate.text())
        check("second click leaves the wallet alone",
              int((await db.find_individual(g, 10))["balance"]) == 5_000,
              (await db.find_individual(g, 10))["balance"])

        # --- a forced loss -----------------------------------------------
        allin.roll = lambda number, rng=None: allin.draw_size(5) - 1
        i = FakeInteraction(alice)
        await Individuals.settle_allin(cog, i, 5, 5_000)
        print("  loss reply:", i.text())
        check("loss reply is an error", "You Lose It All" in i.text(), i.text())
        check("loss empties the wallet",
              int((await db.find_individual(g, 10))["balance"]) == 0,
              (await db.find_individual(g, 10))["balance"])
    finally:
        allin.roll = real_roll

    rows = await db.fetchall("SELECT * FROM transactions WHERE kind='individual_allin'")
    player_rows = [r for r in rows if r["account_type"] == "individual"]
    house_rows = [r for r in rows if r["account_type"] == "casino"]
    # The stake is a row in its own right, so the win writes two and the loss one.
    check("stake rows plus one payout row", len(player_rows) == 3, len(player_rows))
    check("rows carry the actual movements",
          sorted(int(r["amount"]) for r in player_rows) == [-5_000, -1_000, 5_000],
          [int(r["amount"]) for r in player_rows])
    check("the house ledger mirrors the player ledger",
          sum(int(r["amount"]) for r in house_rows)
          == -sum(int(r["amount"]) for r in player_rows),
          (sum(int(r["amount"]) for r in house_rows),
           sum(int(r["amount"]) for r in player_rows)))
    ledger = await db.fetchall(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions "
        "WHERE account_type='individual' AND account_id=?", (row["id"],),
    )
    check("wallet reconciles with the ledger",
          int((await db.find_individual(g, 10))["balance"]) == int(ledger[0]["s"]),
          (int((await db.find_individual(g, 10))["balance"]), int(ledger[0]["s"])))

    # --- refusals must not draw or move money --------------------------
    await db.credit_individual(g, 10, 500)
    before = int((await db.find_individual(g, 10))["balance"])
    calls = []
    allin.roll = lambda number, rng=None: calls.append(1) or 0
    try:
        # A wallet big enough that a 100x payout would breach the 100M cap.
        await db.get_individual(g, 30, "Whale")
        whale = FakeUser(30, "Whale")
        await db.credit_individual(g, 30, 2_000_000)
        i = FakeInteraction(whale)
        await Individuals.allin.callback(cog, i, 100)
        check("cap-breaking proposal refused", "Bet Refused" in i.text(), i.text())
        check("refusal explains the cap", "cap" in i.text().lower(), i.text())
        check("whale wallet untouched",
              int((await db.find_individual(g, 30))["balance"]) == 2_000_000)

        # 2x from the same wallet stays under the cap and is offered.
        i = FakeInteraction(whale)
        await Individuals.allin.callback(cog, i, 2)
        check("2x under the cap is offered", "Confirm Your All-In" in i.text(), i.text())

        # A confirmation whose stake the wallet can no longer cover.
        i = FakeInteraction(alice)
        await Individuals.settle_allin(cog, i, 5, 999_999)
        check("stale confirmation refused", "Bet Refused" in i.text(), i.text())
        check("stale refusal mentions the balance", "Nothing was staked" in i.text(), i.text())

        check("refusals never drew", calls == [], calls)
    finally:
        allin.roll = real_roll
    check("refusals left the balance alone",
          int((await db.find_individual(g, 10))["balance"]) == before)

    # --- a wallet below the minimum -----------------------------------
    await db.get_individual(g, 20, "Poor")
    poor = FakeUser(20, "Poor")
    i = FakeInteraction(poor)
    await Individuals.allin.callback(cog, i, 5)
    check("empty wallet refused", "Bet Refused" in i.text(), i.text())
    check("empty wallet untouched",
          int((await db.find_individual(g, 20))["balance"]) == 0)

    # --- no wallet / no guild -----------------------------------------
    i = FakeInteraction(FakeUser(99, "Nobody"))
    await Individuals.allin.callback(cog, i, 5)
    check("no-wallet refused", "No Wallet" in i.text(), i.text())

    i = FakeInteraction(alice, guild_id=None)
    await Individuals.allin.callback(cog, i, 5)
    check("guild-only enforced", "Guild Only" in i.text(), i.text())

    await db.close()


def main():
    print("-- pure odds --")
    pure_checks()
    print("-- cog money handling --")
    asyncio.run(cog_checks())
    print(f"\nFAILURES: {len(fails)}")
    for f in fails:
        print("  -", f)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
