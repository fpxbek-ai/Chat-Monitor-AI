"""Offline tests: no Telegram login, network, aiotdlib or bot token required."""
import asyncio
import ast
from datetime import timezone
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Set
import unittest

from monitor_store import MessageStore, parse_chat_ids


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = MessageStore(str(Path(self.tmp.name) / 'messages.db'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_isolate_same_message_id_across_groups(self):
        self.assertTrue(self.db.add(chat_id=-100111, message_id=10, author_id=1, author_name='A', message_text='one', timestamp='2026-10-15T01:00:00+00:00'))
        self.assertTrue(self.db.add(chat_id=-100222, message_id=10, author_id=2, author_name='B', message_text='two', timestamp='2026-10-15T01:00:00+00:00'))
        self.assertFalse(self.db.add(chat_id=-100111, message_id=10, author_id=5, author_name='E', message_text='duplicate', timestamp='2026-10-15T01:00:00+00:00'))
        self.assertEqual(self.db.get(chat_id=-100111, message_id=10).message_text, 'one')
        self.db.update(chat_id=-100111, message_id=10, message_text='changed', media_type='text')
        self.assertEqual(self.db.get(chat_id=-100222, message_id=10).message_text, 'two')
        self.db.delete(chat_id=-100111, message_id=10)
        self.assertIsNone(self.db.get(chat_id=-100111, message_id=10))
        self.assertIsNotNone(self.db.get(chat_id=-100222, message_id=10))

    def test_allowlist_default_deny_and_validation(self):
        self.assertEqual(parse_chat_ids(''), set())
        self.assertEqual(parse_chat_ids('"-100111,-100222"'), {-100111, -100222})
        with self.assertRaises(ValueError):
            parse_chat_ids('-1001,ALL')
        with self.assertRaises(ValueError):
            parse_chat_ids('0')


# Execute only the actual handler method from the provided TDLib source, while
# injecting a local fake TDLib event system. This tests edits/deletions offline.
import __future__
import os


def load_tdlib_handler():
    path = Path(__file__).with_name('tdlib_userbot.py')
    tree = ast.parse(path.read_text())
    td_class = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == 'TdlibUserbot')
    handler = next(x for x in td_class.body if isinstance(x, ast.FunctionDef) and x.name == 'register_message_tracking_handlers')
    code = compile(ast.Module(body=[handler], type_ignores=[]), str(path), 'exec', flags=__future__.annotations.compiler_flag)
    names = SimpleNamespace(UPDATE_NEW_MESSAGE='new', UPDATE_MESSAGE_EDITED='edited', UPDATE_MESSAGE_CONTENT='content', UPDATE_DELETE_MESSAGES='deleted')
    globs = {
        'API': SimpleNamespace(Types=names), 'Client': object,
        'Set': Set, 'asyncio': asyncio, 'os': os, 'logger': logging.getLogger('test'),
        '_as_dict': lambda o: o if isinstance(o, dict) else getattr(o, 'data', {}),
        '_extract_text_from_content': lambda content: (content.get('text', {}) or {}).get('text', ''),
        '_extract_tdlib_file': lambda content: (None, 'text'),
        '_is_self_destruct': lambda data: bool(data.get('self_destruct_type')),
    }
    helpers = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in {'_as_dict', '_extract_text_from_content', '_extract_tdlib_file', '_is_self_destruct'}) or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_CONTENT_MEDIA_MAP' for t in n.targets))]
    exec(compile(ast.Module(body=helpers, type_ignores=[]), str(path), 'exec', flags=__future__.annotations.compiler_flag), globs)
    exec(code, globs)
    return globs['register_message_tracking_handlers']


class FakeClient:
    def __init__(self):
        self.handlers = {}

    def on_event(self, typ):
        def decorator(f):
            self.handlers[typ] = f
            return f
        return decorator


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, parse_mode=None):
        self.sent.append((uid, text))


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MessageStore(str(Path(self.tmp.name) / 'messages.db'))
        self.client = FakeClient()
        self.bot = FakeBot()
        self.subject = SimpleNamespace(client=self.client, _tracking_ready=True)

        async def get_name(uid):
            return f'Person {uid}'

        async def is_bot(uid):
            return False

        async def raw_query(payload):
            return {'content': {'text': {'text': 'edited from getMessage'}}}

        self.subject._get_user_fullname = get_name
        self.subject._is_bot_user = is_bot
        self.subject._raw_request = raw_query
        load_tdlib_handler()(self.subject, aiogram_bot=self.bot, notify_user_id=99,
                             timezone_local=timezone.utc, messagesx_cls=self.store,
                             watched_chat_ids={-100111})

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def send_new(self, chat, message, author, text):
        data = {'message': {'chat_id': chat, 'id': message, 'date': 1780000000,
                            'sender_id': {'@type': 'messageSenderUser', 'user_id': author},
                            'content': {'text': {'text': text}}}}
        await self.client.handlers['new'](self.client, SimpleNamespace(data=data))

    async def test_allowed_only_and_edit_delete(self):
        await self.send_new(-100999, 5, 15, 'PRIVATE SHOULD NOT SAVE')
        self.assertIsNone(self.store.get(chat_id=-100999, message_id=5))
        await self.send_new(-100111, 5, 15, '<important>')
        record = self.store.get(chat_id=-100111, message_id=5)
        self.assertEqual(record.author_id, 15)
        self.assertEqual(record.message_text, '<important>')
        await self.client.handlers['content'](self.client, SimpleNamespace(data={'chat_id': -100111, 'message_id': 5, 'new_content': {'text': {'text': 'after'}}}))
        self.assertEqual(self.store.get(chat_id=-100111, message_id=5).message_text, 'after')
        self.assertEqual(len(self.bot.sent), 1)
        self.assertIn('&lt;important&gt;', self.bot.sent[0][1])
        await self.client.handlers['edited'](self.client, SimpleNamespace(data={'chat_id': -100111, 'message_id': 5}))
        self.assertEqual(len(self.bot.sent), 2)  # 'getMessage' mocked with a newer version
        await self.client.handlers['edited'](self.client, SimpleNamespace(data={'chat_id': -100111, 'message_id': 5}))
        self.assertEqual(len(self.bot.sent), 2)  # idempotent repeated edit event
        await self.client.handlers['deleted'](self.client, SimpleNamespace(data={'chat_id': -100111, 'message_ids': [5], 'is_permanent': True}))
        self.assertEqual(len(self.bot.sent), 3)
        self.assertIsNone(self.store.get(chat_id=-100111, message_id=5))

    async def test_ambiguous_delete_and_anonymous_sender_are_ignored(self):
        await self.send_new(-100111, 9, 18, 'exists')
        await self.client.handlers['deleted'](self.client, SimpleNamespace(data={'message_ids': [9], 'is_permanent': True}))
        self.assertIsNotNone(self.store.get(chat_id=-100111, message_id=9))
        await self.client.handlers['deleted'](self.client, SimpleNamespace(data={'chat_id': -100111, 'message_ids': [9], 'is_permanent': False}))
        self.assertIsNotNone(self.store.get(chat_id=-100111, message_id=9))
        anonymous = {'message': {'chat_id': -100111, 'id': 10, 'date': 1780000000,
                                 'sender_id': {'@type': 'messageSenderChat', 'chat_id': -100111},
                                 'content': {'text': {'text': 'anonymous'}}}}
        await self.client.handlers['new'](self.client, SimpleNamespace(data=anonymous))
        self.assertIsNone(self.store.get(chat_id=-100111, message_id=10))


if __name__ == '__main__':
    unittest.main()
