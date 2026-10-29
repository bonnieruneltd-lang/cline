"""Database integrity and concurrency checks on throwaway SQLite files."""
import asyncio, os, sys, tempfile, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from database import Database, InsufficientFunds, now_iso
import config

fails = []
def check(label, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + label + (("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(label)

TMP = tempfile.mkdtemp()
DBPATH = os.path.join(TMP, "t.db")

async def main():
    db = Database(DBPATH)
    await db.connect()
    g = 1

    # --- basic account + debit guard --------------------------------------
    nid = await db.create_nation(g, "Aurum", 111)
    kid = await db.create_kingdom(g, "Crown", 222)
    await db.add_balance("nations", nid, 1000)
    try:
        await db.debit("nations", nid, 1001); check("debit overdraw raises", False)
    except InsufficientFunds as e:
        check("debit overdraw raises", e.balance == 1000)
    check("no negative balance", (await db.get_nation_by_id(nid))["balance"] == 1000)

    # --- transfer atomicity ----------------------------------------------
    s, d = await db.transfer_balance("nations", nid, "kingdoms", kid, 400)
    check("transfer balances", (s, d) == (600, 400), (s, d))
    try:
        await db.transfer_balance("nations", nid, "kingdoms", kid, 10_000)
    except InsufficientFunds:
        pass
    check("failed transfer rolls back", (await db.get_nation_by_id(nid))["balance"] == 600
          and (await db.get_kingdom_by_id(kid))["balance"] == 400)

    # --- nested transaction joins outer --------------------------------
    try:
        async with db.transaction():
            await db.add_balance("nations", nid, 50)
            async with db.transaction():
                await db.add_balance("nations", nid, 50)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    check("nested tx rolled back with outer", (await db.get_nation_by_id(nid))["balance"] == 600,
          (await db.get_nation_by_id(nid))["balance"])

    # --- individual cap ---------------------------------------------------
    added, bal = await db.credit_individual(g, 1, 5)
    check("first credit creates wallet", (added, bal) == (5, 5), (added, bal))
    added, bal = await db.credit_individual(g, 1, config.INDIVIDUAL_MAX_CREDITS)
    check("cap trims credit", added == config.INDIVIDUAL_MAX_CREDITS - 5 and bal == config.INDIVIDUAL_MAX_CREDITS, (added, bal))
    added, bal = await db.credit_individual(g, 1, 1000)
    check("cap refuses more", added == 0 and bal == config.INDIVIDUAL_MAX_CREDITS, (added, bal))

    # --- daily claim guard ------------------------------------------------
    await db.get_individual(g, 2, "Two")
    ok1 = await db.claim_individual_daily(g, 2, expected_last=None, new_last="2026-10-22T00:00:00", streak=1, multiplier_bp=10000)
    ok2 = await db.claim_individual_daily(g, 2, expected_last=None, new_last="2026-10-22T00:00:01", streak=1, multiplier_bp=10000)
    row = await db.find_individual(g, 2)
    check("daily guarded once", ok1 and not ok2 and row["last_daily_at"] == "2026-10-22T00:00:00", (ok1, ok2, row and row["last_daily_at"]))
    check("claim on missing wallet is a no-op",
          await db.claim_individual_daily(g, 77, expected_last=None, new_last="x",
                                          streak=1, multiplier_bp=10000) is False)

    # --- delete individual erases ledger rows -----------------------------
    row2 = await db.get_individual(g, 2, "Two")
    await db.log_tx(guild_id=g, kind="daily", account_type="individual", account_id=row2["id"], amount=2000)
    await db.delete_individual(g, 2)
    rows = await db.fetchall("SELECT * FROM transactions WHERE account_type='individual' AND account_id=?", (row2["id"],))
    check("delete removes ledger", rows == [], len(rows))

    # --- union funds + contract flow --------------------------------------
    await db.credit_union(g, 1_000_000)
    await db.add_balance("nations", nid, 3_000_000)
    cid = await db.create_contract(
        guild_id=g, case_id="UN-AURUM-001", nation_id=nid, nation_name="Aurum",
        nation_tag="AURUM", requester_id=111, cycle=1, budget=200_000, proposal="road",
    )
    won = await db.approve_contract(cid, upfront=50_000, escrow=150_000,
                                    approved_at=now_iso(), claim_account_type="nation",
                                    claim_account_id=nid)
    check("approve once", won)
    won2 = await db.approve_contract(cid, upfront=50_000, escrow=150_000,
                                     approved_at=now_iso(), claim_account_type="nation",
                                     claim_account_id=nid)
    check("approve twice refused", not won2)

    # upfront claim guard
    check("tranche claim once", await db.claim_contract_tranche(cid, "upfront"))
    check("tranche claim twice refused", not await db.claim_contract_tranche(cid, "upfront"))

    # --- short treasury rolls back the claim flag -------------------------
    await db.debit_union(g, await db.get_union_balance(g))  # drain
    cid2 = await db.create_contract(
        guild_id=g, case_id="UN-AURUM-002", nation_id=nid, nation_name="Aurum",
        nation_tag="AURUM", requester_id=111, cycle=1, budget=100_000, proposal="well",
    )
    await db.approve_contract(cid2, upfront=25_000, escrow=75_000, approved_at=now_iso(),
                              claim_account_type="nation", claim_account_id=nid)
    try:
        async with db.transaction():
            if await db.claim_contract_tranche(cid2, "upfront"):
                await db.debit_union(g, 25_000)
    except InsufficientFunds:
        pass
    c = await db.get_contract(cid2)
    check("short treasury keeps flag unset", c["upfront_claimed"] == 0, c["upfront_claimed"])

    # --- rename touches only open contracts -------------------------------
    await db.rename_account("nation", nid, "Aurum Prime")
    c = await db.get_contract(cid2)
    check("rename updates open contract snapshot", c["nation_name"] == "Aurum Prime", c["nation_name"])
    await db.set_contract_status(cid2, "completed")
    await db.rename_account("nation", nid, "Aurum II")
    c = await db.get_contract(cid2)
    check("rename leaves closed contract snapshot", c["nation_name"] == "Aurum Prime", c["nation_name"])

    # --- kit cooldown guard -----------------------------------------------
    check("kit cooldown is 7 days", config.KIT_COOLDOWN_DAYS == 7,
          config.KIT_COOLDOWN_DAYS)
    now = dt.datetime.now(dt.timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S")
    ready = (now - dt.timedelta(days=config.KIT_COOLDOWN_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    a = await db.claim_kit_cooldown(g, 9, ts, ready)
    b = await db.claim_kit_cooldown(g, 9, ts, ready)
    check("kit cooldown once", a and not b, (a, b))

    # Six days is still inside the window; the seventh is exactly eligible.
    six_days = now + dt.timedelta(days=6)
    six_stamp = six_days.strftime("%Y-%m-%dT%H:%M:%S")
    six_ready = (six_days - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    c6 = await db.claim_kit_cooldown(g, 9, six_stamp, six_ready)
    check("kit cooldown blocks day 6", not c6, c6)

    seven_days = now + dt.timedelta(days=7)
    seven_stamp = seven_days.strftime("%Y-%m-%dT%H:%M:%S")
    seven_ready = (seven_days - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    c7 = await db.claim_kit_cooldown(g, 9, seven_stamp, seven_ready)
    check("kit cooldown opens on day 7", c7, c7)

    # --- order redemption once --------------------------------------------
    oid = await db.create_order(guild_id=g, redeem_key="RC-AAA-BBB", buyer_id=1,
                                treasury_type="nation", treasury_id=nid, items_total=100,
                                shulker_fee=0, discount=0, final_total=100)
    check("redeem once", await db.redeem_order(oid, "uuid-1"))
    check("redeem twice refused", not await db.redeem_order(oid, "uuid-2"))

    # --- commit_balance_to_union outside tx must assert -------------------
    await db.add_union_member(g, "nation", nid)
    try:
        await db.commit_balance_to_union(g, "nation", nid); check("commit requires tx", False)
    except AssertionError:
        check("commit requires tx", True)

    # --- a plain write must not commit another task's transaction ----------
    before_k = int((await db.get_kingdom_by_id(kid))["balance"])
    in_tx = asyncio.Event()
    write_started = asyncio.Event()

    async def rollbacking_tx():
        try:
            async with db.transaction():
                await db.add_balance("kingdoms", kid, 7)
                in_tx.set()
                await write_started.wait()
                await asyncio.sleep(0.01)
                raise RuntimeError("rollback")
        except RuntimeError:
            pass

    async def independent_write():
        await in_tx.wait()
        write_started.set()
        await db.add_union_member(g, "kingdom", kid)  # plain execute(), no transaction

    await asyncio.wait_for(asyncio.gather(rollbacking_tx(), independent_write()), timeout=10)
    check("independent write survives another task's rollback",
          await db.is_union_member(g, "kingdom", kid))
    check("independent write does not commit the other task's rollback",
          int((await db.get_kingdom_by_id(kid))["balance"]) == before_k,
          (before_k, int((await db.get_kingdom_by_id(kid))["balance"])))

    # --- deleting an account burns and logs its remaining balance ---------
    burn_nation = await db.create_nation(g, "Burnable", 333)
    await db.add_balance("nations", burn_nation, 1_234)
    await db.log_tx(
        guild_id=g, kind="test_setup", account_type="nation",
        account_id=burn_nation, account_name="Burnable",
        amount=1_234, balance_after=1_234,
    )
    burned = await db.delete_account("nation", burn_nation, actor_id=999)
    check("deleted nation reports burned balance", burned == 1_234, burned)
    check("deleted nation row is gone", await db.get_nation_by_id(burn_nation) is None)
    del_rows = await db.fetchall(
        "SELECT * FROM transactions WHERE kind='nation_delete' AND account_id=?",
        (burn_nation,),
    )
    check("delete writes one nation ledger row", len(del_rows) == 1, len(del_rows))
    check("delete ledger row is the negative balance",
          len(del_rows) == 1
          and int(del_rows[0]["amount"]) == -1_234
          and int(del_rows[0]["balance_after"]) == 0,
          [(r["amount"], r["balance_after"]) for r in del_rows])
    total = await db.fetchone(
        """SELECT COALESCE(SUM(amount), 0) AS s FROM transactions
           WHERE account_type='nation' AND account_id=?""",
        (burn_nation,),
    )
    check("deleted nation ledger nets to zero", int(total["s"]) == 0, total["s"])

    burn_kingdom = await db.create_kingdom(g, "Burn Crown", 444)
    await db.add_balance("kingdoms", burn_kingdom, 500)
    await db.log_tx(
        guild_id=g, kind="test_setup", account_type="kingdom",
        account_id=burn_kingdom, account_name="Burn Crown",
        amount=500, balance_after=500,
    )
    burned = await db.delete_account("kingdom", burn_kingdom, actor_id=999)
    check("deleted kingdom reports burned balance", burned == 500, burned)
    check("deleted kingdom row is gone", await db.get_kingdom_by_id(burn_kingdom) is None)
    kd_rows = await db.fetchall(
        "SELECT * FROM transactions WHERE kind='kingdom_delete' AND account_id=?",
        (burn_kingdom,),
    )
    check("delete writes one kingdom ledger row", len(kd_rows) == 1, len(kd_rows))
    kd_total = await db.fetchone(
        """SELECT COALESCE(SUM(amount), 0) AS s FROM transactions
           WHERE account_type='kingdom' AND account_id=?""",
        (burn_kingdom,),
    )
    check("deleted kingdom ledger nets to zero", int(kd_total["s"]) == 0, kd_total["s"])

    other_guild_nation = await db.create_nation(2, "Other Guild", 555)
    await db.add_balance("nations", other_guild_nation, 777)
    check("cross-guild delete is refused",
          await db.delete_account(
              "nation", other_guild_nation, actor_id=999, guild_id=g
          ) == 0)
    check("cross-guild delete leaves the row",
          int((await db.get_nation_by_id(other_guild_nation))["balance"]) == 777)
    cross_rows = await db.fetchall(
        "SELECT * FROM transactions WHERE kind='nation_delete' AND account_id=?",
        (other_guild_nation,),
    )
    check("cross-guild delete writes no ledger row", cross_rows == [], len(cross_rows))

    empty_nation = await db.create_nation(g, "Empty", None)
    burned = await db.delete_account("nation", empty_nation, actor_id=999)
    empty_rows = await db.fetchall(
        "SELECT * FROM transactions WHERE kind='nation_delete' AND account_id=?",
        (empty_nation,),
    )
    check("zero-balance delete logs a zero row and burns nothing",
          burned == 0 and len(empty_rows) == 1 and int(empty_rows[0]["amount"]) == 0,
          (burned, len(empty_rows)))
    check("missing account delete is a no-op",
          await db.delete_account("nation", 999_999, actor_id=999) == 0)

    await db.close()
    print("\nFAILURES:", len(fails))
    for f in fails:
        print(" -", f)

try:
    asyncio.run(main())
except Exception as exc:
    print("ERROR:", type(exc).__name__, exc)
    for f in fails:
        print(" -", f)
    raise SystemExit(1)
raise SystemExit(1 if fails else 0)
