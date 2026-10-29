"""
factions.py
===========
Domain rules for the two Royal factions and The Crown's supplier economy.

The Workers' Union and The Crown are mutually exclusive. This module owns the
membership transition (including cleanup when an account switches sides), the
Crown's 100% regular-soldier tax, and the supplier contract lifecycle. It is
database-level and Discord-free so the rules can be tested without a login.

Money rules
-----------
* The Crown tax is the regular-soldier payout line, paid from the treasury into
  ``crown_account`` in the same transaction as the payout credit.
* Supplier payments debit ``crown_account`` and credit the contracting treasury
  in one transaction; the ledger gets both rows.
* Credit donations move credits from the treasury to the faction house account.
* Item deliveries do not pay until a contract is complete, and a short Crown
  treasury rolls the whole delivery back.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

import config
from database import Database, parse_iso, now_iso
from shop import pricing

UNION = "union"
CROWN = "crown"
FACTIONS = (UNION, CROWN)
FACTION_LABELS = {
    UNION: "Workers' Union",
    CROWN: "The Crown",
}
FACTION_EMOJI = {UNION: "⚒️", CROWN: "👑"}

_TABLES = {"nation": "nations", "kingdom": "kingdoms"}


class FactionError(Exception):
    """A user-facing faction/contract refusal; no money moved."""


class CrownContractError(FactionError):
    """A supplier contract cannot be created as requested."""


class CrownTreasuryShort(FactionError):
    """The Crown cannot cover the payout a delivery would unlock."""


@dataclass(frozen=True)
class JoinResult:
    faction: str
    previous: Optional[str]
    status: str
    joined_at: str
    already: bool = False
    reactivated: bool = False
    cancelled_commit: bool = False
    cancelled_contracts: int = 0


@dataclass(frozen=True)
class DeliveryResult:
    paid: int
    fulfilled: tuple[int, ...]
    delivered: int
    excess: int
    treasury_after: int


@dataclass(frozen=True)
class CreditDonation:
    amount: int
    balance_after: int
    treasury_after: int


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------
def faction_label(faction: Optional[str]) -> str:
    return FACTION_LABELS.get(faction or "", "Unaffiliated")


def total_soldiers(row) -> int:
    """Happy + regular soldiers on a nation row.

    Kingdoms do not store stats themselves; ``account_soldiers`` sums their
    member nations.
    """
    return int(row["happy_soldiers"] or 0) + int(row["regular_soldiers"] or 0)


def crown_eligible(soldiers: int) -> bool:
    """Crown membership is open; soldier count is a soft contract advantage."""
    return True


def regular_soldier_income(result) -> int:
    """The regular-soldier subtotal of a nation/kingdom payout result."""
    for line in result.lines:
        if str(line.label).lower().startswith("regular soldiers"):
            return int(line.subtotal)
    return 0


def crown_tax_due(
    *,
    faction: Optional[str],
    status: Optional[str],
    joined_at: Optional[str],
    soldiers: int,
    result,
    now: dt.datetime,
) -> int:
    """Credits the Crown takes from this payout, at 100% of the regular line."""
    if faction != CROWN:
        return 0
    joined = parse_iso(joined_at)
    if joined is None:
        return 0
    if now < joined + dt.timedelta(days=config.CROWN_TAX_GRACE_DAYS):
        return 0
    return regular_soldier_income(result) * config.CROWN_TAX_BPS // 10_000


def grace_ends_at(joined_at: Optional[str]) -> Optional[dt.datetime]:
    joined = parse_iso(joined_at)
    if joined is None:
        return None
    return joined + dt.timedelta(days=config.CROWN_TAX_GRACE_DAYS)


def tax_status(
    *,
    faction: Optional[str],
    status: Optional[str],
    joined_at: Optional[str],
    soldiers: int,
    now: Optional[dt.datetime] = None,
) -> tuple[bool, str]:
    """``(tax_active, human reason)`` for Albert's report."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if faction != CROWN:
        return False, "Not a Crown member"
    grace = grace_ends_at(joined_at)
    if grace is not None and now < grace:
        return False, f"Grace period until {grace.strftime('%Y-%m-%d %H:%M UTC')}"
    return True, "100% of regular-soldier income"


def demand_after_cycle(demand_bp: int, last_cycle: Optional[int], cycle: int) -> int:
    """Decay demand at most once per natural cycle."""
    demand_bp = int(demand_bp)
    if last_cycle is not None and int(cycle) <= int(last_cycle):
        return demand_bp
    return max(
        config.CROWN_DEMAND_FLOOR_BP,
        demand_bp - config.CROWN_DEMAND_DECAY_BP,
    )


def effective_unit_price(unit_price: int, demand_bp: int) -> int:
    return max(1, int(unit_price) * int(demand_bp) // 10_000)


def contract_value(unit_price: int, quantity: int, demand_bp: int) -> int:
    return effective_unit_price(unit_price, demand_bp) * int(quantity)


def max_contract_quantity(cap: int, unit_price: int, demand_bp: int) -> int:
    unit = effective_unit_price(unit_price, demand_bp)
    return max(0, int(cap) // unit)


def contract_cap(balance: int) -> int:
    return config.crown_contract_cap(balance)


def contract_slots(account_type: str) -> int:
    return int(config.CROWN_CONTRACTS_PER_ACCOUNT.get(account_type, 0))


def demand_percent(demand_bp: int) -> str:
    return f"{int(demand_bp) / 100:g}%"



# ---------------------------------------------------------------------------
# Account and membership helpers
# ---------------------------------------------------------------------------
async def get_account(
    db: Database, guild_id: int, account_type: str, account_id: int
):
    if account_type == "nation":
        row = await db.get_nation_by_id(int(account_id))
    elif account_type == "kingdom":
        row = await db.get_kingdom_by_id(int(account_id))
    else:
        return None
    if row is None or int(row["guild_id"]) != int(guild_id):
        return None
    return row


async def account_soldiers(
    db: Database, guild_id: int, account_type: str, account_id: int, row=None
) -> int:
    row = row or await get_account(db, guild_id, account_type, account_id)
    if row is None:
        return 0
    if account_type == "nation":
        return total_soldiers(row)
    members = await db.nations_in_kingdom(int(account_id))
    return sum(total_soldiers(member) for member in members)


async def account_label(
    db: Database, guild_id: int, account_type: str, account_id: int
) -> str:
    row = await get_account(db, guild_id, account_type, account_id)
    return row["name"] if row else f"{account_type} #{account_id}"


async def _set_status(
    db: Database, guild_id: int, account_type: str, account_id: int, status: str
) -> None:
    await db.execute(
        """UPDATE faction_members SET status = ?
           WHERE guild_id = ? AND account_type = ? AND account_id = ?""",
        (status, guild_id, account_type, account_id),
    )


async def refreshed_member(
    db: Database, guild_id: int, account_type: str, account_id: int
):
    """Return the membership row with Crown status refreshed for current stats.

    Called inside a transaction so the status read/write is atomic with the
    decision that uses it. Union rows are always active.
    """
    member = await db.get_faction_member(guild_id, account_type, account_id)
    if member is None or member["faction"] != CROWN:
        return member
    soldiers = await account_soldiers(db, guild_id, account_type, account_id)
    desired = "active" if crown_eligible(soldiers) else "ineligible"
    if member["status"] != desired:
        await _set_status(db, guild_id, account_type, account_id, desired)
        member = await db.get_faction_member(guild_id, account_type, account_id)
    return member


async def join_faction(
    db: Database,
    *,
    guild_id: int,
    account_type: str,
    account_id: int,
    faction: str,
    actor_id: int,
) -> JoinResult:
    """Join or switch factions, cleaning up the old side in the same transaction."""
    if faction not in FACTIONS:
        raise FactionError("Choose the Workers' Union or The Crown.")
    if account_type not in _TABLES:
        raise FactionError("Only a nation or kingdom can join a faction.")
    async with db.transaction():
        account = await get_account(db, guild_id, account_type, account_id)
        if account is None:
            raise FactionError("That nation or kingdom no longer exists.")
        member = await db.get_faction_member(guild_id, account_type, account_id)
        previous = member["faction"] if member is not None else None

        # Clean the opposite side even when there was no faction row: a legacy
        # whole-balance Union commitment (or stray Crown contract) must not
        # survive an allegiance change.
        cancelled_contracts = 0
        cancelled_commit = False
        if faction == CROWN:
            cur = await db.execute(
                """UPDATE union_commits SET status = 'cancelled'
                   WHERE guild_id = ? AND account_type = ? AND account_id = ?
                     AND status IN ('pending', 'active')""",
                (guild_id, account_type, account_id),
            )
            cancelled_commit = cur.rowcount > 0
            await db.execute(
                """DELETE FROM union_members
                   WHERE guild_id = ? AND account_type = ? AND account_id = ?""",
                (guild_id, account_type, account_id),
            )
        else:
            cur = await db.execute(
                """UPDATE crown_contracts SET status = 'cancelled'
                   WHERE guild_id = ? AND account_type = ? AND account_id = ?
                     AND status = 'active'""",
                (guild_id, account_type, account_id),
            )
            cancelled_contracts = cur.rowcount

        if member is not None and member["faction"] == faction:
            reactivated = False
            if member["status"] != "active":
                await _set_status(
                    db, guild_id, account_type, account_id, "active"
                )
                member = await db.get_faction_member(
                    guild_id, account_type, account_id
                )
                reactivated = True
            return JoinResult(
                faction=faction,
                previous=faction,
                status=member["status"],
                joined_at=member["joined_at"],
                already=not reactivated,
                reactivated=reactivated,
                cancelled_commit=cancelled_commit,
                cancelled_contracts=cancelled_contracts,
            )

        if member is not None:
            await db.execute(
                """DELETE FROM faction_members
                   WHERE guild_id = ? AND account_type = ? AND account_id = ?""",
                (guild_id, account_type, account_id),
            )

        stamp = now_iso()
        await db.execute(
            """INSERT INTO faction_members
               (guild_id, account_type, account_id, faction, status, joined_at)
               VALUES (?, ?, ?, ?, 'active', ?)""",
            (guild_id, account_type, account_id, faction, stamp),
        )
        if faction == UNION:
            await db.execute(
                """INSERT OR REPLACE INTO union_members
                   (guild_id, account_type, account_id, joined_at)
                   VALUES (?, ?, ?, ?)""",
                (guild_id, account_type, account_id, stamp),
            )
        else:
            # Defensive: a Crown row must never coexist with the legacy mirror.
            await db.execute(
                """DELETE FROM union_members
                   WHERE guild_id = ? AND account_type = ? AND account_id = ?""",
                (guild_id, account_type, account_id),
            )

    return JoinResult(
        faction=faction,
        previous=previous,
        status="active",
        joined_at=stamp,
        cancelled_commit=cancelled_commit,
        cancelled_contracts=cancelled_contracts,
    )


# ---------------------------------------------------------------------------
# Crown tax and contract lifecycle
# ---------------------------------------------------------------------------
async def apply_crown_tax(
    db: Database,
    *,
    guild_id: int,
    account_type: str,
    account_id: int,
    result,
    now: dt.datetime,
) -> tuple[int, int]:
    """Deduct the Crown's regular-soldier cut inside the payout transaction.

    Returns ``(tax_paid, balance_after)``. The account was just credited with
    the full payout; this takes the regular-soldier line back into the Crown
    treasury and writes both ledger sides. All Crown members are active; the
    soldier count is shown in the representative briefing.
    """
    async with db.transaction():
        member = await refreshed_member(db, guild_id, account_type, account_id)
        account = await get_account(db, guild_id, account_type, account_id)
        if account is None:
            return 0, 0
        balance = int(account["balance"])
        if member is None or member["faction"] != CROWN:
            return 0, balance
        soldiers = await account_soldiers(
            db, guild_id, account_type, account_id, account
        )
        tax = crown_tax_due(
            faction=member["faction"],
            status=member["status"],
            joined_at=member["joined_at"],
            soldiers=soldiers,
            result=result,
            now=now,
        )
        if tax <= 0:
            return 0, balance
        table = _TABLES[account_type]
        after = await db.debit(table, int(account_id), tax)
        crown_after = await db.credit_crown(guild_id, tax)
        label = account["name"]
        await db.log_tx(
            guild_id=guild_id,
            kind="crown_tax",
            account_type=account_type,
            account_id=int(account_id),
            account_name=label,
            counterparty_type="crown",
            counterparty_name="The Crown",
            amount=-tax,
            balance_after=after,
            note=(
                f"Crown tax: 100% of {regular_soldier_income(result):,} "
                "regular-soldier income"
            ),
        )
        await db.log_tx(
            guild_id=guild_id,
            kind="crown_tax",
            account_type="crown",
            account_id=guild_id,
            account_name="The Crown",
            counterparty_type=account_type,
            counterparty_id=int(account_id),
            counterparty_name=label,
            amount=tax,
            balance_after=crown_after,
            note=f"Crown tax from {label}",
        )
    return tax, after


async def expire_crown_contracts(db: Database, guild_id: int, cycle: int) -> int:
    """Mark active contracts past their deadline expired; free their slots."""
    async with db.transaction():
        cur = await db.execute(
            """UPDATE crown_contracts SET status = 'expired'
               WHERE guild_id = ? AND status = 'active' AND deadline_cycle <= ?""",
            (guild_id, int(cycle)),
        )
    return cur.rowcount


async def create_crown_contract(
    db: Database,
    *,
    guild_id: int,
    account_type: str,
    account_id: int,
    item_key: str,
    quantity: int,
    budget: Optional[int] = None,
) -> object:
    """Sign a Crown supplier contract for one catalog item.

    The payout is locked at signing from the account's current demand. Capacity,
    value cap and the Crown's ability to cover every active contract are all
    re-read under the transaction lock.
    """
    quantity = int(quantity)
    if quantity <= 0:
        raise CrownContractError("Quantity must be greater than zero.")
    if account_type not in _TABLES:
        raise CrownContractError("Only a nation or kingdom can hold a Crown contract.")
    async with db.transaction():
        member = await refreshed_member(db, guild_id, account_type, account_id)
        if member is None or member["faction"] != CROWN:
            raise CrownContractError("Only a Crown member may commit to a supplier contract.")
        account = await get_account(db, guild_id, account_type, account_id)
        if account is None:
            raise CrownContractError("That nation or kingdom no longer exists.")

        cycle = await db.current_cycle(guild_id)
        await expire_crown_contracts(db, guild_id, cycle)
        limit = contract_slots(account_type)
        active = await db.count_active_crown_contracts(
            guild_id, account_type, account_id
        )
        if active >= limit:
            raise CrownContractError(
                f"A {account_type} may hold at most {limit} active Crown contract(s); "
                f"{active} already active. Fulfil or cancel one first."
            )

        catalog = await db.get_catalog_item(item_key)
        if catalog is None:
            raise CrownContractError("That item is not in the Royal catalog.")
        if pricing.is_bundle(catalog["key"]):
            raise CrownContractError(
                "Crown contracts buy a specific item; choose a component item, "
                "not an armor bundle."
            )
        banned = await db.banned_keys()
        if pricing.is_banned(catalog["key"], banned):
            raise CrownContractError(f"**{catalog['display_name']}** is banned from Crown purchase.")

        sale = await db.get_crown_sale(
            guild_id, account_type, account_id, catalog["key"]
        )
        demand = int(sale["demand_bp"]) if sale else config.CROWN_DEMAND_START_BP
        unit_price = int(catalog["price"])
        value = contract_value(unit_price, quantity, demand)
        if value <= 0:
            raise CrownContractError("That contract would be worth zero credits.")
        if budget is not None:
            if int(budget) <= 0:
                raise CrownContractError("The requested budget must be positive.")
            if value > int(budget):
                raise CrownContractError(
                    f"That contract is worth **{value:,}**, above the requested "
                    f"budget of **{int(budget):,}**. Raise the budget or reduce "
                    "the quantity."
                )

        cap = contract_cap(int(account["balance"]))
        if value > cap:
            max_qty = max_contract_quantity(cap, unit_price, demand)
            raise CrownContractError(
                f"That contract is worth **{value:,}**, above the "
                f"**{cap:,}** cap for a treasury of **{int(account['balance']):,}**. "
                f"The most it can commit to at this item and demand is "
                f"**{max_qty:,}** unit(s)."
            )
        crown_balance = await db.get_crown_balance(guild_id)
        reserved = await db.crown_reserved(guild_id)
        if crown_balance < reserved + value:
            raise CrownTreasuryShort(
                f"The Crown holds **{crown_balance:,}** and has already committed "
                f"**{reserved:,}**; it cannot reserve another **{value:,}**. "
                "An admin must fund it with `/faction fund faction:crown`."
            )

        deadline = cycle + config.CROWN_CONTRACT_CYCLES
        cur = await db.execute(
            """INSERT INTO crown_contracts
               (guild_id, account_type, account_id, item_key, display_name,
                quantity, delivered, unit_price, demand_bp, value, status,
                cycle, deadline_cycle, created_at, note)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 'active', ?, ?, ?, ?)""",
            (
                guild_id, account_type, account_id, catalog["key"],
                catalog["display_name"], quantity, unit_price, demand, value,
                cycle, deadline, now_iso(),
                f"Signed at {demand_percent(demand)} demand",
            ),
        )
        contract_id = int(cur.lastrowid)
        contract = await db.get_crown_contract(contract_id)
    return contract


async def _record_crown_sale(
    db: Database,
    *,
    guild_id: int,
    account_type: str,
    account_id: int,
    item_key: str,
    units: int,
    unit_price: int,
    cycle: int,
) -> int:
    """Add a fulfilled sale and decay this account's demand once per cycle."""
    sale = await db.get_crown_sale(guild_id, account_type, account_id, item_key)
    demand = int(sale["demand_bp"]) if sale else config.CROWN_DEMAND_START_BP
    last_cycle = int(sale["last_cycle"]) if sale and sale["last_cycle"] is not None else None
    demand = demand_after_cycle(demand, last_cycle, cycle)
    supplier_until = int(cycle) + config.CROWN_SUPPLIER_CYCLES
    stamp = now_iso()
    if sale is None:
        await db.execute(
            """INSERT INTO crown_sales
               (guild_id, account_type, account_id, item_key, units_sold,
                demand_bp, last_cycle, last_unit_price, supplier_until_cycle,
                updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                guild_id, account_type, account_id, item_key, int(units),
                demand, int(cycle), int(unit_price), supplier_until, stamp,
            ),
        )
    else:
        await db.execute(
            """UPDATE crown_sales
               SET units_sold = units_sold + ?, demand_bp = ?, last_cycle = ?,
                   last_unit_price = ?, supplier_until_cycle = ?, updated_at = ?
               WHERE guild_id = ? AND account_type = ? AND account_id = ?
                 AND item_key = ?""",
            (
                int(units), demand, int(cycle), int(unit_price), supplier_until,
                stamp, guild_id, account_type, account_id, item_key,
            ),
        )
    return demand


async def _delivery_plan(
    db: Database,
    *,
    guild_id: int,
    account_type: str,
    account_id: int,
    item_key: str,
    quantity: int,
):
    """Validate a Crown delivery and allocate it across active contracts.

    Must run inside the caller's transaction. Returns
    ``(account, cycle, allocations, completions, payout, remaining)``.
    """
    if quantity <= 0:
        raise FactionError("Quantity must be greater than zero.")
    if account_type not in _TABLES:
        raise FactionError("Only a nation or kingdom can supply The Crown.")
    member = await refreshed_member(db, guild_id, account_type, account_id)
    if member is None or member["faction"] != CROWN:
        raise FactionError("Only a Crown member may deliver to The Crown.")
    account = await get_account(db, guild_id, account_type, account_id)
    if account is None:
        raise FactionError("That nation or kingdom no longer exists.")

    _catalog = await db.get_catalog_item(item_key)
    if _catalog is None:
        raise FactionError("That item is not in the Royal catalog.")
    if pricing.is_bundle(_catalog["key"]):
        raise FactionError(
            "Crown deliveries are for a specific item; choose a component item, "
            "not an armor bundle."
        )

    cycle = await db.current_cycle(guild_id)
    await expire_crown_contracts(db, guild_id, cycle)
    contracts = await db.fetchall(
        """SELECT * FROM crown_contracts
           WHERE guild_id = ? AND account_type = ? AND account_id = ?
             AND item_key = ? AND status = 'active'
           ORDER BY id ASC""",
        (guild_id, account_type, account_id, item_key),
    )
    remaining = quantity
    allocations: list[tuple[object, int]] = []
    for contract in contracts:
        need = int(contract["quantity"]) - int(contract["delivered"])
        if need <= 0:
            continue
        take = min(need, remaining)
        if take:
            allocations.append((contract, take))
            remaining -= take
        if remaining <= 0:
            break

    completions = [
        contract
        for contract, take in allocations
        if int(contract["delivered"]) + take >= int(contract["quantity"])
    ]
    payout = sum(int(contract["value"]) for contract in completions)
    if payout:
        crown_balance = await db.get_crown_balance(guild_id)
        if crown_balance < payout:
            raise CrownTreasuryShort(
                f"The Crown holds **{crown_balance:,}** but this delivery "
                f"would unlock **{payout:,}**. Nothing was delivered; ask an "
                "admin to fund the Crown."
            )
    return account, cycle, allocations, completions, payout, remaining


async def _pay_crown_contract(
    db: Database,
    *,
    guild_id: int,
    account_type: str,
    account_id: int,
    label: str,
    contract,
    actor_id: int,
) -> int:
    """Pay one completed contract and write both ledger sides."""
    value = int(contract["value"])
    table = _TABLES[account_type]
    after = await db.add_balance(table, int(account_id), value)
    crown_after = await db.debit_crown(guild_id, value)
    await db.log_tx(
        guild_id=guild_id,
        kind="crown_supplier",
        account_type=account_type,
        account_id=int(account_id),
        account_name=label,
        counterparty_type="crown",
        counterparty_name="The Crown",
        amount=value,
        balance_after=after,
        actor_id=actor_id,
        note=(
            f"Crown supplier contract #{contract['id']} fulfilled: "
            f"{contract['display_name']} x{contract['quantity']}"
        ),
    )
    await db.log_tx(
        guild_id=guild_id,
        kind="crown_supplier",
        account_type="crown",
        account_id=guild_id,
        account_name="The Crown",
        counterparty_type=account_type,
        counterparty_id=int(account_id),
        counterparty_name=label,
        amount=-value,
        balance_after=crown_after,
        actor_id=actor_id,
        note=f"Paid supplier contract #{contract['id']} to {label}",
    )
    return value


async def deliver_crown_items(
    db: Database,
    *,
    guild_id: int,
    account_type: str,
    account_id: int,
    item_key: str,
    quantity: int,
    actor_id: int,
) -> DeliveryResult:
    """Record a Crown delivery, fill contracts oldest-first, and pay completions."""
    quantity = int(quantity)
    if quantity <= 0:
        raise FactionError("Quantity must be greater than zero.")
    if account_type not in _TABLES:
        raise FactionError("Only a nation or kingdom can supply The Crown.")
    async with db.transaction():
        account, cycle, allocations, completions, _payout, remaining = (
            await _delivery_plan(
                db,
                guild_id=guild_id,
                account_type=account_type,
                account_id=account_id,
                item_key=item_key,
                quantity=quantity,
            )
        )
        label = account["name"]
        paid = 0
        fulfilled: list[int] = []
        for contract, take in allocations:
            delivered = int(contract["delivered"]) + take
            complete = delivered >= int(contract["quantity"])
            await db.execute(
                """UPDATE crown_contracts
                   SET delivered = ?, status = ?, fulfilled_at = ?
                   WHERE id = ? AND status = 'active'""",
                (
                    delivered,
                    "fulfilled" if complete else "active",
                    now_iso() if complete else None,
                    int(contract["id"]),
                ),
            )
            if complete:
                paid += await _pay_crown_contract(
                    db,
                    guild_id=guild_id,
                    account_type=account_type,
                    account_id=account_id,
                    label=label,
                    contract=contract,
                    actor_id=actor_id,
                )
                fulfilled.append(int(contract["id"]))

        if completions:
            completed_units = sum(
                int(contract["quantity"]) for contract in completions
            )
            last = completions[-1]
            last_unit = int(last["value"]) // max(1, int(last["quantity"]))
            await _record_crown_sale(
                db,
                guild_id=guild_id,
                account_type=account_type,
                account_id=account_id,
                item_key=last["item_key"],
                units=completed_units,
                unit_price=last_unit,
                cycle=cycle,
            )

        note = (
            f"Supplier delivery: {quantity} unit(s), {len(fulfilled)} contract(s) "
            "fulfilled"
            if allocations
            else f"Item donation to The Crown: {quantity} unit(s)"
        )
        await db.record_faction_donation(
            guild_id=guild_id,
            account_type=account_type,
            account_id=account_id,
            faction=CROWN,
            kind="item",
            item_key=item_key,
            quantity=quantity,
            amount=paid,
            cycle=cycle,
            actor_id=actor_id,
            note=note,
        )
        treasury_after = await db.get_crown_balance(guild_id)
    return DeliveryResult(
        paid=paid,
        fulfilled=tuple(fulfilled),
        delivered=quantity - remaining,
        excess=remaining,
        treasury_after=treasury_after,
    )


async def donate_credits(
    db: Database,
    *,
    guild_id: int,
    account_type: str,
    account_id: int,
    faction: str,
    amount: int,
    actor_id: int,
) -> CreditDonation:
    """Move credits from a treasury into the faction house account."""
    amount = int(amount)
    if amount <= 0:
        raise FactionError("Donation must be greater than zero.")
    if faction not in FACTIONS:
        raise FactionError("Choose the Workers' Union or The Crown.")
    async with db.transaction():
        member = await refreshed_member(db, guild_id, account_type, account_id)
        if member is None or member["faction"] != faction:
            raise FactionError(
                f"That account does not belong to {faction_label(faction)}."
            )
        account = await get_account(db, guild_id, account_type, account_id)
        if account is None:
            raise FactionError("That nation or kingdom no longer exists.")
        cycle = await db.current_cycle(guild_id)
        table = _TABLES[account_type]
        after = await db.debit(table, int(account_id), amount)
        if faction == CROWN:
            treasury_after = await db.credit_crown(guild_id, amount)
            house_name = "The Crown"
        else:
            treasury_after = await db.credit_union(guild_id, amount)
            house_name = "Workers' Union"
        label = account["name"]
        ledger_kind = "union_donate" if faction == UNION else "faction_donate"
        await db.log_tx(
            guild_id=guild_id,
            kind=ledger_kind,
            account_type=account_type,
            account_id=int(account_id),
            account_name=label,
            counterparty_type=faction,
            counterparty_name=house_name,
            amount=-amount,
            balance_after=after,
            actor_id=actor_id,
            note=f"Donation to {house_name}",
        )
        await db.log_tx(
            guild_id=guild_id,
            kind=ledger_kind,
            account_type=faction,
            account_id=guild_id,
            account_name=house_name,
            counterparty_type=account_type,
            counterparty_id=int(account_id),
            counterparty_name=label,
            amount=amount,
            balance_after=treasury_after,
            actor_id=actor_id,
            note=f"Donation from {label}",
        )
        await db.record_faction_donation(
            guild_id=guild_id,
            account_type=account_type,
            account_id=int(account_id),
            faction=faction,
            kind="credits",
            amount=amount,
            cycle=cycle,
            actor_id=actor_id,
            note=f"Credit donation to {house_name}",
        )
    return CreditDonation(
        amount=amount, balance_after=after, treasury_after=treasury_after
    )


async def cancel_crown_contract(
    db: Database,
    *,
    guild_id: int,
    contract_id: int,
    actor_id: int,
) -> object:
    """Cancel an active contract. Delivered items are forfeited; no payment moves."""
    async with db.transaction():
        contract = await db.get_crown_contract(int(contract_id))
        if contract is None or int(contract["guild_id"]) != int(guild_id):
            raise CrownContractError("No Crown contract matches that id.")
        if contract["status"] != "active":
            raise CrownContractError(
                f"That contract is **{contract['status']}**, not active."
            )
        await db.execute(
            """UPDATE crown_contracts SET status = 'cancelled'
               WHERE id = ? AND status = 'active'""",
            (int(contract_id),),
        )
        contract = await db.get_crown_contract(int(contract_id))
    return contract


async def union_report(
    db: Database,
    *,
    guild_id: int,
    account_type: str,
    account_id: int,
) -> dict:
    """Facts Gilbert needs for an /audience union conversation."""
    account = await get_account(db, guild_id, account_type, account_id)
    member = await db.get_faction_member(guild_id, account_type, account_id)
    soldiers = await account_soldiers(
        db, guild_id, account_type, account_id, account
    )
    epoch = await db.current_cycle(guild_id)
    contributions = await db.union_contributions(
        guild_id, account_type, account_id
    )
    commit = await db.get_union_commit(guild_id, account_type, account_id)
    return {
        "guild_id": guild_id,
        "account_type": account_type,
        "account_id": account_id,
        "account_name": account["name"] if account else f"{account_type} #{account_id}",
        "account_balance": int(account["balance"]) if account else 0,
        "faction": member["faction"] if member else None,
        "faction_label": faction_label(member["faction"] if member else None),
        "status": member["status"] if member else None,
        "joined_at": member["joined_at"] if member else None,
        "soldiers": soldiers,
        "union_member": bool(
            member
            and member["faction"] == UNION
            and member["status"] == "active"
        ),
        "union_balance": await db.get_union_balance(guild_id),
        "union_members": len(await db.list_union_members(guild_id)),
        "commit": commit,
        "committed": bool(commit and commit["status"] == "active"),
        "contributions": contributions,
        "contracts_used": await db.count_contracts_for_cycle(
            guild_id, account_type, account_id, epoch, faction=UNION
        ),
        "contract_limit": int(config.CONTRACTS_PER_CYCLE.get(account_type, 0)),
        "cycle": epoch,
    }


async def crown_report(
    db: Database,
    *,
    guild_id: int,
    account_type: str,
    account_id: int,
    now: Optional[dt.datetime] = None,
) -> dict:
    """Everything Albert knows about one account, as plain data for the UI/AI."""
    now = now or dt.datetime.now(dt.timezone.utc)
    account = await get_account(db, guild_id, account_type, account_id)
    member = await db.get_faction_member(guild_id, account_type, account_id)
    soldiers = await account_soldiers(
        db, guild_id, account_type, account_id, account
    )
    faction = member["faction"] if member else None
    status = member["status"] if member else None
    joined_at = member["joined_at"] if member else None
    active, reason = tax_status(
        faction=faction,
        status=status,
        joined_at=joined_at,
        soldiers=soldiers,
        now=now,
    )
    crown_balance = await db.get_crown_balance(guild_id)
    cycle = await db.current_cycle(guild_id)
    await expire_crown_contracts(db, guild_id, cycle)
    contracts = await db.list_crown_contracts(
        guild_id, account_type=account_type, account_id=account_id, limit=10
    )
    active_contracts = await db.count_active_crown_contracts(
        guild_id, account_type, account_id
    )
    sales = await db.list_crown_sales(guild_id, account_type, account_id)
    donations = await db.list_faction_donations(
        guild_id, account_type, account_id, limit=10
    )
    union_commit = await db.get_union_commit(guild_id, account_type, account_id)
    recent_tax = []
    if faction == CROWN:
        recent_tax = await db.fetchall(
            """SELECT * FROM transactions
               WHERE guild_id = ? AND account_type = ? AND account_id = ?
                 AND kind = 'crown_tax'
               ORDER BY id DESC LIMIT 5""",
            (guild_id, account_type, account_id),
        )
    return {
        "guild_id": guild_id,
        "account_type": account_type,
        "account_id": account_id,
        "account_name": account["name"] if account else f"{account_type} #{account_id}",
        "account_balance": int(account["balance"]) if account else 0,
        "faction": faction,
        "faction_label": faction_label(faction),
        "status": status,
        "joined_at": joined_at,
        "soldiers": soldiers,
        "eligible": crown_eligible(soldiers),
        "grace_ends_at": grace_ends_at(joined_at),
        "crown_tax_active": active,
        "crown_tax_reason": reason,
        "tax_disabled": not active,
        "committed": (
            active_contracts > 0
            if faction == CROWN
            else bool(union_commit and union_commit["status"] == "active")
        ),
        "active_contracts": active_contracts,
        "contract_slots": contract_slots(account_type),
        "contracts": contracts,
        "sales": sales,
        "donations": donations,
        "union_commit": union_commit,
        "recent_tax": recent_tax,
        "crown_balance": crown_balance,
        "crown_reserved": await db.crown_reserved(guild_id),
        "cycle": cycle,
        "now": now,
    }

