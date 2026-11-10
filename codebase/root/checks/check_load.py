"""Concurrency and load checks: many users at once on one database.

    python3 checks/check_load.py

Builds a throwaway SQLite file and proves the properties the bot relies on when
a whole server is typing at the same time:

* a read on the pool is served from a WAL snapshot **without waiting** for an
  open money transaction (the old single-connection design queued it behind);
* concurrent writes stay exact - no lost updates, no overdraw under a burst;
* a mixed read/write burst of hundreds of operations raises nothing (no
  ``database is locked``);
* readers really are ``query_only`` and the pool size is clamped;
* the autocomplete snapshot cache serves repeats and expires;
* the shared AI client is reused and its concurrency/retry knobs parse.
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database
from database import Database, InsufficientFunds
import utils
from gilbert import ai as ai_module

fails = []


def check(label, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + label + (("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(label)


async def main():
    tmp = tempfile.mkdtemp()
    db = Database(os.path.join(tmp, "load.db"))
    await db.connect()
    g = 1

    # ------------------------------------------------------------------ pool
    check("read pool is at least one connection", db.read_pool_size >= 1, db.read_pool_size)
    check("read pool matches the queue size",
          db._read_pool is not None and db._read_pool.maxsize == db.read_pool_size,
          db._read_pool and db._read_pool.maxsize)
    check("one writer + a reader pool",
          len(db._read_conns) == db.read_pool_size and db.conn is not None,
          len(db._read_conns))

    pragmas = []
    for reader in db._read_conns:
        cur = await reader.execute("PRAGMA query_only")
        pragmas.append((await cur.fetchone())[0])
        await cur.close()
    check("every reader is query-only", all(p == 1 for p in pragmas), pragmas)

    # ------------------------------------------ a read never waits for a write
    nid = await db.create_nation(g, "Aurum", 111)
    await db.add_balance("nations", nid, 1000)

    holding = asyncio.Event()
    release = asyncio.Event()

    async def long_transaction():
        async with db.transaction():
            await db.add_balance("nations", nid, 500)
            holding.set()
            await release.wait()

    async def read_during_transaction():
        await holding.wait()
        started = time.perf_counter()
        row = await db.get_nation_by_id(nid)
        elapsed = time.perf_counter() - started
        return elapsed, int(row["balance"])

    tx_task = asyncio.create_task(long_transaction())
    wait_task = asyncio.create_task(read_during_transaction())
    elapsed, seen = await asyncio.wait_for(wait_task, timeout=5)
    still_open = not tx_task.done()
    release.set()
    await asyncio.wait_for(tx_task, timeout=5)
    check("read returns while a money transaction is still open", still_open, still_open)
    check("read does not queue behind the writer (under 0.25s)", elapsed < 0.25, f"{elapsed:.3f}s")
    check("open transaction's uncommitted row is not visible to readers", seen == 1000, seen)

    # ------------------------------------------------ concurrent writes are exact
    check("transaction committed its own write",
          int((await db.get_nation_by_id(nid))["balance"]) == 1500,
          int((await db.get_nation_by_id(nid))["balance"]))
    gid = await db.create_kingdom(g, "Crown", 222)
    await asyncio.gather(*(db.add_balance("kingdoms", gid, 1) for _ in range(120)))
    check("120 concurrent credits: no lost updates",
          int((await db.get_kingdom_by_id(gid))["balance"]) == 120,
          int((await db.get_kingdom_by_id(gid))["balance"]))

    src = await db.create_nation(g, "Source", 1)
    dst = await db.create_nation(g, "Dest", 2)
    await db.add_balance("nations", src, 200)

    async def move():
        try:
            await db.transfer_balance("nations", src, "nations", dst, 5)
            return True
        except InsufficientFunds:
            return False

    results = await asyncio.gather(*(move() for _ in range(80)))
    src_bal = int((await db.get_nation_by_id(src))["balance"])
    dst_bal = int((await db.get_nation_by_id(dst))["balance"])
    check("concurrent transfers never overdraw", src_bal >= 0, src_bal)
    check("every successful transfer moved exactly its amount",
          src_bal == 200 - 5 * sum(results) and dst_bal == 5 * sum(results),
          (src_bal, dst_bal, sum(results)))

    # ------------------------------------- a mixed burst of readers + writers
    errors = []

    async def reader_task(i):
        try:
            await db.get_nation_by_id(nid)
            await db.list_nations(g)
            await db.list_individuals(g)
            await db.list_kingdoms(g)
        except Exception as exc:  # noqa: BLE001 - the assertion is "nothing raised"
            errors.append(f"read {i}: {type(exc).__name__}: {exc}")

    async def writer_task(i):
        try:
            async with db.transaction():
                await db.add_balance("kingdoms", gid, 1)
                await db.log_tx(guild_id=g, kind="load", account_type="kingdom",
                                account_id=gid, amount=1, balance_after=0)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"write {i}: {type(exc).__name__}: {exc}")

    started = time.perf_counter()
    await asyncio.wait_for(
        asyncio.gather(
            *(reader_task(i) for i in range(240)),
            *(writer_task(i) for i in range(40)),
        ),
        timeout=60,
    )
    burst = time.perf_counter() - started
    check("mixed burst of 280 operations raised nothing", not errors, errors[:3])
    check("mixed burst finished promptly", burst < 10.0, f"{burst:.2f}s")
    check("burst writes all landed",
          int((await db.get_kingdom_by_id(gid))["balance"]) == 120 + 40,
          int((await db.get_kingdom_by_id(gid))["balance"]))

    # ------------------------------------- reads inside a transaction see it
    async with db.transaction():
        await db.add_balance("kingdoms", gid, 7)
        inside = int((await db.fetchone(
            "SELECT balance FROM kingdoms WHERE id = ?", (gid,)
        ))["balance"])
    check("reads inside a transaction use the writer and see uncommitted rows",
          inside == 120 + 40 + 7, inside)

    # ------------------------------- the kingdom member cap holds under a burst
    cap_kingdom = await db.create_kingdom(g, "Six", 333)
    for i in range(5):
        nid_i = await db.create_nation(g, f"Member{i}", 400 + i)
        await db.assign_nation_to_kingdom(g, nid_i, cap_kingdom, max_members=6)
    candidates = [await db.create_nation(g, f"Late{i}", 500 + i) for i in range(6)]
    audit_before = len(await db.fetchall(
        "SELECT id FROM transactions WHERE kind='kingdom_addnation' AND account_id = ?",
        (cap_kingdom,),
    ))

    async def join(nation_id):
        status, *_ = await db.assign_nation_to_kingdom(
            g, nation_id, cap_kingdom, max_members=6, actor_id=999
        )
        return status

    outcomes = await asyncio.gather(*(join(c) for c in candidates))
    members = await db.nations_in_kingdom(cap_kingdom)
    audit = await db.fetchall(
        "SELECT id FROM transactions WHERE kind='kingdom_addnation' AND account_id = ?",
        (cap_kingdom,),
    )
    check("six concurrent joins for one free slot: exactly one wins",
          outcomes.count("ok") == 1, outcomes)
    check("the other joins report the kingdom is full",
          outcomes.count("full") == 5, outcomes)
    check("the kingdom never exceeds its cap", len(members) == 6, len(members))
    check("only the winning join writes a ledger row",
          len(audit) - audit_before == 1, len(audit) - audit_before)

    # ------------------------------ the Union member listing is one joined read
    await db.add_union_member(g, "nation", nid)
    await db.add_union_member(g, "kingdom", gid)
    union_rows = await db.list_union_member_accounts(g)
    union_names = {r["name"] for r in union_rows}
    check("Union listing resolves every member name in one query",
          union_names == {"Aurum", "Crown"}, union_names)

    # ------------------------------------------------------ autocomplete cache
    utils.clear_autocomplete_cache()
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return calls["n"]

    first = await utils.autocomplete_snapshot(("t", 1), fetch)
    second = await utils.autocomplete_snapshot(("t", 1), fetch)
    check("autocomplete snapshot is cached on a repeat",
          first == 1 and second == 1 and calls["n"] == 1, (first, second, calls["n"]))
    old_ttl = utils.AUTOCOMPLETE_CACHE_SECONDS
    utils.AUTOCOMPLETE_CACHE_SECONDS = 0.0
    try:
        third = await utils.autocomplete_snapshot(("t", 1), fetch)
    finally:
        utils.AUTOCOMPLETE_CACHE_SECONDS = old_ttl
    check("autocomplete snapshot refreshes after the TTL", third == 2, third)

    # --------------------------------------------------------- AI client knobs
    with_test_env = {
        "CHATBOT_MAX_CONCURRENCY": "3",
        "CHATBOT_RETRIES": "0",
    }
    backup = {k: os.environ.get(k) for k in with_test_env}
    os.environ.update(with_test_env)
    try:
        check("AI concurrency cap reads the env", ai_module._max_concurrency() == 3,
              ai_module._max_concurrency())
        check("AI retries can be switched off", ai_module._max_attempts() == 1,
              ai_module._max_attempts())
    finally:
        for key, value in backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    check("AI concurrency cap is clamped",
          ai_module._env_int("CHATBOT_MAX_CONCURRENCY", 8, 1, 64) >= 1)
    # Backoff is jittered (a burst of negotiations must not retry in lockstep),
    # so the exact value is not fixed - the band and the cap are.
    check("retry backoff is jittered around the base",
          all(ai_module.RETRY_BASE_SECONDS * 0.75
              <= ai_module._retry_delay(None, 0)
              <= ai_module.RETRY_BASE_SECONDS * 1.25 for _ in range(20)),
          ai_module._retry_delay(None, 0))
    check("retry backoff is capped",
          all(ai_module._retry_delay(None, 5) <= ai_module.RETRY_MAX_SECONDS
              for _ in range(20)))
    check("retry backoff still grows",
          ai_module._retry_delay(None, 3) > ai_module._retry_delay(None, 0))
    first_session = ai_module.shared_session()
    second_session = ai_module.shared_session()
    check("one shared AI session per loop", first_session is second_session)
    await ai_module.close_shared_sessions()
    check("shared AI session is closed on shutdown", first_session.closed)

    # ------------------------------- the AI client against a loopback server
    # Proves the retry/cap behaviour end to end without leaving the machine.
    from aiohttp import web

    state = {"mode": "ok", "hits": 0, "inflight": 0, "peak": 0}

    async def handler(request):
        state["hits"] += 1
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])
        try:
            if state["mode"] == "ratelimit" and state["hits"] <= 2:
                return web.json_response(
                    {"error": "slow down"}, status=429, headers={"Retry-After": "0"}
                )
            if state["mode"] == "broken":
                return web.json_response({"error": "boom"}, status=500)
            return web.json_response({"choices": [{"message": {"content": "hello"}}]})
        finally:
            state["inflight"] -= 1

    runner = None
    live_env = {
        "CHATBOT_API_KEY": "test",
        "CHATBOT_MODEL": "m",
        "CHATBOT_TIMEOUT": "5",
        "CHATBOT_RETRIES": "2",
        "CHATBOT_MAX_CONCURRENCY": "3",
    }
    live_backup = {k: os.environ.get(k) for k in live_env}
    live_backup["CHATBOT_API_URL"] = os.environ.get("CHATBOT_API_URL")
    try:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        os.environ.update(live_env)
        os.environ["CHATBOT_API_URL"] = f"http://127.0.0.1:{port}/v1"

        client = ai_module.GilbertAI()
        state.update(mode="ok", hits=0)
        reply = await client.complete([{"role": "user", "content": "x"}])
        check("AI client returns a normal reply", reply == "hello", reply)

        state.update(mode="ratelimit", hits=0)
        reply = await client.complete([{"role": "user", "content": "x"}])
        check("AI client retries a 429 and recovers",
              reply == "hello" and state["hits"] == 3, (reply, state["hits"]))

        state.update(mode="broken", hits=0)
        reply = await client.complete([{"role": "user", "content": "x"}])
        check("AI client gives up after its retries",
              reply is None and state["hits"] == 3, (reply, state["hits"]))

        state.update(mode="ok", hits=0, peak=0)
        replies = await asyncio.gather(
            *(client.complete([{"role": "user", "content": "x"}]) for _ in range(9))
        )
        check("AI client answers every queued call", replies.count("hello") == 9,
              replies.count("hello"))
        check("AI client caps simultaneous provider calls",
              state["peak"] <= 3, state["peak"])
    except OSError as exc:  # a host that forbids loopback listeners
        print(f"skip  AI loopback round-trip ({exc})")
    finally:
        await ai_module.close_shared_sessions()
        if runner is not None:
            await runner.cleanup()
        for key, value in live_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ------------------------------------------------- pool size env is clamped
    os.environ["DB_READ_POOL_SIZE"] = "999"
    try:
        check("read pool size is clamped to the maximum",
              database._read_pool_size() == database.MAX_READ_POOL_SIZE,
              database._read_pool_size())
    finally:
        os.environ.pop("DB_READ_POOL_SIZE", None)

    await db.close()
    check("close closes the writer and every reader",
          db.conn is None and db._read_conns == [])

    print("\nFAILURES:", len(fails))
    for failure in fails:
        print(" -", failure)


try:
    asyncio.run(main())
except Exception as exc:  # noqa: BLE001 - an uncaught error is a failure, not a pass
    print("ERROR:", type(exc).__name__, exc)
    for failure in fails:
        print(" -", failure)
    raise SystemExit(1)
raise SystemExit(1 if fails else 0)
