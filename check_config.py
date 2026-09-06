#!/usr/bin/env python3
from pathlib import Path
import os, sys
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
required = ('TELEGRAM_BOT_TOKEN','GITHUB_TOKEN','TELEGRAM_ADMIN_IDS')
missing = [k for k in required if not os.getenv(k,'').strip()]
if missing:
    print('Configuration incomplete: ' + ', '.join(missing))
    sys.exit(1)
print('Configuration OK')
print('GitHub account 1: configured')
print('GitHub account 2: ' + ('configured' if os.getenv('GITHUB_TOKEN_2','').strip() else 'not configured'))
sys.exit(0)
