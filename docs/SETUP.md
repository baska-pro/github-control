# Setup

## 1. Clone

```bash
git clone https://github.com/baska-pro/github-control.git
cd github-control
```

## 2. Install dependencies

```bash
bash scripts/install.sh
```

## 3. Configure

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Required values:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ADMIN_IDS`
- `GITHUB_TOKEN`
- `GITHUB_OWNER`

Optional second account:

- `GITHUB_ALIAS_2`
- `GITHUB_TOKEN_2`
- `GITHUB_OWNER_2`

Validate without printing secrets:

```bash
.venv/bin/python check_config.py
```

## 4. Run manually

```bash
.venv/bin/python github_control_bot.py
```

## 5. Run with PM2

```bash
pm2 start ecosystem.config.cjs
pm2 save
pm2 status github-control-bot
```

For PM2 startup after reboot:

```bash
pm2 startup
```

Run the command printed by PM2, then run `pm2 save` again.

## Runtime directories

The bot creates/uses:

- `data/` for persistent state/audit data
- `tmp/` for temporary runtime work
- `repos/` for local Git working trees
- `.venv/` for Python dependencies

All are excluded from Git.
