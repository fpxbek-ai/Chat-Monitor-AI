"""Offline isolation tests. No Telegram accounts or native library are opened."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from account_profiles import Account, Profiles, RuntimeLock, load_profiles, prepare_account
from monitor_store import MessageStore
import multi_monitor as multi
from test_monitor import FakeBot, FakeClient, load_tdlib_handler


def profiles_at(path):
    return Profiles(Path(path) / 'accounts.ini', '/not-loaded/libtdjson.so', tuple(
        Account(slot, uid, True, frozenset(), 'Asia/Tashkent', 'qr')
        for slot, uid in [('first', 10), ('second', 20), ('third', 30)]))


class ProfilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'accounts.ini'

    def write(self, ids=(10, 20, 30), extra=''):
        self.path.write_text('[tdlib]\ntdjson_path=/not-loaded/libtdjson.so\n' + ''.join(
            f'[account:slot{i}]\nuser_id={uid}\nall_chats=true\n{extra}\n'
            for i, uid in enumerate(ids)))

    def test_three_profiles_and_self_recipients(self):
        self.write()
        p = load_profiles(self.path)
        self.assertEqual([a.recipient_id for a in p.accounts], [10, 20, 30])
        self.assertEqual(len({p.directory(a) for a in p.accounts}), 3)

    def test_reject_duplicates_four_accounts_zero_and_recipient_override(self):
        for ids, extra in [((10, 10), ''), ((10, 20, 30, 40), ''), ((0,), ''),
                           ((10,), 'notify_user_id=20')]:
            with self.subTest(ids=ids, extra=extra):
                self.write(ids, extra)
                with self.assertRaises(ValueError):
                    load_profiles(self.path)

    def test_identity_based_storage_and_private_permissions(self):
        profiles = profiles_at(self.tmp.name)
        directory = prepare_account(profiles, profiles.accounts[0])
        other = Account('first', 99, True, frozenset(), 'UTC', 'qr')
        self.assertNotEqual(directory, profiles.directory(other))
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        (directory / 'messages.db').symlink_to(Path(self.tmp.name) / 'other.db')
        with self.assertRaises(ValueError):
            prepare_account(profiles, profiles.accounts[0])

    def test_shared_lock_blocks_another_mode_and_releases(self):
        lock = Path(self.tmp.name) / '.chat-monitor.lock'
        with RuntimeLock(lock):
            with self.assertRaises(RuntimeError):
                with RuntimeLock(lock):
                    pass
        with RuntimeLock(lock):
            pass

    def test_credentials_are_slot_specific(self):
        a = profiles_at(self.tmp.name).accounts
        env = {'CHAT_MONITOR_API_ID': '123', 'CHAT_MONITOR_API_HASH': 'dummy',
               a[0].phone_env: '+10000000001', a[1].phone_env: '+10000000002'}
        self.assertNotEqual(multi.credentials(a[0], env)[2], multi.credentials(a[1], env)[2])
        with self.assertRaises(ValueError):
            multi.credentials(a[2], env)

    def test_check_is_offline_and_never_reads_credentials(self):
        self.write()
        with patch.object(multi, 'credentials', side_effect=AssertionError('must stay offline')):
            self.assertEqual(multi.main(['--config', str(self.path), '--check']), 0)


class IsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_accounts_same_chat_and_message_no_cross_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            profiles = profiles_at(root)
            bot = FakeBot()
            subjects, stores = [], []
            for account in profiles.accounts:
                client = FakeClient()
                subject = SimpleNamespace(client=client, _tracking_ready=True,
                    _get_user_fullname=AsyncMock(return_value='Author'),
                    _is_bot_user=AsyncMock(return_value=False))
                subject.register_message_tracking_handlers = lambda _s=subject, **kw: load_tdlib_handler()(_s, **kw)
                store = MessageStore(str(prepare_account(profiles, account) / 'messages.db'))
                multi.bind_tracking(subject, bot, account, store, 876)
                subjects.append(subject)
                stores.append(store)
                msg = {'chat_id': -100123, 'id': 42, 'date': 1780000000,
                       'sender_id': {'@type': 'messageSenderUser', 'user_id': 100},
                       'content': {'text': {'text': f'private-{account.slot}'}}}
                await client.handlers['new'](client, SimpleNamespace(data={'message': msg}))
                # Bot and service chat must remain excluded for every account.
                for excluded in (876, 777000):
                    await client.handlers['new'](client, SimpleNamespace(data={'message': dict(msg, chat_id=excluded)}))
                    self.assertIsNone(store.get(chat_id=excluded, message_id=42))
            client = subjects[0].client
            await client.handlers['content'](client, SimpleNamespace(data={
                'chat_id': -100123, 'message_id': 42,
                'new_content': {'text': {'text': 'first-edited'}}}))
            self.assertEqual([uid for uid, _ in bot.sent], [10])
            self.assertNotIn('private-second', bot.sent[0][1])
            client = subjects[1].client
            await client.handlers['deleted'](client, SimpleNamespace(data={
                'chat_id': -100123, 'message_ids': [42], 'is_permanent': True}))
            self.assertEqual([uid for uid, _ in bot.sent], [10, 20])
            self.assertIsNone(stores[1].get(chat_id=-100123, message_id=42))
            self.assertEqual(stores[0].get(chat_id=-100123, message_id=42).message_text, 'first-edited')
            self.assertEqual(stores[2].get(chat_id=-100123, message_id=42).message_text, 'private-third')
            subjects[2]._tracking_ready = False
            client = subjects[2].client
            await client.handlers['deleted'](client, SimpleNamespace(data={
                'chat_id': -100123, 'message_ids': [42], 'is_permanent': True}))
            self.assertEqual(len(bot.sent), 2)

    async def test_resume_only_by_default_and_explicit_authorize(self):
        account = profiles_at('/tmp').accounts[0]
        userbot = SimpleNamespace(start=AsyncMock())
        await multi.connect_account(userbot, account)
        self.assertEqual(userbot.expected_test_user_id, 10)
        self.assertFalse(userbot.start.call_args.kwargs['allow_authorization'])
        await multi.connect_account(userbot, account, authorize=True)
        self.assertTrue(userbot.start.call_args.kwargs['allow_authorization'])

    async def test_real_start_rejects_wrong_account_before_tracking(self):
        from tdlib_userbot import TdlibUserbot
        subject = object.__new__(TdlibUserbot)
        subject._settings = object()
        subject._patch_auth_input = lambda **kw: None
        subject.expected_test_user_id = 10
        subject.stop = AsyncMock()
        fake = SimpleNamespace(__aenter__=AsyncMock(), api=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=20))))
        with patch('tdlib_userbot.Client', return_value=fake):
            with self.assertRaisesRegex(RuntimeError, 'Account mismatch'):
                await subject.start(allow_authorization=False)
        self.assertFalse(subject._tracking_ready)
        subject.stop.assert_awaited_once()

    async def test_real_settings_use_distinct_session_roots(self):
        from tdlib_userbot import TdlibUserbot
        with tempfile.TemporaryDirectory() as root:
            first = TdlibUserbot(123, 'dummy', '+10000000001', '', '/missing', session_directory=str(Path(root)/'10'))
            second = TdlibUserbot(123, 'dummy', '+10000000002', '', '/missing', session_directory=str(Path(root)/'20'))
            self.assertNotEqual(first._settings.files_directory, second._settings.files_directory)
            self.assertFalse(first._settings.use_secret_chats)

    async def test_auth_states_do_not_request_login_during_resume(self):
        from tdlib_userbot import TdlibUserbot
        subject = object.__new__(TdlibUserbot)
        original = AsyncMock()
        subject.client = SimpleNamespace(_on_authorization_state_update=original)
        subject._resume_only = True
        subject._patch_auth_input(use_qr=True)
        for state in ['authorizationStateWaitPhoneNumber', 'authorizationStateWaitCode',
                      'authorizationStateWaitPassword', 'authorizationStateWaitOtherDeviceConfirmation']:
            subject._auth_needed = asyncio.get_running_loop().create_future()
            await subject.client._on_authorization_state_update(SimpleNamespace(ID=state))
            self.assertTrue(subject._auth_needed.result())
        original.assert_not_awaited()


class CommandsTests(unittest.IsolatedAsyncioTestCase):
    async def dispatch(self, uid, text='/status', private=True):
        from aiogram import Bot, Dispatcher
        from aiogram.types import Chat, Message, MessageEntity, Update, User
        bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi')
        bot.me = AsyncMock(return_value=User(id=123456789, is_bot=True, first_name='Bot', username='TestBot'))
        bot.session = AsyncMock(return_value=True)
        dp = Dispatcher()
        dp.include_router(multi.build_router(profiles_at('/tmp'), {'first': 'running', 'second': 'auth_required', 'third': 'disabled'}))
        message = Message(message_id=1, date=datetime.now(timezone.utc),
            chat=Chat(id=uid if private else -100123, type='private' if private else 'supergroup'),
            from_user=User(id=uid, is_bot=False, first_name='Person'), text=text,
            entities=[MessageEntity(type='bot_command', offset=0, length=len(text.split()[0]))])
        await dp.feed_update(bot, Update(update_id=1, message=message))
        return bot.session

    async def test_own_status_only_even_with_other_id_argument(self):
        session = await self.dispatch(10, '/status 20')
        text = session.call_args.args[1].text
        self.assertIn('Мониторинг работает', text)
        self.assertNotIn('Нужна отдельная авторизация', text)
        self.assertEqual(session.call_args.args[1].chat_id, 10)
        session = await self.dispatch(20)
        self.assertIn('Нужна отдельная авторизация', session.call_args.args[1].text)

    async def test_stranger_only_gets_own_id_and_groups_get_nothing(self):
        session = await self.dispatch(99, '/start')
        self.assertIn('ID: 99', session.call_args.args[1].text)
        self.assertNotIn('Мониторинг работает', session.call_args.args[1].text)
        session = await self.dispatch(10, private=False)
        session.assert_not_awaited()


class FakeProcess:
    def __init__(self, code=20, running=False):
        self.stdout = asyncio.StreamReader()
        self.returncode = code
        self.stopped = asyncio.Event()
        if running:
            self.stdout.feed_data(b'{"state":"running"}\n')
        if code is not None:
            self.stdout.feed_eof()
            self.stopped.set()

    async def wait(self):
        await self.stopped.wait()
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.stopped.set()
        self.stdout.feed_eof()


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_exactly_one_bot_poller_for_three_workers(self):
        profiles = profiles_at('/tmp')
        bot = AsyncMock()
        bot.get_webhook_info.return_value = SimpleNamespace(url='')
        context = AsyncMock()
        context.__aenter__.return_value = bot
        launched = []

        async def worker(_profiles, account, states):
            launched.append(account.slot)
            await asyncio.Event().wait()

        async def poll(*args, **kwargs):
            await asyncio.sleep(0)

        dispatcher = SimpleNamespace(include_router=lambda r: None, start_polling=AsyncMock(side_effect=poll))
        with patch('aiogram.Bot', return_value=context), patch('aiogram.Dispatcher', return_value=dispatcher):
            with patch.object(multi, 'supervise', side_effect=worker), patch.dict('os.environ', {'CHAT_MONITOR_BOT_TOKEN': 'dummy'}):
                await multi.run_service(profiles)
        self.assertEqual(set(launched), {'first', 'second', 'third'})
        dispatcher.start_polling.assert_awaited_once()
        bot.delete_webhook.assert_not_awaited()

    async def test_missing_session_stops_only_that_worker_no_auto_login(self):
        profiles = profiles_at('/tmp')
        states = {'second': 'running'}
        with patch.object(asyncio, 'create_subprocess_exec', AsyncMock(return_value=FakeProcess())) as spawn:
            await multi.supervise(profiles, profiles.accounts[0], states)
        self.assertEqual(states, {'first': 'auth_required', 'second': 'running'})
        self.assertNotIn('--authorize', spawn.call_args.args)
        self.assertIn('--worker', spawn.call_args.args)
        self.assertEqual(spawn.await_count, 1)

    async def test_crash_retries_then_stops_on_configuration_error(self):
        profiles = profiles_at('/tmp')
        states = {}
        with patch.object(asyncio, 'create_subprocess_exec', AsyncMock(side_effect=[FakeProcess(1), FakeProcess(21)])) as spawn:
            with patch.object(asyncio, 'sleep', AsyncMock()) as sleep:
                await multi.supervise(profiles, profiles.accounts[0], states)
        self.assertEqual(spawn.await_count, 2)
        sleep.assert_awaited_once_with(5)
        self.assertEqual(states['first'], 'configuration_error')

    async def test_service_stop_terminates_live_worker(self):
        profiles = profiles_at('/tmp')
        process = FakeProcess(None, running=True)
        states = {}
        with patch.object(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)):
            task = asyncio.create_task(multi.supervise(profiles, profiles.accounts[0], states))
            for _ in range(20):
                if states.get('first') == 'running':
                    break
                await asyncio.sleep(0)
            self.assertEqual(states['first'], 'running')
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(process.returncode, -15)


if __name__ == '__main__':
    unittest.main()
