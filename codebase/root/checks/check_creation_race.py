"""Concurrent /nation request must create exactly one account and one bonus row.

Also covers the approval path when a direct /nation add wins the name race:
the IntegrityError must close the petition instead of leaving it pending, and
the case-insensitive name rules (the schema's UNIQUE is case-sensitive while
every lookup is COLLATE NOCASE) for create, rename and the commands.
"""
import asyncio, os, sys, tempfile
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import sqlite3
from database import Database, NameTaken
from creation import CreationApprovalView, found_account
from cogs.kingdom import Kingdom
from cogs.nation import Nation


class FakeResponse:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, embed=None, **kwargs):
        self.sent.append(embed)

    async def edit_message(self, embed=None, view=None, **kwargs):
        self.edited.append(embed)


class FakeFollowup:
    async def send(self, embed=None, **kwargs):
        pass


class FakeUser:
    id = 999
    guild_permissions = type("Perms", (), {"administrator": True})()


class FakeInteraction:
    guild_id = 1

    def __init__(self):
        self.user = FakeUser()
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class FakeBot:
    def __init__(self, db):
        self.db = db


async def main():
    db = Database(os.path.join(tempfile.mkdtemp(), "c.db"))
    await db.connect()
    g = 1

    # two concurrent approvals founding the same name
    async def found():
        try:
            await found_account(db, g, "nation", "Same", 111, 999, note="t")
            return "created"
        except (sqlite3.IntegrityError, NameTaken):
            return "refused"

    res = await asyncio.gather(found(), found(), found())
    rows = await db.list_nations(g)
    txs = await db.fetchall("SELECT * FROM transactions WHERE kind='nation_create'")
    print("results:", res)
    print("nations:", len(rows), "bonus ledger rows:", len(txs),
          "balance:", rows[0]["balance"] if rows else None)
    ok = (res.count("created") == 1 and len(rows) == 1 and len(txs) == 1)

    # An IntegrityError during approval (a direct add won the name race) must
    # mark the petition failed in a new transaction; otherwise the card says
    # "Name Taken" while the row stays pending forever.
    creation_id = await db.create_pending_creation(g, "nation", "Race", 111, 999)
    real_create = db.create_nation

    async def boom(*args, **kwargs):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: nations.name")

    db.create_nation = boom
    interaction = FakeInteraction()
    try:
        await CreationApprovalView(FakeBot(db), creation_id)._approve(interaction)
    finally:
        db.create_nation = real_create

    petition = await db.get_pending_creation(creation_id)
    race_rows = [n for n in await db.list_nations(g) if n["name"] == "Race"]
    race_tx = await db.fetchall(
        "SELECT * FROM transactions WHERE kind='nation_create' AND account_name='Race'"
    )
    print("petition after name race:", petition["status"])
    print("race account rows:", len(race_rows), "race bonus rows:", len(race_tx))
    print("reply:", interaction.response.edited[-1].title if interaction.response.edited else None)
    ok = ok and petition["status"] == "failed" and not race_rows and not race_tx

    # The two direct admin add paths must catch the same race instead of
    # surfacing an unhandled IntegrityError. Each command gets its own patched
    # create_* and its own name, so neither leaves a row behind.
    async def direct_add(command, cog, attr, name, label):
        real = getattr(db, attr)

        async def boom2(*args, **kwargs):
            raise sqlite3.IntegrityError("UNIQUE constraint failed: name")

        setattr(db, attr, boom2)
        race_interaction = FakeInteraction()
        try:
            await command.callback(cog, race_interaction, name, None)
        finally:
            setattr(db, attr, real)
        made = await db.fetchall(
            f"SELECT * FROM {'nations' if attr == 'create_nation' else 'kingdoms'} WHERE name = ?",
            (name,),
        )
        reply = race_interaction.response.sent[-1].title if race_interaction.response.sent else None
        print(f"{label} race reply:", reply, "rows:", len(made))
        return "Name Taken" in (reply or "") and not made

    direct_nation = await direct_add(
        Nation.add, Nation(FakeBot(db)), "create_nation", "DirectNation", "nation"
    )
    direct_kingdom = await direct_add(
        Kingdom.add, Kingdom(FakeBot(db)), "create_kingdom", "DirectKingdom", "kingdom"
    )
    ok = ok and direct_nation and direct_kingdom

    # --- a name is unique ignoring case -----------------------------------
    # UNIQUE (guild_id, name) is case-sensitive but every lookup is
    # COLLATE NOCASE, so "Alpha" and "ALPHA" used to be able to coexist and one
    # of them was unreachable. The insert now re-checks the name itself.
    async def found_named(name):
        try:
            await found_account(db, g, "nation", name, 111, 999, note="case")
            return "created"
        except NameTaken:
            return "refused"

    first = await found_named("Alpha")
    second = await found_named("ALPHA")
    alpha_rows = await db.fetchall(
        "SELECT * FROM nations WHERE guild_id = ? AND name = ? COLLATE NOCASE", (g, "alpha")
    )
    print("case variant:", first, second, "rows:", len(alpha_rows))
    ok = ok and (
        first == "created" and second == "refused" and len(alpha_rows) == 1
        and (await db.get_nation(g, "aLpHa"))["id"] == alpha_rows[0]["id"]
    )

    # Three founders, three casings, one winner.
    race = await asyncio.gather(found_named("Beta"), found_named("beta"), found_named("BETA"))
    beta_rows = await db.fetchall(
        "SELECT * FROM nations WHERE guild_id = ? AND name = ? COLLATE NOCASE", (g, "beta")
    )
    beta_tx = await db.fetchall(
        "SELECT * FROM transactions WHERE kind='nation_create' AND account_name = ? COLLATE NOCASE",
        ("beta",),
    )
    print("case race:", race, "rows:", len(beta_rows), "bonus rows:", len(beta_tx))
    ok = ok and race.count("created") == 1 and len(beta_rows) == 1 and len(beta_tx) == 1

    # The same rule for kingdoms.
    await db.create_kingdom(g, "Kalmar", 222)
    try:
        await db.create_kingdom(g, "kalmar", 222)
        kingdom_dup = False
    except NameTaken:
        kingdom_dup = True
    kalmar = await db.fetchall(
        "SELECT * FROM kingdoms WHERE guild_id = ? AND name = ? COLLATE NOCASE", (g, "kalmar")
    )
    print("kingdom case variant refused:", kingdom_dup, "rows:", len(kalmar))
    ok = ok and kingdom_dup and len(kalmar) == 1

    # A rename cannot take another account's name, whatever the casing, and a
    # pure case change of its own name is still allowed.
    gamma = await db.create_nation(g, "Gamma", 111)
    try:
        await db.rename_account("nation", gamma, "alpha")
        rename_clash = False
    except NameTaken:
        rename_clash = True
    unchanged = (await db.get_nation_by_id(gamma))["name"] == "Gamma"
    await db.rename_account("nation", gamma, "GAMMA")
    recased = (await db.get_nation_by_id(gamma))["name"] == "GAMMA"
    print("rename clash refused:", rename_clash, "unchanged:", unchanged, "recased:", recased)
    ok = ok and rename_clash and unchanged and recased

    # /nation rename must report the refusal instead of surfacing an exception.
    delta = await db.create_nation(g, "Delta", 111)
    real_rename = db.rename_account

    async def rename_boom(*args, **kwargs):
        raise NameTaken("nations", "Epsilon")

    db.rename_account = rename_boom
    rename_interaction = FakeInteraction()
    try:
        await Nation.rename.callback(Nation(FakeBot(db)), rename_interaction, "Delta", "Epsilon")
    finally:
        db.rename_account = real_rename
    rename_reply = (
        rename_interaction.response.sent[-1].title if rename_interaction.response.sent else None
    )
    delta_name = (await db.get_nation_by_id(delta))["name"]
    print("/nation rename reply:", rename_reply, "name:", delta_name)
    ok = ok and "Name Taken" in (rename_reply or "") and delta_name == "Delta"

    print("VERDICT:", "ok" if ok else "BUG")
    await db.close()
    raise SystemExit(0 if ok else 1)

asyncio.run(main())
