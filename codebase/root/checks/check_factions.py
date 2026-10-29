"""Factions, the Crown tax, supplier contracts and Albert's records."""
import asyncio
import datetime as dt
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import albert_prompt  # noqa: E402
import config  # noqa: E402
import factions  # noqa: E402
import payouts  # noqa: E402
from cogs.factions import Factions  # noqa: E402
from cogs.kingdom import Kingdom  # noqa: E402
from cogs.payout import Payout  # noqa: E402
from cogs.union import Union  # noqa: E402
from database import Database  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


class FakeLogger:
    def warning(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass


class FakeChannel:
    id = 900
    name = "commit-requests"
    mention = "<#900>"

    def __init__(self):
        self.sent = []

    async def send(self, embed=None, view=None):
        self.sent.append((embed, view))
        return SimpleNamespace(id=901)


class FakeBot:
    def __init__(self, db):
        self.db = db
        self.logger = FakeLogger()
        self.cogs = {}
        self.channel = None
        self.views = []

    def get_channel(self, channel_id):
        if self.channel is not None and channel_id == self.channel.id:
            return self.channel
        return None

    async def fetch_channel(self, channel_id):
        return self.get_channel(channel_id)

    def add_view(self, view):
        self.views.append(view)

    def get_cog(self, name):
        return self.cogs.get(name)


class FakeUser:
    def __init__(self, uid, admin=False):
        self.id = uid
        self.mention = f"<@{uid}>"
        self.display_name = f"User {uid}"
        self.guild_permissions = SimpleNamespace(administrator=admin)


class FakeResponse:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, embed=None, **kwargs):
        self.sent.append(embed)

    async def edit_message(self, embed=None, view=None, **kwargs):
        self.edited.append(embed)

    async def send(self, embed=None, **kwargs):
        self.sent.append(embed)

    async def defer(self, **kwargs):
        pass


class FakeInteraction:
    guild_id = 1

    def __init__(self, uid=111, admin=False):
        self.user = FakeUser(uid, admin)
        self.response = FakeResponse()
        self.followup = FakeResponse()

    async def edit_original_response(self, embed=None, **kwargs):
        self.response.edited.append(embed)


async def fund_crown(db, guild_id, amount, actor=999):
    async with db.transaction():
        after = await db.credit_crown(guild_id, amount)
        await db.log_tx(
            guild_id=guild_id, kind="crown_fund", account_type="crown",
            account_id=guild_id, account_name="The Crown", amount=amount,
            balance_after=after, actor_id=actor, note="test fund",
        )
    return after


def pure_checks():
    check("contract cap below 15M", config.crown_contract_cap(14_999_999) == 10_000_000)
    check("contract cap at 15M", config.crown_contract_cap(15_000_000) == 15_000_000)
    check("contract cap at 50M", config.crown_contract_cap(50_000_000) == 15_000_000)
    check("contract cap above 50M is a third",
          config.crown_contract_cap(90_000_000) == 30_000_000)
    check("demand starts full", factions.demand_after_cycle(10_000, None, 1) == 9_500)
    check("demand does not decay twice in one cycle",
          factions.demand_after_cycle(9_500, 1, 1) == 9_500)
    check("demand decays in the next cycle",
          factions.demand_after_cycle(9_500, 1, 2) == 9_000)
    check("demand has a floor",
          factions.demand_after_cycle(4_000, 1, 2) == config.CROWN_DEMAND_FLOOR_BP)
    check("contract value uses demand",
          factions.contract_value(1_000, 100, 5_000) == 50_000)
    check("max quantity uses the cap",
          factions.max_contract_quantity(10_000_000, 1_000, 10_000) == 10_000)
    check("Crown has no soldier gate",
          config.CROWN_SOLDIER_MINIMUM == 0 and factions.crown_eligible(0))
    # Pinned deliberately: contracts open a conversation, never a dice roll.
    check("no rolling judgement remains",
          not hasattr(factions, "judge_contract")
          and not hasattr(factions, "contract_chance")
          and not hasattr(factions, "ContractJudgement"))
    check("Crown tax is 100%", config.CROWN_TAX_BPS == 10_000)
    check("a kingdom holds six nations", config.MAX_NATIONS_PER_KINGDOM == 6)

    nation = payouts.nation_payout(1, 1, 1, 20)
    check("regular-soldier line is isolated",
          factions.regular_soldier_income(nation) == 400,
          factions.regular_soldier_income(nation))
    now = dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc)
    joined = (now - dt.timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%S")
    check("tax applies after 30 days",
          factions.crown_tax_due(
              faction=factions.CROWN, status="active", joined_at=joined,
              soldiers=20, result=nation, now=now,
          ) == 400)
    joined_recent = (now - dt.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
    check("grace blocks the tax",
          factions.crown_tax_due(
              faction=factions.CROWN, status="active", joined_at=joined_recent,
              soldiers=20, result=nation, now=now,
          ) == 0)
    check("low soldier count does not block the tax",
          factions.crown_tax_due(
              faction=factions.CROWN, status="active", joined_at=joined,
              soldiers=0, result=nation, now=now,
          ) == 400)
    active, reason = factions.tax_status(
        faction=factions.CROWN, status="active", joined_at=joined_recent,
        soldiers=20, now=now,
    )
    check("grace status is explicit", not active and "Grace" in reason, reason)


async def membership_and_tax(db):
    g = 1
    a = await db.create_nation(g, "Aurum", 111)
    await db.update_nation_stats(a, 10, 5, 10, 20)
    b = await db.create_nation(g, "Nineteen", 222)
    await db.update_nation_stats(b, 0, 0, 0, 19)

    result = await factions.join_faction(
        db, guild_id=g, account_type="nation", account_id=a,
        faction=factions.CROWN, actor_id=111,
    )
    check("Crown join succeeds", result.faction == factions.CROWN and not result.already)
    low = await factions.join_faction(
        db, guild_id=g, account_type="nation", account_id=b,
        faction=factions.CROWN, actor_id=222,
    )
    check("19 soldiers can join the Crown", low.status == "active")

    # Switch to Union, then create an active whole-balance commitment.
    switched = await factions.join_faction(
        db, guild_id=g, account_type="nation", account_id=a,
        faction=factions.UNION, actor_id=111,
    )
    check("switch records the old faction", switched.previous == factions.CROWN)
    check("Union membership is visible",
          await db.is_union_member(g, "nation", a))
    mirror = await db.fetchone(
        "SELECT * FROM union_members WHERE guild_id=? AND account_type='nation' AND account_id=?",
        (g, a),
    )
    check("legacy union mirror is written", mirror is not None)
    commit_id = await db.create_union_commit_request(g, "nation", a, 111)
    await db.decide_union_commit(commit_id, "active", 111)
    check("Union commitment is active", await db.is_union_committed(g, "nation", a))

    # Joining the Crown must cancel the Union commitment and clear the mirror.
    back = await factions.join_faction(
        db, guild_id=g, account_type="nation", account_id=a,
        faction=factions.CROWN, actor_id=111,
    )
    check("switching away cancels the Union commitment", back.cancelled_commit)
    commit = await db.get_union_commit(g, "nation", a)
    check("commitment row is cancelled", commit["status"] == "cancelled", commit["status"])
    mirror = await db.fetchone(
        "SELECT * FROM union_members WHERE guild_id=? AND account_type='nation' AND account_id=?",
        (g, a),
    )
    check("legacy union mirror is cleared", mirror is None)
    rows = await db.list_faction_members(g)
    check("one faction row per account", len(rows) == 2, len(rows))

    # Crown tax after the 30-day grace: 20 regular soldiers x 20 = 400.
    await db.execute(
        "UPDATE faction_members SET joined_at='2026-01-01T00:00:00' "
        "WHERE guild_id=? AND account_type='nation' AND account_id=?",
        (g, a),
    )
    cog = Payout.__new__(Payout)
    cog.bot = FakeBot(db)
    summary = await cog._run_cycle(g, natural=False)
    row = next(r for r in summary["nations"] if r[0] == "Aurum")
    check("payout row carries six fields", len(row) == 6, row)
    check("Crown tax is the regular line", row[5] == 400, row)
    check("account keeps the rest",
          int((await db.get_nation_by_id(a))["balance"]) == 24_650,
          (await db.get_nation_by_id(a))["balance"])
    check("Crown treasury receives the tax", await db.get_crown_balance(g) == 400,
          await db.get_crown_balance(g))
    tax_rows = await db.fetchall("SELECT * FROM transactions WHERE kind='crown_tax'")
    check("Crown tax has both ledger sides", len(tax_rows) == 2, len(tax_rows))

    # Grace: a recent join pays no Crown tax.
    await db.execute(
        "UPDATE faction_members SET joined_at=? "
        "WHERE guild_id=? AND account_type='nation' AND account_id=?",
        ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S"), g, a),
    )
    before = int((await db.get_nation_by_id(a))["balance"])
    grace = await cog._run_cycle(g, natural=False)
    after = int((await db.get_nation_by_id(a))["balance"])
    grace_row = next(r for r in grace["nations"] if r[0] == "Aurum")
    check("grace pays no Crown tax", grace_row[5] == 0, grace_row)
    check("grace leaves the full allowance", after - before == 25_050, after - before)

    # Low troop count is a soft disadvantage, never an ineligible status.
    await db.execute(
        "UPDATE faction_members SET joined_at='2026-01-01T00:00:00' "
        "WHERE guild_id=? AND account_type='nation' AND account_id=?",
        (g, a),
    )
    await db.update_nation_stats(a, 0, 0, 0, 9)
    low_tax = await cog._run_cycle(g, natural=False)
    member = await db.get_faction_member(g, "nation", a)
    low_row = next(r for r in low_tax["nations"] if r[0] == "Aurum")
    check("low-troop Crown member is still taxed", low_row[5] == 180, low_row)
    check("low-troop membership stays active", member["status"] == "active",
          member["status"])
    return a, b



async def contracts_and_delivery(db, a, b):
    g = 1
    await db.add_catalog_item({
        "key": "test:block", "display_name": "Test Block",
        "category": "Basic Building", "price": 1000,
        "unit_qty": 1, "stackable": 1, "max_stack": 64,
    })
    await db.add_catalog_item({
        "key": "test:ore", "display_name": "Test Ore",
        "category": "Materials", "price": 500,
        "unit_qty": 1, "stackable": 1, "max_stack": 64,
    })
    await fund_crown(db, g, 25_000_000)

    try:
        await factions.create_crown_contract(
            db, guild_id=g, account_type="nation", account_id=a,
            item_key="test:block", quantity=100, budget=99_999,
        )
        check("contract above the requested Crown budget is refused", False)
    except factions.CrownContractError:
        check("contract above the requested Crown budget is refused", True)
    check(
        "a refused Crown budget signs nothing",
        await db.count_active_crown_contracts(g, "nation", a) == 0,
    )

    first = await factions.create_crown_contract(
        db, guild_id=g, account_type="nation", account_id=a,
        item_key="test:block", quantity=100, budget=100_000,
    )
    check("contract value is item x demand",
          int(first["value"]) == 100_000, first["value"])
    try:
        await factions.create_crown_contract(
            db, guild_id=g, account_type="nation", account_id=a,
            item_key="test:ore", quantity=1,
        )
        check("nation holds only one active Crown contract", False)
    except factions.CrownContractError:
        check("nation holds only one active Crown contract", True)

    k = await db.create_kingdom(g, "Crownlands", 444)
    await db.set_nation_kingdom(a, k)
    await db.set_nation_kingdom(b, k)
    joined = await factions.join_faction(
        db, guild_id=g, account_type="kingdom", account_id=k,
        faction=factions.CROWN, actor_id=444,
    )
    check("kingdom joins with member soldiers", joined.faction == factions.CROWN)
    await factions.create_crown_contract(
        db, guild_id=g, account_type="kingdom", account_id=k,
        item_key="test:block", quantity=10,
    )
    await factions.create_crown_contract(
        db, guild_id=g, account_type="kingdom", account_id=k,
        item_key="test:ore", quantity=10,
    )
    try:
        await factions.create_crown_contract(
            db, guild_id=g, account_type="kingdom", account_id=k,
            item_key="test:block", quantity=5,
        )
        check("kingdom holds only two active Crown contracts", False)
    except factions.CrownContractError:
        check("kingdom holds only two active Crown contracts", True)

    c = await db.create_nation(g, "Capped", 555)
    await db.update_nation_stats(c, 0, 0, 0, 20)
    await db.add_balance("nations", c, 14_000_000)
    await factions.join_faction(
        db, guild_id=g, account_type="nation", account_id=c,
        faction=factions.CROWN, actor_id=555,
    )
    try:
        await factions.create_crown_contract(
            db, guild_id=g, account_type="nation", account_id=c,
            item_key="test:block", quantity=10_001,
        )
        check("contract above the treasury cap is refused", False)
    except factions.CrownContractError:
        check("contract above the treasury cap is refused", True)
    at_cap = await factions.create_crown_contract(
        db, guild_id=g, account_type="nation", account_id=c,
        item_key="test:block", quantity=10_000,
    )
    check("contract exactly at the cap is allowed",
          int(at_cap["value"]) == 10_000_000, at_cap["value"])
    await factions.cancel_crown_contract(
        db, guild_id=g, contract_id=int(at_cap["id"]), actor_id=555,
    )

    before = int((await db.get_nation_by_id(a))["balance"])
    crown_before = await db.get_crown_balance(g)
    partial = await factions.deliver_crown_items(
        db, guild_id=g, account_type="nation", account_id=a,
        item_key="test:block", quantity=40, actor_id=111,
    )
    check("partial delivery pays nothing", partial.paid == 0 and partial.delivered == 40)
    check("partial delivery leaves the Crown alone",
          await db.get_crown_balance(g) == crown_before)
    full = await factions.deliver_crown_items(
        db, guild_id=g, account_type="nation", account_id=a,
        item_key="test:block", quantity=60, actor_id=111,
    )
    check("full delivery pays the contract", full.paid == 100_000, full.paid)
    check("full delivery fulfils the contract", full.fulfilled == (int(first["id"]),))
    check("supplier is credited",
          int((await db.get_nation_by_id(a))["balance"]) == before + 100_000)
    check("Crown pays from its treasury",
          await db.get_crown_balance(g) == crown_before - 100_000)
    sale = await db.get_crown_sale(g, "nation", a, "test:block")
    check("first sale decays demand to 95%", int(sale["demand_bp"]) == 9_500,
          sale["demand_bp"])
    check("supplier status is recorded",
          int(sale["supplier_until_cycle"])
          == (await db.current_cycle(g)) + config.CROWN_SUPPLIER_CYCLES)

    second = await factions.create_crown_contract(
        db, guild_id=g, account_type="nation", account_id=a,
        item_key="test:block", quantity=100,
    )
    check("next contract sees 95% demand", int(second["value"]) == 95_000,
          second["value"])
    await factions.deliver_crown_items(
        db, guild_id=g, account_type="nation", account_id=a,
        item_key="test:block", quantity=100, actor_id=111,
    )
    sale = await db.get_crown_sale(g, "nation", a, "test:block")
    check("demand does not decay twice in one cycle",
          int(sale["demand_bp"]) == 9_500, sale["demand_bp"])

    await db.advance_contract_cycle(g, natural=True)
    third = await factions.create_crown_contract(
        db, guild_id=g, account_type="nation", account_id=a,
        item_key="test:block", quantity=100,
    )
    check("a contract before the next sale keeps 95% demand",
          int(third["value"]) == 95_000, third["value"])
    await factions.deliver_crown_items(
        db, guild_id=g, account_type="nation", account_id=a,
        item_key="test:block", quantity=100, actor_id=111,
    )
    sale = await db.get_crown_sale(g, "nation", a, "test:block")
    check("a sale in the next cycle drops demand to 90%",
          int(sale["demand_bp"]) == 9_000, sale["demand_bp"])
    fourth = await factions.create_crown_contract(
        db, guild_id=g, account_type="nation", account_id=a,
        item_key="test:block", quantity=100,
    )
    check("the following contract prices at 90% demand",
          int(fourth["value"]) == 90_000, fourth["value"])
    expired = await factions.expire_crown_contracts(
        db, g, int(fourth["deadline_cycle"])
    )
    check("overdue contract expires and frees the slot", expired >= 1, expired)
    check("expired contract is no longer active",
          (await db.get_crown_contract(int(fourth["id"])))["status"] == "expired")



async def donations_short_and_report(db, a):
    g = 1
    before = int((await db.get_nation_by_id(a))["balance"])
    crown_before = await db.get_crown_balance(g)
    await factions.donate_credits(
        db, guild_id=g, account_type="nation", account_id=a,
        faction=factions.CROWN, amount=5_000, actor_id=111,
    )
    check("credit donation moves credits",
          int((await db.get_nation_by_id(a))["balance"]) == before - 5_000)
    check("credit donation credits the Crown",
          await db.get_crown_balance(g) == crown_before + 5_000)
    pure = await factions.deliver_crown_items(
        db, guild_id=g, account_type="nation", account_id=a,
        item_key="test:ore", quantity=5, actor_id=111,
    )
    check("item with no contract is a pure donation",
          pure.paid == 0 and pure.delivered == 0 and pure.excess == 5, pure)

    # A short Crown rolls the whole delivery back.
    g2 = 2
    s = await db.create_nation(g2, "Short", 666)
    await db.update_nation_stats(s, 0, 0, 0, 20)
    await factions.join_faction(
        db, guild_id=g2, account_type="nation", account_id=s,
        faction=factions.CROWN, actor_id=666,
    )
    await fund_crown(db, g2, 100_000)
    short_contract = await factions.create_crown_contract(
        db, guild_id=g2, account_type="nation", account_id=s,
        item_key="test:block", quantity=100,
    )
    await db.debit_crown(g2, 60_000)
    balance_before = int((await db.get_nation_by_id(s))["balance"])
    try:
        await factions.deliver_crown_items(
            db, guild_id=g2, account_type="nation", account_id=s,
            item_key="test:block", quantity=100, actor_id=666,
        )
        check("short Crown refuses the delivery", False)
    except factions.CrownTreasuryShort:
        check("short Crown refuses the delivery", True)
    check("short refusal leaves the contract untouched",
          int((await db.get_crown_contract(int(short_contract["id"])))["delivered"]) == 0)
    check("short refusal moves no credits",
          int((await db.get_nation_by_id(s))["balance"]) == balance_before)
    await fund_crown(db, g2, 100_000)
    paid = await factions.deliver_crown_items(
        db, guild_id=g2, account_type="nation", account_id=s,
        item_key="test:block", quantity=100, actor_id=666,
    )
    check("funded Crown pays the contract", paid.paid == 100_000, paid)

    report = await factions.crown_report(
        db, guild_id=g, account_type="nation", account_id=a
    )
    context = albert_prompt.build_context(report)
    fallback = albert_prompt.fallback_reply(report, "Report.")
    budget_context = albert_prompt.build_context(report, requested_budget=75_000)
    check("Albert context names the account", "Aurum" in context)
    check("Albert context includes supplier history", "SUPPLIER HISTORY" in context)
    check("Albert context includes Crown treasury", "CROWN TREASURY" in context)
    check("Albert context includes a requested budget",
          "REQUESTED CONTRACT BUDGET" in budget_context
          and "75,000" in budget_context)
    check("Albert fallback is in character",
          "Royal Orders" in fallback and "Aurum" in fallback)
    check("Albert prompt allows every audience",
          "regardless of faction" in albert_prompt.ALBERT_SYSTEM_PROMPT)
    check("Albert context marks audience open", "AUDIENCE:" in context)

    a_tx = await db.fetchone(
        "SELECT COALESCE(SUM(amount),0) s FROM transactions "
        "WHERE account_type='nation' AND account_id=?", (a,),
    )
    crown_tx = await db.fetchone(
        "SELECT COALESCE(SUM(amount),0) s FROM transactions "
        "WHERE account_type='crown' AND account_id=?", (g,),
    )
    check("supplier account reconciles",
          int((await db.get_nation_by_id(a))["balance"]) == int(a_tx["s"]),
          (int((await db.get_nation_by_id(a))["balance"]), int(a_tx["s"])))
    check("Crown account reconciles",
          await db.get_crown_balance(g) == int(crown_tx["s"]),
          (await db.get_crown_balance(g), int(crown_tx["s"])))



async def command_checks(db):
    g = 1
    f = await db.create_nation(g, "Factionville", 333)
    await db.update_nation_stats(f, 0, 0, 0, 20)
    await fund_crown(db, g, 50_000)
    cog = Factions(FakeBot(db))
    interaction = FakeInteraction(uid=333)

    await Factions.faction_join.callback(
        cog, interaction, SimpleNamespace(value=factions.CROWN), "nation:Factionville"
    )
    check("command /faction join replies with allegiance",
          "Allegiance Sworn" in (interaction.response.sent[-1].title or ""),
          interaction.response.sent[-1].title)

    # Legacy Crown supplier contracts stay serviced: /faction contract no longer
    # creates them, but deliveries and payments must keep working for old rows.
    supplier = await factions.create_crown_contract(
        db, guild_id=g, account_type="nation", account_id=f,
        item_key="test:block", quantity=10,
    )
    await Factions.faction_deliver.callback(
        cog, interaction, "nation:Factionville", "test:block", 10
    )
    check("command /faction deliver delivers and pays",
          "Crown Delivery" in (interaction.response.sent[-1].title or ""),
          interaction.response.sent[-1].title)
    check("command /faction deliver paid the supplier",
          int((await db.get_nation_by_id(f))["balance"]) == 10_000,
          (await db.get_nation_by_id(f))["balance"])
    check("the legacy supplier contract was fulfilled",
          (await db.get_crown_contract(int(supplier["id"])))["status"] == "fulfilled")

    await Factions.faction_status.callback(
        cog, interaction, SimpleNamespace(value=factions.CROWN),
        "nation:Factionville",
    )
    check("command /faction status renders Albert's report",
          "Royal Orders" in (interaction.response.sent[-1].title or ""),
          interaction.response.sent[-1].title)

    union_nation = await db.create_nation(g, "Unionville", 444)
    await db.update_nation_stats(union_nation, 0, 0, 0, 5)
    await factions.join_faction(
        db, guild_id=g, account_type="nation",
        account_id=union_nation, faction=factions.UNION, actor_id=444,
    )
    union_interaction = FakeInteraction(uid=444)

    class FakeContractDesk:
        def __init__(self):
            self.calls = []

        async def request_contract(self, interaction, account, proposal, budget, faction="union"):
            self.calls.append((account, proposal, budget, faction))
            await interaction.response.send_message(embed=None)
            return True

    contract_desk = FakeContractDesk()
    cog.bot.cogs["Contract"] = contract_desk
    await Factions.faction_contract.callback(
        cog, union_interaction, SimpleNamespace(value=factions.UNION),
        "nation:Unionville", "A public road", 5_000,
    )
    check("command /faction contract routes Union work to Gilbert",
          contract_desk.calls == [
              ("nation:Unionville", "A public road", 5_000, factions.UNION)
          ], contract_desk.calls)

    # A non-member may request a Crown contract; allegiance is never required.
    free = await db.create_nation(g, "Freetown", 555)
    free_interaction = FakeInteraction(uid=555)
    await Factions.faction_contract.callback(
        cog, free_interaction, SimpleNamespace(value=factions.CROWN),
        "nation:Freetown", "A public well", 2_500,
    )
    check("a non-member can request a Crown contract",
          contract_desk.calls[-1] == (
              "nation:Freetown", "A public well", 2_500, factions.CROWN
          ), contract_desk.calls[-1])
    check("the request did not enrol the non-member",
          (await db.get_faction_member(g, "nation", free)) is None)

    blank = FakeInteraction(uid=444)
    await Factions.faction_contract.callback(
        cog, blank, SimpleNamespace(value=factions.UNION),
        "nation:Unionville", "   ", 5_000,
    )
    check("a blank proposal is refused",
          "Proposal Needed" in (blank.response.sent[-1].title or ""),
          blank.response.sent[-1].title)
    check("a refused proposal opens no case",
          len(contract_desk.calls) == 2)


    cog.bot.channel = FakeChannel()
    await db.set_union_commit_channel(g, cog.bot.channel.id)
    cog.bot.cogs["Union"] = Union(cog.bot)
    await Factions.faction_commitment.callback(
        cog, union_interaction, "nation:Unionville"
    )
    pending = await db.get_union_commit(g, "nation", union_nation)
    check("command /faction commitment routes to the Union desk",
          pending is not None and pending["status"] == "pending")
    check("Union /commit posts the approval request",
          len(cog.bot.channel.sent) == 1)

    await Factions.faction_uncommit.callback(
        cog, union_interaction, "nation:Unionville"
    )
    cancelled = await db.get_union_commit(g, "nation", union_nation)
    check("command /faction uncommit cancels a pending request",
          cancelled is not None and cancelled["status"] == "cancelled")

    # The hub forwards admin actions with ``.callback(...)``, which does not
    # run the ``@admin_only`` check registered on the delegate. A non-admin
    # could therefore mint credits into either faction treasury and drain
    # the Crown's, so the hub has to refuse them itself.
    pleb = FakeInteraction(uid=777, admin=False)
    crown_before = await db.get_crown_balance(g)
    await Factions.faction_fund.callback(
        cog, pleb, SimpleNamespace(value=factions.CROWN), 1_000_000
    )
    crown_minted = await db.get_crown_balance(g)
    check("non-admin /faction fund cannot mint Crown credits",
          crown_minted == crown_before, (crown_before, crown_minted))
    check("non-admin /faction fund is refused",
          "Admins Only" in (pleb.response.sent[-1].title or ""),
          pleb.response.sent[-1].title)

    pleb_withdraw = FakeInteraction(uid=777, admin=False)
    await Factions.faction_withdraw.callback(cog, pleb_withdraw, 500_000)
    crown_after_withdraw = await db.get_crown_balance(g)
    check("non-admin /faction withdraw cannot drain the Crown",
          crown_after_withdraw == crown_before,
          (crown_before, crown_after_withdraw))

    union_before = await db.get_union_balance(g)
    pleb_union = FakeInteraction(uid=777, admin=False)
    await Factions.faction_fund.callback(
        cog, pleb_union, SimpleNamespace(value=factions.UNION), 1_000_000
    )
    check("non-admin /faction fund cannot mint Union credits",
          await db.get_union_balance(g) == union_before)

    channel_before = (await db.get_settings(g))["union_commit_channel_id"]
    pleb_channel = FakeInteraction(uid=777, admin=False)
    await Factions.faction_setchannel.callback(
        cog, pleb_channel, SimpleNamespace(id=424242, name="hijacked", mention="<#424242>")
    )
    check("non-admin /faction setchannel cannot move the approval channel",
          (await db.get_settings(g))["union_commit_channel_id"] == channel_before,
          (channel_before, (await db.get_settings(g))["union_commit_channel_id"]))

    boss = FakeInteraction(uid=778, admin=True)
    await Factions.faction_fund.callback(
        cog, boss, SimpleNamespace(value=factions.CROWN), 1_000_000
    )
    check("an admin can still fund the Crown through /faction fund",
          await db.get_crown_balance(g) == crown_before + 1_000_000,
          (crown_before, await db.get_crown_balance(g)))


async def main():
    db = Database(os.path.join(tempfile.mkdtemp(), "factions.db"))
    await db.connect()
    try:
        pure_checks()
        a, b = await membership_and_tax(db)
        await contracts_and_delivery(db, a, b)
        await donations_short_and_report(db, a)
        await command_checks(db)
        await kingdom_cap_check(db)
    finally:
        await db.close()
    print("\nFAILURES:", len(fails))
    for failure in fails:
        print(" -", failure)
    raise SystemExit(1 if fails else 0)


async def kingdom_cap_check(db):
    """The command boundary is 6: the sixth joins, the seventh is refused."""
    g = 3
    kingdom_id = await db.create_kingdom(g, "Sixfold", 111)
    nations = []
    for index in range(1, 8):
        nations.append(await db.create_nation(g, f"Member{index}", 200 + index))
    for nation_id in nations[:5]:
        await db.set_nation_kingdom(nation_id, kingdom_id)
    cog = Kingdom(FakeBot(db))
    interaction = FakeInteraction(uid=111, admin=True)
    interaction.guild_id = g

    await Kingdom.addnation.callback(
        cog, interaction, "Sixfold", "Member6"
    )
    check("the sixth nation joins a kingdom",
          "Nation Joined" in (interaction.response.sent[-1].title or ""),
          interaction.response.sent[-1].title)
    await Kingdom.addnation.callback(
        cog, interaction, "Sixfold", "Member7"
    )
    check("the seventh nation is refused",
          "Kingdom Full" in (interaction.response.sent[-1].title or ""),
          interaction.response.sent[-1].title)
    check("the kingdom stops at six members",
          len(await db.nations_in_kingdom(kingdom_id)) == 6,
          len(await db.nations_in_kingdom(kingdom_id)))


if __name__ == "__main__":
    asyncio.run(main())
