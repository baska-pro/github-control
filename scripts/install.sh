#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

command -v python3 >/dev/null 2>&1 || { echo "python3 is required"; exit 1; }
command -v git >/dev/null 2>&1 || { echo "git is required"; exit 1; }
command -v gh >/dev/null 2>&1 || { echo "GitHub CLI (gh) is required"; exit 1; }

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
mkdir -p data tmp repos
chmod 700 data tmp repos

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env from .env.example. Fill it before starting the bot."
else
  chmod 600 .env
fi

echo "Install complete. Validate with: .venv/bin/python check_config.py"
