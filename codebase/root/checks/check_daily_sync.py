"""Daily-sync checks: URL encoding, SQL generation, env config, digest."""
import json, logging, os, sys, tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import daily_sync  # noqa: E402

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
messages = daily_sync.build_guide_messages("MATERIAL")
check("prompt has system+user", len(messages) == 2)
check("prompt asks for how it works",
      "how the Royal Court bot works" in messages[0]["content"])
check("prompt forbids invention", "never invent a command" in messages[0]["content"])
check("prompt carries the material", "MATERIAL" in messages[1]["content"])
check("prompt asks for a complete document", "COMPLETE document" in messages[0]["content"])
check("prompt asks for an FAQ", "FAQ of at least six" in messages[0]["content"])
check("prompt asks for worked examples", "worked example" in messages[0]["content"])
check("prompt pins the platform", "1.20.1" in messages[0]["content"])
check("prompt sizes words from chars, with markup headroom",
      f"{daily_sync.DEFAULT_GUIDE_MAX_CHARS * daily_sync.GUIDE_WORD_HEADROOM // 10 // daily_sync.CHARS_PER_WORD} words"
      in messages[0]["content"], messages[0]["content"][-200:])
wide = daily_sync.build_guide_messages("MATERIAL", max_chars=6000)
check("smaller cap asks for fewer words", "900 words" in wide[0]["content"])
check("default guide cap is roomy",
      daily_sync.DEFAULT_GUIDE_MAX_CHARS >= 20000, daily_sync.DEFAULT_GUIDE_MAX_CHARS)
check("default source cap is roomy",
      daily_sync.DEFAULT_GUIDE_SOURCE_CHARS >= 50000, daily_sync.DEFAULT_GUIDE_SOURCE_CHARS)

# The guide can run on its own model, temperature and token budget.
with patch.dict(os.environ, {
    "DAILY_GUIDE_MODEL": "nvidia/nemotron-3.5-super-vl",
    "DAILY_GUIDE_TEMPERATURE": "0.2",
    "DAILY_GUIDE_MAX_TOKENS": "20000",
}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    check("guide model from env", ds.guide_model == "nvidia/nemotron-3.5-super-vl", ds.guide_model)
    check("guide temperature from env", ds.guide_temperature == 0.2, ds.guide_temperature)
    check("guide tokens from env", ds.guide_max_tokens == 20000, ds.guide_max_tokens)

with patch.dict(os.environ, {
    "DAILY_GUIDE_MODEL": "", "DAILY_GUIDE_TEMPERATURE": "", "DAILY_GUIDE_MAX_TOKENS": "",
}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    check("guide model defaults to the chat model", ds.guide_model == "", ds.guide_model)
    check("guide temperature defaults cool",
          ds.guide_temperature == daily_sync.DEFAULT_GUIDE_TEMPERATURE, ds.guide_temperature)
    check("guide token budget defaults big",
          ds.guide_max_tokens == daily_sync.DEFAULT_GUIDE_MAX_TOKENS, ds.guide_max_tokens)

with patch.dict(os.environ, {"DAILY_GUIDE_TEMPERATURE": "5", "DAILY_GUIDE_MAX_TOKENS": "1"}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    check("guide temperature is clamped", ds.guide_temperature == 1.0, ds.guide_temperature)
    check("guide token floor is enforced", ds.guide_max_tokens == 512, ds.guide_max_tokens)

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

# Startup run is on by default and can be switched off per half.
with patch.dict(os.environ, {
    "DAILY_TASK_RUN_ON_START": "",
    "TURSO_RUN_ON_START": "",
    "DAILY_GUIDE_RUN_ON_START": "",
}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    check("run-on-start defaults on",
          ds.run_on_start and ds.turso_run_on_start and ds.guide_run_on_start)

with patch.dict(os.environ, {
    "DAILY_TASK_RUN_ON_START": "false",
    "TURSO_RUN_ON_START": "false",
    "DAILY_GUIDE_RUN_ON_START": "false",
    "DAILY_TASK_START_DELAY_SECONDS": "12",
}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    check("run-on-start can be disabled", ds.run_on_start is False)
    check("mirror startup run can be disabled", ds.turso_run_on_start is False)
    check("startup delay from env", ds.start_delay == 12, ds.start_delay)

# The guide row carries a real guild id (a channel id is not a guild id).
with patch.dict(os.environ, {"DAILY_GUIDE_GUILD_ID": "5", "GUILD_ID": "77"}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    check("guide guild id prefers DAILY_GUIDE_GUILD_ID",
          ds.guide_guild_id() == 5, ds.guide_guild_id())
with patch.dict(os.environ, {"DAILY_GUIDE_GUILD_ID": "", "GUILD_ID": "77"}, clear=False):
    ds = daily_sync.DailySync(FakeBot(None))
    check("guide guild id falls back to GUILD_ID", ds.guide_guild_id() == 77)
with patch.dict(os.environ, {"DAILY_GUIDE_GUILD_ID": "", "GUILD_ID": ""}, clear=False):
    bot = FakeBot(None)
    check("guide guild id None with no guilds", daily_sync.DailySync(bot).guide_guild_id() is None)

    class _Guild:
        id = 123

    bot.guilds = [_Guild()]
    check("guide guild id from the only guild", daily_sync.DailySync(bot).guide_guild_id() == 123)

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


# --- source material & guide ------------------------------------------------


class _Param:
    def __init__(self, name, required=True):
        self.name = name
        self.required = required


class _Command:
    def __init__(self, name, description, params=()):
        self.qualified_name = name
        self.description = description
        self.parameters = list(params)


class _Tree:
    def __init__(self, commands):
        self._commands = list(commands)

    def walk_commands(self):
        return iter(self._commands)


class _Bot(FakeBot):
    def __init__(self, db=None, commands=()):
        super().__init__(db)
        self.tree = _Tree(commands)


source = daily_sync.DailySync(
    _Bot(None, [
        _Command("payout cash", "Pay allowances immediately.", [_Param("guild", False)]),
        _Command("payout default", "Run the full cycle."),
        _Command("transfer", "Move credits.", [_Param("source"), _Param("amount")]),
    ])
).build_source()

check("source collects commands", len(source["commands"]) == 3, source["commands"])
check("source command carries args",
      any("payout cash (guild?)" in c for c in source["commands"]), source["commands"])
check("source collects constants",
      any(c.startswith("NATION_BASE") for c in source["constants"]))
check("source collects checks",
      any(c.startswith("check_") for c in source["checks"]))
check("source collects modules",
      any(c.startswith("config:") for c in source["modules"]))
check("source collects rules", len(source["rules"]) >= 15, len(source["rules"]))

# The RULES block exists because raw constants were read as rules: 50% is a
# member shop discount, not dues, and 3 is a kingdom quota, not a three-cycle
# contract. Pin the wording that a guide can safely copy.
rules = " ".join(source["rules"])
check("rules state the real dues",
      "nation 1.75%, kingdom 2%" in rules, rules[:300])
check("rules separate the discount from dues",
      "discount on purchases, never a fee" in rules)
check("rules state the contract quota", "1 per nation, 3 per kingdom" in rules)
check("rules state the 25/75 split",
      "25% is claimable at approval and 75% at release" in rules)
check("rules state the individual cap", "capped at 100,000,000 credits" in rules)
check("rules state the leverage ladder", "3x the party's balance up to" in rules)
check("rules state the case-ID prefixes", "`UN-<TAG>-<NNN>`" in rules and "`CR-<TAG>-<NNN>`" in rules)
check("rules price the slots", "8.78%" in rules)
check("rules price roulette", "2.70%" in rules)
check("rules price the all-in", "9.09%" in rules)
check("rules price the coinflip", "50% win chance" in rules and "0% payout" not in rules)

text = daily_sync.format_source(source)
check("formatted source has command surface", "COMMAND SURFACE" in text)
check("formatted source has constants", "ECONOMY CONSTANTS" in text)
check("formatted source marks the rules authoritative",
      "ECONOMY RULES (authoritative" in text)
check("formatted source warns constants are not rules",
      "a name here is NOT a rule" in text)
check("formatted source puts the rules before the constants",
      text.index("ECONOMY RULES") < text.index("ECONOMY CONSTANTS"))
check("formatted source has modules", "MODULES" in text)
check("formatted source caps length",
      len(daily_sync.format_source(source, max_chars=50)) == 50)

guide = daily_sync.fallback_guide(source)
check("fallback guide is about the bot", "How the Royal Court works" in guide)
check("fallback guide groups commands",
      "`/payout` - 2 command(s)" in guide, guide[:200])
check("fallback guide lists modules", "config:" in guide)

# --- guide trimming ---------------------------------------------------------
# The live guide is capped at 24,000 characters. A plain slice cut it mid-word
# and left an inline code span open ("`/fishing"), so the tail of the document
# was unreadable. Trimming has to land on a structural boundary and say so.
check("a guide inside the cap is untouched", daily_sync.trim_guide("short", 100) == "short")

long_guide = "\n\n".join(
    [f"## Section {i}\n" + ("word " * 30).strip() for i in range(300)]
)
trimmed = daily_sync.trim_guide(long_guide, 2000)
check("trim enforces the cap", len(trimmed) <= 2000, len(trimmed))
check("trim says it was trimmed", trimmed.endswith(daily_sync.GUIDE_TRIM_NOTE), trimmed[-120:])
check("trim keeps whole lines",
      trimmed[: -len(daily_sync.GUIDE_TRIM_NOTE)].rstrip().endswith("word"), trimmed[-160:])

# The exact live defect: the cut lands inside "`/fishing" with the span open.
open_span = "text\n\nUse `/casino fund` or `/fishing"
repaired = daily_sync._drop_open_markup(open_span)
check("an open inline span is dropped",
      "/fishing" not in repaired and repaired.count("`") % 2 == 0, repr(repaired))
check("closed spans before it survive", "`/casino fund`" in repaired, repr(repaired))
open_fence = "intro\n```\ncode line\n"
check("an open fence is dropped",
      daily_sync._drop_open_markup(open_fence) == "intro\n", repr(daily_sync._drop_open_markup(open_fence)))

fenced_doc = "\n\n".join(["## S\n" + "word " * 30 for _ in range(60)]) + "\n\n```\nunclosed"
fenced_cut = daily_sync.trim_guide(fenced_doc, 1200)
check("trim never leaves a fence open", fenced_cut.count("```") % 2 == 0, fenced_cut[-120:])
check("trim leaves balanced inline code", fenced_cut.count("`") % 2 == 0, fenced_cut[-120:])
check("trim on a degenerate cap still respects it",
      len(daily_sync.trim_guide("x" * 500, 50)) == 50)

print("\nFAILURES:", len(fails))
for f in fails:
    print(" -", f)
raise SystemExit(1 if fails else 0)
