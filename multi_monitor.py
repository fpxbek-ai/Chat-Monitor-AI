"""One bot poller, up to three isolated TDLib worker processes.

No network or authorization on import or --check. Login is a separate,
interactive operation selected explicitly with --authorize SLOT.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import signal
import sys
from zoneinfo import ZoneInfo

from account_profiles import Account, Profiles, RuntimeLock, load_profiles, prepare_account

LOG = logging.getLogger('multi_monitor')
AUTH_REQUIRED = 20
CONFIG_ERROR = 21
ACCOUNT_BUSY = 22
STATE_TEXT = {
    'starting': 'Подключение…',
    'running': 'Мониторинг работает.',
    'retrying': 'Восстанавливаю подключение.',
    'auth_required': 'Нужна отдельная авторизация владельцем в терминале.',
    'configuration_error': 'Нужно проверить настройки этого аккаунта.',
    'busy': 'Сессия уже занята другим процессом.',
    'disabled': 'Мониторинг этого аккаунта выключен в настройках.',
}


def build_router(profiles: Profiles, states: dict[str, str]):
    from aiogram import Router, types
    from aiogram.filters import Command
    router = Router(name='private_account_commands')
    owners = {a.user_id: a for a in profiles.accounts}

    @router.message(Command('start', 'id', 'chatid', 'status'))
    async def command(message: types.Message):
        # Never reveal a user's connection status or account data in a group.
        if not message.from_user or message.chat.type != 'private':
            return
        if message.chat.id != message.from_user.id:
            return
        account = owners.get(message.from_user.id)
        if account is None:
            await message.answer(
                f'Ваш Telegram ID: {message.from_user.id}\n'
                'Аккаунт пока не подключён. Передайте свой ID владельцу системы. '
                'Коды входа и пароли в бота не отправляйте.')
            return
        state = states.get(account.slot, 'starting' if account.enabled else 'disabled')
        await message.answer(
            f'Ваш Telegram ID: {account.user_id}\n{STATE_TEXT.get(state, STATE_TEXT["starting"])}\n'
            'Уведомления направляются только вам. Вход через ссылку на бота '
            'сам по себе не подключает ваши переписки.')
    return router


def credentials(account: Account, env=None):
    env = os.environ if env is None else env
    api_id = int(env.get('CHAT_MONITOR_API_ID', '0'))
    api_hash = env.get('CHAT_MONITOR_API_HASH', '')
    phone = env.get(account.phone_env, '').strip()
    if api_id <= 0 or not api_hash or not phone.startswith('+') or not phone[1:].isdigit():
        raise ValueError('Set API credentials and the selected account phone environment variable')
    return api_id, api_hash, phone, env.get(account.password_env, '')


def bind_tracking(userbot, bot, account, store, bot_id):
    userbot.register_message_tracking_handlers(
        aiogram_bot=bot, notify_user_id=account.recipient_id,
        timezone_local=ZoneInfo(account.timezone), messagesx_cls=store,
        watched_chat_ids=set(account.chat_ids), all_chats=account.all_chats,
        excluded_chat_ids={777000, bot_id},
    )


async def connect_account(userbot, account, *, authorize=False, register=None):
    # Keep the legacy field for compatibility; verification also applies to
    # non-test profiles. No handler can store data before this check succeeds.
    userbot.expected_test_user_id = account.user_id
    await userbot.start(use_qr=account.auth_mode == 'qr',
                        on_client_created=register, allow_authorization=authorize)


async def run_account(profiles: Profiles, account: Account, *, authorize=False):
    from tdlib_userbot import TdlibUserbot
    api_id, api_hash, phone, password = credentials(account)
    directory = prepare_account(profiles, account)
    with RuntimeLock(directory / 'worker.lock'):
        userbot = TdlibUserbot(api_id, api_hash, phone, password, profiles.tdjson_path,
                              session_directory=str(directory / 'session'))
        try:
            if authorize:
                await connect_account(userbot, account, authorize=True)
                print('Account identity verified; session saved. Monitoring was not started.', flush=True)
                return
            from aiogram import Bot
            from monitor_store import MessageStore
            async with Bot(os.environ['CHAT_MONITOR_BOT_TOKEN']) as bot:
                me = await bot.get_me()
                store = MessageStore(str(directory / 'messages.db'))
                await connect_account(userbot, account, register=lambda: bind_tracking(
                    userbot, bot, account, store, me.id))
                print(json.dumps({'state': 'running'}), flush=True)

                async def cleanup():
                    while True:
                        store.delete_old_messages((datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
                        await asyncio.sleep(6 * 60 * 60)

                tasks = [asyncio.create_task(userbot.client.idle()), asyncio.create_task(cleanup())]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    raise RuntimeError('Worker stopped unexpectedly')
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await userbot.stop()


def exit_state(code: int) -> str:
    return {AUTH_REQUIRED: 'auth_required', CONFIG_ERROR: 'configuration_error',
            ACCOUNT_BUSY: 'busy'}.get(code, 'retrying')


async def terminate_child(process):
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def supervise(profiles, account, states):
    delay = 5
    while True:
        states[account.slot] = 'starting'
        process = None
        ready = False
        started = asyncio.get_running_loop().time()
        try:
            # No credentials in argv, no child stdin, no second getUpdates poller.
            process = await asyncio.create_subprocess_exec(
                sys.executable, '-u', str(Path(__file__).resolve()),
                '--config', str(profiles.config_path), '--worker', account.slot,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            while True:
                remaining = max(0.01, 90 - (asyncio.get_running_loop().time() - started))
                line = await asyncio.wait_for(process.stdout.readline(), timeout=None if ready else remaining)
                if not line:
                    break
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(event, dict) and event.get('state') == 'running':
                    ready = True
                    states[account.slot] = 'running'
            code = await process.wait()
            states[account.slot] = exit_state(code)
            LOG.info('Account %s: %s', account.slot, states[account.slot])
            if code in {AUTH_REQUIRED, CONFIG_ERROR, ACCOUNT_BUSY}:
                return  # Never launch login prompts or retry a mismatched identity.
        except (OSError, asyncio.TimeoutError):
            states[account.slot] = 'retrying'
            LOG.warning('Account %s: worker unavailable; retrying', account.slot)
        finally:
            if process is not None:
                await terminate_child(process)
        if ready and asyncio.get_running_loop().time() - started > 300:
            delay = 5
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


async def run_service(profiles):
    from aiogram import Bot, Dispatcher
    states = {a.slot: 'starting' if a.enabled else 'disabled' for a in profiles.accounts}
    async with Bot(os.environ['CHAT_MONITOR_BOT_TOKEN']) as bot:
        # A webhook is an existing external deployment: do not remove it silently.
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise RuntimeError('A webhook is configured; stop the other deployment first')
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(profiles, states))
        workers = [asyncio.create_task(supervise(profiles, a, states))
                   for a in profiles.accounts if a.enabled]
        try:
            await dispatcher.start_polling(bot, allowed_updates=['message'])
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


async def worker_with_signals(profiles, account):
    task = asyncio.create_task(run_account(profiles, account))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='accounts.ini')
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--check', action='store_true')
    modes.add_argument('--run', action='store_true')
    modes.add_argument('--authorize', metavar='SLOT')
    modes.add_argument('--worker', metavar='SLOT', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    os.umask(0o077)
    logging.basicConfig(level=logging.WARNING)
    try:
        profiles = load_profiles(args.config)
        if args.check:
            print(f'Configuration valid: {len(profiles.accounts)} account(s). No Telegram connection made.')
            return 0
        if args.worker:
            account = profiles.select(args.worker)
            if not account.enabled:
                return CONFIG_ERROR
            asyncio.run(worker_with_signals(profiles, account))
            return 0
        with RuntimeLock(profiles.config_path.parent / '.chat-monitor.lock'):
            if args.authorize:
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    raise ValueError('Authorization requires an interactive terminal, never bot messages or logs')
                asyncio.run(run_account(profiles, profiles.select(args.authorize), authorize=True))
            else:
                asyncio.run(run_service(profiles))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        # Never dump TDLib exceptions, phone numbers, codes, tokens, or payloads.
        text = str(error)
        if 'TEST_LOGIN_REQUIRED' in text or 'Account mismatch' in text:
            code = AUTH_REQUIRED
        elif 'already running' in text:
            code = ACCOUNT_BUSY
        elif isinstance(error, (ValueError, KeyError, FileNotFoundError)):
            code = CONFIG_ERROR
        else:
            code = 1
        print(f'Monitor stopped: {exit_state(code)}. Check configuration/session locally.', flush=True)
        return code


if __name__ == '__main__':
    raise SystemExit(main())
