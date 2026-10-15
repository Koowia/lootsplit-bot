import os

def load_env():
    try:
        with open('.env', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass

load_env()

# Discord
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN', '')
OFFICER_ROLE_IDS = [int(x) for x in os.environ.get('OFFICER_ROLE_IDS', '').split(',') if x.strip()]
ALERT_OWNER_IDS = [int(x) for x in os.environ.get('ALERT_OWNER_IDS', '').split(',') if x.strip()]

# Google
GOOGLE_SCRIPT_URL = os.environ.get('GOOGLE_SCRIPT_URL', '')

# Guild
GUILD_NAME = os.environ.get('GUILD_NAME', 'Your Guild')

# Database
DB_PATH = os.environ.get('DB_PATH', 'lootsplit.db')
STARTUP_STATE_FILE = os.environ.get('STARTUP_STATE_FILE', 'startup_state.json')

# Sync
SYNC_INTERVAL_MIN = 30
HEARTBEAT_STALE_MIN = 60

# Crash alerts
CRASH_WINDOW_MIN = 10
CRASH_ALERT_THRESHOLD = 3