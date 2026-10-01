"""
Standalone StringSession generator for the Telegram monitor.

Reads API_ID and API_HASH from the .env file in this directory (same
credentials used by main.py).  Prompts for phone number + OTP + optional
2FA password, prints the resulting SESSION_STRING, and writes it back into
.env automatically.

Run once:
    python generate_session.py
"""

import os
import re
import sys
import asyncio

# ── Locate .env next to this file ─────────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_DIR, ".env")


def _load_env(path: str) -> dict:
    """
    Minimal .env parser — handles KEY=VALUE lines, strips inline comments,
    ignores blank lines and lines starting with #.
    """
    env: dict = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, raw = line.partition("=")
            value = raw.split("#")[0].strip().strip("\"'")
            env[key.strip()] = value
    return env


def _validate_api_credentials(api_id_raw: str, api_hash: str) -> tuple:
    """
    Validate the shape of API credentials before attempting a network call.

    Returns (api_id_int, error_message).  error_message is None on success.
    """
    if not api_id_raw or not api_hash:
        return None, (
            "API_ID or API_HASH is missing from .env.\n"
            "  1. Visit https://my.telegram.org → API development tools\n"
            "  2. Create an app and copy the api_id (integer) and api_hash\n"
            "  3. Paste them into .env as API_ID=<int> and API_HASH=<hash>"
        )

    try:
        api_id = int(api_id_raw)
    except ValueError:
        return None, f"API_ID must be an integer; got: {api_id_raw!r}"

    if api_id <= 0:
        return None, f"API_ID must be a positive integer; got: {api_id}"

    if len(api_hash) != 32 or not re.fullmatch(r"[0-9a-f]{32}", api_hash):
        return None, (
            f"API_HASH looks wrong (expected 32 hex characters); got: {api_hash!r}\n"
            "  Double-check the value at https://my.telegram.org"
        )

    return api_id, None


def _write_session_to_env(env_path: str, session_string: str) -> bool:
    """Replace or append SESSION_STRING in .env.  Returns True on success."""
    key_pattern = re.compile(r"^SESSION_STRING\s*=.*$", re.MULTILINE)
    new_line = f"SESSION_STRING={session_string}"
    try:
        content = open(env_path, encoding="utf-8").read() if os.path.exists(env_path) else ""
        if key_pattern.search(content):
            updated = key_pattern.sub(new_line, content)
        else:
            updated = content.rstrip("\n") + "\n" + new_line + "\n"
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write(updated)
        return True
    except OSError:
        return False


async def _generate(api_id: int, api_hash: str) -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import (
        ApiIdInvalidError,
        ApiIdPublishedFloodError,
        PhoneNumberInvalidError,
        PhoneCodeInvalidError,
        PhoneCodeExpiredError,
        SessionPasswordNeededError,
        PasswordHashInvalidError,
        FloodWaitError,
    )

    print()
    print("=" * 60)
    print("  Telegram StringSession Generator")
    print("=" * 60)
    print(f"  api_id : {api_id}")
    print(f"  api_hash: {api_hash[:6]}…{api_hash[-4:]}")
    print("=" * 60)
    print()

    client = TelegramClient(StringSession(), api_id, api_hash)

    try:
        await client.start()
    except ApiIdInvalidError:
        print()
        print("ERROR: ApiIdInvalidError — Telegram rejected these credentials.")
        print()
        print("  The api_id / api_hash combination is not recognised by Telegram.")
        print("  This usually means one of:")
        print("    • The values in .env belong to a different Telegram account or app")
        print("    • The app was deleted at https://my.telegram.org")
        print("    • The api_hash was copied incorrectly (extra/missing character)")
        print()
        print("  Fix:")
        print("    1. Go to  https://my.telegram.org  and log in with the phone")
        print("       number you want to monitor.")
        print("    2. Open 'API development tools'.")
        print("    3. Copy the App api_id (integer) and App api_hash (32-char hex).")
        print("    4. Update API_ID and API_HASH in:")
        print(f"         {_ENV_PATH}")
        print("    5. Re-run:  python generate_session.py")
        print()
        raise SystemExit(1)
    except ApiIdPublishedFloodError:
        print("ERROR: These API credentials have been flagged for abuse by Telegram.")
        print("  Create a new app at https://my.telegram.org and update .env.")
        raise SystemExit(1)
    except PhoneNumberInvalidError:
        print("ERROR: The phone number you entered is not valid.")
        raise SystemExit(1)
    except PhoneCodeInvalidError:
        print("ERROR: The verification code was incorrect.")
        raise SystemExit(1)
    except PhoneCodeExpiredError:
        print("ERROR: The verification code expired.  Re-run the script to request a new one.")
        raise SystemExit(1)
    except FloodWaitError as e:
        print(f"ERROR: Telegram rate-limit — wait {e.seconds}s before retrying.")
        raise SystemExit(1)

    session_string = client.session.save()
    await client.disconnect()

    saved = _write_session_to_env(_ENV_PATH, session_string)

    print()
    print("=" * 60)
    print("  Login successful!")
    if saved:
        print(f"  SESSION_STRING written to: {_ENV_PATH}")
    print("=" * 60)
    print()
    print(session_string)
    print()
    if not saved:
        print("  (Could not write to .env — copy the string above and set it manually.)")
    print("=" * 60)
    print()


def main() -> None:
    env = _load_env(_ENV_PATH)
    api_id_raw = env.get("API_ID", os.getenv("API_ID", "")).strip()
    api_hash   = env.get("API_HASH", os.getenv("API_HASH", "")).strip()

    api_id, err = _validate_api_credentials(api_id_raw, api_hash)
    if err:
        print(f"\nConfiguration error:\n  {err}\n")
        raise SystemExit(1)

    try:
        asyncio.run(_generate(api_id, api_hash))
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(0)


if __name__ == "__main__":
    main()
