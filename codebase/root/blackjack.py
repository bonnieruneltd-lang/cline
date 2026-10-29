"""
blackjack.py
============
Blackjack, as pure rules with no Discord and no database.

Rules implemented
-----------------
* A shoe of ``config.BLACKJACK_DECKS`` decks, shuffled and drawn from the end.
* Number cards count face value, tens and court cards count 10, an ace counts
  11 or 1 - whichever keeps the hand at or under 21.
* A two-card 21 is a **blackjack** and pays **3:2**. Any other win pays even
  money. Equal totals push and the stake comes back.
* The dealer **stands on all 17s**, including soft 17, which is the house
  friendly variant and keeps the rules simple to state in the embed.
* The player may hit or stand; there is no split, double or insurance.

The payout functions return the **gross** return (stake included), because that
is what the Casino has to pay out and therefore what it must be able to cover.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List, Optional, Sequence

import config

# Card rank -> blackjack value. The ace is stored as 11 and demoted by
# :func:`hand_value` when the hand would otherwise bust.
_RANKS = {
    "A": 11, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8,
    "9": 9, "10": 10, "J": 10, "Q": 10, "K": 10,
}


@dataclass(frozen=True)
class Outcome:
    """A settled hand."""

    result: str          # "win", "loss" or "push"
    blackjack: bool      # the player won with a natural 21
    gross: int           # credits the player gets back in total
    net: int             # credits gained (+) or lost (-) relative to the stake
    label: str           # short phrase for the embed

    @property
    def won(self) -> bool:
        return self.result == "win"


SIGNS = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
_RANKS_BY_NAME = dict(_RANKS)
for _suit in SIGNS.values():
    for _rank in _RANKS:
        _RANKS_BY_NAME[f"{_rank}{_suit}"] = _RANKS[_rank]


def new_shoe(decks: Optional[int] = None, rng: Optional[random.Random] = None) -> List[str]:
    """A freshly shuffled shoe, as card ranks such as ``A♠``."""
    decks = config.BLACKJACK_DECKS if decks is None else int(decks)
    shoe = [f"{rank}{suit}" for _ in range(decks) for rank in _RANKS for suit in SIGNS.values()]
    (rng or random).shuffle(shoe)
    return shoe


def draw(shoe: List[str], rng: Optional[random.Random] = None) -> str:
    """Take the top card. Reshuffles when the shoe is empty, so a hand never fails."""
    if not shoe:
        shoe.extend(new_shoe(rng=rng))
    return shoe.pop()


def card_value(card: str) -> int:
    """Value of a printed card or a bare rank."""
    if card in _RANKS:
        return _RANKS[card]
    return _RANKS_BY_NAME[card]


def hand_value(cards: Sequence[str]) -> int:
    """Best total for a hand: aces count 11 until that would bust, then 1."""
    total = sum(card_value(c) for c in cards)
    aces = sum(1 for c in cards if c.startswith("A"))
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_soft(cards: Sequence[str]) -> bool:
    """True when at least one ace is still being counted as 11.

    A hand like ``A♠ A♥ 9♦`` is a soft 21 even though the raw total (31) is
    over 21: one ace is demoted to 1 and the other remains an 11. Counting only
    "raw total <= 21" misclassified that hand as hard.
    """
    aces = sum(1 for card in cards if card.startswith("A"))
    if not aces:
        return False
    total = hand_value(cards)
    if total > 21:
        return False
    raw = sum(card_value(card) for card in cards)
    demoted = (raw - total) // 10
    return demoted < aces


def is_bust(cards: Sequence[str]) -> bool:
    return hand_value(cards) > 21


def is_blackjack(cards: Sequence[str]) -> bool:
    """A natural: exactly two cards totalling 21."""
    return len(cards) == 2 and hand_value(cards) == 21


def dealer_should_hit(cards: Sequence[str]) -> bool:
    """Dealer draws below 17 and stands on everything else, soft 17 included."""
    return hand_value(cards) < 17


def gross_return(*, stake: int, blackjack: bool) -> int:
    """Total credits returned on a win: even money, or 3:2 for a natural.

    ``BLACKJACK_BLACKJACK_BP`` is the *profit* in basis points of the stake, so a
    win returns stake plus that profit. The integer division drops at most one
    credit on an odd stake, in the house's favour.
    """
    stake = int(stake)
    if blackjack:
        return stake + (stake * config.BLACKJACK_BLACKJACK_BP) // 10_000
    return stake * 2


def settle(
    player: Sequence[str], dealer: Sequence[str], *, stake: int
) -> Outcome:
    """Compare two finished hands and work out what the player is owed."""
    stake = int(stake)
    p_total = hand_value(player)
    d_total = hand_value(dealer)
    p_natural = is_blackjack(player)
    d_natural = is_blackjack(dealer)

    if p_total > 21:
        return Outcome("loss", False, 0, -stake, "Bust")
    # A natural beats a made 21; two naturals push. Checked before the plain
    # comparison, which would otherwise treat 21 and 21 as equal.
    if p_natural and d_natural:
        return Outcome("push", True, stake, 0, "Push")
    if p_natural:
        gross = gross_return(stake=stake, blackjack=True)
        return Outcome("win", True, gross, gross - stake, "Blackjack")
    if d_natural:
        return Outcome("loss", False, 0, -stake, "Dealer blackjack")
    if d_total > 21:
        gross = gross_return(stake=stake, blackjack=False)
        return Outcome("win", False, gross, gross - stake, "Dealer bust")
    if p_total > d_total:
        gross = gross_return(stake=stake, blackjack=False)
        return Outcome("win", False, gross, gross - stake, "Higher hand")
    if p_total == d_total:
        return Outcome("push", False, stake, 0, "Push")
    return Outcome("loss", False, 0, -stake, "Dealer higher")


def hand_label(cards: Sequence[str]) -> str:
    """``A♠ K♥ (21)`` for display."""
    total = hand_value(cards)
    tag = f"{total}" if total <= 21 else f"{total} — bust"
    if is_blackjack(cards):
        tag = "blackjack"
    return f"{' '.join(cards)} ({tag})"


def visible_dealer(cards: Sequence[str]) -> str:
    """The dealer's hand with the hole card hidden, for the player's view."""
    if len(cards) < 2:
        return " ".join(cards)
    return f"{cards[0]} 🂠"


def max_gross(stake: int) -> int:
    """Worst case the house must cover for this stake: a blackjack payout."""
    return gross_return(stake=stake, blackjack=True)
