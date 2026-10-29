# Working notes for agents

Local mirror of the live Royal Court bot. The bot runs from the SFTP account
root; this checkout is a working copy, not a deployment origin.

Before changing anything, read `README.md` sections 7 (economy rules), 10
(contract payout model) and 12 (maintenance log), then `BUG-FIXES.md` for the
defects already found in the area you are touching and the checks that cover
them. Section 12 records what is
already deployed and how it was verified, so you can tell live code from local
work.

## Environment

- SFTP access uses the environment variables `SFTP_HOST`, `SFTP_PORT`,
  `SFTP_USER` and `SFTP_PASS`. They are injected per session from the owner's
  registered secrets: just name them in a command and they are exported. They
  are **not** persisted to disk, so a fresh session that cannot see them should
  recover them from `~/.bash_history` (earlier sessions ran with them inline)
  into a `chmod 600` file such as `/tmp/.sftp_env`, and `source` that. Never
  copy credentials into this directory, and never upload `.env`, `*.db`,
  `*.db-wal`, `*.db-shm` or `__pycache__`.
- `python3 -m pip install -r requirements.txt` is enough to run the logic tests.
- `paramiko` is needed for SFTP access; it is not a bot dependency.

## Checks before uploading

```bash
python3 checks/run_all.py         # the whole suite, PASS/FAIL table
python3 -m compileall -q .        # syntax
python3 -m pyflakes .             # unused names / undefined references
```

`checks/run_all.py` covers pure maths, database integrity, transaction
isolation, contract lifecycle/quota/approval/case-ID guards, individual
transfers, unified all-direction transfers, the per-run tax cycle (including
stale-list and recreated-wallet races), bank/draw guards, factions/Crown
contracts and tax, coinflip, all-in, blackjack, fishing, poker, Gilbert
configuration/prompt facts, the Casino and Fishing Guild houses, the `/daily`
race, the daily Turso mirror/AI-guide config, the creation race, the
wallet-deletion lifecycle, the runner's own
false-pass guard and a cog-load smoke test. All **19** pass as of 2026-10-29
(`check_casino.py` also drives `/poker` end to end, `check_fishing.py` drives
`/fish`, and `check_factions.py` drives the faction/Crown commands; 
`check_contracts.py` drives a real Crown request, the Crown-funded claim and the
per-faction quota). All of them work on throwaway SQLite
files, so they never touch the live database. The runner only accepts a
recognised summary line as well as exit 0; a bare exit or an `ERROR:` line is a
failure.
The cog-load check also asserts the command count: `bot.tree.walk_commands()`
must list **112** commands and `COGS` must have 16 entries, asserted by
`checks/check_cogs.py` (**112 = 111 + `/give`**; 111 = 113 - `/faction audience`
and the two specialized contract subcommands + `/faction contract`. 113 was the
`/faction` group's sixteen action subcommands; 97 was the single `/faction`
command. `/contract` remains the contract-lifecycle group. 107 = 104 + `/payout
default`, `/payout trigger` and the top-level `/transfer`; `/payout cash`
replaced `/payout run`, so that rename is net zero. 104 was `/fish` plus the
`/fishingguild` group and its three subcommands; 99 before that; 98 before
`/poker`; 93 before `/blackjack` and `/casino`; 92 before `/allin`; 91 before
`/individual coinflip`; 89 before `/individual transfer` and `/individual pay`;
80 before `/daily`, `/individual` and `/leaderboard` landed). If that number
moves, a command was added or lost — update the assertion deliberately, never
by editing the script to match whatever it printed.
`daily_sync.py` (the daily Turso mirror + AI guide) is started from `bot.py` the
same way `api.py` is: it is **not** a cog, so it adds no commands and leaves the
112 / 16 assertions untouched. All of its knobs are environment variables (see
README "Daily sync"); with no `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` it only logs
that the mirror is skipped.



`check_cogs.py` prints a harmless `Client has not been properly initialised`
traceback at the end: it starts `Payout.cycle_task` without logging in. Ignore it,
but do not silence it — the exception is what tells you the loop wiring changed.

## Deploying

The live app is the SFTP account root (`/`), so remote paths are `/bot.py`,
`/cogs/...`, `/README.md`. Upload only the changed files, keeping a timestamped
backup of each (`<file>.bak-YYYYMMDD-HHMMSS`) beside the original, then
re-download and compare SHA-256. Code changes do nothing until the process is
restarted from the hosting panel; schema migrations only run at startup.

A proven deploy loop:

1. Check each target's remote mtime against your pull time. If any is newer,
   re-pull before editing — someone uploaded over you.
2. For each file: `sftp.get` it to a temp path, `sftp.put` that byte-exact copy
   to `<file>.bak-<stamp>`, then `sftp.put` the new local file over the original.
3. Compare SHA-256 of the remote original against the local file. All must match.
4. Pull the whole deployed `.py` tree into an empty directory (skip `.env`,
   `*.db`, `__pycache__`) and run `compileall`, `pyflakes`, the logic checks and
   the cog-load smoke test **against that copy**, not your working copy. This is
   what catches a file you forgot to upload.

### Last known live state

2026-10-29 03:00 UTC (**0216 build confirmed live**). The process restarted at
03:00:49–55 and loaded the `/give` build: the live surface is **112** commands,
`/give` takes `source`, `member` and `amount`, and the startup schema created
`case_counters_prefix`. The 02:00:04–05 restart had loaded the
`20261029-0028` build (proposal-only `/faction contract`, `contracts.faction`
backfilled to `union`, 111 commands). The live DB passes `integrity_check: ok`
with 0 per-account ledger gaps; the only aggregate gap is the historical
**30,000** from deleted nation id 5. The next Union case is `UN-SOVERE-004` and
the first Crown case is `CR-SOVERE-001`; `UN-COMMUN-001` is still unclaimed
(both flags 0), so one `/contract claim` pays the whole 1,000,000 to the
Commune. Suite **18/18**, surface **112 / 16 cogs**.

2026-10-29 (uploaded, stamp `20261029-0028`; **live since 02:00 UTC**). **One
proposal field.** `/faction contract` takes `faction`, `account`, a free-text
`proposal` and `budget`; `item`, `quantity` and `project` are gone. Union
proposals are public work for Gilbert; Crown proposals are supply offers for
Albert whose goods an officer collects in-game (the bot does not track item
counts). The 25/75 tranches still come from the chosen faction's treasury.
Proposal text lives in the legacy `contracts.project` column; the retired
`project=` keyword is accepted by the storage/prompt APIs for one deploy
window. The dead `/commit` method was deleted; `/faction commitment` and
`/faction uncommit` call the Union desk directly. Suite **18/18**, 111 / 16
cogs, hashes verified against the deployed pull.

2026-10-28 (uploaded, stamp `20261029-0000`; **restart pending**). **Contract
requests are conversational again.** `/faction contract` takes `faction`,
`account`, `project`, `budget` — no `item`, no `quantity`, no dice roll and no
faction-membership check. Union requests open Gilbert, Crown requests open
Albert, and both briefings carry the live numbers before the first reply. The
25/75 tranches are paid from `contracts.faction`'s treasury (`union_account` or
`crown_account`; Crown cases use `CR-*` case IDs and a separate quota). Legacy
Crown supplier rows still deliver through `/faction deliver`. The suite is
**18/18** (Crown request/claim/quota/treasury and prompt-preload checks were
added), `compileall`/`pyflakes` clean, surface unchanged at **111 / 16 cogs**.
All 16 changed files were uploaded with `.bak-20261029-0000` backups,
re-downloaded and SHA-256 matched; a fresh pull of the deployed tree
(`/tmp/deployed_20261029-0000`) passes 18/18.

2026-10-28 08:12 UTC. **`/faction` hub admin-check bypass fixed (uploaded, stamp
`20261028-0311`).** `admin_only()` is an `app_commands.check` on the command
object, so forwarding with `<command>.callback(...)` skipped it: `/faction fund`
let any member mint credits into the Crown or Union treasury and
`/faction withdraw` let them burn the Crown's. `Factions._require_admin()` now
gates `fund`, `withdraw` and `setchannel`, and those wrappers also carry
`@admin_only()`. `check_factions.py` drives the hub as a non-admin (both
treasuries and the commitment channel must be unchanged, and an admin must still
be able to fund), `check_cogs.py` pins the three subcommands. Uploaded four
files (`cogs/factions.py`, `checks/check_factions.py`, `checks/check_cogs.py`,
`BUG-FIXES.md`) with `.bak-20261028-031103` backups; SHA-256 matches, a fresh
pull of the deployed tree is byte-identical to the working copy, and
`compileall`/`pyflakes`/the suite (**18/18**, **111 / 16 cogs**) pass against
that pull (`/tmp/deployed_fixcheck`). **Restart remains required** because the
process still serves the 0135 surface — and that surface contains the bypass, so
the restart is also what closes it.


2026-10-28 04:35 UTC. **Faction contract judgment (uploaded, stamp
`20261028-0435`).** Removed `/faction audience`, `/faction union-contract` and
`/faction crown-contract`; `/faction contract` takes a required budget and
judges the account's live record. Crown odds scale with soldiers and
credits/items received; Union odds scale with membership and
dues/donations/commitments. No hard soldier/donation threshold: Crown
membership is open at any soldier count, and the contract minimum is 1 credit.
A failed roll creates no contract. `check_factions.py` covers the score
ordering, a 19-soldier Crown join, low-troop tax and approved/refused contract
paths; `check_cogs.py` pins **111 / 16 cogs**. Deploy hash-verified and
re-pulled to `/tmp/deployed_20261028-0435`, passing **18/18**. **Restart is
required** for the new surface and rules.

2026-10-28 03:55 UTC. **Audience contract-budget request (uploaded, stamp
`20261028-0355`).** `/faction audience` takes an optional `budget` in credits.
It is included in both representatives' context and fallbacks, shown as a
`Requested Budget` field, and stored with the conversation; it is discussion
only and cannot approve or create a contract. The no-membership prompt/tone
change is included. `check_factions.py` sends 50,000/30,000 budgets through the
Crown/Union audience routes and checks the context, response field and memory.
Deploy hash-verified and re-pulled to `/tmp/deployed_20261028-0355`, passing
**18/18**. The live 0135 action-subcommand group is serving, but its audience
subcommand still exposes only `faction`/`account`/`message`; **0355 needs one
more restart** to load the budget option and prompts.

2026-10-28 03:25 UTC. **Audience is membership-neutral (uploaded, stamp
`20261028-0325`).** `/faction audience` is open to every account, including
unaffiliated accounts and the other faction; no membership check runs. The
prompts and fallbacks state that membership only changes warmth: Union/Crown
members in good standing get a warmer representative, while nonmembers still
get a full, courteous answer. `check_factions.py` adds an unaffiliated
`Neutral` nation and drives both audience routes with the AI disabled, plus
prompt/context checks. Deploy hash-verified and re-pulled to
`/tmp/deployed_20261028-0325`, passing **18/18**. A live check shows the 0135
action-subcommand group is serving; the 0325 prompt/tone code is uploaded and
needs one more restart if the process started before it.

2026-10-28 01:35 UTC. **One `/faction` group with action subcommands
(uploaded, stamp `20261028-0135`).** Removed `/join`, `/commit`, `/donate`,
`/audience`, the `/crown` group and the `/union` group. `/faction` is a group
whose subcommands each expose only their own options: `join`, `leave`,
`union-contract`, `crown-contract`, `commitment`, `uncommit`, `donate`,
`deliver`, `audience`, `status`, `contracts`, `cancel`, `fund`, `withdraw`,
`members`, `setchannel`. `/contract` remains the contract-lifecycle group.
The surface is **113 / 16 cogs**. `check_factions.py` drives join, Crown
contract/delivery/status, both audiences, Union contract, commitment and
uncommit through the subcommands; `check_cogs.py` pins 113 and asserts every
old faction command is absent. The deploy was hash-verified and re-pulled to
`/tmp/deployed_20261028-0135`, passing **18/18**. A live Discord command check
after the upload shows the process serving the 0125 **single** `/faction` hub:
`/commit`, `/audience`, `/union` and `/crown` are gone, but `/faction` still has
the static `faction`/`action` options. **Restart remains required** for 0135 so
the action subcommands replace it.

2026-10-28 01:05 UTC. **Commitments consolidated into `/commit`
(uploaded, stamp `20261028-0105`).** Removed `/union commit` and
`/union uncommit`; `/commit` now has `kind:commitment` and `kind:uncommit`
(`Union.request_commit()` / the extracted `Union.cancel_commit()`). The
`/union` group is treasury, membership, donations, funding and channel
configuration only. The surface is **114 / 16 cogs**; `check_factions.py`
drives commitment and uncommit through `/commit`, `check_cogs.py` pins 114 and
asserts both old Union subcommands are absent. Deploy hash-verified and
re-pulled to `/tmp/deployed_20261028-0105`, passing **18/18**. A live Discord
command check at 01:05 UTC shows the 0015/0055 surface is serving (`/audience`,
`/commit` with `kind`, `/contract` without `request`) and still exposes
`/union commit` / `/union uncommit`, so **restart remains required** for 0105.

2026-10-28 00:55 UTC. **Single `/commit` contract entry point (uploaded,
stamp `20261028-0055`).** Removed `/contract request`; `/commit` now takes
`faction` + `kind`:
`faction:union kind:contract` (project + budget, delegated to the extracted
`Contract.request_contract()`, old leader/Union Admin and quota rules),
`faction:union kind:commitment` (`Union.request_commit()`),
`faction:crown kind:contract` (supplier contract), and
`faction:crown kind:commitment` (refused). The `/contract` group now holds
lifecycle commands only. The surface is **116 / 16 cogs**; `check_factions.py`
covers the Union contract routing and commitment route, `check_cogs.py` pins
116. The deploy was hash-verified and pulled to `/tmp/deployed_20261028-0055`,
passing **18/18**; **restart remains required**.

2026-10-28 00:15 UTC. **Merged faction commands and audience repair
(uploaded, stamp `20261028-0015`).** `/commit` now takes `faction` first:
`faction:union` calls the extracted `Union.request_commit()` (identical to
`/union commit`), `faction:crown` signs the supplier contract. `/audience`
takes `faction:union|crown` and routes to Gilbert (`factions.union_report()` +
`gilbert.prompt.union_audience_*`) or Albert (`factions.crown_report()` +
`albert_prompt`). The old `/crown audience` subcommand is gone. The AI call is
capped at 20s (`config.NPC_AUDIENCE_TIMEOUT_SECONDS`) with a deterministic
fallback; `albert_memory` gained an `npc` column (`albert`/`gilbert`) via
migration so histories cannot mix. `check_factions.py` covers both audience
routes with AI disabled, memory separation, and the real Union desk through
`/commit faction:union`; the suite is **18/18**, `compileall`/`pyflakes` clean,
command surface **117 / 16 cogs**, and the deployed copy at
`/tmp/deployed_20261028-0015` passes **18/18**; the live tree is **74/74**
`.py`/`.md` hash-identical. **Restart from the hosting panel is still required**
before `/audience`, the new `/commit` choices and the `albert_memory.npc`
migration appear live.

2026-10-27 23:55 UTC. **Faction/Crown restart confirmed live.** Uploads
`20261027-2325` (faction/Crown feature plus the five bug-testing fixes) and
`20261027-2340` (kingdom cap 6 and docs) are serving. The startup migration
created `faction_members` / `crown_account` / `crown_contracts` / `crown_sales`
/ `faction_donations` / `albert_memory`, seeded the three legacy Union members,
and an admin funded the Crown with **10,000,000** credits (`crown_fund` tx at
`23:55:35`). The 30,000-credit `BoxierFire25` gap remains. Smoke test:
`/crown status` → `/nation stats` (20 soldiers) → `/join crown` → `/crown fund`
→ `/commit faction:crown` → `/donate item` → `/crown contracts`, plus
`/audience faction:union` for the Gilbert route.

2026-10-27 23:25 UTC. **Factions/Crown feature uploaded (stamp
`20261027-2325`; restart pending).** Added `factions.py`, `albert_prompt.py`,
`cogs/factions.py`, the `faction_members` / `crown_account` /
`crown_contracts` / `crown_sales` / `faction_donations` / `albert_memory`
schema, the Crown payout tax, and `/join`, `/commit`, `/donate` plus `/crown`.
The suite is **18/18** and the command surface is **117 / 16 cogs**. A dry
migration of the pulled live database created every table and seeded the three
legacy Union members into `faction_members` with `integrity_check: ok`. The
upload also carries the five bug-testing fixes. A follow-up upload (stamp
`20261027-2340`) raises `MAX_NATIONS_PER_KINGDOM` from 4 to 6 and pins the
command boundary. **The running process still serves the previous build until
the hosting panel restarts it**; the new tables are created at that startup.
The live SFTP tree is hash-verified against this checkout.

2026-10-27 22:55 UTC. **Live audit and local fixes.** The live tree is still the
`20261027-221143` build: all 70 deployed `.py`/`.md` files matched this checkout
by SHA-256, and the live DB copy passed `integrity_check` with exact
per-account ledger reconciliation except the known 30,000-credit
`BoxierFire25` gap. The **restart has happened**: live rows at 22:23:49 UTC
include the new individual net-worth tax, Union-commit hooks and a natural
`last_cycle_at` of 22:22:57, so the 22:11 build is serving. A bug pass then
found and fixed five defects locally (not uploaded yet): a concurrent
`/payout default` could pay twice because the due-check ran outside the cycle
lock; `/bank setbalance` awaited Discord inside its DB transaction; the
all-in confirmation could be double-clicked into two bets; `blackjack.is_soft`
misread multi-ace soft hands; and a creation name race could strand a petition
or crash a direct add. See `BUG-FIXES.md`; the suite is **17/17** with the new
regression cases. That 22:11 restart is no longer pending; the later 23:25 faction upload
is a separate restart.

2026-10-27 22:11 UTC. Uploaded with stamp `20261027-221143` (per-file backups
beside each original). The payout surface
is now `/payout cash` (Cash Cycle, no schedule/contract-cycle change),
`/payout default` (full natural cycle in one run) and `/payout trigger` (queue or
schedule the natural cycle without paying); `/payout run` is gone. New
`account_transfers.py` is the one atomic transfer core, exposed by the new
top-level `/transfer` and by `/bank transfer`; it supports every direction
between individual, nation and kingdom accounts. The bug-testing pass also fixed
the silent balance burn on `/nation delete`/`/kingdom delete` (now
`Database.delete_account()`) and the `/contract reopen` hole that made a
rejected case claimable. The deployed tree was re-pulled to
`/tmp/deployed_20261027-221143` and passed `checks/run_all.py` **17/17** with
`compileall`/`pyflakes` clean; command count **107 / 15 cogs**. No schema
migration was needed and the live DB was not modified. **Restart pending**: the
running process still serves the previous build until the hosting panel restarts
it. The historical 30,000-credit gap from deleted nation id 5 (`BoxierFire25`)
remains and is not rewritten by this build.

2026-10-27 13:40 UTC. `/poker` now uses Minecraft-themed bot names generated
through `GilbertAI` with the same `AI_API_KEY` as Gilbert. New `poker_names.py`
does a background refresh (8s cap) and `/poker` samples three names
synchronously; `config.POKER_BOT_NAME_POOL` is the offline fallback. Uploaded
with per-file backups (`<file>.bak-20261027-134031`): `poker_names.py` (new),
`config.py`, `cogs/games.py`, `checks/check_poker.py`, `README.md`,
`cogs/README.md` and `AGENTS.md`. `checks/run_all.py` **16/16**,
`compileall`/`pyflakes` clean, command count **104 / 14 cogs**. The deployed
tree was re-pulled to `/tmp/deployed_20261027-134031` and passed **16/16**
against that copy. **Restart still pending** from the earlier
economy/audit/kit uploads: the running process still uses the old medieval names
(and the old odds, tax, cooldown and unfixed audit paths) until the hosting
panel restarts it. This is the session close-out state: every code and doc change
is uploaded and hash-verified; only the hosting-panel restart remains.

2026-10-27 13:34 UTC. Kit cooldown shortened from 30 to **7 days**
(`config.KIT_COOLDOWN_DAYS`), uploaded with per-file backups
(`<file>.bak-20261027-133459`): `config.py`, `cogs/kits.py`,
`checks/check_database.py`, `README.md`, `cogs/README.md` and `AGENTS.md`.
`checks/run_all.py` **16/16** with an explicit day-6 blocked / day-7 allowed
boundary; `compileall`/`pyflakes` clean; command count **104 / 14 cogs**.
The deployed tree was re-pulled to `/tmp/deployed_20261027-133459` and passed
**16/16** against that copy. Existing `kit_cooldowns` rows are untouched and
become eligible after seven days. **Restart still pending** from the earlier
economy/audit upload as well: the running process still uses the 30-day cooldown
(and the old 49% odds, 1.2x all-in and natural-only tax) until the hosting panel
restarts it.

2026-10-27 13:26 UTC. Economy retune and all eleven audit fixes uploaded with
per-file backups (`<file>.bak-20261027-132619`): 32 files (30 existing with
backups, plus new `checks/check_bank.py` and `checks/check_runner.py`). Coinflip
is now a 50% win / 2x fair pass-through (`house_edge_bp() == 0`), `/allin` uses
1.1x hardness (`10/11` return, 9.09% edge), and the 5% individual tax runs on
**every** payout run, natural and manual. The audit fixes cover the stale tax
list, wallet resurrection, draw membership, contract quota/approval races,
stale `setbalance`, guarded statuses, cross-guild case IDs, poker uncalled
bets/short all-ins, the DM settings ghost row and the runner false pass. Every
upload was hash-verified (32/32). The deployed tree was pulled to
`/tmp/deployed_20261027-132619` and passed **16/16** with
`compileall`/`pyflakes` clean; command count is **104 / 14 cogs**. A read-only
copy of the live database migrated cleanly (`integrity: ok`,
`case_counters_global` present, no duplicate case IDs, no ghost settings row).
**Restart still required** from the hosting panel: the running process is still
the old 49% / 1.2x / natural-only-tax build until it restarts, and this upload
is the one that creates `case_counters_global`.

2026-10-27 03:39 UTC. `/fish` and the Fishing Guild uploaded with per-file
backups (`<file>.bak-20261027-033920`): new `fishing.py`, `cogs/fishing.py` and
`checks/check_fishing.py`; changed `bot.py`, `config.py`, `database.py`,
`cogs/ledger.py`, `checks/run_all.py`, `checks/check_cogs.py`, `README.md`,
`AGENTS.md`, `cogs/README.md` and `BUG-FIXES.md`. `/fish` pays a random 20–100
credit catch from `fishing_guild_account`; `/fishingguild balance` is public and
`fund`/`withdraw` are admin-only. The deployed tree was re-pulled and passed
**14/14** with `compileall`/`pyflakes` clean; command count is **104 / 14 cogs**.
The process restarted after this upload: `fishing_guild_account` is present and
is live. The guild was funded with 1,000,000; live `/fish` data shows ten catches
totalling 813 credits, a **999,187** guild balance, reconciled ledger rows and
`poker_hands` present. No restart remains. A close-out stress test also ran 10
concurrent catches against a 100-credit guild: five paid, five refused, guild 0,
players 100, and 10 ledger rows netting to zero. Exact cap/guild boundaries
passed, and a live-DB copy migrated cleanly. README/AGENTS docs were refreshed
with backup `.bak-20261027-125500`.

2026-10-27 02:24 UTC. `/poker` uploaded with per-file backups
(`.bak-20261027-022436`): `cogs/games.py`, `gambling_ui.py`, `casino.py`,
`config.py`, `database.py`, `bot.py`, `checks/check_casino.py`,
`checks/check_cogs.py`, `README.md` and `cogs/README.md`. The command is a
public four-seat Texas Hold'em table with Fold/Check/Call/Raise/All-In,
bot turns, a 180s auto-fold, a held `poker_hands` buy-in with startup refund,
and final-stack settlement through
`casino.finish_wager(..., payout_override=final_stack)`. The deployed tree was
re-pulled and passed **13/13** with `compileall`/`pyflakes` clean; command
count is **99 / 13 cogs**. **Restart still pending**, and this is the upload
that creates `poker_hands`.

2026-10-27 01:49 UTC. Public gambling UI uploaded with per-file backups
(`.bak-20261027-014918`): new `gambling_ui.py`, `cogs/individual.py`,
`cogs/games.py`, `checks/check_coinflip.py`, `checks/check_allin.py`,
`checks/check_casino.py`, `README.md` and `cogs/README.md`. Every upload was
hash-verified; the deployed `.py` tree was re-pulled to `/tmp/deployed_ui` and
passed **13/13** with `compileall`/`pyflakes` clean. This pass changes UI only:
spectator cards, reaction bait, blackjack table fields and the `/casino balance`
House & Floor dashboard. Reaction calls are deferred until after the result
response, so they cannot delay a button edit. At that time poker was still
engine-only; the 02:24 `/poker` entry above supersedes it. `AGENTS.md` itself
was re-uploaded after this verification with backup `.bak-20261027-015322`
(and again after the reaction-ordering fix, `.bak-20261027-015723`).
**Restart still pending.**

2026-10-27 01:28 UTC. Contract minimum corrected to **100 credits** (maximum
remains 100,000,000), so a new contract is 100–100M; lower-budget rows are
grandfathered for claim/release. Backups use `.bak-20261027-012852`. Uploaded
files: `config.py`, `contract_limits.py`, `cogs/contract.py`, `gilbert/ai.py`,
`gilbert/prompt.py`, `checks/check_logic.py`, `checks/check_gilbert.py`,
`README.md`, `AGENTS.md`, `BUG-FIXES.md`, `cogs/README.md`,
`gilbert/README.md`. The deployed tree passed **13/13** after the upload.
The live `.env` was rotated to the new owner-supplied `AI_API_KEY`
(backup `.env.bak-20261027-012852`) and a test completion returned HTTP 200.
Restart still pending.

2026-10-27 00:24 UTC. Transaction/contract/Gilbert fixes uploaded with per-file
backups (`<file>.bak-20261027-002422`): `database.py`, `creation.py`,
`cogs/bank.py`, `cogs/contract.py`, `cogs/games.py`, `cogs/individual.py`,
`cogs/kits.py`, `cogs/shop.py`, `cogs/union.py`, `gilbert/ai.py`,
`checks/check_cogs.py`, `checks/check_database.py`,
`checks/check_transactions.py`, `checks/run_all.py`, plus new
`checks/check_contracts.py` and `checks/check_gilbert.py`. Every upload was
hash-verified; the deployed `.py` tree was re-pulled and `checks/run_all.py`
passed **13/13** with `compileall`/`pyflakes` clean. **Restart pending.** At that
time the live `.env` key was invalid; it was replaced later (see the 01:28 UTC
entry above).

2026-10-24 17:05 UTC. Poker engine landed **without a command**: `poker.py`
(new), `checks/check_poker.py` (new), `config.py` and `checks/run_all.py`
changed, backup stamp `20261024-165039`; docs uploaded with stamp
`20261024-170500`. Every file was hash-verified after upload and the deployed
tree was re-pulled and re-tested: `compileall` and `pyflakes` clean,
`checks/run_all.py` **11/11**. Nothing imports `poker.py`, so the upload cannot
change live behaviour and the command count stays at **98 commands / 13 cogs**.

2026-10-24 04:43 UTC. Seventeen files uploaded with per-file backups
(`.bak-20261024-044331`), every one hash-verified, then the deployed tree was
re-pulled and re-tested: `compileall` and `pyflakes` clean, `checks/run_all.py`
**10/10**. Carries the Casino house account, `/blackjack`, `/casino`, public
gambling, and the pinned count at **98 commands / 13 cogs**.

**The process has not been restarted.** `/blackjack` and `/casino` are on disk
but inert until the hosting panel restarts the bot, which is also when
`casino_account` and `blackjack_hands` are created. If a later session finds the
commands missing, check for a restart before re-uploading anything.

2026-10-23 09:44 UTC. Four uploads, all hash-verified and then re-verified by
pulling the deployed tree and running `checks/run_all.py` against it:

- stamp `20261023-094442` — `allin.py` (new), `checks/check_allin.py` (new),
  `config.py`, `cogs/individual.py`, `cogs/ledger.py`, `checks/run_all.py`,
  `checks/check_cogs.py`, `README.md`, `AGENTS.md`, `BUG-FIXES.md`,
  `cogs/README.md`. Carries `/allin` (whole wallet, n-times payout at 1 in 1.2n,
  flat 16.67% edge), the `finally` fix in `check_cogs.py`, and the pinned count
  at 93. Deployed tree: 46 `.py` files, compile + lint clean, 9/9 checks.

- stamp `20261023-035552` — `coinflip.py` (new), `checks/check_coinflip.py`
  (new), `config.py`, `cogs/individual.py`, `cogs/ledger.py`,
  `checks/run_all.py`, `checks/check_cogs.py`, `README.md`, `AGENTS.md`,
  `BUG-FIXES.md`, `cogs/README.md`. Carries `/individual coinflip` (49% win,
  2x payout, 2% net sink) and the command-count assertion in `check_cogs.py`.
  Deployed tree: 44 `.py` files, compile + lint clean, 8/8 checks.

- stamp `20261023-022154` — `payouts.py`, `config.py`, `database.py`,
  `cogs/payout.py`, `cogs/individual.py`, `README.md`, `AGENTS.md`,
  `BUG-FIXES.md`. Carries individual transfers (`/individual transfer`,
  `/individual pay`), the 5% individual net worth tax, and the per-task
  `Database.transaction()` fix.
- stamp `20261023-022535` — the new `checks/` suite plus refreshed docs.

Rollback is copying the `.bak-<stamp>` files back over the originals and
restarting. Delete `/checks` and its `.bak` files if you need a clean revert.

**Restart still pending.** SFTP gives no shell, so the process must be restarted
from the hosting panel before any of this takes effect.

## Nation and kingdom accounts

- Creation has two paths that must behave the same: `/nation request` /
  `/kingdom request` (petition, admin approves with the card buttons) and the
  admin `/nation add` / `/kingdom add` shortcut. Both call
  `creation.found_account()`, which creates the row, credits the creation bonus
  and writes the `nation_create` / `kingdom_create` ledger row inside the
  caller's transaction. Do not add a third path that credits a bonus.
- `/nation delete` and `/kingdom delete` call `Database.delete_account()`, which
  re-reads the row and writes a negative `nation_delete` / `kingdom_delete`
  ledger row for the remaining balance (`balance_after = 0`) in the same
  transaction. Never call `delete_nation()` / `delete_kingdom()` directly from a
  command: that is the silent-burn path the check now guards against.
- `/nation rename` and `/kingdom rename` call `Database.rename_account()`. It
  updates the row and the `nation_name` snapshot on open contracts only
  (`negotiating`/`approved`). Ids, balances, leaders, `union_members` and
  `union_commits` are untouched; `transactions.account_name` is history and is
  never rewritten. The cog writes a `nation_rename` / `kingdom_rename` ledger
  row with the old name in the note.
- Before creating or renaming, check the target table with `get_nation` /
  `get_kingdom` (both `COLLATE NOCASE`) and scan
  `pending_creations_for_guild()`. The SQL `UNIQUE` constraint is
  case-sensitive, so it is not a substitute for the lookup; see
  `BUG-FIXES.md` known issue 7.

## Contract plumbing worth knowing

- The Union pays the contractor out of `union_account`; the party is never
  charged. `approve` records the split and the claim account, and pays nothing,
  but it refuses unless the treasury holds the whole budget. Credits only move
  in `Contract._pay_claim()`, which flips `claim_contract_tranche()`,
  `debit_union()`s the treasury, adds the balance and writes both ledger rows
  inside one transaction. A short treasury rolls the claim back, flag included.
- A new contract budget must be positive and at most **100,000,000 credits**:
  `config.CONTRACT_MIN_CREDITS == 1` (no practical minimum) and
  `config.CONTRACT_MAX_CREDITS == 100_000_000`. `budget_allowed()` checks the
  minimum as well as the leverage ladder, and `request`/`approve` reject budgets
  outside that range. Lower-budget contracts created before this rule are
  grandfathered for claim/release only.
- Union credits are created only by `/faction fund` (admin),
  `/faction donate`, member dues or an approved
  `/faction commitment`. Do not add another inflow; the
  whole point is no money printing.
- Per-cycle contract quota lives in `config.CONTRACTS_PER_CYCLE` (nation 1,
  kingdom 3). `count_contracts_for_cycle()` ignores cancelled/rejected rows.
  `create_contract(..., quota_limit=...)` re-checks the count **inside the insert
  transaction**; `/faction contract faction:union` (through
  `Contract.request_contract`) passes the limit and deletes the Discord thread
  it opened if it loses the race. Never rely on the command's pre-check.
- `contracts.case_id` is globally `UNIQUE`. `next_case_number()` allocates
  **per faction prefix** from `case_counters_prefix` (`UN` or `CR`, per tag)
  inside one transaction, so two guilds with the same nation tag cannot both
  generate `UN-AURUM-001`, and Crown cases start at `CR-<TAG>-001` even when
  Union cases already exist for that tag. Union allocations also raise and
  update the legacy `case_counters` / `case_counters_global` floors, so the
  pre-prefix numbering continues where it left off; the shared global row is a
  UN mirror, never the source of a CR number.
- `Contract._approve_checked()` does the final approval in one transaction: it
  re-reads the contract, owner balance, Union treasury, claim account and budget,
  then calls `approve_contract()`. The pre-read in the command is only for early
  feedback, never the decision.
- Reopen only accepts `Contract.REOPENABLE_STATUSES` (`approved`, `closed`,
  `completed`). `rejected` and `cancelled` are final: reopening one would move it
  back to `negotiating` and make the forfeited tranche claimable again.
  `checks/check_contracts.py` pins the tuple.
- Union membership is per guild in `union_members`. Dues run inside the payout
  transaction (`Payout._charge_union_dues`), so the payout and its tax can never
  diverge. Dues are a percentage of the member's **whole balance** (1.75%
  nation, 2% kingdom, stored as basis points in `config.UNION_TAX_BPS` and
  calculated by `payouts.union_dues()`) taken after the payout credits, not a
  percentage of the payout. Dues exceeding the payout is intentional.
  The member shop discount is per line, only for
  `config.UNION_MEMBER_DISCOUNT_CATEGORIES`, and replaces the treasury discount
  on those lines rather than stacking with it. Gilbert gets a membership line in
  `contract_context(union_member=...)` and is deliberately warmer to members.
  He also receives `union_dues_paid`, `union_donated` and `union_committed`
  from `Database.union_contributions()`, which sums the account's `union_tax`,
  `union_donate` and `union_commit` rows; contract grants from the Union are
  not contributions.
- `/faction commitment` is an admin-approved standing
  commitment in `union_commits` (`pending` → `active`/`denied`, or
  `cancelled`); `kind:uncommit` cancels a pending or active row through the same
  guarded update. Approval happens through `UnionCommitApprovalView` in the
  channel set by `/faction setchannel`. The
  approving transaction also donates the balance the account already holds, so
  an approved commitment never leaves money behind. While
  active, `Database.commit_balance_to_union()` must run **inside the same
  transaction as every credit** to the account: payout, contract claim,
  `/bank deposit`, transfer destination and approved draw. It debits the whole
  balance, credits the Union and writes both `union_commit` ledger rows. It
  asserts `_in_tx` for that reason. `commit_balance_to_union` is a no-op when
  the account is not active, so callers can invoke it unconditionally after a
  real credit. `/bank setbalance` is an admin correction and deliberately does
  not trigger it. `settings.union_commit_channel_id` must be set with
  `/faction setchannel`; while it is `NULL`,
  `/faction commitment` refuses.
- `contracts.nation_id` is a legacy column: it is `0` for kingdom contracts.
  Always go through `_owner_type()` / `_owner_id()` / `_owner_row()`.
- Rows with `approved_at` and a `redeem_key` predate the grant model. They were
  charged in full, so `_claimable_tranches()` treats the whole budget as owed.
  Do not add new key-based payouts: `api.py` answers `410 no_in_game_currency`.
- `upfront_claimed` / `final_claimed` guard Discord claims. The old
  `upfront_redeemed` / `final_redeemed` flags still matter for legacy rows.
- `contract_cycle` only advances on a natural payout cycle. Keep the advance
  inside the payout transaction so a failed payout cannot burn a cycle.

## Individual wallets and `/daily`

- `individuals` holds one row per `(guild_id, discord_id)`, created on first
  use. `id` is the ledger `account_id` (`account_type = 'individual'`); it is
  global and never reused, so a deleted member's history cannot attach to a new
  row.
- Balances are **not** created with `set_balance` + `log_tx` in the money
  commands. Use `credit_individual()`, which trims the credit to the remaining
  room under `config.INDIVIDUAL_MAX_CREDITS` and returns `(added, balance)`.
  The caller logs the **added** amount, never the requested amount, so the
  ledger stays honest when the cap bites. `debit_individual()` is the only spend
  path and guards the balance in the UPDATE.
- `/daily` state lives on the same row (`daily_streak`, `daily_bp`,
  `last_daily_at`). `daily.evaluate()` is pure and owns the rules: first claim
  100%, +10 points per consecutive claim (cap 400%), -20 points per missed day
  (floor 60%). A claim made 24–48 hours after the previous one is consecutive;
  every full window after that is one missed day.
- A claim flips the state with `claim_individual_daily()`, guarded on the
  `last_daily_at` value that was read, then credits and logs inside the same
  transaction. The guard is why a double click or two devices cannot both be
  paid. Do not add a second daily-paying path without the same guard.
- `/individual delete` removes the wallet **and its `transactions` rows**. This
  is the one deliberate exception to the append-only ledger: the member asked
  for erasure. Do not reuse it for anything else.
- Individuals are outside the shop and contract economies: contracts still pay
  only into nation/kingdom treasuries and the shop still debits them. That is
  why `discount_percent("individual") == 0` is a rule, not just a default.
- Individuals can **send and receive** credits. `/transfer` (and the extended
  `/bank transfer`) moves credits in every direction between individuals,
  nations and kingdoms; `/give` is the treasury-to-member shortcut (a nation or
  kingdom leader pays a member wallet from the treasury); `/individual transfer`
  and `/individual pay` remain the member-facing shortcuts. Every path debits
  the source, credits the destination and writes both ledger rows in one
  transaction. A treasury destination runs `commit_balance_to_union()`, exactly
  like a deposit.
- A transfer **into** an individual wallet uses `credit_individual_strict()`,
  which raises `CapExceeded(balance, amount, room)` instead of trimming. Never
  use the trimming `credit_individual()` for a transfer: the sender is already
  debited, so a trim would delete their credits.
- Every **payout run** (Cash and Default) charges each wallet
  `payouts.individual_tax(balance)` = 5% of the whole balance
  (`config.INDIVIDUAL_TAX_BPS`), floored to whole credits, through
  `Database.tax_individual()`. `_run_cycle()` reads the wallet list and each
  wallet **inside** the payout transaction, so a concurrent catch/daily is
  reflected and a deleted/recreated wallet is taxed under its live id.
  `config.INDIVIDUAL_TAX_ENABLED` pauses it; `config.INDIVIDUAL_TAX_DESTINATION`
  is `"union"` (default) or `"burn"`.
- **Nations and kingdoms are never taxed by the individual rule and have no
  balance cap.** Their only charge is Workers' Union dues, and only for members.
  If you add an individual-style cap check, scope it to `individuals`.
- **Every gamble settles against the Casino, never against thin air.** A wager
  goes through `casino.begin_wager()` (stake into the house) and
  `casino.finish_wager()` (payout out of it), both inside one
  `db.transaction()`, and both sides get a ledger row with
  `account_type='casino'`. The sum of all player wallets plus the Casino balance
  must be **identical** before and after any number of bets.
  `checks/check_casino.py` asserts this over 200 consecutive wagers. Paying a win
  with `credit_individual()` alone is the bug this design exists to prevent.
- The Casino has exactly two taps: `/casino fund` and `/casino withdraw`, both
  admin-only, logged as `casino_fund` / `casino_withdraw`. `Database.debit_casino()`
  puts the `balance >= amount` guard in the `UPDATE` so the house cannot go
  negative even with concurrent payouts, and `casino.cover_problem()` refuses a
  wager whose worst-case payout the house cannot pay before any money moves.
- **Fishing is a transfer from the Fishing Guild, never a faucet.** `/fish`
  (`cogs/fishing.py`) credits the caller's individual wallet and debits
  `fishing_guild_account` in one `db.transaction()`, with a `fishing_catch` row
  on each side (`account_type='fishing_guild'` on the house row). The guild has
  exactly two taps, `/fishingguild fund` and `/fishingguild withdraw`, both
  admin-only and logged; `Database.debit_fishing_guild()` keeps the
  `balance >= amount` guard in the UPDATE. `fishing.catch_problem()` refuses a
  catch before any money moves when the guild is short or the payout would cross
  `config.INDIVIDUAL_MAX_CREDITS`; the payout is an inclusive random
  `config.FISHING_MIN_CREDITS..FISHING_MAX_CREDITS` (20–100). The catch re-reads
  the wallet and guild under the transaction lock, so a deleted wallet is never
  recreated and a stale guild balance cannot be paid.
- `/individual coinflip` has a fixed **50%** win rate. `coinflip.resolve()`
  decides the winner from the roll alone (`roll < config.COINFLIP_WIN_BP`); the
  player's call only picks the displayed face, so it can never tilt the odds.
  `coinflip.house_edge_bp()` is `10_000 - WIN_BP * PAYOUT_MULTIPLIER`, currently
  **0**: a fair pass-through, with the Casino funding wins from its own balance.
  Raising the win rate above 5,000 makes the game a faucet; lowering it creates a
  sink. The 50/50 rate is an explicit owner decision; do not "restore" 49/51
  without a new instruction.
- **A deleted wallet is never resurrected.** `debit_individual()` and
  `credit_individual_strict()` use `find_individual()` and raise
  `AccountMissing` instead of creating a row. `has_open_hand()` /
  `delete_individual_checked()` refuse `/individual delete` while a blackjack or
  poker hand is open, and settlement re-reads the wallet under its transaction.
- Coinflip screens the stake with `coinflip.stake_problem()` **before** rolling:
  minimum, maximum, overdraw and remaining room under the 100M cap. A win credits
  the wallet, so a cap-breaking stake has to be refused up front rather than
  rolling back a legitimate win. Refusals must not roll the RNG or touch money.
- The stake debit and the winnings credit share one `db.transaction()`. Each
  bet writes **two** player rows (the stake held, then the payout) on a win and
  **one** on a loss, plus the mirroring house rows, so the wallet and the Casino
  both reconcile with their ledger.
- `/allin` stakes the **whole** individual wallet and pays `n` times it. Its
  chance is `1 in (1.1 x n)`, which makes the expected return `1/1.1 = 10/11` for
  every `n`: a flat **9.09%** house edge. Do not "improve" the payout or the
  hardness for one number alone - both scale together, and a mismatch turns the
  game into a faucet. `allin.house_edge()` must stay positive.
- `/allin` refuses `n = 1`: a 1x payout returns the stake with no profit, so it
  is a strictly dominated bet. The slash-command range starts at
  `config.ALLIN_MIN_NUMBER` (2) for the same reason.
- The all-in cap check is on the **payout**, not the stake (`balance - stake +
  n * stake`), because a win is what can breach the 100M limit. It runs before
  the draw, via `allin.stake_problem()`, so a refusal never rolls or moves money.
- `/allin` asks for confirmation because a miss empties the wallet, and
  `settle_allin()` re-reads the balance when the button is pressed: a stake the
  wallet can no longer cover is refused, never silently resized. The confirmation
  is posted in the channel; only the button is limited to the caller.
- **Blackjack holds the stake while the player decides**, so two things must
  stay true. The hand is recorded in `blackjack_hands` at the deal and
  `bot._refund_abandoned_blackjack_hands()` refunds any hand still open at
  startup - without that row a restart keeps the stake. And
  `Database.close_blackjack_hand()` is a guarded one-shot flip that
  `settle_hand()` checks **inside** the payout transaction, so a timeout racing
  a button press pays once.
- The house must cover `blackjack.max_gross(stake)` before a deal (3:2 on a
  natural), not merely 2x: a natural is paid on the deal and would otherwise
  overdraw the Casino.

## Payout modes and unified transfers

`cogs/payout.py` has three admin commands over one engine,
`Payout._run_cycle(guild_id, natural=...)`:

- **`/payout cash`** (`natural=False`) pays nation/kingdom allowances, Union
  dues/commitments and the individual net-worth tax. It must **not** call
  `advance_contract_cycle()` or `set_cycle_times()`: the Cash Cycle is cash-only
  and cannot reset the natural schedule or burn a contract cycle.
- **`/payout default`** (`natural=True`, also the 14-day job) runs the full
  natural cycle: the same payments plus `advance_contract_cycle(natural=True)`
  and the next `last_cycle_at`/`next_cycle_at`. It refuses when the next cycle is
  not due unless the admin passes `force: True`.
- **`/payout trigger days:N`** only queues/schedules the natural cycle
  (`next_cycle_at = now` for `days:0`, or `now + N days`); it pays nothing. The
  10-minute `cycle_task` executes it through the default path.

`_run_cycle` serialises runs with `self._cycle_lock` before it reads anything.
The database transaction makes the money movement atomic, but without the lock
the 10-minute task and an admin command could both act on the same starting
snapshot and pay stale allowances twice. Do not remove the wrapper or call
`_run_cycle_locked` directly from a command. Both the `/payout default` command
and the 10-minute task pass `require_due=True`, which repeats the natural-cycle
due-check **after** the lock is held; without it two simultaneous Default runs
both pay and both advance `contract_cycle` (the command's pre-check is only
feedback). `force:true` bypasses the guard deliberately; the checks call
`_run_cycle` directly without it, which is why it defaults to off.

Both cycle modes charge the 5% individual net-worth tax through
`payouts.individual_tax()` and `Database.tax_individual()`; nations/kingdoms are
never taxed. The tax loop re-reads every wallet inside the payout transaction,
so a deleted/recreated wallet is taxed under its live id, never a dead one.

`account_transfers.py` is the **only** credit-transfer implementation. It is
called by `/transfer` and `/give` (`cogs/transfers.py`), `/bank transfer`
(`cogs/bank.py`), `/individual transfer` and `/individual pay`
(`cogs/individual.py`). `/give` is the treasury-to-member shortcut: it accepts
only a nation/kingdom source, resolves the member wallet on the destination and
writes a `treasury_give` row on each side. It supports every pair between
`nation`, `kingdom` and `individual`:

- `transfer_funds()` opens one `db.transaction()`, re-reads the source and
  destination (a stale autocomplete snapshot never decides balances or
  leadership), refuses a cross-guild account, and refuses same-account.
- The actor must lead the source treasury or own the source wallet; an
  administrator may move any account. Receiving needs no permission.
- The source is debited with the guard in the `UPDATE`
  (`Database.debit` / `debit_individual`). An individual destination is credited
  with `credit_individual_strict`, so a cap-breaking transfer raises
  `CapExceeded` and rolls the source back instead of trimming credits.
- Both ledger rows are written in that transaction. A treasury destination also
  runs `commit_balance_to_union()` in the same transaction.
- An explicit inbound transfer may create a missing individual destination
  wallet on first receipt; spending from a missing wallet is refused, and
  settlement paths still never resurrect a wallet that vanished mid-hand.
- All four commands call this core. `/individual transfer` and
  `/individual pay` only build their refs and keep their own reply wording; do
  not add a second transfer SQL path.

## Factions and The Crown (`factions.py`, `cogs/factions.py`)

- **One allegiance per nation/kingdom.** `faction_members` has
  `PRIMARY KEY (guild_id, account_type, account_id)` and `faction` is `union` or
  `crown`. All joins/switches go through `factions.join_faction()`; never insert
  a faction row from a command. Switching cancels the old side's active Union
  commitment and Crown contracts in the same transaction. `union_members` is a
  legacy mirror: `migrate()` seeds `faction_members` from it at startup, the
  Union DB methods keep it in sync, and `is_union_member()` reads
  `faction_members`.
- **Crown membership has no soldier gate.** `CROWN_SOLDIER_MINIMUM` is 0;
  `crown_eligible()` is always true, and migration reactivates old
  `ineligible` rows. Contracts are not scored or rolled at all now: soldiers are
  shown in the representative's briefing as standing, and a kingdom sums its
  member nations for that figure.
- **Crown tax is 100% of the regular-soldier payout line**, not the total
  payout. `factions.apply_crown_tax()` is called from `Payout._run_cycle()` inside
  the payout transaction, after the payout credit and before Union dues; it
  debits the treasury, credits `crown_account`, and writes both `crown_tax`
  rows. The 30-day grace is `joined_at + CROWN_TAX_GRACE_DAYS`. There is no
  unpaid-dues table: the cut is immediate.
- **Contracts are item-specific and capacity-limited.** `CROWN_CONTRACTS_PER_ACCOUNT`
  is `{nation: 1, kingdom: 2}` for `status='active'` rows. The value cap uses
  the contracting account's own treasury: 10M below 15M, 15M through 50M, a
  third above 50M. The Crown must hold the full value plus every active
  contract's value before signing. `crown_contracts.value` and
  `demand_bp` are locked at signing.
- **Demand is per account + item.** `crown_sales.demand_bp` starts at 10,000 and
  falls 500 bp (5 points) in each natural cycle in which that account fulfils
  that item, floored at 4,000 (40%). `demand_after_cycle()` enforces at most one
  decay per cycle. A signed contract is unaffected; the next contract uses the
  new demand. Fulfilment updates `crown_sales` and sets
  `supplier_until_cycle = cycle + CROWN_SUPPLIER_CYCLES`.
- **Delivery is oldest-contract-first and pays only on full quantity.** A
  partial delivery records progress and pays nothing; excess is a pure item
  donation. `deliver_crown_items()` checks every completion's total value
  against the Crown balance before writing anything, then updates, pays, logs
  `crown_supplier` both sides, records the sale and the donation row in one
  transaction. A short treasury raises `CrownTreasuryShort` and rolls it all
  back.
- **Contracts expire at the natural cycle deadline.** `CROWN_CONTRACT_CYCLES`
  (3) is added to the cycle at signing; the payout loop expires overdue rows
  before a new slot can be claimed. `/faction cancel` cancels an active
  Crown contract without payment; delivered units are forfeited.
- **The Crown treasury is a house account.** `crown_account` receives tax,
  credit donations and supplier payments only through the logged paths.
  `/faction fund` and `/faction withdraw` are admin-only; withdraw
  refuses to leave less than `db.crown_reserved()` (the sum of active contract
  values). Never add a direct balance update.
- **`/faction` is a single command group with one subcommand per action.**
  This is the Discord-native way to show only the relevant options after the
  action is chosen; a slash command cannot change its options dynamically.
  - `/faction contract` takes `faction`, `account`, a free-text `proposal` and
    `budget`.
    It never rolls dice and never checks faction membership. Both factions call
    `Contract.request_contract()` (leader or Union Admin; proposal + budget;
    per-faction quota): the Union opens a Gilbert thread, The Crown opens an
    Albert thread. `contracts.faction` records the payer and picks
    `union_account` or `crown_account` for approval and claims. `item` and
    `quantity` belong only to legacy Crown supplier rows and must not return to
    the command.
  - Both briefings preload the account's live numbers
    (`gilbert.prompt.contract_context`, `albert_prompt.contract_context`). Do
    not make the model ask for a balance, soldier count, quota or treasury the
    bot already holds.
  - `/faction commitment` calls `Union.request_commit()`; `/faction uncommit`
    calls `Union.cancel_commit()`.
  - The other subcommands route join/leave, donate/deliver, status, contracts,
    cancel, fund, withdraw, members and setchannel. Legacy supplier deliveries
    still require Crown membership; proposal contracts do not.
- **Audience is removed.** There is no `/faction audience`; contract requests
  are the only representative-facing action. `albert_memory` and the audience
  prompt helpers remain for old data but are not reachable.
- Config knobs live in `config.py` under "Factions — The Crown": 30-day grace,
  100% tax, capacity, value tiers, 3-cycle term, 3-cycle supplier status, 5%
  decay and 40% floor. `CROWN_SOLDIER_MINIMUM` is 0 (no gate).

## Poker engine (`poker.py`)

`poker.py` is a pure engine — no Discord, no database, no imports from the bot.
`cogs/games.py` loads it for `/poker`; the engine itself is proven by
`checks/check_poker.py` and the end-to-end command by `checks/check_casino.py`.

- **Hand start is three calls, in this order:** `_deal()`, `_post_blinds()`,
  `_start_preflop()`. `_begin_street()` is the *street reset* — it sets
  `current_bet = 0` and zeroes every seat's `committed` — so it must never open
  preflop. Doing that used to leave the blinds in the pot as dead money that
  nobody had to match; see `BUG-FIXES.md`.
- **`committed` is the current betting round; `total` is the whole hand.**
  `pot` sums `total`, so a street reset never loses chips; `to_call()` and
  `min_raise_to()` read `committed`. Reset one without the other and the betting
  logic lies while the pot looks right.
- **Preflop action starts left of the big blind,** not left of the button, and
  the big blind keeps its option (`check` is legal once everyone has limped).
  Postflop action starts left of the button. `_start_preflop()` vs
  `_begin_street(first=None)` is where that difference lives.
- The human is **seat 0** and the button starts on seat 0, so the player acts
  second preflop and first postflop.
- **Suit is the last character of a card string** (`"10♠"` -> `♠`). `card_value()`
  and the flush test both rely on that; a new card format has to keep it.
- **`distribute()` is the only payout path.** It returns the uncalled portion of
  a unique highest contribution to that contributor (even if folded), then builds
  one pot per remaining contribution level, skips any seat with no rank in the
  map (folded), and gives the odd chip to the first winner left of the button. Do
  not pay a seat directly from `total`.
- **A short all-in does not reopen raising.** `raise_locked` tracks seats that
  already acted on the street; an all-in below `min_raise` adds them back to
  `to_act` so they can call, but `legal_actions()` withholds raise/all-in until a
  full raise or the next street clears the lock. An all-in raise still adds every
  other seat to `to_act`; do not reintroduce the old `if seat.all_in` shortcut.
- The table holds `4 x stake`; `max_gross(stake)` is the house's worst case and
  is what the Casino must be able to cover before a table opens — the same rule
  as `blackjack.max_gross()`.
- Holds confirmed by the check, keep them true: chips are conserved over 400
  seeded hands, every hand terminates, no stack goes negative, and the same seed
  replays identically.
- **Bot names are generated outside the engine.** `poker_names.table_names()`
  samples three Minecraft-themed names from an AI pool refreshed through
  `GilbertAI` (the same `AI_API_KEY` as Gilbert) in the background, with
  `config.POKER_BOT_NAME_POOL` as the offline fallback. `Casino.__init__` starts
  the refresh; `/poker` only samples synchronously. Never await the AI inside
  `/poker` or import it into `poker.py` — the engine stays pure and a slow
  request must not expire an interaction.
- `/poker` stakes through `casino.begin_wager()` / `casino.finish_wager()`,
  screens the 200 credit minimum and the 100M cap before dealing, and records
  the held buy-in in `poker_hands`. Settlement passes
  `payout_override=final_stack` so chips left after a losing hand still return;
  startup refunds any open row after a restart. Preserve those rules when
  changing the command.

## Money commands and amount parsing

`/transfer`, `/give`, `/bank transfer`, `/bank draw`, `/bank withdraw`,
`/individual transfer` and `/individual pay` take `amount` as a string and run it
through `utils.parse_amount(value, available)`: plain integers, `1,500` style, or
`all` / `half` / `quarter` of the paying balance. Fractions round down. `/give`
resolves the share against the treasury balance because the treasury pays. Any
new command that takes an amount should use the same helper so the wording stays
consistent.

## Money invariants

- Never write `balance = balance - x` without a `balance >= x` guard; use
  `Database.debit()` for nations/kingdoms and `Database.debit_union()` for the
  Union treasury.
- Every balance change and its ledger row must share one `db.transaction()`.
- `Database.transaction()` is re-entrant (depth counter) and serialised by an
  asyncio lock, so nested use **in the same task** joins the outer transaction.
  The depth counter is a `ContextVar`, not an instance attribute: a process-wide
  counter let one task's block join another task's transaction, so its work was
  committed or rolled back with a stranger's. Do not go back to a plain integer.
  Plain `execute()`/`fetchone()`/`fetchall()` calls take the same lock outside a
  transaction; without it an autocommit `conn.commit()` could commit another
  task's open transaction before it rolled back.
- Anything that flips a one-shot status (contract tranche, pending draw,
  petition decision, contract approval/close/release/reject, kit cooldown) must
  do it with a guarded UPDATE and branch on the returned row count. The guarded
  helpers are `claim_pending_draw()`, `claim_pending_creation()`,
  `approve_contract()`, `transition_contract()`, `claim_kit_cooldown()` and
  `claim_contract_tranche()`; the plain setters (`set_draw_status()`,
  `set_creation_status()`, `set_contract_status()`, `set_kit_cooldown()`) are not
  safe for one-shot decisions. A contract claim must re-read the contract inside
  its transaction and refuse a status of `rejected`/`cancelled`.
- Individual credits go through `credit_individual()` so the 100M cap is applied
  in one place. Never `UPDATE individuals SET balance = balance + ?` directly.
- A draw approval re-reads the request, kingdom, nation, membership
  (`nation["kingdom_id"] == kingdom["id"]`), current leader and treasury balance
  **inside** the claim transaction. A nation removed from the kingdom after
  asking must be marked `invalid`, never paid. Exceptions use
  `claim_pending_draw(..., "failed")`, never the plain setter.
- `/bank setbalance` re-reads the target balance inside its transaction and logs
  `new - fresh_old`; using the pre-read row makes the ledger delta wrong after a
  concurrent deposit.
- The check runner requires a recognised summary (`FAILURES:`, `VERDICT:` or
  `ALL OK`) as well as exit 0. A bare exit or an `ERROR:` line must fail; every
  check script exits non-zero on failures or an uncaught exception.
- If a wallet is at the cap, the daily claim is still consumed and writes no
  ledger row (nothing moved); that is intentional and surfaced in the reply.

## Open product decision (ask before implementing)

- Individuals **can send money out** (`/individual transfer`, `/individual pay`)
  but still **cannot spend** in the Royal Shop or hold contracts: the shop and
  contracts move nation/kingdom treasuries only. The owner has not decided
  whether individuals should buy from the shop at full price.
  `shop/pricing.discount_percent()` already returns 0 for `"individual"`, so
  enabling it is a checkout change, not a pricing change. Do not enable it
  silently: it changes documented economy behaviour in README section 7.
- **Resolved:** treasuries can pay an individual wallet through `/transfer` (and
  `/bank transfer`). `/individual pay` is still the one-way member → treasury
  shortcut. Do not route shop purchases or contract payouts through the transfer
  path: individuals remain outside those economies.

## Known gaps (not implemented)

- `nations` / `kingdoms` use a case-sensitive `UNIQUE (guild_id, name)` while
  every lookup is `COLLATE NOCASE`. The commands check before writing, so a
  duplicate that differs only by case needs two simultaneous requests; a
  `UNIQUE ... COLLATE NOCASE` index would close the window but needs a
  migration and a duplicate check first.
- `/kingdom addnation` checks `MAX_NATIONS_PER_KINGDOM` (now 6) before it
  updates, with no transaction around the count and the write. Two simultaneous
  admin calls can leave seven members; a guarded count-and-update would close
  it.
- `/casino withdraw` does not reserve the worst-case payout of open blackjack or
  poker hands. Draining the house mid-hand can make settlement and the startup
  refund fail; a withdraw-time reserve check would close it. The deal-time cover
  check itself is correct.
- Forge mod (`forge-mod/` is a README only).
- Contracts approved before `/contract setboard` was configured have no board
  message; they can still be claimed with `/contract claim`.
- The `checks/` suite **is** the automated test suite: 18 standalone scripts run
  by `python3 checks/run_all.py`. They cover ledger rollback, payout cycles,
  transfer routing in all nine directions, leverage boundaries, contract
  claim/quota/approval races, draws, Union guards, membership, dues, pricing,
  commitments, the `/union` surface, faction switching, Crown tax and supplier
  contracts, the individual cap, `/daily`, data deletion, leaderboards, the kit
  cooldown, coinflip/all-in/blackjack/poker/fishing and the cog-load smoke test.
  `BUG-FIXES.md` lists every check and what it proves.
