"""
Telegram Monitoring Userbot
────────────────────────────
Monitors specified Telegram groups for keyword matches, forwards matching
messages via the userbot account, and fires real-time alerts through the
Telegram Bot API.

Uses Telethon's StringSession so the script runs on ephemeral hosts
(Railway, Heroku, Docker) with no on-disk session file.

Usage:
    python main.py

Environment variables (all in .env):
    API_ID, API_HASH         — Telegram userbot credentials (my.telegram.org)
    SESSION_STRING           — Telethon StringSession value; leave empty on
                               first run — the script will log in interactively,
                               print the string, and exit so you can copy it.
    BOT_TOKEN                — Telegram Bot API token for alert messages
    ALERT_CHAT_ID            — Chat/user ID that receives bot alerts
    SOURCE_GROUPS            — Comma-separated group usernames or IDs to watch
    TARGET_ACCOUNT           — Forward destination (username, ID, or "me")
    KEYWORDS                 — Comma-separated keywords to match
"""

import os
import re
import asyncio
import logging
import logging.handlers
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

load_dotenv()

# ── Reconnect / backoff constants ─────────────────────────────────────────────
MAX_RETRIES  = 10
BASE_BACKOFF = 5    # seconds
MAX_BACKOFF  = 300  # 5-minute ceiling


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_chat(val: str) -> "int | str":
    """Return an integer chat ID when val is numeric, otherwise a username string."""
    try:
        return int(val.strip())
    except ValueError:
        return val.strip()


def _sanitize_session_string(raw: str) -> str:
    """
    Normalize a SESSION_STRING that may have been mangled during copy-paste
    into a cloud dashboard (Railway, Heroku, Docker secrets, etc.).

    Handles:
    - Leading / trailing whitespace and accidental surrounding quotes (' or ")
    - Internal whitespace, spaces, tabs, newlines (\\n) and carriage returns
      (\\r) introduced when a long string wraps across lines in a web UI
    - Missing base64 padding ('=' characters) caused by truncation or
      dashboard stripping of trailing equals signs
    - Any non-base64 characters that sneak in (zero-width spaces, BOM, etc.)
    """
    if not raw:
        return raw

    # 1. Strip outer whitespace then surrounding quote characters
    cleaned = raw.strip().strip("\"'")

    # 2. Remove ALL internal whitespace (covers every Unicode whitespace class)
    cleaned = re.sub(r"\s+", "", cleaned)

    # 3. Drop any character that is not valid base64 (A-Z a-z 0-9 + / = -)
    #    Telethon uses URL-safe base64 which replaces + with - and / with _
    cleaned = re.sub(r"[^A-Za-z0-9+/=\-_]", "", cleaned)

    # 4. Re-add missing padding so len(cleaned) is a multiple of 4
    remainder = len(cleaned) % 4
    if remainder:
        cleaned += "=" * (4 - remainder)

    return cleaned


# ── Config dataclass ──────────────────────────────────────────────────────────

@dataclass
class Config:
    api_id: int
    api_hash: str
    session_string: str          # Telethon StringSession value (may be empty on first run)
    bot_token: str
    alert_chat_id: str
    source_groups: list
    target_account: "int | str"
    keywords: list
    keyword_pattern: "re.Pattern | None"

    @classmethod
    def from_env(cls) -> "Config":
        api_id_raw     = os.getenv("API_ID", "").strip()
        api_hash       = os.getenv("API_HASH", "").strip()
        session_string = _sanitize_session_string(os.getenv("SESSION_STRING", ""))
        bot_token      = os.getenv("BOT_TOKEN", "").strip()
        alert_chat     = os.getenv("ALERT_CHAT_ID", "").strip()

        if not api_id_raw or not api_hash:
            raise ValueError("API_ID and API_HASH must be set in .env")
        try:
            api_id = int(api_id_raw)
        except ValueError:
            raise ValueError(f"API_ID must be a valid integer, got: {api_id_raw!r}")

        source_raw   = os.getenv("SOURCE_GROUPS", "")
        keywords_raw = os.getenv("KEYWORDS", "")
        keywords = [k.strip().lower() for k in keywords_raw.split(",") if k.strip()]

        # Word-boundary regex — avoids "buy" matching "goodbye"
        pattern: "re.Pattern | None" = None
        if keywords:
            pat = r"\b(" + "|".join(re.escape(k) for k in keywords) + r")\b"
            pattern = re.compile(pat, re.IGNORECASE)

        return cls(
            api_id=api_id,
            api_hash=api_hash,
            session_string=session_string,
            bot_token=bot_token,
            alert_chat_id=alert_chat,
            source_groups=[_parse_chat(g) for g in source_raw.split(",") if g.strip()],
            target_account=_parse_chat(os.getenv("TARGET_ACCOUNT", "me")),
            keywords=keywords,
            keyword_pattern=pattern,
        )


# ── Logging (rotating file + stdout) ─────────────────────────────────────────

def setup_logging() -> logging.Logger:
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.log")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Rotate at 5 MB, keep 3 backups
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(ch)

    # Quieten Telethon's own verbose logging
    logging.getLogger("telethon").setLevel(logging.WARNING)

    return logging.getLogger("monitor")


# ── Telegram Bot API alert ────────────────────────────────────────────────────

async def send_alert(cfg: Config, text: str) -> None:
    """
    Fire-and-forget alert via the Telegram Bot API.
    Silently skipped when BOT_TOKEN / ALERT_CHAT_ID are not configured.
    """
    if not cfg.bot_token or not cfg.alert_chat_id:
        return

    url = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
    payload = {
        "chat_id": cfg.alert_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(url, json=payload)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        logging.getLogger("monitor").warning(f"Bot alert delivery failed: {e}")


# ── Userbot event handler ─────────────────────────────────────────────────────

def register_handler(
    client: TelegramClient, cfg: Config, logger: logging.Logger
) -> None:
    chats = cfg.source_groups if cfg.source_groups else None

    @client.on(events.NewMessage(chats=chats))
    async def handler(event: events.NewMessage.Event) -> None:
        # When monitoring all chats, skip private 1-on-1 conversations
        if not cfg.source_groups and event.is_private:
            return

        message_text: str = event.message.message or ""

        # No keywords → nothing to match
        if not cfg.keyword_pattern:
            return

        if not cfg.keyword_pattern.search(message_text):
            return

        chat_title: str = getattr(event.chat, "title", str(event.chat_id))
        logger.info(f"Keyword match in '{chat_title}' — processing…")

        # ── Forward via userbot ────────────────────────────────────────────
        try:
            await client.forward_messages(cfg.target_account, event.message)
            logger.info("Message forwarded successfully.")
        except FloodWaitError as e:
            logger.warning(f"Flood-wait imposed by Telegram: sleeping {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except RPCError as e:
            logger.error(f"Telegram RPC error during forward: {e}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Unexpected forward error: {e}")

        # ── Bot API alert ──────────────────────────────────────────────────
        # Escape HTML special chars before embedding in the message
        safe_snippet = (
            message_text[:300]
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        alert_text = (
            f"🔔 <b>Keyword match</b>\n"
            f"📌 Group: <b>{chat_title}</b>\n\n"
            f"<i>{safe_snippet}</i>"
        )
        await send_alert(cfg, alert_text)


# ── Main loop with exponential-backoff reconnection ───────────────────────────

async def run(cfg: Config, logger: logging.Logger) -> None:
    """
    Outer reconnect loop.
    On network-level failures the loop sleeps with exponential backoff and
    reconnects automatically.  Telegram RPC errors and unexpected exceptions
    follow the same policy.  A clean KeyboardInterrupt exits gracefully.
    """
    attempt = 0

    while True:
        client = TelegramClient(StringSession(cfg.session_string), cfg.api_id, cfg.api_hash)
        register_handler(client, cfg, logger)

        try:
            logger.info(f"Connecting to Telegram (attempt {attempt + 1})…")
            await client.start()

            logger.info(
                "Connected. Monitoring %s group(s), %d keyword(s).",
                len(cfg.source_groups) if cfg.source_groups else "all",
                len(cfg.keywords),
            )
            await send_alert(cfg, "✅ <b>Monitor started</b> — watching for keyword matches.")

            attempt = 0  # reset backoff counter on a successful connection

            # Telethon's event loop — blocks until disconnected
            await client.run_until_disconnected()

        except (ConnectionError, OSError, TimeoutError) as exc:
            attempt += 1
            if attempt > MAX_RETRIES:
                msg = f"Max retries ({MAX_RETRIES}) exceeded — shutting down."
                logger.critical(msg)
                await send_alert(cfg, f"🚨 {msg}")
                break

            backoff = min(BASE_BACKOFF * (2 ** (attempt - 1)), MAX_BACKOFF)
            logger.warning(
                "Network error (%s). Retry %d/%d in %ds…", exc, attempt, MAX_RETRIES, backoff
            )
            await send_alert(
                cfg,
                f"⚠️ Network error — reconnecting in {backoff}s "
                f"(attempt {attempt}/{MAX_RETRIES}).",
            )
            await asyncio.sleep(backoff)

        except RPCError as exc:
            attempt += 1
            backoff = min(BASE_BACKOFF * (2 ** (attempt - 1)), MAX_BACKOFF)
            logger.error("Telegram RPC error: %s. Retry in %ds…", exc, backoff)
            await asyncio.sleep(backoff)

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received — shutting down cleanly.")
            await send_alert(cfg, "🛑 Monitor stopped manually.")
            break

        except Exception as exc:  # noqa: BLE001
            attempt += 1
            backoff = min(BASE_BACKOFF * (2 ** (attempt - 1)), MAX_BACKOFF)
            logger.exception("Unexpected error: %s. Retry in %ds…", exc, backoff)
            await asyncio.sleep(backoff)

        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass


# ── Persist SESSION_STRING back to disk ──────────────────────────────────────

def _persist_session_string(session_string: str, script_dir: str) -> str:
    """
    Write SESSION_STRING into .env (primary) or example.env (fallback).

    Rules:
    • If SESSION_STRING=... already exists in the file (any value, including
      empty), that line is replaced in-place.
    • If the key is absent, it is appended at the end of the file.
    • If neither .env nor example.env can be written, raises RuntimeError so
      the caller can still print the string to stdout as a last resort.

    Returns the path of the file that was successfully updated.
    """
    import re as _re

    key_pattern = _re.compile(r"^SESSION_STRING\s*=.*$", _re.MULTILINE)
    new_line = f"SESSION_STRING={session_string}"

    candidates = [
        os.path.join(script_dir, ".env"),
        os.path.join(script_dir, "example.env"),
    ]

    for filepath in candidates:
        try:
            if os.path.exists(filepath):
                content = open(filepath, "r", encoding="utf-8").read()
                if key_pattern.search(content):
                    # Replace the existing SESSION_STRING line
                    updated = key_pattern.sub(new_line, content)
                else:
                    # Key absent — append (ensure trailing newline before)
                    updated = content.rstrip("\n") + "\n" + new_line + "\n"
            else:
                # File doesn't exist yet — create it (only for example.env fallback)
                updated = new_line + "\n"

            with open(filepath, "w", encoding="utf-8") as fh:
                fh.write(updated)

            return filepath

        except OSError:
            # Permission error or other I/O issue — try the next candidate
            continue

    raise RuntimeError("Could not write SESSION_STRING to .env or example.env")


# ── First-run: interactive login → StringSession generator ───────────────────

async def generate_session(api_id: int, api_hash: str, script_dir: str) -> None:
    """
    Interactive first-run flow for ephemeral environments.

    Prompts for phone number + OTP (and optional 2FA password), generates
    a StringSession, automatically persists it to .env (or example.env as
    fallback), and prints it to stdout for Railway / manual copy-paste.
    """
    print()
    print("=" * 60)
    print("  SESSION_STRING not set — running first-time login")
    print("=" * 60)
    print()

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()                    # interactive: phone → OTP → 2FA if needed
    session_string = client.session.save()
    await client.disconnect()

    # ── Persist to .env / example.env ────────────────────────────────────────
    saved_to: "str | None" = None
    try:
        saved_to = _persist_session_string(session_string, script_dir)
    except RuntimeError as exc:
        print(f"\n⚠️  Could not save automatically: {exc}")

    # ── Always print the string regardless of file-save outcome ──────────────
    print()
    print("=" * 60)
    print("  ✅  Login successful!")
    if saved_to:
        print(f"  📄  SESSION_STRING written to: {saved_to}")
    print("=" * 60)
    print()
    print(session_string)
    print()
    print("=" * 60)
    if saved_to:
        print("  The string has been saved — restart the script to begin monitoring.")
    else:
        print("  Add it to your .env / Railway variable as:")
        print("  SESSION_STRING=<paste the string above>")
    print("=" * 60)
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    logger = setup_logging()

    try:
        cfg = Config.from_env()
    except ValueError as exc:
        logging.error("Configuration error: %s", exc)
        raise SystemExit(1) from exc

    script_dir = os.path.dirname(os.path.abspath(__file__))

    # ── First-run path: SESSION_STRING missing → generate and persist ─────────
    if not cfg.session_string:
        logger.info("SESSION_STRING is empty — starting interactive session generator.")
        try:
            asyncio.run(generate_session(cfg.api_id, cfg.api_hash, script_dir))
        except KeyboardInterrupt:
            print("\nLogin cancelled.")
        raise SystemExit(0)

    # ── Normal path: SESSION_STRING present → start monitoring ───────────────
    logger.info("Starting Telegram Monitor…")
    asyncio.run(run(cfg, logger))


if __name__ == "__main__":
    main()
