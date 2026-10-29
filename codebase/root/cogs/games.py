"""
cogs/games.py
=============
The Casino: ``/blackjack`` and the house account every wager settles against.

The Casino is the counterparty for gambling, the same way the Workers' Union is
the counterparty for contracts. A losing bet credits it, a winning bet is paid
out of it, and it refuses a wager it cannot cover - so the games move credits
between accounts instead of creating them. Only ``/casino fund`` puts credits in.

``/blackjack`` deals a hand in the channel rather than in private, so the table
has an audience. The stake is taken when the cards are dealt and held by the
house while the hand is played; an abandoned hand is settled at stand when the
view times out, so a stake is never stranded in limbo.

``/poker`` seats the caller at a four-seat Texas Hold'em table against three
house bots. The buy-in is held by the Casino and the player is paid their final
chip stack, including chips left after a losing hand. The table is in memory,
so ``poker_hands`` exists solely so startup can refund a process that died
mid-hand.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import blackjack
import casino
import config
import embeds
import gambling_ui
import poker
import poker_names
from checks import admin_only
from database import AccountMissing, CapExceeded, InsufficientFunds
from utils import money, parse_amount

log = logging.getLogger(__name__)


class BlackjackView(discord.ui.View):
    """Hit or stand, with the author's stake already held by the house."""

    def __init__(self, cog: "Casino", *, guild_id: int, author_id: int, stake: int, wallet) -> None:
        super().__init__(timeout=config.BLACKJACK_TIMEOUT_SECONDS)
        self.cog = cog
        self.guild_id = guild_id
        self.author_id = author_id
        self.stake = stake
        self.wallet = wallet
        self.shoe: list[str] = blackjack.new_shoe()
        self.player = [blackjack.draw(self.shoe), blackjack.draw(self.shoe)]
        self.dealer = [blackjack.draw(self.shoe), blackjack.draw(self.shoe)]
        self.settled = False
        self.hand_id: int | None = None
        self.message: discord.Message | None = None
        self.last_result: str | None = None

    # -- helpers -----------------------------------------------------------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=embeds.error("Not Your Table", "This hand belongs to someone else. Start your own with `/blackjack`."),
                ephemeral=True,
            )
            return False
        return True

    def _locked(self) -> discord.Embed:
        return embeds.info(
            "Hand Closed",
            "This hand is already settled. Deal a new one with `/blackjack`.",
        )

    def _close(self) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()

    async def add_result_reactions(self) -> None:
        """Seed spectator reactions after the result response has been sent."""
        if self.settled and self.last_result is not None:
            await gambling_ui.add_spectator_reactions(
                self.message, good=self.last_result == "win"
            )

    def _table(self, *, reveal: bool, footer: str) -> discord.Embed:
        player = blackjack.hand_label(self.player)
        dealer = (
            blackjack.hand_label(self.dealer)
            if reveal
            else f"{blackjack.visible_dealer(self.dealer)} — showing {blackjack.card_value(self.dealer[0])}"
        )
        embed = discord.Embed(
            title="🂡 Blackjack",
            description=(
                f"👀 **The table is watching.** Only the player can act; "
                f"spectators can react with 🎉 or 💀 after the result.\n\n"
                f"<@{self.author_id}> pushes **{money(self.stake)}** across the felt."
            ),
            color=config.GOLD_COLOR,
        )
        embed.add_field(name="Dealer", value=dealer, inline=True)
        embed.add_field(name="Your Hand", value=player, inline=True)
        embed.add_field(name="Stake", value=money(self.stake), inline=True)
        embed.set_footer(text=footer)
        return embed

    async def settle_hand(self, *, revealed: bool, footer: str) -> discord.Embed | None:
        """Pay the hand out exactly once and build the closing embed.

        Returns ``None`` when the hand was already settled, which happens if the
        timeout and a button press race.
        """
        outcome = blackjack.settle(self.player, self.dealer, stake=self.stake)
        self.last_result = outcome.result
        try:
            async with self.cog.db.transaction():
                if self.hand_id is not None and not await self.cog.db.close_blackjack_hand(self.hand_id):
                    self.settled = True
                    return None
                after, house = await casino.finish_wager(
                    self.cog.db,
                    guild_id=self.guild_id,
                    wallet=self.wallet,
                    stake=self.stake,
                    gross=outcome.gross,
                    outcome=outcome.result,
                    kind="individual_blackjack",
                    actor_id=self.author_id,
                    note=f"Blackjack: {outcome.label}",
                )
        except (CapExceeded, InsufficientFunds, AccountMissing) as exc:
            # The transaction rolled back, so the stake is still held and the
            # hand row is still open. Do not mark the view settled: the player
            # or timeout can retry once the cap/house problem is resolved.
            self.settled = False
            log.warning(
                "blackjack settlement failed (guild=%s user=%s): %s",
                self.guild_id,
                self.author_id,
                exc,
            )
            return embeds.error(
                "Settlement Failed",
                "The Casino could not settle this hand right now. Your stake is still held; "
                "please try again, or ask an admin to check the Casino balance.",
            )
        self.settled = True
        self._close()

        if outcome.result == "win":
            tint, head = config.GOLD_COLOR, f"You Win — {outcome.label}"
        elif outcome.result == "push":
            tint, head = discord.Color.blurple(), "Push — Stake Returned"
        else:
            tint, head = discord.Color.dark_red(), f"You Lose — {outcome.label}"

        embed = self._table(
            reveal=revealed,
            footer=f"{head} · Balance {money(after)} · Casino {money(house)}",
        )
        embed.color = tint
        embed.add_field(
            name="Result",
            value=(
                f"**{head}**\n"
                f"Stake: {money(self.stake)}\n"
                f"Returned: {money(outcome.gross)}\n"
                f"Net: {outcome.net:+,}\n"
                f"Balance: **{money(after)}** · Casino: **{money(house)}**"
            ),
            inline=False,
        )
        return embed

    def _play_dealer(self) -> None:
        while blackjack.dealer_should_hit(self.dealer):
            self.dealer.append(blackjack.draw(self.shoe))

    # -- buttons -----------------------------------------------------------
    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="🃏")
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.settled:
            return await interaction.response.send_message(embed=self._locked(), ephemeral=True)

        self.player.append(blackjack.draw(self.shoe))
        # A bust settles immediately: the hand cannot be improved and the dealer
        # never draws against a dead hand.
        if blackjack.is_bust(self.player):
            embed = await self.settle_hand(revealed=True, footer="Bust")
            await interaction.response.edit_message(
                embed=embed or self._locked(), view=self
            )
            await self.add_result_reactions()
            return
        if blackjack.hand_value(self.player) == 21:
            self._play_dealer()
            embed = await self.settle_hand(revealed=True, footer="Twenty-one")
            await interaction.response.edit_message(
                embed=embed or self._locked(), view=self
            )
            await self.add_result_reactions()
            return
        await interaction.response.edit_message(
            embed=self._table(reveal=False, footer="Hit or stand?"), view=self
        )

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, emoji="✋")
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.settled:
            return await interaction.response.send_message(embed=self._locked(), ephemeral=True)
        self._play_dealer()
        embed = await self.settle_hand(revealed=True, footer="Dealer stands")
        await interaction.response.edit_message(embed=embed or self._locked(), view=self)
        await self.add_result_reactions()

    # -- timeout -----------------------------------------------------------
    async def on_timeout(self) -> None:
        """Settle an abandoned hand at stand rather than leaving the stake held."""
        if self.settled or self.message is None:
            return
        try:
            self._play_dealer()
            embed = await self.settle_hand(revealed=True, footer="Timed out — stood automatically")
            if embed is None:
                return
        except Exception:
            # The settlement is a database transaction, so a failure here leaves
            # the stake with the house and no result recorded. That is worth a
            # log line, not silence.
            log.exception("blackjack timeout settlement failed (guild=%s user=%s)", self.guild_id, self.author_id)
            return
        try:
            await self.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            # The table message was deleted; the payout already stands.
            pass
        await self.add_result_reactions()

class PokerRaiseModal(discord.ui.Modal, title="Raise the Table"):
    """A modal because Discord buttons cannot collect a free-form raise."""

    amount = discord.ui.TextInput(
        label="Raise to (total chips)",
        placeholder="e.g. 600 — the total you want your street bet to be",
        min_length=1,
        max_length=20,
    )

    def __init__(self, view: "PokerTableView") -> None:
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.view.player_action(interaction, "raise", str(self.amount.value))


class PokerTableView(discord.ui.View):
    """One public Texas Hold'em table: player seat 0 vs three house bots."""

    def __init__(
        self,
        cog: "Casino",
        *,
        guild_id: int,
        author_id: int,
        stake: int,
        wallet,
        game: poker.Holdem,
    ) -> None:
        super().__init__(timeout=config.POKER_TIMEOUT_SECONDS)
        self.cog = cog
        self.guild_id = guild_id
        self.author_id = author_id
        self.stake = stake
        self.wallet = wallet
        self.game = game
        self.table_id: int | None = None
        self.message: discord.Message | None = None
        self.settled = False
        self.last_result: str | None = None

    # -- helpers -----------------------------------------------------------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=embeds.error(
                    "Not Your Table",
                    "This table belongs to someone else. Start your own with `/poker`.",
                ),
                ephemeral=True,
            )
            return False
        return True

    def _locked(self) -> discord.Embed:
        return embeds.info(
            "Table Closed",
            "This hand is already settled. Sit down again with `/poker`.",
        )

    def _close(self) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()

    def _sync_buttons(self) -> None:
        """Enable exactly the actions the player can take right now."""
        can_act = (
            not self.settled
            and not self.game.complete
            and self.game.actor == 0
        )
        for child in self.children:
            child.disabled = not can_act
        if not can_act:
            return
        options = self.game.legal_actions()
        player = self.game.player()
        to_call = self.game.to_call(player)
        self.fold_button.disabled = "fold" not in options
        self.call_button.disabled = not ({"check", "call"} & set(options))
        self.call_button.label = f"Call {money(to_call)}" if to_call else "Check"
        self.raise_button.disabled = "raise" not in options
        self.raise_button.label = f"Raise (min {money(self.game.min_raise_to(player))})"
        self.all_in_button.disabled = "all_in" not in options

    def _footer(self) -> str:
        return (
            f"Stake {money(self.stake)} · blinds {self.game.small_blind:,}/"
            f"{self.game.big_blind:,} · only the player can act"
        )

    def _table_embed(self) -> discord.Embed:
        return gambling_ui.poker_table(
            user_mention=f"<@{self.author_id}>",
            stake=self.stake,
            game=self.game,
            footer=self._footer(),
        )

    async def _edit_message(self, embed: discord.Embed) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            log.exception(
                "could not edit poker table (guild=%s user=%s)",
                self.guild_id,
                self.author_id,
            )

    async def add_result_reactions(self) -> None:
        if self.settled and self.last_result is not None:
            await gambling_ui.add_spectator_reactions(
                self.message, good=self.last_result == "win"
            )



    # -- buttons -----------------------------------------------------------
    async def _maybe_blocked(self, interaction: discord.Interaction) -> bool:
        if self.settled or self.game.complete:
            await interaction.response.send_message(
                embed=self._locked(), ephemeral=True
            )
            return True
        if self.game.actor != 0:
            await interaction.response.send_message(
                embed=embeds.info(
                    "Bots Are Acting",
                    "The house is still thinking. Wait for your turn before acting.",
                ),
                ephemeral=True,
            )
            return True
        return False

    async def player_action(
        self,
        interaction: discord.Interaction,
        action: str,
        raw_amount: str | None = None,
    ) -> None:
        """Apply one player action, then let the bots answer."""
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Not Your Table", "Only the player who sat down can act here."
                ),
                ephemeral=True,
            )
        if await self._maybe_blocked(interaction):
            return

        options = self.game.legal_actions()
        if action not in options:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Action Not Available",
                    f"`{action}` is not legal here. Available: {', '.join(options)}.",
                ),
                ephemeral=True,
            )

        amount: int | None = None
        if action == "raise":
            player = self.game.player()
            try:
                target = int(
                    (raw_amount or "").strip().replace(",", "").replace("_", "")
                )
            except ValueError:
                return await interaction.response.send_message(
                    embed=embeds.error(
                        "Invalid Raise",
                        "Enter a whole number for the total you want to raise to.",
                    ),
                    ephemeral=True,
                )
            low, high = self.game.min_raise_to(player), self.game.max_raise_to(player)
            if target < low or target > high:
                return await interaction.response.send_message(
                    embed=embeds.error(
                        "Raise Out of Range",
                        f"Raise to between **{money(low)}** and **{money(high)}**.",
                    ),
                    ephemeral=True,
                )
            amount = target

        try:
            self.game.act(0, action, amount)
        except poker.PokerError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Action Refused", str(exc)), ephemeral=True
            )

        # Acknowledge the player's action immediately. Bot turns are resolved
        # after this response so a slow table edit cannot expire the interaction.
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._table_embed(), view=self)

        try:
            self.game.run_bots()
        except poker.PokerError:
            log.exception(
                "poker bot loop failed (guild=%s user=%s)",
                self.guild_id,
                self.author_id,
            )
            await self._edit_message(
                embeds.error(
                    "Table Paused",
                    "The house could not finish its turn. The buy-in is still held; "
                    "start a new table or ask an admin to check the log.",
                )
            )
            return

        if self.game.complete:
            embed = await self.settle_table()
            await self._edit_message(embed or self._locked())
        else:
            self._sync_buttons()
            await self._edit_message(self._table_embed())
        await self.add_result_reactions()

    @discord.ui.button(label="Fold", style=discord.ButtonStyle.danger, emoji="🏳️")
    async def fold_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.player_action(interaction, "fold")

    @discord.ui.button(label="Check", style=discord.ButtonStyle.primary, emoji="✅")
    async def call_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._maybe_blocked(interaction):
            return
        options = self.game.legal_actions()
        await self.player_action(interaction, "check" if "check" in options else "call")

    @discord.ui.button(label="Raise", style=discord.ButtonStyle.secondary, emoji="📈")
    async def raise_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._maybe_blocked(interaction):
            return
        if "raise" not in self.game.legal_actions():
            return await interaction.response.send_message(
                embed=embeds.error("Raise Not Available", "You cannot raise here."),
                ephemeral=True,
            )
        await interaction.response.send_modal(PokerRaiseModal(self))

    @discord.ui.button(label="All-In", style=discord.ButtonStyle.danger, emoji="🔥")
    async def all_in_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.player_action(interaction, "all_in")

    # -- settlement --------------------------------------------------------
    async def settle_table(self, *, footer: str | None = None) -> discord.Embed | None:
        """Pay the player's final stack and close the table exactly once."""
        if self.settled:
            return None
        if not self.game.complete:
            return embeds.error("Hand Not Finished", "The table is still in play.")
        final_stack = int(self.game.player_stack)
        outcome = self.game.outcome()
        try:
            async with self.cog.db.transaction():
                if self.table_id is not None and not await self.cog.db.close_poker_hand(
                    self.table_id
                ):
                    self.settled = True
                    return None
                after, house = await casino.finish_wager(
                    self.cog.db,
                    guild_id=self.guild_id,
                    wallet=self.wallet,
                    stake=self.stake,
                    gross=final_stack,
                    outcome=outcome,
                    kind="individual_poker",
                    actor_id=self.author_id,
                    note=f"Poker: {outcome}, final stack {final_stack:,}",
                    payout_override=final_stack,
                )
        except (CapExceeded, InsufficientFunds, AccountMissing) as exc:
            # The transaction rolled back, so the table row is still open and
            # the buy-in is still held. Keep the view alive for a retry.
            self.settled = False
            log.warning(
                "poker settlement failed (guild=%s user=%s): %s",
                self.guild_id,
                self.author_id,
                exc,
            )
            return embeds.error(
                "Settlement Failed",
                "The Casino could not settle this table right now. Your buy-in is "
                "still held; please try again or ask an admin to check the Casino.",
            )
        except Exception:
            self.settled = False
            log.exception(
                "poker settlement crashed (guild=%s user=%s)",
                self.guild_id,
                self.author_id,
            )
            return embeds.error(
                "Settlement Failed",
                "The table could not be settled. Your buy-in is still held; ask an admin.",
            )

        self.settled = True
        self.last_result = outcome
        self._close()
        return gambling_ui.poker_result(
            user_mention=f"<@{self.author_id}>",
            stake=self.stake,
            final_stack=final_stack,
            balance=after,
            house=house,
            game=self.game,
            outcome=outcome,
            footer=footer
            or f"Settled · Casino stack {money(house)} · every hand is public",
        )

    async def on_timeout(self) -> None:
        """Fold an abandoned table, then settle it so the buy-in is not stranded."""
        if self.settled or self.message is None:
            return
        if not self.game.complete:
            try:
                if self.game.actor == 0 and "fold" in self.game.legal_actions():
                    self.game.act(0, "fold")
                elif self.game.actor is not None:
                    self.game.run_bots()
            except Exception:
                log.exception(
                    "poker timeout could not fold (guild=%s user=%s)",
                    self.guild_id,
                    self.author_id,
                )
                return
        embed = await self.settle_table(footer="Timed out — folded automatically")
        if embed is None:
            return
        await self._edit_message(embed)
        await self.add_result_reactions()




class Casino(commands.Cog):
    """🎰 The Casino: blackjack, poker, and the house account behind every wager."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Warm the Minecraft-themed name pool with Gilbert's AI credentials.
        # This is background-only; /poker never waits on the network.
        poker_names.start_refresh()

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    group = app_commands.Group(name="casino", description="🎰 The Casino house account.")

    # ------------------------------------------------------------------
    # Blackjack
    # ------------------------------------------------------------------
    @app_commands.command(
        name="blackjack",
        description="Play a hand of blackjack against the Casino. Winner takes the house's credits.",
    )
    @app_commands.describe(
        amount="Credits to stake, or all / half / quarter of your balance.",
    )
    async def blackjack(self, interaction: discord.Interaction, amount: str):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "The Casino only runs inside a server."),
                ephemeral=True,
            )

        wallet = await self.db.find_individual(interaction.guild_id, interaction.user.id)
        if wallet is None:
            return await interaction.response.send_message(
                embed=embeds.info(
                    "No Wallet",
                    "You have no individual balance yet. Claim your first credits with `/daily`.",
                ),
                ephemeral=True,
            )

        balance = int(wallet["balance"])
        try:
            stake = parse_amount(amount, balance)
        except ValueError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Amount", str(exc)), ephemeral=True
            )

        if stake > config.BLACKJACK_MAX_STAKE:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Bet Refused",
                    f"The table maximum is **{money(config.BLACKJACK_MAX_STAKE)}**.",
                ),
                ephemeral=True,
            )

        house = await self.db.get_casino_balance(interaction.guild_id)
        problem = casino.wager_problem(
            stake=stake,
            balance=balance,
            gross=blackjack.max_gross(stake),
            house=house,
            minimum=config.BLACKJACK_MIN_STAKE,
        )
        if problem:
            return await interaction.response.send_message(
                embed=embeds.error("Bet Refused", problem), ephemeral=True
            )

        view = BlackjackView(
            self, guild_id=interaction.guild_id, author_id=interaction.user.id,
            stake=stake, wallet=wallet,
        )

        try:
            # The stake moves into the house before the cards are shown, so the
            # player cannot spend it mid-hand and leave the loss unpaid.
            async with self.db.transaction():
                _after, house_after = await casino.begin_wager(
                    self.db,
                    guild_id=interaction.guild_id,
                    wallet=wallet,
                    stake=stake,
                    kind="individual_blackjack",
                    actor_id=interaction.user.id,
                    note="Blackjack stake",
                )
                view.hand_id = await self.db.open_blackjack_hand(
                    guild_id=interaction.guild_id,
                    discord_id=interaction.user.id,
                    individual_id=int(wallet["id"]),
                    stake=stake,
                    player_cards=" ".join(view.player),
                    dealer_cards=" ".join(view.dealer),
                )
        except InsufficientFunds:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Insufficient Funds",
                    f"You only hold **{money(balance)}** in your individual balance.",
                ),
                ephemeral=True,
            )
        except AccountMissing:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "No Wallet",
                    "Your individual wallet was erased before the bet landed. Nothing was staked.",
                ),
                ephemeral=True,
            )
        except CapExceeded:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Bet Refused",
                    "A win would take you past the individual cap. Nothing was staked.",
                ),
                ephemeral=True,
            )

        # A natural is settled on the deal - there is no decision to make.
        natural = blackjack.is_blackjack(view.player) or blackjack.is_blackjack(view.dealer)
        if natural:
            if not blackjack.is_blackjack(view.dealer):
                view._play_dealer()
            embed = await view.settle_hand(revealed=True, footer="Dealt a natural")
            await interaction.response.send_message(embed=embed or view._locked())
            try:
                view.message = await interaction.original_response()
            except Exception:
                view.message = None
            await view.add_result_reactions()
            return

        embed = view._table(
            reveal=False,
            footer=(
                f"House holds {money(house_after)} · hit or stand below · "
                f"blackjack pays 3:2 · dealer stands on all 17s"
            ),
        )
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    # ------------------------------------------------------------------
    # Poker
    # ------------------------------------------------------------------
    @app_commands.command(
        name="poker",
        description="Sit at a four-seat Texas Hold'em table against three house bots.",
    )
    @app_commands.describe(
        amount="Buy-in, or all / half / quarter of your balance.",
    )
    async def poker(self, interaction: discord.Interaction, amount: str):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "The Casino only runs inside a server."),
                ephemeral=True,
            )

        wallet = await self.db.find_individual(interaction.guild_id, interaction.user.id)
        if wallet is None:
            return await interaction.response.send_message(
                embed=embeds.info(
                    "No Wallet",
                    "You have no individual balance yet. Claim your first credits with `/daily`.",
                ),
                ephemeral=True,
            )

        balance = int(wallet["balance"])
        try:
            stake = parse_amount(amount, balance)
        except ValueError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Amount", str(exc)), ephemeral=True
            )

        if stake > config.POKER_MAX_STAKE:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Buy-In Refused",
                    f"The high table maximum is **{money(config.POKER_MAX_STAKE)}**.",
                ),
                ephemeral=True,
            )

        house = await self.db.get_casino_balance(interaction.guild_id)
        problem = casino.wager_problem(
            stake=stake,
            balance=balance,
            gross=poker.max_gross(stake),
            house=house,
            minimum=config.POKER_MIN_STAKE,
        )
        if problem:
            return await interaction.response.send_message(
                embed=embeds.error("Buy-In Refused", problem), ephemeral=True
            )

        game = poker.Holdem(
            stake=stake,
            names=poker_names.table_names(len(config.POKER_BOT_NAMES)),
        )
        try:
            # The opening bots act before the table is posted, so the player
            # receives a ready-to-play turn instead of a wall of dead buttons.
            game.run_bots()
        except poker.PokerError:
            log.exception(
                "poker initial bot loop failed (guild=%s user=%s)",
                interaction.guild_id,
                interaction.user.id,
            )
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Table Could Not Open",
                    "The house could not deal a clean hand. Nothing was staked.",
                ),
                ephemeral=True,
            )

        view = PokerTableView(
            self,
            guild_id=interaction.guild_id,
            author_id=interaction.user.id,
            stake=stake,
            wallet=wallet,
            game=game,
        )

        try:
            # The buy-in moves into the house before the table is posted, and
            # the row is written in the same transaction so a restart can refund
            # a table whose buttons vanished with the process.
            async with self.db.transaction():
                _after, _house_after = await casino.begin_wager(
                    self.db,
                    guild_id=interaction.guild_id,
                    wallet=wallet,
                    stake=stake,
                    kind="individual_poker",
                    actor_id=interaction.user.id,
                    note="Poker buy-in",
                )
                view.table_id = await self.db.open_poker_hand(
                    guild_id=interaction.guild_id,
                    discord_id=interaction.user.id,
                    individual_id=int(wallet["id"]),
                    stake=stake,
                )
        except InsufficientFunds:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Insufficient Funds",
                    f"You only hold **{money(balance)}** in your individual balance.",
                ),
                ephemeral=True,
            )
        except AccountMissing:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "No Wallet",
                    "Your individual wallet was erased before the buy-in landed. Nothing was staked.",
                ),
                ephemeral=True,
            )
        except CapExceeded:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Buy-In Refused",
                    "A winning table could take you past the individual cap. Nothing was staked.",
                ),
                ephemeral=True,
            )

        if game.complete:
            embed = await view.settle_table()
            await interaction.response.send_message(embed=embed or view._locked())
            try:
                view.message = await interaction.original_response()
            except Exception:
                view.message = None
            await view.add_result_reactions()
            return

        view._sync_buttons()
        await interaction.response.send_message(
            embed=view._table_embed(),
            view=view,
        )
        try:
            view.message = await interaction.original_response()
        except Exception:
            view.message = None


    # ------------------------------------------------------------------
    # House account
    # ------------------------------------------------------------------
    @group.command(name="balance", description="Show the Casino balance that every wager settles against.")
    async def balance(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "The Casino only runs inside a server."),
                ephemeral=True,
            )
        house = await self.db.get_casino_balance(interaction.guild_id)
        embed = gambling_ui.casino_floor(house)
        await interaction.response.send_message(embed=embed)

    @group.command(name="fund", description="[Admin] Credit the Casino so it can pay winning bets.")
    @app_commands.describe(amount="Credits to add to the Casino.")
    @admin_only()
    async def fund(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, None]):
        async with self.db.transaction():
            after = await self.db.credit_casino(interaction.guild_id, amount)
            await self.db.log_tx(
                guild_id=interaction.guild_id,
                kind="casino_fund",
                account_type="casino",
                account_id=interaction.guild_id,
                account_name="The Casino",
                amount=amount,
                balance_after=after,
                actor_id=interaction.user.id,
                note="Admin funded the Casino",
            )
        await interaction.response.send_message(
            embed=embeds.success(
                "Casino Funded",
                f"**{money(amount)}** added to the Casino.\nNew balance: **{money(after)}**",
            )
        )

    @group.command(name="withdraw", description="[Admin] Remove credits from the Casino.")
    @app_commands.describe(amount="Credits to take out of the Casino.")
    @admin_only()
    async def withdraw(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, None]):
        try:
            async with self.db.transaction():
                after = await self.db.debit_casino(interaction.guild_id, amount)
                await self.db.log_tx(
                    guild_id=interaction.guild_id,
                    kind="casino_withdraw",
                    account_type="casino",
                    account_id=interaction.guild_id,
                    account_name="The Casino",
                    amount=-amount,
                    balance_after=after,
                    actor_id=interaction.user.id,
                    note="Admin withdrew from the Casino",
                )
        except InsufficientFunds:
            house = await self.db.get_casino_balance(interaction.guild_id)
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Insufficient Funds",
                    f"The Casino holds **{money(house)}**. Nothing was withdrawn.",
                ),
                ephemeral=True,
            )
        await interaction.response.send_message(
            embed=embeds.success(
                "Casino Withdrawn",
                f"**{money(amount)}** taken out of the Casino.\nNew balance: **{money(after)}**",
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Casino(bot))
