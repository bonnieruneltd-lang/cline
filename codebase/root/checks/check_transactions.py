"""Per-task transaction isolation checks.

Two independent tasks must each get their own transaction: one committing, one
rolling back, without either affecting the other.
"""
import asyncio, os, sys, tempfile
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from database import Database

fails = []
def check(label, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


async def independent_tasks_are_isolated():
    db = Database(os.path.join(tempfile.mkdtemp(), "a.db"))
    await db.connect()
    g = 1
    nid = await db.create_nation(g, "A", 1)
    kid = await db.create_kingdom(g, "K", 2)

    async def commits():
        async with db.transaction():
            await db.add_balance("kingdoms", kid, 500)

    async def rolls_back():
        try:
            async with db.transaction():
                await db.add_balance("nations", nid, 100)
                raise RuntimeError("rollback")
        except RuntimeError:
            pass

    await asyncio.gather(commits(), rolls_back())
    check("committed task survives other's rollback",
          (await db.get_kingdom_by_id(kid))["balance"] == 500,
          (await db.get_kingdom_by_id(kid))["balance"])
    check("rolled-back task leaves nothing",
          (await db.get_nation_by_id(nid))["balance"] == 0,
          (await db.get_nation_by_id(nid))["balance"])

    # A plain write from another task must wait for the open transaction; if it
    # commits the connection itself, the +7 survives the rollback.
    before_n = int((await db.get_nation_by_id(nid))["balance"])
    in_tx = asyncio.Event()
    write_started = asyncio.Event()

    async def plain_write():
        await in_tx.wait()
        write_started.set()
        await db.add_union_member(g, "kingdom", kid)

    async def slow_rollback():
        try:
            async with db.transaction():
                await db.add_balance("nations", nid, 7)
                in_tx.set()
                await write_started.wait()
                await asyncio.sleep(0.05)
                raise RuntimeError("boom")
        except RuntimeError:
            pass

    await asyncio.gather(plain_write(), slow_rollback())
    check("independent write survives another task's rollback",
          await db.is_union_member(g, "kingdom", kid))
    check("independent write does not commit another task's rollback",
          int((await db.get_nation_by_id(nid))["balance"]) == before_n,
          (before_n, int((await db.get_nation_by_id(nid))["balance"])))
    await db.close()


async def nested_joins_same_task():
    db = Database(os.path.join(tempfile.mkdtemp(), "b.db"))
    await db.connect()
    nid = await db.create_nation(1, "B", 1)

    async with db.transaction():
        await db.add_balance("nations", nid, 50)
        async with db.transaction():
            await db.add_balance("nations", nid, 50)

    check("nested joins outer", (await db.get_nation_by_id(nid))["balance"] == 100,
          (await db.get_nation_by_id(nid))["balance"])

    try:
        async with db.transaction():
            await db.add_balance("nations", nid, 10)
            async with db.transaction():
                raise RuntimeError("inner")
    except RuntimeError:
        pass
    check("inner failure rolls back outer", (await db.get_nation_by_id(nid))["balance"] == 100,
          (await db.get_nation_by_id(nid))["balance"])
    await db.close()


async def main():
    await independent_tasks_are_isolated()
    await nested_joins_same_task()
    print("\nFAILURES:", len(fails))

asyncio.run(main())
raise SystemExit(1 if fails else 0)
