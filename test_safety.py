"""Offline regression tests. No Telegram client is instantiated."""
import ast
import asyncio
import ctypes
import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree
import test_monitor as base

class RegressionTests(base.HandlerTests):
    async def event(self, kind, **data):
        await self.client.handlers[kind](self.client, SimpleNamespace(data=data))

    async def test_long_html_and_unicode(self):
        await self.send_new(-100111,20,7,'<&😀>'*2000)
        await self.event('content',chat_id=-100111,message_id=20,new_content={'text':{'text':'&<new>😀'*2000}})
        self.assertEqual(len(self.bot.sent),1)
        parsed=ElementTree.fromstring('<root>'+self.bot.sent[0][1]+'</root>')
        plain=''.join(parsed.itertext())
        self.assertLess(len(plain.encode('utf-16-le'))//2,4096)
        self.assertIn('After:',plain)
        self.assertIn('…',plain)

    async def test_outside_allowlist_never_queries_or_notifies(self):
        calls=[]
        async def forbidden(*args): calls.append(args); raise AssertionError('outside scope')
        self.subject._raw_request=forbidden
        self.subject._is_bot_user=forbidden
        self.subject._get_user_fullname=forbidden
        await self.send_new(-999,20,7,'outside')
        await self.event('edited',chat_id=-999,message_id=20)
        await self.event('deleted',chat_id=-999,message_ids=[20],is_permanent=True)
        self.assertEqual(calls,[])
        self.assertEqual(self.bot.sent,[])

    async def test_unverified_account_and_empty_allowlist_deny_all(self):
        self.subject._tracking_ready=False
        await self.send_new(-100111,20,7,'unverified')
        self.assertIsNone(self.store.get(chat_id=-100111,message_id=20))
        self.subject._tracking_ready=True
        base.load_tdlib_handler()(self.subject,self.bot,99,None,self.store,set())
        await self.send_new(-100111,20,7,'empty')
        self.assertIsNone(self.store.get(chat_id=-100111,message_id=20))

    async def test_caption_edit_and_duplicate_deletions(self):
        await self.send_new(-100111,20,7,'original')
        await self.event('content',chat_id=-100111,message_id=20,new_content={'@type':'messagePhoto','caption':{'text':'caption'},'photo':{'sizes':[]}})
        self.assertEqual(self.store.get(chat_id=-100111,message_id=20).message_text,'[photo] caption')
        for _ in range(2):
            await self.event('deleted',chat_id=-100111,message_ids=[20,20],is_permanent=True)
        self.assertEqual(len(self.bot.sent),2)

    async def race(self,kind):
        entered=asyncio.Event();release=asyncio.Event()
        async def slow(uid): entered.set();await release.wait();return 'Test author'
        self.subject._get_user_fullname=slow
        new=asyncio.create_task(self.send_new(-100111,20,7,'original'))
        await asyncio.wait_for(entered.wait(),1)
        data={'chat_id':-100111}
        if kind=='deleted': data.update(message_ids=[20],is_permanent=True)
        else: data.update(message_id=20,new_content={'text':{'text':'after'}})
        change=asyncio.create_task(self.event(kind,**data))
        await asyncio.sleep(0);release.set()
        await asyncio.wait_for(asyncio.gather(new,change),2)
        self.assertEqual(len(self.bot.sent),1)

    async def test_new_delete_race(self):
        await self.race('deleted')
        self.assertIsNone(self.store.get(chat_id=-100111,message_id=20))

    async def test_new_edit_race(self):
        await self.race('content')
        self.assertEqual(self.store.get(chat_id=-100111,message_id=20).message_text,'after')

    async def test_notification_failure_preserves_version_for_retry(self):
        await self.send_new(-100111,20,7,'original')
        original=self.bot.send_message
        async def fail(*args,**kwargs): raise RuntimeError('test failure')
        self.bot.send_message=fail
        data={'chat_id':-100111,'message_id':20,'new_content':{'text':{'text':'after'}}}
        await self.event('content',**data)
        self.assertEqual(self.store.get(chat_id=-100111,message_id=20).message_text,'original')
        self.bot.send_message=original
        await self.event('content',**data)
        self.assertEqual(self.store.get(chat_id=-100111,message_id=20).message_text,'after')
        self.assertEqual(len(self.bot.sent),1)

    async def test_disappearing_and_bot_messages_ignored(self):
        await self.event('new',message={'chat_id':-100111,'id':20,'sender_id':{'@type':'messageSenderUser','user_id':7},'self_destruct_type':{'@type':'messageSelfDestructTypeTimer'},'content':{'text':{'text':'excluded'}}})
        self.assertIsNone(self.store.get(chat_id=-100111,message_id=20))
        async def bot(uid): return True
        self.subject._is_bot_user=bot
        await self.send_new(-100111,20,7,'bot')
        self.assertIsNone(self.store.get(chat_id=-100111,message_id=20))

class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_start_never_reads_credentials(self):
        tree=ast.parse(Path('main.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='main')
        def forbidden(): raise AssertionError('Credentials must not be read')
        ns={'sys':SimpleNamespace(argv=['main.py']),'read_settings':forbidden}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'main.py','exec'),ns)
        await ns['main']()

    async def test_native_callback_abi_without_loading_tdlib(self):
        tree=ast.parse(Path('tdlib_userbot.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_patched_core_init')
        class Function:
            def __init__(self): self.calls=[]
            def __call__(self,*args): self.calls.append(args)
        lib=SimpleNamespace(**{name:Function() for name in ['td_create_client_id','td_receive','td_send','td_execute','td_set_log_message_callback']})
        ns={'logging':logging}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'tdlib_userbot.py','exec'),ns)
        with tempfile.NamedTemporaryFile() as f,patch('ctypes.CDLL',return_value=lib):
            obj=SimpleNamespace();ns['_patched_core_init'](obj,f.name)
        self.assertEqual(len(lib.td_set_log_message_callback.calls[0]),2)
        self.assertIs(lib.td_set_log_message_callback.argtypes[0],ctypes.c_int)
        self.assertIs(lib.td_set_log_message_callback.calls[0][1],obj._log_callback)

if __name__=='__main__': unittest.main()
