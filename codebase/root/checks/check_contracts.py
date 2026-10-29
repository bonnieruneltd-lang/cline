"""Contract lifecycle checks: rejection cannot be undone by close, and claims
re-read the status before paying.

These tests drive the real ``_pay_claim``/``_claimable_tranches`` helpers with a
throwaway SQLite database and a minimal fake cog, so no Discord login is needed.
"""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

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

    async def create_thread(self, name, **kwargs):
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

