"""Offline aiogram command regression tests."""
import os,tempfile,unittest
from pathlib import Path
from datetime import datetime,timezone
from unittest.mock import AsyncMock,patch
from aiogram import Bot,Dispatcher
from aiogram.types import Chat,Message,MessageEntity,Update,User
import main
class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def dispatch(self,text,uid=10,test_id=20):
        bot=Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi')
        bot.me=AsyncMock(return_value=User(id=123456789,is_bot=True,first_name='Test',username='ChatMonitorPrivateBot'))
        bot.session=AsyncMock(return_value=True)
        dp=Dispatcher();dp.include_router(main.build_router(10,test_id))
        m=Message(message_id=1,date=datetime.now(timezone.utc),chat=Chat(id=-100123,type='supergroup',title='Test'),from_user=User(id=uid,is_bot=False,first_name='Test') if uid else None,text=text,entities=[MessageEntity(type='bot_command',offset=0,length=len(text))])
        await dp.feed_update(bot,Update(update_id=1,message=m))
        return bot.session
    async def test_owner_commands(self):
        for text in ['/id','/chatid','/id@ChatMonitorPrivateBot','/chatid@ChatMonitorPrivateBot']:
            session=await self.dispatch(text)
            self.assertEqual(session.await_count,1)
            self.assertIn('-100123',session.call_args.args[1].text)
    async def test_test_account(self):
        self.assertEqual((await self.dispatch('/id',20)).await_count,1)
    async def test_unauthorized_and_other_bot(self):
        for uid,test_id in [(30,20),(20,0),(0,20)]:
            self.assertEqual((await self.dispatch('/id',uid,test_id)).await_count,0)
        self.assertEqual((await self.dispatch('/id@OtherBot')).await_count,0)
    async def test_pending_preserved(self):
        bot=AsyncMock();bot.get_me.return_value.username='ChatMonitorPrivateBot'
        with patch.object(main,'Dispatcher') as factory:
            factory.return_value.start_polling=AsyncMock()
            await main.chat_id_only(bot,10,20)
        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
    def test_discovery_ignores_invalid_group_setting(self):
        old=os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            try:
                os.chdir(d)
                Path('config.ini').write_text('[telegram]\nuser_id = 10\n[monitor]\nchat_ids = invalid\n')
                self.assertEqual(main.read_settings(discovery=True)[3],set())
                with self.assertRaises(ValueError):main.read_settings()
            finally:os.chdir(old)
if __name__=='__main__':unittest.main()
