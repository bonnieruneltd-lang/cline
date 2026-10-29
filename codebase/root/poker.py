"""
poker.py
========
Texas Hold'em for one player against three house bots, as pure rules with no
Discord and no database.

Design
------
Four seats, each starting with the same stack (the player's ``stake``). The
player's stack is debited from their wallet when the hand is dealt; the three
bot stacks are funded by the Casino, so the whole table is settled against the
house exactly like blackjack: the stake goes in, the player's *final chip count*
comes back out. Because the chips in play total ``4 x stake``, the house has to
reserve that much before dealing.

Everything here is deterministic under a seeded ``random.Random``, so
``checks/check_poker.py`` can replay hands exactly. The bots are deliberately
simple: a strength estimate compared against the pot odds, plus a small jitter so
they are not readable.

Hand ranking
------------
There are only 21 five-card combinations in seven cards, so :func:`hand_rank`
scores all of them and takes the best. That is slower than a lookup table and
far harder to get wrong, which is the right trade for a project this size.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import config

SUITS = ("♠", "♥", "♦", "♣")
RANKS = ("2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A")
RANK_VALUE = {rank: index + 2 for index, rank in enumerate(RANKS)}

HIGH_CARD = 0
PAIR = 1
TWO_PAIR = 2
TRIPS = 3
STRAIGHT = 4
FLUSH = 5
FULL_HOUSE = 6
QUADS = 7
STRAIGHT_FLUSH = 8

CATEGORY_NAMES = {
    HIGH_CARD: "High Card",
    PAIR: "Pair",
    TWO_PAIR: "Two Pair",
    TRIPS: "Three of a Kind",
    STRAIGHT: "Straight",
    FLUSH: "Flush",
    FULL_HOUSE: "Full House",
    QUADS: "Four of a Kind",
    STRAIGHT_FLUSH: "Straight Flush",
}

# How good each category is, for the bots only. The player is never told.
_STRENGTH = {
    HIGH_CARD: 0.18,
    PAIR: 0.42,
    TWO_PAIR: 0.62,
    TRIPS: 0.78,
    STRAIGHT: 0.86,
    FLUSH: 0.89,
    FULL_HOUSE: 0.94,
    QUADS: 0.97,
    STRAIGHT_FLUSH: 0.99,
}

PREFLOP = "preflop"
FLOP = "flop"
TURN = "turn"
RIVER = "river"
SHOWDOWN = "showdown"
COMPLETE = "complete"

_STREETS = (PREFLOP, FLOP, TURN, RIVER)


class PokerError(Exception):
    """An action that the rules do not allow from this seat right now."""


def new_deck(rng: Optional[random.Random] = None) -> List[str]:
    """A shuffled 52-card deck. Cards are drawn with ``list.pop()``."""
    deck = [rank + suit for rank in RANKS for suit in SUITS]
    (rng or random).shuffle(deck)
    return deck


def card_value(card: str) -> int:
    """2-14. ``10♠`` -> 10, ``A♠`` -> 14."""
    return RANK_VALUE[card[:-1]]


def _straight_high(ranks: Sequence[int]) -> Optional[int]:
    """The top card of a five-rank straight, or ``None``. Handles the wheel."""
    distinct = sorted(set(ranks), reverse=True)
    if len(distinct) != 5:
        return None
    if distinct[0] - distinct[4] == 4:
        return distinct[0]
    # A-2-3-4-5: the ace is low and the straight is a five-high.
    if distinct == [14, 5, 4, 3, 2]:
        return 5
    return None


def score_five(cards: Sequence[str]) -> Tuple[int, ...]:
    """A sortable score for exactly five cards. Bigger is better."""
    if len(cards) != 5:
        raise ValueError(f"score_five needs exactly 5 cards, got {len(cards)}")
    ranks = sorted((card_value(c) for c in cards), reverse=True)
    flush = len({c[-1] for c in cards}) == 1
    counts = {}
    for value in ranks:
        counts[value] = counts.get(value, 0) + 1
    # Sort groups by count then by rank, so quads come before the kicker.
    groups = sorted(counts.items(), key=lambda item: (-item[1], -item[0]))
    shape = tuple(count for _, count in groups)
    by_rank = tuple(value for value, _ in groups)
    straight = _straight_high(ranks)

    if flush and straight:
        return (STRAIGHT_FLUSH, straight)
    if shape == (4, 1):
        return (QUADS, by_rank[0], by_rank[1])
    if shape == (3, 2):
        return (FULL_HOUSE, by_rank[0], by_rank[1])
    if flush:
        return (FLUSH,) + tuple(ranks)
    if straight:
        return (STRAIGHT, straight)
    if shape == (3, 1, 1):
        return (TRIPS, by_rank[0], by_rank[1], by_rank[2])
    if shape == (2, 2, 1):
        return (TWO_PAIR, by_rank[0], by_rank[1], by_rank[2])
    if shape == (2, 1, 1, 1):
        return (PAIR, by_rank[0], by_rank[1], by_rank[2], by_rank[3])
    return (HIGH_CARD,) + tuple(ranks)


def hand_rank(cards: Sequence[str]) -> Tuple[int, ...]:
    """The best five-card score available from five to seven cards."""
    if len(cards) < 5:
        raise ValueError(f"need at least 5 cards to rank, got {len(cards)}")
    if len(cards) == 5:
        return score_five(cards)
    return max(score_five(combo) for combo in itertools.combinations(cards, 5))


def category(rank: Sequence[int]) -> int:
    return int(rank[0])


def describe(rank: Sequence[int]) -> str:
    """``Two Pair, Kings and Nines`` - readable, without naming every kicker."""
    name = CATEGORY_NAMES[category(rank)]
    rest = list(rank[1:])

    def label(value: int) -> str:
        return {14: "Aces", 13: "Kings", 12: "Queens", 11: "Jacks"}.get(
            value, f"{value}s"
        )

    if category(rank) in (STRAIGHT, STRAIGHT_FLUSH):
        return f"{name}, {label(rest[0])} high"
    if category(rank) == QUADS:
        return f"{name}, {label(rest[0])}"
    if category(rank) == FULL_HOUSE:
        return f"{name}, {label(rest[0])} full of {label(rest[1])}"
    if category(rank) == TWO_PAIR:
        return f"{name}, {label(rest[0])} and {label(rest[1])}"
    if category(rank) in (PAIR, TRIPS):
        return f"{name} of {label(rest[0])}"
    return f"{name}, {label(rest[0])} high"


def preflop_strength(hole: Sequence[str]) -> float:
    """0-1 estimate used by the bots before any community cards are out."""
    high, low = sorted((card_value(c) for c in hole), reverse=True)
    if high == low:
        return min(0.95, 0.50 + (high - 2) / 12 * 0.45)
    score = (high - 2) / 12 * 0.45 + (low - 2) / 12 * 0.20
    if hole[0][-1] == hole[1][-1]:
        score += 0.06
    if high - low == 1:
        score += 0.05
    if high == 14:
        score += 0.05
    return max(0.0, min(1.0, score))


def strength(hole: Sequence[str], board: Sequence[str]) -> float:
    """0-1 estimate of the best hand a seat holds right now."""
    if len(board) + len(hole) < 5:
        return preflop_strength(hole)
    rank = hand_rank(list(hole) + list(board))
    score = _STRENGTH[category(rank)]
    # A tiny nudge for the kicker keeps the bots from playing every pair alike.
    tiebreak = rank[1] if len(rank) > 1 else 0
    return min(1.0, score + (tiebreak / 14) * 0.04)


@dataclass
class Seat:
    index: int
    name: str
    is_player: bool
    stack: int
    hole: List[str] = field(default_factory=list)
    folded: bool = False
    all_in: bool = False
    committed: int = 0      # chips put in on the current street
    total: int = 0          # chips put in over the whole hand

    @property
    def active(self) -> bool:
        """Still in the hand (not folded)."""
        return not self.folded


def max_gross(stake: int) -> int:
    """Worst case the house must cover: the player wins every chip at the table."""
    return int(stake) * (1 + len(config.POKER_BOT_NAMES))


def blinds_for(stake: int) -> Tuple[int, int]:
    """``(small, big)`` for a table whose stacks are ``stake`` deep."""
    small = max(1, int(stake) // config.POKER_BLINDS_DIVISOR)
    return small, small * 2


class Holdem:
    """One hand of no-limit Texas Hold'em, four handed.

    Seats are always in the same order: seat 0 is the player, seats 1-3 are the
    bots. The button sits on the player, so the blinds are seats 1 and 2 and the
    player is second to act before the flop and second after it.
    """

    def __init__(
        self,
        *,
        stake: int,
        names: Sequence[str] = config.POKER_BOT_NAMES,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.stake = int(stake)
        self.rng = rng or random.Random()
        self.deck = new_deck(self.rng)
        self.board: List[str] = []
        self.street = PREFLOP
        self.button = 0
        self.log: List[str] = []

        seats = [Seat(0, "You", True, self.stake)]
        for offset, name in enumerate(names, start=1):
            seats.append(Seat(offset, name, False, self.stake))
        self.seats: List[Seat] = seats

        self.small_blind, self.big_blind = blinds_for(self.stake)
        self.current_bet = 0
        self.min_raise = self.big_blind
        self.actor: Optional[int] = None
        self.to_act: set[int] = set()
        # Seats that have already acted and are facing a short all-in. They may
        # call or fold, but a short all-in does not reopen raising for them.
        # A full raise clears this set; a new street clears it too.
        self.raise_locked: set[int] = set()
        self.complete = False
        self.winnings: List[int] = [0] * len(self.seats)
        self.showdown_ranks: Dict[int, Tuple[int, ...]] = {}

        self._deal()
        self._post_blinds()
        self._start_preflop()

    # -- setup -------------------------------------------------------------
    def _deal(self) -> None:
        for _ in range(2):
            for seat in self.seats:
                seat.hole.append(self.deck.pop())

    def _put_in(self, seat: Seat, amount: int) -> int:
        """Move chips from a stack into the pot, capped by the stack."""
        amount = max(0, min(int(amount), seat.stack))
        seat.stack -= amount
        seat.committed += amount
        seat.total += amount
        if seat.stack == 0:
            seat.all_in = True
        return amount

    def _post_blinds(self) -> None:
        self.small_blind_seat = self._seat_after(self.button)
        self.big_blind_seat = self._seat_after(self.small_blind_seat)
        small_seat = self.seats[self.small_blind_seat]
        big_seat = self.seats[self.big_blind_seat]
        self._put_in(small_seat, self.small_blind)
        self._put_in(big_seat, self.big_blind)
        self.log.append(
            f"{small_seat.name} posts {self.small_blind:,} · "
            f"{big_seat.name} posts {self.big_blind:,}"
        )

    def _start_preflop(self) -> None:
        """Preflop is the one street whose pot is not empty when it opens.

        The blinds are already committed, so ``committed`` must survive and
        ``current_bet`` starts at the big blind. Action begins with the seat
        left of the big blind; the blinds still get their option.
        """
        self.street = PREFLOP
        self.current_bet = max(seat.committed for seat in self.seats)
        self.min_raise = self.big_blind
        self.raise_locked.clear()
        self.to_act = {s.index for s in self.seats if self.can_act(s)}
        if len(self.to_act) < 2:
            self._run_out()
            return
        self.actor = self._next_actor_from(self._seat_after(self.big_blind_seat))
        if self.actor is None:
            self._advance_street()

    def _seat_after(self, index: int, *, active_only: bool = False) -> int:
        count = len(self.seats)
        for step in range(1, count + 1):
            candidate = (index + step) % count
            if active_only and not self.seats[candidate].active:
                continue
            return candidate
        return index

    # -- state -------------------------------------------------------------
    @property
    def pot(self) -> int:
        """Everything in the middle: blinds, calls and raises so far."""
        return sum(seat.total for seat in self.seats)

    @property
    def round_pot(self) -> int:
        """Chips committed on the current street."""
        return sum(seat.committed for seat in self.seats)

    def seats_in_hand(self) -> List[Seat]:
        return [seat for seat in self.seats if seat.active]

    def can_act(self, seat: Seat) -> bool:
        return seat.active and not seat.all_in and seat.stack > 0

    def to_call(self, seat: Seat) -> int:
        return max(0, self.current_bet - seat.committed)

    def min_raise_to(self, seat: Seat) -> int:
        """The smallest legal "raise to" total for this seat."""
        return min(self.current_bet + self.min_raise, seat.committed + seat.stack)

    def max_raise_to(self, seat: Seat) -> int:
        return seat.committed + seat.stack

    def player(self) -> Seat:
        return self.seats[0]

    # -- streets -----------------------------------------------------------
    def _begin_street(self, street: str, *, first: Optional[int] = None) -> None:
        self.street = street
        self.current_bet = 0
        self.min_raise = self.big_blind
        self.raise_locked.clear()
        for seat in self.seats:
            seat.committed = 0

        if street == FLOP:
            self.board.extend(self._draw(3))
            self.log.append(f"Flop: {' '.join(self.board)}")
        elif street == TURN:
            self.board.extend(self._draw(1))
            self.log.append(f"Turn: {self.board[-1]}")
        elif street == RIVER:
            self.board.extend(self._draw(1))
            self.log.append(f"River: {self.board[-1]}")

        self.to_act = {s.index for s in self.seats if self.can_act(s)}
        if len(self.to_act) < 2:
            # Nobody can act: the rest of the board runs out and we show down.
            self._run_out()
            return
        start = self._seat_after(self.button) if first is None else first
        self.actor = self._next_actor_from(start)
        if self.actor is None:
            self._advance_street()

    def _draw(self, count: int) -> List[str]:
        # A fresh deck is only possible if the shoe somehow ran out mid-hand,
        # which cannot happen four handed, but the guard keeps draw() total.
        if len(self.deck) < count:
            self.deck.extend(new_deck(self.rng))
        return [self.deck.pop() for _ in range(count)]

    def _next_actor_from(self, start: int) -> Optional[int]:
        for step in range(len(self.seats)):
            candidate = (start + step) % len(self.seats)
            if candidate in self.to_act:
                return candidate
        return None

    def _advance_street(self) -> None:
        if self.street == RIVER:
            return self._showdown()
        self._begin_street(_STREETS[_STREETS.index(self.street) + 1])

    def _run_out(self) -> None:
        """Deal whatever is missing and settle - used when betting is over."""
        while len(self.board) < 5:
            self.board.extend(self._draw(3 if len(self.board) == 0 else 1))
            self.log.append(f"Board: {' '.join(self.board)}")
        self._showdown()

    # -- actions -----------------------------------------------------------
    def legal_actions(self, seat_index: Optional[int] = None) -> List[str]:
        index = self.actor if seat_index is None else seat_index
        if index is None or self.complete:
            return []
        seat = self.seats[index]
        if not self.can_act(seat):
            return []
        options = ["fold"]
        if self.to_call(seat) == 0:
            options.append("check")
        else:
            options.append("call")
        if self.max_raise_to(seat) > self.current_bet and index not in self.raise_locked:
            options.append("raise")
        if self.max_raise_to(seat) > self.current_bet and index not in self.raise_locked:
            options.append("all_in")
        return options

    def act(self, seat_index: int, action: str, amount: Optional[int] = None) -> List[str]:
        """Apply one action. Returns the log lines it produced."""
        if self.complete:
            raise PokerError("The hand is already over.")
        if self.actor != seat_index:
            raise PokerError("It is not that seat's turn.")
        seat = self.seats[seat_index]
        if not self.can_act(seat):
            raise PokerError("That seat cannot act.")
        if action not in self.legal_actions(seat_index):
            raise PokerError(f"{action} is not legal here.")

        start = len(self.log)
        if action == "fold":
            seat.folded = True
            self.log.append(f"{seat.name} folds")
        elif action == "check":
            self.log.append(f"{seat.name} checks")
        elif action == "call":
            paid = self._put_in(seat, self.to_call(seat))
            self.log.append(f"{seat.name} calls {paid:,}")
        elif action in ("raise", "all_in"):
            if action == "all_in":
                target = self.max_raise_to(seat)
            else:
                target = self.max_raise_to(seat) if amount is None else int(amount)
            target = max(self.min_raise_to(seat), min(target, self.max_raise_to(seat)))
            previous_min_raise = self.min_raise
            paid = self._put_in(seat, target - seat.committed)
            raise_size = seat.committed - self.current_bet
            if raise_size > 0:
                if raise_size >= previous_min_raise:
                    # A full raise reopens action for everyone still able to act.
                    self.min_raise = max(self.min_raise, raise_size)
                    self.raise_locked.clear()
                else:
                    # A short all-in does not reopen raising for seats that have
                    # already acted this street. They still have to answer the
                    # extra amount, so they go back into to_act below, but
                    # legal_actions() withholds raise/all_in from them.
                    self.raise_locked |= {
                        s.index
                        for s in self.seats
                        if self.can_act(s)
                        and s.index != seat_index
                        and s.index not in self.to_act
                    }
            self.current_bet = max(self.current_bet, seat.committed)
            verb = "is all in for" if seat.all_in else "raises to"
            self.log.append(f"{seat.name} {verb} {seat.committed:,} ({paid:,} more)")

        self.to_act.discard(seat_index)
        if action in ("raise", "all_in"):
            # Everyone else still in the hand has to answer the raise. This
            # includes seats that already acted: a short all-in still raises the
            # price for them, even though legal_actions() withholds a re-raise.
            self.to_act |= {
                s.index for s in self.seats if self.can_act(s) and s.index != seat_index
            }

        self._settle_turn(seat_index)
        return self.log[start:]

    def _settle_turn(self, seat_index: int) -> None:
        if len(self.seats_in_hand()) <= 1:
            return self._award_to_last_standing()
        if not self.to_act:
            return self._advance_street()
        self.actor = self._next_actor_from(self._seat_after(seat_index))
        if self.actor is None:
            self._advance_street()

    # -- bots --------------------------------------------------------------
    def bot_choice(self, seat_index: int) -> Tuple[str, Optional[int]]:
        """What a bot does, as ``(action, amount)``.

        Strength against the pot odds, with jitter and an occasional bluff, so a
        bot that is only calling is not a readable tell.
        """
        seat = self.seats[seat_index]
        options = self.legal_actions(seat_index)
        if not options:
            return ("check", None)

        raw = strength(seat.hole, self.board) + self.rng.uniform(-0.05, 0.05)
        raw = max(0.0, min(1.0, raw))
        to_call = self.to_call(seat)
        pot = self.pot

        if "fold" in options and to_call > 0:
            odds = to_call / (pot + to_call)
            # Fold only when the price is clearly wrong, and not always - a bot
            # that never calls a bet is free to bluff against.
            if raw < odds - config.POKER_FOLD_MARGIN and self.rng.random() < 0.8:
                return ("fold", None)

        # Value raises, plus a rare bluff when nothing is owed.
        raise_ok = "raise" in options
        if raise_ok and raw > config.POKER_RAISE_STRENGTH and self.rng.random() < 0.55:
            return ("raise", self.min_raise_to(seat))
        if raise_ok and to_call == 0 and self.rng.random() < config.POKER_BLUFF_RATE:
            return ("raise", self.min_raise_to(seat))
        if "call" in options:
            return ("call", None)
        if "check" in options:
            return ("check", None)
        return ("fold", None)

    def act_for_bot(self, seat_index: int) -> List[str]:
        action, amount = self.bot_choice(seat_index)
        return self.act(seat_index, action, amount)

    def run_bots(self) -> List[str]:
        """Let every bot act until the player is up or the hand is over."""
        lines: List[str] = []
        guard = 0
        while not self.complete and self.actor is not None and not self.seats[self.actor].is_player:
            guard += 1
            if guard > 200:  # a raise war that never ends would hang the bot
                raise PokerError("bot betting did not converge")
            lines.extend(self.act_for_bot(self.actor))
        return lines

    # -- settlement --------------------------------------------------------
    def _award_to_last_standing(self) -> None:
        survivor = self.seats_in_hand()[0]
        amount = self.pot
        self.winnings[survivor.index] = amount
        survivor.stack += amount
        self.showdown_ranks = {}
        self.log.append(f"{survivor.name} takes {amount:,} - everyone else folded")
        self._finish()

    def _showdown(self) -> None:
        live = self.seats_in_hand()
        self.showdown_ranks = {
            seat.index: hand_rank(list(seat.hole) + list(self.board)) for seat in live
        }
        contributions = [seat.total for seat in self.seats]
        self.winnings = distribute(
            contributions,
            {index: rank for index, rank in self.showdown_ranks.items()},
            button=self.button,
            seat_count=len(self.seats),
        )
        for seat in self.seats:
            seat.stack += self.winnings[seat.index]
        best = max(self.showdown_ranks.values()) if self.showdown_ranks else None
        winners = [
            self.seats[index].name
            for index, rank in self.showdown_ranks.items()
            if rank == best
        ]
        self.log.append(
            f"Showdown: {', '.join(winners)} wins with {describe(best) if best else '-'}"
        )
        self._finish()

    def _finish(self) -> None:
        self.complete = True
        self.street = COMPLETE
        self.actor = None
        self.to_act = set()

    @property
    def player_stack(self) -> int:
        """The player's chips now - what the house settles against."""
        return self.seats[0].stack

    @property
    def player_net(self) -> int:
        return self.player_stack - self.stake

    def outcome(self) -> str:
        """``win``, ``push`` or ``loss`` for the player, for display and ledger."""
        net = self.player_net
        if net > 0:
            return "win"
        if net == 0:
            return "push"
        return "loss"


def distribute(
    contributions: Sequence[int],
    ranks: Dict[int, Tuple[int, ...]],
    *,
    button: int = 0,
    seat_count: Optional[int] = None,
) -> List[int]:
    """Split the pot by side pot, returning what each seat wins.

    ``contributions`` is what each seat put in over the hand; ``ranks`` maps the
    seats still in the hand to their showdown score (seats missing from it
    folded). Chips are never created or destroyed: the returned list always sums
    to ``sum(contributions)``, which is what ``checks/check_poker.py`` asserts.

    An uncalled bet is returned to the seat that made it before the pots are
    built. If one seat contributed more than every other seat, the excess is not
    a pot: it goes back to that contributor even if they folded. Without this,
    the old fallback handed the uncalled chips to whichever live seat happened
    to remain.

    Odd chips in a split pot go to the first winner left of the button, as at a
    real table.
    """
    count = len(contributions)
    seat_count = count if seat_count is None else seat_count
    winnings = [0] * count
    stakes = [int(c) for c in contributions]

    # Return the uncalled portion of the highest contribution. Only one seat can
    # hold the unique maximum, so there is at most one refund.
    if stakes:
        highest = max(stakes)
        if highest > 0:
            leaders = [index for index, value in enumerate(stakes) if value == highest]
            if len(leaders) == 1:
                leader = leaders[0]
                second = max(
                    (value for index, value in enumerate(stakes) if index != leader),
                    default=0,
                )
                uncalled = highest - second
                if uncalled > 0:
                    winnings[leader] += uncalled
                    stakes[leader] -= uncalled

    levels = sorted({value for value in stakes if value > 0})
    previous = 0

    for level in levels:
        amount = sum(
            min(value, level) - min(value, previous) for value in stakes
        )
        previous = level
        if amount <= 0:
            continue
        eligible = [
            index
            for index, value in enumerate(stakes)
            if index in ranks and value >= level
        ]
        if not eligible:
            # Every seat that reached this level folded. The chips belong to
            # whoever is left, even if they put in less.
            eligible = [index for index in ranks if stakes[index] > 0]
        if not eligible:
            continue
        best = max(ranks[index] for index in eligible)
        winners = [index for index in eligible if ranks[index] == best]
        share, remainder = divmod(amount, len(winners))
        for index in winners:
            winnings[index] += share
        order = sorted(
            winners, key=lambda index: (index - button - 1) % seat_count
        )
        for offset in range(remainder):
            winnings[order[offset % len(order)]] += 1

    return winnings
