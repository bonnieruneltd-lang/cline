# `royal-bot/` — unused placeholder

This directory contains only this README and is **not** where the bot runs.

The live Royal Court bot lives in the parent directory of this SFTP account:

```
../bot.py
../api.py
../database.py
../cogs/
../shop/
../gilbert/
../royal_bank.db
```

See `../README.md` for the project overview, status board and file map.

Keep this folder out of deployment scripts unless it is deliberately turned
into a staging or backup area. Uploading a second copy of the bot here would
create two competing copies and a very confusing `DB_PATH`.
