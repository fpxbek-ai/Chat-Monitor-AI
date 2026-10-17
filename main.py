"""Chat Monitor AI v0.3: explicitly permitted chats only, test-account mode."""
from __future__ import annotations

import asyncio
import configparser
from datetime import datetime, timedelta, timezone
import logging
import os
import sys

from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
import pytz

from monitor_store import MessageStore, parse_chat_ids

logger = logging.getLogger("chat_monitor")
logging.basicConfig(level=logging.INFO)


def read_settings(*, discovery=False):
    config = configparser.ConfigParser(interpolation=None)
    if not config.read("config.ini"):
        raise RuntimeError("config.ini not found. Keep it local and out of Git.")
    user_id = int(config["telegram"]["user_id"].strip('"'))
    timezone_name = config.get("timezone", "name", fallback="Asia/Tashkent").strip('"')
    tz = pytz.timezone(timezone_name)
    all_chats = config.getboolean("monitor", "all_chats", fallback=False)
    chat_ids = set() if discovery or all_chats else parse_chat_ids(config.get("monitor", "chat_ids", fallback=""))
    return config, user_id, tz, chat_ids


def build_router(owner_id: int, test_user_id: int = 0) -> Router:
    router = Router(name="chat_monitor_private_commands")
    allowed_users = {uid for uid in (owner_id, test_user_id) if uid > 0}

    @router.message(Command("start"))
    async def start_command(message: types.Message):
        if not message.from_user or message.from_user.id not in allowed_users:
            return
        await message.answer("Бот работает. В тестовой группе отправьте /chatid или /id с вашего разрешённого аккаунта.")

    @router.message(Command("chatid", "id"))
    async def chatid_command(message: types.Message):
        if not message.from_user or message.from_user.id not in allowed_users:
            return
        await message.answer(
            f"Chat ID (TDLib test candidate): <code>{message.chat.id}</code>\n"
            "Copy this number into [monitor] chat_ids in your local config.ini.",
            parse_mode="HTML",
        )
    return router


async def chat_id_only(bot: Bot, owner_id: int, test_user_id: int = 0) -> None:
    dp = Dispatcher()
    dp.include_router(build_router(owner_id, test_user_id))
    await bot.delete_webhook(drop_pending_updates=False)
    me = await bot.get_me()
    print(f"CHAT-ID MODE READY: @{me.username}; send /chatid@{me.username} in your TEST group. Keep this process running.", flush=True)
    await dp.start_polling(bot)


async def monitor(bot: Bot, config, owner_id: int, tz, chat_ids: set[int]) -> None:
    from tdlib_userbot import init_userbot

    all_chats = config.getboolean("monitor", "all_chats", fallback=False)
    if not chat_ids and not all_chats:
        raise RuntimeError("No [monitor] chat_ids configured: monitoring is disabled (safe default).")
    if not all_chats and (len(chat_ids) != 1 or next(iter(chat_ids)) >= 0):
        raise RuntimeError("Exactly one test group is required")
    expected_id = config.getint("monitor", "test_user_id", fallback=0)
    if expected_id <= 0:
        raise RuntimeError("Set [monitor] test_user_id to the separate test account ID")
    tdlib_cfg = config["tdlib"]
    api_id = int(os.environ["CHAT_MONITOR_API_ID"])
    api_hash = os.environ["CHAT_MONITOR_API_HASH"]
    path = tdlib_cfg["tdjson_path"].strip('"')
    phone = os.environ.get("CHAT_MONITOR_TEST_PHONE", "").strip()
    if not phone:
        raise RuntimeError("CHAT_MONITOR_TEST_PHONE must identify the separate test account")
    password = os.environ.get("CHAT_MONITOR_TEST_PASSWORD", "")
    mode = tdlib_cfg.get("auth_mode", "qr").strip('"').lower()

    os.umask(0o077)
    store = MessageStore("messages.db")
    bot_identity = await bot.get_me()
    userbot = init_userbot(api_id, api_hash, phone, password, path)
    userbot.expected_test_user_id = expected_id

    # Register TDLib handlers before the account is authenticated so no initial
    # update can bypass the explicit allowlist by arriving before registration.
    def register_handlers():
        userbot.register_message_tracking_handlers(
            aiogram_bot=bot,
            notify_user_id=owner_id,
            timezone_local=tz,
            messagesx_cls=store,
            watched_chat_ids=chat_ids,
            all_chats=all_chats,
            excluded_chat_ids={777000, bot_identity.id},
        )

    dp = Dispatcher()
    dp.include_router(build_router(owner_id, expected_id))
    logger.info("Tracking scope: %s", "all test-account chats" if all_chats else sorted(chat_ids))
    try:
        await userbot.start(use_qr=(mode != "code"), on_client_created=register_handlers,
                            allow_authorization="--authorize-test-account" in sys.argv)
    except BaseException:
        await userbot.stop()
        raise
    await bot.delete_webhook(drop_pending_updates=False)

    async def cleanup():
        while True:
            await asyncio.sleep(6 * 60 * 60)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            store.delete_old_messages(cutoff)

    task = asyncio.create_task(cleanup())
    try:
        await asyncio.gather(dp.start_polling(bot), userbot.client.idle())
    finally:
        task.cancel()
        await userbot.stop()


async def main():
    if "--chat-id-only" not in sys.argv and not ("--run-test-monitor" in sys.argv and any(flag in sys.argv for flag in ("--authorize-test-account", "--resume-test-account"))):
        print("SAFE MODE: Telegram is not started. Explicit test-run and authorization flags required.")
        return
    config, owner_id, tz, chat_ids = read_settings(discovery="--chat-id-only" in sys.argv)
    token = os.environ["CHAT_MONITOR_BOT_TOKEN"]
    async with Bot(token=token) as bot:
        if "--chat-id-only" in sys.argv:
            await chat_id_only(bot, owner_id, int(config.get("monitor", "test_user_id", fallback="0").strip(chr(34))))
        else:
            if not chat_ids and not config.getboolean("monitor", "all_chats", fallback=False):
                print("SAFE MODE: no monitored chat IDs. Use python main.py --chat-id-only first.")
                return
            await monitor(bot, config, owner_id, tz, chat_ids)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped by user")
