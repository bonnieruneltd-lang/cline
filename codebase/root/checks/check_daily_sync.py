"""Daily-sync checks: URL encoding, SQL generation, env config, digest."""
import asyncio, json, logging, os, sys, tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import daily_sync  # noqa: E402
from database import Database  # noqa: E402

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")
        print(f"FAIL  {label}  {detail}")
    else:
        print(f"ok    {label}")


class FakeBot:
    def __init__(self, db):
        self.db = db
        self.logger = logging.getLogger("check")

    def is_ready(self):
        return False


# --- endpoint normalisation -------------------------------------------------
check("libsql -> https pipeline",
      daily_sync.libsql_http_endpoint("libsql://x.turso.io")
      == "https://x.turso.io/v2/pipeline")
check("trailing slash stripped",
      daily_sync.libsql_http_endpoint("libsql://x.turso.io/")
      == "https://x.turso.io/v2/pipeline")
check("https pipeline kept",
      daily_sync.libsql_http_endpoint("https://x.turso.io/v2/pipeline")
      == "https://x.turso.io/v2/pipeline")
check("ws -> http",
      daily_sync.libsql_http_endpoint("ws://localhost:8080")
      == "http://localhost:8080/v2/pipeline")
check("empty stays empty", daily_sync.libsql_http_endpoint("") == "")

# --- typed values & SQL -----------------------------------------------------
check("null", daily_sync.encode_value(None) == {"type": "null"})
check("int", daily_sync.encode_value(7) == {"type": "integer", "value": "7"})
check("bool true", daily_sync.encode_value(True) == {"type": "integer", "value": "1"})
check("float", daily_sync.encode_value(1.5)["type"] == "float")
check("text", daily_sync.encode_value("hi") == {"type": "text", "value": "hi"})
check("blob", daily_sync.encode_value(b"ab")["type"] == "blob")
check("quote escapes", daily_sync.quote_identifier('a"b') == '"a""b"')
check("insert statement",
      daily_sync.insert_statement("t", ["a", "b"])
      == 'INSERT OR REPLACE INTO "t" ("a", "b") VALUES (?, ?)',
      daily_sync.insert_statement("t", ["a", "b"]))
check("CREATE TABLE made idempotent",
      daily_sync.ensure_create_if_not_exists("CREATE TABLE t (id INTEGER)")
      == "CREATE TABLE IF NOT EXISTS t (id INTEGER)")
check("existing IF NOT EXISTS kept",
      daily_sync.ensure_create_if_not_exists("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
      == "CREATE TABLE IF NOT EXISTS t (id INTEGER)")
check("empty DDL stays empty", daily_sync.ensure_create_if_not_exists("") == "")

# --- guide prompt -----------------------------------------------------------
messages = daily_sync.build_guide_messages("SHEET")
check("prompt has system+user", len(messages) == 2)
check("prompt forbids invention", "Never invent" in messages[0]["content"])
check("prompt carries the digest", "SHEET" in messages[1]["content"])

# --- env driven config (nothing hard-bound) ---------------------------------
with patch.dict(os.environ, {
    "TURSO_DATABASE_URL": "libsql://db.turso.io",
    "TURSO_AUTH_TOKEN": "tok",
    "DAILY_TASK_HOUR_UTC": "3",
    "DAILY_GUIDE_CHANNEL_ID": "123",
}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    check("turso enabled from env", ds.turso_enabled is True)
    check("endpoint from env", ds.turso_endpoint == "https://db.turso.io/v2/pipeline")
    check("hour from env", ds.hour_utc == 3)
    check("channel from env", ds.guide_channel_id == 123)

with patch.dict(os.environ, {"TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": ""}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    check("missing creds disables mirror", ds.turso_enabled is False)

with patch.dict(os.environ, {
    "TURSO_DATABASE_URL": "libsql://db.turso.io",
    "TURSO_AUTH_TOKEN": "tok",
    "TURSO_SYNC_ENABLED": "false",
}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    check("TURSO_SYNC_ENABLED=false disables mirror", ds.turso_enabled is False)

# The mirror has its own runtime + temp config, separate from the guide.
with patch.dict(os.environ, {
    "TURSO_DATABASE_URL": "libsql://db.turso.io",
    "TURSO_AUTH_TOKEN": "tok",
    "TURSO_RUN_HOUR_UTC": "4",
    "TURSO_TEMP_DIR": "/tmp/turso-scratch",
}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    check("mirror runtime hour from env", ds.turso_run_hour == 4, ds.turso_run_hour)
    check("mirror temp dir from env", str(ds.temp_dir) == "/tmp/turso-scratch", ds.temp_dir)
    check("mirror runtime configured", ds.turso_run_configured is True)

with patch.dict(os.environ, {
    "TURSO_DATABASE_URL": "libsql://db.turso.io",
    "TURSO_AUTH_TOKEN": "tok",
    "TURSO_RUN_ENABLED": "false",
}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    check("TURSO_RUN_ENABLED=false stops the mirror runtime",
          ds.turso_run_configured is False)

# The checkpoint is written into the mirror's own temp directory.
checkpoint_dir = tempfile.mkdtemp()
with patch.dict(os.environ, {"TURSO_TEMP_DIR": checkpoint_dir}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    ds.write_checkpoint({"nations": 2, "transactions": 5})
    saved = json.loads((Path(checkpoint_dir) / "last_sync.json").read_text())
    check("checkpoint tables", saved["tables"] == 2, saved.get("tables"))
    check("checkpoint rows", saved["rows"] == 7, saved.get("rows"))
    check("checkpoint per-table", saved["per_table"]["transactions"] == 5, saved)

# The guide only runs when it has a destination (channel, file or Turso).
with patch.dict(os.environ, {
    "DAILY_GUIDE_CHANNEL_ID": "0",
    "DAILY_GUIDE_PATH": "",
    "TURSO_DATABASE_URL": "",
    "TURSO_AUTH_TOKEN": "",
}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    check("guide off without a destination", ds.guide_configured is False)

with patch.dict(os.environ, {
    "DAILY_GUIDE_CHANNEL_ID": "999",
    "TURSO_DATABASE_URL": "",
    "TURSO_AUTH_TOKEN": "",
}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    check("guide on with a channel", ds.guide_configured is True)

with patch.dict(os.environ, {
    "DAILY_GUIDE_CHANNEL_ID": "0",
    "DAILY_GUIDE_PATH": "guide.md",
    "TURSO_DATABASE_URL": "",
    "TURSO_AUTH_TOKEN": "",
}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    check("guide on with a file path", ds.guide_configured is True)


# --- digest from a throwaway database --------------------------------------
async def digest_checks():
    db = Database(os.path.join(tempfile.mkdtemp(), "d.db"))
    await db.connect()
    try:
        nation_id = await db.create_nation(1, "Aurum", 42)
        await db.add_balance("nations", nation_id, 20_000)
        await db.create_kingdom(1, "Crownlands", 42)
        await db.get_individual(1, 7, "Tester")
        await db.log_tx(guild_id=1, kind="payout", account_type="nation",
                        account_id=nation_id, account_name="Aurum", amount=20_000,
                        balance_after=20_000, note="test payout")
        ds = daily_sync.DailySync(FakeBot(db))
        digest = await ds.build_digest(24)
        check("digest counts nations", digest["table_counts"].get("nations", 0) >= 1)
        check("digest counts transactions",
              digest["table_counts"].get("transactions", 0) >= 1)
        check("digest keeps recent tx", len(digest["recent_transactions"]) == 1)
        check("digest sees top balance",
              digest["top_balances"]["nations"][0]["balance"] == 20_000)
        check("digest lists houses", "casino_account" in digest["houses"])
        text = daily_sync.format_digest(digest)
        check("formatted digest has counts", "TABLE ROW COUNTS" in text)
        check("formatted digest has tx", "test payout" in text)
        guide = daily_sync.fallback_guide(digest)
        check("fallback guide non-empty", "daily guide" in guide and "/daily" in guide)
    finally:
        await db.close()


asyncio.run(digest_checks())

print("\nFAILURES:", len(fails))
for f in fails:
    print(" -", f)
raise SystemExit(1 if fails else 0)
