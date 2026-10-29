"""Daily-claim concurrency plus import/cog-load smoke test (expects 112 commands)."""
import asyncio, os, sys, tempfile, datetime as dt
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from database import Database
import daily, config


async def daily_concurrency():
    db = Database(os.path.join(tempfile.mkdtemp(), "d.db"))
    await db.connect()
    from cogs.individual import Individuals

    class FakeBot:
        def __init__(self, db): self.db = db

    cog = Individuals(FakeBot(db))
    now = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)
    results = await asyncio.gather(
        cog.claim_daily(1, 42, "Tester", now),
        cog.claim_daily(1, 42, "Tester", now),
        cog.claim_daily(1, 42, "Tester", now),
    )
    paid = [r for r in results if r[1]]
    row = await db.find_individual(1, 42)
    tx = await db.fetchall("SELECT * FROM transactions WHERE kind='daily'")
    print("claims paying:", len(paid), "balance:", row["balance"], "ledger rows:", len(tx))
    assert len(paid) == 1, paid
    assert row["balance"] == daily.payout_for(config.DAILY_BP_START), row["balance"]
    assert len(tx) == 1

    # second claim after cooldown adds +10 points and pays at the new rate
    later = now + dt.timedelta(hours=24)
    outcome, claimed, added, after = await cog.claim_daily(1, 42, "Tester", later)
    print("second claim:", outcome.multiplier_bp, claimed, added, after)
    assert claimed and outcome.multiplier_bp == 11000 and added == 2200

    # wallet at cap: claim is consumed, nothing credited, balance stays at cap
    await db.get_individual(1, 99, "Rich")
    await db.credit_individual(1, 99, config.INDIVIDUAL_MAX_CREDITS)
    o, c, a, b = await cog.claim_daily(1, 99, "Rich", now)
    row99 = await db.find_individual(1, 99)
    print("cap claim:", c, a, b, row99["last_daily_at"])
    assert c and a == 0 and b == config.INDIVIDUAL_MAX_CREDITS and row99["last_daily_at"]
    print("daily concurrency ok")
    await db.close()


async def cog_smoke():
    from bot import RoyalCourt, COGS
    bot = RoyalCourt()
    # Never create the working copy's royal_bank.db: this is a throwaway smoke
    # test, like every other check.
    bot.db.path = os.path.join(tempfile.mkdtemp(), "cog-smoke.db")
    await bot.db.connect()
    try:
        from shop.seed import seed_shop
        await seed_shop(bot.db)
        for ext in COGS:
            await bot.load_extension(ext)
        n = len(list(bot.tree.walk_commands()))
        print("loaded cogs:", len(COGS), "commands:", n)
        # Pinned on purpose: a silently dropped command is invisible until a user
        # reports it missing. Update this line deliberately when the surface
        # changes. 111 = 113 - the audience and two specialized contract
        # subcommands + the single /faction contract request subcommand. Each
        # action exposes only its own options; /contract remains the lifecycle
        # group. 112 adds the top-level /give treasury-to-member grant.
        assert n == 112, f"expected 112 commands, found {n}"
        assert len(COGS) == 16, f"expected 16 cogs, found {len(COGS)}"
        names = {command.qualified_name for command in bot.tree.walk_commands()}
        for required in (
            "payout cash", "payout default", "payout trigger", "transfer", "give",
            "faction join", "faction contract", "faction commitment",
            "faction uncommit", "faction status", "faction fund",
            "faction withdraw", "faction setchannel",
        ):
            assert required in names, f"missing command {required}"
        # /give is the treasury-to-member shortcut: a nation/kingdom source, a
        # member and an amount. It must never expose a raw individual source.
        give_cmd = next(
            command for command in bot.tree.walk_commands()
            if command.qualified_name == "give"
        )
        give_params = {param.name for param in give_cmd.parameters}
        assert give_params == {"source", "member", "amount"}, give_params
        # The contract request is deliberately minimal: faction, account,
        # proposal and budget. Item/quantity belonged to the retired Crown
        # supplier request and must not come back.
        contract_cmd = next(
            command for command in bot.tree.walk_commands()
            if command.qualified_name == "faction contract"
        )
        contract_params = {param.name for param in contract_cmd.parameters}
        assert contract_params == {"faction", "account", "proposal", "budget"}, contract_params
        for old in (
            "join", "commit", "donate", "audience", "crown", "union",
            "crown status", "crown contracts", "crown cancel", "crown fund",
            "crown withdraw", "union commit", "union uncommit", "contract request",
            "faction audience", "faction union-contract", "faction crown-contract",
        ):
            assert old not in names, f"old command {old} must be gone"
    finally:
        # In a finally block because a failed assertion above used to leave the
        # aiosqlite worker thread and the payout cycle task running, so the
        # interpreter hung at shutdown instead of reporting the failure.
        payout = bot.get_cog("Payout")
        if payout and payout.cycle_task.is_running():
            payout.cycle_task.cancel()
        await bot.db.close()
        try:
            await bot.close()
        except Exception:
            pass


async def main():
    await daily_concurrency()
    await cog_smoke()

asyncio.run(main())
print("ALL OK")
