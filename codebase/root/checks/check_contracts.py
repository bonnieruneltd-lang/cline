"""Contract lifecycle checks: rejection cannot be undone by close, and claims
re-read the status before paying.

These tests drive the real ``_pay_claim``/``_claimable_tranches`` helpers with a
throwaway SQLite database and a minimal fake cog, so no Discord login is needed.
"""
import asyncio
import os
import sys
import tempfile
import time
from types import SimpleNamespace

import discord

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from database import AccountMissing, Database, now_iso  # noqa: E402
from cogs.contract import ApprovalError, ClaimError, Contract  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


async def make_contract(db, guild_id, owner_id, budget=1000):
    cid = await db.create_contract(
        guild_id=guild_id,
        case_id=f"UN-T{guild_id}-{budget}",
        nation_id=owner_id,
        nation_name="Test Nation",
        nation_tag="TEST",
        requester_id=99,
        cycle=1,
        budget=budget,
        proposal="Test public road",
        owner_type="nation",
        owner_id=owner_id,
    )
    await db.approve_contract(
        cid,
        upfront=budget // 4,
        escrow=budget - budget // 4,
        approved_at=now_iso(),
        claim_account_type="nation",
        claim_account_id=owner_id,
    )
    return cid


async def make_pending(db, guild_id, owner_id, budget=1000, case_id=None, cycle=1):
    return await db.create_contract(
        guild_id=guild_id,
        case_id=case_id or f"UN-P{guild_id}-{budget}",
        nation_id=owner_id,
        nation_name="Pending Nation",
        nation_tag="PENDING",
        requester_id=99,
        cycle=cycle,
        budget=budget,
        proposal="Pending public road",
        owner_type="nation",
        owner_id=owner_id,
    )


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeMember:
    display_name = "Tester"


class FakeGuild:
    def get_member(self, member_id):
        return FakeMember()


class FakeChannel:
    def __init__(self):
        self.guild = FakeGuild()
        self.sent = []

    def typing(self):
        return FakeTyping()

    async def send(self, text=None, **kwargs):
        self.sent.append(text)


class CapturingAI:
    """Stand-in for GilbertAI that keeps the system prompt it was given."""

    def __init__(self):
        self.messages = None

    async def complete(self, messages):
        self.messages = messages
        return "Right then, let's have a look."


class FakeThread:
    _next_id = 5000

    def __init__(self, name, guild):
        FakeThread._next_id += 1
        self.id = FakeThread._next_id
        self.name = name
        self.mention = f"<#{self.id}>"
        self.guild = guild
        self.sent = []

    def typing(self):
        return FakeTyping()

    async def add_user(self, user):
        return None

    async def send(self, text=None, **kwargs):
        if text is not None:
            self.sent.append(text)

    async def delete(self):
        return None


class FakeParent:
    def __init__(self, guild):
        self.guild = guild
        self.id = 777
        self.category = None
        self.last_thread = None
        self.last_kwargs = {}

    async def create_thread(self, name, **kwargs):
        self.last_kwargs = dict(kwargs)
        self.last_thread = FakeThread(name, self.guild)
        return self.last_thread


class FakeRequestGuild:
    id = 5

    def __init__(self):
        self.me = None
        self.default_role = None
        self.parent = FakeParent(self)

    def get_channel(self, channel_id):
        return self.parent

    def get_role(self, role_id):
        return None

    def get_member(self, member_id):
        return FakeMember()


class FakeResp:
    def __init__(self):
        self.sent = []

    async def defer(self, **kwargs):
        return None

    async def send_message(self, embed=None, **kwargs):
        self.sent.append(embed)

    async def send(self, embed=None, **kwargs):
        self.sent.append(embed)


class FakeRequestInteraction:
    guild_id = 5

    def __init__(self, guild, uid):
        self.guild = guild
        self.user = SimpleNamespace(
            id=uid, display_name="Tester", mention=f"<@{uid}>",
            guild_permissions=SimpleNamespace(administrator=False),
        )
        self.response = FakeResp()
        self.followup = FakeResp()


async def main():
    db = Database(os.path.join(tempfile.mkdtemp(), "contracts.db"))
    await db.connect()
    guild = 1
    nid = await db.create_nation(guild, "Contract Test", 99)
    other_nid = await db.create_nation(2, "Other Guild", 98)
    await db.add_balance("nations", nid, 10_000)
    await db.credit_union(guild, 20_000)

    cog = object.__new__(Contract)
    cog.bot = SimpleNamespace(db=db)
    cog._thread_ids = set()
    cog._locks = {}
    actor = SimpleNamespace(id=42)

    # A normal approved claim pays the upfront tranche once.
    cid = await make_contract(db, guild, nid)
    contract = await db.get_contract(cid)
    paid, lines = await cog._pay_claim(contract, actor)
    check("approved claim pays the upfront tranche", paid == 250, (paid, lines))
    paid_again, _ = await cog._pay_claim(await db.get_contract(cid), actor)
    check("second claim is a no-op", paid_again == 0, paid_again)
    check("second claim leaves the Union alone", await db.get_union_balance(guild) == 19_750,
          await db.get_union_balance(guild))

    # Rejected contracts are not payable, and close cannot turn rejection into
    # an archive that makes the tranche claimable again.
    cid_rejected = await make_contract(db, guild, nid, budget=2_000)
    await db.update_contract(cid_rejected, status="rejected")
    rejected = await db.get_contract(cid_rejected)
    check("rejected contract has no claimable tranche",
          cog._claimable_tranches(rejected) == [], cog._claimable_tranches(rejected))
    try:
        await cog._pay_claim(rejected, actor)
        check("rejected claim is refused", False)
    except ClaimError:
        check("rejected claim is refused", True)
    check("rejected claim leaves the Union alone", await db.get_union_balance(guild) == 19_750,
          await db.get_union_balance(guild))

    won_close = await db.transition_contract(
        cid_rejected,
        from_statuses=("negotiating", "approved"),
        to_status="closed",
        closed_at=now_iso(),
    )
    still = await db.get_contract(cid_rejected)
    check("close cannot transition a rejected contract", not won_close and still["status"] == "rejected",
          (won_close, still["status"]))
    check("rejection still has no claimable tranche after close attempt",
          cog._claimable_tranches(still) == [], cog._claimable_tranches(still))

    # Reopen must not resurrect a rejection: the reopen path deliberately
    # excludes rejected/cancelled, so the forfeited tranche stays dead.
    check("rejected is not a reopenable status",
          "rejected" not in Contract.REOPENABLE_STATUSES,
          Contract.REOPENABLE_STATUSES)
    check("cancelled is not a reopenable status",
          "cancelled" not in Contract.REOPENABLE_STATUSES,
          Contract.REOPENABLE_STATUSES)
    won_reopen = await db.transition_contract(
        cid_rejected,
        from_statuses=Contract.REOPENABLE_STATUSES,
        to_status="negotiating",
        closed_at=None,
    )
    after_reopen = await db.get_contract(cid_rejected)
    check("reopen cannot transition a rejected contract",
          not won_reopen and after_reopen["status"] == "rejected",
          (won_reopen, after_reopen["status"]))
    check("rejection stays unclaimable after a reopen attempt",
          cog._claimable_tranches(after_reopen) == [],
          cog._claimable_tranches(after_reopen))

    # An approved contract can be archived and still released/claimed.
    cid_closed = await make_contract(db, guild, nid, budget=3_000)
    closed = await db.transition_contract(
        cid_closed,
        from_statuses=("negotiating", "approved"),
        to_status="closed",
        closed_at=now_iso(),
    )
    released = await db.transition_contract(
        cid_closed,
        from_statuses=("approved", "negotiating", "closed"),
        to_status="completed",
        released=2_250,
        completed_at=now_iso(),
    )
    closed_row = await db.get_contract(cid_closed)
    check("approved contract can be archived", closed, closed)
    check("closed contract can still be released", released, released)
    check("released closed contract exposes the final tranche",
          ("final", 2_250) in cog._claimable_tranches(closed_row),
          cog._claimable_tranches(closed_row))

    # A claim target in another guild is never accepted.
    cid_cross = await make_contract(db, guild, nid, budget=4_000)
    await db.update_contract(cid_cross, claim_account_id=other_nid)
    cross = await db.get_contract(cid_cross)
    target_type, target = await cog._claim_target(cross)
    check("cross-guild claim target is refused", target is None, (target_type, target))

    # Deleted destinations must fail loudly, not debit the source and pay zero.
    victim = await db.create_nation(guild, "Victim", 97)
    await db.add_balance("nations", victim, 500)
    await db.delete_nation(victim)
    before = int((await db.get_nation_by_id(nid))["balance"])
    try:
        await db.transfer_balance("nations", nid, "nations", victim, 100)
        check("transfer to a deleted account raises", False)
    except AccountMissing:
        check("transfer to a deleted account raises", True)
    check("failed transfer leaves the source untouched",
          int((await db.get_nation_by_id(nid))["balance"]) == before,
          (before, int((await db.get_nation_by_id(nid))["balance"])))
    try:
        await db.add_balance("nations", victim, 100)
        check("credit to a deleted account raises", False)
    except AccountMissing:
        check("credit to a deleted account raises", True)
    # The actual close command must also refuse a rejected contract, not just
    # the low-level transition helper.
    class FakeResponse:
        def __init__(self):
            self.sent = []
            self.done = False

        def is_done(self):
            return self.done

        async def send_message(self, embed=None, ephemeral=False):
            self.sent.append(embed)
            self.done = True

    fake_response = FakeResponse()
    fake_interaction = SimpleNamespace(
        guild_id=guild,
        user=SimpleNamespace(id=99),
        response=fake_response,
    )
    await cog.close_contract(fake_interaction, cid_rejected)
    after_close = await db.get_contract(cid_rejected)
    check("close command refuses a rejected contract",
          bool(fake_response.sent) and after_close["status"] == "rejected",
          (len(fake_response.sent), after_close["status"]))
    check("close command leaves rejection unclaimable",
          cog._claimable_tranches(after_close) == [], cog._claimable_tranches(after_close))

    # --- globally unique case numbers across guilds ----------------------
    n1 = await db.next_case_number(1, "AURUM")
    n2 = await db.next_case_number(2, "AURUM")
    check("case numbers are globally unique", n1 != n2, (n1, n2))
    raced = await asyncio.gather(
        db.next_case_number(1, "RACE"), db.next_case_number(2, "RACE")
    )
    check("concurrent case numbers are distinct", len(set(raced)) == 2, raced)

    # --- per-faction prefix counters -------------------------------------
    # A tag with no contracts yet: UN and CR each start at 001 and advance
    # independently, so the first Crown case is CR-TAG-001 even when the Union
    # already holds UN-TAG-001..NNN.
    un_starter = await db.next_case_number(1, "STARTER", prefix="UN")
    cr_starter = await db.next_case_number(1, "STARTER", prefix="CR")
    check(
        "UN and CR counters both start at 001",
        (un_starter, cr_starter) == (1, 1),
        (un_starter, cr_starter),
    )
    un_next = await db.next_case_number(1, "STARTER", prefix="UN")
    cr_next = await db.next_case_number(1, "STARTER", prefix="CR")
    check(
        "each prefix advances its own counter",
        (un_next, cr_next) == (2, 2),
        (un_next, cr_next),
    )
    un_aurum = await db.next_case_number(1, "AURUM", prefix="UN")
    cr_aurum = await db.next_case_number(1, "AURUM", prefix="CR")
    check(
        "UN continues at 003 while CR starts at 001 on the same tag",
        (un_aurum, cr_aurum) == (3, 1),
        (un_aurum, cr_aurum),
    )
    raced_prefix = await asyncio.gather(
        db.next_case_number(1, "MIXED", prefix="UN"),
        db.next_case_number(1, "MIXED", prefix="CR"),
    )
    check(
        "concurrent UN/CR reservations do not share a counter",
        tuple(raced_prefix) == (1, 1),
        raced_prefix,
    )

    # --- the cycle quota is enforced inside the insert --------------------
    quota_nation = await db.create_nation(9, "Quota", 77)
    quota_results = await asyncio.gather(
        db.create_contract(
            guild_id=9, case_id="UN-QUOTA-001", nation_id=quota_nation,
            nation_name="Quota", nation_tag="QUOTA", requester_id=77,
            cycle=9, budget=1_000, proposal="one", owner_id=quota_nation,
            quota_limit=1,
        ),
        db.create_contract(
            guild_id=9, case_id="UN-QUOTA-002", nation_id=quota_nation,
            nation_name="Quota", nation_tag="QUOTA", requester_id=77,
            cycle=9, budget=1_000, proposal="two", owner_id=quota_nation,
            quota_limit=1,
        ),
    )
    check("atomic quota creates exactly one contract",
          sorted(result > 0 for result in quota_results) == [False, True],
          quota_results)
    quota_rows = await db.count_contracts_for_cycle(9, "nation", quota_nation, 9)
    check("atomic quota leaves one row", quota_rows == 1, quota_rows)

    # --- approval re-reads status, owner funds and the Union treasury -----
    g3 = 3
    guild3_owner = await db.create_nation(g3, "Approval Test", 88)
    await db.add_balance("nations", guild3_owner, 10_000)
    await db.credit_union(g3, 5_000)

    cid_ok = await make_pending(db, g3, guild3_owner, budget=1_000, case_id="UN-APP-001")
    approved, claim_type, claim_row = await cog._approve_checked(cid_ok, None)
    check("approval helper funds a valid contract",
          approved["status"] == "approved" and claim_type == "nation"
          and int(claim_row["id"]) == guild3_owner,
          (approved["status"], claim_type, claim_row and claim_row["id"]))

    cid_stale = await make_pending(db, g3, guild3_owner, budget=1_000, case_id="UN-APP-002")
    await db.update_contract(cid_stale, status="rejected")
    try:
        await cog._approve_checked(cid_stale, None)
        check("approval re-reads a rejected status", False)
    except ApprovalError as exc:
        check("approval re-reads a rejected status", exc.title == "Wrong Status", exc.title)

    cid_owner = await make_pending(db, g3, guild3_owner, budget=1_000, case_id="UN-APP-003")
    await db.set_balance("nations", guild3_owner, 0)
    try:
        await cog._approve_checked(cid_owner, None)
        check("approval re-reads the owner balance", False)
    except ApprovalError as exc:
        check("approval re-reads the owner balance",
              exc.title == "Budget Exceeds Limit", exc.title)
    await db.set_balance("nations", guild3_owner, 10_000)

    cid_union = await make_pending(db, g3, guild3_owner, budget=1_000, case_id="UN-APP-004")
    await db.debit_union(g3, await db.get_union_balance(g3))
    try:
        await cog._approve_checked(cid_union, None)
        check("approval re-reads the Union treasury", False)
    except ApprovalError as exc:
        check("approval re-reads the Union treasury",
              exc.title == "Union Treasury Short", exc.title)

    # --- Crown-funded supply proposals --------------------------------
    # The Crown pays from its own treasury, its quota is separate from the
    # Union's, and a short Crown treasury refuses approval instead of minting.
    g4 = 4
    crown_owner = await db.create_nation(g4, "Crown Builder", 66)
    await db.add_balance("nations", crown_owner, 10_000)
    await db.credit_crown(g4, 5_000)
    union_before = await db.get_union_balance(g4)

    cid_crown = await db.create_contract(
        guild_id=g4, case_id="CR-CROWN-001", nation_id=crown_owner,
        nation_name="Crown Builder", nation_tag="CROWN", requester_id=66,
        cycle=1, budget=1_000, proposal="A public well", owner_id=crown_owner,
        quota_limit=1, faction="crown",
    )
    approved_crown, _, _ = await cog._approve_checked(cid_crown, None)
    check("a Crown contract is approved", approved_crown["status"] == "approved")
    check("Crown approval moves no credits",
          await db.get_crown_balance(g4) == 5_000,
          await db.get_crown_balance(g4))
    paid_crown, _ = await cog._pay_claim(await db.get_contract(cid_crown), actor)
    check("a Crown claim pays the upfront tranche", paid_crown == 250, paid_crown)
    check("the Crown paid, not the Union",
          await db.get_crown_balance(g4) == 4_750
          and await db.get_union_balance(g4) == union_before,
          (await db.get_crown_balance(g4), await db.get_union_balance(g4)))
    check("the quotas are per faction",
          await db.count_contracts_for_cycle(g4, "nation", crown_owner, 1, faction="union") == 0
          and await db.count_contracts_for_cycle(g4, "nation", crown_owner, 1, faction="crown") == 1,
          (await db.count_contracts_for_cycle(g4, "nation", crown_owner, 1, faction="union"),
           await db.count_contracts_for_cycle(g4, "nation", crown_owner, 1, faction="crown")))

    await db.debit_crown(g4, await db.get_crown_balance(g4))
    cid_short = await db.create_contract(
        guild_id=g4, case_id="CR-CROWN-002", nation_id=crown_owner,
        nation_name="Crown Builder", nation_tag="CROWN", requester_id=66,
        cycle=1, budget=1_000, proposal="A second well", owner_id=crown_owner,
        faction="crown",
    )
    try:
        await cog._approve_checked(cid_short, None)
        check("a short Crown treasury refuses approval", False)
    except ApprovalError as exc:
        check("a short Crown treasury refuses approval",
              exc.title == "Crown Treasury Short", exc.title)

    # --- a real Crown request opens a Crown thread with a CR case ID -----
    g5 = 5
    frontier = await db.create_nation(g5, "Frontier", 55)
    await db.add_balance("nations", frontier, 10_000)
    await db.set_contract_channel(g5, 777)
    flow_ai = CapturingAI()
    cog.ai = flow_ai
    cog.bot = SimpleNamespace(
        db=db, logger=SimpleNamespace(exception=lambda *a, **k: None)
    )
    request_guild = FakeRequestGuild()
    request = FakeRequestInteraction(request_guild, 55)
    opened = await cog.request_contract(
        request, "nation:Frontier", "A public bridge", 1_000, faction="crown"
    )
    check("a Crown request opens a case", opened is True)
    crown_rows = await db.list_contracts(g5, faction="crown")
    crown_case = crown_rows[0] if crown_rows else None
    check("the Crown case uses the CR prefix",
          bool(crown_case) and crown_case["case_id"].startswith("CR-"),
          crown_case and crown_case["case_id"])
    check("the case is recorded as Crown-funded",
          bool(crown_case) and crown_case["faction"] == "crown",
          crown_case and crown_case["faction"])
    thread = request_guild.parent.last_thread
    check("the thread is a Crown negotiation",
          thread is not None and thread.name.startswith("crown-contract-"),
          thread and thread.name)
    check("the confirmation names Albert",
          "Albert" in (request.followup.sent[-1].description or ""),
          request.followup.sent[-1].description)
    check("the opening turn briefs Albert",
          flow_ai.messages is not None
          and "You are Albert" in flow_ai.messages[0]["content"])
    check("the opening turn carries the live case ID",
          bool(crown_case)
          and f"Case ID: {crown_case['case_id']}" in flow_ai.messages[0]["content"])
    check("the opening turn calls it a proposal",
          "has proposed:" in flow_ai.messages[-1]["content"])
    check("a Crown request does not enrol the nation",
          (await db.get_faction_member(g5, "nation", frontier)) is None)

    # --- the opening briefing already holds the numbers ------------------
    # This is what the representative sees on the first turn: no dice, no
    # questions about stats the bot already knows.
    ai = CapturingAI()
    cog.ai = ai

    crown_channel = FakeChannel()
    await cog._agent_reply(
        await db.get_contract(cid_crown), crown_channel, user_text="Open it"
    )
    crown_system = ai.messages[0]["content"]
    check("Crown cases brief Albert", "You are Albert" in crown_system, crown_system[:60])
    check("Albert's briefing carries the case ID", "CR-CROWN-001" in crown_system)
    check("Albert's briefing labels the proposal", "Proposal brief:" in crown_system)
    check("Albert's briefing carries the budget", "1,000 credits" in crown_system)
    check("Albert's briefing carries the Crown treasury", "Crown treasury" in crown_system)
    check("Albert's briefing preloads soldiers", "Soldiers: " in crown_system)
    check("Albert's briefing preloads the quota", "Crown-funded contracts this cycle:" in crown_system)
    check("Albert is told membership is not required",
          "Membership is never required" in crown_system)
    check("the Crown reply was posted", "let's have a look" in (crown_channel.sent[-1] or ""))

    union_channel = FakeChannel()
    await cog._agent_reply(
        await db.get_contract(cid), union_channel, user_text="Open it"
    )
    union_system = ai.messages[0]["content"]
    check("Union cases brief Gilbert", "You are Gilbert" in union_system, union_system[:60])
    check("Gilbert's briefing carries the Union treasury", "Union treasury balance" in union_system)
    check("Gilbert's briefing labels the proposal", "Proposal brief:" in union_system)
    check("Gilbert's briefing preloads soldiers", "Soldiers fielded by this" in union_system)
    check("Gilbert is told membership is not required",
          "Membership is never required" in union_system)
    check("the Union reply was posted", "let's have a look" in (union_channel.sent[-1] or ""))

    # --- helpers can join a negotiation, and talks run side by side ---------
    # A negotiation is a private thread: only the requester and the Union Admins
    # are added when it opens, so a party could not bring a builder or treasurer
    # in to help. /contract invite adds them, and the persona has to answer them
    # (an added member talking to a wall is the bug this pins).
    class Person:
        def __init__(self, uid, name, *, admin=False, bot=False):
            self.id = uid
            self.display_name = name
            self.mention = f"<@{uid}>"
            self.bot = bot
            self.roles = []
            self.guild_permissions = SimpleNamespace(administrator=admin)

    class SpacePermissions:
        def __init__(self, inside):
            self.view_channel = inside
            self.send_messages = inside

    class SpaceThread:
        """Private-thread stand-in: membership decides who can see it."""

        def __init__(self, tid, guild, name="union-contract-space"):
            self.id = tid
            self.name = name
            self.guild = guild
            self.mention = f"<#{tid}>"
            self.members = set()
            self.sent = []
            self.archived = False
            self.locked = False
            self.invitable = False
            self.added = []

        def typing(self):
            return FakeTyping()

        async def add_user(self, member):
            if self.archived or self.locked:
                raise discord.HTTPException(
                    SimpleNamespace(status=403, reason="Forbidden"), "Forbidden"
                )
            self.members.add(member.id)
            self.added.append(member.id)

        async def edit(self, **kwargs):
            if "archived" in kwargs:
                self.archived = bool(kwargs["archived"])
            if "locked" in kwargs:
                self.locked = bool(kwargs["locked"])
            if "invitable" in kwargs:
                self.invitable = bool(kwargs["invitable"])

        async def fetch_member(self, user_id):
            """Discord's thread-membership probe: absent means NotFound."""
            if int(user_id) not in self.members:
                raise discord.NotFound(
                    SimpleNamespace(status=404, reason="Not Found"), "Unknown Member"
                )
            return SimpleNamespace(id=int(user_id))

        def permissions_for(self, member):
            # Deliberately permissive, like a real Thread: discord.py resolves
            # the *parent* channel, so a stranger can look permitted here. The
            # join test below proves the membership probe is what decides.
            return SpacePermissions(True)

        async def send(self, text=None, embed=None, **kwargs):
            self.sent.append(text if text is not None else embed)

    class SpaceChannel:
        """Text-channel ticket stand-in: overwrites decide who may speak."""

        def __init__(self, cid, guild=None):
            self.id = cid
            self.name = "union-contract-channel"
            self.mention = f"<#{cid}>"
            self.guild = guild
            self.overwrites = {}
            self.sent = []

        def typing(self):
            return FakeTyping()

        async def set_permissions(self, member, **kwargs):
            self.overwrites[member.id] = kwargs

        def permissions_for(self, member):
            return SpacePermissions(member.id in self.overwrites)

        async def send(self, text=None, embed=None, **kwargs):
            self.sent.append(text if text is not None else embed)

    class SpaceGuild:
        id = 1

        def __init__(self, spaces):
            self.spaces = spaces
            self.default_role = None
            self.me = None

        def get_channel(self, channel_id):
            return self.spaces.get(int(channel_id))

        def get_role(self, role_id):
            return None

        def get_member(self, member_id):
            return None

    class InviteInteraction:
        def __init__(self, guild, user, channel_id=None):
            self.guild = guild
            self.guild_id = guild.id
            self.channel_id = channel_id
            self.user = user
            self.response = FakeResp()
            self.followup = FakeResp()

    class EchoAI:
        """Replies with the turn it was given, so histories can be compared.

        ``expect_concurrent`` turns the timing test into a *gate*: each call
        waits until that many calls are in flight (or until a short deadline
        passes), so a run that really does answer negotiations in parallel
        reaches the peak deterministically instead of hoping the scheduler
        lines twenty tasks up. A global lock would never reach it, and the
        deadline keeps a serialised implementation from hanging the suite.
        """

        def __init__(self, delay=0.0, expect_concurrent=0, gate_timeout=2.0):
            self.delay = delay
            self.expect_concurrent = int(expect_concurrent or 0)
            self.gate_timeout = gate_timeout
            self.gate = asyncio.Event()
            self.gate_deadline = None
            self.inflight = 0
            self.peak = 0

        async def _wait_for_gate(self) -> None:
            if not self.expect_concurrent or self.inflight >= self.expect_concurrent:
                self.gate.set()
                return
            if self.gate_deadline is None:
                self.gate_deadline = time.monotonic() + self.gate_timeout
            remaining = self.gate_deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(self.gate.wait(), remaining)
            except asyncio.TimeoutError:
                pass

        async def complete(self, messages):
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            try:
                await self._wait_for_gate()
                if self.delay:
                    await asyncio.sleep(self.delay)
                return f"echo:{messages[-1]['content']}"
            finally:
                self.inflight -= 1

    helper = Person(701, "Helper")
    outsider = Person(702, "Outsider")
    second_helper = Person(703, "Treasurer")
    leader = Person(99, "Leader")

    space_a = SpaceThread(8101, None)
    space_b = SpaceThread(8102, None)
    guild = SpaceGuild({8101: space_a, 8102: space_b})
    space_a.guild = guild
    space_b.guild = guild
    # cog_load() warms this cache from the contracts table; the fakes are not
    # discord.Thread instances, so seed it the same way here.
    cog._thread_ids = {space_a.id, space_b.id}

    cid_a = await make_pending(db, guild.id, nid, budget=1_500, case_id="UN-INVITE-001")
    cid_b = await make_pending(db, guild.id, nid, budget=1_700, case_id="UN-INVITE-002")
    await db.set_contract_thread(cid_a, space_a.id)
    await db.set_contract_thread(cid_b, space_b.id)

    cog.bot = SimpleNamespace(
        db=db, logger=SimpleNamespace(exception=lambda *a, **k: None)
    )
    cog.ai = EchoAI(delay=0.05)

    def message(person, text, space):
        return SimpleNamespace(
            author=person,
            guild=guild,
            content=text,
            channel=space,
            id=hash((person.id, text)) & 0xFFFF,
        )

    # A member who is not in the space is ignored: private means private.
    await cog.on_message(message(outsider, "I am just reading along", space_a))
    check("a stranger is not answered in a negotiation",
          space_a.sent == [] and space_a.members == set(), space_a.sent)

    # Two negotiations talk at the same time: the lock is per thread, not global.
    await asyncio.gather(
        cog.on_message(message(leader, "Alice: first question", space_a)),
        cog.on_message(message(leader, "Bob: second question", space_b)),
    )
    check("two negotiations are answered in parallel", cog.ai.peak == 2, cog.ai.peak)
    check("each negotiation posts its own reply",
          len(space_a.sent) == 1 and len(space_b.sent) == 1,
          (space_a.sent, space_b.sent))
    history_a = await db.get_contract(cid_a)
    history_b = await db.get_contract(cid_b)
    check("the first conversation keeps its own history",
          "Alice: first question" in (history_a["history"] or "")
          and "Bob: second question" not in (history_a["history"] or ""),
          history_a["history"])
    check("the second conversation keeps its own history",
          "Bob: second question" in (history_b["history"] or "")
          and "Alice: first question" not in (history_b["history"] or ""),
          history_b["history"])

    # The party invites a helper; only the requester (or a Union Admin) may.
    invite = InviteInteraction(guild, leader, channel_id=space_a.id)
    await Contract.invite.callback(cog, invite, helper)
    check("the requester can invite a helper", helper.id in space_a.members, space_a.members)
    check("an older ticket is made invitable for next time",
          space_a.invitable is True, space_a.invitable)
    check("the invite is confirmed",
          bool(invite.response.sent)
          and invite.response.sent[-1].title.endswith("Member Added"),
          invite.response.sent and invite.response.sent[-1].title)
    announced = [
        item.title for item in space_a.sent
        if isinstance(getattr(item, "title", None), str)
    ]
    check("the space announces the new member",
          any("Added to the Negotiation" in title for title in announced),
          announced)

    # The invited helper is answered from then on.
    space_a.sent.clear()
    await cog.on_message(message(helper, "What about the foundation depth?", space_a))
    check("the invited helper is answered", len(space_a.sent) == 1, space_a.sent)
    helper_history = await db.get_contract(cid_a)
    check("the helper's turn reaches the model",
          "Helper: What about the foundation depth?" in (helper_history["history"] or ""),
          helper_history["history"])

    # A helper cannot hand out access, and a finished ticket takes nobody.
    blocked = InviteInteraction(guild, helper, channel_id=space_a.id)
    await Contract.invite.callback(cog, blocked, second_helper)
    check("a helper cannot invite others",
          second_helper.id not in space_a.members
          and "Not Authorized" in blocked.response.sent[-1].title,
          blocked.response.sent[-1].title)

    # An archived ticket is reopened so the invite still lands.
    space_a.archived = True
    space_a.locked = True
    reopen = InviteInteraction(guild, leader, channel_id=space_a.id)
    await Contract.invite.callback(cog, reopen, second_helper)
    check("an archived ticket is reopened for the invite",
          second_helper.id in space_a.members and not space_a.archived,
          (second_helper.id in space_a.members, space_a.archived))

    await db.update_contract(cid_b, status="rejected")
    closed = InviteInteraction(guild, leader, channel_id=space_b.id)
    await Contract.invite.callback(cog, closed, outsider)
    check("a finished ticket takes no new people",
          outsider.id not in space_b.members
          and "Ticket Closed" in closed.response.sent[-1].title,
          closed.response.sent[-1].title)

    # The channel-ticket fallback grants an overwrite instead of membership,
    # and there the resolved permissions are what let the helper speak.
    channel_space = SpaceChannel(8201, guild)
    guild.spaces[channel_space.id] = channel_space
    cog._thread_ids.add(channel_space.id)
    cid_c = await make_pending(db, guild.id, nid, budget=900, case_id="UN-INVITE-003")
    await db.set_contract_thread(cid_c, channel_space.id)
    channel_invite = InviteInteraction(guild, leader, channel_id=channel_space.id)
    await Contract.invite.callback(cog, channel_invite, helper)
    check("a channel ticket grants an overwrite",
          helper.id in channel_space.overwrites, channel_space.overwrites)
    channel_space.sent.clear()
    await cog.on_message(message(helper, "Can we move the delivery date?", channel_space))
    check("the helper is answered in a channel ticket",
          len(channel_space.sent) == 1, channel_space.sent)
    channel_space.sent.clear()
    await cog.on_message(message(outsider, "Let me in too", channel_space))
    check("a stranger is still ignored in a channel ticket",
          channel_space.sent == [], channel_space.sent)

    # The thread is created invitable, so Discord's own Add Members works too.
    check("negotiation threads are invitable",
          request_guild.parent.last_kwargs.get("invitable") is True,
          request_guild.parent.last_kwargs)

    # --- capacity: more than twenty parties at once -------------------------
    # "Can it handle 20+ at the same time?" is answered here: the database
    # serialises writes and the case counter is transactional, so a burst must
    # still produce unique case IDs, one thread each and no lost rows.
    class BurstParent:
        def __init__(self, guild):
            self.guild = guild
            self.id = 777
            self.category = None
            self.created = 0

        async def create_thread(self, name, **kwargs):
            await asyncio.sleep(0.002)
            self.created += 1
            thread = FakeThread(name, self.guild)
            self.guild.threads.append(thread)
            return thread

    class BurstGuild:
        id = 5

        def __init__(self):
            self.default_role = None
            self.me = None
            self.parent = BurstParent(self)
            self.threads = []
            self.channels = []
            self.created = self.threads

        def get_channel(self, channel_id):
            return self.parent

        def get_role(self, role_id):
            return None

        def get_member(self, member_id):
            return None

    await db.set_contract_channel(5, 777)
    burst_guild = BurstGuild()
    burst_cog = object.__new__(Contract)
    logs = []
    burst_cog.bot = SimpleNamespace(
        db=db,
        logger=SimpleNamespace(
            exception=lambda *a, **k: logs.append(("exception", a)),
            warning=lambda *a, **k: logs.append(("warning", a)),
            info=lambda *a, **k: None,
        ),
    )
    burst_cog._thread_ids = set()
    burst_cog._locks = {}
    burst_cog.ai = EchoAI()

    BURST = 25
    burst_nations = []
    for i in range(BURST):
        nation_id = await db.create_nation(5, f"Burst {i:02d}", 500 + i)
        await db.add_balance("nations", nation_id, 50_000)
        burst_nations.append(nation_id)
    burst_interactions = [FakeRequestInteraction(burst_guild, 500 + i) for i in range(BURST)]
    burst_results = await asyncio.gather(*[
        burst_cog.request_contract(
            burst_interactions[i], f"nation:Burst {i:02d}", f"Project {i}", 1_000
        )
        for i in range(BURST)
    ], return_exceptions=True)
    burst_errors = [r for r in burst_results if isinstance(r, Exception)]
    burst_rows = await db.fetchall(
        "SELECT case_id, thread_id FROM contracts WHERE guild_id = 5 AND requester_id >= 500"
    )
    burst_cases = [row["case_id"] for row in burst_rows]
    check("25 simultaneous requests all open", sum(1 for r in burst_results if r is True) == BURST,
          [type(e).__name__ for e in burst_errors[:3]])
    check("25 simultaneous requests raise nothing", not burst_errors,
          [type(e).__name__ for e in burst_errors[:3]])
    check("every burst case ID is unique", len(set(burst_cases)) == BURST, len(set(burst_cases)))
    check("every burst contract has its own thread",
          len(burst_rows) == BURST and all(row["thread_id"] for row in burst_rows),
          len(burst_rows))
    check("the burst opened one thread per case", burst_guild.parent.created == BURST,
          burst_guild.parent.created)
    check("no burst row was lost to a case-number race",
          len({row["thread_id"] for row in burst_rows}) == BURST,
          len({row["thread_id"] for row in burst_rows}))

    # Twenty negotiations talking at the same time: one lock per thread, so the
    # AI is entered concurrently, and no history leaks across cases.
    talk_ai = EchoAI(delay=0.03, expect_concurrent=20)
    burst_cog.ai = talk_ai
    talk_spaces = []
    for i in range(20):
        space = SpaceThread(9100 + i, burst_guild)
        space.members.add(600 + i)
        contract_id = await db.create_contract(
            guild_id=5, case_id=f"UN-TALK-{i:03d}", nation_id=burst_nations[i],
            nation_name=f"Burst {i:02d}", nation_tag="TALK", requester_id=600 + i,
            cycle=1, budget=1_000, proposal="bridge", owner_type="nation",
            owner_id=burst_nations[i],
        )
        await db.set_contract_thread(contract_id, space.id)
        burst_cog._thread_ids.add(space.id)
        talk_spaces.append(space)
    await asyncio.gather(*[
        burst_cog.on_message(SimpleNamespace(
            author=Person(600 + i, f"Speaker {i}"), guild=burst_guild,
            content=f"turn[{i}]", channel=talk_spaces[i], id=i, bot=False,
        ))
        for i in range(20)
    ])
    talk_rows = await db.fetchall(
        "SELECT case_id, history FROM contracts WHERE case_id LIKE 'UN-TALK-%' ORDER BY case_id"
    )
    foreign = [
        row["case_id"] for row in talk_rows
        if sum(f"turn[{i}]" in (row["history"] or "") for i in range(20)) > 1
    ]
    check("twenty negotiations are answered at once", talk_ai.peak == 20, talk_ai.peak)
    check("every one of the twenty gets a reply",
          all(len(space.sent) == 1 for space in talk_spaces),
          [len(space.sent) for space in talk_spaces])
    check("no turn leaks into another negotiation",
          not foreign and len(talk_rows) == 20, foreign[:3])

    # --- memory: a long case does not forget its own beginning --------------
    # Storage keeps every turn; the prompt gets a digest of the older ones. This
    # is the difference between "we agreed the east road on Tuesday" and a
    # representative who contradicts himself on Thursday.
    class RecordingAI:
        def __init__(self):
            self.calls = []

        async def complete(self, messages):
            self.calls.append(messages)
            return f"noted:{messages[-1]['content']}"

    memory_ai = RecordingAI()
    burst_cog.ai = memory_ai
    long_space = SpaceThread(9400, burst_guild)
    long_requester = Person(700, "Long Talker")
    long_space.members.add(long_requester.id)
    long_id = await db.create_contract(
        guild_id=5, case_id="UN-MEMORY-001", nation_id=burst_nations[1],
        nation_name="Burst 01", nation_tag="MEMO", requester_id=long_requester.id,
        cycle=1, budget=1_000, proposal="east road", owner_type="nation",
        owner_id=burst_nations[1],
    )
    await db.set_contract_thread(long_id, long_space.id)
    burst_cog._thread_ids.add(long_space.id)
    for index in range(26):
        await burst_cog.on_message(SimpleNamespace(
            author=long_requester, guild=burst_guild,
            content=("first message: the east road, paid on delivery" if index == 0
                     else f"message {index}"),
            channel=long_space, id=10_000 + index, bot=False,
        ))
    transcript = await db.persona_turns(
        guild_id=5, account_type="nation", account_id=burst_nations[1],
        npc="gilbert", contract_id=long_id,
    )
    check("every turn of a long case is stored", len(transcript) == 52, len(transcript))
    check("the stored transcript keeps the opening line",
          "first message" in transcript[0]["content"], transcript[0]["content"][:40])
    last_prompt = memory_ai.calls[-1]
    check("an over-long case still sends a digest",
          any(m["role"] == "system" and m["content"].startswith(
              "EARLIER IN THIS NEGOTIATION") for m in last_prompt),
          [m["content"][:40] for m in last_prompt if m["role"] == "system"])
    check("the digest remembers the opening of the case",
          any("first message" in m["content"] for m in last_prompt))
    check("the verbatim window stays bounded",
          sum(1 for m in last_prompt if m["role"] in ("user", "assistant"))
          == 16,
          sum(1 for m in last_prompt if m["role"] in ("user", "assistant")))
    check("the legacy history column still works for other code",
          (await db.get_contract(long_id))["history"].count("message") >= 30)

    # A second case with the same nation is briefed with what happened in the
    # first one: continuity across negotiations, never the current transcript.
    prior_space = SpaceThread(9401, burst_guild)
    prior_requester = Person(701, "Same Nation")
    prior_space.members.add(prior_requester.id)
    prior_id = await db.create_contract(
        guild_id=5, case_id="UN-MEMORY-002", nation_id=burst_nations[1],
        nation_name="Burst 01", nation_tag="MEMO", requester_id=prior_requester.id,
        cycle=2, budget=1_000, proposal="west road", owner_type="nation",
        owner_id=burst_nations[1],
    )
    await db.set_contract_thread(prior_id, prior_space.id)
    burst_cog._thread_ids.add(prior_space.id)
    await burst_cog.on_message(SimpleNamespace(
        author=prior_requester, guild=burst_guild,
        content="a brand new question", channel=prior_space, id=11_000, bot=False,
    ))
    new_prompt = memory_ai.calls[-1]
    recall = [m["content"] for m in new_prompt
              if m["role"] == "system" and m["content"].startswith("PRIOR EXCHANGES")]
    check("a new case recalls the earlier one", bool(recall), [m["content"][:40] for m in new_prompt])
    check("the recall quotes the earlier case",
          bool(recall) and "message 25" in recall[0], (recall or [""])[0][:80])
    check("the recall does not carry the current case",
          bool(recall) and "a brand new question" not in recall[0])
    # A party that has never negotiated before opens with a clean slate.
    fresh_nation = await db.create_nation(5, "Fresh Start", 702)
    await db.add_balance("nations", fresh_nation, 50_000)
    fresh_space = SpaceThread(9402, burst_guild)
    fresh_member = Person(703, "Newcomer")
    fresh_space.members.add(fresh_member.id)
    fresh_id = await db.create_contract(
        guild_id=5, case_id="UN-MEMORY-003", nation_id=fresh_nation,
        nation_name="Fresh Start", nation_tag="NEW", requester_id=fresh_member.id,
        cycle=1, budget=1_000, proposal="first ever deal", owner_type="nation",
        owner_id=fresh_nation,
    )
    await db.set_contract_thread(fresh_id, fresh_space.id)
    burst_cog._thread_ids.add(fresh_space.id)
    await burst_cog.on_message(SimpleNamespace(
        author=fresh_member, guild=burst_guild,
        content="our very first contract", channel=fresh_space, id=11_001, bot=False,
    ))
    check("a first-time party gets no recall block",
          not any(m["content"].startswith("PRIOR EXCHANGES")
                  for m in memory_ai.calls[-1]),
          [m["content"][:30] for m in memory_ai.calls[-1] if m["role"] == "system"])

    # Discord rate limits: a refused thread falls back to a channel ticket, and
    # when both routes are limited the request fails cleanly with no row.
    class LimitedParent:
        def __init__(self, guild):
            self.guild = guild
            self.id = 777
            self.category = None

        async def create_thread(self, name, **kwargs):
            raise discord.HTTPException(
                SimpleNamespace(status=429, reason="Too Many Requests"), "rate limited"
            )

    class LimitedGuild:
        def __init__(self, guild_id, channel_ok):
            self.id = guild_id
            self.default_role = None
            self.me = None
            self.parent = LimitedParent(self)
            self.channel_ok = channel_ok
            self.channels = []

        def get_channel(self, channel_id):
            return self.parent

        def get_role(self, role_id):
            return None

        def get_member(self, member_id):
            return None

        async def create_text_channel(self, name, **kwargs):
            if not self.channel_ok:
                raise discord.HTTPException(
                    SimpleNamespace(status=429, reason="Too Many Requests"), "rate limited"
                )
            channel = SpaceChannel(8300 + len(self.channels), self)
            self.channels.append(channel)
            return channel

    class BurstInteraction:
        def __init__(self, guild, user):
            self.guild = guild
            self.guild_id = guild.id
            self.user = user
            self.response = FakeResp()
            self.followup = FakeResp()

    async def limited_run(guild_id, channel_ok, count=3):
        await db.set_contract_channel(guild_id, 777)
        guild = LimitedGuild(guild_id, channel_ok)
        cog2 = object.__new__(Contract)
        cog2.bot = SimpleNamespace(
            db=db, logger=SimpleNamespace(
                exception=lambda *a, **k: None, warning=lambda *a, **k: None,
                info=lambda *a, **k: None,
            )
        )
        cog2._thread_ids = set()
        cog2._locks = {}
        cog2.ai = EchoAI()
        interactions = []
        for i in range(count):
            nation_id = await db.create_nation(guild_id, f"Limit {i:02d}", 700 + i)
            await db.add_balance("nations", nation_id, 50_000)
            interactions.append(BurstInteraction(guild, Person(700 + i, f"Speaker {i}")))
        results = await asyncio.gather(*[
            cog2.request_contract(interactions[i], f"nation:Limit {i:02d}", "A bridge", 1_000)
            for i in range(count)
        ], return_exceptions=True)
        rows = await db.fetchall(
            "SELECT thread_id FROM contracts WHERE guild_id = ? AND requester_id >= 700",
            (guild_id,),
        )
        failures = sum(
            "Ticket Failed" in (embed.title or "")
            for interaction in interactions for embed in interaction.followup.sent
            if getattr(embed, "title", None)
        )
        return results, rows, guild, failures

    results, rows, guild6, failures = await limited_run(6, channel_ok=True)
    check("a rate-limited thread falls back to a channel ticket",
          sum(1 for r in results if r is True) == 3 and len(rows) == 3
          and len(guild6.channels) == 3 and failures == 0,
          (sum(1 for r in results if r is True), len(rows), len(guild6.channels), failures))
    check("the fallback records the channel as the ticket",
          all(row["thread_id"] in {c.id for c in guild6.channels} for row in rows),
          [row["thread_id"] for row in rows])

    results, rows, guild7, failures = await limited_run(7, channel_ok=False)
    check("a doubly rate-limited request fails cleanly",
          sum(1 for r in results if r is True) == 0 and rows == [] and failures == 3,
          (sum(1 for r in results if r is True), len(rows), failures))

    # A Union Admin that cannot be added is reported, never silently skipped.
    class FailingThread:
        name = "union-contract-failing"

        async def add_user(self, member):
            raise discord.HTTPException(
                SimpleNamespace(status=403, reason="Forbidden"), "forbidden"
            )

    class AdminRole:
        def __init__(self, members):
            self.members = members

    class AdminGuild:
        def get_role(self, role_id):
            return AdminRole([Person(800, "Admin A"), Person(801, "Admin B")])

    warnings = []
    burst_cog.bot = SimpleNamespace(
        db=db,
        logger=SimpleNamespace(
            exception=lambda *a, **k: None,
            warning=lambda *a, **k: warnings.append(a),
            info=lambda *a, **k: None,
        ),
    )
    await burst_cog._add_union_admins(FailingThread(), AdminGuild(), {"union_admin_role_id": 9})
    check("a failed Union Admin add is logged, not swallowed",
          bool(warnings) and "Union Admin" in str(warnings[0]), warnings[:1])

    await db.close()
    print("\nFAILURES:", len(fails))
    for failure in fails:
        print(" -", failure)


try:
    asyncio.run(main())
except Exception as exc:
    print("ERROR:", type(exc).__name__, exc)
    for failure in fails:
        print(" -", failure)
    raise SystemExit(1)
raise SystemExit(1 if fails else 0)

