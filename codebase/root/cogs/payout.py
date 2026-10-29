"""
cogs/payout.py
==============
Royal payouts and the two payout commands.

* **Cash Cycle** (``/payout cash``) pays the nation and kingdom allowances
  immediately. It never touches the natural schedule or the contract cycle, so
  an administrator can settle cash without resetting anything.
* **Default Cycle** (``/payout default``) is the full natural cycle: cash
  allowances, Union dues and commitments, the 5% individual net-worth tax, the
  contract-cycle advance and the next-cycle timestamp, in one execution.
* **Trigger** (``/payout trigger``) queues or schedules a natural cycle without
  paying now; the 10-minute task picks it up when it is due.

Every 14 days the bot:
  1. Computes each nation's payout and credits it to that nation's Royal Bank.
  2. Computes each kingdom's payout (from member-nation stats) and credits it.
  3. Charges Union dues/commitments and the individual net-worth tax.
  4. Posts a full summary to the assigned payout channel.
  5. Logs every credit to the ledger.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import embeds
import factions
from checks import admin_only
from database import parse_iso
from payouts import individual_tax, kingdom_payout, nation_payout, union_dues
from utils import kingdom_autocomplete, nation_autocomplete, money


class Payout(commands.Cog):
    """⚜ Payout previews and the 14-day Royal payout cycle."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cycle_task.start()

    def cog_unload(self) -> None:
        self.cycle_task.cancel()

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    group = app_commands.Group(
        name="payout",
        description=(
            "⚜ Payout previews, the Cash Cycle, the Default Cycle and the "
            "natural-cycle trigger."
        ),
    )

    # -- previews ----------------------------------------------------------
    @group.command(name="nation", description="Preview a nation's next payout.")
    @app_commands.describe(nation="The nation.")
    @app_commands.autocomplete(nation=nation_autocomplete)
    async def nation(self, interaction: discord.Interaction, nation: str):
        row = await self.db.get_nation(interaction.guild_id, nation)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", f"No nation named **{nation}**."), ephemeral=True
            )
        result = nation_payout(
            row["happy_workers"], row["regular_workers"], row["happy_soldiers"], row["regular_soldiers"]
        )
        await interaction.response.send_message(embed=embeds.payout_embed(f"Nation Payout · {row['name']}", result))

    @group.command(name="kingdom", description="Preview a kingdom's next payout.")
    @app_commands.describe(kingdom="The kingdom.")
    @app_commands.autocomplete(kingdom=kingdom_autocomplete)
    async def kingdom(self, interaction: discord.Interaction, kingdom: str):
        row = await self.db.get_kingdom(interaction.guild_id, kingdom)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", f"No kingdom named **{kingdom}**."), ephemeral=True
            )
        members = await self.db.nations_in_kingdom(row["id"])
        result = kingdom_payout(members)
        await interaction.response.send_message(embed=embeds.payout_embed(f"Kingdom Payout · {row['name']}", result))

    @group.command(name="all", description="Preview the payout for every nation and kingdom.")
    async def all_payouts(self, interaction: discord.Interaction):
        nations = await self.db.list_nations(interaction.guild_id)
        kingdoms = await self.db.list_kingdoms(interaction.guild_id)
        if not nations and not kingdoms:
            return await interaction.response.send_message(
                embed=embeds.info("Nothing to Pay", "No nations or kingdoms exist yet."), ephemeral=True
            )
        embed = discord.Embed(title="⚜ Royal Payout Preview", color=config.GOLD_COLOR)
        if nations:
            lines = []
            for n in nations:
                total = nation_payout(
                    n["happy_workers"], n["regular_workers"], n["happy_soldiers"], n["regular_soldiers"]
                ).total
                lines.append(f"🏛️ **{n['name']}** — {money(total)}")
            embed.add_field(name="Nations", value="\n".join(lines)[:1024], inline=False)
        if kingdoms:
            # Group member nations by kingdom in a single pass (avoids N+1).
            by_kingdom: dict[int, list] = {}
            for n in nations:
                if n["kingdom_id"] is not None:
                    by_kingdom.setdefault(n["kingdom_id"], []).append(n)
            lines = []
            for k in kingdoms:
                total = kingdom_payout(by_kingdom.get(k["id"], [])).total
                lines.append(f"👑 **{k['name']}** — {money(total)}")
            embed.add_field(name="Kingdoms", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text="⚜ The Royal Court · Royal Bank of the Realm")
        await interaction.response.send_message(embed=embed)

    # -- cash cycle --------------------------------------------------------
    @group.command(
        name="cash",
        description=(
            "[Admin] Cash Cycle — pay immediate cash allowances; no schedule "
            "or contract-cycle change."
        ),
    )
    @admin_only()
    async def cash(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        summary = await self._run_cycle(interaction.guild_id, natural=False)
        if summary is None:
            return await interaction.followup.send(
                embed=embeds.info(
                    "Nothing to Pay",
                    "No nations, kingdoms or individual wallets exist yet.",
                ),
                ephemeral=True,
            )
        await interaction.followup.send(
            embed=embeds.success(
                "Cash Cycle Complete",
                "Cash allowances were paid. The natural payout schedule and the "
                "contract cycle were left untouched. The summary is posted below.",
            ),
            ephemeral=True,
        )
        await self._post_summary(interaction.guild_id, summary)

    # -- default cycle -----------------------------------------------------
    @group.command(
        name="default",
        description="[Admin] Default Cycle — run the full natural payout cycle now.",
    )
    @app_commands.describe(force="Run even when the next natural cycle is not due.")
    @admin_only()
    async def default(self, interaction: discord.Interaction, force: bool = False):
        await interaction.response.defer(thinking=True, ephemeral=True)
        settings = await self.db.get_settings(interaction.guild_id)
        nxt = parse_iso(settings["next_cycle_at"]) if settings else None
        now = dt.datetime.now(dt.timezone.utc)
        if nxt is not None and now < nxt and not force:
            return await interaction.followup.send(
                embed=embeds.info(
                    "Cycle Not Due",
                    f"The next natural cycle is <t:{int(nxt.timestamp())}:R>. "
                    "Use `/payout trigger days:0` to queue it, or pass "
                    "`force: True` to run it now.",
                ),
                ephemeral=True,
            )
        summary = await self._run_cycle(
            interaction.guild_id, natural=True, require_due=not force
        )
        if summary is not None and summary.get("skipped") == "not_due":
            nxt = parse_iso(summary.get("next_cycle_at"))
            when = f"<t:{int(nxt.timestamp())}:R>" if nxt else "later"
            return await interaction.followup.send(
                embed=embeds.info(
                    "Cycle Not Due",
                    "Another natural cycle completed just before this request, "
                    f"so nothing was paid. The next natural cycle is {when}.",
                ),
                ephemeral=True,
            )
        if summary is None:
            return await interaction.followup.send(
                embed=embeds.info(
                    "Nothing to Pay",
                    "No nations, kingdoms or individual wallets exist yet.",
                ),
                ephemeral=True,
            )
        await interaction.followup.send(
            embed=embeds.success(
                "Default Cycle Complete",
                "Cash allowances, dues, commitments, the individual net-worth tax "
                "and the natural cycle advance were applied together. The summary "
                "is posted below.",
            ),
            ephemeral=True,
        )
        await self._post_summary(interaction.guild_id, summary)

    # -- trigger / schedule ------------------------------------------------
    @group.command(
        name="trigger",
        description=(
            "[Admin] Trigger or schedule the natural payout cycle without paying now."
        ),
    )
    @app_commands.describe(
        days=(
            "0 = due now (the 10-minute task runs it); 1-365 = schedule in "
            "that many days."
        ),
    )
    @admin_only()
    async def trigger(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 0, 365] = 0,
    ):
        await interaction.response.defer(thinking=True, ephemeral=True)
        settings = await self.db.get_settings(interaction.guild_id)
        now = dt.datetime.now(dt.timezone.utc)
        when = now + dt.timedelta(days=days)
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S")
        async with self.db.transaction():
            await self.db.set_cycle_times(
                interaction.guild_id,
                settings["last_cycle_at"] if settings else None,
                stamp,
            )
            await self.db.log_tx(
                guild_id=interaction.guild_id,
                kind="config",
                actor_id=interaction.user.id,
                note=(
                    "Natural payout cycle "
                    + ("queued now" if days == 0 else f"scheduled in {days} day(s)")
                    + f" (next={stamp})"
                ),
            )
        if days == 0:
            body = (
                "The natural cycle is **due now**. The 10-minute cycle task will "
                "run it at its next tick; use `/payout default force:true` to run "
                "it immediately."
            )
        else:
            body = (
                f"The next natural cycle is scheduled for "
                f"<t:{int(when.timestamp())}:F> "
                f"(<t:{int(when.timestamp())}:R>). No credits moved."
            )
        await interaction.followup.send(
            embed=embeds.success("Natural Cycle Scheduled", body),
            ephemeral=True,
        )

    # -- cycle engine ------------------------------------------------------
    @tasks.loop(minutes=10)
    async def cycle_task(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self._tick()
        except Exception as exc:  # pragma: no cover - defensive
            self.bot.logger.exception("Payout cycle tick failed: %s", exc)

    @cycle_task.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _tick(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        for settings in await self.db.all_settings():
            guild_id = settings["guild_id"]
            nxt = parse_iso(settings["next_cycle_at"])
            if nxt is None:
                # First run: schedule the first cycle 14 days out.
                await self.db.set_cycle_times(
                    guild_id, settings["last_cycle_at"], (now + dt.timedelta(days=config.PAYOUT_CYCLE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
                )
                continue
            if now >= nxt:
                summary = await self._run_cycle(
                    guild_id, natural=True, require_due=True
                )
                if summary is not None and summary.get("skipped") == "not_due":
                    # An admin command (or another tick in this process) won
                    # the lock and completed this cycle. Do not pay it again
                    # and do not post a second summary.
                    continue
                if summary is not None:
                    await self._post_summary(guild_id, summary)
                elif parse_iso((await self.db.get_settings(guild_id))["next_cycle_at"]) == nxt:
                    # Nothing to pay, so _run_cycle left the schedule alone. Roll
                    # the window forward anyway: otherwise the tick fires again
                    # every 10 minutes and inflates the contract cycle counter.
                    await self.db.set_cycle_times(
                        guild_id,
                        now.strftime("%Y-%m-%dT%H:%M:%S"),
                        (now + dt.timedelta(days=config.PAYOUT_CYCLE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S"),
                    )

    async def _charge_union_dues(
        self, guild_id: int, account_type: str, row, balance: int, payout: int
    ) -> int:
        """Move a member's dues out of their balance into the Union treasury.

        Dues are a share of the member's **whole balance** as it stands after
        this cycle's payout is credited — not a share of the payout alone.
        Runs inside the caller's payout transaction. Returns the amount charged.
        """
        if balance <= 0:
            return 0
        if not await self.db.is_union_member(guild_id, account_type, row["id"]):
            return 0
        tax = union_dues(balance, account_type)
        if tax <= 0:
            return 0
        pct = config.union_tax_label(account_type)

        table = "nations" if account_type == "nation" else "kingdoms"
        after = await self.db.debit(table, row["id"], tax)
        union_after = await self.db.credit_union(guild_id, tax)
        await self.db.log_tx(
            guild_id=guild_id, kind="union_tax",
            account_type=account_type, account_id=row["id"], account_name=row["name"],
            counterparty_type="union", counterparty_name="Workers' Union",
            amount=-tax, balance_after=after,
            note=f"{pct}% Union dues on a {balance:,} balance after a {payout:,} payout",
        )
        await self.db.log_tx(
            guild_id=guild_id, kind="union_tax",
            account_type="union", account_id=guild_id, account_name="Workers' Union",
            counterparty_type=account_type, counterparty_id=row["id"], counterparty_name=row["name"],
            amount=tax, balance_after=union_after,
            note=f"{pct}% dues from {row['name']}",
        )
        return tax

    async def _run_cycle(
        self, guild_id: int, *, natural: bool, require_due: bool = False
    ):
        """Serialise payout runs, then execute the cash/default engine.

        The 10-minute task and an administrator can ask for a cycle at the same
        moment. The database transaction already makes the money movement
        atomic, but two overlapping runs would both act on the same starting
        snapshot, so the second could pay stale allowances on top of the first.
        This lock keeps runs in one process sequential.

        ``require_due`` repeats the Default Cycle's due-check **after** the lock
        is held. The command and the 10-minute task both pass it, so a run that
        waited behind a winner cannot pay a second full cycle. It is off by
        default because the engine itself is also driven directly by the checks
        and by an admin's deliberate ``force`` run.
        """
        lock = getattr(self, "_cycle_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._cycle_lock = lock
        async with lock:
            if natural and require_due:
                settings = await self.db.get_settings(guild_id)
                nxt = parse_iso(settings["next_cycle_at"]) if settings else None
                if nxt is not None and dt.datetime.now(dt.timezone.utc) < nxt:
                    return {
                        "skipped": "not_due",
                        "next_cycle_at": settings["next_cycle_at"],
                    }
            return await self._run_cycle_locked(guild_id, natural=natural)

    async def _run_cycle_locked(self, guild_id: int, *, natural: bool):
        nations = await self.db.list_nations(guild_id)
        kingdoms = await self.db.list_kingdoms(guild_id)

        # Contract eligibility is tied to NATURAL cycles only. The Cash Cycle
        # pays allowances but never advances the contract cycle or rewrites the
        # payout schedule; the Default Cycle does both.
        # Group member nations by kingdom once (avoids N+1 queries).
        by_kingdom: dict[int, list] = {}
        for n in nations:
            if n["kingdom_id"] is not None:
                by_kingdom.setdefault(n["kingdom_id"], []).append(n)

        nation_rows = []
        kingdom_rows = []
        now = dt.datetime.now(dt.timezone.utc)

        # Credit every account and write the ledger inside ONE transaction so the
        # whole cycle is atomic — a crash cannot pay some accounts but not others.
        async with self.db.transaction():
            # Read the wallet list inside the transaction too. A list read before
            # the lock would tax yesterday's balance; a deleted/recreated wallet
            # could put the individual_tax row on the dead id and break
            # reconciliation. Each wallet is then re-read by (guild, member) so a
            # concurrent /fish or /daily is reflected in the tax base.
            individuals = await self.db.list_individuals(guild_id)
            if not nations and not kingdoms and not individuals:
                return None
            if natural:
                await self.db.advance_contract_cycle(guild_id, natural=True)
                # A supplier contract lives for a fixed number of natural
                # cycles. Expire it at the start of the first cycle past its
                # deadline, before any new contract can claim the slot.
                await factions.expire_crown_contracts(
                    self.db, guild_id, await self.db.current_cycle(guild_id)
                )
            for n in nations:
                result = nation_payout(
                    n["happy_workers"], n["regular_workers"], n["happy_soldiers"], n["regular_soldiers"]
                )
                after = await self.db.add_balance("nations", n["id"], result.total)
                await self.db.log_tx(
                    guild_id=guild_id, kind="payout",
                    account_type="nation", account_id=n["id"], account_name=n["name"],
                    amount=result.total, balance_after=after, note="14-day nation payout",
                )
                crown_tax, after = await factions.apply_crown_tax(
                    self.db,
                    guild_id=guild_id,
                    account_type="nation",
                    account_id=n["id"],
                    result=result,
                    now=now,
                )
                dues = await self._charge_union_dues(guild_id, "nation", n, after, result.total)
                committed = 0
                if result.total > 0:
                    committed = await self.db.commit_balance_to_union(
                        guild_id, "nation", n["id"]
                    )
                nation_rows.append(
                    (
                        n["name"], result.total, after - dues - committed,
                        dues, committed, crown_tax,
                    )
                )

            for k in kingdoms:
                result = kingdom_payout(by_kingdom.get(k["id"], []))
                after = await self.db.add_balance("kingdoms", k["id"], result.total)
                await self.db.log_tx(
                    guild_id=guild_id, kind="payout",
                    account_type="kingdom", account_id=k["id"], account_name=k["name"],
                    amount=result.total, balance_after=after, note="14-day kingdom payout",
                )
                crown_tax, after = await factions.apply_crown_tax(
                    self.db,
                    guild_id=guild_id,
                    account_type="kingdom",
                    account_id=k["id"],
                    result=result,
                    now=now,
                )
                dues = await self._charge_union_dues(guild_id, "kingdom", k, after, result.total)
                committed = 0
                if result.total > 0:
                    committed = await self.db.commit_balance_to_union(
                        guild_id, "kingdom", k["id"]
                    )
                kingdom_rows.append(
                    (
                        k["name"], result.total, after - dues - committed,
                        dues, committed, crown_tax,
                    )
                )

            # Net worth tax: individuals only, on every payout run (Cash and
            # Default), so a 5% charge always accompanies a distribution.
            individual_rows = []
            for snapshot in individuals:
                wallet = await self.db.find_individual(
                    guild_id, int(snapshot["discord_id"])
                )
                if wallet is None:
                    # Erased after the list read: nothing to tax, and no row
                    # may point at the dead wallet id.
                    continue
                owed = individual_tax(int(wallet["balance"]))
                if owed <= 0:
                    continue
                paid, after_wallet = await self.db.tax_individual(
                    guild_id,
                    wallet,
                    owed,
                    to_union=config.INDIVIDUAL_TAX_DESTINATION == "union",
                    note=(
                        f"{config.individual_tax_label()}% net worth tax on "
                        f"{int(wallet['balance']):,}"
                    ),
                )
                if paid:
                    individual_rows.append(
                        (wallet["display_name"] or f"<@{wallet['discord_id']}>", paid, after_wallet)
                    )

            if natural:
                await self.db.set_cycle_times(
                    guild_id,
                    now.strftime("%Y-%m-%dT%H:%M:%S"),
                    (now + dt.timedelta(days=config.PAYOUT_CYCLE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S"),
                )
        return {
            "nations": nation_rows,
            "kingdoms": kingdom_rows,
            "individuals": individual_rows,
            "when": now,
            "mode": "default" if natural else "cash",
            "natural": bool(natural),
        }

    async def _post_summary(self, guild_id: int, summary: dict) -> None:
        settings = await self.db.get_settings(guild_id)
        channel_id = settings["payout_channel_id"]
        if not channel_id:
            self.bot.logger.warning("Guild %s has no payout channel set; summary not posted.", guild_id)
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                self.bot.logger.warning("Payout channel %s not found for guild %s.", channel_id, guild_id)
                return

        mode = summary.get("mode", "default")
        if mode == "cash":
            title = "⚜ Cash Cycle Complete"
            description = (
                "Immediate cash allowances have been credited to the **Royal Bank**. "
                "The natural payout schedule and the contract cycle were **not** "
                "changed.\n\nTo convert to in-game currency, ask an admin to pay you out."
            )
        else:
            title = "⚜ Default Cycle Complete"
            description = (
                "The full natural cycle ran: allowances, Union dues, commitments, "
                "the Crown tax and the individual net-worth tax were applied, and "
                "the next cycle has been scheduled.\n\nTo convert to in-game "
                "currency, ask an admin to pay you out."
            )

        embed = discord.Embed(
            title=title,
            description=description,
            color=config.GOLD_COLOR,
        )
        if summary["nations"]:
            lines = [
                f"🏛️ **{name}** — +{money(amt)} → balance {money(bal)}"
                + (f" · Union dues {money(dues)}" if dues else "")
                + (f" · Crown tax {money(crown_tax)}" if crown_tax else "")
                + (f" · committed {money(committed)}" if committed else "")
                for name, amt, bal, dues, committed, crown_tax in summary["nations"]
            ]
            embed.add_field(name="Nations", value="\n".join(lines)[:1024], inline=False)
        if summary["kingdoms"]:
            lines = [
                f"👑 **{name}** — +{money(amt)} → balance {money(bal)}"
                + (f" · Union dues {money(dues)}" if dues else "")
                + (f" · Crown tax {money(crown_tax)}" if crown_tax else "")
                + (f" · committed {money(committed)}" if committed else "")
                for name, amt, bal, dues, committed, crown_tax in summary["kingdoms"]
            ]
            embed.add_field(name="Kingdoms", value="\n".join(lines)[:1024], inline=False)
        if summary.get("individuals"):
            tax_lines = [
                f"👤 {name} — −{money(paid)} → balance {money(bal)}"
                for name, paid, bal in summary["individuals"]
            ]
            embed.add_field(
                name=f"Net Worth Tax ({config.individual_tax_label()}%)",
                value="\n".join(tax_lines)[:1024],
                inline=False,
            )
        nxt = parse_iso(settings["next_cycle_at"]) if settings else None
        if mode == "cash":
            cycle_line = (
                "Cash Cycle: the natural schedule and contract cycle were left "
                "unchanged."
                + (
                    f" Next natural cycle: <t:{int(nxt.timestamp())}:R>."
                    if nxt
                    else ""
                )
            )
        elif nxt:
            cycle_line = (
                "Default Cycle: contract cycle advanced. "
                f"Next natural cycle: <t:{int(nxt.timestamp())}:R>."
            )
        else:
            cycle_line = "Default Cycle: contract cycle advanced; next cycle not scheduled."
        embed.add_field(name="Cycle", value=cycle_line, inline=False)
        embed.timestamp = summary["when"]
        embed.set_footer(text="⚜ The Royal Court · Royal Bank of the Realm")
        try:
            await channel.send(embed=embed)
        except Exception as exc:  # pragma: no cover
            self.bot.logger.warning("Failed to post payout summary: %s", exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Payout(bot))
