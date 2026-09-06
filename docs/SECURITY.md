# Security

## Tokens

Keep all real credentials in `.env` only. Never commit `.env`.

Recommended:

- Use GitHub fine-grained PATs where possible.
- Grant access only to repositories that need to be managed.
- Grant only the permissions required by the features you use.
- Rotate a token immediately if it is ever exposed.
- Use separate tokens for account 1 and account 2.

## Telegram access

Set `TELEGRAM_ADMIN_IDS` to trusted numeric Telegram user IDs only. The bot rejects users outside this allowlist.

## Runtime data

The following paths are intentionally excluded from Git:

- `.env`
- `.venv/`
- `data/`
- `tmp/`
- `repos/`
- logs and backup files

## Server permissions

Recommended:

```bash
chmod 600 .env
chmod 700 data tmp repos
```

Run the bot under a dedicated non-root user where possible.
