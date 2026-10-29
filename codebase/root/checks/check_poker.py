"""Poker engine proof: hand ranking, side pots, blinds and hand completion.

    python3 checks/check_poker.py

`poker.py` is a pure engine with no Discord or database imports, so this check
needs no fixtures. It proves the four things a Discord layer would otherwise
discover the hard way:

* the evaluator orders every category correctly, reads the wheel as five-high
  and picks the best five of seven;
* `distribute()` splits main and side pots, never pays a folded seat and sends
  the odd chip left of the button;
* blinds are posted once, stay in the pot when preflop opens, and the seat
  left of the big blind acts first while the blinds keep their option;
* four-handed hands always terminate with chips conserved, and a seeded hand
  replays identically.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import poker
import poker_names

failures = []


def check(name, cond, detail=""):
    if not cond:
        failures.append(f"{name} {detail}")
        print(f"FAIL  {name} {detail}")


# --- evaluator: known orderings ------------------------------------------
def rank(cards):
    return poker.hand_rank([c for c in cards.split()])


sf = rank("9♠ 8♠ 7♠ 6♠ 5♠")
quads = rank("A♠ A♥ A♦ A♣ K♠")
fh = rank("K♠ K♥ K♦ 9♣ 9♠")
flush = rank("A♠ J♠ 8♠ 5♠ 2♠")
straight = rank("9♠ 8♥ 7♦ 6♣ 5♠")
trips = rank("Q♠ Q♥ Q♦ 7♣ 2♠")
twopair = rank("J♠ J♥ 4♦ 4♣ A♠")
pair = rank("10♠ 10♥ 8♦ 5♣ 2♠")
high = rank("A♠ Q♥ 9♦ 6♣ 3♠")

check("category order",
      sf > quads > fh > flush > straight > trips > twopair > pair > high,
      [poker.category(r) for r in (sf, quads, fh, flush, straight, trips, twopair, pair, high)])
check("straight flush category", poker.category(sf) == poker.STRAIGHT_FLUSH)
check("quads category", poker.category(quads) == poker.QUADS)

# Wheel: A2345 is a five-high straight, not ace-high.
wheel = rank("A♠ 2♥ 3♦ 4♣ 5♠")
check("wheel is a straight", poker.category(wheel) == poker.STRAIGHT)
check("wheel is five-high", wheel[1] == 5, wheel)
check("six-high straight beats the wheel", rank("6♠ 5♥ 4♦ 3♣ 2♠") > wheel)

# Kicker comparisons.
check("pair kicker decides",
      rank("10♠ 10♥ A♦ 5♣ 2♠") > rank("10♠ 10♥ K♦ 5♣ 2♠"))
check("two pair: higher second pair wins",
      rank("J♠ J♥ 9♦ 9♣ 2♠") > rank("J♠ J♥ 8♦ 8♣ A♠"))
check("full house compares the trips first",
      rank("K♠ K♥ K♦ 2♣ 2♠") > rank("Q♠ Q♥ Q♦ A♣ A♠"))

# --- best five of seven ---------------------------------------------------
seven_flush = rank("A♠ J♠ 8♠ 5♠ 2♠ K♥ Q♦")
check("seven-card flush found", poker.category(seven_flush) == poker.FLUSH, seven_flush)
check("seven-card flush picks the best five", seven_flush == (poker.FLUSH, 14, 11, 8, 5, 2), seven_flush)

board_plays = rank("A♠ K♥ Q♦ J♣ 10♠ 2♦ 3♥")
check("board straight plays", poker.category(board_plays) == poker.STRAIGHT, board_plays)
check("board straight is ace-high", board_plays[1] == 14, board_plays)

both_pair = rank("A♠ A♥ K♦ K♣ Q♠ Q♥ J♦")
check("two pair from three pairs takes the top two",
      both_pair == (poker.TWO_PAIR, 14, 13, 12), both_pair)

quads_seven = rank("7♠ 7♥ 7♦ 7♣ A♠ K♥ 2♦")
check("quads with ace kicker", quads_seven == (poker.QUADS, 7, 14), quads_seven)

check("identical hands tie", rank("A♠ K♥ Q♦ J♣ 9♠") == rank("A♦ K♠ Q♥ J♦ 9♣"))

# --- describe() -----------------------------------------------------------
check("describe two pair", "Two Pair" in poker.describe(twopair), poker.describe(twopair))
check("describe full house", "full of" in poker.describe(fh), poker.describe(fh))

# --- distribute(): side pots ---------------------------------------------
# Seat 0 all in for 100, seats 1 and 2 in for 300 each. Main pot 300 (100 each),
# side pot 400 between 1 and 2.
contrib = [100, 300, 300]
ranks = {
    0: poker.hand_rank("A♠ A♥ A♦ K♠ K♥".split()),          # quads-ish, best
    1: poker.hand_rank("K♠ K♦ 7♥ 7♦ 2♠".split()),
    2: poker.hand_rank("Q♠ Q♦ J♥ J♦ 9♠".split()),
}
won = poker.distribute(contrib, ranks)
check("side pot sums to the pot", sum(won) == 700, won)
check("short stack only wins the main pot", won[0] == 300, won)
check("side pot goes to the second best", won[1] == 400, won)
check("third place wins nothing", won[2] == 0, won)

# Folded players contribute but cannot win.
won = poker.distribute([100, 100, 100], {1: (poker.PAIR, 5, 4, 3, 2)})
check("folded chips go to the survivor", won == [0, 300, 0], won)

# Split pot with an odd chip: 3 ways, 101 chips, first winner left of button.
won = poker.distribute([101], {0: (poker.PAIR, 2, 3, 4, 5)}, button=0, seat_count=4)
check("single seat takes its own pot", won == [101], won)

won = poker.distribute(
    [34, 34, 34],
    {0: (poker.STRAIGHT, 9), 1: (poker.STRAIGHT, 9), 2: (poker.STRAIGHT, 9)},
    button=3, seat_count=4,
)
check("odd chip to the left of the button", won == [34, 34, 34], won)
check("three-way split sums", sum(won) == 102, won)

# --- game invariants over many seeded hands -------------------------------
def auto_player(game):
    """Stand-in player: call when it is cheap, otherwise check."""
    while not game.complete:
        if game.actor is None:
            break
        seat = game.seats[game.actor]
        if seat.is_player:
            options = game.legal_actions()
            if "check" in options:
                game.act(game.actor, "check")
            elif "call" in options:
                game.act(game.actor, "call")
            else:
                game.act(game.actor, "fold")
        else:
            game.act_for_bot(game.actor)


bad_conservation = bad_negative = bad_street = 0
player_wins = 0
for seed in range(400):
    game = poker.Holdem(stake=1000, rng=random.Random(seed))
    auto_player(game)
    total = sum(s.stack for s in game.seats)
    if total != 4 * 1000:
        bad_conservation += 1
        print("  conservation", seed, total, [s.stack for s in game.seats])
    if any(s.stack < 0 for s in game.seats):
        bad_negative += 1
    if not game.complete:
        bad_street += 1
        print("  not complete", seed, game.street, game.actor)
    if game.player_net > 0:
        player_wins += 1

check("chips conserved over 400 hands", bad_conservation == 0, bad_conservation)
check("no negative stacks", bad_negative == 0, bad_negative)
check("every hand terminates", bad_street == 0, bad_street)
check("player sometimes wins", 0 < player_wins < 400, player_wins)
print(f"  player won {player_wins}/400 calling stations")

# Determinism: same seed, same result.
a = poker.Holdem(stake=1000, rng=random.Random(99))
b = poker.Holdem(stake=1000, rng=random.Random(99))
auto_player(a)
auto_player(b)
check("seeded hands are reproducible",
      [s.stack for s in a.seats] == [s.stack for s in b.seats],
      ([s.stack for s in a.seats], [s.stack for s in b.seats]))

# Blinds actually post.
g = poker.Holdem(stake=1000, rng=random.Random(1))
check("blind sizes", poker.blinds_for(1000) == (20, 40), poker.blinds_for(1000))
check("pot starts at the blinds", g.pot == 60, g.pot)
check("blinds are debited from the stacks", [s.stack for s in g.seats] == [1000, 980, 960, 1000],
      [s.stack for s in g.seats])
check("player (on the button) acts second preflop", g.actor == 3, g.actor)
check("first seat to act owes a full big blind", g.to_call(g.seats[g.actor]) == 40, g.to_call(g.seats[g.actor]))
check("max_gross reserves the whole table", poker.max_gross(1000) == 4000)

# The blinds must not be wiped when preflop opens: the pot is already 60 and
# the big blind keeps its option to check or raise once everyone has limped.
while g.street == poker.PREFLOP and not g.complete and g.actor != g.big_blind_seat:
    g.act(g.actor, "call")
check("big blind can still act after limps", g.actor == g.big_blind_seat, g.actor)
check("big blind owes nothing after limps", g.to_call(g.seats[g.big_blind_seat]) == 0,
      g.to_call(g.seats[g.big_blind_seat]))
check("big blind may check its option", "check" in g.legal_actions(), g.legal_actions())

# Checking the option ends preflop. The flop resets the betting round only:
# the chips already committed stay in the pot and the board comes out.
pot_before = g.pot
g.act(g.big_blind_seat, "check")
check("flop follows the preflop round", g.street == poker.FLOP, g.street)
check("flop deals three cards", len(g.board) == 3, g.board)
check("flop resets the bet to zero", g.current_bet == 0, g.current_bet)
check("flop resets committed chips", all(s.committed == 0 for s in g.seats), [s.committed for s in g.seats])
check("preflop chips stay in the pot", g.pot == pot_before, (g.pot, pot_before))
check("first to act postflop is left of the button", g.actor == 1, g.actor)


# --- uncalled bets and short all-ins -------------------------------------
# A unique highest contribution above the second-highest is not a pot: it is
# returned to the contributor, even when that seat folded.
w = poker.distribute([100, 50, 50, 0], {1: rank("A♠ A♥ K♦ K♣ Q♠"), 2: rank("J♠ J♥ 9♦ 9♣ 8♠")})
check("uncalled excess returns to a folded contributor", w == [50, 150, 0, 0], w)
check("uncalled excess is conserved", sum(w) == 200, sum(w))

w = poker.distribute([100, 50, 50, 0], {0: rank("10♠ 10♥ 8♦ 5♣ 2♠"), 1: rank("A♠ A♥ K♦ K♣ Q♠"), 2: rank("J♠ J♥ 9♦ 9♣ 8♠")})
check("live max contributor gets the uncalled chips back when beaten",
      w == [50, 150, 0, 0], w)

w = poker.distribute([100, 50, 50, 0], {0: rank("A♠ A♥ A♦ 7♣ 2♠"), 1: rank("K♠ Q♥ J♦ 9♣ 8♠"), 2: rank("J♠ J♥ 9♦ 9♣ 8♠")})
check("live max winner keeps the uncalled chips", w == [200, 0, 0, 0], w)

# A short all-in does not reopen raising for a seat that already acted on the
# street. It must still be able to call the extra amount.
g = poker.Holdem(stake=1000, rng=random.Random(7))
while g.street == poker.PREFLOP and not g.complete:
    options = g.legal_actions()
    if "check" in options:
        g.act(g.actor, "check")
    elif "call" in options:
        g.act(g.actor, "call")
    else:
        g.act(g.actor, "fold")
g.seats[2].stack = 30          # the big blind can only raise 30, below the 40 minimum
g.act(1, "check")
g.act(2, "all_in")
g.act(3, "call")
g.act(0, "call")
check("short all-in returns action to a prior actor", g.actor == 1, g.actor)
short_options = g.legal_actions(1)
check("short all-in withholds raise", "raise" not in short_options, short_options)
check("short all-in withholds all_in", "all_in" not in short_options, short_options)
check("short all-in still allows a call", "call" in short_options, short_options)
g.act(1, "call")
check("next street clears the raise lock", g.street == poker.TURN and "raise" in g.legal_actions(),
      (g.street, g.legal_actions()))


# --- Minecraft-themed bot names ------------------------------------------
# The AI reply is optional; parsing and the fallback pool must work offline.
parsed = poker_names.parse_names(
    '```json\n["Creeper Cass", "Redstone Rex", "Ender Ellie"]\n```'
)
check("name parser handles fenced JSON",
      parsed == ["Creeper Cass", "Redstone Rex", "Ender Ellie"], parsed)
parsed = poker_names.parse_names("1. Creeper Cass\n2. Redstone Rex\n3. Ender Ellie")
check("name parser handles numbered lines",
      parsed == ["Creeper Cass", "Redstone Rex", "Ender Ellie"], parsed)
parsed = poker_names.parse_names('{"names": ["Steve", "Alex", "Steve"]}')
check("name parser dedupes case-insensitively", parsed == ["Steve", "Alex"], parsed)
check("name parser rejects junk",
      poker_names.parse_names('["", "x", "bad/<script>", "Ok Name"]') == ["Ok Name"],
      poker_names.parse_names('["", "x", "bad/<script>", "Ok Name"]'))

fallback = poker_names.fallback_names(3, random.Random(1))
check("fallback names are unique", len(set(fallback)) == 3, fallback)
check("fallback names are Minecraft-themed",
      set(fallback).issubset(set(config.POKER_BOT_NAME_POOL)), fallback)
check("default bot names come from the themed pool",
      set(config.POKER_BOT_NAMES).issubset(set(config.POKER_BOT_NAME_POOL)),
      config.POKER_BOT_NAMES)

named = poker.Holdem(
    stake=1000,
    names=("Creeper Cass", "Redstone Rex", "Ender Ellie"),
    rng=random.Random(1),
)
check("custom bot names populate seats",
      [seat.name for seat in named.seats[1:]] ==
      ["Creeper Cass", "Redstone Rex", "Ender Ellie"],
      [seat.name for seat in named.seats[1:]])


print()
print(f"FAILURES: {len(failures)}")
sys.exit(1 if failures else 0)
