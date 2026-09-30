# Telegram Keyword Monitor

A real-time Telegram threat intelligence and keyword monitoring userbot. It watches specified groups and channels for configurable keywords, forwards matching messages to a target account, and fires instant alerts through the Telegram Bot API — running continuously on Railway (or any other host) with no on-disk session file.

---

## Features

- **Real-time monitoring** — event-driven via Telethon; zero polling, near-zero CPU idle usage
- **Keyword matching** — word-boundary regex (avoids false positives like "buyer" matching "buy")
- **Dual notification** — forwards the original message via the userbot *and* sends a formatted Bot API alert
- **Ephemeral-safe** — uses `StringSession` so no `.session` file is needed; works on Railway, Heroku, Docker
- **Auto-reconnect** — exponential back-off (5 s → 300 s cap) on network drops; up to 10 retries before alerting and shutting down gracefully
- **Rotating logs** — `monitor.log` rotates at 5 MB (3 backups kept)

---

## Requirements

| Dependency | Purpose |
|---|---|
| Python ≥ 3.10 | Runtime |
| `telethon` | Telegram userbot / MTProto client |
| `httpx` | Async HTTP calls to the Bot API |
| `python-dotenv` | Loads `.env` into `os.environ` |

Install everything at once:

```bash
pip install -r requirements.txt
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in the values before running.

| Variable | Required | Description |
|---|---|---|
| `API_ID` | ✅ | Integer app ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | ✅ | App hash from [my.telegram.org](https://my.telegram.org) |
| `SESSION_STRING` | ✅ (after first run) | Telethon `StringSession` value — generated on first run |
| `BOT_TOKEN` | ⬜ | Bot token from [@BotFather](https://t.me/BotFather) — enables Bot API alerts |
| `ALERT_CHAT_ID` | ⬜ | Chat / user ID that receives Bot API alert messages |
| `SOURCE_GROUPS` | ⬜ | Comma-separated group usernames or numeric IDs to watch. Leave empty to watch **all** groups the account is in |
| `TARGET_ACCOUNT` | ⬜ | Forward destination: username, numeric ID, or `me` (Saved Messages). Defaults to `me` |
| `KEYWORDS` | ⬜ | Comma-separated keywords to match (case-insensitive word boundaries). Defaults to `urgent,buy,sell,important` |

### Example `.env`

```dotenv
API_ID=12345678
API_HASH=0123456789abcdef0123456789abcdef
SESSION_STRING=                        # leave empty on first run

BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
ALERT_CHAT_ID=-1001234567890

SOURCE_GROUPS=-1009876543210,crypto_signals,intel_feed
TARGET_ACCOUNT=me
KEYWORDS=urgent,breach,alert,sell,buy
```

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/your-username/telegram-monitor.git
cd telegram-monitor
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env — fill in API_ID, API_HASH, and optionally BOT_TOKEN + ALERT_CHAT_ID
```

### 3. Generate a StringSession (first run only)

Leave `SESSION_STRING` empty in `.env`, then run:

```bash
python main.py
```

The script detects the missing session and starts an interactive login:

```
============================================================
  SESSION_STRING not set — running first-time login
============================================================

Please enter your phone number: +1 555 000 1234
Please enter the code you received: 12345

============================================================
  ✅  Login successful!
  📄  SESSION_STRING written to: /path/to/.env
============================================================

1BVtsOHoBu3GmFkd0nXnP7q...   ← the session string (also saved to .env)
```

The generated value is **automatically written back into `.env`** (or `example.env` if `.env` is not writable). Restart the script — login will not be prompted again.

### 4. Run locally

```bash
python main.py
```

Logs appear on stdout and in `monitor.log`.

---

## Running in the Background (macOS)

### Option A — launchd (recommended: survives reboots and terminal close)

```bash
# Install the bundled plist
cp com.user.telegram-monitor.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.user.telegram-monitor.plist

# Check status
launchctl list | grep telegram

# Tail live logs
tail -f monitor.log

# Stop
launchctl unload ~/Library/LaunchAgents/com.user.telegram-monitor.plist
```

### Option B — tmux

```bash
tmux new-session -d -s monitor \
  "cd ~/path/to/telegram-monitor && source venv/bin/activate && python main.py"

# Attach: tmux attach -t monitor
# Detach: Ctrl+B then D
```

### Option C — nohup

```bash
nohup venv/bin/python main.py >> monitor.log 2>&1 &
echo $! > monitor.pid
```

---

## Deploying to Railway

Railway uses ephemeral storage — files written to disk do not survive restarts. The `StringSession` approach means no session file is ever needed.

### Steps

1. **Generate your `SESSION_STRING` locally** (see [Setup → Step 3](#3-generate-a-stringsession-first-run-only) above).

2. **Push the project to GitHub** (`.env` and `*.session` are already in `.gitignore`).

3. **Create a new Railway project** → connect your GitHub repo.

4. **Set environment variables** in Railway → Variables:

   | Key | Value |
   |---|---|
   | `API_ID` | your integer app ID |
   | `API_HASH` | your app hash |
   | `SESSION_STRING` | the long string from step 1 |
   | `BOT_TOKEN` | your bot token |
   | `ALERT_CHAT_ID` | target chat ID |
   | `KEYWORDS` | `urgent,buy,sell` |
   | `SOURCE_GROUPS` | group IDs / usernames |
   | `TARGET_ACCOUNT` | `me` or a chat ID |

5. **Deploy.** Railway detects `requirements.txt` and runs `python main.py` automatically.

> **Note:** Railway's Start Command should be `python main.py`. Set it under *Settings → Deploy → Custom Start Command* if it is not detected automatically.

---

## Project Structure

```
telegram-monitor/
├── main.py                              # Application entry point
├── requirements.txt                     # Python dependencies
├── .env.example                         # Template — copy to .env and fill in
├── .gitignore                           # Keeps secrets and artifacts out of git
├── com.user.telegram-monitor.plist      # macOS launchd agent
└── monitor.log                          # Runtime log (git-ignored, auto-rotated)
```

---

## Security Notes

- **Never commit `.env`** — it contains your Telegram API credentials. The `.gitignore` prevents this, but double-check with `git status` before pushing.
- **`SESSION_STRING` = full account access.** Treat it like a password. Store it only in Railway Variables or a secrets manager, never in source code.
- **`*.session` files** are also git-ignored. If you run locally they are created in the project directory but never tracked.
- The Bot API alert token (`BOT_TOKEN`) is separate from the userbot credentials — an attacker with only the bot token can send messages to `ALERT_CHAT_ID` but cannot access your Telegram account.

---

## License

MIT
