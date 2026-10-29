"""
config.py
=========
Central configuration for the Royal Court bot.

All payout rates are FIXED for this edition (per the user's decision:
"no for both for now"). They are hard-coded here so there is a single
source of truth. If a future edition wants editable rates, these values
can be moved into the `settings` table.
"""

# ---------------------------------------------------------------------------
# Royal Court Decrees: Nation Payouts (every 14 days)
# ---------------------------------------------------------------------------
NATION_BASE = 20_000                    # Rule 7.1  Base Allowance
NATION_PER_HAPPY_WORKER = 250           # Rule 7.2  Happy Workers
NATION_PER_REGULAR_WORKER = 30          # Rule 7.3  Regular Workers
NATION_PER_HAPPY_SOLDIER = 200          # Rule 7.4  Happy Soldiers
NATION_PER_REGULAR_SOLDIER = 20         # Rule 7.5  Regular Soldiers

# ---------------------------------------------------------------------------
# Royal Court Decrees: Kingdom Payouts (every 14 days)
# ---------------------------------------------------------------------------
KINGDOM_BASE = 50_000                   # Rule 8.1  Base Allowance
KINGDOM_PER_MEMBER_NATION = 10_000      # Rule 8.2  Member Nations
KINGDOM_PER_HAPPY_WORKER = 125          # Rule 8.3  Happy Workers (across members)
KINGDOM_PER_REGULAR_WORKER = 15         # Rule 8.4  Regular Workers (across members)
KINGDOM_PER_HAPPY_SOLDIER = 100         # Rule 8.5  Happy Soldiers (across members)
KINGDOM_PER_REGULAR_SOLDIER = 10        # Rule 8.6  Regular Soldiers (across members)

# ---------------------------------------------------------------------------
# Cycle & structural rules
# ---------------------------------------------------------------------------
PAYOUT_CYCLE_DAYS = 14                  # Payout every 14 days
MAX_NATIONS_PER_KINGDOM = 6             # A kingdom is made of up to 6 nations

# ---------------------------------------------------------------------------
# One-time creation bonuses (credited to the Royal Bank when first founded)
# ---------------------------------------------------------------------------
NATION_CREATION_BONUS = 30_000          # Bonus for founding a nation
KINGDOM_CREATION_BONUS = 50_000         # Bonus for founding a kingdom

# ---------------------------------------------------------------------------
# Cosmetic / theme
# ---------------------------------------------------------------------------
CURRENCY_NAME = "credits"
CURRENCY_SYMBOL = "⚜"
BRAND_COLOR = 0x8B0000                  # deep royal red
GOLD_COLOR = 0xD4AF37                   # royal gold
GREEN_COLOR = 0x2E8B57                  # approval / success
RED_COLOR = 0xB22222                    # denial / error
BLUE_COLOR = 0x4169E1                   # info

# ---------------------------------------------------------------------------
# Royal Shop: treasury payments, discounts, shulker packaging & kit rules
# ---------------------------------------------------------------------------
SHULKER_BOX_FEE = 3_000                 # credits added per shulker box used for packaging
SHOP_SLOT_LIMIT = 20                    # a purchase using more than this many stacks is shulker-packed
NATION_DISCOUNT = 0.20                  # 20% off when funded from a Nation treasury
KINGDOM_DISCOUNT = 0.25                 # 25% off when funded from a Kingdom treasury

KIT_MAX_ITEM_TYPES = 10                 # max distinct item types per kit
KIT_COOLDOWN_DAYS = 7                   # players may create one kit every 7 days

# ---------------------------------------------------------------------------
# Workers' Union — Gilbert contract system
# ---------------------------------------------------------------------------
CONTRACT_MAX_CREDITS = 100_000_000      # hard cap per contract
CONTRACT_MIN_CREDITS = 1                # no practical minimum; budget must be positive
CONTRACT_UPFRONT_PCT = 25               # % paid upfront; remainder held in escrow
CONTRACT_HISTORY_LIMIT = 40             # messages of AI context kept per contract
CONTRACT_THREAD_ARCHIVE_MINUTES = 10080 # 7 days of inactivity before auto-archive

# Contracts an owner may hold in one natural payout cycle, by owner type.
CONTRACTS_PER_CYCLE = {"nation": 1, "kingdom": 3}

# ---------------------------------------------------------------------------
# Workers' Union — treasury, membership, discount & dues
# ---------------------------------------------------------------------------
# Every contract credit comes out of the Union treasury. Admins fund it with
# /union fund; members may donate with /union donate. Nothing else creates
# Union credits.
UNION_MEMBER_DISCOUNT = 50              # % off eligible categories for members
UNION_MEMBER_DISCOUNT_CATEGORIES = frozenset({
    "Basic Building",
    "Wood & Masonry",
    "Food & Crops",
})
# Dues taken on every payout, paid into the Union treasury by member accounts.
# Charged on the account's whole balance after the payout is credited, not on
# the payout alone. Stored in basis points (1 bp = 0.01%) so credit maths stays
# integer: 175 bp = 1.75%, 200 bp = 2%.
UNION_TAX_BPS = {"nation": 175, "kingdom": 200}


def union_tax_label(account_type: str) -> str:
    """Human-readable dues rate for an account type, e.g. 175 bp -> '1.75'."""
    bps = UNION_TAX_BPS.get(account_type, 0)
    whole, frac = divmod(bps, 100)
    return str(whole) if frac == 0 else f"{whole}.{frac:02d}".rstrip("0")


# How much more than its treasury a party may commit to, by contract size.
# Ordered smallest contract first; the final entry must use ``None`` as the
# ceiling. See contract_limits.py for the ladder and why it is safe to pay.
CONTRACT_LEVERAGE_TIERS = (
    (10_000_000, 3),                    # up to 10M: 3x the treasury balance
    (20_000_000, 2),                    # up to 20M: 2x the treasury balance
    (None, 1),                          # above 20M: fully funded (1x)
)

# ---------------------------------------------------------------------------
# Factions — The Crown (Albert) and the Workers' Union (Gilbert)
# ---------------------------------------------------------------------------
# A nation or kingdom belongs to at most one faction. The Union side reuses the
# existing membership, discount and dues rules. The Crown side has its own
# treasury and a 100% tax on the regular-soldier income line; its legacy item
# supplier contracts keep decaying demand. Soldier count is informational, never
# a contract gate, and faction membership is never required to request work.
CROWN_SOLDIER_MINIMUM = 0               # no hard gate; soldiers are standing only
CROWN_TAX_GRACE_DAYS = 30               # 0% Crown tax for the first 30 days
CROWN_TAX_BPS = 10_000                  # 100% of the regular-soldier payout line

# Active Crown supplier contracts a single account may hold at once.
CROWN_CONTRACTS_PER_ACCOUNT = {"nation": 1, "kingdom": 2}

# A contract is active for this many natural payout cycles. At the natural
# cycle whose number reaches the deadline it expires and frees the slot; no
# credits move for an unfulfilled contract.
CROWN_CONTRACT_CYCLES = 3

# How long a fulfilled contract makes the account the official supplier of that
# item, in natural payout cycles (display/status only, no exclusivity).
CROWN_SUPPLIER_CYCLES = 3

# Demand decay: each natural cycle in which the same account fulfils a contract
# for the same item reduces that item's demand by 5 percentage points, floored
# at 40% of the catalog price. Contracts already signed keep the value they
# were signed at; the next contract sees the lower demand.
CROWN_DEMAND_START_BP = 10_000
CROWN_DEMAND_DECAY_BP = 500
CROWN_DEMAND_FLOOR_BP = 4_000

# Crown contract value caps, by the contracting account's treasury balance.
# Below 15M: 10M. 15M through 50M: 15M. Above 50M: one third of the balance.
CROWN_CONTRACT_CAP_LOW_BALANCE = 15_000_000
CROWN_CONTRACT_CAP_MID_BALANCE = 50_000_000
CROWN_CONTRACT_CAP_LOW = 10_000_000
CROWN_CONTRACT_CAP_MID = 15_000_000

# /audience waits only this long for an NPC model response; the deterministic
# in-character fallback answers immediately after a timeout.
NPC_AUDIENCE_TIMEOUT_SECONDS = 20


def crown_contract_cap(balance: int) -> int:
    """Largest total Crown contract value this treasury may commit to."""
    balance = max(0, int(balance))
    if balance < CROWN_CONTRACT_CAP_LOW_BALANCE:
        return CROWN_CONTRACT_CAP_LOW
    if balance <= CROWN_CONTRACT_CAP_MID_BALANCE:
        return CROWN_CONTRACT_CAP_MID
    return balance // 3


# ---------------------------------------------------------------------------
# Individuals — personal wallets and the /daily allowance
# ---------------------------------------------------------------------------
# Every guild member has their own wallet, separate from nation and kingdom
# treasuries. It is hard-capped: credits beyond the cap are never stored.
INDIVIDUAL_MAX_CREDITS = 100_000_000

# /daily pays DAILY_BASE scaled by a multiplier kept in basis points
# (10,000 bp = 100%). One claim per 24 hours; consecutive claims add
# DAILY_BP_STEP, a missed day subtracts DAILY_BP_MISS_PENALTY, and the value
# stays between DAILY_BP_FLOOR (60%) and DAILY_BP_CAP (400% = 8,000 credits).
DAILY_BASE = 2_000
DAILY_COOLDOWN_HOURS = 24
DAILY_BP_START = 10_000
DAILY_BP_STEP = 1_000
DAILY_BP_CAP = 40_000
DAILY_BP_MISS_PENALTY = 2_000
DAILY_BP_FLOOR = 6_000

# Rows shown by /leaderboard (Discord allows 10 comfortably in one embed).
LEADERBOARD_LIMIT = 10

# ---------------------------------------------------------------------------
# Individuals — net worth tax, charged every payout cycle
# ---------------------------------------------------------------------------
# Individuals only. Nations and kingdoms are deliberately untaxed by this rule
# and have no balance cap at all; their only charge is Workers' Union dues,
# which apply to members only (see UNION_TAX_BPS above).
#
# The tax is a share of the wallet's *whole* balance, taken on every payout run
# (natural 14-day cycle and manual /payout run alike), floored to whole credits.
# Stored in basis points so the arithmetic stays integer: 500 bp = 5%.
INDIVIDUAL_TAX_BPS = 500

# Set False to stop charging the tax without removing the code or the ledger
# kind. Useful for a targeted pause; changing INDIVIDUAL_TAX_BPS to 0 does the
# same thing.
INDIVIDUAL_TAX_ENABLED = True

# Where the collected tax goes: "union" credits the Workers' Union treasury for
# the guild (money stays in the economy and can fund contracts), "burn" removes
# it from circulation entirely so it acts as an inflation sink.
INDIVIDUAL_TAX_DESTINATION = "union"


def individual_tax_label() -> str:
    """Human-readable individual tax rate, e.g. 500 bp -> '5'."""
    whole, frac = divmod(INDIVIDUAL_TAX_BPS, 100)
    return str(whole) if frac == 0 else f"{whole}.{frac:02d}".rstrip("0")


# ---------------------------------------------------------------------------
# Individuals — /individual coinflip
# ---------------------------------------------------------------------------
# A staked bet on a coin. The player wins COINFLIP_WIN_BP times in 10,000
# (5,000 = an even 50%) and loses the rest. With a 2x return the long-run drift
# is 10,000 - 5,000 x 2 = 0 bp: a fair pass-through. The Casino still has to
# cover each win from its own balance (see casino.cover_problem), so variance,
# not an edge, is what moves the house balance. Lower COINFLIP_WIN_BP creates a
# sink; raising it above 5,000 makes the game a faucet.
COINFLIP_WIN_BP = 5_000
COINFLIP_PAYOUT_MULTIPLIER = 2          # gross return on a win: stake back + equal winnings

# Stake bounds. The minimum makes spam pointless (each bet writes a ledger row);
# the maximum stops one lucky flip from minting a fortune or wiping a wallet.
COINFLIP_MIN_STAKE = 100
COINFLIP_MAX_STAKE = 10_000_000


# ---------------------------------------------------------------------------
# Individuals — /allin
# ---------------------------------------------------------------------------
# Stake the whole wallet on one draw for an n-times return. The chance of
# winning is 1 in (hardness * n), so the hardness is the only thing that sets
# the house edge: expected return is 1/hardness no matter which number is
# played. At 1.1 (11/10) every number carries a 9.09% edge (10/11 returned), and
# a higher number is only more variance, not a worse bet. Keep the numerator
# above the denominator or the game stops being a sink and starts printing
# credits.
ALLIN_HARDNESS_NUMERATOR = 11           # 11/10 = 1.1x harder than the number picked
ALLIN_HARDNESS_DENOMINATOR = 10
ALLIN_MIN_NUMBER = 2                    # 1 would pay 1x, i.e. risk all for no profit
ALLIN_MAX_NUMBER = 100
ALLIN_MIN_BALANCE = 100                 # below this there is no point playing


# ---------------------------------------------------------------------------
# Individuals — /blackjack, and The Casino house account
# ---------------------------------------------------------------------------
# Blackjack settles against the Casino house balance, so a win is paid by the
# house rather than minted. The house therefore has to reserve the worst case
# before a hand is dealt: a natural pays 3:2, i.e. 2.5x the stake returned.
BLACKJACK_DECKS = 6                     # cards before the shoe is reshuffled
BLACKJACK_BLACKJACK_BP = 15_000         # 3:2 -> profit of 150% of the stake
BLACKJACK_MIN_STAKE = 100
BLACKJACK_MAX_STAKE = 10_000_000
BLACKJACK_DEALER_STANDS_ON = 17         # stands on soft 17 too
BLACKJACK_TIMEOUT_SECONDS = 120         # an abandoned hand is settled at stand

# The Casino is the counterparty for every wager. It cannot pay out more than it
# holds, which is what stops gambling from inflating the money supply; the only
# way credits enter it is an admin `/casino fund`.
CASINO_MIN_FUNDING = 1

# ---------------------------------------------------------------------------
# Individuals — /fish and the Fishing Guild house account
# ---------------------------------------------------------------------------
# A catch is one random fish worth an inclusive 20-100 credit payout. The
# credits come out of the Fishing Guild house balance, never thin air; admins
# fund it with `/fishingguild fund` and can drain it with
# `/fishingguild withdraw`, exactly like the Casino. The guild must hold the
# whole catch before it is paid, and the individual 100M cap still applies.
FISHING_MIN_CREDITS = 20
FISHING_MAX_CREDITS = 100
FISHING_NAMES = (
    "Mudscale Carp",
    "Silverfin",
    "River Perch",
    "Copper Koi",
    "Storm Trout",
    "Moon Eel",
    "Ruby Snapper",
    "Frost Pike",
    "Golden Sturgeon",
    "Brine Bass",
)

# ---------------------------------------------------------------------------
# Individuals — /poker (Texas Hold'em, one player against three house bots)
# ---------------------------------------------------------------------------
# Each seat starts with the player's stake, so the chips at the table (and the
# house's worst case) are 4 x stake. Blinds are a fraction of it, which keeps
# the stack around 50 big blinds deep - enough for real postflop play.
# Curated Minecraft-themed fallback names. `poker_names.py` samples this pool and
# also refreshes a larger AI-generated pool through GilbertAI (same AI_API_KEY)
# in the background, so /poker never waits on the network.
POKER_BOT_NAME_POOL = (
    "Creeper Cass",
    "Redstone Rex",
    "Ender Ellie",
    "Diamond Dot",
    "Nether Nell",
    "Glow Squid Gus",
    "Villager Vic",
    "Piston Petra",
    "Bedrock Bramble",
    "Copper Clara",
    "Lapis Lars",
    "Blaze Bella",
    "Mossy Mira",
    "Tuff Theo",
    "Amethyst Ada",
    "Sculk Silas",
)
POKER_BOT_NAMES = POKER_BOT_NAME_POOL[:3]  # fallback/default; /poker samples the pool
POKER_BLINDS_DIVISOR = 50               # small blind = stake // 50, big = 2x
POKER_MIN_STAKE = 200                   # 4 big blinds; below this the game is a coinflip
POKER_MAX_STAKE = 10_000_000
POKER_TIMEOUT_SECONDS = 180             # an abandoned table is folded
POKER_FOLD_MARGIN = 0.06                # how far below the pot odds a bot will still call
POKER_RAISE_STRENGTH = 0.62             # strength above which a bot raises for value
POKER_BLUFF_RATE = 0.08                 # chance of a raise with nothing owed
POKER_BOT_DELAY_SECONDS = 0.7           # reserved for a future paced bot animation

