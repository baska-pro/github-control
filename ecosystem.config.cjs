const path = require('path');
const root = __dirname;
module.exports = {
  apps: [{
    name: 'github-control-bot',
    script: path.join(root, 'github_control_bot.py'),
    cwd: root,
    interpreter: path.join(root, '.venv', 'bin', 'python'),
    autorestart: true,
    restart_delay: 5000,
    max_restarts: 10,
    min_uptime: '10s',
    time: true,
    merge_logs: true,
    env: { PYTHONUNBUFFERED: '1' }
  }]
};
