"""
checks.py
=========
Reusable app-command checks.

Admins are identified by Discord's built-in *administrator* permission
(per the user's decision). Leader checks are done inline in the cogs
because they depend on the specific account being acted upon.
"""

from __future__ import annotations

import discord
from discord import app_commands

from utils import is_admin


class NotAdmin(app_commands.CheckFailure):
    """Raised when a non-admin invokes an admin-only command."""


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            raise NotAdmin("You need the **Administrator** permission to use this command.")
        return True

    return app_commands.check(predicate)
