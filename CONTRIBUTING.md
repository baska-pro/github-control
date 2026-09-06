# Contributing

GitHub Control Bot is distributed under the BASKA-PRO PERSONAL USE LICENSE.

Contributions, issue reports, and improvement suggestions are welcome, but the repository license still governs use, modification, redistribution, and commercial use.

## Before submitting changes

1. Never commit `.env`, tokens, credentials, logs, runtime state, or cloned repositories.
2. Run `python -m py_compile github_control_bot.py check_config.py`.
3. Run `bash -n scripts/install.sh`.
4. Keep Telegram callbacks backward-compatible where practical.
5. Preserve safety checks for destructive GitHub/Git operations.
6. Update documentation when adding user-facing features.

## Screenshots

Put sanitized screenshots under `assets/screenshots/`. Follow the naming and privacy guidance in `assets/screenshots/README.md`.
