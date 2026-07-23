/**
 * PM2 production ecosystem — IG Agent v31 Trading Desk
 *
 * DO NOT `pm2 start` while a broker position is open or another agent
 * already binds :8080 (launchd / instance lock). Flatten first, then:
 *   pm2 start ecosystem.config.js --env production
 */
module.exports = {
  apps: [
    {
      name: "ig-agent-v31",
      cwd: __dirname,
      script: ".venv/bin/python3",
      args: ["src/main.py"],
      interpreter: "none",
      instances: 1,
      exec_mode: "fork",
      autorestart: true,
      max_restarts: 20,
      min_uptime: "30s",
      kill_timeout: 30000,
      listen_timeout: 120000,
      exp_backoff_restart_delay: 2000,
      env: {
        NODE_ENV: "development",
        PYTHONPATH: "src",
        IG_AGENT_CONFIG: "config/config_v31_demo_throughput.json",
        PYTHONUNBUFFERED: "1",
      },
      env_production: {
        NODE_ENV: "production",
        PYTHONPATH: "src",
        IG_AGENT_CONFIG: "config/config_v31_demo_throughput.json",
        PYTHONUNBUFFERED: "1",
      },
      out_file: "src/data/v31-production/logs/sys_stdout.log",
      error_file: "src/data/v31-production/logs/sys_stderr.log",
      merge_logs: true,
      time: true,
    },
  ],
};
