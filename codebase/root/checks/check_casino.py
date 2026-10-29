"""The Casino house account and blackjack: rules, money and no money printing.

    python3 checks/check_casino.py

The central property under test is that **gambling moves credits instead of
creating them**: for every settled wager the player and the house must change by
equal and opposite amounts, so the total across both accounts is constant. A
one-credit leak there is a faucet nobody would notice for weeks.
"""
import asyncio, os, sys, tempfile
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import blackjack
import casino
import config
from database import AccountMissing, Database, InsufficientFunds

fails = []
def check(label, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


# ---------------------------------------------------------------------------
# Pure blackjack rules
# ---------------------------------------------------------------------------
def pure_checks():
    h = blackjack.hand_value
    check("number cards count face value", h(["7♠", "8♥"]) == 15, h(["7♠", "8♥"]))
    check("court cards count ten", h(["K♠", "Q♥"]) == 20, h(["K♠", "Q♥"]))
    check("ace counts eleven when it fits", h(["A♠", "6♥"]) == 17, h(["A♠", "6♥"]))
    check("ace drops to one to avoid busting", h(["A♠", "6♥", "K♦"]) == 17, h(["A♠", "6♥", "K♦"]))
    check("two aces with a nine makes 21", h(["A♠", "A♥", "9♦"]) == 21, h(["A♠", "A♥", "9♦"]))
    check("three aces make 13", h(["A♠", "A♥", "A♦"]) == 13, h(["A♠", "A♥", "A♦"]))
    check("bust is reported", h(["K♠", "Q♥", "J♦"]) == 30, h(["K♠", "Q♥", "J♦"]))
    check("soft ace recognised", blackjack.is_soft(["A♠", "6♥"]) is True)
    check("demoted ace is not soft", blackjack.is_soft(["A♠", "6♥", "K♦"]) is False)
    check("two aces with a nine is still soft",
          blackjack.is_soft(["A♠", "A♥", "9♦"]) is True)
    check("four demoted aces are not soft",
          blackjack.is_soft(["A♠", "A♥", "A♦", "A♣", "K♥"]) is False)
    check("bust recognised", blackjack.is_bust(["K♠", "Q♥", "J♦"]) is True)

    check("two-card 21 is a natural", blackjack.is_blackjack(["A♠", "K♥"]) is True)
    check("three-card 21 is not a natural", blackjack.is_blackjack(["7♠", "7♥", "7♦"]) is False)

    check("dealer hits 16", blackjack.dealer_should_hit(["10♠", "6♥"]) is True)
    check("dealer stands on 17", blackjack.dealer_should_hit(["10♠", "7♥"]) is False)
    check("dealer stands on soft 17", blackjack.dealer_should_hit(["A♠", "6♥"]) is False)
    check("dealer stands on 21", blackjack.dealer_should_hit(["A♠", "K♥"]) is False)

    # The payout rule is what the whole economy depends on.
    check("a normal win returns double", blackjack.gross_return(stake=1_000, blackjack=False) == 2_000)
    check("a natural pays 3:2", blackjack.gross_return(stake=1_000, blackjack=True) == 2_500)
    check("3:2 on an odd stake floors in the house's favour",
          blackjack.gross_return(stake=101, blackjack=True) == 252,
          blackjack.gross_return(stake=101, blackjack=True))
    check("worst case reserved is the natural",
          blackjack.max_gross(1_000) == 2_500, blackjack.max_gross(1_000))

    # Every settlement branch.
    s = blackjack.settle
    check("player bust loses", s(["K♠", "Q♥", "5♦"], ["10♠", "9♥"], stake=100).result == "loss")
    check("dealer bust wins", s(["10♠", "8♥"], ["K♦", "9♣", "5♥"], stake=100).result == "win")
    check("higher hand wins", s(["10♠", "9♥"], ["10♦", "8♣"], stake=100).result == "win")
    check("lower hand loses", s(["10♠", "7♥"], ["10♦", "8♣"], stake=100).result == "loss")
    check("equal hands push", s(["10♠", "9♥"], ["10♦", "9♣"], stake=100).result == "push")
    check("natural beats a made 21",
          s(["A♠", "K♥"], ["7♦", "7♣", "7♥"], stake=100).result == "win")
    check("two naturals push", s(["A♠", "K♥"], ["A♦", "Q♣"], stake=100).result == "push")
    check("dealer natural beats 20", s(["10♠", "Q♥"], ["A♦", "Q♣"], stake=100).result == "loss")
    check("a win with a natural banks 3:2",
          s(["A♠", "K♥"], ["10♦", "9♣"], stake=1_000).gross == 2_500)
    check("a push returns exactly the stake",
          s(["10♠", "9♥"], ["10♦", "9♣"], stake=1_000).net == 0)
    check("a bust returns nothing", s(["K♠", "Q♥", "5♦"], ["10♠", "9♥"], stake=1_000).gross == 0)

    shoe = blackjack.new_shoe()
    check("a six-deck shoe has 312 cards", len(shoe) == 312, len(shoe))
    check("the shoe holds every rank",
          {c[:-1] for c in shoe}
          == {"A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"},
          sorted({c[:-1] for c in shoe}))
    check("the shoe holds four suits",
          {c[-1] for c in shoe} == set(blackjack.SIGNS.values()), sorted({c[-1] for c in shoe}))
    short = []
    drawn = blackjack.draw(short)
    check("an empty shoe reshuffles instead of failing", drawn is not None and len(short) == 311)


# ---------------------------------------------------------------------------
# Money: settlement, conservation, refusals
# ---------------------------------------------------------------------------
async def money_checks():
    db = Database(os.path.join(tempfile.mkdtemp(), "casino.db"))
    await db.connect()
    g = 1
    alice = await db.get_individual(g, 10, "Alice")
    await db.credit_individual(g, 10, 100_000)
    await db.log_tx(
        guild_id=g, kind="individual_grant", account_type="individual",
        account_id=alice["id"], account_name="Alice", amount=100_000, balance_after=100_000,
    )
    await db.credit_casino(g, 1_000_000)

    async def total():
        wallet = int((await db.find_individual(g, 10))["balance"])
        house = await db.get_casino_balance(g)
        return wallet, house, wallet + house

    start = await total()

    # --- a win is paid by the house, not minted -------------------------
    w = await db.find_individual(g, 10)
    async with db.transaction():
        await casino.begin_wager(db, guild_id=g, wallet=w, stake=10_000, kind="individual_blackjack")
        p_after, h_after = await casino.finish_wager(
            db, guild_id=g, wallet=w, stake=10_000, gross=25_000,
            outcome="win", kind="individual_blackjack",
        )
    check("a win credits the player", p_after == 115_000, p_after)
    check("a win debits the house", h_after == 985_000, h_after)
    check("a win invents no credits", (await total())[2] == start[2], ((await total()), start))

    # --- a loss funds the house ------------------------------------------
    w = await db.find_individual(g, 10)
    async with db.transaction():
        await casino.begin_wager(db, guild_id=g, wallet=w, stake=10_000, kind="individual_blackjack")
        p_after, h_after = await casino.finish_wager(
            db, guild_id=g, wallet=w, stake=10_000, gross=0,
            outcome="loss", kind="individual_blackjack",
        )
    check("a loss debits the player", p_after == 105_000, p_after)
    check("a loss credits the house", h_after == 995_000, h_after)
    check("a loss destroys no credits", (await total())[2] == start[2], ((await total()), start))

    # --- a push returns the stake exactly --------------------------------
    w = await db.find_individual(g, 10)
    async with db.transaction():
        await casino.begin_wager(db, guild_id=g, wallet=w, stake=10_000, kind="individual_blackjack")
        p_after, h_after = await casino.finish_wager(
            db, guild_id=g, wallet=w, stake=10_000, gross=10_000,
            outcome="push", kind="individual_blackjack",
        )
    check("a push restores the player", p_after == 105_000, p_after)
    check("a push restores the house", h_after == 995_000, h_after)
    check("a push moves nothing net", (await total())[2] == start[2], ((await total()), start))

    # --- poker returns the remaining stack, not just win/push/loss --------
    poker_before = await total()
    w = await db.find_individual(g, 10)
    async with db.transaction():
        await casino.begin_wager(db, guild_id=g, wallet=w, stake=10_000, kind="individual_poker")
        p_after, h_after = await casino.finish_wager(
            db, guild_id=g, wallet=w, stake=10_000, gross=7_000,
            outcome="loss", kind="individual_poker", payout_override=7_000,
        )
    check("a losing poker hand still returns its remaining stack",
          p_after == poker_before[0] - 3_000 and h_after == poker_before[1] + 3_000,
          (p_after, h_after, poker_before))
    check("a losing poker hand conserves credits",
          p_after + h_after == poker_before[2], (p_after, h_after, poker_before))

    w = await db.find_individual(g, 10)
    win_before = await total()
    async with db.transaction():
        await casino.begin_wager(db, guild_id=g, wallet=w, stake=10_000, kind="individual_poker")
        p_after, h_after = await casino.finish_wager(
            db, guild_id=g, wallet=w, stake=10_000, gross=25_000,
            outcome="win", kind="individual_poker", payout_override=25_000,
        )
    check("a winning poker hand pays the final stack",
          p_after == win_before[0] + 15_000 and h_after == win_before[1] - 15_000,
          (p_after, h_after, win_before))
    check("a winning poker hand conserves credits",
          p_after + h_after == win_before[2], (p_after, h_after, win_before))

    # --- conservation under a long random run ----------------------------
    before = await total()
    for i in range(200):
        w = await db.find_individual(g, 10)
        stake = 500
        won = i % 2 == 0
        gross = blackjack.max_gross(stake) if won else 0
        async with db.transaction():
            await casino.begin_wager(db, guild_id=g, wallet=w, stake=stake, kind="individual_blackjack")
            await casino.finish_wager(
                db, guild_id=g, wallet=w, stake=stake, gross=gross,
                outcome="win" if won else "loss", kind="individual_blackjack",
            )
    after = await total()
    check("200 wagers conserve every credit exactly",
          after[2] == before[2], (before, after))
    check("the house is never negative after the run", after[1] >= 0, after[1])

    # --- the house refuses what it cannot cover --------------------------
    poor = await db.get_casino_balance(g)
    check("an uncovered payout is refused",
          casino.cover_problem(1_000, 2_000) is not None, poor)
    check("a covered payout is allowed", casino.cover_problem(2_000, 2_000) is None)
    check("the refusal names the shortfall", "cannot cover" in casino.cover_problem(1_000, 2_000))

    check("minimum stake enforced",
          casino.wager_problem(stake=99, balance=10**6, gross=198, house=10**9, minimum=100) is not None)
    check("minimum stake accepted",
          casino.wager_problem(stake=100, balance=10**6, gross=200, house=10**9, minimum=100) is None)
    check("overdraw refused",
          casino.wager_problem(stake=2_000, balance=1_000, gross=4_000, house=10**9, minimum=100) is not None)
    check("a cap-breaking win is refused",
          casino.wager_problem(
              stake=1_000, balance=config.INDIVIDUAL_MAX_CREDITS - 500,
              gross=2_000, house=10**9, minimum=100,
          ) is not None)
    check("a cap-fitting win is allowed",
          casino.wager_problem(
              stake=1_000, balance=config.INDIVIDUAL_MAX_CREDITS - 2_000,
              gross=2_000, house=10**9, minimum=100,
          ) is None)

    # --- the house balance has a hard floor -----------------------------
    house = await db.get_casino_balance(g)
    try:
        await db.debit_casino(g, house + 1)
        check("the house cannot be overdrawn", False)
    except InsufficientFunds:
        check("the house cannot be overdrawn", True)
    check("the floor leaves the balance intact",
          await db.get_casino_balance(g) == house, await db.get_casino_balance(g))

    # --- ledger: both sides recorded and mirroring -----------------------
    rows = await db.fetchall("SELECT * FROM transactions WHERE kind='individual_blackjack'")
    player_rows = [r for r in rows if r["account_type"] == "individual"]
    house_rows = [r for r in rows if r["account_type"] == "casino"]
    check("both sides of every wager are logged", len(house_rows) == len(player_rows),
          (len(house_rows), len(player_rows)))
    check("the house ledger mirrors the player ledger",
          sum(int(r["amount"]) for r in house_rows) == -sum(int(r["amount"]) for r in player_rows),
          (sum(int(r["amount"]) for r in house_rows),
           sum(int(r["amount"]) for r in player_rows)))
    wallet_row = await db.find_individual(g, 10)
    ledger = await db.fetchall(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions "
        "WHERE account_type='individual' AND account_id=?", (wallet_row["id"],),
    )
    check("the wallet reconciles with its ledger",
          int(wallet_row["balance"]) == int(ledger[0]["s"]),
          (int(wallet_row["balance"]), int(ledger[0]["s"])))

    # --- a hand can only be settled once --------------------------------
    hand = await db.open_blackjack_hand(
        guild_id=g, discord_id=10, individual_id=int(wallet_row["id"]),
        stake=1_000, player_cards="10♠ 9♥", dealer_cards="9♦ 8♣",
    )
    check("an open hand is listed", hand in [int(r["id"]) for r in await db.all_open_blackjack_hands()])
    check("the first close wins", await db.close_blackjack_hand(hand) is True)
    check("the second close is refused", await db.close_blackjack_hand(hand) is False)
    check("a settled hand leaves the open list",
          hand not in [int(r["id"]) for r in await db.all_open_blackjack_hands()])

    await db.close()


# ---------------------------------------------------------------------------
# A restart refunds the stake of a hand in progress
# ---------------------------------------------------------------------------
async def restart_checks():
    import logging
    from bot import RoyalCourt

    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

    db = Database(os.path.join(tempfile.mkdtemp(), "restart.db"))
    await db.connect()
    g = 1
    row = await db.get_individual(g, 10, "Alice")
    await db.credit_individual(g, 10, 10_000)
    await db.credit_casino(g, 100_000)

    # Deal a hand the way the command does: stake held, hand recorded.
    wallet = await db.find_individual(g, 10)
    async with db.transaction():
        await casino.begin_wager(db, guild_id=g, wallet=wallet, stake=4_000,
                                 kind="individual_blackjack")
        await db.open_blackjack_hand(
            guild_id=g, discord_id=10, individual_id=int(row["id"]),
            stake=4_000, player_cards="10♠ 9♥", dealer_cards="9♦ 8♣",
        )
    check("the stake is held before the hand is played",
          int((await db.find_individual(g, 10))["balance"]) == 6_000)

    class FakeSelf:
        pass

    fake = FakeSelf()
    fake.db = db
    await RoyalCourt._refund_abandoned_blackjack_hands(fake)

    check("a restart refunds the interrupted stake",
          int((await db.find_individual(g, 10))["balance"]) == 10_000,
          (await db.find_individual(g, 10))["balance"])
    check("the refund takes the credits back from the house",
          await db.get_casino_balance(g) == 100_000, await db.get_casino_balance(g))
    check("the hand is no longer open", await db.all_open_blackjack_hands() == [])

    # Running it again must not pay twice.
    await RoyalCourt._refund_abandoned_blackjack_hands(fake)
    check("a second startup does not pay twice",
          int((await db.find_individual(g, 10))["balance"]) == 10_000,
          (await db.find_individual(g, 10))["balance"])

    # Poker uses the same held-buy-in / restart-refund rule.
    wallet = await db.find_individual(g, 10)
    async with db.transaction():
        await casino.begin_wager(db, guild_id=g, wallet=wallet, stake=4_000,
                                 kind="individual_poker")
        await db.open_poker_hand(
            guild_id=g, discord_id=10, individual_id=int(row["id"]), stake=4_000,
        )
    check("a poker buy-in is held while the table is live",
          int((await db.find_individual(g, 10))["balance"]) == 6_000,
          (await db.find_individual(g, 10))["balance"])
    await RoyalCourt._refund_abandoned_poker_hands(fake)
    check("a restart refunds the poker buy-in",
          int((await db.find_individual(g, 10))["balance"]) == 10_000,
          (await db.find_individual(g, 10))["balance"])
    check("the poker refund takes the credits back from the house",
          await db.get_casino_balance(g) == 100_000,
          await db.get_casino_balance(g))
    check("the poker table is no longer open", await db.all_open_poker_hands() == [])
    await RoyalCourt._refund_abandoned_poker_hands(fake)
    check("a second poker startup does not pay twice",
          int((await db.find_individual(g, 10))["balance"]) == 10_000,
          (await db.find_individual(g, 10))["balance"])
    await db.close()


# ---------------------------------------------------------------------------
# The /blackjack command end to end
# ---------------------------------------------------------------------------
class FakeUser:
    def __init__(self, uid, name):
        self.id = uid
        self.display_name = name
        self.mention = f"<@{uid}>"
        self.bot = False
        self.guild_permissions = None


class FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)


class FakeResp:
    def __init__(self, sink, message):
        self._sink = sink
        self._message = message
        self.edited = []
        self.calls = []

    async def send_message(self, embed=None, **kw):
        self.calls.append(kw)
        self._sink.append(embed)

    async def edit_message(self, embed=None, view=None, **kw):
        self.calls.append(kw)
        self.edited.append(embed)
        self._sink.append(embed)


class FakeInteraction:
    def __init__(self, user, guild_id=1):
        self.user = user
        self.guild_id = guild_id
        self.sent = []
        self.message = FakeMessage()
        self.response = FakeResp(self.sent, self.message)

    async def original_response(self):
        return self.message

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


async def cog_checks():
    from cogs.games import Casino, BlackjackView

    db = Database(os.path.join(tempfile.mkdtemp(), "bj.db"))
    await db.connect()
    g = 1
    cog = Casino(FakeBot(db))
    alice = FakeUser(10, "Alice")
    await db.get_individual(g, 10, "Alice")
    await db.credit_individual(g, 10, 50_000)

    # --- an empty house refuses the deal --------------------------------
    i = FakeInteraction(alice)
    await Casino.blackjack.callback(cog, i, "1,000")
    check("an unfunded house refuses", "Bet Refused" in i.text(), i.text())
    check("the refusal names the house shortfall", "cannot cover" in i.text(), i.text())
    check("a refused deal takes no stake",
          int((await db.find_individual(g, 10))["balance"]) == 50_000)

    await db.credit_casino(g, 1_000_000)

    # --- a full hand ----------------------------------------------------
    # Stack the shoe so the deal is the same every run: draw() pops the end, so
    # the list is written hole-card first. Player 19 (10 + 9), dealer 18.
    real_shoe = blackjack.new_shoe
    blackjack.new_shoe = lambda *a, **k: [
        "2♣", "8♣", "10♦",   # dealer draws 10, then 8
        "3♦", "9♥", "10♠",   # player draws 10, then 9
    ]
    i = FakeInteraction(alice)
    await Casino.blackjack.callback(cog, i, "5,000")
    blackjack.new_shoe = real_shoe
    print("  deal:", i.text()[:160])
    # Gambling has to be visible in the channel, so people can watch.
    check("the deal is posted to the channel, not hidden",
          not any(kw.get("ephemeral") for kw in i.response.calls), i.response.calls)
    check("the stake is taken on the deal",
          int((await db.find_individual(g, 10))["balance"]) == 45_000,
          (await db.find_individual(g, 10))["balance"])
    check("the hand is recorded as open", len(await db.all_open_blackjack_hands()) == 1)
    check("the deal hides the hole card", "🂠" in i.text(), i.text())
    check("the table invites spectators", "table is watching" in i.text(), i.text())

    # Rebuild the view from the recorded hand to drive the buttons.
    hand_row = (await db.all_open_blackjack_hands())[0]
    view = BlackjackView(
        cog, guild_id=g, author_id=10, stake=5_000,
        wallet=await db.find_individual(g, 10),
    )
    view.hand_id = int(hand_row["id"])
    view.player = ["10♠", "9♥"]
    view.dealer = ["10♦", "8♣"]

    # Standing must pay out and close the hand.
    j = FakeInteraction(alice)
    await view.stand.callback(j)
    check("standing settles the hand", view.settled is True)
    check("the settled hand is closed", await db.all_open_blackjack_hands() == [])
    check("a 19 against 18 wins", "You Win" in j.text(), j.text())
    balance = int((await db.find_individual(g, 10))["balance"])
    check("the win is paid from the house", balance == 55_000, balance)
    check("the house paid it, not the void",
          await db.get_casino_balance(g) == 1_000_000 - 5_000, await db.get_casino_balance(g))

    # A second press must not pay again.
    j2 = FakeInteraction(alice)
    await view.stand.callback(j2)
    check("a settled hand cannot be paid twice",
          int((await db.find_individual(g, 10))["balance"]) == 55_000,
          (await db.find_individual(g, 10))["balance"])

    # --- someone else cannot press the buttons --------------------------
    j3 = FakeInteraction(FakeUser(77, "Mallory"))
    allowed = await view.interaction_check(j3)
    check("another user cannot play your hand", allowed is False)
    check("the intruder is told why", "Not Your Table" in j3.text(), j3.text())

    # --- a hand the house cannot cover is refused -----------------------
    small = Database(os.path.join(tempfile.mkdtemp(), "poor.db"))
    await small.connect()
    cog2 = Casino(FakeBot(small))
    await small.get_individual(g, 10, "Alice")
    await small.credit_individual(g, 10, 50_000)
    await small.credit_casino(g, 1_000)
    i = FakeInteraction(alice)
    await Casino.blackjack.callback(cog2, i, "5,000")
    check("a stake the house cannot cover is refused", "Bet Refused" in i.text(), i.text())
    check("the refused deal left the wallet alone",
          int((await small.find_individual(g, 10))["balance"]) == 50_000)
    i = FakeInteraction(alice)
    await Casino.poker.callback(cog2, i, "5,000")
    check("a poker buy-in the house cannot cover is refused",
          "Buy-In Refused" in i.text(), i.text())
    check("the refused poker buy-in left the wallet alone",
          int((await small.find_individual(g, 10))["balance"]) == 50_000)
    await small.close()

    # --- /casino balance is the public floor dashboard -------------------
    i = FakeInteraction(alice)
    await Casino.balance.callback(cog, i)
    check("the casino floor lists the games", "Tonight on the floor" in i.text(), i.text())
    check("the casino floor invites spectators", "table is watching" in i.text(), i.text())

    # --- /poker end to end ----------------------------------------------
    before_poker = int((await db.find_individual(g, 10))["balance"])
    house_before_poker = await db.get_casino_balance(g)
    i = FakeInteraction(alice)
    await Casino.poker.callback(cog, i, "1,000")
    print("  poker deal:", i.text()[:180])
    check("the poker table is posted publicly",
          not any(kw.get("ephemeral") for kw in i.response.calls), i.response.calls)
    check("the poker table has a public view", bool(i.response.calls[-1].get("view")))
    check("the poker deal shows the table", "TEXAS HOLD'EM" in i.text(), i.text())
    check("the poker buy-in is held",
          int((await db.find_individual(g, 10))["balance"]) == before_poker - 1_000,
          (await db.find_individual(g, 10))["balance"])
    check("the poker table is recorded as open", len(await db.all_open_poker_hands()) == 1)

    table = i.response.calls[-1]["view"]
    mallory = FakeInteraction(FakeUser(77, "Mallory"))
    check("another user cannot play your poker table",
          await table.interaction_check(mallory) is False)
    j = FakeInteraction(alice)
    await table.fold_button.callback(j)
    result = table.message.edits[-1]["embed"]
    check("folding settles the table", table.settled is True)
    check("folding returns the uncommitted stack",
          int((await db.find_individual(g, 10))["balance"]) == before_poker,
          (await db.find_individual(g, 10))["balance"])
    check("the poker table is closed", await db.all_open_poker_hands() == [])
    check("the poker house finishes where it started",
          await db.get_casino_balance(g) == house_before_poker,
          await db.get_casino_balance(g))
    check("the poker result is public", "POKER" in (result.title or ""), result.title)

    # A full hand with bot turns, streets and showdown/settlement.
    total_before = (
        int((await db.find_individual(g, 10))["balance"]) + await db.get_casino_balance(g)
    )
    i = FakeInteraction(alice)
    await Casino.poker.callback(cog, i, "1,000")
    table = i.response.calls[-1].get("view")
    raised = False
    for _ in range(40):
        if table is None or table.settled or table.game.complete:
            break
        options = table.game.legal_actions()
        if not raised and "raise" in options:
            action = "raise"
            amount = str(table.game.min_raise_to(table.game.player()))
            raised = True
        elif "check" in options:
            action = "check"
            amount = None
        elif "call" in options:
            action = "call"
            amount = None
        else:
            action = "fold"
            amount = None
        await table.player_action(FakeInteraction(alice), action, amount)
    check("a full poker hand terminates and settles",
          table is not None and table.settled, None if table is None else table.game.log[-3:])
    check("a full poker hand leaves no open table", await db.all_open_poker_hands() == [])
    total_after = (
        int((await db.find_individual(g, 10))["balance"]) + await db.get_casino_balance(g)
    )
    check("a full poker hand conserves every credit",
          total_after == total_before, (total_before, total_after))

    # --- /casino fund and withdraw --------------------------------------
    i = FakeInteraction(alice)
    await Casino.withdraw.callback(cog, i, 10**9)
    check("an over-withdrawal is refused", "Insufficient Funds" in i.text(), i.text())

    await db.close()


# ---------------------------------------------------------------------------
# Every gambling command posts in the channel so the table has an audience
# ---------------------------------------------------------------------------
class Side:
    """Stands in for app_commands.Choice[str]."""

    def __init__(self, value):
        self.value = value
        self.name = value.title()


async def public_checks():
    import coinflip
    from cogs.individual import Individuals

    db = Database(os.path.join(tempfile.mkdtemp(), "public.db"))
    await db.connect()
    g = 1
    cog = Individuals(FakeBot(db))
    alice = FakeUser(10, "Alice")
    await db.get_individual(g, 10, "Alice")
    await db.credit_individual(g, 10, 100_000)
    await db.credit_casino(g, 1_000_000)

    real_roll = coinflip.roll
    try:
        coinflip.roll = lambda rng=None: 0
        i = FakeInteraction(alice)
        await Individuals.coinflip.callback(cog, i, Side("heads"), "1,000")
        check("coinflip result is public",
              not any(kw.get("ephemeral") for kw in i.response.calls), i.response.calls)

        import allin as allin_mod
        real_allin = allin_mod.roll
        allin_mod.roll = lambda number, rng=None: 0
        try:
            i = FakeInteraction(alice)
            await Individuals.allin.callback(cog, i, 5)
            check("the all-in confirmation is public",
                  not any(kw.get("ephemeral") for kw in i.response.calls), i.response.calls)
        finally:
            allin_mod.roll = real_allin
    finally:
        coinflip.roll = real_roll

    await db.close()


async def wallet_lifecycle_checks():
    """Deleting a wallet during or before a hand must never resurrect it."""
    db = Database(os.path.join(tempfile.mkdtemp(), "lifecycle.db"))
    await db.connect()
    g = 1
    await db.credit_casino(g, 100_000)

    # An open hand blocks erasure at the database layer, not just in the view.
    wallet = await db.get_individual(g, 1, "Open Hand")
    await db.credit_individual(g, 1, 1_000)
    hand = await db.open_blackjack_hand(
        guild_id=g, discord_id=1, individual_id=int(wallet["id"]), stake=100,
        player_cards="A♠ K♥", dealer_cards="9♠ 7♥",
    )
    check("delete is refused while a hand is open",
          await db.delete_individual_checked(g, 1) == "open")
    check("delete_individual returns False while a hand is open",
          await db.delete_individual(g, 1) is False)
    check("wallet survives the blocked delete", await db.find_individual(g, 1) is not None)
    check("closing the hand allows the delete",
          await db.close_blackjack_hand(hand) is True)
    check("wallet deletes after the hand closes",
          await db.delete_individual_checked(g, 1) == "deleted")
    check("wallet is really gone", await db.find_individual(g, 1) is None)

    # Strict credit is a settlement path: it must not create a wallet.
    try:
        await db.credit_individual_strict(g, 2, 100)
        check("strict credit refuses a missing wallet", False)
    except AccountMissing:
        check("strict credit refuses a missing wallet", True)
    check("strict credit did not create a wallet", await db.find_individual(g, 2) is None)

    # A stale wallet row passed to begin_wager must not resurrect the wallet.
    stale = await db.get_individual(g, 3, "Stale Stake")
    await db.credit_individual(g, 3, 1_000)
    await db.delete_individual(g, 3)
    try:
        await casino.begin_wager(
            db, guild_id=g, wallet=stale, stake=100, kind="test_wager"
        )
        check("begin_wager refuses a deleted wallet", False)
    except AccountMissing:
        check("begin_wager refuses a deleted wallet", True)
    check("begin_wager did not resurrect the wallet",
          await db.find_individual(g, 3) is None)

    # A payout to a deleted wallet is refused; a loss moves nothing and does
    # not recreate the row either.
    stale_payout = await db.get_individual(g, 4, "Stale Payout")
    await db.credit_individual(g, 4, 1_000)
    await db.delete_individual(g, 4)
    try:
        await casino.finish_wager(
            db, guild_id=g, wallet=stale_payout, stake=100, gross=200,
            outcome="win", kind="test_wager",
        )
        check("finish_wager refuses a payout to a deleted wallet", False)
    except AccountMissing:
        check("finish_wager refuses a payout to a deleted wallet", True)
    check("settlement did not resurrect the wallet",
          await db.find_individual(g, 4) is None)

    stale_loss = await db.get_individual(g, 5, "Stale Loss")
    await db.credit_individual(g, 5, 1_000)
    await db.delete_individual(g, 5)
    after, _house = await casino.finish_wager(
        db, guild_id=g, wallet=stale_loss, stake=100, gross=200,
        outcome="loss", kind="test_wager",
    )
    check("a loss on a deleted wallet moves nothing", after == 0, after)
    check("a loss did not resurrect the wallet",
          await db.find_individual(g, 5) is None)

    await db.close()


def main():
    print("-- blackjack rules --")
    pure_checks()
    print("-- house money --")
    asyncio.run(money_checks())
    print("-- restart recovery --")
    asyncio.run(restart_checks())
    print("-- wallet lifecycle --")
    asyncio.run(wallet_lifecycle_checks())
    print("-- the /blackjack command --")
    asyncio.run(cog_checks())
    print("-- gambling is visible in the channel --")
    asyncio.run(public_checks())
    print(f"\nFAILURES: {len(fails)}")
    if fails:
        for f in fails:
            print("  -", f)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
