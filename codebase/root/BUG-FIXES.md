# Bug fixes — catalogue

A running record of every defect found in this project, area by area. Each entry
says what went wrong, why it mattered, and what the fix was. Newest first.

Use this file when you touch an area: the "still open" notes at the end tell you
what is known and deliberate, so you do not "fix" a design decision or miss a
real one.

Format per entry:

```
### <short title>
Symptom: what a user or admin would see.
Cause:   the actual defect.
Impact:  severity and blast radius.
Fix:     the change, with the symbol names to grep for.
Test:    the check that proves it (see "Verification" at the end).
```

---

## Crown case IDs shared the Union counter — 2026-10-29

### The first Crown case for a tag would be CR-TAG-004, not CR-TAG-001

**Symptom.** The Crown-funded proposal model uses `CR-<TAG>-<NNN>` case IDs, but
the first Crown case for a tag that already held three Union cases came out as
`CR-<TAG>-004`. The prefix was right; the sequence was not faction-local, so a
"new Crown starter ID" could not be promised.

**Cause.** `Database.next_case_number()` kept one `case_counters_global` row per
nation tag and used it as the floor for both prefixes. `UN` and `CR` therefore
interleaved numbers instead of each starting at 001.

**Impact.** Cosmetic numbering, but it made Crown case IDs look like a
continuation of the Union's series and undermined the documented CR/UN split.

**Fix.** Numbers are reserved from `case_counters_prefix`, keyed by
`(prefix, nation_tag)`, inside one transaction. Union allocations still read and
update the legacy `case_counters` / `case_counters_global` floors, so existing
`UN-...-003` numbering continues at 004; Crown allocations start at 001 and
never touch the shared row. The table is created by the schema on startup, so
no column migration is needed.

**Test.** `checks/check_contracts.py` asserts that UN and CR both start at 001,
each prefix advances independently, UN continues at 003 while CR starts at 001
on the same tag, and concurrent UN/CR reservations do not share a counter.

---

> **Superseded 2026-10-29:** the `project` option described below was renamed
> to a single free-text `proposal`, and the Crown flow now buys supplies
> collected by an admin. See README section 12.

## Contract requests were gated by a dice roll and faction membership — 2026-10-28

### A contract could be refused by RNG, and Crown requests needed allegiance

**Symptom.** `/faction contract` could refuse a request even though the account
met every written rule, because a hidden 1–100 roll had to land under a computed
"chance". Crown requests additionally required Crown membership and two extra
fields (`item`, `quantity`), and the representative only saw the account's
numbers after a roll had already passed.

**Cause.** The 2026-10-28 contract-judgment update replaced the direct contract
request with a scored audience. `factions.judge_contract()` rolled
`random.randint(1, 100)` against `contract_chance()`, `cogs/factions.py` refused
the request when the roll lost, and the Crown path required a `faction_members`
row plus an item/quantity supplier contract.

**Impact.** A compliant request could be denied with no rule broken, membership
became a gate for The Crown, and the Crown supplier model forced every request
into a catalog item instead of a project. The roll also meant the AI negotiated
only the requests that survived the dice.

**Fix.**
- Removed `judge_contract()`, `contract_chance()` and `ContractJudgement`; no
  `random` import remains in `factions.py`.
- `/faction contract` is now `faction`, `account`, `project`, `budget` only.
  Both paths call `Contract.request_contract()`, which opens that faction's
  representative directly: Gilbert for the Union, Albert for The Crown.
- **Membership is never required.** The Crown gate is gone from the request.
  `gilbert.prompt` rule 9 and `albert_prompt`'s hard rules state this in the
  model prompt too.
- `contracts.faction` (`union`/`crown`) records the payer. Crown cases get
  `CR-<TAG>-<NNN>` IDs, a separate quota and `crown_account` tranches; approval
  checks the unreserved Crown balance.
- Both briefings are built before the first reply and carry the live numbers
  (balance, soldiers, treasury/reserved, quota, contributions or supply history,
  tax status). `gilbert.prompt.contract_context()` gained `soldiers`;
  `albert_prompt.contract_context()` is new.
- Both personas were softened: Gilbert is explicitly kind/encouraging and Albert
  warm, each told to thank the petitioner and look for a way to say yes without
  breaking a rule.
- Legacy Crown supplier rows still deliver and pay (`/faction deliver`,
  `create_crown_contract`, demand decay); only new supplier requests are gone.

**Test.** `checks/check_contracts.py` drives a real Crown request (CR case ID,
`crown-contract-*` thread, `faction='crown'`, Albert intro), the Crown-funded
25% claim, the per-faction quota, a short Crown treasury refusal, and asserts
both system prompts contain the case ID, budget, treasury, soldiers and
membership rule. `checks/check_factions.py` drives a non-member Crown request
through `/faction contract` and pins the absence of the RNG helpers;
`checks/check_cogs.py` pins the four options; `checks/check_gilbert.py` pins the
tone and membership text. Suite **18/18**.

---

## `/faction` hub bypassed every admin check it forwarded to — 2026-10-28

### A non-admin could mint faction credits and drain the Crown

**Symptom.** Any guild member could run `/faction fund faction:crown amount:1000000`
and see `Crown Funded`, or `/faction fund faction:union` and see
`Union Funded`, with no administrator permission. `/faction withdraw` let the
same member burn the Crown's treasury, and `/faction setchannel` let them move
the Union commitment-approval channel.

**Cause.** `admin_only()` is an `app_commands.check`, i.e. a predicate attached
to the *command object* that Discord's dispatcher runs for the command the user
typed. The `/faction` hub subcommands (`faction_fund`, `faction_withdraw`,
`faction_setchannel`) are plain wrappers that forward with
`other_command.callback(...)`, which invokes the raw coroutine and skips the
registered checks. `crown_fund`, `crown_withdraw` and `Union.fund` each carried
`@admin_only()`, but the check never ran on the delegated path, so the hub was
an unauthenticated mint for both faction treasuries. Crown credits pay supplier
contracts and Union credits fund building contracts, so the mint converts
directly into player balances.

**Fix.** `Factions._require_admin()` gates the delegate at the hub
(`is_admin()` plus an `Admins Only` refusal), and the three wrappers also carry
`@admin_only()` so the registered command is gated as well. Only delegated
*admin* actions needed this: the public delegates (`join`, `leave`, `donate`,
`deliver`, `commitment`, `uncommit`, `status`, `contracts`, `cancel`, `members`)
already re-check leadership inside the implementation.

**Test.** `checks/check_factions.py` drives the hub as a non-admin and asserts
that both treasuries and the commitment channel are unchanged, plus that an
administrator can still fund the Crown.

---

### A failed Union opening still reported "the record passed"

**Symptom.** If the Union contract desk refused a request after the roll passed
(missing channel, quota used, leverage limit, permission, or a treasury error),
the player still received a second ephemeral embed saying the record passed,
immediately after the error.

**Cause.** `Contract.request_contract()` returned `None` on every path, so
`Factions.faction_contract` could not tell an opened case from a refused one and
posted the review unconditionally.

**Fix.** `request_contract()` now returns `True` only after the thread, case and
Gilbert intro exist; `faction_contract()` sends the review only when that is
`True`. The new regression asserts both the successful review and the silent
failure.

### Crown terms errors hid the judgment

**Symptom.** A Crown request could pass the roll and then fail on the requested
budget ceiling, value cap or treasury reserve with no indication of the chance or
the record that produced the approval.

**Cause.** The `CrownTreasuryShort` / `FactionError` handlers built a fresh error
embed and dropped the `ContractJudgement`.

**Fix.** Both handlers call `_add_judgement_fields()` before replying, so the
player sees the chance, roll and factors that passed while the terms are
corrected.

### Stale command and audience scaffolding

**Symptom.** Refusal text still told admins to run `/crown fund`; comments and
the status line still described a soldier eligibility gate; delivery and
donation carried unreachable "requires at least 0 total soldiers" branches;
`/faction` carried a dead audience method and AI imports that could not be
reached.

**Cause.** The surface was renamed faster than every string and helper was
retired.

**Fix.** The refusal names `/faction fund faction:crown`, Crown status describes
soldiers as a soft odds advantage, the dead eligibility branches are gone, and
the unreachable audience method/imports are removed. `checks/check_factions.py`
now also proves a Crown budget below the locked value signs no contract.

## Faction contract judgment — 2026-10-28 (superseded by the conversational request above)

> **Historical.** This section describes the RNG "audience" model that was
> removed on 2026-10-28. `factions.contract_chance()` / `judge_contract()` no
> longer exist and `/faction contract` no longer takes `item`/`quantity`.

### Audience was the wrong entry point for contract requests

**Symptom.** Players could describe a contract budget to a representative, but
the conversation could not create anything, and Crown membership had a hard
20-soldier gate while contract requests had a 100-credit floor. There was no
single contract command that judged the account's actual record.

**Cause.** Audience and contract creation were separate; eligibility was binary
instead of record-based; the two contract subcommands had different inputs.

**Fix.** Removed `/faction audience`, `/faction union-contract` and
`/faction crown-contract`. `/faction contract` takes a required `budget` and
judges the live account through `factions.contract_chance()` /
`factions.judge_contract()`:
- Crown chance = base + soldiers + credits/items received + supply units.
- Union chance = base + membership + dues/donations/commitments + soldiers.

There is no hard soldier/donation threshold (Crown joins at any soldier count;
`CROWN_SOLDIER_MINIMUM = 0`), and the contract minimum is 1 credit. A failed
roll creates no contract. A successful Crown request must fit inside the
requested budget as a value ceiling; a successful Union request opens the
Gilbert negotiation.

**Test.** `checks/check_factions.py` checks score ordering, a 19-soldier Crown
join, low-troop Crown tax, approved and refused `/faction contract` outcomes,
and the budget/store paths. `checks/check_cogs.py` pins 111 commands.

---

## Audience open to every account — 2026-10-28

### The audience prompts implied membership was required

**Symptom.** The command did not reject nonmembers, but the representative
fallbacks/context told unaffiliated accounts to `/faction join`, which read as a
condition for speaking. There was no clear rule that members get warmer
treatment.

**Cause.** The prompts described membership and allegiance as facts, but no
explicit access rule; the Gilbert/Albert fallbacks used join language as the
next step for nonmembers.

**Fix.** Added an explicit audience rule to Albert's system prompt and Gilbert's
audience context: every account may speak, membership only changes warmth.
Fallbacks now say the audience is granted to unaffiliated/other-faction accounts
and that joining is optional. The command itself still has **no**
faction-membership check; only the account leader/admin may speak for it.
`/faction audience` also takes an optional `budget` for a contract-budget
request; it is included in the context, fallbacks, response field and stored
conversation, but remains discussion-only.

**Test.** `checks/check_factions.py` creates an unaffiliated `Neutral` nation
and drives both `/faction audience` routes with the AI disabled, asserting both
representatives answer and say allegiance/membership is not required; prompt
and context audience lines are checked too. It sends a budget to each route and
verifies the response field, context and stored memory.

---

## One faction command — 2026-10-28

### Faction actions were spread across `/join`, `/commit`, `/union` and `/crown`

**Symptom.** A Union building contract, a Crown supplier contract, a Union
commitment, a donation, an audience and every treasury/membership action each
had a different command.

**Cause.** The original Union and Crown command groups grew separately; merging
single actions into `/commit` and `/audience` still left several entry points.

**Fix.** Removed `/join`, `/commit`, `/donate`, `/audience`, the `/crown` group
and the `/union` group. `/faction` is now a single command group with one
subcommand per action, so each action shows only its own options: `join`,
`leave`, `union-contract`, `crown-contract`, `commitment`, `uncommit`,
`donate`, `deliver`, `audience`, `status`, `contracts`, `cancel`, `fund`,
`withdraw`, `members`, `setchannel`. The subcommands route to the existing
helpers (`Contract.request_contract()`, `Union.request_commit()`,
`Union.cancel_commit()` and the old panel methods). The `/contract` group
remains only for contract lifecycle administration.

**Test.** `checks/check_factions.py` drives join, Crown contract/delivery,
status, both audiences, Union contract, commitment and uncommit through the
subcommands; `checks/check_cogs.py` asserts 111 commands and that every old
faction command is absent.

---

## Faction command merge and audience repair — 2026-10-28

### `/crown audience` could leave the interaction waiting on the model

**Symptom.** The command deferred, then could sit on “thinking” for up to the
provider's full timeout when the AI was slow or unreachable. It also had no
Union counterpart, so the two representatives were reached through different
surfaces.

**Cause.** `cogs/factions.py` called `GilbertAI.complete()` directly, inheriting
the global 60-second `AI_TIMEOUT`, and `/crown audience` hardcoded Albert,
`crown_report` and `albert_memory`.

**Impact.** A slow provider made the command look broken even though the
deterministic fallback was ready; users had to remember `/union commit` versus
`/commit` and had no Gilbert audience command.

**Fix.** One top-level `/audience faction:union|crown` now routes to Gilbert
(`factions.union_report()` plus `gilbert.prompt.union_audience_context`) or
Albert (`factions.crown_report()` plus `albert_prompt`). The model call is
capped by `config.NPC_AUDIENCE_TIMEOUT_SECONDS` (20s) and any exception falls
back to the in-character reply. Conversation rows carry an `npc` column
(`albert`/`gilbert`) so the two histories cannot bleed into each other. The old
`/crown audience` subcommand is removed; the `/crown` group keeps status,
contracts, cancel, fund and withdraw.

### `/commit` was Crown-only although Union had an equivalent flow

**Symptom.** A Union member choosing `/commit` was told to use the separate
Union commitment command; the two factions' commitment paths had different
names and arguments.

**Cause.** `/commit` was implemented only as the Crown supplier contract, while
the Union whole-balance request lived in `cogs/union.py`.

**Fix.** `/commit` now takes `faction` first. `faction:union` calls the
extracted `Union.request_commit()`, the same code path the old Union command
used, so the existing admin-approval channel, pending-row checks and cleanup are
shared.
`faction:crown` keeps the supplier contract flow and still requires `item` +
`quantity`. The selected faction must match `faction_members`; an account with
no allegiance is told to `/join`.

**Test.** `checks/check_factions.py` exercises both `/audience` routes (with the
AI disabled), verifies NPC-separated memory, and verifies that a Union
`/commit` reaches the Union desk. `checks/check_cogs.py` pins `/audience` in the
117-command surface.

---

## Live audit and bug pass — 2026-10-27 (uploaded with `20261027-2325`)

**Status.** Found during a read-only check of the live tree and database after
the `20261027-221143` deploy. All five are fixed, covered by checks, and uploaded
with the faction update in stamp `20261027-2325`. The running process needs the
hosting-panel restart before the fixes are live.

### Two simultaneous Default Cycles paid a full cycle twice

**Symptom.** Two administrators clicking `/payout default` at the same moment,
or an admin click racing the 10-minute task, paid every nation and kingdom
allowance twice and advanced `contract_cycle` by two. Reproduced with two
concurrent command callbacks: a nation holding no balance gained **40,000**
instead of 20,000, two payout ledger rows were written, and the cycle moved
from 1 to 3.

**Cause.** Both the command and `Payout._tick()` checked `next_cycle_at`
**before** calling `_run_cycle()`. `_run_cycle()` serialises with
`_cycle_lock`, but the loser still entered the engine after the winner had
committed and advanced the schedule; its stale pre-check was the only guard.

**Impact.** Credits are created by the cycle, so this is a direct money bug,
and it burns an extra contract cycle for every account.

**Fix.** `_run_cycle(..., require_due=False)` now repeats the natural-cycle
due-check **after** the lock is held and returns
`{"skipped": "not_due", ...}` when the cycle is no longer due. The command and
the task pass `require_due=True`; `/payout default force:true` deliberately
passes `False`. The task ignores a skipped result instead of posting a second
summary.

**Test.** `checks/check_tax_cycle.py` runs two `require_due=True` cycles
concurrently and asserts one allowance, one contract-cycle advance and one
`not_due` result.

### `/bank setbalance` held the write lock across a Discord call

**Symptom.** A slow or expired interaction reply blocked every other money
command for the duration, and if the reply failed the admin's balance edit
rolled back even though the command had reached Discord.

**Cause.** `setbalance` awaited `interaction.response.send_message(...)` inside
`async with self.db.transaction()`, including the not-found branch.

**Impact.** `BEGIN IMMEDIATE` is process-wide through the shared connection, so
a network call inside it stalls the whole economy; the reply could also make a
successful edit appear applied when it was about to roll back.

**Fix.** The transaction now only re-reads and writes; `fresh`/`old` are
captured, the block commits, and both the missing-account and success replies
happen afterwards.

**Test.** `checks/check_bank.py` spies on the response and records
`db._in_tx` at send time; it must be `False`.

### A double click on the all-in confirmation could place two bets

**Symptom.** Two component interactions delivered before the first response
could both settle the same confirmation. After a win the wallet still covered
the old stake, so the second click placed a second all-in that the player never
intended.

**Cause.** `AllinConfirm` disabled its buttons, but an interaction already in
flight still reached `settle_allin()`; there was no one-shot guard.

**Impact.** Unintended second wager (conserved against the Casino, but the
player's money was put at risk twice). A lose-then-click was harmless only
because the empty wallet failed the balance re-check.

**Fix.** `settle_allin(..., view=...)` flips `view.used` synchronously before
any await and refuses the duplicate ephemerally; `Cancel` refuses once a bet
has started.

**Test.** `checks/check_allin.py` settles one confirmation twice through the
same view and asserts the second reply is refused and the wallet is unchanged.

### `blackjack.is_soft()` misread multi-ace soft hands

**Symptom.** `A♠ A♥ 9♦` (a soft 21) was reported as hard, because the raw total
is 31 even though one ace is still counted as 11.

**Cause.** `is_soft()` tested `sum(card_value(c)) <= 21` instead of counting how
many aces the best total had demoted.

**Impact.** A pure helper only; no game rule branches on it, so no live money
was affected. Any future soft-17/double rule would have used the wrong answer.

**Fix.** Return true when the hand is not bust and
`(raw - hand_value) // 10 < ace_count`.

**Test.** `checks/check_casino.py` pins `A,A,9` as soft and four demoted aces
plus a king as hard.

### A name race at creation could strand a petition or crash a direct add

**Symptom.** If a direct `/nation add` or `/kingdom add` won the name race
between the approval's uniqueness check and its insert, the card was edited to
"Name Taken" but the petition row stayed `pending` indefinitely. The reverse
race — an approval winning while a direct add was inserting — surfaced an
unhandled `IntegrityError` and the generic "Something went wrong" reply.

**Cause.** `sqlite3.IntegrityError` rolled back the approval transaction
(including the status flip), and the handler returned without closing the row.
The two direct add commands had no handler at all.

**Impact.** A stale pending petition could not be approved and blocked reuse of
the name; a second click was needed to move it to `failed`. A direct add could
fail with an opaque error instead of naming the clash.

**Fix.** The approval error path now marks the petition `failed` in a fresh
transaction and respects a concurrent decision (`claim_pending_creation`
returns False). `/nation add` and `/kingdom add` catch the same
`IntegrityError` and reply "Name Taken".

**Test.** `checks/check_creation_race.py` forces the insert to raise
`IntegrityError` on the approval, `/nation add` and `/kingdom add` paths, then
asserts the petition is `failed`, no account exists and no bonus row was
written.

---

## Bug-testing close-out — 2026-10-27

**Status.** Both defects are fixed, covered by checks and uploaded with the
session close-out (stamp `20261027-221143`); the running process still needs the
hosting-panel restart.

### Dissolving a nation or kingdom silently burned its balance

**Symptom.** `/nation delete` and `/kingdom delete` removed the account and any
credits still in it, but the `nation_delete` / `kingdom_delete` ledger row had
`amount = 0`. The deleted treasury vanished from the asset total while its
ledger history still contained the credits, so the books no longer reconciled.

**Cause.** The delete commands called `Database.delete_nation()` /
`delete_kingdom()` and then wrote a zero-amount audit row. Nothing read the
balance or logged the burn.

**Impact.** Accounting integrity. On the live database one deleted nation
(`BoxierFire25`, id 5) left exactly **30,000 credits** unaccounted: assets
55,943,499 vs ledger 55,973,499. The historical gap is not repaired by this
change; it prevents new ones.

**Fix.** `Database.delete_account(account_type, account_id, ...)` re-reads the
row inside one transaction, deletes it, and writes a negative
`nation_delete` / `kingdom_delete` ledger row for the remaining balance with
`balance_after = 0`. Both cogs call it and report the burned amount. The helper
also handles a zero balance and a missing row.

**Test.** `checks/check_database.py` asserts the burned amount, the negative
ledger row, a zero-balance delete, a missing-row no-op, and that the deleted
account's ledger sums to zero.

### Reopening a rejected contract made its forfeited tranche claimable

**Symptom.** After a contract was rejected, `/contract reopen` moved it back to
`negotiating`; `_claimable_tranches()` then returned the unclaimed upfront
tranche again, even though rejection is supposed to forfeit it.

**Cause.** `reopen` passed `from_statuses=("approved", "closed", "completed",
"rejected", "cancelled")`. Rejection was only blocked while the status was
still `rejected`; reopening removed that guard.

**Impact.** A Union Admin could un-forfeit a rejected case and pay the 25%
upfront from the Union treasury. It contradicted the documented rule that a
rejected case's unclaimed tranches are forfeited.

**Fix.** `cogs/contract.py` now exposes `Contract.REOPENABLE_STATUSES =
("approved", "closed", "completed")` and `reopen` uses it. `rejected` and
`cancelled` are final; `close` already refused them.

**Test.** `checks/check_contracts.py` asserts the constant excludes
`rejected`/`cancelled`, that a transition from `rejected` with the reopen
statuses returns False, and that `_claimable_tranches()` stays empty.

---

## Contract minimum — 2026-10-27

### New contracts could be opened below the owner's intended minimum

**Symptom.** A party could request or approve a contract for 10 credits even
though the intended minimum is 100 credits.

**Cause.** `budget_allowed()` only checked the leverage ceiling, and
`/contract request` only rejected non-positive budgets or values above the
100,000,000 maximum. No minimum existed in config or in the approval path.

**Fix.** Added `config.CONTRACT_MIN_CREDITS = 100`; a new contract must be
between 100 and 100,000,000 credits. `contract_limits.budget_allowed()` enforces
the minimum, `/contract request` reports the allowed range, and
`/contract approve` refuses a pending below-minimum row. Existing below-100 rows
remain claimable/releasable. Gilbert's prompt and the public documentation were
updated.

**Test.** `checks/check_logic.py` asserts that 99 is refused even when leverage
would allow it, and that a 100-credit contract is accepted when the party holds
100 credits.

---

## Transaction, contracts and Gilbert — 2026-10-26

### An autocommit write could commit another task's transaction

**Symptom.** A failed transfer could still leave its balance change behind, or a
rolled-back contract claim could leave a tranche flag set, only under concurrent
commands.

**Cause.** `Database.execute()` called `conn.commit()` whenever the *calling*
task was not in a transaction. The connection is shared, so if another task had
`BEGIN IMMEDIATE` open, that commit ended the other task's transaction. Its later
`rollback()` was a no-op. The old check only asserted that the unrelated write
survived; it did not assert that the other task's rollback still rolled back.

**Fix.** `execute()`, `fetchone()` and `fetchall()` now take `_tx_lock` outside a
transaction; the depth owner is checked with `asyncio.current_task()` so a child
task cannot inherit a parent's transaction either. Added
`check_database.py` and `check_transactions.py` assertions for the balance
remaining unchanged.

### A rejected contract could be made claimable by `/contract close`

**Symptom.** After a Union Admin rejected a funded contract, the requester could
run `/contract close case_id:` and then `/contract claim` to receive the tranches.

**Cause.** `close_contract()` only refused status `"closed"`, so it changed
`"rejected"` to `"closed"`. `_claimable_tranches()` only blocked the exact status
`"rejected"`.

**Fix.** `close_contract()` refuses `rejected`, `completed` and `cancelled`, and
uses the new guarded `Database.transition_contract()` for `negotiating`/
`approved` to `closed`. `release`, `reject` and `reopen` use the same guarded
transition. `_pay_claim()` re-reads the contract inside its transaction and
refuses rejected/cancelled rows. Closing an approved contract no longer strands
the 75% escrow because release accepts `closed` when `approved_at` is set.

**Test.** New `checks/check_contracts.py`: rejected claim refused, rejection not
moveable to closed, close command refuses, closed approved contract still
releases, cross-guild target refused.

### Contract, order and kit lookups were not scoped to the interaction guild

**Symptom.** In a multi-guild deployment an admin or member could act on a
contract, order or kit from another guild by passing its global ID/key.

**Cause.** `get_contract_by_case()`, `get_order_by_key()` and `get_kit()` are
global lookups; the command handlers did not compare `guild_id` with
`interaction.guild_id`. `/contract approve` even used the caller guild's Union
balance and claim-account resolution for another guild's contract.

**Fix.** Every contract lifecycle command and button path checks
`Contract._same_guild()`; `_owner_row()` and `_claim_target()` reject rows from a
different guild; `/shop order`, `/shop markredeemed` and `/kit view`/`buy` check
the record guild. Guild-only guards were added to the Union balance/members
commands and to the relevant DM-capable commands.

### A deleted destination could silently destroy credits

**Symptom.** `/individual pay`, `/bank transfer` or a contract claim could debit
the source and credit nothing if the destination nation/kingdom was deleted at
the same moment.

**Cause.** `add_balance()` and the destination side of `transfer_balance()` used
an unconditional `UPDATE ... WHERE id = ?` and treated zero affected rows as a
balance of zero.

**Fix.** Both raise the new `AccountMissing`; `/bank deposit`/`transfer` and
`/individual pay` report that the account was deleted. The transaction rolls the
source debit back.

**Test.** `checks/check_contracts.py` asserts a transfer/credit to a deleted
account raises and leaves the source untouched.

### A blackjack settlement exception locked the table

**Symptom.** A hand could become unclickable while its stake stayed with the
Casino until the process restarted.

**Cause.** `BlackjackView.settle_hand()` set `settled = True` before the DB
transaction. `CapExceeded`/`InsufficientFunds` rolled the transaction back but
left the in-memory view marked settled.

**Fix.** The view stays unsettled on those exceptions and returns a
"Settlement Failed" embed; the transaction rolls back, so the hand can be retried
or refunded at startup.

### Gilbert was falling back because the live AI key was invalid

**Symptom.** Members saw Gilbert's "line's gone dead" fallback instead of an AI
reply.

**Cause.** The live `.env` points at `https://api.deepseek.com` and its
`AI_API_KEY` was rejected with HTTP 401. `AI_MODEL=deepseek-flash` is accepted by
this endpoint, but the old code defaulted to that legacy name and had no explicit
model normalisation.

**Fix.** `gilbert/ai.py` now defaults to `deepseek-chat`, strips a duplicated
`/chat/completions` suffix, and normalises the legacy `deepseek-flash` name to
`deepseek-chat` for portability. Failures log the model/URL/status without
logging the key; the fallback tells members an admin must check the AI link.
**The live `.env` was rotated to a valid owner-supplied key (backup
`.env.bak-20261027-012852`), and the endpoint returned HTTP 200 on a test
completion.** `.env` is never included in the source upload/repository.

**Test.** New `checks/check_gilbert.py` covers model/base-URL normalisation,
disabled-without-key behavior, tier boundaries and prompt context.

---

## Poker engine — 2026-10-24

`poker.py` is new and not yet imported by the bot, so none of these reached a
player. They were found by `checks/check_poker.py` before any Discord code was
written.

### Preflop opened with an empty betting round, so the blinds did not count

**Symptom.** A hand started with 60 credits in the pot but every seat was told
it owed nothing. The first actor could `check` instead of calling the big blind,
and the big blind was not treated as having posted at all.

**Cause.** The hand-start sequence dealt, posted the blinds, then opened preflop
by calling `_begin_street(PREFLOP)`. That method is the *street reset*: it sets
`current_bet = 0` and zeroes every seat's `committed`. Correct for flop, turn
and river, wrong for preflop, where the blinds are already live bets. The chips
stayed inside `seat.total`, so nothing was destroyed — they simply became dead
money that nobody was obliged to match, and `to_call()` returned 0 for the whole
table.

**Impact.** High for the game, none for the ledger. Every preflop would have
been free to enter and the small blind would have been the only seat punished
for posting, which is the opposite of the rule.

**Fix.** Preflop is no longer opened with `_begin_street()`. `_post_blinds()`
posts and records `small_blind_seat` / `big_blind_seat`, and the new
`_start_preflop()` opens the street without touching `committed`: it sets
`current_bet = max(committed)`, keeps `min_raise = big_blind` and builds
`to_act`. `__init__` now calls `_deal()`, `_post_blinds()`, `_start_preflop()`.
Do not reintroduce a `_begin_street(PREFLOP)` call; `_begin_street` is for
flop/turn/river only.

**Test.** `checks/check_poker.py`: "pot starts at the blinds", "blinds are
debited from the stacks", "first seat to act owes a full big blind", plus "the
big blind can still act after limps", "big blind owes nothing after limps" and
"big blind may check its option". It also asserts that the flop resets the bet
to zero and `committed` while the preflop chips stay in the pot.

### The small blind acted first preflop

**Symptom.** Order was button, small blind, big blind, under-the-gun — so the
player on the button acted second-to-last instead of second.

**Cause.** `_begin_street()` defaults `start = self._seat_after(self.button)`,
which is the small blind. That is right postflop (action starts left of the
button) and wrong preflop (action starts left of the *big blind*, so the blinds
act last and keep their option).

**Impact.** Medium: a rules error visible to every player at the table, and it
removed the big blind's option, because the round ended when the big blind was
reached last with nothing to call.

**Fix.** `_start_preflop()` sets
`self.actor = self._next_actor_from(self._seat_after(self.big_blind_seat))`.

**Test.** `checks/check_poker.py`: "player (on the button) acts second preflop"
asserts `actor == 3` with the button on seat 0, and "first to act postflop is
left of the button" asserts the button-relative order returns after the flop.

### The check itself expected the wrong flush

**Symptom.** `check_poker.py` reported `(5, 14, 11, 8, 5, 2)` as a failure for
`A♠ J♠ 8♠ 5♠ 2♠ K♥ Q♦`.

**Cause.** The expectation was `(FLUSH, 14, 13, 11, 8, 5)`, which includes the
king — but `K♥` and `Q♦` are not spades, so the best five-card flush is
A-J-8-5-2. The engine was right and the assertion was wrong.

**Impact.** None in production; it cost a false defect hunt. Recorded so the
next reader does not "fix" `hand_rank()` to match a bad expectation.

**Fix.** Expected value corrected to `(FLUSH, 14, 11, 8, 5, 2)`.

**Test.** `checks/check_poker.py`, "seven-card flush picks the best five".

---

## Casino and blackjack — 2026-10-24

### A winning wager reported the payout as the new balance

**Symptom.** None in the database, but the embed lied: a wallet holding 6,000
after a 2,000 win announced "Balance: 2,000". A player reading their own balance
after a bet would think the win had been taken back.

**Cause.** `Database.credit_individual_strict()` returns
`(amount_added, balance_after)` — the same tuple order the old coinflip code
used as `_added, after`. `casino.finish_wager()` unpacked it the other way,
`player_after, _ = ...`, so it took the amount credited as the balance.

**Impact.** Medium, and it would have been worse than cosmetic. The balance was
correct in the database, but the wrong value was also written into the
`balance_after` column of the ledger row, so the ledger's own audit trail
disagreed with the wallet and any later reconciliation would chase a phantom
discrepancy. No credits were created or lost.

**Fix.** `casino.finish_wager()` now unpacks `_added, player_after` and says so
in a comment. Grep for `credit_individual_strict` — every caller needs this
tuple order.

**Test.** `checks/check_casino.py` asserts the exact balances after a win, a
loss and a push (115,000 / 105,000 / 105,000), so a swapped tuple fails on the
first hand rather than being noticed by a player.

### A restart mid-hand would have kept the stake

**Symptom.** A player who dealt a hand and then saw the bot restart would lose
their stake with no hand and no result.

**Cause.** Blackjack takes the stake at the deal and holds it while the player
decides, but the Hit/Stand buttons live only in the `BlackjackView` instance in
memory. A restart dropped the view, so `on_timeout` never fired and nothing ever
settled the hand. The credits stayed in the Casino.

**Impact.** Medium. It cannot print money (the house gains), but it silently
charges a player for a hand they never finished, which is the worst kind of bug
to have in a gambling command because it looks like bad luck.

**Fix.** Every dealt hand is written to `blackjack_hands` in the same
transaction that takes the stake. `bot._refund_abandoned_blackjack_hands()` runs
at startup after the persistent views are re-registered and pays any hand still
`open` back in full (`finish_wager(..., outcome="push")`). Refunding is the only
safe resolution: it cannot create credits because the stake came from the player.

**Test.** `checks/check_casino.py` deals a hand, runs the startup helper against
a `Database` with that row, and asserts the wallet is whole, the Casino is
reduced by the same amount and the hand is no longer open - then runs it a second
time to prove it does not pay twice.

### A timeout could have raced a button press and paid twice

**Symptom.** None seen; the window is two seconds wide.

**Cause.** `settle_hand()` set `self.settled = True` and then awaited the
settlement, but `on_timeout` and a button callback are separate tasks. If the
120s timer fired while a Stand press was paying out, both could reach
`finish_wager()`.

**Impact.** High if hit: a double payout mints credits, which is exactly what
the house account exists to prevent.

**Fix.** `Database.close_blackjack_hand()` flips `status` from `open` to
`settled` in a guarded `UPDATE ... WHERE status='open'` and returns whether it
won the race. `settle_hand()` calls it **inside** the payout transaction and
returns `None` without paying if another task already closed the hand.

**Test.** `checks/check_casino.py` closes a hand twice and asserts the first
call returns `True`, the second `False`; it also stands twice and asserts the
second press leaves the balance untouched.

---

## Transactions — 2026-10-22

### One task's transaction could swallow another task's work

**Symptom:** None visible. A balance change would silently vanish, or a change
that should have failed would appear to succeed, only under concurrent commands.

**Cause:** `Database.transaction()` kept its depth in process-wide instance
fields (`_tx_depth`, `_in_tx`) and every statement awaits, so two coroutines
interleaved between statements. A second task's `async with db.transaction()`
saw a non-zero depth, assumed it was a nested block, and **joined the open
transaction** instead of opening its own. Its work was then committed — or
rolled back — by the other task's block.

**Impact:** High. The blast radius is every money path that uses the context
manager: shop checkout, `/bank transfer`, `/bank draw`, contract claims, the
payout cycle and `/daily`. A task that committed could lose its write if a
concurrent task raised, with no exception anywhere. It also defeated
`commit_balance_to_union()`'s `assert self._in_tx` guard, which a stranger's
transaction could satisfy.

**Fix:** The depth counter is a `ContextVar` (per asyncio task) and `_in_tx` is
now a property over it. A nested block in the **same** task still joins the
outer transaction; a **different** task finds depth 0, waits on `_tx_lock` and
issues its own `BEGIN IMMEDIATE`. See `Database.transaction()`.

**Test:** `check_db.py`'s `independent write survives another task's rollback`
(previously failing) plus the `tx_race.py` suite: a committed task survives
another task's rollback, a rolled-back task leaves nothing, an independent plain
write is not swallowed, nested blocks join, and an inner failure rolls the outer
back.

## Individual transfers and net worth tax — 2026-10-22

### A transfer into a capped wallet would have deleted credits

**Symptom:** Sending credits to a member near the 100M cap would make the credits
disappear: debited from the sender, never stored for the recipient.

**Cause:** `credit_individual()` trims every credit to the remaining room. That
is correct for a faucet (`/daily`, `/individual give`) but wrong for a transfer,
where the sender has already been debited for the full amount.

**Impact:** High, and silent: it would destroy player money with a success reply.

**Fix:** `Database.credit_individual_strict()` refuses instead of trimming and
raises `CapExceeded(balance, amount, room)`. `/individual transfer` catches it,
reports the remaining room and transfers nothing; the enclosing transaction rolls
the sender's debit back.

**Test:** `check_e2e.py` — a recipient with 10 credits of room is refused, the
sender's balance is unchanged, and no ledger row is written.

### The net worth tax needed a natural-cycle-only trigger

**Symptom:** N/A (design guard, not a defect).

**Cause:** `_run_cycle()` serves both the automatic 14-day tick and the admin
`/payout run`. Charging the tax on both would let an admin drain every wallet by
repeating the manual command.

**Impact:** Medium if it had shipped: a trusted-but-destructive foot-gun.

**Fix:** The tax block is gated on `natural`. `/payout run` charges nothing.
Union dues still run on both, as documented.

**Test:** `check_cycle.py` — a manual run leaves balances untouched and reports
no individual tax.

---

## Individuals and `/daily` — new on 2026-10-22

### Individual balances could pass the 100M cap

**Symptom:** None yet — this is a new balance type, so the trap is future code.

**Cause:** A personal wallet has a hard ceiling (`config.INDIVIDUAL_MAX_CREDITS`).
A plain `balance = balance + amount` would sail past it and the excess would be
indistinguishable from real credits.

**Impact:** High if it happened: the cap is the only limit on the daily faucet.

**Fix:** `Database.credit_individual()` is the only credit path. It computes
`min(amount, cap - balance)` inside the caller's transaction and returns
`(added, balance_after)`. Callers log `added`, never the requested amount.
`debit_individual()` guards the spend in the UPDATE, like `debit()`.
`/individual give`, `/daily` and any future inflow must use it.

**Test:** `individual cap` (partial credit, then 0 at the cap, overdraw refused)
and `daily cap` (a 2,000 payout trimmed to the remaining 500, ledger reconciles).

### A double-clicked `/daily` could pay twice

**Symptom:** Two claims at the same moment could add two daily payouts.

**Cause:** "Read `last_daily_at`, then write it" is a check-then-act race, the
same shape as the draw race.

**Impact:** High — `/daily` is a repeating faucet, so a reliable double-claim is
unlimited money.

**Fix:** `claim_daily()` runs the read, the state flip
(`claim_individual_daily()`, guarded on the timestamp read), the credit and the
`daily` ledger row inside one `db.transaction()`. The guarded UPDATE means only
the call that still sees the old timestamp wins, even outside a transaction.

**Test:** `daily concurrent` (two simultaneous claims, exactly one paid, one
ledger row), `daily claim` (second claim inside 24h refused).

### Deleting a wallet left its credit history behind

**Symptom:** `/individual delete` must remove "stored data for an individual
balance"; leaving `transactions` rows would keep the member id, amounts and
dates.

**Cause:** The ledger is append-only by design, and every other deletion keeps
its rows.

**Impact:** Medium — a privacy promise the bot would silently break.

**Fix:** `Database.delete_individual()` deletes the wallet row **and** every
`transactions` row with `account_type = 'individual'` and that `account_id`, in
one transaction. This is the documented exception to the append-only ledger.
`account_id` is a global autoincrement id that is never reused, so later rows
cannot collide with erased history.

**Test:** `individual delete` (row and rows gone, second delete returns False,
no rows resurrected).

### Leaderboards could be merged by accident

**Symptom:** One ranking mixing treasuries and players would compare balances
with different caps and rules.

**Cause:** A single `ORDER BY balance` over a `UNION` of three tables is the
obvious shortcut.

**Impact:** Medium — wrong ranking, misleading economy signal.

**Fix:** `cogs/leaderboard.py` keeps one query per type in `_QUERIES` and one
`top(kind, guild_id)` accessor; there is no cross-table query. Each subcommand
renders its own embed and footer.

**Test:** `leaderboards` (nations, kingdoms and individuals ordered separately,
individual labels as mentions).

---

## One-shot status guards (2026-10-22 bug pass)

### A failed draw DM could strand a pending draw

**Symptom:** If the kingdom leader had DMs closed *and* the user lookup failed,
or Discord returned an error other than `Forbidden`, the draw stayed `pending`
forever and could not be retried cleanly.

**Cause:** `cogs/bank.py` caught only `discord.Forbidden`. `NotFound` and
transient `HTTPException`s escaped, leaving the row and its message behind.

**Impact:** Medium — a dead request blocks nothing else, but the ledger shows a
pending draw that no one can resolve.

**Fix:** the DM block catches `discord.HTTPException` (the parent of both) and
marks the draw `failed` before replying.

**Test:** covered by review; the failure path needs a live Discord API, so the
local suite cannot exercise it.

### Petition approval could found the account twice

**Symptom:** Two admins clicking **Approve** together could both run the
approval. The name UNIQUE constraint stopped the second account, but the button
died with an unhandled `IntegrityError`; a race against `/nation add` looked like
a crash instead of "Name Taken".

**Cause:** `CreationApprovalView._approve()` called the unguarded
`set_creation_status()` after founding the account, so the status was never the
gate.

**Impact:** Medium — one bonus could be attempted twice; the second attempt was
noisy rather than silent.

**Fix:** `Database.claim_pending_creation()` guards on `status = 'pending'` and
runs inside the same transaction as `found_account()`. A lost race answers
"Already Handled"; a name collision from `/nation add` answers "Name Taken".

**Test:** `kit/petition guards` (the flip succeeds once, then returns False) and
`creation race` (two foundings, one account, one bonus).

### Contract approval was not a one-shot status

**Symptom:** Two admins pressing **Approve** at once could both pass the
`negotiating` check, both post to the board and both DM the parties; the second
write could overwrite the claim account chosen by the first.

**Cause:** `approve` read the row, then called `update_contract()` with no
status guard — unlike every other one-shot transition.

**Impact:** Medium — no credits moved, but the payout destination and the board
could disagree.

**Fix:** `Database.approve_contract()` performs
`UPDATE ... WHERE id = ? AND status = 'negotiating' AND approved_at IS NULL` and
returns whether this call won; the cog branches on it.

**Test:** `contract approve once` (two concurrent approvals, exactly one wins,
the row keeps the first split).

### Release and delete wrote state and ledger separately

**Symptom:** A crash between the status change and its ledger row could leave a
contract released with no history, or an account deleted with no deletion row.

**Cause:** `Contract.release()` and `/nation delete` / `/kingdom delete` used
two autocommit statements where the rest of the code uses one transaction.

**Impact:** Low — presentation/audit only, no credits.

**Fix:** both wrapped in `async with self.db.transaction()`.

**Test:** covered by `core` (transaction semantics) and review; no credit moves.

### Two kit modals could beat the 30-day cooldown

**Symptom:** A player could open two kit modals and submit both, receiving two
kits in one cooldown window.

**Cause:** the cooldown was read in `/kit create`, then written after the modal
was submitted — a check-then-act window measured in minutes.

**Impact:** Medium — a free kit is a real item grant.

**Fix:** `Database.claim_kit_cooldown()` inserts/updates the row only when the
stored timestamp is older than the cutoff, and the kit plus its items are
written in the same transaction. The pre-check stays only as a courtesy.

**Test:** `kit/petition guards` (first claim True, immediate second False, after
the window True).

### Case numbers could collide under concurrent requests

**Symptom:** Two simultaneous `/contract request` calls for the same nation tag
could be assigned the same `case_id`, and one would fail on the UNIQUE
constraint.

**Cause:** `next_case_number()` bumped the counter and read it back in two
autocommit statements, so both callers could read the same value.

**Impact:** Low — one request errors, nothing is mispaid.

**Fix:** the bump and the read share one transaction.

**Test:** `case counter` (eight concurrent calls produce 1..8, no duplicates).

---

## Bank — transfers and draws (`cogs/bank.py`, `database.py`)

### Draw approval could pay the same request twice

**Symptom:** A kingdom could lose twice the requested amount (or its draw could
appear paid twice in the ledger) if two admins clicked **Approve** at almost the
same moment, or one admin double-clicked on a slow connection.

**Cause:** `DrawApprovalView._finalize()` read the draw, checked
`status == "pending"`, moved the money, and only then called
`set_draw_status()`. Between the check and the write, a second interaction could
pass the same check. There was no guarded UPDATE and no shared transaction.

**Impact:** High — silent, unrecoverable credit duplication between a kingdom
and a nation, visible only as a ledger mismatch.

**Fix:** `Database.claim_pending_draw(draw_id, status)` performs
`UPDATE ... WHERE id = ? AND status = 'pending'` and returns whether *this* call
won. `_finalize()` now claims the row *inside* the same transaction that runs
`transfer_balance()` and both `log_tx()` calls, and branches on the row count.
A loser gets "Already Handled". If the transfer raises `InsufficientFunds` the
transaction rolls back, the row is explicitly marked `failed`, and nobody can
retry a request that cannot be paid.

**Test:** `draw approval pays once (race fixed)` — two concurrent claim+transfer
attempts, exactly one succeeds, balances move once. This replaces an earlier
test that asserted the bug existed.

### Transfer wrote its ledger rows outside the money transaction

**Symptom:** None observed, but a process kill between the balance write and the
ledger writes would leave balances that no ledger row explains — the exact thing
`/ledger` exists to disprove.

**Cause:** `transfer_balance()` was awaited, then the two `log_tx()` calls ran
separately in autocommit.

**Impact:** Medium — invariant violation, not a user-visible bug until a crash.

**Fix:** The transfer and both ledger rows now sit inside one
`async with self.db.transaction()`.

**Test:** `transfer atomic` and `transaction rollback`.

### `/bank setbalance` could leave a balance with no ledger row

**Symptom:** None observed. If the process died between the balance write and
the ledger write, the account's balance and the sum of its transactions would
disagree forever, and `/ledger` would report a discrepancy no command caused.

**Cause:** `Bank.setbalance` awaited `set_balance()`, then `log_tx()` in
autocommit. Every other money path already shared a transaction.

**Impact:** Low — admin-only correction, but it breaks the project's core
reconciliation invariant.

**Fix:** Both writes now sit inside one `async with self.db.transaction()` in
`cogs/bank.py`. The admin correction still deliberately skips
`commit_balance_to_union()`.

**Test:** `setbalance writes one ledger row` — set a nation's balance, assert the
new balance and that its transaction sum equals it.

---

## Ledger (`cogs/ledger.py`)

### `/ledger view` and `/ledger all` failed at high limits

**Symptom:** `/ledger view limit:50` produced "Invalid Form Body" from Discord
instead of a ledger.

**Cause:** Both commands declared `app_commands.Range[int, 1, 50]`, but Discord
embeds allow at most 25 fields. The command built one field per row.

**Impact:** Low (annoying, not data loss), but it was the documented way to read
a long history.

**Fix:** Range capped at 25 and the descriptions now say "max 25".

**Test:** `embed field count > 25 (ledger limit 50)` asserts that no command can
build more than 25 fields.

---

## Kits (`cogs/kits.py`)

### `/kit create` could fail after already creating the kit

**Symptom:** An occasional "Something went wrong" from `/kit create`, with the
kit actually saved and the 30-day cooldown consumed — so the player lost their
kit for nothing.

**Cause:** `_publish_kit()` used `self.bot.fetch_channel(...)` /
`fetch_user(...)` without exception handling *after* `create_kit()` and the
cooldown write. A deleted kit channel, a permission change or an unknown user
raised `NotFound`/`Forbidden` and aborted the command.

**Impact:** Medium — visible loss of a limited resource.

**Fix:** Channel lookup is `get_channel` first, then a guarded
`fetch_channel` catching `NotFound`, `Forbidden` and `HTTPException`; a failure
logs a warning and returns, leaving the kit intact. The creator lookup is
guarded the same way and falls back to a mention.

**Test:** Import + cog-load smoke test (the function must import cleanly);
behaviour was reviewed against the `discord.py` exception hierarchy.

---

## HTTP API (`api.py`)

### Non-object JSON bodies returned 500

**Symptom:** `POST /api/redeem` with `[]` or `"hello"` produced a 500 and a
traceback instead of a clean client error.

**Cause:** The handler assumed `request.json()` returns a dict and called
`.get()` on it.

**Impact:** Low — noisy logs, and monitoring counts a client mistake as a
server fault.

**Fix:** After decoding, `isinstance(data, dict)` is enforced and violations
return `400 {"error": "invalid_json"}`.

**Test:** `api json type handling`.

### Retired contract routes answered before authorisation

**Symptom:** Anyone could probe `/api/contract/preview` and
`/api/contract/redeem` and get a `410`.

**Cause:** The catch-all `410 no_in_game_currency` response was returned before
`self._authorized(request)`.

**Impact:** Low — information disclosure only (the endpoints are dead), but it
is inconsistent with the rest of the API.

**Fix:** Both handlers check the bearer token first and return `401` when it is
missing or wrong.

**Test:** Covered by reading the handler order; the smoke test proves the routes
still register.

---

## Contracts (`cogs/contract.py`, `database.py`)

### Contract credits were created from nothing

**Symptom:** Every claimed contract increased the total money supply. Treasury
balances and the ledger reconciled, but the sum of all balances grew with each
claim, which is the inflation the Union was supposed to prevent.

**Cause:** `_pay_claim()` called `add_balance()` on the claim account with no
funding source. `approve` did not check any budget either.

**Impact:** High — the core economic defect behind this whole update.

**Fix:** New `union_account` table and `Database.credit_union()` /
`debit_union()`. `_pay_claim()` now debits the treasury before crediting the
recipient, inside the same transaction as the tranche flag and both ledger rows.
`/contract approve` refuses a budget larger than the treasury balance. Union
inflows are limited to `/union fund` (admin), `/union donate`, member dues and
admin-approved `/union commit` donations.

**Test:** `claim requires Union funds`, `claim rolls back on short Union`,
`concurrent claims pay once`, `full payout cycle with dues` (Union ledger
reconciliation).

### A short treasury could have half-paid a claim

**Symptom:** For a two-tranche claim (25% + 75%), a treasury that covered only
the first tranche could pay 25% and then fail.

**Cause:** Hypothetical at the time, because nothing checked the treasury at
all. Worth pinning down before adding a balance.

**Impact:** High if it had shipped — a partially paid claim is very hard to
reason about later.

**Fix:** The whole loop runs in one transaction, so the `InsufficientFunds`
raised by `debit_union()` on the second tranche rolls back the first tranche's
flag, credit and ledger rows. `_pay_claim()` converts it into a `ClaimError`
that names the shortfall and points at `/union fund`.

**Test:** `claim rolls back on short Union` — funds 400,000 against a
250,000 + 750,000 claim, asserts zero paid, zero Union spend, both flags still
zero.

### Kingdom contracts were limited to one per cycle

**Symptom:** A kingdom asking for a second contract in the same cycle was told
"a nation may hold one contract per natural cycle".

**Cause:** `count_contracts_for_cycle()` was compared against a hard-coded
truthiness check (`if existing:`), and the message assumed a nation.

**Impact:** Product decision, not a crash — but it blocked a requested feature.

**Fix:** `config.CONTRACTS_PER_CYCLE = {"nation": 1, "kingdom": 3}` and the
comparison is `existing >= limit`, with a message naming the actual limit.

**Test:** `contract quota (1 nation / 3 kingdom)` against the live rows.

---

## Shop pricing (`shop/pricing.py`, `shop/checkout.py`, `shop/embeds.py`)

### The member discount must not stack with the treasury discount

**Symptom:** N/A — this is a design trap rather than a shipped bug. Applying 50%
and then 20% would give 60% off, and nobody could explain the price.

**Cause:** `quote()` applied a single discount to the whole subtotal.

**Impact:** Would have made the Union discount ambiguous and easy to exploit.

**Fix:** `quote()` takes `member_items_total`. That part is discounted at
`config.UNION_MEMBER_DISCOUNT`; the remainder plus the shulker fee keeps the
treasury rate. `member_items_total=0` (non-members) is arithmetically identical
to the old code. `orders.union_discount` stores the split so the order embed can
show both lines.

**Test:** `pricing: member discount maths` (including clamping and the shulker
fee), `checkout: member vs non-member` (dirt vs redstone vs iron blocks, mixed
cart, persisted rows).

### Bundles and kits were not category-aware

**Symptom:** A kit of building blocks would have missed the member discount, or
a kit of iron blocks would have wrongly received it.

**Cause:** `checkout_kit()` summed prices without looking at categories.

**Impact:** Wrong prices in both directions.

**Fix:** `pricing.is_union_eligible(category)` gates every line, and
`checkout_kit()` accumulates `member_items_total` per component.

**Test:** `checkout: member vs non-member` and the kit path in the smoke test.

---

## Union commands (`cogs/union.py`)

No defects shipped in this file — it is new. Behaviours that were easy to get
wrong and are therefore tested:

- `join`, `leave` and `donate` accept only the account's leader or an admin.
  A stranger gets "Not Authorized" and nothing moves (`union commands end to
  end`).
- Joining twice says "Already a Member" instead of duplicating the row; leaving
  twice says "Not a Member" (`INSERT OR IGNORE` plus the row count from
  `remove_union_member`).
- A donation larger than the balance is refused before any write, and
  `parse_amount` already rejects zero, `half` of 1 credit, and negative input.
- Deleting a nation or kingdom removes its membership row, so a re-created
  account with the same id cannot inherit it.
- Every inflow and outflow reconciles: the sum of `transactions` rows with
  `account_type='union'` equals `union_account.balance` (`payout dues 5%/2%`).

### An approved commitment left the existing balance in the bank

**Symptom:** An account with 883,184 credits asked to commit everything. The
administrator approved, and the account still showed 883,184. The balance was
only donated when the next credit arrived — which could be a full 14-day payout
later, or never if the account was idle. The member could also spend it first
and the "commit everything" promise was silently void.

**Cause:** `UnionCommitApprovalView._finalize()` only flipped the
`union_commits` row to `active`. The donation was attached to credit events
(`Payout._run_cycle`, `Contract._pay_claim`, `/bank deposit`, transfer
destination, approved draw), so approval itself moved no money.

**Impact:** Medium — no credits were created or lost, but `/union commit` did
not do what it says until an unrelated event fired.

**Fix:** `_finalize()` now calls `Database.commit_balance_to_union()` inside the
same transaction that decides the request, after the status flip. The approval
embed reports the amount donated immediately. The request embed states that the
current balance goes on approval and every future credit goes in full.

**Test:** `commit approval donates the bank balance` — request, approve, assert
the account is at 0, the Union grew by that amount, and both `union_commit`
ledger rows exist; then a later credit is still donated and a second approval
is refused.

---

## Payouts (`cogs/payout.py`)

### Dues were calculated on the payout instead of the balance

**Symptom:** Long-standing members paid very little: a nation holding millions
paid 5% of its 14-day allowance, not 5% of its treasury.

**Cause:** `_charge_union_dues()` used `tax = payout * pct // 100`. The dues
base was the credit just added, not the account's whole balance.

**Impact:** High — the Union's main recurring income was understated by orders
of magnitude on any account with savings.

**Fix:** `_charge_union_dues(guild_id, account_type, row, balance, payout)` now
uses `tax = balance * pct // 100`, where `balance` is the balance returned by
`add_balance()` — i.e. the whole balance *after* the payout credits. Both
callers pass `after`. Non-members, zero balances and floored-to-zero amounts
still skip the charge, and the `debit()` guard is untouched because the rate is
below 100%. Ledger notes record the balance and the payout.

**Test:** `payout dues whole balance` (member nation and kingdom deltas vs
`balance * pct // 100`, non-member unchanged, 5% of 19 = 0, four `union_tax`
rows reconciling with the treasury, summary balances) and
`gilbert member context` (member and non-member briefings differ).

### Dues could have diverged from the payout

**Symptom:** N/A — prevented, not observed.

**Cause:** Dues are a second balance change on the same account as the payout.

**Impact:** A separate transaction would allow "payout credited, dues not
charged" after a crash.

**Fix:** `_charge_union_dues()` runs inside the caller's payout transaction and
is called from both the nation and kingdom loops. Tax comes from
`payouts.union_dues(balance, account_type)` — basis points of the whole balance,
never negative, never fractional, skipped for non-members and zero balances.

**Test:** `payout dues 5%/2%` (including 5% of 99 = 4, and ledger
reconciliation) and `full payout cycle with dues` (real `_run_cycle`, member vs
non-member deltas).

### Payout preview embeds could overflow

**Symptom:** With enough nations, `/payout all` would fail on a field over 1024
characters.

**Cause:** The preview joined every row without truncation, unlike the summary.

**Impact:** Low — the preview would break as the server grows.

**Fix:** Both fields are truncated to 1024 characters.

**Test:** Reviewed; the summary path was already truncated.

---

## Documentation (`README.md`, `AGENTS.md`, sub-READMEs)

### Stale statements that contradicted the code

**Symptom:** An agent reading the docs would implement the wrong thing.

**Cause:** The contract economics were rewritten but several files still
described the old model.

**Impact:** Medium — future maintenance hazard.

**Fix:**

| File | Was | Now |
| --- | --- | --- |
| `contract_limits.py` docstring | "75% escrow debited when released" | Treasury funds the whole value; party is never charged |
| `cogs/contract.py` module docstring | "approve debits the nation treasury, releases a key" | approve checks the treasury and records the split; claim pays |
| `forge-mod/README.md` | Told the mod to preview/redeem `UC-`/`UF-` keys | Explicitly removed; routes are `410` |
| `gilbert/README.md` | "One contract per nation or kingdom" | 1 nation / 3 kingdoms, plus treasury grounding |
| `cogs/README.md` | "one contract per natural cycle", claims create credits | Quota, Union debit, retryable shortfall |
| `shop/README.md` | Treasury discount only | Member discount rules, no stacking, `union_discount` |
| `AGENTS.md` | 66 commands, "credits only move in `_pay_claim`" | 73 commands, Union rules and the one-shot-status invariant |

**Test:** Docs were checked against `git diff` and the test suite; no line in
those files now describes a function that does not exist.

---

## Verification harness (`checks/`)

### A failing assertion hung the suite instead of reporting it

**Symptom.** `checks/check_cogs.py` pinned the expected command count with an
`assert`, but the assertion ran *before* `bot.db.close()` and `bot.close()`. When
the count changed, the `AssertionError` propagated out of the coroutine, so the
cleanup never ran: the aiosqlite worker thread stayed alive as a non-daemon
thread, the interpreter blocked in `threading._shutdown`, and the process sat
there until `run_all.py` killed it at its 600s subprocess timeout. The failure
was real but reported as a ten-minute stall, and `run_all.py` attributed it to
"no summary line" rather than to the assertion.

**Fix.** The body of `cog_smoke()` is wrapped in `try/finally`, so the payout
cycle task is cancelled and the database and client are closed whether the
assertions pass or fail. Cancelling the cycle task also stops it from being
reported as a leaked task at shutdown.

**Check.** Run a copy of the script with the pinned count deliberately wrong
(`sed 's/assert n == 98/assert n == 999/'`) and confirm it exits non-zero within
seconds. It used to take the full 600s timeout.

### Pinned counts are deliberate

`check_cogs.py` asserts **98** commands and **13** cogs. The number moves only
when the command surface genuinely changes: 98 after `/blackjack` and `/casino`
(the `/casino` group itself counts as a command, plus its three subcommands),
93 after `/allin`, 92 after `/individual coinflip`, 91 before it, 89 before the
individual transfers, 80 before `/daily`, `/individual` and `/leaderboard`. When it moves, update the
assertion on purpose and note why — never re-baseline it to whatever the script
printed.

---

## Known remaining issues (deliberate, not fixed)

1. **`/nation delete` destroys the balance silently.** The confirmation view
   names the account but not the credits that vanish with it. Deleting is
   admin-only and intentional, so this is a UX gap rather than a defect. A
   future change could require typing the name or offer a payout first.
2. **`/bank withdraw` is a money faucet by design.** It is the admin's tool for
   putting credits into the economy and has no Union counterpart. Do not
   "fix" it; the Union treasury is separate on purpose.
3. **The v1 API has one static bearer token.** Adequate for a single trusted
   consumer; rotation is manual.
4. **Legacy contract `UN-COMMUN-001` needs Union funding before it can be
   claimed.** This is the new rule working as intended, not a bug. Fund the
   treasury before the party clicks Claim.
5. **`_is_contract_space()` treats any channel whose name starts with
   `union-contract-` as a contract space.** Only reachable by manually renaming
   a channel; harmless.
6. **The checks live in `checks/` and are deployed with the app.** They used to
   be throwaway scripts outside version control, which is how earlier defects
   went unnoticed; they are now a permanent suite with a runner. They are still
   not run automatically — nothing invokes them at boot or on a schedule, so a
   regression is only caught when someone runs `checks/run_all.py`. Wiring them
   into a pre-deploy step is the obvious next improvement.
7. **Account name uniqueness is case-sensitive in SQLite but case-insensitive
   in every query.** `UNIQUE (guild_id, name)` would allow `Foo` and `foo`
   side by side, while `get_nation` / `get_kingdom` use `COLLATE NOCASE` and
   would return one of them. Every create and rename path checks first, so this
   needs two near-simultaneous requests to hit; a `COLLATE NOCASE` unique index
   plus a duplicate scan is the clean fix.
8. **The six-nation kingdom cap is checked before the update.** `cogs/kingdom.py`
   counts `nations_in_kingdom()` and then calls `set_nation_kingdom()` outside a
   transaction. Two simultaneous admin `/kingdom addnation` calls can both see
   five members and leave seven. Admin-only and low-frequency; the fix is a
   guarded count-and-update in one transaction.
9. **`/casino withdraw` can strand an open hand's stake.** The command only
   checks the current house balance, not the worst-case payout reserve of open
   blackjack/poker hands. Draining the house mid-hand makes settlement (and a
   restart refund, which debits the house) fail until it is funded again. The
   deal already requires cover; a withdraw-time reserve check would close it.

---

## Audit defects from the 2026-10-27 audit (fixed)

These were found during the full-source bug check that preceded the Fishing
Guild work. **All eleven are fixed**, and each now has a regression check; the
suite exercises the races directly rather than relying on code inspection.
Mapping:

| # | Defect | Fixed in | Check |
|---|---|---|---|
| 1 | Stale wallet list before individual tax | `cogs/payout.py` re-reads inside the payout transaction | `check_tax_cycle.py` |
| 2 | Deleted wallet resurrected by a hand | `find_individual` in debits/credits; open-hand delete guard; settlement re-reads | `check_casino.py` |
| 3 | Draw approval ignored kingdom membership | `DrawApprovalView._finalize` re-reads and refuses ex-members | `check_bank.py` |
| 4 | Contract quota was not atomic | `create_contract(quota_limit=...)` counts inside the insert | `check_contracts.py` |
| 5 | Approval checked stale owner/Union balances | `Contract._approve_checked` re-reads inside the approval transaction | `check_contracts.py` |
| 6 | `setbalance` logged a stale delta | Command re-reads the balance inside its transaction | `check_bank.py` |
| 7 | Plain one-shot status writes | Guarded `claim_pending_draw` / `claim_pending_creation` everywhere | `check_bank.py` |
| 8 | Case IDs collided across guilds | Global per-tag `case_counters_global` reservation | `check_contracts.py` |
| 9 | Poker paid uncalled excess / short all-in reopened | `distribute` refunds the uncalled bet; `raise_locked` | `check_poker.py` |
| 10 | DM `/decree info` ghost settings row | Guild guard plus `get_settings(None) is None` | `check_bank.py` |
| 11 | Runner reported a false pass | Unknown/bare output is a failure; every check exits non-zero | `check_runner.py` |

The original descriptions are kept below as the defect record.

1. **Natural-cycle individual tax read a stale wallet list.** `cogs/payout.py`
   reads `list_individuals()` before opening the payout transaction. A catch
   between the read and the transaction is taxed on the old balance; a
   deleted/recreated wallet can put the `individual_tax` row on the dead id and
   break reconciliation. Proven with a scratch harness: a 10,000 → 100,000
   wallet paid 500 instead of 5,000. Fix: read the list and re-read each wallet
   inside the payout transaction; add a race check.
2. **Deleting a wallet during an open blackjack/poker hand resurrects it.**
   `casino.finish_wager()` uses the stale wallet row and
   `credit_individual_strict()` creates a new wallet, but the payout ledger row
   uses the old `wallet["id"]`. Proven: a deleted wallet came back with 1,000
   credits while its ledger sum was 0. Fix: block `/individual delete` while a
   hand is open, or settle against a freshly read wallet id.
3. **Draw approval does not re-check kingdom membership.** A nation can request
   a draw, be removed from the kingdom, and still be paid when the leader
   approves; `DrawApprovalView._finalize()` reads the kingdom/nation but never
   checks `nation["kingdom_id"] == kingdom["id"]`. Proven with a scratch
   harness. Fix: re-read and verify inside the claim/transfer transaction.
4. **Contract quota is not atomic.** `/contract request` counts existing
   contracts outside the transaction and `Database.create_contract()` has no
   quota guard. Two concurrent requests for one nation both passed a limit of
   one in a scratch harness. Fix: re-check the quota in the transaction that
   inserts the contract, or use a guarded insert.
5. **Contract approval checks stale owner/Union balances.** `owner` and the
   Union balance are read before the approval transaction, so a concurrent
   transfer or withdrawal can invalidate the leverage/treasury gate. Fix:
   re-read both inside the transaction before `approve_contract()`.
6. **`/bank setbalance` logs a stale delta.** It uses `row["balance"]` read
   before the transaction; a concurrent deposit makes the ledger sum differ
   from the balance. Proven: 1,000 → deposit 1,000 → set 1,500 left a ledger sum
   of 2,500. Fix: re-read the balance inside the transaction.
7. **Some one-shot status writes still use plain setters.** `cogs/bank.py` uses
   `set_draw_status()` at lines 76, 123, 163 and 172; `creation.py` uses
   `set_creation_status()` at line 170. A concurrent success can be overwritten
   to `failed`/`invalid`. Use the guarded `claim_pending_draw()` /
   `claim_pending_creation()` helpers inside a transaction.
8. **Contract case IDs collide across guilds.** `contracts.case_id` is globally
   `UNIQUE`, but `case_counters` is per `(guild_id, nation_tag)`. Two guilds with
   a nation of the same tag both generate `UN-AURUM-001`; the second insert
   raises `UNIQUE constraint failed`. Fix: composite uniqueness, a global
   counter, or retry on collision.
9. **Poker can pay uncalled excess to the survivor.** `poker.distribute()`'s
   fallback gives levels above the highest live contribution to the live
   winner; standard rules return an uncalled bet to the contributor. A short
   all-in also reopens betting for players who have already acted. Chips are
   conserved, but the rules are not standard Hold'em.
10. **`/decree info` in a DM can create a ghost settings row and crash.**
    `get_settings(None)` inserts a row with a rowid-assigned guild id, then
    returns `None` because `guild_id = NULL` never matches. Add a guild guard
    (and optionally reject `None` in `get_settings()`).
11. **The check runner can report a false pass.** `checks/run_all.py` treats
    “exit 0 with no summary line” as PASS; `check_contracts.py` and
    `check_database.py` catch unexpected exceptions, print `ERROR`, and exit 0
    without a summary. Proven with an injected `RuntimeError`. Require a
    summary line in the runner and add `sys.exit(1 if fails else 0)` to every
    check.

---

## Verification

Everything above was checked against a copy of the live database
(`royal_bank.db` plus its `-wal`/`-shm`), never against the live file itself.
The ad-hoc suites described below were scratch scripts and are gone. What
survives is the `checks/` directory in this repository, which is deployed with
the app and covers every defect recorded above:

```bash
python3 checks/run_all.py      # whole suite, PASS/FAIL table, non-zero exit on failure
python3 -m compileall -q .     # syntax
python3 -m pyflakes .          # unused names, undefined references
```

| File | Covers |
| --- | --- |
| `checks/check_logic.py` | payout formulas, pricing and discounts, amount parsing, tax label |
| `checks/check_database.py` | schema and migrations, guarded one-shot flips, balance/ledger reconciliation, overdraw guard, account deletion logs the burned balance and nets to zero |
| `checks/check_transactions.py` | transaction isolation between tasks, nesting, rollback of an inner block, plain writes cannot commit another task's transaction |
| `checks/check_contracts.py` | contract rejection cannot be undone by close **or reopen**, claim status re-check, closed-approved release, cross-guild target refusal, deleted destination guard, globally unique case IDs, atomic cycle quota, approval re-reads status/owner/treasury, a real Crown request (CR case ID, Crown thread, `faction='crown'`, Albert intro), Crown-funded claim, per-faction quota, short Crown treasury refusal, and both prompts preloading the live numbers |
| `checks/check_transfers.py` | `/individual transfer` and `/individual pay`, cap refusal, self-transfer, no-wallet |
| `checks/check_full_transfers.py` | all nine transfer directions, concurrent debits (exactly ten of twenty), source permission/admin override, cap and overdraw refusals, cross-guild and same-account guards, ledger nets, Union-commitment hook |
| `checks/check_bank.py` | `/bank setbalance` fresh delta and reply-after-commit, draw ex-member/status guards, one-shot creation status, DM `/decree info` without a ghost settings row |
| `checks/check_tax_cycle.py` | 5% individual tax on Cash and Default runs, stale-list/fresh-balance and recreated-wallet IDs, ledger both sides, disable switch; Cash leaves the schedule/contract cycle alone and Default advances both; Cash/Default/Trigger command paths; two simultaneous natural runs pay and advance exactly once |
| `checks/check_factions.py` | faction switching and Union-commit/contract cleanup, open Crown membership (no soldier gate), a non-member Crown contract request through `/faction contract`, the removed RNG helpers, Crown tax/grace, contract capacity, requested-budget ceilings and treasury value caps, partial/full delivery, demand decay, expiry, short-Crown rollback, Albert context, and the `/faction` join/contract/commitment/deliver/status callbacks |
| `checks/check_coinflip.py` | coinflip odds are exactly 50% over all 10,000 rolls for either call, zero house edge, stake guards, cap refusal, refusal never rolls or moves money, ledger reconciles |
| `checks/check_allin.py` | all-in odds exhausted for every number 2-100, expected return flat at 10/11, house edge 1/11, n=1 refusal, cap and balance refusals, no draw on refusal, duplicate confirmation refused, ledger reconciles |
| `checks/check_casino.py` | blackjack rules (including multi-ace soft hands) and every settlement branch, house solvency and floor, conservation over 200 wagers, restart refund, double-settle guard, open-hand delete guard and no wallet resurrection, public posts |
| `checks/check_fishing.py` | `/fish` random 20–100 range, pure cap/short-guild refusals, guarded Fishing Guild balance, end-to-end catch, both ledger sides, conservation, no-wallet and admin fund/withdraw |
| `checks/check_poker.py` | evaluator category order, wheel, best five of seven, side pots and odd chips, uncalled-bet refunds, folded seats never paid, short all-ins not reopening raises, blind posting and the big blind's option, flop resets without losing the pot, 400 seeded hands with chips conserved and every hand terminating |
| `checks/check_gilbert.py` | DeepSeek model alias/base-URL normalisation, disabled-without-key, tier boundaries, prompt context facts (including soldiers), the friendlier tone, the membership-never-required rule, and Albert's contract briefing |
| `checks/check_cogs.py` | concurrent `/daily` claims, import and cog-load smoke test, asserts 111 commands and 16 cogs, that `/faction` is the only faction command, and that `/faction contract` exposes exactly `faction`/`account`/`proposal`/`budget` |
| `checks/check_creation_race.py` | concurrent creation yields one account and one bonus ledger row; an approval name race closes the petition as `failed`; `/nation add` and `/kingdom add` report the same clash instead of crashing |
| `checks/check_runner.py` | the runner rejects a bare exit or an `ERROR:` line with no summary instead of reporting a false pass |

Every script builds its own throwaway SQLite database in a temp directory. None
of them read `royal_bank.db`, so the live file is never touched.
`check_poker.py` needs no database at all: `poker.py` is pure.

All 18 scripts pass as of 2026-10-28, against the working copy. When you fix a
bug, add its check here — a fix without a check is how the
transaction-isolation defect survived this long.

| Area | Checks |
| --- | --- |
| Money core | transaction rollback, debit guard, transfer atomicity, `setbalance` fresh-delta atomicity, ledger reconciliation |
| Contracts | leverage ladder, claim idempotency, double click, concurrent claim, 75%-until-release, closed contract, legacy refund, atomic quota, global case IDs, approval re-read, short treasury, midway rollback |
| Draws | approval race, ex-member re-check, one-shot status |
| Shop | pricing maths, member vs non-member, mixed cart, shulker fee, double redemption |
| Union | treasury guards, membership lifecycle, cleanup on delete, dues, donations, command surface, commitment approval donation |
| Individuals | 100M cap (partial and full), overdraw guard, deletion erases wallet + ledger rows, open-hand delete guard, no resurrection on settlement |
| `/daily` | first claim, 24h cooldown, +10%/day, 400% cap, -20%/missed day, 60% floor, concurrent claims once, cap-trimmed payout |
| Payout tax | natural + manual runs, stale-list re-read, deleted/recreated wallet id, ledger both sides, disable switch |
| Gambling | coinflip 50% / zero edge, all-in 10/11 / 1/11 edge, blackjack branches, poker side pots/uncalled refunds/short all-ins, conservation |
| Leaderboards | nation, kingdom and individual queries and embeds stay separate and ordered |
| One-shot guards | petition flip, draw/creation status, contract double-approval, kit cooldown, concurrent case numbers |
| Live data | `integrity_check`, per-account and Union ledger reconciliation, union and individuals schema present, pre-fix dues rows identified |
| API | JSON type handling, retired routes |
| Schema | migrations over the live schema, `orders.union_discount`, `case_counters_global` |
| Runner | summary required; bare exit/ERROR is a failure |

All 18 scripts pass. When you add a check, add it to this table.
