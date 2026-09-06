# GitHub Control Bot

Telegram-based GitHub control center for managing repositories, files, Git operations, issues, pull requests, releases, Actions, security, notifications, and two GitHub accounts.

## Highlights

- Repository dashboard, favorites, recent repositories, search, batch operations
- File & Folder Manager with create folder/file, text input, `.txt` source, media upload, multi-file upload, ZIP extraction
- Git clone/status/diff/log/fetch/pull/push/branch/stash/cherry-pick/revert/reset/conflict helper
- Selective commit, README editor, safety backup branches, auto-sync
- Issues, pull requests, labels, milestones, releases/assets, GitHub Actions
- Collaborators, branch protection/rulesets, repository administration
- Secrets/variables and security dashboards
- Persistent Telegram state and GitHub notifications
- Two GitHub accounts with isolated tokens, repository state, cache, and clone paths

## Requirements

- Linux server
- Python 3.10+
- Git
- GitHub CLI (`gh`)
- Node.js + PM2 for production process management
- Telegram bot token
- GitHub personal access token with only the permissions you need

## Install

```bash
bash scripts/install.sh
cp .env.example .env
nano .env
.venv/bin/python check_config.py
pm2 start ecosystem.config.cjs
pm2 save
```

See [docs/SETUP.md](docs/SETUP.md) for the full setup guide and [docs/SECURITY.md](docs/SECURITY.md) for token guidance.

## Configuration

The application reads `.env` from the repository root. Account 1 uses `GITHUB_TOKEN` / `GITHUB_OWNER`; account 2 optionally uses `GITHUB_TOKEN_2` / `GITHUB_OWNER_2`. Tokens are never meant to be committed.

Runtime repositories are cloned under `LOCAL_REPO_BASE` and are ignored by Git.

## Telegram flow

`/menu` → Repository → select repository → Files & Git → File & Folder Manager.

The file manager supports creating folders and files, direct text content, `.txt` content import, documents, media, and ZIP extraction. Repositories can be cloned automatically when a local working tree is first required.

## Security

- `.env`, runtime state, logs, virtual environments, temporary files, clones, and backups are ignored.
- Restrict access with `TELEGRAM_ADMIN_IDS`.
- Use fine-grained GitHub PATs where possible and grant the minimum repository permissions required.
- Do not paste real tokens into issues, commits, Telegram chats, or documentation.

## License

MIT License. See [LICENSE](LICENSE).
