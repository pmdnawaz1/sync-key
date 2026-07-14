const path = require('path');

module.exports = {
  apps: [{
    name: 'synckey',
    script: process.platform === 'win32' ? 'python.exe' : 'python3',
    args: '-m synckey serve',
    cwd: __dirname,
    interpreter: 'none',
    watch: false,
    autorestart: true,
    restart_delay: 10000,
    max_restarts: 10,
    min_uptime: 5000,
    exp_backoff_restart_delay: 100,
    kill_timeout: 5000,
    max_memory_restart: '500M',
    env: {
      PYTHONUNBUFFERED: '1',
      PYTHONDONTWRITEBYTECODE: '1'
    }
  }]
};
