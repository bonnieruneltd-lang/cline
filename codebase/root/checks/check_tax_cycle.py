"""The 5% tax inside the real payout cycle, via Payout._run_cycle."""
import asyncio, datetime as dt, os, sys, tempfile
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import config
from database import Database
from cogs.payout import Payout

fails = []
def check(label, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


class FakeChannel:
    def __init__(self):
        self.embeds = []

    async def send(self, embed=None, **kwargs):
        self.embeds.append(embed)


class FakeBot:
    def __init__(self, db):
        self.db = db
        self.channel = FakeChannel()
        class L:
            def warning(self, *a, **k): pass
            def exception(self, *a, **k): pass
            def info(self, *a, **k): pass
        self.logger = L()

    def get_channel(self, channel_id):
        return self.channel

    async def fetch_channel(self, channel_id):
        return self.channel


async def main():
    db = Database(os.path.join(tempfile.mkdtemp(), "cyc.db"))
    await db.connect()
    g = 1
    bot = FakeBot(db)
    cog = Payout.__new__(Payout)   # skip __init__ (it starts the tasks loop)
    cog.bot = bot

    # three wallets: 10k, 19 (rounds to 0), 0
    await db.get_individual(g, 1, "Rich")
    await db.credit_individual(g, 1, 10_000)
    await db.get_individual(g, 2, "Tiny")
    await db.credit_individual(g, 2, 19)
    await db.get_individual(g, 3, "Empty")

    summary = await cog._run_cycle(g, natural=True)
    check("cycle returns summary", summary is not None)
    taxed = {n: (p, b) for n, p, b in summary["individuals"]}
    check("rich taxed 500", taxed.get("Rich") == (500, 9_500), taxed.get("Rich"))
    check("tiny not taxed", "Tiny" not in taxed, taxed.get("Tiny"))
    check("empty not taxed", "Empty" not in taxed)

    check("wallet reflects tax", int((await db.find_individual(g, 1))["balance"]) == 9_500,
          (await db.find_individual(g, 1))["balance"])
    check("union received tax", await db.get_union_balance(g) == 500,
          await db.get_union_balance(g))

    # second natural cycle taxes the reduced balance
    summary = await cog._run_cycle(g, natural=True)
    check("second cycle taxes 475",
          summary["individuals"][0][1] == 475, summary["individuals"][0][1])

    # manual run taxes the same 5%: the tax is per payout run, not only per
    # natural cycle. The Cash Cycle must not touch the natural schedule or the
    # contract cycle, even though it pays allowances and charges the tax.
    cash_before = await db.get_settings(g)
    summary = await cog._run_cycle(g, natural=False)
    check("manual run also taxes", len(summary["individuals"]) == 1,
          summary["individuals"])
    check("manual tax is 451", summary["individuals"][0][1] == 451,
          summary["individuals"])
    check("manual run reduces the wallet",
          int((await db.find_individual(g, 1))["balance"]) == 8_574,
          (await db.find_individual(g, 1))["balance"])
    check("union received the manual tax", await db.get_union_balance(g) == 1_426,
          await db.get_union_balance(g))
    cash_after = await db.get_settings(g)
    check("cash cycle leaves next_cycle_at alone",
          cash_before["next_cycle_at"] == cash_after["next_cycle_at"],
          (cash_before["next_cycle_at"], cash_after["next_cycle_at"]))
    check("cash cycle leaves contract_cycle alone",
          cash_before["contract_cycle"] == cash_after["contract_cycle"],
          (cash_before["contract_cycle"], cash_after["contract_cycle"]))

    # nations/kingdoms untouched by the individual tax (they still get payouts)
    nid = await db.create_nation(g, "Aurum", 111)
    await db.add_balance("nations", nid, 1_000_000)
    # A known stale schedule makes the default cycle's rewrite observable even
    # when two test runs happen inside the same second.
    await db.set_cycle_times(g, None, "2020-01-01T00:00:00")
    default_before = await db.get_settings(g)
    summary = await cog._run_cycle(g, natural=True)
    default_after = await db.get_settings(g)
    nation_bal = int((await db.get_nation_by_id(nid))["balance"])
    expected = 1_000_000 + config.NATION_BASE
    check("nation gets payout, no tax",
          nation_bal == expected, f"{nation_bal} != {expected}")
    taxed_nations = await db.fetchall(
        "SELECT * FROM transactions WHERE kind='individual_tax' AND account_type='nation'"
    )
    check("no tax rows for nations", taxed_nations == [], len(taxed_nations))
    check("default cycle advances contract_cycle",
          int(default_after["contract_cycle"]) == int(default_before["contract_cycle"]) + 1,
          (default_before["contract_cycle"], default_after["contract_cycle"]))
    check("default cycle rewrites the natural schedule",
          default_after["next_cycle_at"] != "2020-01-01T00:00:00",
          default_after["next_cycle_at"])

    # ledger reconciles: individual_tax rows net to the union delta
    rows = await db.fetchall("SELECT * FROM transactions WHERE kind='individual_tax'")
    total = sum(int(r["amount"]) for r in rows)
    check("tax ledger nets to zero", total == 0, total)

    # --- stale wallet list and deleted/recreated ids ----------------------
    # A list read before the payout transaction must not decide the tax base.
    await db.get_individual(g, 10, "Snapshot")
    await db.credit_individual(g, 10, 10_000)
    snapshot = await db.find_individual(g, 10)
    await db.credit_individual(g, 10, 90_000)   # now 100,000
    real_list = db.list_individuals

    async def stale_list(_guild_id):
        return [snapshot]

    db.list_individuals = stale_list
    try:
        summary = await cog._run_cycle(g, natural=True)
    finally:
        db.list_individuals = real_list
    taxed_snapshot = {n: (p, b) for n, p, b in summary["individuals"]}
    check("stale snapshot is taxed on the fresh balance",
          taxed_snapshot.get("Snapshot") == (5_000, 95_000),
          taxed_snapshot.get("Snapshot"))

    # A wallet erased and recreated between the list read and the tax must be
    # taxed under its new id, never the dead one.
    await db.get_individual(g, 11, "Ghost")
    await db.credit_individual(g, 11, 10_000)
    old_wallet = await db.find_individual(g, 11)
    await db.delete_individual(g, 11)
    await db.get_individual(g, 11, "Ghost")
    await db.credit_individual(g, 11, 20_000)
    new_wallet = await db.find_individual(g, 11)
    check("recreated wallet has a new id", old_wallet["id"] != new_wallet["id"],
          (old_wallet["id"], new_wallet["id"]))

    async def ghost_list(_guild_id):
        return [old_wallet]

    db.list_individuals = ghost_list
    try:
        summary = await cog._run_cycle(g, natural=True)
    finally:
        db.list_individuals = real_list
    taxed_ghost = {n: (p, b) for n, p, b in summary["individuals"]}
    check("recreated wallet is taxed under the new id",
          taxed_ghost.get("Ghost") == (1_000, 19_000),
          taxed_ghost.get("Ghost"))
    dead_rows = await db.fetchall(
        "SELECT * FROM transactions WHERE kind='individual_tax' AND account_id = ?",
        (old_wallet["id"],),
    )
    live_rows = await db.fetchall(
        "SELECT * FROM transactions WHERE kind='individual_tax' AND account_id = ?",
        (new_wallet["id"],),
    )
    check("no tax row points at the dead wallet id", dead_rows == [], len(dead_rows))
    check("the tax row points at the live wallet id", len(live_rows) == 1, len(live_rows))

    # disable switch
    config.INDIVIDUAL_TAX_ENABLED = False
    bal = int((await db.find_individual(g, 1))["balance"])
    summary = await cog._run_cycle(g, natural=True)
    check("disabled charges nothing", summary["individuals"] == [], summary["individuals"])
    check("balance unchanged when disabled",
          int((await db.find_individual(g, 1))["balance"]) == bal)
    config.INDIVIDUAL_TAX_ENABLED = True

    # --- /payout trigger queues or schedules, but never pays ---------------
    class TriggerUser:
        id = 9001
        guild_permissions = type("Perms", (), {"administrator": True})()

    class TriggerResponse:
        async def defer(self, **kwargs):
            pass

    class TriggerFollowup:
        def __init__(self):
            self.sent = []

        async def send(self, embed=None, **kwargs):
            self.sent.append(embed)

    class TriggerInteraction:
        guild_id = g
        user = TriggerUser()
        response = TriggerResponse()
        followup = TriggerFollowup()

    union_before_trigger = await db.get_union_balance(g)
    wallet_before_trigger = int((await db.find_individual(g, 1))["balance"])
    await Payout.trigger.callback(cog, TriggerInteraction(), 0)
    queued = await db.get_settings(g)
    queued_at = dt.datetime.strptime(
        queued["next_cycle_at"], "%Y-%m-%dT%H:%M:%S"
    ).replace(tzinfo=dt.timezone.utc)
    check("trigger queues a cycle due now",
          queued_at <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2),
          queued_at)
    check("trigger moved no wallet credits",
          int((await db.find_individual(g, 1))["balance"]) == wallet_before_trigger)
    check("trigger moved no Union credits",
          await db.get_union_balance(g) == union_before_trigger)

    await Payout.trigger.callback(cog, TriggerInteraction(), 7)
    scheduled = await db.get_settings(g)
    scheduled_at = dt.datetime.strptime(
        scheduled["next_cycle_at"], "%Y-%m-%dT%H:%M:%S"
    ).replace(tzinfo=dt.timezone.utc)
    expected_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)
    check("trigger schedules seven days out",
          abs((scheduled_at - expected_at).total_seconds()) < 5,
          (scheduled_at, expected_at))

    # --- /payout cash and /payout default commands end to end -------------
    await db.set_payout_channel(g, 123)

    class CommandInteraction:
        guild_id = g
        user = TriggerUser()

        def __init__(self):
            self.response = TriggerResponse()
            self.followup = TriggerFollowup()

    cash_before = await db.get_settings(g)
    await Payout.cash.callback(cog, CommandInteraction())
    cash_after = await db.get_settings(g)
    check("cash command leaves the schedule alone",
          cash_before["next_cycle_at"] == cash_after["next_cycle_at"],
          (cash_before["next_cycle_at"], cash_after["next_cycle_at"]))
    check("cash command leaves the contract cycle alone",
          cash_before["contract_cycle"] == cash_after["contract_cycle"],
          (cash_before["contract_cycle"], cash_after["contract_cycle"]))
    check("cash command posts a Cash Cycle summary",
          len(bot.channel.embeds) == 1
          and "Cash Cycle Complete" in (bot.channel.embeds[-1].title or ""),
          len(bot.channel.embeds))

    not_due = CommandInteraction()
    await Payout.default.callback(cog, not_due, False)
    check("default command refuses when not due",
          bool(not_due.followup.sent)
          and "Cycle Not Due" in (not_due.followup.sent[-1].title or ""),
          [embed.title for embed in not_due.followup.sent])
    check("refused default posts no summary", len(bot.channel.embeds) == 1,
          len(bot.channel.embeds))

    forced = CommandInteraction()
    await Payout.default.callback(cog, forced, True)
    forced_after = await db.get_settings(g)
    check("default command advances the contract cycle",
          int(forced_after["contract_cycle"]) == int(cash_after["contract_cycle"]) + 1,
          (cash_after["contract_cycle"], forced_after["contract_cycle"]))
    check("default command reschedules the natural cycle",
          forced_after["next_cycle_at"] != cash_after["next_cycle_at"],
          (cash_after["next_cycle_at"], forced_after["next_cycle_at"]))
    check("default command posts a Default Cycle summary",
          len(bot.channel.embeds) == 2
          and "Default Cycle Complete" in (bot.channel.embeds[-1].title or ""),
          len(bot.channel.embeds))

    # --- two simultaneous natural runs must pay only once -----------------
    # The command's due-check is outside the cycle lock, so the guard has to be
    # repeated after the lock is held. Without it both callers pay a full cycle
    # and advance the contract cycle twice.
    await db.set_cycle_times(g, None, "2020-01-01T00:00:00")
    race_before = int((await db.get_nation_by_id(nid))["balance"])
    race_cycle_before = int((await db.get_settings(g))["contract_cycle"])
    race = await asyncio.gather(
        cog._run_cycle(g, natural=True, require_due=True),
        cog._run_cycle(g, natural=True, require_due=True),
    )
    paid = [s for s in race if s is not None and not s.get("skipped")]
    skipped = [s for s in race if s is not None and s.get("skipped") == "not_due"]
    race_after = int((await db.get_nation_by_id(nid))["balance"])
    race_cycle_after = int((await db.get_settings(g))["contract_cycle"])
    check("concurrent natural runs pay once", len(paid) == 1, race)
    check("the losing concurrent run reports not due", len(skipped) == 1, race)
    check("concurrent runs credit one allowance",
          race_after - race_before == config.NATION_BASE,
          (race_before, race_after))
    check("concurrent runs advance the contract cycle once",
          race_cycle_after == race_cycle_before + 1,
          (race_cycle_before, race_cycle_after))

    await db.close()
    print("\nFAILURES:", len(fails))

asyncio.run(main())
raise SystemExit(1 if fails else 0)
