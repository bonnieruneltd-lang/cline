"""Every transfer direction, permission, cap, cross-guild and Union hook.

Uses the real ``account_transfers.transfer_funds`` core against a throwaway
database. It does not need Discord.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import account_transfers as transfers
from database import CapExceeded, Database, InsufficientFunds

fails = []


def check(label, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + label + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


async def total_assets(db):
    total = 0
    for table in (
        "nations",
        "kingdoms",
        "individuals",
        "union_account",
        "casino_account",
        "fishing_guild_account",
    ):
        row = await db.fetchone(f"SELECT COALESCE(SUM(balance), 0) AS s FROM {table}")
        total += int(row["s"])
    return total


async def main():
    db = Database(os.path.join(tempfile.mkdtemp(), "full_transfers.db"))
    await db.connect()
    g = 1

    n_aurum = await db.create_nation(g, "Aurum", 111)
    n_borealis = await db.create_nation(g, "Borealis", 112)
    k_crown = await db.create_kingdom(g, "Crown", 222)
    k_realm = await db.create_kingdom(g, "Realm", 223)
    for table, account_id in (
        ("nations", n_aurum),
        ("nations", n_borealis),
        ("kingdoms", k_crown),
        ("kingdoms", k_realm),
    ):
        await db.add_balance(table, account_id, 50_000)
    for uid, name in ((333, "Alice"), (444, "Bob")):
        await db.get_individual(g, uid, name)
        await db.credit_individual(g, uid, 50_000)

    async def ref(kind, key):
        return await transfers.resolve_ref(db, g, f"{kind}:{key}")

    async def fresh_balance(account):
        if account.kind == transfers.INDIVIDUAL:
            row = await db.find_individual(g, int(account.discord_id))
        elif account.kind == transfers.KINGDOM:
            row = await db.get_kingdom_by_id(account.id)
        else:
            row = await db.get_nation_by_id(account.id)
        return int(row["balance"]) if row else 0

    before_total = await total_assets(db)

    # --- all six account pair shapes, both directions where distinct ------
    directions = [
        ("nation:Aurum", "kingdom:Crown"),
        ("nation:Aurum", "individual:333"),
        ("kingdom:Crown", "nation:Aurum"),
        ("kingdom:Crown", "individual:333"),
        ("individual:333", "nation:Aurum"),
        ("individual:333", "kingdom:Crown"),
        ("individual:333", "individual:444"),
        ("nation:Aurum", "nation:Borealis"),
        ("kingdom:Crown", "kingdom:Realm"),
    ]
    for index, (source_value, destination_value) in enumerate(directions):
        source = await ref(*source_value.split(":", 1))
        destination = await ref(*destination_value.split(":", 1))
        check(f"direction {index} resolves", source is not None and destination is not None)
        if source is None or destination is None:
            continue
        actor = source.discord_id if source.kind == transfers.INDIVIDUAL else source.leader_id
        source_before = await fresh_balance(source)
        destination_before = await fresh_balance(destination)
        result = await transfers.transfer_funds(
            db,
            guild_id=g,
            actor_id=int(actor),
            is_admin=False,
            source=source,
            destination=destination,
            amount=101,
            kind=f"test_dir_{index}",
        )
        check(
            f"{source.kind}->{destination.kind} source debited",
            result.source_balance == source_before - 101,
            (result.source_balance, source_before),
        )
        check(
            f"{source.kind}->{destination.kind} destination credited",
            result.destination_balance == destination_before + 101,
            (result.destination_balance, destination_before),
        )

    check("transfers conserve total assets", await total_assets(db) == before_total)

    # --- concurrent debits from one wallet -------------------------------
    # The guarded UPDATE and BEGIN IMMEDIATE must let exactly ten 100-credit
    # transfers through a 1,000-credit wallet; the other ten must not overdraw.
    await db.get_individual(g, 777, "Concurrent A")
    await db.credit_individual(g, 777, 1_000)
    await db.get_individual(g, 778, "Concurrent B")
    concurrent_source = await ref("individual", "777")
    concurrent_destination = await ref("individual", "778")

    async def attempt():
        try:
            await transfers.transfer_funds(
                db,
                guild_id=g,
                actor_id=777,
                is_admin=False,
                source=concurrent_source,
                destination=concurrent_destination,
                amount=100,
                kind="test_concurrent",
            )
            return "ok"
        except InsufficientFunds:
            return "refused"

    results = await asyncio.gather(*(attempt() for _ in range(20)))
    check("concurrent debits pay exactly ten", results.count("ok") == 10, results.count("ok"))
    check("concurrent debits refuse the rest", results.count("refused") == 10,
          results.count("refused"))
    check("concurrent source ends at zero",
          await fresh_balance(concurrent_source) == 0,
          await fresh_balance(concurrent_source))
    check("concurrent destination receives 1,000",
          await fresh_balance(concurrent_destination) == 1_000,
          await fresh_balance(concurrent_destination))
    concurrent_rows = await db.fetchall(
        "SELECT amount FROM transactions WHERE kind='test_concurrent'"
    )
    check("concurrent ledger has 20 rows and nets to zero",
          len(concurrent_rows) == 20
          and sum(int(row["amount"]) for row in concurrent_rows) == 0,
          (len(concurrent_rows), sum(int(row["amount"]) for row in concurrent_rows)))

    # --- permission: non-leader, admin override, wallet ownership ---------
    nation = await ref("nation", "Aurum")
    wallet = await ref("individual", "333")
    nation_before = await fresh_balance(nation)
    try:
        await transfers.transfer_funds(
            db,
            guild_id=g,
            actor_id=9999,
            is_admin=False,
            source=nation,
            destination=wallet,
            amount=1,
        )
        check("non-leader refused", False)
    except transfers.TransferError:
        check("non-leader refused", True)
    check("non-leader moved nothing", await fresh_balance(nation) == nation_before)

    result = await transfers.transfer_funds(
        db,
        guild_id=g,
        actor_id=9999,
        is_admin=True,
        source=nation,
        destination=wallet,
        amount=1,
    )
    check(
        "administrator override works",
        result.source_balance == nation_before - 1,
        result.source_balance,
    )

    wallet = await ref("individual", "333")
    wallet_before = await fresh_balance(wallet)
    try:
        await transfers.transfer_funds(
            db,
            guild_id=g,
            actor_id=444,
            is_admin=False,
            source=wallet,
            destination=nation,
            amount=1,
        )
        check("other member cannot spend a wallet", False)
    except transfers.TransferError:
        check("other member cannot spend a wallet", True)
    check("wallet unchanged by foreign attempt", await fresh_balance(wallet) == wallet_before)

    # --- individual cap: strict credit refuses, source rolled back --------
    await db.get_individual(g, 555, "Capped")
    await db.credit_individual(g, 555, config.INDIVIDUAL_MAX_CREDITS - 50)
    capped = await ref("individual", "555")
    nation = await ref("nation", "Aurum")
    nation_before = await fresh_balance(nation)
    try:
        await transfers.transfer_funds(
            db,
            guild_id=g,
            actor_id=111,
            is_admin=False,
            source=nation,
            destination=capped,
            amount=100,
        )
        check("cap refusal", False)
    except CapExceeded as exc:
        check("cap refusal reports room", exc.room == 50, exc.room)
    check("cap refusal rolled the source back", await fresh_balance(nation) == nation_before)

    # --- source overdraw -------------------------------------------------
    wallet = await ref("individual", "333")
    wallet_balance = await fresh_balance(wallet)
    destination = await ref("nation", "Aurum")
    destination_before = await fresh_balance(destination)
    try:
        await transfers.transfer_funds(
            db,
            guild_id=g,
            actor_id=333,
            is_admin=False,
            source=wallet,
            destination=destination,
            amount=wallet_balance + 1,
        )
        check("overdraw refused", False)
    except InsufficientFunds:
        check("overdraw refused", True)
    check("overdraw left the destination alone", await fresh_balance(destination) == destination_before)

    # --- same account and cross-guild ------------------------------------
    try:
        await transfers.transfer_funds(
            db,
            guild_id=g,
            actor_id=111,
            is_admin=True,
            source=nation,
            destination=nation,
            amount=1,
        )
        check("same account refused", False)
    except transfers.TransferError:
        check("same account refused", True)

    other_guild_nation = await db.create_nation(2, "Other Realm", 777)
    await db.add_balance("nations", other_guild_nation, 1_000)
    check("other guild does not resolve", await ref("nation", "Other Realm") is None)
    foreign = transfers.AccountRef(
        kind=transfers.NATION,
        id=other_guild_nation,
        name="Other Realm",
        balance=1_000,
        leader_id=777,
    )
    try:
        await transfers.transfer_funds(
            db,
            guild_id=g,
            actor_id=777,
            is_admin=False,
            source=foreign,
            destination=nation,
            amount=1,
        )
        check("cross-guild source refused", False)
    except transfers.TransferError:
        check("cross-guild source refused", True)


    # --- Union commitment hook on a treasury destination ------------------
    committed_nation = await db.create_nation(g, "Committed", 888)
    commit_id = await db.create_union_commit_request(g, "nation", committed_nation, 888)
    activated = await db.decide_union_commit(commit_id, "active", 999)
    check("commitment activated", activated)
    commit_baseline = await total_assets(db)
    union_before = await db.get_union_balance(g)
    destination = await ref("nation", "Committed")
    source = await ref("individual", "333")
    source_before = await fresh_balance(source)
    result = await transfers.transfer_funds(
        db,
        guild_id=g,
        actor_id=333,
        is_admin=False,
        source=source,
        destination=destination,
        amount=500,
        kind="test_commit",
    )
    check("commitment donation reported", result.committed == 500, result.committed)
    check("committed destination ends at zero", result.destination_balance == 0,
          result.destination_balance)
    check("Union received the transfer", await db.get_union_balance(g) == union_before + 500,
          await db.get_union_balance(g))
    check("committed source debited once", await fresh_balance(source) == source_before - 500,
          await fresh_balance(source))

    # --- ledger integrity -------------------------------------------------
    rows = await db.fetchall(
        "SELECT kind, amount FROM transactions WHERE kind LIKE 'test_dir_%' OR kind='test_commit'"
    )
    by_kind = {}
    for row in rows:
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + int(row["amount"])
    check("every transfer kind nets to zero", all(total == 0 for total in by_kind.values()),
          by_kind)
    check("all nine directions wrote ledger rows",
          sum(1 for kind in by_kind if kind.startswith("test_dir_")) == 9, by_kind.keys())

    commit_rows = await db.fetchall(
        "SELECT amount FROM transactions WHERE kind='union_commit'"
    )
    check("commitment ledger nets to zero",
          sum(int(row["amount"]) for row in commit_rows) == 0, len(commit_rows))
    check("assets conserved end to end", await total_assets(db) == commit_baseline)

    await db.close()
    print("\nFAILURES:", len(fails))


asyncio.run(main())
raise SystemExit(1 if fails else 0)

