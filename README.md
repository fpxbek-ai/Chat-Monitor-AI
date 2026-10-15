# Chat Monitor AI — test-group mode

This working copy monitors only one explicitly allowed test group.
The upstream description below does not describe the current safety restrictions.
See README_TEST_MODE_RU.md for current setup and offline verification.

---

# all-seeing Telegram Bot

A Telegram userbot that monitors your private chats and notifies you about **edited** and **deleted** messages — including media. Built on [TDLib](https://core.telegram.org/tdlib) via [aiotdlib](https://github.com/pylakey/aiotdlib), running as a regular user account with no Business subscription required.

> Fork of [Pfauberg/all-seeing-Telegram-bot](https://github.com/Pfauberg/all-seeing-Telegram-bot) — rewritten to use TDLib instead of the Telegram Business Bot API.

---

## ✨ Features

| Feature | Description |
|---|---|
| 🔍 **Edited messages** | Catches any edit in your private chats and shows you the original text |
| 🗑️ **Deleted messages** | Saves a copy before deletion — text and media |
| 🖼️ **Full media support** | Photo, video, voice, video note, animation, sticker, document, audio |
| 🔥 **Self-destruct messages** | Immediately downloads and forwards timer-deleted / view-once media |
| 👤 **New user detection** | Notifies you when a new person messages you for the first time |
| 📱 **QR-code login** | Default auth method — scan once in Telegram, no SMS needed |
| 🌑 **Offline mode** | Account stays offline while the bot runs — no online indicator shown |
| 🌍 **Multilingual** | English and Russian built-in; easy to add more |
| 🚫 **No Business required** | Works on any Telegram account, no Premium/Business subscription needed |

---

## 🏗️ How It Works

```
Your Telegram account (TDLib userbot)
        │
        │  updateNewMessage       → saves message + media to SQLite
        │  updateMessageEdited    → fetches new content, sends diff to you
        │  updateDeleteMessages   → retrieves saved copy, sends it to you
        ▼
  Notification Bot (aiogram)  →  your Telegram account
```

- **TDLib userbot** (`tdlib_userbot.py`) connects as *your* account and listens to all private chat events in real-time.
- **Notification bot** (`main.py`, aiogram) sends formatted alerts to your Telegram user ID.
- **SQLite** stores message history for 30 days (auto-cleaned at midnight).
- Both run concurrently in a single process via `asyncio.gather`.

---

## ⚙️ Setup

### Step 1 — Create a notification bot

1. Open [@BotFather](https://t.me/botfather) → `/newbot`
2. Copy the **token**

### Step 2 — Get your Telegram API credentials

1. Go to [my.telegram.org](https://my.telegram.org) → **API development tools**
2. Create an app → copy **App api_id** and **App api_hash**

### Step 3 — Get TDLib for your platform

**Windows:**
Download `tdjson.dll` from a prebuilt binary or build from source.
Place it anywhere and note the full path.

**macOS (Apple Silicon):**
```bash
brew install tdlib
# Library will be at: /opt/homebrew/lib/libtdjson.dylib
```

**macOS (Intel):**
```bash
brew install tdlib
# Library will be at: /usr/local/lib/libtdjson.dylib
```

**Linux:**
```bash
# Ubuntu/Debian example — or build from source
sudo apt install libtdjson-dev
# Library will be at: /usr/local/lib/libtdjson.so
```

### Step 4 — Configure

Copy `config.ini.exemple` → `config.ini` and fill in your values:

```ini
[telegram]
token   = "YOUR_BOT_TOKEN"
user_id = "YOUR_TELEGRAM_USER_ID"

[timezone]
name = "Europe/Moscow"   ; see https://en.wikipedia.org/wiki/List_of_tz_database_time_zones

[settings]
language = "ru"          ; ru | en

[tdlib]
api_id      = "YOUR_API_ID"
api_hash    = "YOUR_API_HASH"
phone       = "+7XXXXXXXXXX"
password    = ""         ; 2FA password, leave empty if none
tdjson_path = "/opt/homebrew/lib/libtdjson.dylib"  ; path to tdjson library
auth_mode   = "qr"      ; qr (default) | code — login via QR scan or SMS code
```

> **How to find your user ID:** Send `/start` to your bot, or use [@userinfobot](https://t.me/userinfobot).

### Step 5 — Install and run

**Windows:**
```bat
run.bat
```

**macOS / Linux:**
```bash
chmod +x run.sh
./run.sh
```

**Or manually:**
```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

> **First run — QR mode (default):** A QR code is printed in the terminal. Open Telegram on your phone → **Settings → Devices → Link Desktop Device** and scan it. The session is saved locally and reused on subsequent runs.
>
> **First run — code mode** (`auth_mode = "code"` in config): TDLib sends a confirmation code to your Telegram app or via SMS. Enter it when prompted.
>
> 🌑 **Offline mode:** The account stays offline while the bot is running — no online status is shown to your contacts.

---

## 📩 Notification Examples

**New user:**
```
👤 [JOHN]
ID: 123456789
```

**Edited message:**
```
✏️ [JOHN] 123456789
Message from 23/03/25 01:15

Changed from:
"Hello how are you"

To:
"Hello, how are you?"
```

**Deleted message:**
```
🗑️ [JOHN] 123456789
Message from 23/03/25 01:15

Deleted:
"Meet at 9pm"
```

**Self-destructing message:**
```
🔥 Self-destructing message [⏱ 10s]
👤 JOHN

[photo/video attached]
```

---

## 🌐 Adding a Language

1. Copy `languages/en.py` to `languages/xx.py`
2. Translate the format strings
3. Set `language = "xx"` in `config.ini`

---

## 📁 Project Structure

```
├── main.py              # Aiogram bot + startup orchestration
├── tdlib_userbot.py     # TDLib userbot: auth, event handlers, gift tools
├── config.ini           # Your config (not committed)
├── config.ini.exemple   # Config template
├── languages/
│   ├── en.py            # English strings
│   └── ru.py            # Russian strings
├── messages.db          # SQLite message history (auto-created)
├── users.db             # SQLite user registry (auto-created)
├── run.bat              # Windows launcher
└── run.sh               # macOS/Linux launcher
```

---

## ⚠️ Disclaimer

This tool is intended for personal use on your own account to track messages sent *to you*. Use responsibly and in accordance with Telegram's Terms of Service.
